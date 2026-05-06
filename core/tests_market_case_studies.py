from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription

from .market_case_studies import CASE_STUDY_TASK_PREFIX, build_case_study_signals, build_market_case_study_register
from .models import Company, MarketProofCaseStudy, PracticeTask, UserCompanyAccess


class MarketCaseStudyTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="market-case@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        self.company = Company.objects.create(
            name="Case Proof Co",
            gstin="27CSPAB1234C1Z5",
            tan="CSPA12345B",
            short_code="CSP",
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

    def test_case_study_page_search_csv_and_follow_up_task(self):
        response = self.client.get(reverse("core:market_case_studies"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Market Case Studies")
        self.assertContains(response, "Capture Case Study")

        search_payload = self.client.get(reverse("core:api_search"), {"q": "case studies"}).json()
        self.assertIn("Market Case Studies", {item["name"] for item in search_payload["navigation"]})

        post_response = self.client.post(
            reverse("core:market_case_studies"),
            {
                "action": "create_case_study",
                "company": self.company.pk,
                "title": "Tally pilot objection story",
                "status": MarketProofCaseStudy.STATUS_DRAFT,
                "outcome": MarketProofCaseStudy.OUTCOME_EVALUATING,
                "migration_source": MarketProofCaseStudy.SOURCE_TALLY,
                "client_contact": "Owner",
                "client_role": "Owner",
                "testimonial_quote": "",
                "publish_consent": "",
                "anonymized": "on",
                "consent_reference": "",
                "evidence_reference": "CASE-DRAFT-001",
                "before_process_hours": "6.00",
                "after_process_hours": "3.00",
                "monthly_documents": "80",
                "monthly_invoices": "20",
                "gst_periods_completed": "1",
                "tally_parallel_run_days": "0",
                "commercial_value": "0.00",
                "value_summary": "Pilot proof still needs consent and quote.",
                "owner": self.user.pk,
                "create_follow_up_task": "on",
            },
        )
        self.assertRedirects(
            post_response,
            f"{reverse('core:market_case_studies')}?company={self.company.pk}&status=draft&outcome=evaluating",
        )
        case_study = MarketProofCaseStudy.objects.get(title="Tally pilot objection story")
        self.assertFalse(case_study.is_publishable)
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.company,
                reference=f"{CASE_STUDY_TASK_PREFIX}{case_study.pk}",
            ).exists()
        )

        filtered = self.client.get(reverse("core:market_case_studies"), {"q": "objection"})
        self.assertContains(filtered, "Tally pilot objection story")

        csv_response = self.client.get(reverse("core:market_case_studies"), {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,GSTIN,Title,Status", csv_text)
        self.assertIn("Tally pilot objection story", csv_text)

        context = build_market_case_study_register(self.user)
        self.assertEqual(context["totals"]["missing_total"], 5)

    def test_publishable_case_study_can_be_approved_and_published(self):
        case_study = MarketProofCaseStudy.objects.create(
            company=self.company,
            title="Tally replacement published proof",
            status=MarketProofCaseStudy.STATUS_READY,
            outcome=MarketProofCaseStudy.OUTCOME_PAID,
            migration_source=MarketProofCaseStudy.SOURCE_TALLY,
            client_contact="Finance Lead",
            client_role="Finance Lead",
            testimonial_quote="Akshaya reduced GST close time and replaced our old Tally handoff.",
            publish_consent=True,
            anonymized=True,
            consent_reference="CONSENT-CASE-001",
            evidence_reference="CASE-EVIDENCE-001",
            before_process_hours=Decimal("9.00"),
            after_process_hours=Decimal("4.00"),
            monthly_documents=140,
            monthly_invoices=60,
            gst_periods_completed=2,
            tally_parallel_run_days=14,
            commercial_value=Decimal("42000.00"),
            owner=self.user,
            created_by=self.user,
        )

        approve_response = self.client.post(
            reverse("core:market_case_studies"),
            {
                "action": "approve_case_study",
                "case_study_id": case_study.pk,
                "status": "all",
            },
        )
        self.assertRedirects(approve_response, f"{reverse('core:market_case_studies')}?status=all")
        case_study.refresh_from_db()
        self.assertEqual(case_study.status, MarketProofCaseStudy.STATUS_APPROVED)
        self.assertTrue(case_study.is_publishable)
        self.assertEqual(case_study.hours_saved, Decimal("5.00"))

        publish_response = self.client.post(
            reverse("core:market_case_studies"),
            {
                "action": "publish_case_study",
                "case_study_id": case_study.pk,
                "status": "all",
            },
        )
        self.assertRedirects(publish_response, f"{reverse('core:market_case_studies')}?status=all")
        case_study.refresh_from_db()
        self.assertEqual(case_study.status, MarketProofCaseStudy.STATUS_PUBLISHED)
        self.assertIsNotNone(case_study.published_at)

        signals = build_case_study_signals(self.company)
        self.assertEqual(signals["publishable_count"], 1)
        self.assertEqual(signals["converted_count"], 1)
        self.assertEqual(signals["with_metrics_count"], 1)
