import gzip
from io import StringIO
import tempfile
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from core.models import AuditLog, Company, PracticeTask, UserCompanyAccess
from core.production_trust import (
    BACKUP_POLICY_TASK_PREFIX,
    RESTORE_DRILL_REQUIRED_CHECKS,
    SCHEDULED_BACKUP_TASK_PREFIX,
    build_backup_policy_watchdog,
    build_scheduled_backup_watchdog,
    create_backup_policy_tasks,
    create_scheduled_backup_tasks,
    list_backup_manifests,
    list_restore_drills,
    list_scheduled_backup_runs,
    record_restore_drill,
    run_operational_backup,
    run_scheduled_backup,
    verify_backup_restore_rehearsal,
)


class ProductionTrustTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(
            name="Trust Center Co",
            gstin="27TRUST0000T1Z5",
            short_code="TCC",
        )
        self.staff_user = get_user_model().objects.create_user(
            email="trust-staff@example.com",
            password="secret",
            is_staff=True,
        )
        self.normal_user = get_user_model().objects.create_user(
            email="trust-user@example.com",
            password="secret",
        )
        for user in [self.staff_user, self.normal_user]:
            UserCompanyAccess.objects.create(user=user, company=self.company, role="Admin")
        ClientSubscription.objects.create(
            company=self.company,
            primary_user=self.staff_user,
            status=ClientSubscription.STATUS_ACTIVE,
            subscription_end=timezone.now() + timedelta(days=30),
        )

    def _login(self, user):
        self.client.force_login(user)
        session = self.client.session
        session["current_company_id"] = self.company.pk
        session.save()

    def test_staff_can_view_production_trust_center(self):
        self._login(self.staff_user)

        response = self.client.get(reverse("core:production_trust_center"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Production Trust")
        self.assertContains(response, "Production Preflight")
        self.assertContains(response, "Backup Manifests")
        self.assertContains(response, "Backup Policy Watchdog")
        self.assertContains(response, "Scheduled / Offsite Backups")
        self.assertContains(response, "Restore Drill Evidence")

    def test_non_staff_cannot_view_production_trust_center(self):
        self._login(self.normal_user)

        response = self.client.get(reverse("core:production_trust_center"))

        self.assertRedirects(response, reverse("core:dashboard"))

    def test_backup_drill_writes_manifest_and_data_file(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                result = run_operational_backup(output_dir=output_dir)
                manifests = list_backup_manifests(output_dir=output_dir)

        self.assertIsNotNone(result["manifest"])
        self.assertEqual(len(manifests), 1)
        self.assertTrue(manifests[0]["valid"])
        self.assertTrue(manifests[0]["data_file_exists"])
        self.assertFalse(manifests[0]["encrypted"])
        self.assertEqual(manifests[0]["encryption_status"], "not_configured")

    def test_backup_policy_watchdog_creates_follow_up_tasks(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                run_operational_backup(output_dir=output_dir)
                manifests = list_backup_manifests(output_dir=output_dir)
                watchdog = build_backup_policy_watchdog(manifests, [])
                result = create_backup_policy_tasks(
                    company=self.company,
                    user=self.staff_user,
                    watchdog=watchdog,
                )

        self.assertEqual(watchdog["status"], "Blocked")
        self.assertTrue(any(issue["code"] == "backup_unencrypted" for issue in watchdog["issues"]))
        self.assertTrue(any(issue["code"] == "retention_low" for issue in watchdog["issues"]))
        self.assertGreaterEqual(result["created"], 2)
        tasks = PracticeTask.objects.filter(company=self.company, reference__startswith=BACKUP_POLICY_TASK_PREFIX)
        self.assertGreaterEqual(tasks.count(), 2)
        self.assertTrue(tasks.filter(priority=PracticeTask.PRIORITY_CRITICAL).exists())
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                new_data__source="production_trust_backup_policy",
            ).exists()
        )

    def test_encrypted_backup_can_be_decrypted_and_satisfies_encryption_policy(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            restore_path = Path(root) / "restore.json.gz"
            with override_settings(
                BASE_DIR=Path(root),
                MEDIA_ROOT=str(Path(root) / "media"),
                BACKUP_ENCRYPTION_PASSPHRASE="correct horse battery staple for tests",
                BACKUP_MIN_RETAINED_MANIFESTS=1,
            ):
                result = run_operational_backup(output_dir=output_dir, encrypt=True)
                manifests = list_backup_manifests(output_dir=output_dir)
                watchdog = build_backup_policy_watchdog(manifests, [])
                stdout = StringIO()
                call_command(
                    "decrypt_operational_backup",
                    str(result["manifest"]["path"]),
                    output_file=str(restore_path),
                    stdout=stdout,
                )
                restore_exists = restore_path.exists()
                with gzip.open(restore_path, "rt", encoding="utf-8") as handle:
                    restore_has_content = bool(handle.read(1))

        self.assertTrue(result["manifest"]["encrypted"])
        self.assertTrue(result["manifest"]["encryption_verified"])
        self.assertTrue(result["manifest"]["data_file"].endswith(".json.gz.fernet"))
        self.assertFalse(any(issue["code"] == "backup_unencrypted" for issue in watchdog["issues"]))
        self.assertFalse(any(issue["code"] == "latest_backup_unencrypted" for issue in watchdog["issues"]))
        self.assertTrue(restore_exists)
        self.assertTrue(restore_has_content)

    def test_backup_retention_prunes_old_manifest_and_data_pairs(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                for _idx in range(4):
                    run_operational_backup(
                        output_dir=output_dir,
                        encrypt=False,
                        prune=True,
                        retention_count=2,
                    )
                manifests = list_backup_manifests(output_dir=output_dir, limit=10)
                data_files = list(output_dir.glob("akshaya-data-*"))

        self.assertEqual(len(manifests), 2)
        self.assertEqual(len(data_files), 2)

    def test_scheduled_backup_records_offsite_evidence(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            offsite_dir = Path(root) / "offsite"
            with override_settings(
                BASE_DIR=Path(root),
                MEDIA_ROOT=str(Path(root) / "media"),
                BACKUP_OFFSITE_DIR=str(offsite_dir),
                BACKUP_OFFSITE_REQUIRED=True,
                BACKUP_SCHEDULE_ENABLED=True,
                BACKUP_MIN_RETAINED_MANIFESTS=1,
            ):
                result = run_scheduled_backup(
                    output_dir=output_dir,
                    encrypt=False,
                    prune=True,
                    copy_offsite=True,
                    mode="test-scheduled",
                )
                runs = list_scheduled_backup_runs(output_dir=output_dir)
                watchdog = build_scheduled_backup_watchdog(runs)
                payload = result["scheduled_evidence"]["payload"]
                offsite_manifest_exists = (offsite_dir / payload["manifest_name"]).exists()
                offsite_data_exists = (offsite_dir / payload["data_file"]).exists()

        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["offsite_status"], "copied")
        self.assertEqual(len(payload["copied_files"]), 2)
        self.assertEqual(watchdog["status"], "Ready")
        self.assertTrue(offsite_manifest_exists)
        self.assertTrue(offsite_data_exists)

    def test_scheduled_backup_watchdog_creates_follow_up_tasks(self):
        watchdog = build_scheduled_backup_watchdog(
            [],
            policy={
                "enabled": True,
                "interval_hours": 24,
                "max_age_hours": 26,
                "offsite_required": True,
                "offsite_dir": "",
            },
        )
        result = create_scheduled_backup_tasks(
            company=self.company,
            user=self.staff_user,
            watchdog=watchdog,
        )

        self.assertEqual(watchdog["status"], "Blocked")
        self.assertEqual(result["created"], 1)
        task = PracticeTask.objects.get(company=self.company, reference__startswith=SCHEDULED_BACKUP_TASK_PREFIX)
        self.assertEqual(task.task_type, PracticeTask.TYPE_AUDIT)
        self.assertEqual(task.priority, PracticeTask.PRIORITY_CRITICAL)
        self.assertTrue(
            AuditLog.objects.filter(
                company=self.company,
                model_name="PracticeTask",
                new_data__source="production_trust_scheduled_backup",
            ).exists()
        )

    def test_restore_drill_records_evidence_and_task_follow_up(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                backup_result = run_operational_backup(output_dir=output_dir)
                manifest = backup_result["manifest"]

                failed_result = record_restore_drill(
                    manifest_name=manifest["name"],
                    outcome="failed",
                    checks={"manifest_verified": True},
                    target_environment="local restore drill",
                    notes="Database restored, login smoke failed.",
                    unresolved_findings=2,
                    finding_notes="Login smoke failed\nMedia sample pending",
                    user=self.staff_user,
                    company=self.company,
                    output_dir=output_dir,
                )
                drills = list_restore_drills(output_dir=output_dir)

                self.assertEqual(drills[0]["outcome"], "failed")
                self.assertFalse(drills[0]["passed"])
                self.assertEqual(drills[0]["unresolved_findings"], 2)
                self.assertIn("Login smoke failed", drills[0]["finding_notes"])
                self.assertIsNotNone(failed_result["task"])

                task = PracticeTask.objects.get(company=self.company, reference__startswith="PRODTRUST:RESTORE:")
                self.assertEqual(task.task_type, PracticeTask.TYPE_AUDIT)
                self.assertEqual(task.priority, PracticeTask.PRIORITY_CRITICAL)
                self.assertEqual(task.status, PracticeTask.STATUS_OPEN)
                self.assertIn("Login smoke failed", task.description)
                self.assertTrue(
                    AuditLog.objects.filter(
                        company=self.company,
                        model_name="ProductionRestoreDrill",
                        new_data__evidence_hash=failed_result["payload"]["evidence_hash"],
                    ).exists()
                )

                clean_checks = {key: True for key, _label in RESTORE_DRILL_REQUIRED_CHECKS}
                passed_result = record_restore_drill(
                    manifest_name=manifest["name"],
                    outcome="passed",
                    checks=clean_checks,
                    target_environment="local restore drill",
                    notes="Database, media, login, and report smoke passed.",
                    unresolved_findings=0,
                    finding_notes="",
                    user=self.staff_user,
                    company=self.company,
                    output_dir=output_dir,
                )
                task.refresh_from_db()
                latest_drill = list_restore_drills(output_dir=output_dir)[0]

        self.assertEqual(passed_result["closed_task_count"], 1)
        self.assertEqual(task.status, PracticeTask.STATUS_DONE)
        self.assertTrue(latest_drill["passed"])

    def test_restore_rehearsal_verifies_plain_backup_archive(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                run_operational_backup(output_dir=output_dir)
                result = verify_backup_restore_rehearsal(
                    output_dir=output_dir,
                    user=self.staff_user,
                    company=self.company,
                )
                latest_drill = list_restore_drills(output_dir=output_dir)[0]

        self.assertTrue(result["passed"])
        self.assertGreater(result["verification"]["object_count"], 0)
        self.assertEqual(result["findings"], [])
        self.assertTrue(latest_drill["passed"])
        self.assertEqual(latest_drill["completed_checks"], latest_drill["total_checks"])

    def test_restore_rehearsal_verifies_encrypted_backup_archive(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            with override_settings(
                BASE_DIR=Path(root),
                MEDIA_ROOT=str(Path(root) / "media"),
                BACKUP_ENCRYPTION_PASSPHRASE="restore rehearsal passphrase",
            ):
                run_operational_backup(output_dir=output_dir, encrypt=True)
                result = verify_backup_restore_rehearsal(
                    output_dir=output_dir,
                    user=self.staff_user,
                    company=self.company,
                )

        self.assertTrue(result["passed"])
        self.assertTrue(result["verification"]["encrypted"])
        self.assertGreater(result["verification"]["object_count"], 0)

    def test_restore_rehearsal_rejects_missing_named_manifest(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                run_operational_backup(output_dir=output_dir)
                with self.assertRaisesMessage(ValueError, "Backup manifest not found"):
                    verify_backup_restore_rehearsal(
                        manifest_name="akshaya-manifest-missing.json",
                        output_dir=output_dir,
                        user=self.staff_user,
                        company=self.company,
                    )

    def test_restore_rehearsal_command_emits_json(self):
        with tempfile.TemporaryDirectory() as root:
            output_dir = Path(root) / "backups"
            with override_settings(BASE_DIR=Path(root), MEDIA_ROOT=str(Path(root) / "media")):
                run_operational_backup(output_dir=output_dir)
                stdout = StringIO()
                call_command(
                    "verify_restore_rehearsal",
                    "--output-dir",
                    str(output_dir),
                    "--json",
                    stdout=stdout,
                )

        self.assertIn('"ok": true', stdout.getvalue())
        self.assertIn("verification", stdout.getvalue())
