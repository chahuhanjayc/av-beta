from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription

from .market_external_evidence import (
    EXTERNAL_EVIDENCE_TASK_PREFIX,
    build_external_evidence_signals,
    build_market_external_evidence_register,
)
from .models import Company, MarketProofExternalEvidence, PracticeTask, UserCompanyAccess


class MarketExternalEvidenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="external-proof@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        self.company = Company.objects.create(
            name="External Proof Co",
            gstin="27EPCAB1234C1Z5",
            tan="EPCA12345B",
            short_code="EPC",
            financial_year_start=date(2026, 4, 1),
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timezone.timedelta(days=45),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_external_evidence_page_search_csv_and_follow_up_task(self):
        response = self.client.get(reverse("core:market_external_evidence"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "External Evidence Register")
        self.assertContains(response, "Capture External Evidence")

        search_payload = self.client.get(reverse("core:api_search"), {"q": "external evidence"}).json()
        self.assertIn("External Evidence Register", {item["name"] for item in search_payload["navigation"]})

        post_response = self.client.post(
            reverse("core:market_external_evidence"),
            {
                "action": "create_evidence",
                "company": self.company.pk,
                "category": MarketProofExternalEvidence.CATEGORY_PROVIDER,
                "status": MarketProofExternalEvidence.STATUS_RECEIVED,
                "source": MarketProofExternalEvidence.SOURCE_PROVIDER,
                "title": "GST production credential approval",
                "evidence_reference": "PROVIDER-TICKET-001",
                "artifact_sha256": "c" * 64,
                "evidence_url": "",
                "notes": "Provider confirmed production credentials for GST API.",
                "due_date": timezone.localdate().isoformat(),
                "expires_on": "",
                "owner": self.user.pk,
                "create_follow_up_task": "on",
            },
        )
        self.assertRedirects(
            post_response,
            f"{reverse('core:market_external_evidence')}?company={self.company.pk}&category=provider_production&status=received",
        )
        evidence = MarketProofExternalEvidence.objects.get(title="GST production credential approval")
        self.assertFalse(evidence.is_verified)
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference=f"{EXTERNAL_EVIDENCE_TASK_PREFIX}{evidence.pk}",
            ).exists()
        )

        verify_response = self.client.post(
            reverse("core:market_external_evidence"),
            {
                "action": "verify_evidence",
                "evidence_id": evidence.pk,
                "status": "all",
            },
        )
        self.assertRedirects(verify_response, f"{reverse('core:market_external_evidence')}?status=all")
        evidence.refresh_from_db()
        self.assertTrue(evidence.is_verified)
        self.assertEqual(evidence.verified_by, self.user)
        self.assertEqual(evidence.follow_up_task.status, PracticeTask.STATUS_DONE)

        filtered = self.client.get(reverse("core:market_external_evidence"), {"q": "credential", "status": "all"})
        self.assertContains(filtered, "GST production credential approval")

        csv_response = self.client.get(reverse("core:market_external_evidence"), {"export": "csv", "status": "all"})
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,GSTIN,Category,Status", csv_text)
        self.assertIn("GST production credential approval", csv_text)

        context = build_market_external_evidence_register(self.user, {"status": "all"})
        self.assertEqual(context["totals"]["verified"], 1)
        self.assertEqual(context["totals"]["required_missing"], 5)

    def test_external_evidence_signals_complete_after_required_categories_verified(self):
        for category in [
            MarketProofExternalEvidence.CATEGORY_PROVIDER,
            MarketProofExternalEvidence.CATEGORY_PILOT,
            MarketProofExternalEvidence.CATEGORY_CASE_STUDY,
            MarketProofExternalEvidence.CATEGORY_STATUTORY,
            MarketProofExternalEvidence.CATEGORY_BACKUP,
            MarketProofExternalEvidence.CATEGORY_SECURITY,
        ]:
            MarketProofExternalEvidence.objects.create(
                company=self.company,
                category=category,
                status=MarketProofExternalEvidence.STATUS_VERIFIED,
                source=MarketProofExternalEvidence.SOURCE_CA,
                title=f"Verified {category}",
                evidence_reference=f"EXT-{category}",
                verified_by=self.user,
                verified_at=timezone.now(),
                created_by=self.user,
            )

        signals = build_external_evidence_signals(self.company)
        self.assertTrue(signals["complete"])
        self.assertEqual(signals["verified_count"], 6)
        self.assertEqual(signals["missing_required_count"], 0)
        self.assertEqual(signals["completion_score"], 100)
