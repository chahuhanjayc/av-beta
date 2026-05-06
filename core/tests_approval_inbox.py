from datetime import date, timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from core.models import Company, ComplianceFiling, FilingReview, UserCompanyAccess
from integrations.models import IntegrationRequestLog, StatutoryExportLog
from migration.models import ImportSession


class CAApprovalInboxTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Approval Inbox Co",
            gstin="27INBOX0000I1Z5",
            short_code="AIC",
        )
        self.user = get_user_model().objects.create_user(
            email="approval-inbox@example.com",
            password="approval-pass",
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

    def _create_inbox_fixtures(self):
        ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/blocked.csv",
            file_type="csv",
            status="parsed",
            source_system=ImportSession.SOURCE_TALLY_EXCEL,
            sync_mode=ImportSession.SYNC_REPLACE_PERIOD,
            source_company_guid="TALLY-INBOX-001",
            source_period_start=date(2026, 5, 1),
            source_period_end=date(2026, 5, 31),
            source_file_hash="a" * 64,
            import_fingerprint="b" * 64,
            detected_mapping={"date": "Date", "ledger": "Ledger", "debit": "Debit", "credit": "Credit"},
            ledger_mapping={},
            duplicate_voucher_count=1,
            validation_report={"issues": []},
        )
        FilingReview.objects.create(
            company=self.company,
            review_type=FilingReview.TYPE_GST_MONTHLY,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            status=FilingReview.STATUS_REVIEWED,
            readiness_score=84,
            risk_score=16,
            reviewed_by=self.user,
            blocker_snapshot={
                "approval": {
                    "unwaived_critical_count": 0,
                    "unwaived_warning_count": 2,
                }
            },
        )
        ComplianceFiling.objects.create(
            company=self.company,
            filing_type=ComplianceFiling.TYPE_GSTR1,
            title="GSTR-1 May 2026",
            status=ComplianceFiling.STATUS_READY_FOR_REVIEW,
            due_date=timezone.localdate() + timedelta(days=3),
            reviewer=self.user,
        )
        IntegrationRequestLog.objects.create(
            company=self.company,
            provider="Manual IRP",
            service=IntegrationRequestLog.SERVICE_E_INVOICE,
            status=IntegrationRequestLog.STATUS_FAILED,
            response_code="500",
            error_message="IRP rejected the e-invoice payload.",
            requested_by=self.user,
        )
        StatutoryExportLog.objects.create(
            company=self.company,
            generated_by=self.user,
            export_type=StatutoryExportLog.TYPE_GSTR1_JSON,
            status=StatutoryExportLog.STATUS_REJECTED,
            period_start=date(2026, 5, 1),
            period_end=date(2026, 5, 31),
            file_name="gstr1.json",
            file_sha256="c" * 64,
            row_count=4,
        )

    def test_ca_approval_inbox_surfaces_cross_workflow_queue(self):
        self._create_inbox_fixtures()

        response = self.client.get(reverse("core:ca_approval_inbox"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "CA Approval Inbox")
        self.assertContains(response, "Tally import approval required")
        self.assertContains(response, "Filing review awaiting approval")
        self.assertContains(response, "Compliance filing ready for CA review")
        self.assertContains(response, "E-Invoice failure")
        self.assertContains(response, "GSTR-1 JSON rejected")

    def test_ca_approval_inbox_filters_and_exports_csv(self):
        self._create_inbox_fixtures()

        filtered = self.client.get(reverse("core:ca_approval_inbox"), {"category": "Tally Migration"})

        self.assertEqual(filtered.status_code, 200)
        self.assertContains(filtered, "Tally import approval required")
        self.assertNotContains(filtered, "E-Invoice failure")

        export = self.client.get(reverse("core:ca_approval_inbox"), {"export": "csv"})
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export["Content-Type"], "text/csv")
        body = export.content.decode()
        self.assertIn("Company,Category,Severity,Status,Title", body)
        self.assertIn("Tally import approval required", body)
        self.assertIn("E-Invoice failure", body)
