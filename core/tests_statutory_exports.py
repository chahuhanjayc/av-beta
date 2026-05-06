from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Company, PracticeTask, UserCompanyAccess
from core.statutory_exports import (
    build_statutory_export_center,
    parse_gst_export_period,
    parse_tds_export_filters,
)


class StatutoryExportCenterTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Statutory Export Co",
            gstin="27ABCDE1234F1Z5",
            short_code="SE",
        )
        self.user = get_user_model().objects.create_superuser(
            email="stat-export@example.com",
            password="stat-pass",
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_statutory_export_center_builds_gst_and_tds_rows(self):
        period_start, period_end = parse_gst_export_period("2026-04")
        tds_filters = parse_tds_export_filters({
            "fy": "2025",
            "quarter": "Q4",
            "form_type": "26Q",
        })

        center = build_statutory_export_center(
            Company.objects.filter(pk=self.company.pk),
            period_start,
            period_end,
            tds_filters,
        )

        self.assertEqual(center["totals"]["clients"], 1)
        row = center["rows"][0]
        self.assertEqual(row["company"], self.company)
        self.assertIn("GST", row["gst"]["label"])
        self.assertIn("TDS", row["tds"]["label"])
        self.assertGreater(row["gst"]["critical_count"], 0)
        self.assertGreater(row["tds"]["critical_count"], 0)

    def test_statutory_export_center_renders_and_exports_csv(self):
        response = self.client.get(
            reverse("core:statutory_export_center"),
            {"period": "2026-04", "fy": "2025", "quarter": "Q4", "form_type": "26Q"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Statutory Export Center")
        self.assertContains(response, self.company.name)
        self.assertContains(response, "GST Filing Pack")
        self.assertContains(response, "TDS RPU Pack")

        csv_response = self.client.get(
            reverse("core:statutory_export_center"),
            {
                "period": "2026-04",
                "fy": "2025",
                "quarter": "Q4",
                "form_type": "26Q",
                "export": "csv",
            },
        )

        self.assertEqual(csv_response.status_code, 200)
        self.assertEqual(csv_response["Content-Type"], "text/csv; charset=utf-8")
        csv_text = csv_response.content.decode("utf-8")
        self.assertIn("GST Status", csv_text)
        self.assertIn("TDS Status", csv_text)
        self.assertIn(self.company.name, csv_text)

    def test_statutory_export_center_creates_idempotent_blocker_tasks(self):
        post_data = {
            "period": "2026-04",
            "fy": "2025",
            "quarter": "Q4",
            "form_type": "26Q",
            "action": "create_tasks",
        }

        response = self.client.post(reverse("core:statutory_export_center"), post_data)
        self.assertEqual(response.status_code, 302)

        created_count = PracticeTask.objects.filter(
            company=self.company,
            reference__startswith="STATEXPORT:",
        ).count()
        self.assertGreater(created_count, 0)

        self.client.post(reverse("core:statutory_export_center"), post_data)
        self.assertEqual(
            PracticeTask.objects.filter(company=self.company, reference__startswith="STATEXPORT:").count(),
            created_count,
        )
