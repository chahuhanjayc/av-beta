from datetime import date
from decimal import Decimal
import json
import shutil
import tempfile
from io import StringIO

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from core.models import AuditLog, BankStatement, BankStatementRow, Company, PracticeTask, UserCompanyAccess
from integrations.gst import build_e_invoice_payload, build_e_way_bill_payload
from integrations.models import IntegrationConnector, IntegrationRequestLog, IntegrationRetryJob, StatutoryExportLog
from integrations.provider_readiness import build_provider_go_live_readiness
from integrations.readiness import PRODUCTION_EVIDENCE_FIELDS, build_gst_certification_readiness
from integrations.retry_dispatcher import process_due_retry_jobs
from ledger.models import AccountGroup, Ledger
from ocr.models import OCRSubmission
from tds.models import TDSFilingPack, TDSPostFilingTracker, TDSReturnWorkpaper
from vouchers.models import Voucher, VoucherItem


class GSTIntegrationAPITests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.settings_override = override_settings(MEDIA_ROOT=self.temp_dir)
        self.settings_override.enable()
        self.addCleanup(self.settings_override.disable)
        self.addCleanup(shutil.rmtree, self.temp_dir, ignore_errors=True)

        self.company = Company.objects.create(
            name="GST Integration Co",
            gstin="27AAAAA0000A1Z5",
            short_code="GST",
            whatsapp_intake_number="+919876543210",
        )
        self.user = get_user_model().objects.create_superuser(
            email="gst-admin@example.com",
            password="CorrectHorseBatteryStaple123!",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.company,
            role="Admin",
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

        debtors = AccountGroup.objects.create(company=self.company, name="Sundry Debtors", nature="Asset")
        sales_group = AccountGroup.objects.create(company=self.company, name="Sales", nature="Income")
        tax_group = AccountGroup.objects.create(company=self.company, name="Duties and Taxes", nature="Tax")
        self.customer = Ledger.objects.create(
            company=self.company,
            name="GST Customer",
            gstin="27BBBBB1111B1Z5",
            account_group=debtors,
        )
        self.sales = Ledger.objects.create(company=self.company, name="Sales Ledger", account_group=sales_group)
        self.cgst = Ledger.objects.create(company=self.company, name="Output CGST", account_group=tax_group)
        self.sgst = Ledger.objects.create(company=self.company, name="Output SGST", account_group=tax_group)
        self.voucher = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 5, 1),
            status="PENDING",
            place_of_supply="27",
            cgst_amount=Decimal("9.00"),
            sgst_amount=Decimal("9.00"),
            total_tax=Decimal("18.00"),
            dispatch_pincode=400001,
            ship_to_pincode=400002,
            transport_mode="1",
            transport_distance_km=25,
            transporter_id="27ABCDE1234F1Z5",
            vehicle_number="MH12AB1234",
            vehicle_type="R",
        )
        VoucherItem.objects.create(voucher=self.voucher, ledger=self.customer, entry_type="DR", amount=Decimal("118.00"))
        VoucherItem.objects.create(voucher=self.voucher, ledger=self.sales, entry_type="CR", amount=Decimal("100.00"))
        VoucherItem.objects.create(voucher=self.voucher, ledger=self.cgst, entry_type="CR", amount=Decimal("9.00"))
        VoucherItem.objects.create(voucher=self.voucher, ledger=self.sgst, entry_type="CR", amount=Decimal("9.00"))
        self.voucher.approve(None)

    def _production_evidence(self, prefix="PROVIDER"):
        return {
            field["key"]: f"{prefix}-{field['key'].upper()}"
            for field in PRODUCTION_EVIDENCE_FIELDS
        }

    def test_builds_standard_e_invoice_and_e_way_bill_payload_shapes(self):
        einvoice = build_e_invoice_payload(self.voucher)
        eway = build_e_way_bill_payload(self.voucher)

        self.assertEqual(einvoice["Version"], "1.1")
        self.assertEqual(einvoice["TranDtls"]["TaxSch"], "GST")
        self.assertEqual(einvoice["DocDtls"]["No"], self.voucher.number)
        self.assertEqual(einvoice["SellerDtls"]["Pin"], 400001)
        self.assertEqual(einvoice["BuyerDtls"]["Pin"], 400002)
        self.assertEqual(einvoice["ItemList"][0]["AssAmt"], Decimal("100.00"))
        self.assertEqual(eway["docNo"], self.voucher.number)
        self.assertEqual(eway["transMode"], "1")
        self.assertEqual(eway["vehicleNo"], "MH12AB1234")
        self.assertEqual(eway["itemList"][0]["taxableAmount"], Decimal("100.00"))

    def test_downloads_portal_ready_e_invoice_and_e_way_bill_json(self):
        einvoice_response = self.client.get(
            reverse("integrations:e_invoice_payload_download", args=[self.voucher.pk])
        )
        eway_response = self.client.get(
            reverse("integrations:e_way_bill_payload_download", args=[self.voucher.pk])
        )

        self.assertEqual(einvoice_response.status_code, 200)
        self.assertEqual(eway_response.status_code, 200)
        self.assertIn("attachment", einvoice_response["Content-Disposition"])
        self.assertIn("attachment", eway_response["Content-Disposition"])
        einvoice = json.loads(einvoice_response.content.decode("utf-8"))
        eway = json.loads(eway_response.content.decode("utf-8"))
        self.assertEqual(einvoice["DocDtls"]["No"], self.voucher.number)
        self.assertEqual(einvoice["SellerDtls"]["Gstin"], self.company.gstin)
        self.assertEqual(eway["docNo"], self.voucher.number)
        self.assertEqual(eway["fromGstin"], self.company.gstin)

    def test_manual_e_invoice_and_e_way_bill_capture_updates_locked_sales_voucher(self):
        irn_response = self.client.post(
            reverse("integrations:mark_e_invoice_status", args=[self.voucher.pk]),
            {
                "irn": "9f7a8d0e5c4b3a291817161514131211",
                "ack_no": "ACK-PORTAL-1",
                "ack_date": "2026-05-01T12:30:00",
                "status": "ACT",
                "signed_qr_code": "SIGNED-QR-PAYLOAD",
            },
        )
        eway_response = self.client.post(
            reverse("integrations:mark_e_way_bill_status", args=[self.voucher.pk]),
            {
                "e_way_bill_no": "181716151413",
                "e_way_bill_date": "2026-05-01T13:00:00",
                "valid_until": "2026-05-02T23:59:00",
                "status": "ACT",
            },
        )

        self.assertEqual(irn_response.status_code, 302)
        self.assertEqual(eway_response.status_code, 302)
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.e_invoice_irn, "9f7a8d0e5c4b3a291817161514131211")
        self.assertEqual(self.voucher.e_invoice_ack_no, "ACK-PORTAL-1")
        self.assertEqual(self.voucher.e_invoice_status, "ACT")
        self.assertEqual(self.voucher.e_invoice_signed_qr_code, "SIGNED-QR-PAYLOAD")
        self.assertEqual(self.voucher.e_way_bill_no, "181716151413")
        self.assertEqual(self.voucher.e_way_bill_status, "ACT")
        self.assertIsNotNone(self.voucher.e_way_bill_valid_until)
        self.assertEqual(
            AuditLog.objects.filter(
                company=self.company,
                model_name="Voucher",
                record_id=self.voucher.pk,
                action=AuditLog.ACTION_UPDATE,
                new_data__source="manual_capture",
            ).count(),
            2,
        )

    def test_imports_successful_irp_result_json_and_updates_voucher(self):
        payload = {
            "results": [
                {
                    "DocDtls": {"No": self.voucher.number, "Dt": "01/05/2026"},
                    "SellerDtls": {"Gstin": self.company.gstin},
                    "BuyerDtls": {"Gstin": self.customer.gstin},
                    "ValDtls": {"TotInvVal": "118.00"},
                    "Irn": "REALIRN1234567890",
                    "AckNo": "ACK-PORTAL-2",
                    "AckDt": "2026-05-01T12:45:00",
                    "Status": "ACT",
                    "SignedQRCode": "SIGNED-QR-FROM-PORTAL",
                }
            ]
        }
        upload = SimpleUploadedFile(
            "irp-results.json",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

        response = self.client.post(
            reverse("integrations:gst_result_import"),
            {"service": "e_invoice", "result_file": upload},
        )

        self.assertEqual(response.status_code, 200)
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.e_invoice_irn, "REALIRN1234567890")
        self.assertEqual(self.voucher.e_invoice_ack_no, "ACK-PORTAL-2")
        self.assertEqual(self.voucher.e_invoice_status, "ACT")
        self.assertEqual(self.voucher.e_invoice_signed_qr_code, "SIGNED-QR-FROM-PORTAL")
        self.assertTrue(
            IntegrationRequestLog.objects.filter(
                company=self.company,
                voucher=self.voucher,
                provider="portal_upload",
                service=IntegrationRequestLog.SERVICE_E_INVOICE,
                status=IntegrationRequestLog.STATUS_SUCCESS,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="Voucher",
                record_id=self.voucher.pk,
                new_data__source="gst_result_import",
            ).exists()
        )

    def test_imports_failed_eway_result_json_and_creates_ca_task(self):
        payload = [
            {
                "docNo": self.voucher.number,
                "docDate": "01/05/2026",
                "fromGstin": self.company.gstin,
                "toGstin": self.customer.gstin,
                "totInvValue": "118.00",
                "errorMessage": "Vehicle number format invalid",
                "status": "FAILED",
            }
        ]
        upload = SimpleUploadedFile(
            "eway-results.json",
            json.dumps(payload).encode("utf-8"),
            content_type="application/json",
        )

        response = self.client.post(
            reverse("integrations:gst_result_import"),
            {"service": "e_way_bill", "result_file": upload},
        )

        self.assertEqual(response.status_code, 200)
        self.voucher.refresh_from_db()
        self.assertFalse(self.voucher.e_way_bill_no)
        task = PracticeTask.objects.get(company=self.company, reference__startswith=f"GSTRESULT:e_way_bill:{self.voucher.pk}:")
        self.assertEqual(task.task_type, PracticeTask.TYPE_GST)
        self.assertIn("Vehicle number format invalid", task.description)
        self.assertTrue(
            IntegrationRequestLog.objects.filter(
                company=self.company,
                voucher=self.voucher,
                provider="portal_upload",
                service=IntegrationRequestLog.SERVICE_E_WAY_BILL,
                status=IntegrationRequestLog.STATUS_FAILED,
                error_message__icontains="Vehicle number format invalid",
            ).exists()
        )

    def test_traces_result_import_updates_tds_workpaper_connector_and_tracker(self):
        connector = IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_TRACES,
            provider_name="TRACES Portal",
            mode=IntegrationConnector.MODE_MANUAL,
            status=IntegrationConnector.STATUS_READY,
            tan="ABCD12345E",
            username="traces-user",
            credential_reference="TRACES_PORTAL",
        )
        pack = TDSFilingPack.objects.create(
            company=self.company,
            form_type=TDSReturnWorkpaper.FORM_26Q,
            financial_year_start=2026,
            quarter=TDSReturnWorkpaper.Q1,
            period_start=date(2026, 4, 1),
            period_end=date(2026, 6, 30),
            due_date=date(2026, 7, 31),
            status=TDSFilingPack.STATUS_FILED,
        )
        payload = {
            "results": [
                {
                    "form_type": "26Q",
                    "financial_year": "2026-27",
                    "quarter": "Q1",
                    "status": "Processed Without Default",
                    "ack_number": "TDS-ACK-1001",
                    "traces_token": "TRACES-REQ-1",
                    "challan_status": "Matched",
                    "fvu_status": "Validated",
                }
            ]
        }

        response = self.client.post(
            reverse("integrations:traces_result_import"),
            {
                "result_file": SimpleUploadedFile(
                    "traces-result.json",
                    json.dumps(payload).encode("utf-8"),
                    content_type="application/json",
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TRACES Result Import")
        workpaper = TDSReturnWorkpaper.objects.get(
            company=self.company,
            form_type=TDSReturnWorkpaper.FORM_26Q,
            financial_year_start=2026,
            quarter=TDSReturnWorkpaper.Q1,
        )
        self.assertEqual(workpaper.status, TDSReturnWorkpaper.STATUS_FILED)
        self.assertEqual(workpaper.traces_statement_status, TDSReturnWorkpaper.TRACES_ACCEPTED)
        self.assertEqual(workpaper.challan_status, TDSReturnWorkpaper.CHALLAN_MATCHED)
        self.assertEqual(workpaper.fvu_status, TDSReturnWorkpaper.FVU_VALIDATED)
        self.assertEqual(workpaper.ack_number, "TDS-ACK-1001")
        connector.refresh_from_db()
        self.assertEqual(connector.status, IntegrationConnector.STATUS_LIVE)
        self.assertIsNotNone(connector.last_success_at)
        tracker = TDSPostFilingTracker.objects.get(pack=pack)
        self.assertEqual(tracker.statement_status, TDSPostFilingTracker.STATEMENT_PROCESSED)
        self.assertEqual(tracker.traces_request_number, "TRACES-REQ-1")
        self.assertTrue(
            IntegrationRequestLog.objects.filter(
                company=self.company,
                service=IntegrationRequestLog.SERVICE_TRACES,
                status=IntegrationRequestLog.STATUS_SUCCESS,
                response_payload__ack_number="TDS-ACK-1001",
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="TDSReturnWorkpaper",
                record_id=workpaper.pk,
                new_data__source="traces_result_import",
            ).exists()
        )

    def test_traces_result_import_rejection_creates_tds_task(self):
        IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_TRACES,
            provider_name="TRACES Portal",
            mode=IntegrationConnector.MODE_MANUAL,
            status=IntegrationConnector.STATUS_READY,
            tan="ABCD12345E",
            username="traces-user",
            credential_reference="TRACES_PORTAL",
        )
        payload = {
            "results": [
                {
                    "form_type": "26Q",
                    "financial_year": "2026-27",
                    "quarter": "Q1",
                    "status": "Rejected",
                    "message": "Invalid challan mapping",
                }
            ]
        }

        response = self.client.post(
            reverse("integrations:traces_result_import"),
            {
                "result_file": SimpleUploadedFile(
                    "traces-rejected.json",
                    json.dumps(payload).encode("utf-8"),
                    content_type="application/json",
                ),
            },
        )

        self.assertEqual(response.status_code, 200)
        workpaper = TDSReturnWorkpaper.objects.get(
            company=self.company,
            form_type=TDSReturnWorkpaper.FORM_26Q,
            financial_year_start=2026,
            quarter=TDSReturnWorkpaper.Q1,
        )
        self.assertEqual(workpaper.traces_statement_status, TDSReturnWorkpaper.TRACES_REJECTED)
        task = PracticeTask.objects.get(company=self.company, reference__startswith="TRACESRESULT:26Q:2026:Q1:rejected:")
        self.assertEqual(task.task_type, PracticeTask.TYPE_TDS)
        self.assertIn("Invalid challan mapping", task.description)
        self.assertTrue(
            IntegrationRequestLog.objects.filter(
                company=self.company,
                service=IntegrationRequestLog.SERVICE_TRACES,
                status=IntegrationRequestLog.STATUS_FAILED,
                error_message__icontains="Invalid challan mapping",
            ).exists()
        )

    @override_settings(GST_API_PROVIDER="mock", E_INVOICE_ENABLED=True, E_WAY_BILL_ENABLED=True)
    def test_mock_provider_generates_e_invoice_and_e_way_bill(self):
        einvoice_response = self.client.post(
            reverse("integrations:generate_e_invoice_api", args=[self.voucher.pk])
        )
        eway_response = self.client.post(
            reverse("integrations:generate_e_way_bill_api", args=[self.voucher.pk])
        )

        self.assertEqual(einvoice_response.status_code, 200)
        self.assertEqual(eway_response.status_code, 200)
        self.voucher.refresh_from_db()
        self.assertTrue(self.voucher.e_invoice_irn.startswith("MOCKIRN"))
        self.assertEqual(self.voucher.e_invoice_status, "ACT")
        self.assertTrue(self.voucher.e_invoice_signed_invoice)
        self.assertTrue(self.voucher.e_invoice_signed_qr_code.startswith("MOCK-SIGNED-QR"))
        self.assertTrue(self.voucher.e_way_bill_no)
        self.assertEqual(self.voucher.e_way_bill_status, "ACT")
        self.assertIsNotNone(self.voucher.e_way_bill_valid_until)
        self.assertEqual(
            IntegrationRequestLog.objects.filter(company=self.company, status=IntegrationRequestLog.STATUS_SUCCESS).count(),
            2,
        )

    @override_settings(GST_API_PROVIDER="", E_INVOICE_ENABLED=False, E_WAY_BILL_ENABLED=False)
    def test_readiness_flags_missing_provider(self):
        readiness = build_gst_certification_readiness(self.company)

        self.assertFalse(readiness["sandbox_ready"])
        self.assertGreaterEqual(readiness["errors"], 1)
        self.assertIn("GST provider selected", [item["name"] for item in readiness["checks"]])

    @override_settings(
        GST_API_PROVIDER="mock",
        GST_API_SANDBOX_MODE=True,
        E_INVOICE_ENABLED=True,
        E_WAY_BILL_ENABLED=True,
    )
    def test_readiness_accepts_mock_for_sandbox(self):
        readiness = build_gst_certification_readiness(self.company)

        self.assertTrue(readiness["sandbox_ready"])
        self.assertFalse(readiness["production_ready"])
        self.assertEqual(readiness["errors"], 0)

    def test_connector_dashboard_saves_client_owned_regulatory_settings(self):
        response = self.client.get(reverse("integrations:dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Regulatory")
        self.assertContains(response, "Sync Connectors")
        self.assertContains(response, "IRP / E-Invoice")
        self.assertContains(response, "TRACES")
        self.assertContains(response, "Tally Sync")
        self.assertContains(response, "Connected Banking")
        self.assertContains(response, "Bank Feed")
        self.assertContains(response, "TRACES Results")

        response = self.client.post(
            reverse("integrations:connector_update", args=[IntegrationConnector.TYPE_IRP]),
            {
                "provider_name": "Sandbox GSP",
                "mode": IntegrationConnector.MODE_SANDBOX,
                "status": IntegrationConnector.STATUS_READY,
                "gstin": self.company.gstin,
                "username": "irp-user",
                "base_url": "https://sandbox.example.com",
                "credential_reference": "ENV_IRP_SECRET",
                "notes": "Sandbox onboarded",
            },
        )

        self.assertRedirects(response, reverse("integrations:dashboard"))
        connector = IntegrationConnector.objects.get(company=self.company, connector_type=IntegrationConnector.TYPE_IRP)
        self.assertEqual(connector.provider_name, "Sandbox GSP")
        self.assertEqual(connector.status, IntegrationConnector.STATUS_READY)
        self.assertEqual(connector.credential_reference, "ENV_IRP_SECRET")
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="IntegrationConnector",
                record_id=connector.pk,
                action=AuditLog.ACTION_CREATE,
            ).exists()
        )

    def test_connected_bank_feed_import_page_is_available(self):
        response = self.client.get(reverse("integrations:bank_feed_import"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Connected Banking Feed Import")
        self.assertContains(response, "Import and Auto-Match")

    def test_connected_bank_feed_import_creates_statement_and_skips_duplicate(self):
        bank_group = AccountGroup.objects.create(
            company=self.company,
            name="Bank Accounts",
            nature="Asset",
        )
        bank_ledger = Ledger.objects.create(
            company=self.company,
            name="HDFC Current Account",
            account_group=bank_group,
        )
        receipt = Voucher.objects.create(
            company=self.company,
            voucher_type="Receipt",
            date=date(2026, 5, 3),
            narration="NEFT receipt from GST Customer",
        )
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=bank_ledger,
            entry_type="DR",
            amount=Decimal("118.00"),
        )
        VoucherItem.objects.create(
            voucher=receipt,
            ledger=self.customer,
            entry_type="CR",
            amount=Decimal("118.00"),
        )
        receipt.approve(None)
        feed = b"Date,Narration,Credit,Debit,Balance\n2026-05-03,NEFT GST Customer INV001,118.00,,5118.00\n"

        response = self.client.post(
            reverse("integrations:bank_feed_import"),
            {
                "provider_name": "HDFC CSV Feed",
                "account_ledger": bank_ledger.pk,
                "statement_date": "2026-05-03",
                "feed_file": SimpleUploadedFile("hdfc-feed.csv", feed, content_type="text/csv"),
            },
        )

        self.assertEqual(response.status_code, 302)
        statement = BankStatement.objects.get(company=self.company, account_ledger=bank_ledger)
        self.assertRedirects(response, reverse("core:bank_statement_detail", args=[statement.pk]))
        self.assertEqual(statement.rows.count(), 1)
        row = BankStatementRow.objects.get(statement=statement)
        self.assertTrue(row.is_reconciled)
        self.assertEqual(row.matched_voucher, receipt)
        connector = IntegrationConnector.objects.get(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_BANK,
        )
        self.assertEqual(connector.status, IntegrationConnector.STATUS_LIVE)
        self.assertEqual(connector.provider_name, "HDFC CSV Feed")
        self.assertIsNotNone(connector.last_success_at)
        self.assertTrue(
            IntegrationRequestLog.objects.filter(
                company=self.company,
                service=IntegrationRequestLog.SERVICE_BANK_FEED,
                status=IntegrationRequestLog.STATUS_SUCCESS,
                response_code="imported",
                response_payload__statement_id=statement.pk,
                response_payload__auto_matched=1,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="BankStatement",
                record_id=statement.pk,
                new_data__source="connected_bank_feed_import",
            ).exists()
        )

        duplicate_response = self.client.post(
            reverse("integrations:bank_feed_import"),
            {
                "provider_name": "HDFC CSV Feed",
                "account_ledger": bank_ledger.pk,
                "statement_date": "2026-05-03",
                "feed_file": SimpleUploadedFile("hdfc-feed.csv", feed, content_type="text/csv"),
            },
        )

        self.assertRedirects(duplicate_response, reverse("core:bank_statement_detail", args=[statement.pk]))
        self.assertEqual(BankStatement.objects.filter(company=self.company, account_ledger=bank_ledger).count(), 1)
        self.assertTrue(
            IntegrationRequestLog.objects.filter(
                company=self.company,
                service=IntegrationRequestLog.SERVICE_BANK_FEED,
                status=IntegrationRequestLog.STATUS_SUCCESS,
                response_code="duplicate_skipped",
                response_payload__duplicate=True,
            ).exists()
        )

    def test_statutory_integration_control_room_shows_blockers_and_exports_csv(self):
        IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_GST,
            provider_name="GSTN GSP",
            mode=IntegrationConnector.MODE_PRODUCTION,
            status=IntegrationConnector.STATUS_LIVE,
            gstin=self.company.gstin,
            credential_reference="ENV_GST_SECRET",
            credential_last_rotated_at=timezone.now() - timezone.timedelta(days=30),
            last_success_at=timezone.now() - timezone.timedelta(hours=2),
        )
        IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_IRP,
            provider_name="IRP GSP",
            mode=IntegrationConnector.MODE_PRODUCTION,
            status=IntegrationConnector.STATUS_BLOCKED,
            gstin=self.company.gstin,
            credential_reference="ENV_IRP_SECRET",
            last_failure_at=timezone.now(),
            last_error="IRP authentication failed",
        )
        IntegrationRequestLog.objects.create(
            company=self.company,
            voucher=self.voucher,
            requested_by=self.user,
            provider="IRP GSP",
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            status=IntegrationRequestLog.STATUS_FAILED,
            error_message="IRP authentication failed",
        )

        response = self.client.get(reverse("integrations:statutory_control"), {"focus": "all"})

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Statutory Integration Control Room")
        self.assertContains(response, "GST Integration Co")
        self.assertContains(response, "IRP / E-Invoice")
        self.assertContains(response, "IRP authentication failed")
        self.assertContains(response, "Connected Banking")

        csv_response = self.client.get(reverse("integrations:statutory_control"), {"focus": "all", "export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(csv_response["Content-Type"], "text/csv; charset=utf-8")
        body = csv_response.content.decode("utf-8")
        self.assertIn("Company,Connector,Severity,Issue,Next Action", body)
        self.assertIn("GST Integration Co,IRP / E-Invoice,Critical", body)

    def test_statutory_integration_control_room_creates_idempotent_blocker_tasks(self):
        selection_key = f"company:{self.company.pk}:connector:{IntegrationConnector.TYPE_BANK}"

        response = self.client.post(
            reverse("integrations:statutory_control"),
            {
                "action": "create_tasks",
                "focus": "all",
                "connector_ids": [selection_key],
            },
        )

        self.assertEqual(response.status_code, 302)
        task = PracticeTask.objects.get(
            company=self.company,
            reference=f"INTCTL:{self.company.pk}:{IntegrationConnector.TYPE_BANK}",
        )
        self.assertEqual(task.task_type, PracticeTask.TYPE_BANK)
        self.assertEqual(task.priority, PracticeTask.PRIORITY_CRITICAL)
        self.assertIn("Connected Banking", task.description)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                record_id=task.pk,
                new_data__source="statutory_integration_control_room",
            ).exists()
        )

        self.client.post(
            reverse("integrations:statutory_control"),
            {
                "action": "create_tasks",
                "focus": "all",
                "connector_ids": [selection_key],
            },
        )
        self.assertEqual(
            PracticeTask.objects.filter(
                company=self.company,
                reference=f"INTCTL:{self.company.pk}:{IntegrationConnector.TYPE_BANK}",
            ).count(),
            1,
        )

    def test_connector_update_records_rotation_and_closes_resolved_control_task(self):
        connector = IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_IRP,
            provider_name="Old IRP",
            mode=IntegrationConnector.MODE_SANDBOX,
            status=IntegrationConnector.STATUS_NEEDS_SETUP,
            gstin=self.company.gstin,
            last_success_at=timezone.now(),
        )
        task = PracticeTask.objects.create(
            company=self.company,
            title="Fix IRP integration",
            task_type=PracticeTask.TYPE_GST,
            status=PracticeTask.STATUS_IN_PROGRESS,
            reference=f"INTCTL:{self.company.pk}:{IntegrationConnector.TYPE_IRP}",
            created_by=self.user,
            description="Connector missing credential reference.",
        )

        response = self.client.post(
            reverse("integrations:connector_update", args=[IntegrationConnector.TYPE_IRP]),
            {
                "provider_name": "Production IRP",
                "mode": IntegrationConnector.MODE_PRODUCTION,
                "status": IntegrationConnector.STATUS_LIVE,
                "gstin": self.company.gstin,
                "username": "irp-user",
                "base_url": "https://irp.example.com",
                "credential_reference": "VAULT_IRP_SECRET",
                "credential_last_rotated_at": "2026-05-03",
                "notes": "Production connector verified",
            },
        )

        self.assertRedirects(response, reverse("integrations:dashboard"))
        connector.refresh_from_db()
        self.assertEqual(connector.provider_name, "Production IRP")
        self.assertEqual(connector.status, IntegrationConnector.STATUS_LIVE)
        self.assertEqual(connector.credential_reference, "VAULT_IRP_SECRET")
        self.assertEqual(connector.credential_last_rotated_at.date(), date(2026, 5, 3))
        task.refresh_from_db()
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)
        self.assertEqual(task.completed_by, self.user)
        self.assertIn("resolved through settings", task.description)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                record_id=task.pk,
                new_data__source="statutory_integration_control_room_auto_close",
            ).exists()
        )

    def test_provider_go_live_readiness_accepts_complete_production_stack(self):
        now = timezone.now()
        connector_data = [
            (IntegrationConnector.TYPE_GST, IntegrationRequestLog.SERVICE_GST_RETURN, {"gstin": self.company.gstin}),
            (IntegrationConnector.TYPE_IRP, IntegrationRequestLog.SERVICE_E_INVOICE, {"gstin": self.company.gstin}),
            (IntegrationConnector.TYPE_EWAY, IntegrationRequestLog.SERVICE_E_WAY_BILL, {"gstin": self.company.gstin}),
            (IntegrationConnector.TYPE_TRACES, IntegrationRequestLog.SERVICE_TRACES, {"tan": "MUMA12345A", "username": "traces-user"}),
        ]
        for connector_type, service, identity in connector_data:
            IntegrationConnector.objects.create(
                company=self.company,
                connector_type=connector_type,
                provider_name=f"{connector_type.upper()} Provider",
                mode=IntegrationConnector.MODE_PRODUCTION,
                status=IntegrationConnector.STATUS_LIVE,
                base_url="https://provider.example.com",
                credential_reference=f"VAULT_{connector_type.upper()}",
                credential_last_rotated_at=now - timezone.timedelta(days=10),
                last_success_at=now - timezone.timedelta(hours=1),
                metadata=self._production_evidence(connector_type.upper()),
                **identity,
            )
            IntegrationRequestLog.objects.create(
                company=self.company,
                voucher=self.voucher if service in {IntegrationRequestLog.SERVICE_E_INVOICE, IntegrationRequestLog.SERVICE_E_WAY_BILL} else None,
                requested_by=self.user,
                provider=f"{connector_type.upper()} Provider",
                service=service,
                status=IntegrationRequestLog.STATUS_SUCCESS,
                response_code="200",
                response_payload={"ok": True},
            )

        readiness = build_provider_go_live_readiness(self.company)

        self.assertEqual(readiness["status"], "production_ready")
        self.assertGreaterEqual(readiness["score"], 90)
        self.assertEqual(readiness["totals"]["critical_checks"], 0)
        self.assertEqual(readiness["retry_summary"]["open"], 0)

    def test_provider_readiness_requires_production_certification_evidence(self):
        now = timezone.now()
        connector = IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_IRP,
            provider_name="IRP Provider",
            mode=IntegrationConnector.MODE_PRODUCTION,
            status=IntegrationConnector.STATUS_LIVE,
            gstin=self.company.gstin,
            base_url="https://irp.example.com",
            credential_reference="VAULT_IRP",
            credential_last_rotated_at=now - timezone.timedelta(days=10),
            last_success_at=now - timezone.timedelta(hours=1),
        )
        IntegrationRequestLog.objects.create(
            company=self.company,
            voucher=self.voucher,
            requested_by=self.user,
            provider="IRP Provider",
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            status=IntegrationRequestLog.STATUS_SUCCESS,
            response_code="200",
            response_payload={"ok": True},
        )

        readiness = build_provider_go_live_readiness(self.company)
        row = next(item for item in readiness["connector_rows"] if item["type"] == connector.connector_type)

        self.assertEqual(row["certification"]["missing_count"], len(PRODUCTION_EVIDENCE_FIELDS))
        self.assertIn("Production Approval Ref", [check["name"] for check in row["checks"] if check["level"] == "critical"])
        self.assertGreaterEqual(readiness["certification"]["missing"], len(PRODUCTION_EVIDENCE_FIELDS))
        self.assertNotEqual(readiness["status"], "production_ready")

    def test_connector_update_persists_production_certification_evidence(self):
        evidence_payload = self._production_evidence("IRP")
        post_data = {
            "provider_name": "IRP Provider",
            "mode": IntegrationConnector.MODE_PRODUCTION,
            "status": IntegrationConnector.STATUS_LIVE,
            "gstin": self.company.gstin,
            "username": "irp-user",
            "base_url": "https://irp.example.com",
            "credential_reference": "VAULT_IRP",
            "credential_last_rotated_at": "2026-05-03",
            "notes": "Production certification complete",
        }
        for key, value in evidence_payload.items():
            post_data[f"evidence_{key}"] = value

        response = self.client.post(
            reverse("integrations:connector_update", args=[IntegrationConnector.TYPE_IRP]),
            post_data,
        )

        self.assertRedirects(response, reverse("integrations:dashboard"))
        connector = IntegrationConnector.objects.get(company=self.company, connector_type=IntegrationConnector.TYPE_IRP)
        for key, value in evidence_payload.items():
            self.assertEqual(connector.metadata[key], value)

    def test_provider_readiness_queues_failed_requests_idempotently(self):
        connector = IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_IRP,
            provider_name="IRP Provider",
            mode=IntegrationConnector.MODE_PRODUCTION,
            status=IntegrationConnector.STATUS_LIVE,
            gstin=self.company.gstin,
            base_url="https://irp.example.com",
            credential_reference="VAULT_IRP",
            credential_last_rotated_at=timezone.now(),
        )
        log = IntegrationRequestLog.objects.create(
            company=self.company,
            voucher=self.voucher,
            requested_by=self.user,
            provider="IRP Provider",
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            status=IntegrationRequestLog.STATUS_FAILED,
            error_message="IRP auth token expired",
        )

        response = self.client.post(
            reverse("integrations:provider_readiness"),
            {"action": "queue_failed_requests"},
        )

        self.assertRedirects(response, reverse("integrations:provider_readiness"))
        job = IntegrationRetryJob.objects.get(request_log=log)
        self.assertEqual(job.company, self.company)
        self.assertEqual(job.connector, connector)
        self.assertEqual(job.status, IntegrationRetryJob.STATUS_PENDING)
        self.assertEqual(job.priority, IntegrationRetryJob.PRIORITY_CRITICAL)
        self.assertEqual(job.last_error, "IRP auth token expired")
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="IntegrationRetryJob",
                record_id=job.pk,
                new_data__source="provider_go_live_readiness",
            ).exists()
        )

        self.client.post(reverse("integrations:provider_readiness"), {"action": "queue_failed_requests"})
        self.assertEqual(IntegrationRetryJob.objects.filter(request_log=log).count(), 1)

    def test_provider_readiness_syncs_gate_tasks_and_resolves_retry_jobs(self):
        failed_log = IntegrationRequestLog.objects.create(
            company=self.company,
            voucher=self.voucher,
            requested_by=self.user,
            provider="IRP Provider",
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            status=IntegrationRequestLog.STATUS_FAILED,
            error_message="IRP schema rejected",
        )
        job = IntegrationRetryJob.objects.create(
            company=self.company,
            request_log=failed_log,
            voucher=self.voucher,
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            provider="IRP Provider",
            status=IntegrationRetryJob.STATUS_PENDING,
            priority=IntegrationRetryJob.PRIORITY_CRITICAL,
            last_error="IRP schema rejected",
            created_by=self.user,
        )

        sync_response = self.client.post(
            reverse("integrations:provider_readiness"),
            {"action": "sync_provider_tasks"},
        )
        self.assertRedirects(sync_response, reverse("integrations:provider_readiness"))
        task = PracticeTask.objects.filter(
            company=self.company,
            reference__startswith=f"PROVIDERREADY:{self.company.pk}:",
        ).first()
        self.assertIsNotNone(task)
        self.assertIn("Provider readiness", task.title)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                record_id=task.pk,
                new_data__source="provider_go_live_readiness",
            ).exists()
        )

        resolve_response = self.client.post(
            reverse("integrations:provider_retry_job_update", args=[job.pk]),
            {"action": "resolve_retry_job", "note": "Replayed manually in provider portal."},
        )
        self.assertRedirects(resolve_response, reverse("integrations:provider_readiness"))
        job.refresh_from_db()
        self.assertEqual(job.status, IntegrationRetryJob.STATUS_RESOLVED)
        self.assertEqual(job.resolved_by, self.user)
        self.assertIn("Replayed manually", job.last_error)

    @override_settings(GST_API_PROVIDER="mock", GST_API_SANDBOX_MODE=True, E_INVOICE_ENABLED=True)
    def test_retry_dispatcher_resolves_due_e_invoice_job(self):
        connector = IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_IRP,
            provider_name="Mock IRP",
            mode=IntegrationConnector.MODE_SANDBOX,
            status=IntegrationConnector.STATUS_READY,
            gstin=self.company.gstin,
            base_url="https://mock-irp.example.com",
            credential_reference="VAULT_IRP",
            credential_last_rotated_at=timezone.now(),
        )
        failed_log = IntegrationRequestLog.objects.create(
            company=self.company,
            voucher=self.voucher,
            requested_by=self.user,
            provider="Mock IRP",
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            status=IntegrationRequestLog.STATUS_FAILED,
            error_message="Temporary provider timeout",
        )
        job = IntegrationRetryJob.objects.create(
            company=self.company,
            connector=connector,
            request_log=failed_log,
            voucher=self.voucher,
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            provider="Mock IRP",
            status=IntegrationRetryJob.STATUS_PENDING,
            next_attempt_at=timezone.now() - timezone.timedelta(minutes=1),
            last_error="Temporary provider timeout",
            created_by=self.user,
        )

        response = self.client.post(
            reverse("integrations:provider_readiness"),
            {"action": "run_due_retries"},
        )

        self.assertRedirects(response, reverse("integrations:provider_readiness"))
        job.refresh_from_db()
        connector.refresh_from_db()
        self.voucher.refresh_from_db()
        self.assertEqual(job.status, IntegrationRetryJob.STATUS_RESOLVED)
        self.assertEqual(job.attempts, 1)
        self.assertTrue(self.voucher.e_invoice_irn.startswith("MOCKIRN"))
        self.assertEqual(connector.status, IntegrationConnector.STATUS_LIVE)
        self.assertIsNotNone(connector.last_success_at)
        self.assertTrue(
            IntegrationRequestLog.objects.filter(
                company=self.company,
                voucher=self.voucher,
                service=IntegrationRequestLog.SERVICE_E_INVOICE,
                status=IntegrationRequestLog.STATUS_SUCCESS,
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="IntegrationRetryJob",
                record_id=job.pk,
                new_data__source="integration_retry_dispatcher_resolved",
            ).exists()
        )

    @override_settings(GST_API_PROVIDER="mock", GST_API_SANDBOX_MODE=True, E_WAY_BILL_ENABLED=True)
    def test_process_integration_retries_command_dry_run_reports_due_jobs(self):
        IntegrationRetryJob.objects.create(
            company=self.company,
            voucher=self.voucher,
            service=IntegrationRequestLog.SERVICE_E_WAY_BILL,
            provider="Mock EWB",
            status=IntegrationRetryJob.STATUS_PENDING,
            next_attempt_at=timezone.now() - timezone.timedelta(minutes=1),
            last_error="Portal busy",
            created_by=self.user,
        )
        output = StringIO()

        call_command("process_integration_retries", "--company", self.company.short_code, "--dry-run", "--json", stdout=output)

        payload = json.loads(output.getvalue())
        self.assertTrue(payload["dry_run"])
        self.assertEqual(payload["processed"], 0)
        self.assertEqual(payload["jobs"][0]["status"], "ready")
        self.assertEqual(IntegrationRetryJob.objects.get(company=self.company).status, IntegrationRetryJob.STATUS_PENDING)

    def test_e_invoice_cockpit_lists_ready_and_blocked_invoices(self):
        blocked_customer = Ledger.objects.create(
            company=self.company,
            name="Customer Missing GSTIN",
            account_group=self.customer.account_group,
        )
        blocked = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 5, 2),
            status="PENDING",
            place_of_supply="27",
            dispatch_pincode=400001,
            ship_to_pincode=400002,
        )
        VoucherItem.objects.create(voucher=blocked, ledger=blocked_customer, entry_type="DR", amount=Decimal("118.00"))
        VoucherItem.objects.create(voucher=blocked, ledger=self.sales, entry_type="CR", amount=Decimal("100.00"))
        VoucherItem.objects.create(voucher=blocked, ledger=self.cgst, entry_type="CR", amount=Decimal("9.00"))
        VoucherItem.objects.create(voucher=blocked, ledger=self.sgst, entry_type="CR", amount=Decimal("9.00"))
        blocked.approve(None)

        response = self.client.get(
            reverse("integrations:e_invoice_cockpit"),
            {"start_date": "2026-05-01", "end_date": "2026-05-31", "status": "all"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "IRP / E-Invoice Cockpit")
        self.assertContains(response, self.voucher.number)
        self.assertContains(response, blocked.number)
        self.assertContains(response, "Ready")
        self.assertContains(response, "Blocked")
        self.assertContains(response, "Customer GSTIN is required")

    @override_settings(GST_API_PROVIDER="mock", E_INVOICE_ENABLED=True)
    def test_e_invoice_cockpit_generate_creates_irn_and_updates_connector(self):
        connector = IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_IRP,
            provider_name="Mock IRP",
            mode=IntegrationConnector.MODE_SANDBOX,
            status=IntegrationConnector.STATUS_READY,
            gstin=self.company.gstin,
            credential_reference="ENV_IRP_SECRET",
        )
        task = PracticeTask.objects.create(
            company=self.company,
            title="Fix IRP integration",
            task_type=PracticeTask.TYPE_GST,
            status=PracticeTask.STATUS_IN_PROGRESS,
            reference=f"INTCTL:{self.company.pk}:{IntegrationConnector.TYPE_IRP}",
            created_by=self.user,
            description="Waiting for successful IRP provider run.",
        )
        next_url = reverse("integrations:e_invoice_cockpit") + "?status=pending"

        response = self.client.post(
            reverse("integrations:e_invoice_cockpit_generate", args=[self.voucher.pk]),
            {"next": next_url},
        )

        self.assertRedirects(response, next_url)
        self.voucher.refresh_from_db()
        connector.refresh_from_db()
        self.assertTrue(self.voucher.e_invoice_irn.startswith("MOCKIRN"))
        self.assertEqual(connector.status, IntegrationConnector.STATUS_LIVE)
        self.assertIsNotNone(connector.last_success_at)
        task.refresh_from_db()
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)
        self.assertEqual(task.completed_by, self.user)
        self.assertIn("resolved through settings", task.description)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="Voucher",
                record_id=self.voucher.pk,
                new_data__source="gst_provider",
            ).exists()
        )
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                record_id=task.pk,
                new_data__source="statutory_integration_control_room_auto_close",
            ).exists()
        )

    def test_manual_irn_capture_can_return_to_e_invoice_cockpit(self):
        next_url = reverse("integrations:e_invoice_cockpit") + "?status=pending"

        response = self.client.post(
            reverse("integrations:mark_e_invoice_status", args=[self.voucher.pk]),
            {
                "next": next_url,
                "irn": "COCKPITIRN1234567890",
                "ack_no": "ACK-COCKPIT",
                "status": "ACT",
            },
        )

        self.assertRedirects(response, next_url)
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.e_invoice_irn, "COCKPITIRN1234567890")

    def test_e_way_bill_cockpit_lists_ready_blocked_and_expiry_states(self):
        blocked = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 5, 2),
            status="PENDING",
            place_of_supply="27",
            dispatch_pincode=400001,
            ship_to_pincode=400002,
        )
        VoucherItem.objects.create(voucher=blocked, ledger=self.customer, entry_type="DR", amount=Decimal("118.00"))
        VoucherItem.objects.create(voucher=blocked, ledger=self.sales, entry_type="CR", amount=Decimal("100.00"))
        VoucherItem.objects.create(voucher=blocked, ledger=self.cgst, entry_type="CR", amount=Decimal("9.00"))
        VoucherItem.objects.create(voucher=blocked, ledger=self.sgst, entry_type="CR", amount=Decimal("9.00"))
        blocked.approve(None)

        expiring = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 5, 3),
            status="PENDING",
            place_of_supply="27",
            dispatch_pincode=400001,
            ship_to_pincode=400002,
            transport_mode="1",
            transport_distance_km=25,
            vehicle_number="MH12AB1234",
            vehicle_type="R",
            e_way_bill_no="EWB-EXPIRING",
            e_way_bill_valid_until=timezone.now() + timezone.timedelta(hours=12),
        )
        VoucherItem.objects.create(voucher=expiring, ledger=self.customer, entry_type="DR", amount=Decimal("118.00"))
        VoucherItem.objects.create(voucher=expiring, ledger=self.sales, entry_type="CR", amount=Decimal("100.00"))
        VoucherItem.objects.create(voucher=expiring, ledger=self.cgst, entry_type="CR", amount=Decimal("9.00"))
        VoucherItem.objects.create(voucher=expiring, ledger=self.sgst, entry_type="CR", amount=Decimal("9.00"))
        expiring.approve(None)

        response = self.client.get(
            reverse("integrations:e_way_bill_cockpit"),
            {"start_date": "2026-05-01", "end_date": "2026-05-31", "status": "all"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "E-Way Bill Cockpit")
        self.assertContains(response, self.voucher.number)
        self.assertContains(response, blocked.number)
        self.assertContains(response, "transMode is required")
        self.assertContains(response, "Expiring Soon")
        self.assertContains(response, "EWB-EXPIRING")

    @override_settings(GST_API_PROVIDER="mock", E_WAY_BILL_ENABLED=True)
    def test_e_way_bill_cockpit_generate_creates_ewb_and_updates_connector(self):
        connector = IntegrationConnector.objects.create(
            company=self.company,
            connector_type=IntegrationConnector.TYPE_EWAY,
            provider_name="Mock EWB",
            mode=IntegrationConnector.MODE_SANDBOX,
            status=IntegrationConnector.STATUS_READY,
            gstin=self.company.gstin,
            credential_reference="ENV_EWAY_SECRET",
        )
        next_url = reverse("integrations:e_way_bill_cockpit") + "?status=pending"

        response = self.client.post(
            reverse("integrations:e_way_bill_cockpit_generate", args=[self.voucher.pk]),
            {"next": next_url},
        )

        self.assertRedirects(response, next_url)
        self.voucher.refresh_from_db()
        connector.refresh_from_db()
        self.assertTrue(self.voucher.e_way_bill_no)
        self.assertEqual(connector.status, IntegrationConnector.STATUS_LIVE)
        self.assertIsNotNone(connector.last_success_at)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="Voucher",
                record_id=self.voucher.pk,
                new_data__source="gst_provider",
            ).exists()
        )

    def test_manual_eway_capture_can_return_to_e_way_bill_cockpit(self):
        next_url = reverse("integrations:e_way_bill_cockpit") + "?status=pending"

        response = self.client.post(
            reverse("integrations:mark_e_way_bill_status", args=[self.voucher.pk]),
            {
                "next": next_url,
                "e_way_bill_no": "181716151413",
                "e_way_bill_date": "2026-05-01T13:00:00",
                "valid_until": "2026-05-02T23:59:00",
                "status": "ACT",
            },
        )

        self.assertRedirects(response, next_url)
        self.voucher.refresh_from_db()
        self.assertEqual(self.voucher.e_way_bill_no, "181716151413")

    def test_evidence_center_shows_export_and_provider_audit_logs(self):
        StatutoryExportLog.objects.create(
            company=self.company,
            generated_by=self.user,
            export_type=StatutoryExportLog.TYPE_GSTR1_JSON,
            status=StatutoryExportLog.STATUS_GENERATED,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            file_name="GSTR1_GST_202605.json",
            file_sha256="a" * 64,
            row_count=2,
            amount_total=Decimal("118.00"),
            validation_summary={"b2b_rows": 1},
        )
        IntegrationRequestLog.objects.create(
            company=self.company,
            voucher=self.voucher,
            requested_by=self.user,
            provider="mock",
            service=IntegrationRequestLog.SERVICE_E_WAY_BILL,
            status=IntegrationRequestLog.STATUS_FAILED,
            request_digest="b" * 64,
            error_message="Vehicle number format invalid",
        )

        response = self.client.get(reverse("integrations:evidence_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Statutory Evidence Center")
        self.assertContains(response, "GSTR1_GST_202605.json")
        self.assertContains(response, "GSTR-1 JSON")
        self.assertContains(response, "Vehicle number format invalid")
        self.assertContains(response, "Config Errors")
        self.assertContains(response, "CSV Handover")

    def test_evidence_center_exports_csv_handover(self):
        StatutoryExportLog.objects.create(
            company=self.company,
            generated_by=self.user,
            export_type=StatutoryExportLog.TYPE_GSTR3B_JSON,
            status=StatutoryExportLog.STATUS_GENERATED,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            file_name="GSTR3B_GST_202605.json",
            file_sha256="c" * 64,
            row_count=1,
            amount_total=Decimal("18.00"),
        )
        IntegrationRequestLog.objects.create(
            company=self.company,
            voucher=self.voucher,
            requested_by=self.user,
            provider="mock",
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            status=IntegrationRequestLog.STATUS_SUCCESS,
            request_digest="d" * 64,
        )

        response = self.client.get(reverse("integrations:evidence_center"), {"export": "csv"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        body = response.content.decode("utf-8")
        self.assertIn("Evidence Type,Date,Company,Category,Status", body)
        self.assertIn("Statutory Export", body)
        self.assertIn("GSTR-3B JSON", body)
        self.assertIn("Integration Request", body)
        self.assertIn("E-Invoice", body)

    @override_settings(GST_API_PROVIDER="")
    def test_unconfigured_provider_returns_503_and_logs_config_error(self):
        response = self.client.post(
            reverse("integrations:generate_e_invoice_api", args=[self.voucher.pk])
        )

        self.assertEqual(response.status_code, 503)
        self.assertTrue(
            IntegrationRequestLog.objects.filter(
                company=self.company,
                status=IntegrationRequestLog.STATUS_CONFIG_ERROR,
            ).exists()
        )

    @override_settings(WHATSAPP_WEBHOOK_TOKEN="secret-token")
    def test_whatsapp_document_webhook_can_resolve_company_by_intake_number(self):
        upload = SimpleUploadedFile(
            "bill.pdf",
            b"%PDF-1.4\n%",
            content_type="application/pdf",
        )

        response = self.client.post(
            reverse("integrations:whatsapp_document_webhook"),
            data={
                "to": "whatsapp:+91 98765 43210",
                "sender": "+919900001111",
                "file": upload,
            },
            HTTP_X_WEBHOOK_TOKEN="secret-token",
        )

        self.assertEqual(response.status_code, 200, response.content)
        submission = OCRSubmission.objects.get(company=self.company)
        self.assertEqual(submission.source, OCRSubmission.SOURCE_WHATSAPP)
        self.assertIn("+919900001111", submission.ocr_error)
