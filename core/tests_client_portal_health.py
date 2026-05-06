from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from ledger.models import AccountGroup, Ledger
from portal.models import BalanceConfirmation, ClientDocumentRequest, PortalUser

from .client_portal_health import CLIENT_PORTAL_HEALTH_TASK_PREFIX, build_client_portal_health
from .models import Company, PracticeTask, UserCompanyAccess


class ClientPortalHealthTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            email="portal-health@example.com",
            password="CorrectHorseBatteryStaple123!",
            is_staff=True,
        )
        self.ready_company = self._company("Ready Portal Co", "RPC", channels=True)
        self.risk_company = self._company("Disconnected Portal Co", "DPC", channels=False)
        UserCompanyAccess.objects.create(user=self.user, company=self.ready_company, role="Admin")
        UserCompanyAccess.objects.create(user=self.user, company=self.risk_company, role="Admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.ready_company.pk
        session.save()

    def _company(self, name, code, *, channels):
        suffix = sum(ord(ch) for ch in code)
        kwargs = {
            "name": name,
            "gstin": f"27{code}AB1234C1Z5",
            "tan": f"{code}A12345B",
            "short_code": code,
            "financial_year_start": date(2026, 4, 1),
        }
        if channels:
            kwargs.update({
                "whatsapp_intake_number": f"+9188776{suffix}",
                "invoice_email_from_address": f"{code.lower()}@example.com",
            })
        return Company.objects.create(**kwargs)

    def _make_ready_company(self, company):
        today = timezone.localdate()
        group = AccountGroup.objects.create(company=company, name="Portal Debtors", nature="Asset")
        ledger = Ledger.objects.create(
            company=company,
            name="Ready Customer",
            account_group=group,
            email=f"ready-customer-{company.pk}@example.com",
            whatsapp_number="+919876543210",
        )
        portal_user = PortalUser.objects.create(
            name="Ready Customer",
            email=f"ready-portal-{company.pk}@example.com",
            linked_ledger=ledger,
            is_active=True,
        )
        portal_user.set_password("client-password")
        portal_user.save()
        ClientDocumentRequest.objects.create(
            company=company,
            portal_user=portal_user,
            recipient_email=portal_user.email,
            recipient_whatsapp_number=ledger.whatsapp_number,
            title="April invoice proof",
            document_type=ClientDocumentRequest.TYPE_GST_INVOICE,
            status=ClientDocumentRequest.STATUS_CLOSED,
            due_date=today + timezone.timedelta(days=3),
            uploaded_at=timezone.now(),
            closed_at=timezone.now(),
            requested_by=self.user,
        )
        ClientSubscription.objects.create(
            company=company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timezone.timedelta(days=45),
        )
        BalanceConfirmation.objects.create(
            portal_user=portal_user,
            ledger=ledger,
            confirmed_balance=Decimal("0.00"),
            response_status=BalanceConfirmation.STATUS_CONFIRMED,
        )

    def _make_disconnected_company(self, company):
        today = timezone.localdate()
        group = AccountGroup.objects.create(company=company, name="Disconnected Debtors", nature="Asset")
        Ledger.objects.create(
            company=company,
            name="Uninvited Customer",
            account_group=group,
            email=f"uninvited-{company.pk}@example.com",
        )
        ClientDocumentRequest.objects.create(
            company=company,
            title="Missing bank statement",
            document_type=ClientDocumentRequest.TYPE_BANK,
            status=ClientDocumentRequest.STATUS_OPEN,
            due_date=today - timezone.timedelta(days=2),
            requested_by=self.user,
        )

    def test_portal_health_scores_ready_and_disconnected_clients(self):
        self._make_ready_company(self.ready_company)
        self._make_disconnected_company(self.risk_company)

        context = build_client_portal_health(self.user)
        rows = {row["company"].pk: row for row in context["rows"]}

        self.assertEqual(rows[self.ready_company.pk]["band"], "Ready")
        self.assertEqual(rows[self.ready_company.pk]["critical_count"], 0)
        self.assertGreaterEqual(rows[self.ready_company.pk]["score"], 90)
        self.assertEqual(rows[self.risk_company.pk]["band"], "Disconnected")
        self.assertGreater(rows[self.risk_company.pk]["critical_count"], 0)
        self.assertGreater(rows[self.risk_company.pk]["missing_delivery_count"], 0)

    def test_portal_health_page_csv_search_and_task_sync(self):
        self._make_ready_company(self.ready_company)
        self._make_disconnected_company(self.risk_company)

        response = self.client.get(reverse("core:client_portal_health"))
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Client Portal Health")
        self.assertContains(response, "Portal Reachability Queue")

        search_payload = self.client.get(reverse("core:api_search"), {"q": "portal health"}).json()
        self.assertIn("Client Portal Health", {item["name"] for item in search_payload["navigation"]})

        csv_response = self.client.get(reverse("core:client_portal_health"), {"export": "csv"})
        self.assertEqual(csv_response.status_code, 200)
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("Company,GSTIN,Portal Score", csv_text)
        self.assertIn("Disconnected Portal Co", csv_text)

        create_response = self.client.post(reverse("core:client_portal_health"), {"band": "disconnected"})
        self.assertRedirects(create_response, f"{reverse('core:client_portal_health')}?band=disconnected")
        self.assertTrue(
            PracticeTask.objects.filter(
                company=self.risk_company,
                reference__startswith=f"{CLIENT_PORTAL_HEALTH_TASK_PREFIX}{self.risk_company.pk}:",
            ).exists()
        )
