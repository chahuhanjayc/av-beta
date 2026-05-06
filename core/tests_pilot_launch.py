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
from .pilot_launch import PILOT_LAUNCH_TASK_PREFIX, build_pilot_launch_control


class PilotLaunchControlTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="pilot@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        self.ready_company = self._company("Ready Pilot Co", "RPC")
        self.blocked_company = self._company("Blocked Pilot Co", "BPC")
        UserCompanyAccess.objects.create(user=self.user, company=self.ready_company, role="Admin")
        UserCompanyAccess.objects.create(user=self.user, company=self.blocked_company, role="Admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.ready_company.pk
        session.save()

    def _company(self, name, code):
        suffix = sum(ord(ch) for ch in code)
        return Company.objects.create(
            name=name,
            gstin=f"27{code}AB1234C1Z5",
            tan=f"{code}A12345B",
            short_code=code,
            financial_year_start=date(2026, 4, 1),
            whatsapp_intake_number=f"+9198765{suffix}",
            invoice_email_from_address=f"{code.lower()}@example.com",
        )

    def _make_launch_ready(self, company):
        CompanyStatutoryProfile.objects.create(
            company=company,
            gst_registered=False,
            tds_applicable=False,
        )
        group = AccountGroup.objects.create(company=company, name="Launch Assets", nature="Asset")
        for index in range(6):
            Ledger.objects.create(company=company, name=f"Ledger {index}", account_group=group)
        party = Ledger.objects.first()
        portal_user = PortalUser.objects.create(
            name="Pilot Client",
            email=f"portal-{company.pk}@example.com",
            linked_ledger=party,
            is_active=True,
        )
        portal_user.set_password("client-password")
        portal_user.save()
        Voucher.objects.create(company=company, date=date(2026, 4, 3), voucher_type="Sales", status="APPROVED")
        ComplianceFiling.objects.create(
            company=company,
            title="Launch filing",
            filing_type="GST",
            period_start=date(2026, 4, 1),
            period_end=date(2026, 4, 30),
            due_date=timezone.localdate() + timezone.timedelta(days=15),
        )
        ClientSubscription.objects.create(
            company=company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timezone.timedelta(days=30),
        )
        ClientEngagement.objects.create(
            company=company,
            status=ClientEngagement.STATUS_ONBOARDING,
            service_package=ClientEngagement.PACKAGE_FULL_ACCOUNTING,
            partner_owner=self.user,
            scope_summary="Pilot covers books, GST, TDS, document chase, and month-end close.",
            last_reviewed_at=timezone.localdate(),
        )
        AuditLog.objects.create(
            company=company,
            user=self.user,
            action=AuditLog.ACTION_CREATE,
            model_name="Pilot",
            record_id=0,
            object_repr="Pilot setup",
            old_data={},
            new_data={"source": "test"},
        )

    def test_pilot_launch_scores_ready_and_blocked_clients(self):
        self._make_launch_ready(self.ready_company)

        context = build_pilot_launch_control(self.user)
        rows = {row["company"].pk: row for row in context["rows"]}

        self.assertEqual(rows[self.ready_company.pk]["band"], "Pilot Ready")
        self.assertGreaterEqual(rows[self.ready_company.pk]["score"], 90)
        self.assertEqual(rows[self.ready_company.pk]["critical_count"], 0)
        self.assertEqual(rows[self.blocked_company.pk]["band"], "Blocked")
        self.assertGreater(rows[self.blocked_company.pk]["critical_count"], 0)
        self.assertEqual(context["totals"]["clients"], 2)

    def test_pilot_launch_page_csv_and_task_sync(self):
        self._make_launch_ready(self.ready_company)
        response = self.client.get(reverse("core:client_pilot_launch"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Pilot Launch Control")
        self.assertContains(response, "Client Launch Queue")

        search_payload = self.client.get(reverse("core:api_search"), {"q": "pilot launch"}).json()
        self.assertIn("Pilot Launch Control", {item["name"] for item in search_payload["navigation"]})

        csv_response = self.client.get(reverse("core:client_pilot_launch"), {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,GSTIN,Launch Score", csv_text)
        self.assertIn("Blocked Pilot Co", csv_text)

        create_response = self.client.post(reverse("core:client_pilot_launch"), {"band": "blocked"})

        self.assertRedirects(create_response, f"{reverse('core:client_pilot_launch')}?band=blocked")
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.blocked_company,
                reference__startswith=f"{PILOT_LAUNCH_TASK_PREFIX}{self.blocked_company.pk}:",
            ).exists()
        )
