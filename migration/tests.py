import os
import tempfile
from datetime import date, timedelta
from decimal import Decimal

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from migration.parser import SmartParser
from migration.models import ImportSession
from core.models import AuditLog, Company, PracticeTask, UserCompanyAccess
from clients.models import ClientSubscription
from ledger.models import AccountGroup, Ledger
from vouchers.models import Voucher, VoucherItem
from django.contrib.auth import get_user_model


class SmartParserTests(SimpleTestCase):
    def test_tally_amount_and_drcr_columns_are_split_into_debit_credit(self):
        content = (
            "Date,Particulars,Voucher Type,Voucher No,Amount,Dr/Cr\n"
            "2026-05-01,Customer A,Sales,S-1,118.00,Dr\n"
            "2026-05-01,Sales Ledger,Sales,S-1,118.00,Cr\n"
        )
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        try:
            handle.write(content)
            handle.close()

            parser = SmartParser(handle.name, "csv")
            mapping = parser.detect_columns()
            vouchers = parser.group_vouchers(mapping)
            report = parser.build_quality_report(mapping)
        finally:
            os.unlink(handle.name)

        self.assertEqual(mapping["amount"], "amount")
        self.assertEqual(mapping["drcr"], "dr/cr")
        self.assertEqual(len(vouchers), 1)
        self.assertEqual(vouchers[0]["items"][0]["debit"], 118.0)
        self.assertEqual(vouchers[0]["items"][1]["credit"], 118.0)
        self.assertEqual(report["unbalanced_voucher_count"], 0)

    def test_quality_report_flags_party_master_and_row_cleanup_issues(self):
        content = (
            "Date,Particulars,Voucher Type,Voucher No,Debit,Credit,GSTIN,PAN,Email,WhatsApp\n"
            "2026-05-01,Customer A,Sales,S-1,118.00,,BADGST,BADPAN,not-an-email,123\n"
            "2026-05-01,Sales Ledger,Sales,S-1,,118.00,,,,\n"
            "bad-date,Customer B,Sales,S-2,50.00,50.00,27AAAAA0000A1Z5,AAAAA0000A,valid@example.com,+919876543210\n"
        )
        handle = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False)
        try:
            handle.write(content)
            handle.close()

            parser = SmartParser(handle.name, "csv")
            mapping = parser.detect_columns()
            report = parser.build_quality_report(mapping)
        finally:
            os.unlink(handle.name)

        issue_keys = {issue["key"] for issue in report["issues"]}
        self.assertIn("invalid_gstin", issue_keys)
        self.assertIn("invalid_pan", issue_keys)
        self.assertIn("invalid_email", issue_keys)
        self.assertIn("invalid_whatsapp", issue_keys)
        self.assertIn("invalid_dates", issue_keys)
        self.assertIn("both_debit_credit", issue_keys)
        self.assertLess(report["cleanup_score"], 75)
        self.assertGreater(report["blocking_issue_count"], 0)


class MigrationCleanupWorkflowTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Migration Co",
            gstin="27MIGRA0000M1Z5",
            short_code="MIG",
        )
        self.read_only_company = Company.objects.create(
            name="Migration Read Only Co",
            gstin="27MIGRO0000R1Z5",
            short_code="MRO",
        )
        self.user = get_user_model().objects.create_user(
            email="migration@example.com",
            password="migration-pass",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.company,
            role="Admin",
        )
        UserCompanyAccess.objects.create(
            user=self.user,
            company=self.read_only_company,
            role="Viewer",
        )
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
        )
        ClientSubscription.objects.create(
            company=self.read_only_company,
            primary_user=self.user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
        )
        self.client.force_login(self.user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()
        self.client.get(reverse("core:switch_company", args=[self.company.pk]))

        self.import_session = ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/sample.csv",
            file_type="csv",
            status="parsed",
            ledger_mapping={
                "New Vendor": {"action": "create", "id": None},
                "Old Vendor": {"action": "ignore", "id": None},
            },
            validation_report={
                "duplicate_voucher_count": 0,
                "unbalanced_voucher_count": 0,
                "issues": [
                    {
                        "key": "invalid_gstin",
                        "severity": "high",
                        "title": "Invalid GSTIN values",
                        "message": "GSTIN values must be corrected.",
                        "count": 2,
                        "samples": [{"row": 2, "ledger": "New Vendor", "value": "BADGST"}],
                    }
                ],
            },
        )

    def test_cleanup_export_downloads_issue_csv(self):
        response = self.client.get(reverse("migration:cleanup_export", args=[self.import_session.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode()
        self.assertIn("Invalid GSTIN values", body)
        self.assertIn("New ledgers going to Suspense", body)
        self.assertIn("Ignored ledgers", body)

    def test_create_cleanup_tasks_is_idempotent(self):
        url = reverse("migration:cleanup_tasks", args=[self.import_session.pk])

        response = self.client.post(url)
        self.assertRedirects(response, reverse("migration:preview", args=[self.import_session.pk]))
        self.assertEqual(
            PracticeTask.objects.filter(
                company=self.company,
                reference__startswith=f"IMPORTCLEAN:{self.import_session.pk}:",
            ).count(),
            3,
        )

        self.client.post(url)
        self.assertEqual(
            PracticeTask.objects.filter(
                company=self.company,
                reference__startswith=f"IMPORTCLEAN:{self.import_session.pk}:",
            ).count(),
            3,
        )

    def test_import_template_downloads_csv(self):
        response = self.client.get(reverse("migration:template"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Voucher No", body)
        self.assertIn("WhatsApp", body)

    def test_migration_sessions_page_lists_resume_and_health(self):
        response = self.client.get(reverse("migration:sessions"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Tally / Excel Migration Workspace", body)
        self.assertIn("Sync Risk", body)
        self.assertIn("Invalid GSTIN values", body)
        self.assertIn("Resume", body)

    def test_tally_exit_control_scores_clients_and_exports(self):
        response = self.client.get(reverse("migration:exit_control"))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Tally Exit Control", body)
        self.assertIn("Migration Co", body)
        self.assertIn("Parallel Run", body)
        self.assertIn("Invalid GSTIN values", body)
        self.assertIn("Migration Read Only Co", body)
        self.assertIn("Read-only", body)

        export = self.client.get(reverse("migration:exit_control"), {"export": "csv"})
        self.assertEqual(export.status_code, 200)
        self.assertEqual(export["Content-Type"], "text/csv")
        self.assertIn("Company,GSTIN,Exit Score,Band", export.content.decode())
        self.assertIn("Parallel Run Score", export.content.decode())

    def test_tally_exit_control_creates_idempotent_tasks_for_writable_clients(self):
        response = self.client.post(reverse("migration:exit_control"))

        self.assertRedirects(response, reverse("migration:exit_control"))
        self.assertGreater(
            PracticeTask.objects.filter(company=self.company, reference__startswith=f"IMPORTCLEAN:{self.import_session.pk}:").count(),
            0,
        )
        self.assertTrue(
            PracticeTask.objects.filter(company=self.company, reference__startswith=f"TALLYPARALLEL:{self.company.pk}:").exists()
        )
        self.assertFalse(
            PracticeTask.objects.filter(company=self.read_only_company, reference__startswith="TALLYEXIT:").exists()
        )
        first_count = PracticeTask.objects.filter(company=self.company).count()

        self.client.post(reverse("migration:exit_control"))

        self.assertEqual(PracticeTask.objects.filter(company=self.company).count(), first_count)

    def test_upload_captures_tally_sync_fingerprint_and_period_controls(self):
        upload = SimpleUploadedFile(
            "tally-export.csv",
            (
                b"Date,Particulars,Voucher Type,Voucher No,Debit,Credit\n"
                b"2026-05-01,Customer A,Sales,S-1,118.00,\n"
                b"2026-05-01,Sales Ledger,Sales,S-1,,118.00\n"
            ),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("migration:upload"),
            {
                "source_system": ImportSession.SOURCE_TALLY_EXCEL,
                "sync_mode": ImportSession.SYNC_INCREMENTAL,
                "source_company_guid": "TALLY-GUID-001",
                "source_period_start": "2026-05-01",
                "source_period_end": "2026-05-31",
                "file": upload,
            },
        )

        self.assertEqual(response.status_code, 302)
        session = ImportSession.objects.get(source_company_guid="TALLY-GUID-001")
        self.assertEqual(response["Location"], reverse("migration:map_ledgers", args=[session.pk]))
        self.assertEqual(session.sync_mode, ImportSession.SYNC_INCREMENTAL)
        self.assertEqual(session.source_company_guid, "TALLY-GUID-001")
        self.assertEqual(len(session.source_file_hash), 64)
        self.assertEqual(len(session.import_fingerprint), 64)
        self.assertEqual(session.validation_report["sync_control"]["source_company_guid"], "TALLY-GUID-001")
        self.assertEqual(session.validation_report["sync_control"]["import_fingerprint"], session.import_fingerprint)
        self.assertIn("sync_risk", session.validation_report)

    def test_preview_surfaces_tally_sync_duplicate_and_period_risks(self):
        file_hash = "a" * 64
        fingerprint = "b" * 64
        ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/previous.csv",
            file_type="csv",
            status="confirmed",
            source_system=ImportSession.SOURCE_TALLY_EXCEL,
            sync_mode=ImportSession.SYNC_INCREMENTAL,
            source_company_guid="TALLY-GUID-002",
            source_period_start="2026-05-01",
            source_period_end="2026-05-31",
            source_file_hash=file_hash,
            import_fingerprint=fingerprint,
        )
        Voucher.objects.create(
            company=self.company,
            voucher_type="Journal",
            date=date(2026, 5, 10),
            source_system="tally",
            source_reference="TALLY-EXISTING-1",
        )
        current = ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/current.csv",
            file_type="csv",
            status="parsed",
            source_system=ImportSession.SOURCE_TALLY_EXCEL,
            sync_mode=ImportSession.SYNC_REPLACE_PERIOD,
            source_company_guid="TALLY-GUID-002",
            source_period_start="2026-05-15",
            source_period_end="2026-06-15",
            source_file_hash=file_hash,
            import_fingerprint=fingerprint,
            detected_mapping={"date": "Date", "ledger": "Ledger", "debit": "Debit", "credit": "Credit"},
            raw_preview=[{"Date": "2026-05-15", "Ledger": "Customer", "Debit": "100.00", "Credit": ""}],
            ledger_mapping={
                "New Ledger": {"action": "create", "id": None},
                "Ignored Ledger": {"action": "ignore", "id": None},
            },
            duplicate_voucher_count=1,
            unbalanced_voucher_count=1,
            opening_balances_count=1,
            validation_report={"issues": []},
        )

        response = self.client.get(reverse("migration:preview", args=[current.pk]))

        self.assertEqual(response.status_code, 200)
        body = response.content.decode()
        self.assertIn("Tally Sync Risk Review", body)
        self.assertIn("Same source file already imported", body)
        self.assertIn("Same sync fingerprint already exists", body)
        self.assertIn("Tally company period overlaps another import", body)
        self.assertIn("Replace-period sync mode selected", body)
        self.assertIn("Unbalanced vouchers detected", body)
        self.assertIn("Export Sync Risk", body)
        self.assertIn("CA Approval Gate", body)
        self.assertIn("Approve Import", body)
        self.assertIn("CA approval is required before confirmation.", body)

    def test_repeat_tally_import_surfaces_mapping_drift_and_voucher_delta(self):
        debtors = AccountGroup.objects.create(company=self.company, name="Sundry Debtors", nature="Asset")
        sales_group = AccountGroup.objects.create(company=self.company, name="Sales Accounts", nature="Income")
        old_customer = Ledger.objects.create(company=self.company, name="Legacy Customer", account_group=debtors)
        current_customer = Ledger.objects.create(company=self.company, name="Customer A", account_group=debtors)
        sales = Ledger.objects.create(company=self.company, name="Sales Ledger", account_group=sales_group)
        prior = ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/prior.csv",
            file_type="csv",
            status="confirmed",
            source_system=ImportSession.SOURCE_TALLY_EXCEL,
            source_company_guid="TALLY-GUID-DELTA",
            source_period_start=date(2026, 5, 1),
            source_period_end=date(2026, 5, 31),
            ledger_mapping={
                "Customer A": {"action": "map", "id": old_customer.pk},
                "Sales Ledger": {"action": "map", "id": sales.pk},
            },
            validation_report={"issues": []},
        )
        existing = Voucher.objects.create(
            company=self.company,
            voucher_type="Sales",
            date=date(2026, 5, 1),
            source_system="tally",
            source_reference="S-1",
            narration="Prior Tally import",
        )
        VoucherItem.objects.create(voucher=existing, ledger=old_customer, entry_type="DR", amount=Decimal("100.00"))
        VoucherItem.objects.create(voucher=existing, ledger=sales, entry_type="CR", amount=Decimal("100.00"))
        upload = SimpleUploadedFile(
            "tally-delta.csv",
            (
                b"Date,Particulars,Voucher Type,Voucher No,Debit,Credit\n"
                b"2026-05-01,Customer A,Sales,S-1,118.00,\n"
                b"2026-05-01,Sales Ledger,Sales,S-1,,118.00\n"
                b"2026-05-02,Customer A,Sales,S-2,50.00,\n"
                b"2026-05-02,Sales Ledger,Sales,S-2,,50.00\n"
            ),
            content_type="text/csv",
        )

        response = self.client.post(
            reverse("migration:upload"),
            {
                "source_system": ImportSession.SOURCE_TALLY_EXCEL,
                "sync_mode": ImportSession.SYNC_INCREMENTAL,
                "source_company_guid": prior.source_company_guid,
                "source_period_start": "2026-05-01",
                "source_period_end": "2026-05-31",
                "file": upload,
            },
        )

        current = ImportSession.objects.exclude(pk=prior.pk).get(source_company_guid=prior.source_company_guid)
        self.assertRedirects(response, reverse("migration:map_ledgers", args=[current.pk]), fetch_redirect_response=False)
        map_payload = {}
        for name, decision in current.ledger_mapping.items():
            map_payload[f"action_{name}"] = decision["action"]
            if decision["action"] == "map":
                map_payload[f"target_{name}"] = decision["id"]
        self.client.post(reverse("migration:map_ledgers", args=[current.pk]), map_payload)

        current.refresh_from_db()
        report = current.validation_report
        self.assertEqual(report["mapping_drift"]["changed_count"], 1)
        self.assertEqual(report["voucher_delta"]["changed_count"], 1)
        self.assertEqual(report["voucher_delta"]["new_count"], 1)
        risk_keys = {issue["key"] for issue in report["sync_risk"]["issues"]}
        self.assertIn("mapping_drift_changed", risk_keys)
        self.assertIn("voucher_delta_changed", risk_keys)

        preview = self.client.get(reverse("migration:preview", args=[current.pk]))
        body = preview.content.decode()
        self.assertIn("Tally Mapping Drift", body)
        self.assertIn("Voucher Delta Review", body)
        self.assertIn("Ledger mapping changed since prior import", body)
        self.assertIn("Changed existing Tally vouchers", body)

    def test_high_risk_import_confirm_is_blocked_without_ca_approval(self):
        current = ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/current.csv",
            file_type="csv",
            status="parsed",
            source_system=ImportSession.SOURCE_TALLY_EXCEL,
            sync_mode=ImportSession.SYNC_REPLACE_PERIOD,
            source_company_guid="TALLY-GUID-BLOCK",
            source_period_start="2026-05-01",
            source_period_end="2026-05-31",
            source_file_hash="e" * 64,
            import_fingerprint="f" * 64,
            detected_mapping={"date": "Date", "ledger": "Ledger", "debit": "Debit", "credit": "Credit"},
            ledger_mapping={},
            duplicate_voucher_count=1,
            validation_report={"issues": []},
        )

        response = self.client.post(reverse("migration:confirm", args=[current.pk]))

        self.assertRedirects(response, reverse("migration:preview", args=[current.pk]), fetch_redirect_response=False)
        current.refresh_from_db()
        self.assertEqual(current.status, "parsed")
        gate = current.validation_report["approval_gate"]
        self.assertTrue(gate["required"])
        self.assertFalse(gate["can_confirm"])

    def test_ca_approval_records_checklist_snapshot_hash_and_audit_log(self):
        current = ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/current.csv",
            file_type="csv",
            status="parsed",
            source_system=ImportSession.SOURCE_TALLY_EXCEL,
            sync_mode=ImportSession.SYNC_REPLACE_PERIOD,
            source_company_guid="TALLY-GUID-APPROVE",
            source_period_start="2026-05-01",
            source_period_end="2026-05-31",
            source_file_hash="1" * 64,
            import_fingerprint="2" * 64,
            detected_mapping={"date": "Date", "ledger": "Ledger", "debit": "Debit", "credit": "Credit"},
            ledger_mapping={},
            duplicate_voucher_count=1,
            validation_report={"issues": []},
        )

        response = self.client.post(
            reverse("migration:approve_import", args=[current.pk]),
            {
                "backup_taken": "on",
                "period_verified": "on",
                "duplicate_reviewed": "on",
                "ledger_mapping_reviewed": "on",
                "opening_balances_verified": "on",
                "approval_note": "Backup verified and duplicate import risk reviewed by CA.",
            },
        )

        self.assertRedirects(response, reverse("migration:preview", args=[current.pk]), fetch_redirect_response=False)
        current.refresh_from_db()
        self.assertEqual(current.approval_status, ImportSession.APPROVAL_APPROVED)
        self.assertEqual(current.approved_by, self.user)
        self.assertEqual(len(current.approval_evidence_hash), 64)
        self.assertTrue(current.approval_snapshot["approval_blockers"])
        self.assertTrue(current.validation_report["approval_gate"]["can_confirm"])
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="ImportSession",
                record_id=current.pk,
                new_data__approval_status=ImportSession.APPROVAL_APPROVED,
            ).exists()
        )

    def test_sync_risk_export_downloads_csv_evidence(self):
        prior = ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/previous.csv",
            file_type="csv",
            status="confirmed",
            source_system=ImportSession.SOURCE_TALLY_EXCEL,
            source_company_guid="TALLY-GUID-003",
            source_period_start="2026-05-01",
            source_period_end="2026-05-31",
            source_file_hash="c" * 64,
            import_fingerprint="d" * 64,
        )
        current = ImportSession.objects.create(
            user=self.user,
            company=self.company,
            file="migrations/current.csv",
            file_type="csv",
            status="parsed",
            source_system=ImportSession.SOURCE_TALLY_EXCEL,
            source_company_guid=prior.source_company_guid,
            source_period_start="2026-05-15",
            source_period_end="2026-06-15",
            source_file_hash=prior.source_file_hash,
            import_fingerprint=prior.import_fingerprint,
            validation_report={"issues": []},
        )

        response = self.client.get(reverse("migration:sync_risk_export", args=[current.pk]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/csv")
        body = response.content.decode()
        self.assertIn("Session,Source System,Sync Mode", body)
        self.assertIn("Same source file already imported", body)
        self.assertIn("Tally company period overlaps another import", body)
