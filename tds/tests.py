from datetime import date
from decimal import Decimal

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.models import Company, UserCompanyAccess
from ledger.models import AccountGroup, Ledger

from .filing_pack import build_tds_filing_pack, mark_tds_filing_pack_filed, save_tds_filing_pack
from .workbench import build_tds_deposit_watch, tds_deposit_due_date
from .models import (
    TDSCertificateIssue,
    TDSEntry,
    TDSFilingPack,
    TDSPostFilingTracker,
    TDSReturnWorkpaper,
    TDSSection,
)


class TDSReturnWorkbenchTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="TDS Workbench Co",
            gstin="27ABCDE1234F1Z5",
            tan="MUMA12345A",
            tds_responsible_person="Jay Chauhan",
            tds_responsible_designation="Partner",
            short_code="TWC",
        )
        self.user = get_user_model().objects.create_superuser(
            email="tds-workbench@example.com",
            password="tds-pass",
        )
        UserCompanyAccess.objects.create(user=self.user, company=self.company, role="Admin")
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

        self.vendor_group = AccountGroup.objects.create(
            company=self.company,
            name="Sundry Creditors",
            nature="Liability",
        )
        self.tds_group = AccountGroup.objects.create(
            company=self.company,
            name="Duties and Taxes",
            nature="Liability",
        )
        self.vendor = Ledger.objects.create(
            company=self.company,
            name="ABC Contractors",
            account_group=self.vendor_group,
        )
        self.tds_ledger = Ledger.objects.create(
            company=self.company,
            name="TDS Payable 194C",
            account_group=self.tds_group,
        )
        self.section = TDSSection.objects.create(
            company=self.company,
            nature="TDS",
            section_code="194C",
            description="Contractor payments",
            threshold=Decimal("30000.00"),
            rate_individual=Decimal("1.00"),
            rate_company=Decimal("2.00"),
        )

    def _create_entry(self, **overrides):
        data = {
            "company": self.company,
            "section": self.section,
            "deductee_ledger": self.vendor,
            "tds_ledger": self.tds_ledger,
            "transaction_date": date(2026, 2, 10),
            "deductee_type": "Company",
            "deductible_amount": Decimal("100000.00"),
            "rate_applied": Decimal("2.00"),
            "tds_amount": Decimal("2000.00"),
            "pan_number": "ABCDE1234F",
            "is_deposited": True,
            "deposit_date": date(2026, 3, 7),
            "challan_number": "00001",
            "bsr_code": "1234567",
        }
        data.update(overrides)
        return TDSEntry.objects.create(**data)

    def _create_ready_workpaper(self):
        return TDSReturnWorkpaper.objects.create(
            company=self.company,
            form_type=TDSReturnWorkpaper.FORM_26Q,
            financial_year_start=2025,
            quarter=TDSReturnWorkpaper.Q4,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 3, 31),
            due_date=date(2026, 5, 31),
            status=TDSReturnWorkpaper.STATUS_READY_FOR_REVIEW,
            fvu_status=TDSReturnWorkpaper.FVU_VALIDATED,
            challan_status=TDSReturnWorkpaper.CHALLAN_MATCHED,
            traces_statement_status=TDSReturnWorkpaper.TRACES_ACCEPTED,
            form16_status=TDSReturnWorkpaper.FORM16_NOT_APPLICABLE,
            prepared_by=self.user,
            reviewed_by=self.user,
        )

    def _create_filed_pack(self):
        self._create_entry()
        self._create_ready_workpaper()
        pack_data = build_tds_filing_pack(self.company, 2025, TDSReturnWorkpaper.Q4, TDSReturnWorkpaper.FORM_26Q)
        pack = save_tds_filing_pack(pack_data, self.user, "Ready for filing.")
        return mark_tds_filing_pack_filed(pack, self.user, "TDS-ACK-001", "Filed on TRACES.")

    def test_return_workbench_marks_ready_when_core_controls_are_clear(self):
        self._create_entry()
        url = reverse("tds:return_workbench")

        response = self.client.get(url, {"fy": "2025", "quarter": "Q4", "form_type": "26Q"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ready gate clear")
        self.assertContains(response, "ABC Contractors")

        export_response = self.client.get(
            url,
            {"fy": "2025", "quarter": "Q4", "form_type": "26Q", "export": "csv"},
        )
        self.assertEqual(export_response.status_code, 200)
        export_text = export_response.content.decode("utf-8")
        self.assertIn("TDS Return Workbench", export_text)
        self.assertIn("Validations", export_text)
        self.assertIn("ABC Contractors", export_text)

        response = self.client.post(
            url,
            {
                "fy": "2025",
                "quarter": "Q4",
                "form_type": "26Q",
                "status": TDSReturnWorkpaper.STATUS_DRAFT,
                "fvu_status": TDSReturnWorkpaper.FVU_VALIDATED,
                "challan_status": TDSReturnWorkpaper.CHALLAN_MATCHED,
                "traces_statement_status": TDSReturnWorkpaper.TRACES_ACCEPTED,
                "form16_status": TDSReturnWorkpaper.FORM16_NOT_APPLICABLE,
                "traces_token": "TRACES-REQ-1",
                "ack_number": "",
                "notes": "Prepared from challan records.",
                "action": "mark_ready",
            },
        )

        self.assertRedirects(response, f"{url}?fy=2025&quarter=Q4&form_type=26Q")
        workpaper = TDSReturnWorkpaper.objects.get(company=self.company, form_type="26Q", quarter="Q4")
        self.assertEqual(workpaper.status, TDSReturnWorkpaper.STATUS_READY_FOR_REVIEW)
        self.assertEqual(workpaper.prepared_by, self.user)
        self.assertEqual(workpaper.reviewed_by, self.user)
        self.assertEqual(workpaper.summary_snapshot["readiness_score"], 100)
        self.assertEqual(workpaper.summary_snapshot["entry_count"], 1)

    def test_return_workbench_blocks_ready_status_with_critical_issues(self):
        self.company.tan = ""
        self.company.save(update_fields=["tan"])
        self._create_entry(
            pan_number="BAD",
            is_deposited=False,
            deposit_date=None,
            challan_number="",
            bsr_code="",
        )
        url = reverse("tds:return_workbench")

        response = self.client.post(
            url,
            {
                "fy": "2025",
                "quarter": "Q4",
                "form_type": "26Q",
                "status": TDSReturnWorkpaper.STATUS_DRAFT,
                "fvu_status": TDSReturnWorkpaper.FVU_VALIDATED,
                "challan_status": TDSReturnWorkpaper.CHALLAN_MATCHED,
                "traces_statement_status": TDSReturnWorkpaper.TRACES_ACCEPTED,
                "form16_status": TDSReturnWorkpaper.FORM16_NOT_APPLICABLE,
                "traces_token": "",
                "ack_number": "",
                "notes": "",
                "action": "mark_ready",
            },
        )

        self.assertRedirects(response, f"{url}?fy=2025&quarter=Q4&form_type=26Q")
        self.assertFalse(TDSReturnWorkpaper.objects.filter(company=self.company).exists())

        response = self.client.get(url, {"fy": "2025", "quarter": "Q4", "form_type": "26Q"})
        self.assertContains(response, "Critical blockers open")
        self.assertContains(response, "Company TAN is missing or invalid.")

    def test_tds_deposit_watch_tracks_due_dates_and_amounts(self):
        self._create_entry(
            transaction_date=date(2026, 4, 20),
            is_deposited=False,
            deposit_date=None,
            challan_number="",
            bsr_code="",
        )
        self._create_entry(
            transaction_date=date(2026, 5, 20),
            is_deposited=False,
            deposit_date=None,
            challan_number="",
            bsr_code="",
            tds_amount=Decimal("3000.00"),
        )
        self._create_entry(transaction_date=date(2026, 5, 21), is_deposited=True)

        watch = build_tds_deposit_watch(self.company, today=date(2026, 6, 2), horizon_days=5)

        self.assertEqual(tds_deposit_due_date(date(2026, 4, 20)), date(2026, 5, 7))
        self.assertEqual(tds_deposit_due_date(date(2026, 3, 20)), date(2026, 4, 30))
        self.assertEqual(tds_deposit_due_date(date(2026, 12, 20)), date(2027, 1, 7))
        self.assertEqual(watch["summary"]["pending_count"], 2)
        self.assertEqual(watch["summary"]["overdue_count"], 1)
        self.assertEqual(watch["summary"]["due_soon_count"], 1)
        self.assertEqual(watch["summary"]["pending_amount"], Decimal("5000.00"))

    def test_tds_entry_list_exports_filtered_csv(self):
        self._create_entry(
            transaction_date=date(2026, 2, 10),
            pan_number="ABCDE1234F",
            is_deposited=True,
            deposit_date=date(2026, 3, 7),
            challan_number="00001",
            bsr_code="1234567",
        )
        other_vendor = Ledger.objects.create(
            company=self.company,
            name="XYZ Consultants",
            account_group=self.vendor_group,
        )
        self._create_entry(
            deductee_ledger=other_vendor,
            transaction_date=date(2026, 4, 20),
            pan_number="ZZZZZ1234Z",
            is_deposited=False,
            deposit_date=None,
            challan_number="",
            bsr_code="",
        )

        response = self.client.get(
            reverse("tds:entry_list"),
            {"deposited": "1", "q": "ABCDE", "export": "csv"},
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv; charset=utf-8")
        csv_text = response.content.decode("utf-8")
        self.assertIn("Date,Section,Party,PAN,Base Amount,Rate %,TDS Amount,Deposit Due Date", csv_text)
        self.assertIn("ABC Contractors", csv_text)
        self.assertIn("ABCDE1234F", csv_text)
        self.assertIn("2026-03-07", csv_text)
        self.assertNotIn("XYZ Consultants", csv_text)

    def test_tds_register_exports_section_summary_csv(self):
        self._create_entry(is_deposited=False, deposit_date=None, challan_number="", bsr_code="")

        response = self.client.get(reverse("tds:tds_register"), {"export": "csv"})

        self.assertEqual(response.status_code, 200)
        csv_text = response.content.decode("utf-8")
        self.assertIn("Section,Nature,Description,Entries,Total TDS,Deposited,Payable", csv_text)
        self.assertIn("194C", csv_text)
        self.assertIn("2000.00", csv_text)

    def test_tds_filing_pack_generates_and_downloads_exports(self):
        self._create_entry()
        workpaper = self._create_ready_workpaper()
        url = reverse("tds:filing_pack")

        response = self.client.get(url, {"fy": "2025", "quarter": "Q4", "form_type": "26Q"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Exports available")
        self.assertContains(response, "RPU Deductee Preview")

        response = self.client.post(
            url,
            {
                "fy": "2025",
                "quarter": "Q4",
                "form_type": "26Q",
                "notes": "Ready for RPU entry.",
                "action": "generate_pack",
            },
        )

        self.assertRedirects(response, f"{url}?fy=2025&quarter=Q4&form_type=26Q")
        pack = TDSFilingPack.objects.get(company=self.company, form_type="26Q", quarter="Q4")
        self.assertEqual(pack.workpaper, workpaper)
        self.assertEqual(pack.status, TDSFilingPack.STATUS_READY)
        self.assertEqual(pack.summary_snapshot["deductee_export_rows"], 1)
        self.assertEqual(pack.export_snapshot["deductee_rows"][0]["Deductee PAN"], "ABCDE1234F")

        xlsx = self.client.get(
            reverse("tds:filing_pack_download", args=["xlsx"]),
            {"fy": "2025", "quarter": "Q4", "form_type": "26Q"},
        )
        self.assertEqual(xlsx.status_code, 200)
        self.assertEqual(
            xlsx["Content-Type"],
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        deductees = self.client.get(
            reverse("tds:filing_pack_download", args=["deductees"]),
            {"fy": "2025", "quarter": "Q4", "form_type": "26Q"},
        )
        self.assertEqual(deductees.status_code, 200)
        self.assertIn("ABCDE1234F", deductees.content.decode("utf-8-sig"))

        challans = self.client.get(
            reverse("tds:filing_pack_download", args=["challans"]),
            {"fy": "2025", "quarter": "Q4", "form_type": "26Q"},
        )
        self.assertEqual(challans.status_code, 200)
        self.assertIn("1234567", challans.content.decode("utf-8-sig"))

        payload = self.client.get(
            reverse("tds:filing_pack_download", args=["json"]),
            {"fy": "2025", "quarter": "Q4", "form_type": "26Q"},
        ).json()
        self.assertEqual(payload["company"]["tan"], "MUMA12345A")
        self.assertEqual(payload["rpu"]["deductee_rows"][0]["challan_serial"], 1)

    def test_tds_filing_pack_requires_ready_workpaper(self):
        self._create_entry()
        url = reverse("tds:filing_pack")

        response = self.client.get(url, {"fy": "2025", "quarter": "Q4", "form_type": "26Q"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Exports blocked")
        self.assertContains(response, "Mark the TDS workpaper ready for review")

        response = self.client.post(
            url,
            {
                "fy": "2025",
                "quarter": "Q4",
                "form_type": "26Q",
                "notes": "",
                "action": "generate_pack",
            },
        )
        self.assertRedirects(response, f"{url}?fy=2025&quarter=Q4&form_type=26Q")
        self.assertFalse(TDSFilingPack.objects.filter(company=self.company).exists())

        response = self.client.get(
            reverse("tds:filing_pack_download", args=["deductees"]),
            {"fy": "2025", "quarter": "Q4", "form_type": "26Q"},
        )
        self.assertRedirects(response, f"{url}?fy=2025&quarter=Q4&form_type=26Q")

    def test_post_filing_center_tracks_traces_defaults_and_syncs_certificates(self):
        pack = self._create_filed_pack()
        url = reverse("tds:post_filing_center")

        response = self.client.get(url, {"fy": "2025", "quarter": "Q4", "form_type": "26Q"})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "TDS Post-Filing Center")

        response = self.client.post(
            url,
            {
                "fy": "2025",
                "quarter": "Q4",
                "form_type": "26Q",
                "action": "sync_certificates",
            },
        )
        self.assertRedirects(response, f"{url}?fy=2025&quarter=Q4&form_type=26Q")
        cert = TDSCertificateIssue.objects.get(pack=pack)
        self.assertEqual(cert.certificate_type, TDSCertificateIssue.CERT_FORM16A)
        self.assertEqual(cert.deductee_pan, "ABCDE1234F")

        response = self.client.post(
            url,
            {
                "fy": "2025",
                "quarter": "Q4",
                "form_type": "26Q",
                "statement_status": TDSPostFilingTracker.STATEMENT_PROCESSED_DEFAULT,
                "traces_request_number": "TRACES-STATUS-1",
                "justification_report_status": TDSPostFilingTracker.REPORT_DOWNLOADED,
                "justification_request_number": "JR-1",
                "conso_file_status": TDSPostFilingTracker.REPORT_REQUESTED,
                "conso_request_number": "CONSO-1",
                "correction_required": "1",
                "correction_status": TDSPostFilingTracker.CORRECTION_OPEN,
                "correction_reason": "PAN default",
                "notes": "Default under review.",
                "action": "save_tracker",
            },
        )

        self.assertRedirects(response, f"{url}?fy=2025&quarter=Q4&form_type=26Q")
        tracker = TDSPostFilingTracker.objects.get(pack=pack)
        self.assertEqual(tracker.statement_status, TDSPostFilingTracker.STATEMENT_PROCESSED_DEFAULT)
        self.assertTrue(tracker.correction_required)
        self.assertEqual(tracker.correction_reason, "PAN default")
        self.assertIsNotNone(tracker.justification_downloaded_at)

        response = self.client.get(url, {"fy": "2025", "quarter": "Q4", "form_type": "26Q"})
        self.assertContains(response, "Statement is processed with default")
        self.assertContains(response, "ABCDE1234F")

    def test_post_filing_certificate_update_marks_issue_evidence(self):
        pack = self._create_filed_pack()
        self.client.post(
            reverse("tds:post_filing_center"),
            {"fy": "2025", "quarter": "Q4", "form_type": "26Q", "action": "sync_certificates"},
        )
        cert = TDSCertificateIssue.objects.get(pack=pack)
        url = reverse("tds:post_filing_center")

        response = self.client.post(
            url,
            {
                "fy": "2025",
                "quarter": "Q4",
                "form_type": "26Q",
                "certificate_id": cert.pk,
                "status": TDSCertificateIssue.STATUS_ISSUED,
                "request_number": "F16A-REQ-1",
                "issue_channel": TDSCertificateIssue.CHANNEL_EMAIL,
                "evidence_reference": "mail-001",
                "notes": "Sent by email.",
                "action": "update_certificate",
            },
        )

        self.assertRedirects(response, f"{url}?fy=2025&quarter=Q4&form_type=26Q")
        cert.refresh_from_db()
        self.assertEqual(cert.status, TDSCertificateIssue.STATUS_ISSUED)
        self.assertEqual(cert.request_number, "F16A-REQ-1")
        self.assertEqual(cert.issue_channel, TDSCertificateIssue.CHANNEL_EMAIL)
        self.assertEqual(cert.evidence_reference, "mail-001")
        self.assertEqual(cert.issued_by, self.user)
        self.assertIsNotNone(cert.issued_at)
