import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from core.models import AuditLog, Company, PracticeTask, UserCompanyAccess
from inventory.models import (
    CompanySettings as InventorySettings,
    StockItem,
    StockLedger,
    TaxRate,
    VoucherStockItem,
)
from ledger.models import AccountGroup, Ledger
from reports.utils import get_ledger_history, get_receivables_aging
from tds.models import TDSEntry
from vouchers.forms import VoucherItemForm
from vouchers.models import Voucher, VoucherItem
from vouchers.suggestion_engine import get_suggestions


class AccountingBackboneTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Accounting Test Co",
            gstin="27AAAAA0000A1Z5",
            short_code="AT",
            invoice_email_from_name="Accounts Team",
            invoice_email_from_address="accounts@example.com",
            invoice_email_reply_to="billing@example.com",
            invoice_email_subject="Invoice {voucher_number} for {client_name}",
            invoice_email_body="Dear {client_name}, please find {voucher_number} for {amount}.",
        )
        self.user = get_user_model().objects.create_user(
            email="voucher-email@example.com",
            password="secret",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.company,
            role="Accountant",
        )
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
        )
        self.asset_group = AccountGroup.objects.create(
            company=self.company, name="Sundry Debtors", nature="Asset"
        )
        self.income_group = AccountGroup.objects.create(
            company=self.company, name="Sales Accounts", nature="Income"
        )
        self.bank_group = AccountGroup.objects.create(
            company=self.company, name="Bank Accounts", nature="Asset"
        )
        self.liability_group = AccountGroup.objects.create(
            company=self.company, name="Sundry Creditors", nature="Liability"
        )
        self.expense_group = AccountGroup.objects.create(
            company=self.company, name="Purchase Accounts", nature="Expense"
        )

        self.customer = Ledger.objects.create(
            company=self.company, name="Customer A", account_group=self.asset_group
        )
        self.bank = Ledger.objects.create(
            company=self.company, name="Bank", account_group=self.bank_group
        )
        self.sales = Ledger.objects.create(
            company=self.company, name="Sales", account_group=self.income_group
        )
        self.vendor = Ledger.objects.create(
            company=self.company,
            name="Vendor A",
            account_group=self.liability_group,
            tds_section="194C",
            tds_rate=Decimal("10.00"),
            tds_threshold=Decimal("100.00"),
            pan_number="ABCDE1234F",
        )
        self.purchase = Ledger.objects.create(
            company=self.company, name="Purchases", account_group=self.expense_group
        )
        self.tax_rate = TaxRate.objects.create(rate=Decimal("18.00"), description="GST 18%")
        self.stock_item = StockItem.objects.create(
            company=self.company,
            name="Test Product",
            tax_rate=self.tax_rate,
            selling_price=Decimal("1000.00"),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def _sales_invoice(self, amount, invoice_date=date(2026, 4, 1)):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=invoice_date,
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

    def test_auto_gst_lines_balance_and_update_outstanding(self):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 1),
            place_of_supply="27",
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.customer,
            entry_type="DR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("1000.00"),
            stock_item=self.stock_item,
            quantity=Decimal("1.000"),
            rate=Decimal("1000.00"),
        )

        voucher.create_tax_lines()
        voucher.validate_balance()
        voucher.sync_outstanding()
        voucher.refresh_from_db()

        self.assertEqual(voucher.cgst_amount, Decimal("90.00"))
        self.assertEqual(voucher.sgst_amount, Decimal("90.00"))
        self.assertEqual(voucher.igst_amount, Decimal("0.00"))
        self.assertEqual(voucher.total_tax, Decimal("180.00"))
        self.assertEqual(voucher.total_debit(), Decimal("1180.00"))
        self.assertEqual(voucher.total_credit(), Decimal("1180.00"))
        self.assertEqual(voucher.outstanding_amount, Decimal("1180.00"))

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="System <system@example.com>",
    )
    def test_email_invoice_saves_client_email_and_attaches_pdf(self):
        voucher = self._sales_invoice(Decimal("500.00"))

        response = self.client.post(
            reverse("vouchers:email_invoice", args=[voucher.pk]),
            {"recipient_email": "client@example.com"},
        )

        self.assertRedirects(response, reverse("vouchers:detail", args=[voucher.pk]))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.email, "client@example.com")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["client@example.com"])
        self.assertEqual(message.from_email, "Accounts Team <accounts@example.com>")
        self.assertEqual(message.reply_to, ["billing@example.com"])
        self.assertIn(voucher.number, message.subject)
        self.assertEqual(len(message.attachments), 1)
        self.assertTrue(message.attachments[0][0].startswith("Invoice_"))
        self.assertEqual(message.attachments[0][2], "application/pdf")
        audit_log = AuditLog.objects.filter(
            company=self.company,
            action=AuditLog.ACTION_UPDATE,
            model_name="Voucher",
            record_id=voucher.pk,
        ).order_by("-timestamp").first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.new_data["invoice_email_sent_to"], "client@example.com")
        self.assertEqual(audit_log.new_data["client_ledger"], self.customer.name)

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="System <system@example.com>",
    )
    def test_payment_reminder_saves_client_email_and_logs_audit(self):
        voucher = self._sales_invoice(Decimal("500.00"))
        Voucher.objects.filter(pk=voucher.pk).update(due_date=timezone.localdate() - timedelta(days=5))
        voucher.refresh_from_db()
        self.company.payment_reminder_email_subject = "Reminder {voucher_number}: {outstanding}"
        self.company.payment_reminder_email_body = "Dear {client_name}, please pay {outstanding}. {aging_line}"
        self.company.save(update_fields=["payment_reminder_email_subject", "payment_reminder_email_body"])

        response = self.client.post(
            reverse("vouchers:payment_reminder", args=[voucher.pk]),
            {"recipient_email": "collections@example.com"},
        )

        self.assertRedirects(response, reverse("vouchers:outstanding"))
        self.customer.refresh_from_db()
        self.assertEqual(self.customer.email, "collections@example.com")
        self.assertEqual(len(mail.outbox), 1)
        message = mail.outbox[0]
        self.assertEqual(message.to, ["collections@example.com"])
        self.assertEqual(message.subject, f"Reminder {voucher.number}: Rs. 500.00")
        self.assertIn("please pay Rs. 500.00", message.body)
        self.assertEqual(len(message.attachments), 1)
        audit_log = AuditLog.objects.filter(
            company=self.company,
            action=AuditLog.ACTION_UPDATE,
            model_name="Voucher",
            record_id=voucher.pk,
        ).order_by("-timestamp").first()
        self.assertIsNotNone(audit_log)
        self.assertEqual(audit_log.new_data["payment_reminder_sent_to"], "collections@example.com")
        self.assertEqual(audit_log.new_data["client_ledger"], self.customer.name)

    def test_outstanding_statement_totals_follow_status_filter_and_exports_csv(self):
        settled_invoice = self._sales_invoice(Decimal("1000.00"), date(2026, 4, 1))
        open_invoice = self._sales_invoice(Decimal("500.00"), date(2026, 4, 2))
        receipt = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 4, 10),
        )
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=self.bank,
            entry_type="DR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=self.customer,
            entry_type="CR",
            amount=Decimal("1000.00"),
            reference_voucher=settled_invoice,
        )
        receipt.approve(None)
        settled_invoice.sync_outstanding()
        open_invoice.sync_outstanding()

        response = self.client.get(reverse("vouchers:outstanding"), {
            "type": "Sales",
            "status": "outstanding",
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["total_invoiced"], Decimal("500.00"))
        self.assertEqual(response.context["total_outstanding"], Decimal("500.00"))
        self.assertEqual(len(response.context["rows"]), 1)
        self.assertEqual(response.context["rows"][0]["party_name"], self.customer.name)

        csv_response = self.client.get(reverse("vouchers:outstanding"), {
            "type": "Sales",
            "status": "outstanding",
            "export": "csv",
        })
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(csv_response["Content-Type"], "text/csv")
        csv_body = csv_response.content.decode()
        self.assertIn("Invoice No.,Type,Party,Email,GSTIN", csv_body)
        self.assertIn(self.customer.name, csv_body)
        self.assertIn("500.00", csv_body)

    def test_collection_tasks_created_for_overdue_sales_invoices(self):
        voucher = self._sales_invoice(Decimal("500.00"))
        Voucher.objects.filter(pk=voucher.pk).update(due_date=timezone.localdate() - timedelta(days=10))
        voucher.refresh_from_db()

        response = self.client.post(reverse("vouchers:collection_tasks"))

        self.assertRedirects(response, reverse("vouchers:outstanding"))
        task = PracticeTask.objects.get(company=self.company, reference=f"COLLECT:{self.company.pk}:{voucher.pk}")
        self.assertEqual(task.task_type, PracticeTask.TYPE_OTHER)
        self.assertEqual(task.priority, PracticeTask.PRIORITY_HIGH)
        self.assertIn(voucher.number, task.title)
        self.assertIn("Outstanding: Rs.500.00", task.description)

        duplicate_response = self.client.post(reverse("vouchers:collection_tasks"))
        self.assertRedirects(duplicate_response, reverse("vouchers:outstanding"))
        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference=f"COLLECT:{self.company.pk}:{voucher.pk}").count(),
            1,
        )

    def test_receivables_aging_tracks_each_invoice_once(self):
        first_invoice = self._sales_invoice(Decimal("1000.00"))
        second_invoice = self._sales_invoice(Decimal("500.00"), date(2026, 4, 2))

        receipt = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 4, 10),
        )
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=self.bank,
            entry_type="DR",
            amount=Decimal("400.00"),
        )
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=self.customer,
            entry_type="CR",
            amount=Decimal("400.00"),
            reference_voucher=first_invoice,
        )
        receipt.approve(None)

        early_report = get_receivables_aging(self.company, date(2026, 4, 5))
        self.assertEqual(early_report["totals"]["grand"], Decimal("1500.00"))
        self.assertEqual(
            first_invoice.calculate_outstanding(
                as_of_date=date(2026, 4, 5),
                approved_only=True,
            ),
            Decimal("1000.00"),
        )

        report = get_receivables_aging(self.company, date(2026, 5, 31))
        outstanding = sorted(
            entry["outstanding"]
            for bucket in report["buckets"].values()
            for entry in bucket
        )
        entries = [
            entry
            for bucket in report["buckets"].values()
            for entry in bucket
        ]

        self.assertEqual(outstanding, [Decimal("500.00"), Decimal("600.00")])
        self.assertEqual(report["totals"]["grand"], Decimal("1100.00"))
        self.assertTrue(all(entry["ledger_name"] == self.customer.name for entry in entries))
        self.assertTrue(all("due_date" in entry for entry in entries))
        self.assertTrue(all("original" in entry and "settled" in entry for entry in entries))
        second_invoice.refresh_from_db()
        self.assertEqual(second_invoice.outstanding_amount, Decimal("500.00"))

        csv_response = self.client.get(reverse("reports:receivables_aging"), {
            "as_of_date": "2026-05-31",
            "export": "csv",
        })
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(csv_response["Content-Type"], "text/csv")
        self.assertIn("Bucket,Voucher,Customer,Email", csv_response.content.decode())

    def test_pending_settlement_does_not_reduce_invoice_outstanding(self):
        invoice = self._sales_invoice(Decimal("1000.00"))
        receipt = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 4, 10),
            status="PENDING",
        )
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=self.bank,
            entry_type="DR",
            amount=Decimal("400.00"),
        )
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=self.customer,
            entry_type="CR",
            amount=Decimal("400.00"),
            reference_voucher=invoice,
        )

        invoice.sync_outstanding()
        invoice.refresh_from_db()
        self.assertEqual(invoice.outstanding_amount, Decimal("1000.00"))

        receipt.approve(None)
        invoice.refresh_from_db()
        self.assertEqual(invoice.outstanding_amount, Decimal("600.00"))

    def test_inventory_movements_follow_voucher_approval_state(self):
        InventorySettings.objects.create(
            company=self.company,
            prevent_negative_stock=False,
        )
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 1),
            status="PENDING",
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("50.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.vendor,
            entry_type="CR",
            amount=Decimal("50.00"),
        )
        VoucherStockItem.objects.create(
            voucher=voucher,
            stock_item=self.stock_item,
            quantity=Decimal("1.000"),
            rate=Decimal("50.00"),
        )

        voucher.sync_inventory()
        self.assertFalse(StockLedger.objects.filter(voucher=voucher).exists())

        voucher.approve(None)
        self.assertEqual(
            StockLedger.objects.get(voucher=voucher, stock_item=self.stock_item).quantity,
            Decimal("1.000"),
        )

        voucher.unapprove(None)
        self.assertFalse(StockLedger.objects.filter(voucher=voucher).exists())

    def test_ledger_history_includes_opposite_ledger_particulars(self):
        self._sales_invoice(Decimal("1000.00"))

        history = get_ledger_history(
            self.company,
            self.customer.id,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 4, 30),
        )

        self.assertEqual(len(history["history"]), 1)
        self.assertIn("Sales", history["history"][0]["particulars"])
        self.assertEqual(history["closing_balance"], Decimal("1000.00"))
        self.assertEqual(history["closing_type"], "Dr")

    def test_accounting_line_form_does_not_require_stock_quantity_or_rate(self):
        form = VoucherItemForm(
            data={
                "ledger": self.sales.id,
                "entry_type": "CR",
                "amount": "1000.00",
                "narration": "",
                "cost_center": "",
                "reference_voucher": "",
                "stock_item": "",
                "godown": "",
                "batch": "",
                "quantity": "",
                "rate": "",
            },
            company=self.company,
        )

        self.assertTrue(form.is_valid(), form.errors.as_data())
        self.assertEqual(form.cleaned_data["quantity"], Decimal("0.000"))
        self.assertEqual(form.cleaned_data["rate"], Decimal("0.00"))

    def test_stock_line_requires_quantity_when_stock_item_is_selected(self):
        form = VoucherItemForm(
            data={
                "ledger": self.sales.id,
                "entry_type": "CR",
                "amount": "1000.00",
                "narration": "",
                "cost_center": "",
                "reference_voucher": "",
                "stock_item": self.stock_item.id,
                "godown": "",
                "batch": "",
                "quantity": "",
                "rate": "1000.00",
            },
            company=self.company,
        )

        self.assertFalse(form.is_valid())
        self.assertIn("quantity", form.errors)

    def test_ledger_choices_show_group_and_nature(self):
        form = VoucherItemForm(company=self.company)
        label = form.fields["ledger"].label_from_instance(self.sales)

        self.assertIn("Sales Accounts", label)
        self.assertIn("Income", label)

    def test_suggestions_include_account_nature_for_dr_cr_logic(self):
        suggestions = get_suggestions(self.company, "sales 1000")

        self.assertTrue(suggestions)
        self.assertEqual(suggestions[0]["nature"], "Income")

    def test_auto_tds_creates_register_entry_and_keeps_voucher_balanced(self):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 1),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.vendor,
            entry_type="CR",
            amount=Decimal("1000.00"),
        )

        voucher.check_tds_deduction()

        self.assertTrue(voucher.is_balanced())
        self.assertEqual(TDSEntry.objects.filter(voucher=voucher).count(), 1)
        entry = TDSEntry.objects.get(voucher=voucher)
        self.assertEqual(entry.tds_amount, Decimal("100.00"))
        self.assertEqual(entry.pan_number, "ABCDE1234F")

    @override_settings(WHATSAPP_WEBHOOK_TOKEN="secret-token")
    def test_whatsapp_webhook_requires_token_before_approval(self):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Journal",
            date=date(2026, 4, 1),
            status="PENDING",
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.bank,
            entry_type="DR",
            amount=Decimal("100.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.sales,
            entry_type="CR",
            amount=Decimal("100.00"),
        )

        url = reverse("vouchers:whatsapp_webhook")
        payload = {"body": "YES", "voucher_id": voucher.pk}

        denied = self.client.post(url, data=json.dumps(payload), content_type="application/json")
        self.assertEqual(denied.status_code, 403)

        approved = self.client.post(
            url,
            data=json.dumps(payload),
            content_type="application/json",
            HTTP_X_WEBHOOK_TOKEN="secret-token",
        )
        self.assertEqual(approved.status_code, 200)
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, "APPROVED")

    @override_settings(WHATSAPP_WEBHOOK_TOKEN="secret-token")
    def test_whatsapp_approval_runs_tds_before_hard_lock(self):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 1),
            status="PENDING",
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.vendor,
            entry_type="CR",
            amount=Decimal("1000.00"),
        )

        response = self.client.post(
            reverse("vouchers:whatsapp_webhook"),
            data=json.dumps({"body": "YES", "voucher_id": voucher.pk}),
            content_type="application/json",
            HTTP_X_WEBHOOK_TOKEN="secret-token",
        )

        self.assertEqual(response.status_code, 200, response.content)
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, "APPROVED")
        self.assertTrue(voucher.items.filter(ledger__name__icontains="TDS Payable").exists())
        self.assertTrue(voucher.is_balanced())

    def test_approved_voucher_is_hard_locked_until_unapproved(self):
        voucher = self._sales_invoice(Decimal("1000.00"))

        voucher.narration = "tampered"
        with self.assertRaises(ValidationError):
            voucher.save()

        with self.assertRaises(ValidationError):
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=self.sales,
                entry_type="CR",
                amount=Decimal("1.00"),
            )

        voucher.refresh_from_db()
        voucher.unapprove(None)
        voucher.narration = "allowed after unapproval"
        voucher.save()
        voucher.refresh_from_db()
        self.assertEqual(voucher.status, "PENDING")
        self.assertEqual(voucher.narration, "allowed after unapproval")

    def test_voucher_number_is_system_assigned_and_immutable(self):
        voucher = Voucher(
            company=self.company,
            voucher_type="Journal",
            date=date(2026, 4, 1),
            number="MANUAL-0001",
        )
        voucher.save()

        self.assertNotEqual(voucher.number, "MANUAL-0001")
        assigned_number = voucher.number
        voucher.number = "MANUAL-0002"
        with self.assertRaises(ValidationError):
            voucher.save(update_fields=["number"])
        voucher.refresh_from_db()
        self.assertEqual(voucher.number, assigned_number)

    def test_statutory_tds_threshold_blocks_until_tds_line_exists(self):
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=date(2026, 4, 1),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.purchase,
            entry_type="DR",
            amount=Decimal("1000.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.vendor,
            entry_type="CR",
            amount=Decimal("1000.00"),
        )

        with self.assertRaises(ValidationError):
            voucher.clean()

        voucher.check_tds_deduction()
        voucher.clean()
        self.assertTrue(voucher.items.filter(ledger__name__icontains="TDS Payable").exists())

    def test_company_level_negative_stock_blocks_sales_lines(self):
        InventorySettings.objects.create(
            company=self.company,
            prevent_negative_stock=True,
        )
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 4, 1),
        )

        with self.assertRaises(ValidationError):
            VoucherItem.objects.create(
                voucher=voucher,
                ledger=self.sales,
                entry_type="CR",
                amount=Decimal("100.00"),
                stock_item=self.stock_item,
                quantity=Decimal("1.000"),
                rate=Decimal("100.00"),
            )
