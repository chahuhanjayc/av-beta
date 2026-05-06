from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription

from .models import Company, PilotFeedback, PracticeTask, UserCompanyAccess
from .pilot_feedback import (
    PILOT_FEEDBACK_TASK_PREFIX,
    build_pilot_feedback_register,
    create_pilot_feedback_follow_up,
)


class PilotFeedbackRegisterTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="pilot-feedback@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        self.company = Company.objects.create(
            name="Feedback Pilot Co",
            gstin="27FPBAC1234C1Z5",
            tan="FPBA12345B",
            short_code="FPB",
            financial_year_start=date(2026, 4, 1),
        )
        self.other_company = Company.objects.create(
            name="Other Feedback Co",
            gstin="27OFBAC1234C1Z5",
            tan="OFBA12345B",
            short_code="OFB",
            financial_year_start=date(2026, 4, 1),
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        UserCompanyAccess.objects.create(user=self.user, company=self.other_company, role="Admin")
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timezone.timedelta(days=30),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_feedback_register_page_search_csv_and_create_task(self):
        response = self.client.get(reverse("core:pilot_feedback_register"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pilot Feedback Register")
        self.assertContains(response, "Capture Pilot Signal")

        search_payload = self.client.get(reverse("core:api_search"), {"q": "pilot feedback"}).json()
        self.assertIn("Pilot Feedback Register", {item["name"] for item in search_payload["navigation"]})

        post_response = self.client.post(
            reverse("core:pilot_feedback_register"),
            {
                "action": "create_feedback",
                "company": self.company.pk,
                "feedback_type": PilotFeedback.TYPE_OBJECTION,
                "sentiment": PilotFeedback.SENTIMENT_NEGATIVE,
                "confidence_score": "4",
                "severity": PilotFeedback.SEVERITY_HIGH,
                "status": PilotFeedback.STATUS_OPEN,
                "occurred_on": timezone.localdate().isoformat(),
                "assigned_to": self.user.pk,
                "client_contact": "Owner",
                "competitor_reference": PilotFeedback.COMPETITOR_TALLY,
                "evidence_reference": "CALL-001",
                "summary": "Client still wants Tally shortcut flow",
                "detail": "Pilot call highlighted a shortcut workflow gap during migration review.",
                "create_follow_up_task": "on",
            },
        )
        self.assertRedirects(
            post_response,
            f"{reverse('core:pilot_feedback_register')}?company={self.company.pk}&status=open&severity=high&sentiment=negative",
        )

        feedback = PilotFeedback.objects.get(summary="Client still wants Tally shortcut flow")
        self.assertEqual(feedback.recorded_by, self.user)
        self.assertEqual(feedback.competitor_reference, PilotFeedback.COMPETITOR_TALLY)
        self.assertIsNotNone(feedback.follow_up_task)
        self.assertEqual(feedback.follow_up_task.reference, f"{PILOT_FEEDBACK_TASK_PREFIX}{feedback.pk}")
        self.assertEqual(feedback.follow_up_task.priority, PracticeTask.PRIORITY_HIGH)

        filtered = self.client.get(reverse("core:pilot_feedback_register"), {"q": "shortcut"})
        self.assertContains(filtered, "Client still wants Tally shortcut flow")

        csv_response = self.client.get(reverse("core:pilot_feedback_register"), {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,GSTIN,Date,Type,Sentiment,Confidence", csv_text)
        self.assertIn("Feedback Pilot Co", csv_text)

    def test_resolve_feedback_closes_follow_up_and_updates_register_totals(self):
        feedback = PilotFeedback.objects.create(
            company=self.company,
            feedback_type=PilotFeedback.TYPE_BUG,
            sentiment=PilotFeedback.SENTIMENT_NEGATIVE,
            confidence_score=3,
            severity=PilotFeedback.SEVERITY_CRITICAL,
            status=PilotFeedback.STATUS_OPEN,
            occurred_on=timezone.localdate(),
            assigned_to=self.user,
            recorded_by=self.user,
            summary="Client cannot approve imported ledgers",
            detail="Approval blocker captured during pilot review.",
        )
        task, created = create_pilot_feedback_follow_up(feedback, self.user)
        self.assertTrue(created)

        context = build_pilot_feedback_register(self.user)
        self.assertEqual(context["totals"]["blockers"], 1)

        response = self.client.post(
            reverse("core:pilot_feedback_register"),
            {
                "action": "resolve_feedback",
                "feedback_id": feedback.pk,
                "status": "all",
            },
        )
        self.assertRedirects(response, f"{reverse('core:pilot_feedback_register')}?status=all")

        feedback.refresh_from_db()
        task.refresh_from_db()
        self.assertEqual(feedback.status, PilotFeedback.STATUS_RESOLVED)
        self.assertIsNotNone(feedback.resolved_at)
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)
        self.assertEqual(task.completed_by, self.user)

        context = build_pilot_feedback_register(self.user, {"status": "all"})
        self.assertEqual(context["totals"]["blockers"], 0)
        self.assertEqual(context["totals"]["resolved_30"], 1)
