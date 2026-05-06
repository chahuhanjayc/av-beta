from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from core.models import (
    Company,
    CompanyStatutoryProfile,
    PracticeTask,
    UserCompanyAccess,
)


class OperatingReadinessTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Readiness Test Co",
            short_code="",
        )
        self.read_only_company = Company.objects.create(
            name="Read Only Readiness Co",
            gstin="27READO0000R1Z5",
            tan="MUMR00000R",
            short_code="ROR",
            financial_year_start=timezone.localdate().replace(month=4, day=1),
        )
        self.user = get_user_model().objects.create_user(
            email="operating-readiness@example.com",
            password="secret",
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        UserCompanyAccess.objects.create(user=self.user, company=self.read_only_company, role="Viewer")
        for company in [self.company, self.read_only_company]:
            ClientSubscription.objects.create(
                company=company,
                primary_user=self.user,
                status=ClientSubscription.STATUS_ACTIVE,
                subscription_end=timezone.now() + timedelta(days=30),
            )
        CompanyStatutoryProfile.objects.create(
            company=self.company,
            tds_26q_enabled=False,
        )
        CompanyStatutoryProfile.objects.create(company=self.read_only_company)
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_operating_readiness_surfaces_client_go_live_gaps(self):
        response = self.client.get(reverse("core:client_operating_readiness"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Operating Readiness")
        self.assertContains(response, "GSTIN available")
        self.assertContains(response, "TAN available")
        self.assertContains(response, "Upcoming filing workflows prepared")
        self.assertContains(response, "Read-only")

    def test_operating_readiness_creates_idempotent_gap_tasks_for_manageable_clients(self):
        response = self.client.post(reverse("core:client_operating_readiness"))

        self.assertRedirects(response, reverse("core:client_operating_readiness"))
        tasks = PracticeTask.objects.filter(company=self.company, reference__startswith="OPREADY:")
        self.assertGreater(tasks.count(), 0)
        self.assertTrue(tasks.filter(reference=f"OPREADY:{self.company.pk}:gstin").exists())
        self.assertFalse(PracticeTask.objects.filter(company=self.read_only_company, reference__startswith="OPREADY:").exists())
        first_count = tasks.count()

        self.client.post(reverse("core:client_operating_readiness"))

        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference__startswith="OPREADY:").count(),
            first_count,
        )

    def test_operating_readiness_csv_export(self):
        response = self.client.get(reverse("core:client_operating_readiness"), {"export": "csv"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode()
        self.assertIn("Company,GSTIN,Score,Band", body)
        self.assertIn("Readiness Test Co", body)
