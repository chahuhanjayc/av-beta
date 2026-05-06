import os
import shutil
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from audit.models import AuditLog as LegacyAuditLog
from clients.models import ClientSubscription
from core.models import (
    AuditLog,
    BankStatement,
    BankStatementRow,
    Company,
    CompanyStatutoryProfile,
    ComplianceFiling,
    ComplianceNotice,
    GSTEvidenceDocument,
    PracticeTask,
    StatutoryRuleOverride,
    UserCompanyAccess,
)
from core.compliance_autopilot import run_compliance_autopilot
from core.phone import normalize_phone_number
from core.upload_validation import DOCUMENT_EXTENSIONS, validate_uploaded_file
from core.views import _auto_match, _refresh_duplicate_flags, _voucher_matches_bank_row, protected_media
from ledger.models import AccountGroup, Ledger
from ocr.models import OCRSubmission
from vouchers.models import Voucher, VoucherItem


class UploadValidationTests(SimpleTestCase):
    def test_rejects_mismatched_file_signature(self):
        upload = SimpleUploadedFile(
            "invoice.pdf",
            b"not actually a pdf",
            content_type="application/pdf",
        )

        with self.assertRaises(ValidationError):
            validate_uploaded_file(
                upload,
                allowed_extensions=DOCUMENT_EXTENSIONS,
                max_mb=20,
            )

    def test_allows_matching_pdf_signature(self):
        upload = SimpleUploadedFile(
            "invoice.pdf",
            b"%PDF-1.4\n%",
            content_type="application/pdf",
        )

        self.assertIs(
            validate_uploaded_file(
                upload,
                allowed_extensions=DOCUMENT_EXTENSIONS,
                max_mb=20,
            ),
            upload,
        )


class PhoneNormalizationTests(SimpleTestCase):
    def test_normalizes_indian_whatsapp_numbers(self):
        self.assertEqual(normalize_phone_number("98765 43210"), "+919876543210")
        self.assertEqual(normalize_phone_number("09876543210"), "+919876543210")
        self.assertEqual(normalize_phone_number("whatsapp:+91 98765 43210"), "+919876543210")


class ProtectedMediaTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_dir)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

        self.company = Company.objects.create(
            name="Media Co",
            gstin="27AAAAA0000A1Z5",
            short_code="MC",
        )
        self.other_company = Company.objects.create(
            name="Other Media Co",
            gstin="27BBBBB0000B1Z5",
            short_code="OM",
        )
        self.user = get_user_model().objects.create_user(
            email="media@example.com",
            password="secret",
        )
        self.factory = RequestFactory()

    def _write_media(self, media_path, content=b"%PDF-1.4\n"):
        full_path = os.path.join(self.temp_dir, *media_path.split("/"))
        os.makedirs(os.path.dirname(full_path), exist_ok=True)
        with open(full_path, "wb") as handle:
            handle.write(content)

    def _request(self, media_path):
        request = self.factory.get(f"/media/{media_path}")
        request.user = self.user
        request.current_company = self.company
        return request

    def test_serves_only_registered_company_owned_media(self):
        media_path = f"ocr/{self.company.id}/bill.pdf"
        self._write_media(media_path)
        OCRSubmission.objects.create(company=self.company, file=media_path)

        response = protected_media(self._request(media_path), media_path)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_serves_company_owned_gst_evidence(self):
        media_path = f"gst_evidence/{self.company.id}/2026-04/gstr3b.pdf"
        self._write_media(media_path)
        GSTEvidenceDocument.objects.create(
            company=self.company,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            title="GSTR-3B acknowledgement",
            evidence_type=GSTEvidenceDocument.TYPE_GSTR3B_ACK,
            return_type=GSTEvidenceDocument.RETURN_GSTR3B,
            file=media_path,
        )

        response = protected_media(self._request(media_path), media_path)
        self.assertEqual(response.status_code, 200)
        response.close()

    def test_rejects_path_traversal(self):
        media_path = f"ocr/{self.company.id}/../bill.pdf"

        response = protected_media(self._request(media_path), media_path)

        self.assertEqual(response.status_code, 403)

    def test_rejects_existing_file_without_database_ownership(self):
        media_path = f"ocr/{self.company.id}/loose.pdf"
        self._write_media(media_path)

        response = protected_media(self._request(media_path), media_path)

        self.assertEqual(response.status_code, 403)

    def test_rejects_media_owned_by_another_company(self):
        media_path = f"ocr/{self.other_company.id}/bill.pdf"
        self._write_media(media_path)
        OCRSubmission.objects.create(company=self.other_company, file=media_path)

        response = protected_media(self._request(media_path), media_path)

        self.assertEqual(response.status_code, 403)


class BankAutoMatchTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Bank Co",
            gstin="27CCCCC0000C1Z5",
            short_code="BC",
        )
        asset_group = AccountGroup.objects.create(
            company=self.company,
            name="Current Assets",
            nature="Asset",
        )
        income_group = AccountGroup.objects.create(
            company=self.company,
            name="Sales",
            nature="Income",
        )
        self.bank = Ledger.objects.create(
            company=self.company,
            name="Bank",
            account_group=asset_group,
        )
        self.customer = Ledger.objects.create(
            company=self.company,
            name="Customer",
            account_group=asset_group,
        )
        self.sales = Ledger.objects.create(
            company=self.company,
            name="Sales Ledger",
            account_group=income_group,
        )
        self.statement = BankStatement.objects.create(
            company=self.company,
            account_ledger=self.bank,
            statement_date=date(2026, 4, 30),
        )

    def _receipt_voucher(self, status="APPROVED"):
        initial_status = "PENDING" if status == "APPROVED" else status
        voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 4, 30),
            status=initial_status,
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.bank,
            entry_type="DR",
            amount=Decimal("100.00"),
        )
        VoucherItem.objects.create(
            voucher=voucher,
            ledger=self.customer,
            entry_type="CR",
            amount=Decimal("100.00"),
        )
        if status == "APPROVED":
            voucher.approve(None)
        return voucher

    def test_auto_match_uses_amount_entry_type_and_approved_vouchers(self):
        voucher = self._receipt_voucher(status="APPROVED")
        row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Customer receipt",
            credit=Decimal("100.00"),
            row_number=1,
        )

        self.assertEqual(_auto_match(self.statement), 1)
        row.refresh_from_db()
        self.assertTrue(row.is_reconciled)
        self.assertEqual(row.matched_voucher, voucher)
        self.assertEqual(row.match_confidence, 100)
        self.assertIn("Exact amount", row.match_reason)

    def test_auto_match_ignores_unapproved_vouchers(self):
        self._receipt_voucher(status="PENDING")
        row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Customer receipt",
            credit=Decimal("100.00"),
            row_number=1,
        )

        self.assertEqual(_auto_match(self.statement), 0)
        row.refresh_from_db()
        self.assertFalse(row.is_reconciled)

    def test_manual_match_helper_requires_matching_bank_line(self):
        voucher = self._receipt_voucher(status="APPROVED")
        row = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Customer receipt",
            credit=Decimal("100.00"),
            row_number=1,
        )
        wrong_amount = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Customer receipt",
            credit=Decimal("101.00"),
            row_number=2,
        )
        wrong_direction = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="Customer payment",
            debit=Decimal("100.00"),
            row_number=3,
        )

        self.assertTrue(_voucher_matches_bank_row(row, voucher))
        self.assertFalse(_voucher_matches_bank_row(wrong_amount, voucher))
        self.assertFalse(_voucher_matches_bank_row(wrong_direction, voucher))

    def test_duplicate_detection_flags_identical_bank_rows(self):
        first = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="UPI CUSTOMER ABC",
            credit=Decimal("100.00"),
            row_number=1,
        )
        second = BankStatementRow.objects.create(
            statement=self.statement,
            date=date(2026, 4, 30),
            description="UPI CUSTOMER ABC",
            credit=Decimal("100.00"),
            row_number=2,
        )

        _refresh_duplicate_flags(self.statement)

        first.refresh_from_db()
        second.refresh_from_db()
        self.assertTrue(first.potential_duplicate)
        self.assertTrue(second.potential_duplicate)
        self.assertEqual(first.duplicate_group_key, second.duplicate_group_key)


class BankReconciliationExportTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Bank Export Co",
            gstin="27BANKX0000B1Z5",
            short_code="BEX",
        )
        self.user = get_user_model().objects.create_user(
            email="bank-export@example.com",
            password="secret",
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
        bank_group = AccountGroup.objects.create(
            company=self.company,
            name="Bank Accounts",
            nature="Asset",
        )
        self.bank = Ledger.objects.create(
            company=self.company,
            name="Current Account",
            account_group=bank_group,
        )
        self.statement = BankStatement.objects.create(
            company=self.company,
            account_ledger=self.bank,
            statement_date=date(2026, 4, 30),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_bank_statement_detail_exports_reconciliation_csv(self):
        BankStatementRow.objects.create(
            statement=self.statement,
            row_number=1,
            date=date(2026, 4, 29),
            description="Customer receipt",
            credit=Decimal("1200.00"),
            is_reconciled=True,
            match_confidence=100,
            match_reason="Manual voucher match",
        )
        BankStatementRow.objects.create(
            statement=self.statement,
            row_number=2,
            date=date(2026, 4, 30),
            description="Unmatched bank charge",
            debit=Decimal("50.00"),
        )

        self.assertEqual(self.statement.pending_rows, 1)
        response = self.client.get(
            reverse("core:bank_statement_detail", args=[self.statement.pk]),
            {"export": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        csv_text = response.content.decode("utf-8")
        self.assertIn("Row Number,Date,Description,Debit,Credit,Balance,Status", csv_text)
        self.assertIn("Customer receipt", csv_text)
        self.assertIn("Reconciled", csv_text)
        self.assertIn("Unmatched bank charge", csv_text)
        self.assertIn("Pending", csv_text)

    def test_bank_reconciliation_report_exports_company_rows_csv(self):
        BankStatementRow.objects.create(
            statement=self.statement,
            row_number=1,
            date=date(2026, 4, 29),
            description="Company-level pending bank charge",
            debit=Decimal("50.00"),
        )

        response = self.client.get(reverse("core:bank_reconciliation_report"), {"export": "csv"})

        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode("utf-8")
        self.assertIn("Statement Date,Bank Account,Row Date,Description,Debit,Credit,Balance,Status", csv_text)
        self.assertIn("Company-level pending bank charge", csv_text)
        self.assertIn("Pending", csv_text)


class HealthzTests(TestCase):
    def test_healthz_returns_database_status(self):
        response = self.client.get(reverse("core:healthz"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "database": "ok"})


class WorkQueueExportTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Work Queue Co",
            gstin="27WORKQ0000W1Z5",
            short_code="WQC",
        )
        self.user = get_user_model().objects.create_user(
            email="workqueue@example.com",
            password="secret",
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

    def test_practice_work_queue_exports_filtered_csv(self):
        PracticeTask.objects.create(
            company=self.company,
            title="Prepare GSTR-1 review",
            task_type=PracticeTask.TYPE_GST,
            priority=PracticeTask.PRIORITY_HIGH,
            due_date=date(2026, 4, 20),
            assigned_to=self.user,
            reference="GST-APR",
        )
        PracticeTask.objects.create(
            company=self.company,
            title="TDS challan check",
            task_type=PracticeTask.TYPE_TDS,
            priority=PracticeTask.PRIORITY_NORMAL,
        )

        response = self.client.get(reverse("core:practice_tasks"), {"q": "GSTR", "export": "csv"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        csv_text = response.content.decode("utf-8")
        self.assertIn("Client,Task,Type,Priority,Status,Owner,Due Date,Days Overdue", csv_text)
        self.assertIn("Prepare GSTR-1 review", csv_text)
        self.assertIn("GST-APR", csv_text)
        self.assertNotIn("TDS challan check", csv_text)

    def test_compliance_filings_export_filtered_csv(self):
        ComplianceFiling.objects.create(
            company=self.company,
            filing_type=ComplianceFiling.TYPE_GSTR3B,
            title="April GSTR-3B",
            status=ComplianceFiling.STATUS_READY_FOR_REVIEW,
            priority=PracticeTask.PRIORITY_CRITICAL,
            due_date=date(2026, 5, 20),
            assigned_to=self.user,
            reviewer=self.user,
            arn_ack_number="ARN-APR",
        )
        ComplianceFiling.objects.create(
            company=self.company,
            filing_type=ComplianceFiling.TYPE_TDS_26Q,
            title="Q4 TDS return",
            status=ComplianceFiling.STATUS_NOT_STARTED,
        )

        response = self.client.get(reverse("core:compliance_filings"), {
            "type": ComplianceFiling.TYPE_GSTR3B,
            "export": "csv",
        })

        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode("utf-8")
        self.assertIn("Client,Filing,Type,Priority,Status,Owner,Reviewer,Due Date", csv_text)
        self.assertIn("April GSTR-3B", csv_text)
        self.assertIn("ARN-APR", csv_text)
        self.assertNotIn("Q4 TDS return", csv_text)

    def test_compliance_notices_export_filtered_csv(self):
        ComplianceNotice.objects.create(
            company=self.company,
            notice_type=ComplianceNotice.TYPE_GST,
            title="DRC-01 response",
            reference_number="DRC01-APR",
            issue_date=date(2026, 5, 1),
            response_due_date=date(2026, 5, 15),
            status=ComplianceNotice.STATUS_DATA_PENDING,
            assigned_to=self.user,
        )
        ComplianceNotice.objects.create(
            company=self.company,
            notice_type=ComplianceNotice.TYPE_MCA,
            title="MCA clarification",
            reference_number="MCA-1",
        )

        response = self.client.get(reverse("core:compliance_notices"), {
            "type": ComplianceNotice.TYPE_GST,
            "export": "csv",
        })

        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode("utf-8")
        self.assertIn("Client,Notice,Type,Priority,Status,Owner,Issue Date,Response Due Date", csv_text)
        self.assertIn("DRC-01 response", csv_text)
        self.assertIn("DRC01-APR", csv_text)
        self.assertNotIn("MCA clarification", csv_text)


class AppSettingsAccessTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="App Settings Co",
            gstin="27FFFFF0000F1Z5",
            short_code="ASC",
        )
        self.user = get_user_model().objects.create_user(
            email="viewer-settings@example.com",
            password="secret",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.company,
            role="Viewer",
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

    def _settings_payload(self, **overrides):
        payload = {
            "whatsapp_intake_number": "98765 43210",
            "e_invoice_enabled": "on",
            "e_invoice_aato_crore": "12.50",
            "e_invoice_reporting_deadline_days": "30",
            "e_invoice_warning_days": "25",
            "invoice_email_subject": "Invoice {voucher_number} from {company_name}",
            "invoice_email_body": "Dear {client_name}, invoice {voucher_number} is attached.",
            "payment_reminder_email_subject": "Reminder {voucher_number}: {outstanding}",
            "payment_reminder_email_body": "Dear {client_name}, please pay {outstanding}.",
            "gst_registered": "on",
            "gst_return_frequency": CompanyStatutoryProfile.GST_FREQUENCY_MONTHLY,
            "gstr1_frequency": CompanyStatutoryProfile.GSTR1_MONTHLY,
            "qrmp_group": CompanyStatutoryProfile.QRMP_GROUP_A,
            "gstr1_monthly_due_day": "11",
            "gstr1_quarterly_due_day": "13",
            "gstr3b_monthly_due_day": "20",
            "gstr3b_qrmp_due_day": "22",
            "gst_late_fee_per_day": "50.00",
            "gst_nil_late_fee_per_day": "20.00",
            "gst_interest_rate_percent": "18.00",
            "tds_applicable": "on",
            "tds_26q_enabled": "on",
            "tds_deposit_due_day": "7",
            "tds_march_deposit_due_day": "30",
            "tds_deposit_interest_rate_percent_per_month": "1.50",
            "tds_return_late_fee_per_day": "200.00",
            "msme_watch_enabled": "on",
            "msme_default_credit_days": "45",
            "msme_interest_rate_percent": "18.00",
            "due_date_grace_days": "0",
            "rules_notes": "",
        }
        payload.update(overrides)
        return payload

    def test_non_admin_company_user_can_manage_app_settings(self):
        response = self.client.get(reverse("core:app_settings"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "App Settings")
        self.assertContains(response, "Client WhatsApp Number")
        self.assertContains(response, "Invoice Email")
        self.assertContains(response, "Payment Reminder")
        self.assertContains(response, "GST E-Invoice Watch")
        self.assertContains(response, "Statutory Profile")
        self.assertContains(response, "core/app-settings/", html=False)
        self.assertContains(response, "App Settings")
        self.assertNotContains(response, "Admin Panel")

        update = self.client.post(
            reverse("core:app_settings"),
            self._settings_payload(
                gst_return_frequency=CompanyStatutoryProfile.GST_FREQUENCY_QRMP,
                gstr1_frequency=CompanyStatutoryProfile.GSTR1_QUARTERLY,
                qrmp_group=CompanyStatutoryProfile.QRMP_GROUP_B,
                gstr3b_qrmp_due_day="24",
                rules_notes="Quarterly GST client.",
            ),
        )

        self.assertRedirects(update, reverse("core:app_settings"))
        self.company.refresh_from_db()
        self.assertEqual(self.company.whatsapp_intake_number, "+919876543210")
        self.assertTrue(self.company.e_invoice_enabled)
        self.assertEqual(self.company.e_invoice_aato_crore, Decimal("12.50"))
        self.assertEqual(self.company.e_invoice_reporting_deadline_days, 30)
        self.assertEqual(self.company.e_invoice_warning_days, 25)
        self.assertEqual(self.company.payment_reminder_email_subject, "Reminder {voucher_number}: {outstanding}")
        profile = self.company.statutory_profile
        self.assertEqual(profile.gst_return_frequency, CompanyStatutoryProfile.GST_FREQUENCY_QRMP)
        self.assertEqual(profile.gstr1_frequency, CompanyStatutoryProfile.GSTR1_QUARTERLY)
        self.assertEqual(profile.gstr3b_qrmp_due_day, 24)
        self.assertEqual(profile.rules_notes, "Quarterly GST client.")

        settings_page = self.client.get(reverse("core:app_settings"))
        self.assertContains(settings_page, "Open WhatsApp Test")
        self.assertContains(settings_page, "https://wa.me/919876543210", html=False)

    def test_app_settings_validate_e_invoice_warning_window(self):
        response = self.client.post(
            reverse("core:app_settings"),
            self._settings_payload(e_invoice_warning_days="31"),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Warning day cannot be greater than the reporting deadline.")

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_app_settings_can_send_test_invoice_email(self):
        response = self.client.post(
            reverse("core:app_settings"),
            self._settings_payload(
                action="send_test_email",
                invoice_email_from_name="Accounts Team",
                invoice_email_from_address="accounts@example.com",
                invoice_email_reply_to="billing@example.com",
                invoice_email_body="Dear {client_name}, invoice {voucher_number} is attached. {aging_line}",
            ),
        )

        self.assertRedirects(response, reverse("core:app_settings"))
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(mail.outbox[0].reply_to, ["billing@example.com"])
        self.assertIn("AV-TEST-001", mail.outbox[0].subject)
        self.assertIn("Sample Client", mail.outbox[0].body)

    def test_app_settings_can_add_statutory_rule_override(self):
        response = self.client.post(
            reverse("core:app_settings"),
            {
                "action": "add_rule_override",
                "rule_type": StatutoryRuleOverride.RULE_GSTR3B,
                "period_start": "2026-03-01",
                "period_end": "2026-03-31",
                "original_due_date": "2026-04-20",
                "override_due_date": "2026-04-25",
                "late_fee_per_day": "0.00",
                "interest_rate_percent": "",
                "reason": "Notification-based due-date extension.",
            },
        )

        self.assertRedirects(response, reverse("core:app_settings"))
        override = StatutoryRuleOverride.objects.get(company=self.company)
        self.assertEqual(override.rule_type, StatutoryRuleOverride.RULE_GSTR3B)
        self.assertEqual(override.override_due_date, date(2026, 4, 25))
        self.assertEqual(override.created_by, self.user)

        deactivate = self.client.post(
            reverse("core:app_settings"),
            {
                "action": "deactivate_rule_override",
                "override_id": override.pk,
            },
        )

        self.assertRedirects(deactivate, reverse("core:app_settings"))
        override.refresh_from_db()
        self.assertFalse(override.is_active)

    def test_app_settings_can_run_compliance_autopilot(self):
        response = self.client.post(
            reverse("core:app_settings"),
            self._settings_payload(
                action="run_compliance_autopilot",
                autopilot_months="2",
            ),
        )

        self.assertRedirects(response, reverse("core:app_settings"))
        filings = ComplianceFiling.objects.filter(company=self.company)
        self.assertTrue(filings.filter(
            filing_type=ComplianceFiling.TYPE_GST_IMS,
            source_reference__startswith="AUTO:",
        ).exists())
        self.assertTrue(filings.filter(filing_type=ComplianceFiling.TYPE_GSTR1).exists())
        self.assertTrue(filings.filter(filing_type=ComplianceFiling.TYPE_GSTR3B).exists())
        self.assertTrue(filings.filter(filing_type=ComplianceFiling.TYPE_TDS_PAYMENT).exists())
        self.assertTrue(PracticeTask.objects.filter(
            company=self.company,
            reference__startswith="AUTO:",
        ).exists())

        first_count = filings.count()
        self.client.post(
            reverse("core:app_settings"),
            self._settings_payload(
                action="run_compliance_autopilot",
                autopilot_months="2",
            ),
        )

        self.assertEqual(ComplianceFiling.objects.filter(company=self.company).count(), first_count)


class CACommandCenterAutopilotTests(TestCase):
    def setUp(self):
        self.managed_company = Company.objects.create(
            name="Managed Autopilot Co",
            gstin="27AUTOA0000A1Z5",
            tan="MUMA00000A",
            short_code="MAC",
        )
        self.read_only_company = Company.objects.create(
            name="Read Only Autopilot Co",
            gstin="27AUTOR0000R1Z5",
            tan="MUMR00000R",
            short_code="ROC",
        )
        self.user = get_user_model().objects.create_user(
            email="ca-autopilot@example.com",
            password="secret",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.managed_company,
            role="Admin",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.read_only_company,
            role="Viewer",
        )
        for company in [self.managed_company, self.read_only_company]:
            ClientSubscription.objects.create(
                company=company,
                primary_user=self.user,
                status=ClientSubscription.STATUS_ACTIVE,
                subscription_end=timezone.now() + timedelta(days=30),
            )
        CompanyStatutoryProfile.objects.create(company=self.managed_company)
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.managed_company.pk
        session.save()

    def test_command_center_autopilot_uses_manageable_clients_only(self):
        response = self.client.post(
            reverse("core:ca_command_center_autopilot"),
            {"months": "2"},
        )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], reverse("core:ca_command_center"))
        self.assertTrue(ComplianceFiling.objects.filter(company=self.managed_company).exists())
        self.assertFalse(ComplianceFiling.objects.filter(company=self.read_only_company).exists())


class ComplianceAutopilotServiceTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Quarterly Autopilot Co",
            gstin="27QRTLY0000Q1Z5",
            tan="MUMQ00000Q",
            short_code="QAC",
        )
        self.user = get_user_model().objects.create_user(
            email="quarterly-autopilot@example.com",
            password="secret",
        )
        CompanyStatutoryProfile.objects.create(
            company=self.company,
            gst_return_frequency=CompanyStatutoryProfile.GST_FREQUENCY_QRMP,
            gstr1_frequency=CompanyStatutoryProfile.GSTR1_QUARTERLY,
            tds_26q_enabled=False,
            tds_27q_enabled=True,
        )

    def test_autopilot_uses_quarterly_profile_and_27q_form(self):
        result = run_compliance_autopilot(
            companies=[self.company],
            months=1,
            from_date=date(2026, 6, 1),
            created_by=self.user,
        )

        self.assertEqual(result["created"], 5)
        filings = ComplianceFiling.objects.filter(company=self.company)
        self.assertTrue(filings.filter(
            filing_type=ComplianceFiling.TYPE_GSTR1,
            title__contains="Q1 FY 2026-27",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
        ).exists())
        self.assertTrue(filings.filter(
            filing_type=ComplianceFiling.TYPE_TDS_27Q,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            source_reference__startswith="AUTO:",
        ).exists())

        second = run_compliance_autopilot(
            companies=[self.company],
            months=1,
            from_date=date(2026, 6, 1),
            created_by=self.user,
        )

        self.assertEqual(second["created"], 0)
        self.assertEqual(second["existing"], 5)
        self.assertEqual(ComplianceFiling.objects.filter(company=self.company).count(), 5)


class AuditLogImmutabilityTests(TestCase):
    def test_core_audit_logs_cannot_be_bulk_deleted(self):
        company = Company.objects.create(
            name="Audit Co",
            gstin="27DDDDD0000D1Z5",
            short_code="AC",
        )
        AuditLog.objects.create(
            company=company,
            action=AuditLog.ACTION_CREATE,
            model_name="Company",
            record_id=company.id,
            object_repr=str(company),
        )

        with self.assertRaises(ValidationError):
            AuditLog.objects.filter(company=company).delete()

    def test_voucher_audit_uses_core_log_only(self):
        company = Company.objects.create(
            name="Voucher Audit Co",
            gstin="27EEEEE0000E1Z5",
            short_code="VA",
        )
        voucher = Voucher.objects.create(
            company=company,
            voucher_type="Journal",
            date=date(2026, 4, 30),
        )

        self.assertTrue(
            AuditLog.objects.filter(
                company=company,
                action=AuditLog.ACTION_CREATE,
                model_name="voucher",
                record_id=voucher.pk,
            ).exists()
        )
        self.assertFalse(
            LegacyAuditLog.objects.filter(
                model_name="Voucher",
                object_id=str(voucher.pk),
            ).exists()
        )

    def test_audit_log_export_csv_respects_filters(self):
        company = Company.objects.create(
            name="Audit Export Co",
            gstin="27AAAAA0000A1Z5",
            short_code="AEC",
        )
        user = get_user_model().objects.create_superuser(
            email="audit-export@example.com",
            password="secret",
        )
        UserCompanyAccess.objects.create(user=user, company=company, role="Admin")
        AuditLog.objects.create(
            company=company,
            user=user,
            action=AuditLog.ACTION_CREATE,
            model_name="Voucher",
            record_id=101,
            object_repr="Sales 101",
            new_data={"number": "S-101"},
        )
        AuditLog.objects.create(
            company=company,
            user=user,
            action=AuditLog.ACTION_UPDATE,
            model_name="Ledger",
            record_id=202,
            object_repr="Customer 202",
        )
        self.client.force_login(user)
        session = self.client.session
        session["current_company_id"] = company.pk
        session.save()

        response = self.client.get(
            reverse("core:audit_log"),
            {"action": AuditLog.ACTION_CREATE, "export": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        content = response.content.decode()
        self.assertIn("Timestamp,Action,Model,Record ID,Object,User,Old Data,New Data", content)
        self.assertIn("Sales 101", content)
        self.assertNotIn("Customer 202", content)
