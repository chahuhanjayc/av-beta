import json
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from core.models import AuditLog, Company, PracticeTask, UserCompanyAccess
from gstr2b.models import PortalGSTR2BEntry
from gstr2b.parser import GSTR2BParser
from ledger.models import AccountGroup, Ledger
from vouchers.models import Voucher, VoucherItem


def _sample_2b_json(taxable="100.00", cgst="9.00", sgst="9.00", invoice_value="118.00"):
    return {
        "b2b": [
            {
                "ctin": "27ABCDE1234F1Z5",
                "trdnm": "Test Supplier",
                "inv": [
                    {
                        "inum": "INV-001",
                        "dt": "01-05-2026",
                        "val": invoice_value,
                        "itms": [
                            {
                                "itm_det": {
                                    "txval": taxable,
                                    "camt": cgst,
                                    "samt": sgst,
                                    "iamt": "0.00",
                                }
                            }
                        ],
                    }
                ],
            }
        ]
    }


class GSTR2BParserTests(TestCase):
    def test_parser_uses_taxable_value_not_invoice_total_for_taxable_value(self):
        parsed = GSTR2BParser.parse_json(json.dumps(_sample_2b_json()))

        self.assertEqual(parsed[0]["taxable_value"], Decimal("100.00"))
        self.assertEqual(parsed[0]["invoice_value"], Decimal("118.00"))
        self.assertEqual(parsed[0]["tax_amount"], Decimal("18.00"))


class GSTR2BUploadTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="2B Test Co",
            gstin="27TESTC0000A1Z5",
            short_code="2BT",
        )
        self.user = get_user_model().objects.create_user(
            email="gstr2b@example.com",
            password="CorrectHorseBatteryStaple123!",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.company,
            role="Admin",
        )
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def _purchase_voucher(
        self,
        *,
        invoice_number="INV-001",
        vendor_name="Test Supplier",
        vendor_gstin="27ABCDE1234F1Z5",
        vendor_email="",
        vendor_whatsapp="",
        voucher_date=date(2026, 5, 1),
        claimed=True,
    ):
        liability_group, _ = AccountGroup.objects.get_or_create(
            company=self.company,
            name="Sundry Creditors",
            defaults={"nature": "Liability"},
        )
        expense_group, _ = AccountGroup.objects.get_or_create(
            company=self.company,
            name="Purchases",
            defaults={"nature": "Expense"},
        )
        tax_group, _ = AccountGroup.objects.get_or_create(
            company=self.company,
            name="Input GST",
            defaults={"nature": "Tax"},
        )
        vendor, _ = Ledger.objects.get_or_create(
            company=self.company,
            name=vendor_name,
            defaults={
                "account_group": liability_group,
                "gstin": vendor_gstin,
                "email": vendor_email or None,
                "whatsapp_number": vendor_whatsapp or None,
            },
        )
        update_fields = []
        if vendor_email and vendor.email != vendor_email:
            vendor.email = vendor_email
            update_fields.append("email")
        if vendor_whatsapp and vendor.whatsapp_number != vendor_whatsapp:
            vendor.whatsapp_number = vendor_whatsapp
            update_fields.append("whatsapp_number")
        if update_fields:
            vendor.save(update_fields=[*update_fields, "updated_at"])
        purchase_ledger, _ = Ledger.objects.get_or_create(
            company=self.company,
            name="Purchase GST",
            defaults={"account_group": expense_group},
        )
        input_tax, _ = Ledger.objects.get_or_create(
            company=self.company,
            name="Input Tax",
            defaults={"account_group": tax_group},
        )
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Purchase",
            date=voucher_date,
            narration=f"Vendor invoice {invoice_number}",
            cgst_amount=Decimal("9.00"),
            sgst_amount=Decimal("9.00"),
            total_tax=Decimal("18.00"),
            is_itc_claimed=claimed,
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=purchase_ledger,
            entry_type="DR",
            amount=Decimal("100.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=input_tax,
            entry_type="DR",
            amount=Decimal("18.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=vendor,
            entry_type="CR",
            amount=Decimal("118.00"),
        )
        voucher.approve(self.user)
        voucher.refresh_from_db()
        return voucher

    def _portal_entry(self, **overrides):
        defaults = {
            "company": self.company,
            "gstin": "27ABCDE1234F1Z5",
            "supplier_name": "Test Supplier",
            "invoice_number": "INV-001",
            "invoice_date": date(2026, 5, 1),
            "taxable_value": Decimal("100.00"),
            "tax_amount": Decimal("18.00"),
            "is_matched": False,
            "match_status": "missing_in_books",
            "match_score": 0,
            "action_status": "new",
        }
        defaults.update(overrides)
        return PortalGSTR2BEntry.objects.create(**defaults)

    def _upload(self, payload):
        upload = SimpleUploadedFile(
            "gstr2b.json",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )
        return self.client.post(reverse("gstr2b:upload"), {"json_file": upload})

    def test_reupload_refreshes_existing_invoice_without_deleting_history(self):
        first_response = self._upload(_sample_2b_json())
        second_response = self._upload(
            _sample_2b_json(taxable="200.00", cgst="18.00", sgst="18.00", invoice_value="236.00")
        )

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(PortalGSTR2BEntry.objects.filter(company=self.company).count(), 1)

        entry = PortalGSTR2BEntry.objects.get(company=self.company, invoice_number="INV-001")
        self.assertEqual(entry.invoice_date, date(2026, 5, 1))
        self.assertEqual(entry.taxable_value, Decimal("200.00"))
        self.assertEqual(entry.tax_amount, Decimal("36.00"))

    def test_bulk_ims_action_updates_voucher_itc_and_audit_log(self):
        voucher = self._purchase_voucher(claimed=True)
        entry = self._portal_entry(
            is_matched=True,
            match_status="matched",
            matched_voucher=voucher,
            match_score=95,
            action_status="accepted",
        )

        response = self.client.post(
            reverse("gstr2b:bulk_action"),
            {
                "period": "2026-05",
                "entry_ids": [str(entry.pk)],
                "action_status": "pending",
                "action_note": "Vendor amendment expected",
            },
        )

        self.assertEqual(response.status_code, 302)
        entry.refresh_from_db()
        voucher.refresh_from_db()
        self.assertEqual(entry.action_status, "pending")
        self.assertEqual(entry.action_note, "Vendor amendment expected")
        self.assertFalse(voucher.is_itc_claimed)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PortalGSTR2BEntry",
                record_id=entry.pk,
                new_data__source="ims_action",
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="Voucher",
                record_id=voucher.pk,
                new_data__source="ims_action",
                new_data__is_itc_claimed=False,
            ).exists()
        )

        self.client.post(
            reverse("gstr2b:bulk_action"),
            {
                "period": "2026-05",
                "entry_ids": [str(entry.pk)],
                "action_status": "accepted",
            },
        )

        voucher.refresh_from_db()
        self.assertTrue(voucher.is_itc_claimed)

    def test_ims_tasks_can_be_created_for_portal_and_book_gaps(self):
        entry = self._portal_entry(invoice_number="INV-IMS-002")
        voucher = self._purchase_voucher(
            invoice_number="BOOK-001",
            vendor_name="Book Vendor",
            vendor_gstin="27BBCDE1234F1Z5",
            claimed=False,
        )

        response = self.client.post(
            reverse("gstr2b:create_tasks"),
            {
                "period": "2026-05",
                "entry_ids": [str(entry.pk)],
                "voucher_ids": [str(voucher.pk)],
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference=f"IMS2B:{entry.pk}",
                task_type=PracticeTask.TYPE_GST,
            ).exists()
        )
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference=f"IMSBOOK:{voucher.pk}",
                task_type=PracticeTask.TYPE_GST,
            ).exists()
        )

        self.client.post(
            reverse("gstr2b:create_tasks"),
            {
                "period": "2026-05",
                "entry_ids": [str(entry.pk)],
                "voucher_ids": [str(voucher.pk)],
            },
        )
        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference__startswith="IMS").count(),
            2,
        )

    def test_results_csv_export_includes_portal_entries(self):
        self._portal_entry(invoice_number="INV-CSV-001")

        response = self.client.get(
            reverse("gstr2b:results"),
            {"period": "2026-05", "export": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("INV-CSV-001", response.content.decode("utf-8"))

    def test_matched_pending_voucher_is_not_counted_as_missing_in_portal(self):
        voucher = self._purchase_voucher(claimed=False)
        self._portal_entry(
            is_matched=True,
            match_status="matched",
            matched_voucher=voucher,
            match_score=90,
            action_status="pending",
        )

        response = self.client.get(reverse("gstr2b:results"), {"period": "2026-05"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["missing_in_portal"], 0)
        self.assertEqual(response.context["result_counts"]["missing_in_portal"], 0)

    def test_vendor_register_merges_portal_and_book_gaps_by_gstin(self):
        self._portal_entry(invoice_number="PORTAL-GAP-001")
        self._purchase_voucher(
            invoice_number="BOOK-GAP-001",
            vendor_name="Test Supplier",
            vendor_gstin="27ABCDE1234F1Z5",
            claimed=False,
        )

        response = self.client.get(
            reverse("gstr2b:vendor_register"),
            {"from_period": "2026-05", "to_period": "2026-05"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context["summary"]["vendor_count"], 1)
        row = response.context["rows"][0]
        self.assertEqual(row["gstin"], "27ABCDE1234F1Z5")
        self.assertEqual(row["missing_books_count"], 1)
        self.assertEqual(row["missing_portal_count"], 1)
        self.assertEqual(row["itc_at_risk"], Decimal("36.00"))
        self.assertEqual(response.context["summary"]["itc_at_risk"], Decimal("36.00"))

    def test_vendor_register_task_creation_is_deduplicated(self):
        self._portal_entry(invoice_number="PORTAL-GAP-002")

        payload = {
            "from_period": "2026-05",
            "to_period": "2026-05",
            "vendor_keys": ["27ABCDE1234F1Z5"],
        }
        first_response = self.client.post(reverse("gstr2b:vendor_tasks"), payload)
        second_response = self.client.post(reverse("gstr2b:vendor_tasks"), payload)

        self.assertEqual(first_response.status_code, 302)
        self.assertEqual(second_response.status_code, 302)
        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference__startswith="IMSVENDOR").count(),
            1,
        )
        task = PracticeTask.objects.get(company=self.company, reference__startswith="IMSVENDOR")
        self.assertEqual(task.task_type, PracticeTask.TYPE_GST)
        self.assertIn("ITC at risk: Rs. 18.00", task.description)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                record_id=task.pk,
                new_data__source="vendor_gst_register",
            ).exists()
        )

    def test_vendor_register_csv_export(self):
        self._portal_entry(invoice_number="PORTAL-GAP-CSV")

        response = self.client.get(
            reverse("gstr2b:vendor_register"),
            {"from_period": "2026-05", "to_period": "2026-05", "export": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/csv", response["Content-Type"])
        self.assertIn("Test Supplier", response.content.decode("utf-8"))

    def test_vendor_followup_preview_includes_whatsapp_link(self):
        self._purchase_voucher(
            vendor_email="vendor@example.com",
            vendor_whatsapp="+919876543210",
            claimed=False,
        )

        response = self.client.get(
            reverse("gstr2b:vendor_followup"),
            {
                "vendor_key": "27ABCDE1234F1Z5",
                "from_period": "2026-05",
                "to_period": "2026-05",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Vendor GST Follow-up")
        self.assertContains(response, "vendor@example.com")
        self.assertContains(response, "https://wa.me/919876543210")

    @override_settings(
        EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend",
        DEFAULT_FROM_EMAIL="System <system@example.com>",
    )
    def test_vendor_followup_email_saves_contact_creates_task_and_audit(self):
        self._purchase_voucher(claimed=False)
        self._portal_entry(invoice_number="PORTAL-GAP-EMAIL")

        response = self.client.post(
            reverse("gstr2b:vendor_followup"),
            {
                "vendor_key": "27ABCDE1234F1Z5",
                "from_period": "2026-05",
                "to_period": "2026-05",
                "recipient_email": "gst.vendor@example.com",
                "whatsapp_number": "98765 43210",
                "subject": "GST follow-up",
                "message": "Please resolve GST ITC gap.",
                "channel": "email",
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["gst.vendor@example.com"])
        self.assertEqual(mail.outbox[0].subject, "GST follow-up")

        ledger = Ledger.objects.get(company=self.company, gstin="27ABCDE1234F1Z5")
        self.assertEqual(ledger.email, "gst.vendor@example.com")
        self.assertEqual(ledger.whatsapp_number, "+919876543210")

        task = PracticeTask.objects.get(company=self.company, reference__startswith="IMSVENDOR")
        self.assertEqual(task.status, PracticeTask.STATUS_IN_PROGRESS)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="Ledger",
                record_id=ledger.pk,
                new_data__source="vendor_gst_register",
                new_data__channel="email",
                new_data__recipient_email="gst.vendor@example.com",
            ).exists()
        )

    def test_vendor_followup_whatsapp_updates_contact_and_logs_link(self):
        self._purchase_voucher(claimed=False)

        response = self.client.post(
            reverse("gstr2b:vendor_followup"),
            {
                "vendor_key": "27ABCDE1234F1Z5",
                "from_period": "2026-05",
                "to_period": "2026-05",
                "whatsapp_number": "98765 43210",
                "subject": "GST follow-up",
                "message": "Please resolve GST ITC gap.",
                "channel": "whatsapp",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "https://wa.me/919876543210")
        ledger = Ledger.objects.get(company=self.company, gstin="27ABCDE1234F1Z5")
        self.assertEqual(ledger.whatsapp_number, "+919876543210")
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="Ledger",
                record_id=ledger.pk,
                new_data__channel="whatsapp_link",
            ).exists()
        )
