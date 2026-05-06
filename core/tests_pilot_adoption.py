from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from ledger.models import AccountGroup, Ledger
from portal.models import ClientDocumentRequest, PortalUser
from vouchers.models import Voucher

from .models import (
    AuditLog,
    ClientEngagement,
    Company,
    CompanyStatutoryProfile,
    ComplianceFiling,
    PilotFeedback,
    PracticeTask,
    UserCompanyAccess,
)
from .pilot_adoption import PILOT_ADOPTION_TASK_PREFIX, build_pilot_adoption_evidence


class PilotAdoptionEvidenceTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="pilot-adoption@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        self.healthy_company = self._company("Healthy Pilot Co", "HPC")
        self.risk_company = self._company("Risk Pilot Co", "RPC")
        UserCompanyAccess.objects.create(user=self.user, company=self.healthy_company, role="Admin")
        UserCompanyAccess.objects.create(user=self.user, company=self.risk_company, role="Admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.healthy_company.pk
        session.save()

    def _company(self, name, code):
        suffix = sum(ord(ch) for ch in code)
        return Company.objects.create(
            name=name,
            gstin=f"27{code}AB1234C1Z5",
            tan=f"{code}A12345B",
            short_code=code,
            financial_year_start=date(2026, 4, 1),
            whatsapp_intake_number=f"+9188221{suffix}",
            invoice_email_from_address=f"{code.lower()}@example.com",
        )

    def _make_healthy(self, company):
        today = timezone.localdate()
        CompanyStatutoryProfile.objects.create(
            company=company,
            gst_registered=False,
            tds_applicable=False,
        )
        group = AccountGroup.objects.create(company=company, name="Pilot Assets", nature="Asset")
        ledgers = [
            Ledger.objects.create(company=company, name=f"Pilot Ledger {index}", account_group=group)
            for index in range(6)
        ]
        portal_user = PortalUser.objects.create(
            name="Pilot Client",
            email=f"pilot-client-{company.pk}@example.com",
            linked_ledger=ledgers[0],
            is_active=True,
        )
        portal_user.set_password("client-password")
        portal_user.save()
        ClientDocumentRequest.objects.create(
            company=company,
            portal_user=portal_user,
            recipient_email=portal_user.email,
            title="Pilot feedback document",
            document_type=ClientDocumentRequest.TYPE_OTHER,
            status=ClientDocumentRequest.STATUS_CLOSED,
            due_date=today + timezone.timedelta(days=5),
            uploaded_at=timezone.now(),
            closed_at=timezone.now(),
            requested_by=self.user,
        )
        Voucher.objects.create(company=company, date=today, voucher_type="Sales", status="APPROVED")
        ComplianceFiling.objects.create(
            company=company,
            title="Pilot GST workflow",
            filing_type=ComplianceFiling.TYPE_GSTR1,
            period_start=today.replace(day=1),
            period_end=today,
            due_date=today + timezone.timedelta(days=15),
        )
        ClientSubscription.objects.create(
            company=company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timezone.timedelta(days=45),
        )
        ClientEngagement.objects.create(
            company=company,
            status=ClientEngagement.STATUS_ACTIVE,
            service_package=ClientEngagement.PACKAGE_FULL_ACCOUNTING,
            monthly_retainer=Decimal("25000.00"),
            partner_owner=self.user,
            scope_summary="Pilot covers books, GST, client portal, document chase, and month close.",
            risk_rating=ClientEngagement.RISK_LOW,
            last_reviewed_at=today,
        )
        AuditLog.objects.create(
            company=company,
            user=self.user,
            action=AuditLog.ACTION_CREATE,
            model_name="PilotAdoption",
            record_id=0,
            object_repr="Pilot adoption usage",
            old_data={},
            new_data={"source": "test"},
        )
        PracticeTask.objects.create(
            company=company,
            title="Pilot issue resolved",
            task_type=PracticeTask.TYPE_OTHER,
            priority=PracticeTask.PRIORITY_NORMAL,
            status=PracticeTask.STATUS_DONE,
            due_date=today,
            assigned_to=self.user,
            created_by=self.user,
            completed_by=self.user,
            completed_at=timezone.now(),
            reference=f"{PILOT_ADOPTION_TASK_PREFIX}{company.pk}:resolved_test",
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
            evidence_reference="CALL-PILOT-READY",
            summary="Client confirmed they can replace Tally for pilot workflows",
            detail="Client accepted invoice, GST, portal, and document chase flow during pilot review.",
        )

    def test_pilot_adoption_scores_healthy_and_blocked_clients(self):
        self._make_healthy(self.healthy_company)

        context = build_pilot_adoption_evidence(self.user)
        rows = {row["company"].pk: row for row in context["rows"]}

        self.assertIn(rows[self.healthy_company.pk]["band"], {"Scale Ready", "Pilot Healthy"})
        self.assertGreaterEqual(rows[self.healthy_company.pk]["score"], 78)
        self.assertEqual(rows[self.healthy_company.pk]["critical_count"], 0)
        self.assertEqual(rows[self.healthy_company.pk]["feedback_signals"]["recent_feedback_count"], 1)
        self.assertEqual(rows[self.healthy_company.pk]["feedback_signals"]["open_blocker_count"], 0)
        self.assertEqual(rows[self.risk_company.pk]["band"], "Blocked")
        self.assertGreater(rows[self.risk_company.pk]["critical_count"], 0)

    def test_pilot_adoption_page_csv_search_and_task_sync(self):
        self._make_healthy(self.healthy_company)

        response = self.client.get(reverse("core:pilot_adoption_evidence"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pilot Adoption Evidence")
        self.assertContains(response, "Pilot Evidence Queue")

        search_payload = self.client.get(reverse("core:api_search"), {"q": "pilot evidence"}).json()
        self.assertIn("Pilot Adoption Evidence", {item["name"] for item in search_payload["navigation"]})

        csv_response = self.client.get(reverse("core:pilot_adoption_evidence"), {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,GSTIN,Adoption Score", csv_text)
        self.assertIn("Risk Pilot Co", csv_text)

        create_response = self.client.post(reverse("core:pilot_adoption_evidence"), {"band": "blocked"})
        self.assertRedirects(create_response, f"{reverse('core:pilot_adoption_evidence')}?band=blocked")
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.risk_company,
                reference__startswith=f"{PILOT_ADOPTION_TASK_PREFIX}{self.risk_company.pk}:",
            ).exists()
        )
