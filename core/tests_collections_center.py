from datetime import date, timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core.collections_center import build_collections_center
from core.models import AuditLog, Company, PracticeTask, UserCompanyAccess
from ledger.models import AccountGroup, Ledger
from vouchers.models import Voucher, VoucherItem


class CollectionsCenterTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Collections Co",
            gstin="27COLLC0000C1Z5",
            short_code="CC",
            invoice_email_from_name="Collections Team",
            invoice_email_from_address="collections@example.com",
            invoice_email_reply_to="reply@example.com",
            payment_reminder_email_subject="Reminder {voucher_number}: {outstanding}",
            payment_reminder_email_body="Dear {client_name}, pay {outstanding}. {aging_line}",
        )
        self.user = get_user_model().objects.create_superuser(
            email="collections-admin@example.com",
            password="secret",
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        self.asset_group = AccountGroup.objects.create(
            company=self.company,
            name="Sundry Debtors",
            nature="Asset",
        )
        self.income_group = AccountGroup.objects.create(
            company=self.company,
            name="Sales Accounts",
            nature="Income",
        )
        self.customer = Ledger.objects.create(
            company=self.company,
            name="Priority Customer",
            account_group=self.asset_group,
            email="pay@example.com",
            whatsapp_number="+91 98765 43210",
            credit_limit=Decimal("1500.00"),
        )
        self.sales = Ledger.objects.create(
            company=self.company,
            name="Sales",
            account_group=self.income_group,
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def _sales_invoice(self, amount, invoice_date, due_date):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=invoice_date,
            due_date=due_date,
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.customer,
            entry_type="DR",
            amount=amount,
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.sales,
            entry_type="CR",
            amount=amount,
        )
        voucher.approve(None)
        voucher.sync_outstanding()
        voucher.refresh_from_db()
        return voucher

    def test_collections_center_builds_risk_and_contact_rows(self):
        today = timezone.localdate()
        self._sales_invoice(Decimal("1000.00"), date(2026, 4, 1), today - timedelta(days=100))
        Ledger.objects.filter(pk=self.customer.pk).update(credit_limit=Decimal("700.00"))
        self.customer.refresh_from_db()

        center = build_collections_center(Company.objects.filter(pk=self.company.pk), as_of_date=today)

        self.assertEqual(center["totals"]["invoice_count"], 1)
        self.assertEqual(center["totals"]["critical_count"], 1)
        self.assertEqual(center["totals"]["email_ready"], 1)
        self.assertEqual(center["totals"]["whatsapp_ready"], 1)
        row = center["rows"][0]
        self.assertEqual(row["party_name"], self.customer.name)
        self.assertEqual(row["status"], "critical")
        self.assertIn("wa.me", row["whatsapp_url"])
        self.assertEqual(center["party_rows"][0]["status"], "credit_exceeded")

    def test_collections_center_renders_and_exports_csv(self):
        today = timezone.localdate()
        self._sales_invoice(Decimal("1000.00"), date(2026, 4, 1), today - timedelta(days=10))

        response = self.client.get(reverse("core:collections_command_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Collections Center")
        self.assertContains(response, "Priority Customer")
        self.assertContains(response, "Invoice Action Queue")

        csv_response = self.client.get(reverse("core:collections_command_center"), {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(csv_response["Content-Type"], "text/csv; charset=utf-8")
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,Client,Invoice", csv_text)
        self.assertIn("Priority Customer", csv_text)

    def test_collections_center_creates_idempotent_tasks(self):
        today = timezone.localdate()
        voucher = self._sales_invoice(Decimal("1000.00"), date(2026, 4, 1), today - timedelta(days=10))

        response = self.client.post(reverse("core:collections_command_center"), {"action": "create_tasks"})

        self.assertEqual(response.status_code, 302)
        task = PracticeTask.objects.get(company=self.company, reference=f"COLLECT:{self.company.pk}:{voucher.pk}")
        self.assertEqual(task.priority, PracticeTask.PRIORITY_HIGH)
        self.assertIn("Priority Customer", task.description)

        self.client.post(reverse("core:collections_command_center"), {"action": "create_tasks"})
        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference=f"COLLECT:{self.company.pk}:{voucher.pk}").count(),
            1,
        )

    @patch("core.collections_center._send_collection_email")
    def test_collections_center_sends_selected_email_reminders(self, send_email):
        today = timezone.localdate()
        voucher = self._sales_invoice(Decimal("1000.00"), date(2026, 4, 1), today - timedelta(days=10))

        response = self.client.post(
            reverse("core:collections_command_center"),
            {"action": "send_email", "invoice_ids": [str(voucher.pk)]},
        )

        self.assertEqual(response.status_code, 302)
        send_email.assert_called_once()
        audit = AuditLog.objects.filter(
            company=self.company,
            model_name="Voucher",
            record_id=voucher.pk,
            new_data__source="collections_command_center",
        ).first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.new_data["collection_reminder_sent_to"], "pay@example.com")
