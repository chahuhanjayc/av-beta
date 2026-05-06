import hashlib
import json
from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from integrations.models import IntegrationConnector, IntegrationRequestLog, StatutoryExportLog
from integrations.readiness import CONNECTOR_SERVICE_MAP, PRODUCTION_EVIDENCE_FIELDS
from ledger.models import AccountGroup, Ledger
from portal.models import ClientDocumentRequest, PortalUser
from vouchers.models import Voucher

from .market_proof import MARKET_PROOF_TASK_PREFIX, build_market_proof_pack
from .models import (
    AuditLog,
    ClientEngagement,
    Company,
    GSTEvidenceDocument,
    MarketProofCaseStudy,
    MarketProofExternalEvidence,
    PilotFeedback,
    PracticeTask,
    UserCompanyAccess,
)


class MarketProofPackTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="market-proof@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        self.proven_company = self._company("Proven Market Co", "PMC")
        self.gap_company = self._company("Gap Market Co", "GMC")
        UserCompanyAccess.objects.create(user=self.user, company=self.proven_company, role="Admin")
        UserCompanyAccess.objects.create(user=self.user, company=self.gap_company, role="Admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.proven_company.pk
        session.save()

    def _company(self, name, code):
        return Company.objects.create(
            name=name,
            gstin=f"27{code}AB1234C1Z5",
            tan=f"{code}A12345B",
            short_code=code,
            financial_year_start=date(2026, 4, 1),
        )

    def _make_proven(self, company):
        today = timezone.localdate()
        group = AccountGroup.objects.create(company=company, name="Market Debtors", nature="Asset")
        ledger = Ledger.objects.create(company=company, name="Market Pilot Client", account_group=group)
        portal_user = PortalUser.objects.create(
            name="Market Pilot",
            email=f"market-{company.pk}@example.com",
            linked_ledger=ledger,
            is_active=True,
        )
        portal_user.set_password("client-password")
        portal_user.save()
        ClientDocumentRequest.objects.create(
            company=company,
            portal_user=portal_user,
            recipient_email=portal_user.email,
            title="Market proof upload",
            document_type=ClientDocumentRequest.TYPE_OTHER,
            status=ClientDocumentRequest.STATUS_CLOSED,
            due_date=today + timezone.timedelta(days=5),
            uploaded_at=timezone.now(),
            closed_at=timezone.now(),
            requested_by=self.user,
        )
        ClientSubscription.objects.create(
            company=company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timezone.timedelta(days=60),
        )
        ClientEngagement.objects.create(
            company=company,
            status=ClientEngagement.STATUS_ACTIVE,
            service_package=ClientEngagement.PACKAGE_FULL_ACCOUNTING,
            monthly_retainer=Decimal("35000.00"),
            partner_owner=self.user,
            scope_summary="Market proof pilot covers GST, invoice, portal, and migration replacement workflows.",
            risk_rating=ClientEngagement.RISK_LOW,
            last_reviewed_at=today,
        )
        Voucher.objects.create(company=company, date=today, voucher_type="Sales", status="APPROVED")
        AuditLog.objects.create(
            company=company,
            user=self.user,
            action=AuditLog.ACTION_CREATE,
            model_name="MarketProof",
            record_id=0,
            object_repr="Market proof usage",
            old_data={},
            new_data={"source": "test"},
        )
        PilotFeedback.objects.create(
            company=company,
            feedback_type=PilotFeedback.TYPE_CONVERSION_SIGNAL,
            sentiment=PilotFeedback.SENTIMENT_POSITIVE,
            confidence_score=9,
            severity=PilotFeedback.SEVERITY_LOW,
            status=PilotFeedback.STATUS_RESOLVED,
            occurred_on=today,
            assigned_to=self.user,
            recorded_by=self.user,
            resolved_at=timezone.now(),
            competitor_reference=PilotFeedback.COMPETITOR_TALLY,
            evidence_reference="MP-CALL-001",
            summary="Client confirmed Akshaya can replace the Tally pilot workflow",
        )
        PracticeTask.objects.create(
            company=company,
            title="Market proof loop closed",
            task_type=PracticeTask.TYPE_OTHER,
            priority=PracticeTask.PRIORITY_NORMAL,
            status=PracticeTask.STATUS_DONE,
            due_date=today,
            assigned_to=self.user,
            created_by=self.user,
            completed_by=self.user,
            completed_at=timezone.now(),
            reference=f"{MARKET_PROOF_TASK_PREFIX}{company.pk}:closed_loop_test",
        )
        MarketProofCaseStudy.objects.create(
            company=company,
            title="Tally to Akshaya market proof story",
            status=MarketProofCaseStudy.STATUS_APPROVED,
            outcome=MarketProofCaseStudy.OUTCOME_PAID,
            migration_source=MarketProofCaseStudy.SOURCE_TALLY,
            client_contact="Finance Owner",
            client_role="Owner",
            testimonial_quote="Akshaya replaced our Tally pilot workflow with cleaner GST and document follow-up.",
            publish_consent=True,
            consent_reference="CONSENT-MARKET-001",
            evidence_reference="CASE-MARKET-001",
            before_process_hours=Decimal("8.00"),
            after_process_hours=Decimal("3.00"),
            monthly_documents=120,
            monthly_invoices=45,
            gst_periods_completed=2,
            tally_parallel_run_days=14,
            commercial_value=Decimal("35000.00"),
            owner=self.user,
            approved_by=self.user,
            approved_at=timezone.now(),
            created_by=self.user,
        )
        for category in [
            MarketProofExternalEvidence.CATEGORY_PROVIDER,
            MarketProofExternalEvidence.CATEGORY_PILOT,
            MarketProofExternalEvidence.CATEGORY_CASE_STUDY,
            MarketProofExternalEvidence.CATEGORY_STATUTORY,
            MarketProofExternalEvidence.CATEGORY_BACKUP,
            MarketProofExternalEvidence.CATEGORY_SECURITY,
        ]:
            MarketProofExternalEvidence.objects.create(
                company=company,
                category=category,
                status=MarketProofExternalEvidence.STATUS_VERIFIED,
                source=MarketProofExternalEvidence.SOURCE_CA,
                title=f"Verified {category} proof",
                evidence_reference=f"EXT-{category.upper()}-{company.pk}",
                artifact_sha256="b" * 64,
                owner=self.user,
                verified_by=self.user,
                verified_at=timezone.now(),
                created_by=self.user,
            )
        GSTEvidenceDocument.objects.create(
            company=company,
            period_start=today.replace(day=1),
            period_end=today,
            evidence_type=GSTEvidenceDocument.TYPE_GSTR3B_ACK,
            return_type=GSTEvidenceDocument.RETURN_GSTR3B,
            title="Market proof GST acknowledgement",
            file=SimpleUploadedFile("market-proof.pdf", b"%PDF-1.4\nproof", content_type="application/pdf"),
            arn_ack_number="ARN-MARKET-PROOF",
            uploaded_by=self.user,
        )
        StatutoryExportLog.objects.create(
            company=company,
            generated_by=self.user,
            export_type=StatutoryExportLog.TYPE_GSTR1_JSON,
            status=StatutoryExportLog.STATUS_VALIDATED,
            period_start=today.replace(day=1),
            period_end=today,
            file_name="market-proof-gstr1.json",
            file_sha256="a" * 64,
            row_count=1,
            amount_total=Decimal("1000.00"),
        )
        self._make_provider_stack(company)

    def _make_provider_stack(self, company):
        now = timezone.now()
        evidence = {field["key"]: f"MP-{field['key']}" for field in PRODUCTION_EVIDENCE_FIELDS}
        for connector_type, service in [
            (IntegrationConnector.TYPE_GST, IntegrationRequestLog.SERVICE_GST_RETURN),
            (IntegrationConnector.TYPE_IRP, IntegrationRequestLog.SERVICE_E_INVOICE),
            (IntegrationConnector.TYPE_EWAY, IntegrationRequestLog.SERVICE_E_WAY_BILL),
            (IntegrationConnector.TYPE_TRACES, IntegrationRequestLog.SERVICE_TRACES),
        ]:
            connector = IntegrationConnector.objects.create(
                company=company,
                connector_type=connector_type,
                display_name=dict(IntegrationConnector.CONNECTOR_CHOICES)[connector_type],
                provider_name="Market Provider",
                mode=IntegrationConnector.MODE_PRODUCTION,
                status=IntegrationConnector.STATUS_LIVE,
                gstin=company.gstin if connector_type != IntegrationConnector.TYPE_TRACES else "",
                tan=company.tan if connector_type == IntegrationConnector.TYPE_TRACES else "",
                username="traces-user" if connector_type == IntegrationConnector.TYPE_TRACES else "",
                base_url="https://provider.example.test/api",
                credential_reference=f"vault://{connector_type}",
                credential_last_rotated_at=now - timezone.timedelta(days=10),
                last_success_at=now,
                metadata=evidence,
            )
            IntegrationRequestLog.objects.create(
                company=company,
                requested_by=self.user,
                provider=connector.provider_name,
                service=CONNECTOR_SERVICE_MAP[connector_type],
                status=IntegrationRequestLog.STATUS_SUCCESS,
                response_code="200",
                response_payload={"ok": True},
            )

    def test_market_proof_scores_proven_and_gap_clients(self):
        self._make_proven(self.proven_company)

        context = build_market_proof_pack(self.user, current_company=self.proven_company)
        rows = {row["company"].pk: row for row in context["rows"]}

        self.assertIn(rows[self.proven_company.pk]["band"], {"Market Ready", "Proven Pilot"})
        self.assertGreaterEqual(rows[self.proven_company.pk]["score"], 78)
        self.assertEqual(rows[self.proven_company.pk]["critical_count"], 0)
        self.assertGreater(rows[self.proven_company.pk]["proof_signal_count"], 0)
        self.assertEqual(rows[self.proven_company.pk]["case_study_signals"]["publishable_count"], 1)
        self.assertEqual(rows[self.proven_company.pk]["external_evidence_signals"]["missing_required_count"], 0)
        self.assertEqual(rows[self.proven_company.pk]["provider_cert_missing"], 0)
        self.assertEqual(rows[self.gap_company.pk]["band"], "Blocked")
        self.assertGreater(rows[self.gap_company.pk]["critical_count"], 0)

    def test_market_proof_page_csv_search_and_task_sync(self):
        self._make_proven(self.proven_company)

        response = self.client.get(reverse("core:market_proof_pack"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Market Proof Pack")
        self.assertContains(response, "Client Proof Queue")
        self.assertContains(response, "Evidence Pack")
        self.assertContains(response, "External Evidence")

        search_payload = self.client.get(reverse("core:api_search"), {"q": "market proof"}).json()
        self.assertIn("Market Proof Pack", {item["name"] for item in search_payload["navigation"]})
        self.assertIn("Market Proof Evidence Pack", {item["name"] for item in search_payload["navigation"]})

        csv_response = self.client.get(reverse("core:market_proof_pack"), {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,GSTIN,Proof Score", csv_text)
        self.assertIn("Verified External Evidence", csv_text)
        self.assertIn("Gap Market Co", csv_text)

        create_response = self.client.post(reverse("core:market_proof_pack"), {"band": "blocked"})
        self.assertRedirects(create_response, f"{reverse('core:market_proof_pack')}?band=blocked")
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.gap_company,
                reference__startswith=f"{MARKET_PROOF_TASK_PREFIX}{self.gap_company.pk}:",
            ).exists()
        )

    def test_market_proof_evidence_pack_downloads_signed_redacted_payload(self):
        self._make_proven(self.proven_company)

        response = self.client.get(reverse("core:market_proof_evidence_pack"), {"q": "Proven"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/json; charset=utf-8")
        self.assertIn("attachment;", response["Content-Disposition"])

        payload_text = response.content.decode("utf-8")
        pack = json.loads(payload_text)
        digest = pack["sha256"]
        clean_pack = dict(pack)
        clean_pack.pop("sha256")
        expected_digest = hashlib.sha256(
            json.dumps(clean_pack, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
        ).hexdigest()

        self.assertTrue(pack["pack_id"].startswith("MPP-"))
        self.assertEqual(digest, expected_digest)
        self.assertEqual(len(digest), 64)
        self.assertFalse(pack["redaction"]["raw_credentials_included"])
        self.assertNotIn("vault://", payload_text)
        self.assertEqual(pack["filters"]["q"], "proven")
        self.assertEqual(pack["current_company"]["id"], self.proven_company.pk)
        self.assertEqual(len(pack["clients"]), 1)
        self.assertEqual(pack["clients"][0]["company"]["name"], "Proven Market Co")
        self.assertGreaterEqual(pack["clients"][0]["score"], 78)
        self.assertIn("platform", pack)
        self.assertIn("evidence_gaps", pack)
        self.assertEqual(pack["clients"][0]["provider"]["certification"]["missing"], 0)
        self.assertEqual(pack["clients"][0]["external_evidence_signals"]["missing_required_count"], 0)
        self.assertEqual(pack["clients"][0]["external_evidence_signals"]["verified_count"], 6)
        self.assertEqual(len(pack["clients"][0]["provider"]["connectors"]), 4)
        self.assertTrue(pack["clients"][0]["provider"]["connectors"][0]["connector"]["credential_reference_recorded"])
