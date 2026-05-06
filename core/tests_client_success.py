from datetime import date

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from ledger.models import AccountGroup, Ledger
from portal.models import PortalUser
from vouchers.models import Voucher

from .models import (
    AuditLog,
    ClientEngagement,
    Company,
    CompanyStatutoryProfile,
    ComplianceFiling,
    PracticeTask,
    UserCompanyAccess,
)
from .client_success import CLIENT_SUCCESS_TASK_PREFIX, build_client_success_cockpit


class ClientSuccessCockpitTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="success@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        self.healthy_company = self._company("Healthy Success Co", "HSC")
        self.risk_company = self._company("Risk Success Co", "RSC")
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
            whatsapp_intake_number=f"+9187654{suffix}",
            invoice_email_from_address=f"{code.lower()}@example.com",
        )

    def _make_healthy(self, company):
        today = timezone.localdate()
        CompanyStatutoryProfile.objects.create(
            company=company,
            gst_registered=False,
            tds_applicable=False,
        )
        group = AccountGroup.objects.create(company=company, name="Success Assets", nature="Asset")
        ledgers = [
            Ledger.objects.create(company=company, name=f"Ledger {index}", account_group=group)
            for index in range(6)
        ]
        portal_user = PortalUser.objects.create(
            name="Success Client",
            email=f"success-client-{company.pk}@example.com",
            linked_ledger=ledgers[0],
            is_active=True,
        )
        portal_user.set_password("client-password")
        portal_user.save()
        Voucher.objects.create(company=company, date=today, voucher_type="Sales", status="APPROVED")
        ComplianceFiling.objects.create(
            company=company,
            title="Success filing",
            filing_type="GST",
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
            partner_owner=self.user,
            scope_summary="Books, GST, TDS, client portal, and monthly close.",
            risk_rating=ClientEngagement.RISK_MEDIUM,
            last_reviewed_at=today,
        )
        AuditLog.objects.create(
            company=company,
            user=self.user,
            action=AuditLog.ACTION_CREATE,
            model_name="ClientSuccess",
            record_id=0,
            object_repr="Success setup",
            old_data={},
            new_data={"source": "test"},
        )

    def test_success_cockpit_scores_healthy_and_critical_clients(self):
        self._make_healthy(self.healthy_company)

        context = build_client_success_cockpit(self.user)
        rows = {row["company"].pk: row for row in context["rows"]}

        self.assertIn(rows[self.healthy_company.pk]["band"], {"Champion", "Healthy"})
        self.assertGreaterEqual(rows[self.healthy_company.pk]["score"], 78)
        self.assertEqual(rows[self.healthy_company.pk]["critical_count"], 0)
        self.assertEqual(rows[self.risk_company.pk]["band"], "Critical")
        self.assertGreater(rows[self.risk_company.pk]["critical_count"], 0)

    def test_success_page_csv_search_and_task_sync(self):
        self._make_healthy(self.healthy_company)

        response = self.client.get(reverse("core:client_success_cockpit"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Client Success Cockpit")
        self.assertContains(response, "Client Success Queue")

        search_payload = self.client.get(reverse("core:api_search"), {"q": "client success"}).json()
        self.assertIn("Client Success Cockpit", {item["name"] for item in search_payload["navigation"]})

        csv_response = self.client.get(reverse("core:client_success_cockpit"), {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,GSTIN,Success Score", csv_text)
        self.assertIn("Risk Success Co", csv_text)

        create_response = self.client.post(reverse("core:client_success_cockpit"), {"band": "critical"})
        self.assertRedirects(create_response, f"{reverse('core:client_success_cockpit')}?band=critical")
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.risk_company,
                reference__startswith=f"{CLIENT_SUCCESS_TASK_PREFIX}{self.risk_company.pk}:",
            ).exists()
        )
