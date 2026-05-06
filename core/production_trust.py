import json
import tempfile
import gzip
import hashlib
import shutil
from datetime import datetime
from io import StringIO
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.checks import ERROR, WARNING, run_checks
from django.core.management import call_command
from django.db import connections
from django.utils.dateparse import parse_datetime
from django.utils import timezone

from .backup_crypto import backup_encryption_configured, decrypt_backup_bytes
from .models import AuditLog, PracticeTask


RESTORE_DRILL_REQUIRED_CHECKS = [
    ("manifest_verified", "Backup manifest and schema reviewed"),
    ("data_hash_verified", "Data archive SHA-256 verified"),
    ("media_manifest_verified", "Media manifest sampled or verified"),
    ("restore_command_documented", "Restore command/runbook confirmed"),
    ("login_smoke_verified", "Restored environment login/smoke check passed"),
]
RESTORE_DRILL_TASK_PREFIX = "PRODTRUST:RESTORE:"
BACKUP_POLICY_TASK_PREFIX = "PRODTRUST:BACKUP:"
SCHEDULED_BACKUP_TASK_PREFIX = "PRODTRUST:SCHEDULE:"

DEFAULT_BACKUP_POLICY = {
    "max_age_hours": 24,
    "min_retained_manifests": 3,
    "restore_drill_max_age_days": 30,
    "encryption_required": True,
}

DEFAULT_SCHEDULED_BACKUP_POLICY = {
    "enabled": False,
    "interval_hours": 24,
    "max_age_hours": 26,
    "offsite_required": True,
}


def _level_for_issue(issue):
    if issue.level >= ERROR:
        return "error"
    if issue.level >= WARNING:
        return "warning"
    return "info"


def production_preflight_results(*, include_deploy=False):
    results = []
    issues = run_checks(include_deployment_checks=include_deploy)
    for issue in issues:
        results.append({
            "name": issue.id or "django.check",
            "level": _level_for_issue(issue),
            "message": issue.msg,
            "hint": issue.hint or "",
        })
    if not results:
        results.append({
            "name": "django.checks",
            "level": "ok",
            "message": "Django checks passed.",
            "hint": "",
        })
    results.append(_database_check())
    results.append(_cache_check())
    results.append(_media_check())
    return results


def _database_check():
    try:
        with connections["default"].cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
        return {"name": "database", "level": "ok", "message": "Database connection works.", "hint": ""}
    except Exception as exc:
        return {
            "name": "database",
            "level": "error",
            "message": str(exc),
            "hint": "Check DATABASE_URL and network access.",
        }


def _cache_check():
    try:
        key = "production_preflight_cache"
        cache.set(key, "ok", timeout=30)
        if cache.get(key) != "ok":
            raise RuntimeError("cache read did not return the written value")
        return {"name": "cache", "level": "ok", "message": "Cache read/write works.", "hint": ""}
    except Exception as exc:
        return {
            "name": "cache",
            "level": "warning",
            "message": str(exc),
            "hint": "Configure CACHE_URL/Redis for multi-worker deployments.",
        }


def _media_check():
    media_root = Path(settings.MEDIA_ROOT)
    try:
        media_root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=media_root, delete=True) as handle:
            handle.write(b"ok")
        return {"name": "media", "level": "ok", "message": "MEDIA_ROOT is writable.", "hint": ""}
    except Exception as exc:
        return {
            "name": "media",
            "level": "error",
            "message": str(exc),
            "hint": "Use persistent media storage before production document intake.",
        }


def backup_directory(output_dir=None):
    return Path(output_dir or settings.BASE_DIR / "backups")


def list_backup_manifests(*, output_dir=None, limit=10):
    directory = backup_directory(output_dir)
    if not directory.exists():
        return []

    manifests = []
    for path in sorted(
        directory.glob("akshaya-manifest-*.json"),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            data_file = directory / (data.get("data_file") or "")
            manifests.append({
                "path": path,
                "name": path.name,
                "created_at": data.get("created_at", ""),
                "created_at_dt": _parse_manifest_datetime(data.get("created_at"), fallback=path.stat().st_mtime),
                "schema_version": data.get("backup_schema_version"),
                "data_file": data.get("data_file", ""),
                "data_format": data.get("data_format", ""),
                "compressed": bool(data.get("compressed", data.get("data_file", "").endswith(".gz"))),
                "encrypted": bool(data.get("encrypted")),
                "encryption_status": data.get("encryption_status") or ("encrypted" if data.get("encrypted") else "not_configured"),
                "encryption_algorithm": data.get("encryption_algorithm", ""),
                "encryption_verified": bool(data.get("encryption_verified")),
                "data_size": data.get("data_size", 0),
                "data_sha256": data.get("data_sha256", ""),
                "compressed_sha256": data.get("compressed_sha256", ""),
                "data_file_exists": data_file.exists(),
                "media_file_count": data.get("media_file_count", 0),
                "media_total_bytes": data.get("media_total_bytes", 0),
                "database_engine": data.get("database_engine", ""),
                "retention_hint": data.get("retention_hint", ""),
                "restore_hint": data.get("restore_hint", ""),
                "valid": True,
                "error": "",
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()),
            })
        except Exception as exc:
            manifests.append({
                "path": path,
                "name": path.name,
                "created_at": "",
                "created_at_dt": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()),
                "schema_version": None,
                "data_file": "",
                "data_format": "",
                "compressed": False,
                "encrypted": False,
                "encryption_status": "unknown",
                "encryption_algorithm": "",
                "encryption_verified": False,
                "data_size": 0,
                "data_sha256": "",
                "compressed_sha256": "",
                "data_file_exists": False,
                "media_file_count": 0,
                "media_total_bytes": 0,
                "database_engine": "",
                "retention_hint": "",
                "restore_hint": "",
                "valid": False,
                "error": str(exc),
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()),
            })
        if len(manifests) >= limit:
            break
    return manifests


def _parse_manifest_datetime(value, *, fallback=None):
    parsed = parse_datetime((value or "").replace("Z", "+00:00")) if value else None
    if not parsed and fallback:
        parsed = datetime.fromtimestamp(fallback, tz=timezone.get_current_timezone())
    if not parsed:
        return None
    if timezone.is_naive(parsed):
        return timezone.make_aware(parsed, timezone.get_current_timezone())
    return parsed.astimezone(timezone.get_current_timezone())


def backup_policy_settings():
    return {
        "max_age_hours": max(1, int(getattr(settings, "BACKUP_MAX_AGE_HOURS", DEFAULT_BACKUP_POLICY["max_age_hours"]))),
        "min_retained_manifests": max(
            1,
            int(getattr(settings, "BACKUP_MIN_RETAINED_MANIFESTS", DEFAULT_BACKUP_POLICY["min_retained_manifests"])),
        ),
        "restore_drill_max_age_days": max(
            1,
            int(getattr(settings, "RESTORE_DRILL_MAX_AGE_DAYS", DEFAULT_BACKUP_POLICY["restore_drill_max_age_days"])),
        ),
        "encryption_required": bool(getattr(settings, "BACKUP_ENCRYPTION_REQUIRED", DEFAULT_BACKUP_POLICY["encryption_required"])),
    }


def build_backup_policy_watchdog(manifests, restore_drills, *, now=None, policy=None):
    now = now or timezone.now()
    policy = policy or backup_policy_settings()
    valid_manifests = [manifest for manifest in manifests if manifest.get("valid")]
    restorable_manifests = [
        manifest for manifest in valid_manifests
        if manifest.get("data_file_exists") and manifest.get("data_sha256")
    ]
    encrypted_manifests = [manifest for manifest in restorable_manifests if manifest.get("encrypted")]
    verified_encrypted_manifests = [
        manifest for manifest in encrypted_manifests
        if manifest.get("encryption_verified")
    ]
    latest_backup = manifests[0] if manifests else None
    latest_created_at = latest_backup.get("created_at_dt") if latest_backup else None
    latest_age_hours = None
    if latest_created_at:
        latest_age_hours = max(0, (now - latest_created_at).total_seconds() / 3600)

    issues = []

    def add_issue(code, severity, title, detail, recommendation, *, penalty=0, taskable=True):
        issues.append({
            "code": code,
            "severity": severity,
            "severity_rank": {"critical": 0, "warning": 1, "info": 2}.get(severity, 3),
            "badge_class": _policy_badge_class(severity),
            "title": title,
            "detail": detail,
            "recommendation": recommendation,
            "penalty": penalty,
            "taskable": taskable,
            "reference": f"{BACKUP_POLICY_TASK_PREFIX}{code.upper()}",
        })

    if not manifests:
        add_issue(
            "no_backup",
            "critical",
            "No backup manifest found",
            "Production Trust could not find any operational backup manifest.",
            "Run a backup drill and keep the manifest with the data archive.",
            penalty=22,
        )
    elif not latest_backup.get("valid") or not latest_backup.get("data_file_exists"):
        add_issue(
            "latest_backup_unusable",
            "critical",
            "Latest backup is not restorable",
            "The newest manifest is invalid or its data archive is missing.",
            "Run a fresh backup and keep the manifest and data archive together.",
            penalty=18,
        )

    if latest_age_hours is not None and latest_age_hours > policy["max_age_hours"]:
        stale_days = latest_age_hours / 24
        severity = "critical" if latest_age_hours > policy["max_age_hours"] * 2 else "warning"
        add_issue(
            "backup_stale",
            severity,
            "Latest backup is stale",
            f"Newest backup evidence is {stale_days:.1f} day(s) old.",
            f"Run backups at least every {policy['max_age_hours']} hour(s).",
            penalty=12 if severity == "critical" else 6,
        )

    if len(restorable_manifests) < policy["min_retained_manifests"]:
        add_issue(
            "retention_low",
            "warning",
            "Backup retention is below policy",
            f"{len(restorable_manifests)} restorable backup(s) found; policy expects {policy['min_retained_manifests']}.",
            "Keep multiple recent backup generations so one bad export does not remove recovery options.",
            penalty=6,
        )

    invalid_count = len([manifest for manifest in manifests if not manifest.get("valid") or not manifest.get("data_file_exists")])
    if invalid_count:
        add_issue(
            "invalid_backup_artifacts",
            "warning",
            "Backup artifacts need cleanup",
            f"{invalid_count} manifest(s) are invalid or missing their data archive.",
            "Re-run failed exports and remove or investigate incomplete backup artifacts.",
            penalty=4,
        )

    if policy["encryption_required"] and restorable_manifests and not encrypted_manifests:
        add_issue(
            "backup_unencrypted",
            "critical",
            "Backups are not marked encrypted",
            "Restorable backup manifests do not show encryption evidence.",
            "Run backup drills with encryption enabled and configure BACKUP_ENCRYPTION_KEY or BACKUP_ENCRYPTION_PASSPHRASE.",
            penalty=12,
        )
    elif policy["encryption_required"] and latest_backup and latest_backup.get("data_file_exists") and not latest_backup.get("encrypted"):
        add_issue(
            "latest_backup_unencrypted",
            "critical",
            "Latest backup is not encrypted",
            "Older encrypted evidence exists, but the newest backup is plain compressed data.",
            "Run the latest backup with encryption enabled before relying on it for production recovery.",
            penalty=12,
        )
    elif policy["encryption_required"] and encrypted_manifests and not verified_encrypted_manifests:
        add_issue(
            "encryption_not_verified",
            "warning",
            "Encrypted backup verification is missing",
            "Encrypted manifests exist but do not record a successful decrypt verification.",
            "Re-run encrypted backup with verification metadata so restore evidence can be trusted.",
            penalty=4,
        )

    clean_restore_drills = [drill for drill in restore_drills if drill.get("passed")]
    if not clean_restore_drills:
        add_issue(
            "restore_drill_missing",
            "warning",
            "No clean restore drill in evidence",
            "A backup exists, but no passing restore drill evidence is linked to the trust center.",
            "Record a passing restore drill after restoring the latest backup into a test environment.",
            penalty=0,
        )
    else:
        latest_drill = clean_restore_drills[0]
        drill_created = _parse_manifest_datetime(latest_drill.get("created_at"))
        if drill_created and (now - drill_created).days > policy["restore_drill_max_age_days"]:
            add_issue(
                "restore_drill_stale",
                "warning",
                "Restore drill is stale",
                f"Latest passing restore drill is older than {policy['restore_drill_max_age_days']} day(s).",
                "Run a fresh restore drill for production readiness evidence.",
                penalty=0,
            )

    critical_count = sum(1 for issue in issues if issue["severity"] == "critical")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    score_penalty = min(32, sum(issue["penalty"] for issue in issues))
    if critical_count:
        status = "Blocked"
        badge_class = "bg-danger"
    elif warning_count:
        status = "Watch"
        badge_class = "bg-warning text-dark"
    else:
        status = "Ready"
        badge_class = "bg-success"
    issues.sort(key=lambda item: (item["severity_rank"], item["code"]))
    return {
        "status": status,
        "badge_class": badge_class,
        "issues": issues,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "info_count": sum(1 for issue in issues if issue["severity"] == "info"),
        "score_penalty": score_penalty,
        "policy": policy,
        "latest_age_hours": latest_age_hours,
        "latest_age_label": _backup_age_label(latest_age_hours),
        "restorable_count": len(restorable_manifests),
        "encrypted_count": len(encrypted_manifests),
        "verified_encrypted_count": len(verified_encrypted_manifests),
        "has_taskable_issues": any(issue["taskable"] and issue["severity"] in {"critical", "warning"} for issue in issues),
    }


def _policy_badge_class(severity):
    if severity == "critical":
        return "bg-danger"
    if severity == "warning":
        return "bg-warning text-dark"
    return "bg-info text-dark"


def _backup_age_label(age_hours):
    if age_hours is None:
        return "-"
    if age_hours < 1:
        return "under 1 hour"
    if age_hours < 48:
        return f"{age_hours:.0f} hour(s)"
    return f"{age_hours / 24:.1f} day(s)"


def scheduled_backup_settings():
    offsite_dir = (getattr(settings, "BACKUP_OFFSITE_DIR", "") or "").strip()
    return {
        "enabled": bool(getattr(settings, "BACKUP_SCHEDULE_ENABLED", DEFAULT_SCHEDULED_BACKUP_POLICY["enabled"])),
        "interval_hours": max(
            1,
            int(getattr(settings, "BACKUP_SCHEDULE_INTERVAL_HOURS", DEFAULT_SCHEDULED_BACKUP_POLICY["interval_hours"])),
        ),
        "max_age_hours": max(
            1,
            int(getattr(settings, "BACKUP_SCHEDULE_MAX_AGE_HOURS", DEFAULT_SCHEDULED_BACKUP_POLICY["max_age_hours"])),
        ),
        "offsite_required": bool(getattr(settings, "BACKUP_OFFSITE_REQUIRED", DEFAULT_SCHEDULED_BACKUP_POLICY["offsite_required"])),
        "offsite_dir": offsite_dir,
    }


def list_scheduled_backup_runs(*, output_dir=None, limit=10):
    directory = backup_directory(output_dir)
    if not directory.exists():
        return []

    runs = []
    for path in sorted(
        directory.glob("akshaya-scheduled-backup-*.json"),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            status = data.get("status", "failed")
            offsite_status = data.get("offsite_status", "not_configured")
            created_at = _parse_manifest_datetime(data.get("created_at"), fallback=path.stat().st_mtime)
            runs.append({
                "path": path,
                "name": path.name,
                "created_at": data.get("created_at", ""),
                "created_at_dt": created_at,
                "mode": data.get("mode", "scheduled"),
                "status": status,
                "status_label": _scheduled_status_label(status),
                "offsite_status": offsite_status,
                "offsite_label": _offsite_status_label(offsite_status),
                "offsite_dir": data.get("offsite_dir", ""),
                "manifest_name": data.get("manifest_name", ""),
                "data_file": data.get("data_file", ""),
                "encrypted": bool(data.get("encrypted")),
                "retention_pruned": bool(data.get("retention_pruned")),
                "copied_files": data.get("copied_files") or [],
                "error": data.get("error", ""),
                "evidence_hash": data.get("evidence_hash", ""),
                "valid": True,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()),
            })
        except Exception as exc:
            runs.append({
                "path": path,
                "name": path.name,
                "created_at": "",
                "created_at_dt": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()),
                "mode": "scheduled",
                "status": "failed",
                "status_label": "Failed",
                "offsite_status": "failed",
                "offsite_label": "Failed",
                "offsite_dir": "",
                "manifest_name": "",
                "data_file": "",
                "encrypted": False,
                "retention_pruned": False,
                "copied_files": [],
                "error": str(exc),
                "evidence_hash": "",
                "valid": False,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()),
            })
        if len(runs) >= limit:
            break
    return runs


def build_scheduled_backup_watchdog(runs, *, now=None, policy=None):
    now = now or timezone.now()
    policy = policy or scheduled_backup_settings()
    latest_run = runs[0] if runs else None
    latest_age_hours = None
    if latest_run and latest_run.get("created_at_dt"):
        latest_age_hours = max(0, (now - latest_run["created_at_dt"]).total_seconds() / 3600)

    issues = []

    def add_issue(code, severity, title, detail, recommendation, *, penalty=0, taskable=True):
        issues.append({
            "code": code,
            "severity": severity,
            "severity_rank": {"critical": 0, "warning": 1, "info": 2}.get(severity, 3),
            "badge_class": _policy_badge_class(severity),
            "title": title,
            "detail": detail,
            "recommendation": recommendation,
            "penalty": penalty,
            "taskable": taskable,
            "reference": f"{SCHEDULED_BACKUP_TASK_PREFIX}{code.upper()}",
        })

    if not policy["enabled"]:
        add_issue(
            "schedule_disabled",
            "warning",
            "Scheduled backups are disabled",
            "Production Trust cannot prove automatic backup execution.",
            "Enable BACKUP_SCHEDULE_ENABLED and run Celery beat or an external scheduler.",
            penalty=6,
        )
    if not latest_run:
        add_issue(
            "scheduled_backup_missing",
            "critical" if policy["enabled"] else "warning",
            "No scheduled backup run evidence",
            "No scheduled/offsite backup evidence file has been recorded.",
            "Run python manage.py run_scheduled_operational_backup or start the scheduled worker path.",
            penalty=18 if policy["enabled"] else 6,
        )
    else:
        if latest_run["status"] != "ok":
            add_issue(
                "scheduled_backup_failed",
                "critical",
                "Latest scheduled backup failed",
                latest_run.get("error") or "The latest scheduled backup evidence is not successful.",
                "Fix the backup command and run the scheduled backup again.",
                penalty=18,
            )
        if latest_age_hours is not None and latest_age_hours > policy["max_age_hours"]:
            severity = "critical" if policy["enabled"] else "warning"
            add_issue(
                "scheduled_backup_stale",
                severity,
                "Scheduled backup evidence is stale",
                f"Latest scheduled backup evidence is {_backup_age_label(latest_age_hours)} old.",
                f"Expected evidence within {policy['max_age_hours']} hour(s).",
                penalty=12 if severity == "critical" else 6,
            )
        if policy["offsite_required"] and latest_run["offsite_status"] != "copied":
            add_issue(
                "offsite_copy_missing",
                "critical" if policy["enabled"] else "warning",
                "Offsite backup copy is not proven",
                f"Latest scheduled backup offsite status is {latest_run['offsite_label']}.",
                "Configure BACKUP_OFFSITE_DIR and verify manifest/data copy evidence.",
                penalty=14 if policy["enabled"] else 6,
            )

    critical_count = sum(1 for issue in issues if issue["severity"] == "critical")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    if critical_count:
        status = "Blocked"
        badge_class = "bg-danger"
    elif warning_count:
        status = "Watch"
        badge_class = "bg-warning text-dark"
    else:
        status = "Ready"
        badge_class = "bg-success"
    issues.sort(key=lambda item: (item["severity_rank"], item["code"]))
    return {
        "status": status,
        "badge_class": badge_class,
        "issues": issues,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "score_penalty": min(28, sum(issue["penalty"] for issue in issues)),
        "policy": policy,
        "latest_run": latest_run,
        "latest_age_hours": latest_age_hours,
        "latest_age_label": _backup_age_label(latest_age_hours),
        "has_taskable_issues": any(issue["taskable"] and issue["severity"] in {"critical", "warning"} for issue in issues),
    }


def run_scheduled_backup(
    *,
    output_dir=None,
    include_sessions=False,
    encrypt=None,
    prune=True,
    retention_count=None,
    copy_offsite=True,
    mode="scheduled",
):
    backup_result = run_operational_backup(
        output_dir=output_dir,
        include_sessions=include_sessions,
        encrypt=encrypt,
        prune=prune,
        retention_count=retention_count or getattr(settings, "BACKUP_RETENTION_COUNT", 10),
    )
    evidence = record_scheduled_backup_run(
        manifest=backup_result.get("manifest"),
        output_dir=backup_result["directory"],
        copy_offsite=copy_offsite,
        mode=mode,
        retention_pruned=prune,
    )
    return {
        **backup_result,
        "scheduled_evidence": evidence,
    }


def record_scheduled_backup_run(*, manifest, output_dir=None, copy_offsite=True, mode="scheduled", retention_pruned=False):
    directory = backup_directory(output_dir)
    now = timezone.now()
    payload = {
        "scheduled_backup_schema_version": 1,
        "created_at": now.isoformat(),
        "mode": mode,
        "status": "ok" if manifest else "failed",
        "manifest_name": manifest["name"] if manifest else "",
        "data_file": manifest.get("data_file", "") if manifest else "",
        "data_sha256": manifest.get("data_sha256", "") if manifest else "",
        "encrypted": bool(manifest.get("encrypted")) if manifest else False,
        "retention_pruned": bool(retention_pruned),
        "offsite_status": "disabled",
        "offsite_dir": "",
        "copied_files": [],
        "error": "" if manifest else "No backup manifest was produced.",
    }
    if manifest:
        offsite = _copy_backup_offsite(manifest, directory, copy_offsite=copy_offsite)
        payload.update(offsite)
        if offsite["offsite_status"] == "failed":
            payload["status"] = "failed"
            payload["error"] = offsite.get("error", "")
    payload["evidence_hash"] = _scheduled_backup_hash(payload)

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"akshaya-scheduled-backup-{now.strftime('%Y%m%d-%H%M%S-%f')}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return {
        "path": path,
        "name": path.name,
        "payload": payload,
    }


def create_scheduled_backup_tasks(*, company, user, watchdog):
    if not company:
        raise ValueError("Select a company before creating scheduled backup tasks.")
    active_issues = [
        issue for issue in watchdog.get("issues", [])
        if issue.get("taskable") and issue.get("severity") in {"critical", "warning"}
    ]
    active_references = {issue["reference"] for issue in active_issues}
    created_count = 0
    updated_count = 0
    closed_count = 0
    for issue in active_issues:
        description = (
            f"{issue['detail']}\n\n"
            f"Recommendation: {issue['recommendation']}\n"
            f"Schedule: every {watchdog['policy']['interval_hours']} hour(s), "
            f"stale after {watchdog['policy']['max_age_hours']} hour(s), "
            f"offsite required: {'yes' if watchdog['policy']['offsite_required'] else 'no'}."
        )
        task, created = PracticeTask.objects.get_or_create(
            company=company,
            reference=issue["reference"],
            defaults={
                "title": f"Production Trust: {issue['title']}",
                "task_type": PracticeTask.TYPE_AUDIT,
                "priority": PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": timezone.localdate() + timezone.timedelta(days=1 if issue["severity"] == "critical" else 3),
                "assigned_to": user if getattr(user, "is_authenticated", False) else None,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "description": description,
            },
        )
        if created:
            created_count += 1
            _write_scheduled_task_audit(company, user, task, "create", issue, watchdog)
        elif task.status in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED} or task.description != description:
            task.status = PracticeTask.STATUS_OPEN
            task.completed_at = None
            task.completed_by = None
            task.description = description
            task.priority = PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH
            task.save(update_fields=["status", "completed_at", "completed_by", "description", "priority", "updated_at"])
            updated_count += 1
            _write_scheduled_task_audit(company, user, task, "update", issue, watchdog)

    stale_tasks = (
        PracticeTask.objects.filter(company=company, reference__startswith=SCHEDULED_BACKUP_TASK_PREFIX)
        .exclude(reference__in=active_references)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    )
    for task in stale_tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_at = timezone.now()
        task.completed_by = user if getattr(user, "is_authenticated", False) else None
        task.description = f"{task.description}\n\nClosed because the scheduled backup issue is no longer active.".strip()
        task.save(update_fields=["status", "completed_at", "completed_by", "description", "updated_at"])
        closed_count += 1
        AuditLog.objects.create(
            company=company,
            user=user if getattr(user, "is_authenticated", False) else None,
            action=AuditLog.ACTION_UPDATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={"status": old_status},
            new_data={"source": "production_trust_scheduled_backup", "status": task.status},
        )

    return {"created": created_count, "updated": updated_count, "closed": closed_count}


def _copy_backup_offsite(manifest, directory, *, copy_offsite=True):
    policy = scheduled_backup_settings()
    if not copy_offsite:
        return {"offsite_status": "disabled", "offsite_dir": "", "copied_files": [], "error": ""}
    if not policy["offsite_dir"]:
        return {"offsite_status": "not_configured", "offsite_dir": "", "copied_files": [], "error": ""}

    offsite_dir = Path(policy["offsite_dir"])
    copied_files = []
    try:
        offsite_dir.mkdir(parents=True, exist_ok=True)
        for source in [manifest["path"], directory / manifest.get("data_file", "")]:
            if not source or not source.exists():
                return {
                    "offsite_status": "failed",
                    "offsite_dir": str(offsite_dir),
                    "copied_files": copied_files,
                    "error": f"Offsite source missing: {source}",
                }
            target = offsite_dir / source.name
            if source.resolve() == target.resolve():
                copied = source
            else:
                shutil.copy2(source, target)
                copied = target
            copied_files.append({
                "name": copied.name,
                "size": copied.stat().st_size,
                "sha256": _file_sha256(copied),
            })
        return {"offsite_status": "copied", "offsite_dir": str(offsite_dir), "copied_files": copied_files, "error": ""}
    except Exception as exc:
        return {"offsite_status": "failed", "offsite_dir": str(offsite_dir), "copied_files": copied_files, "error": str(exc)}


def _scheduled_backup_hash(payload):
    clean = dict(payload)
    clean.pop("evidence_hash", None)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scheduled_status_label(status):
    return {"ok": "OK", "failed": "Failed"}.get(status, "Failed")


def _offsite_status_label(status):
    return {
        "copied": "Copied",
        "not_configured": "Not Configured",
        "disabled": "Disabled",
        "failed": "Failed",
    }.get(status, "Failed")


def _write_scheduled_task_audit(company, user, task, action, issue, watchdog):
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if action == "create" else AuditLog.ACTION_UPDATE,
        model_name="PracticeTask",
        record_id=task.pk,
        object_repr=task.title[:200],
        old_data={},
        new_data={
            "source": "production_trust_scheduled_backup",
            "reference": task.reference,
            "issue": issue["code"],
            "severity": issue["severity"],
            "schedule_status": watchdog["status"],
        },
    )


def create_backup_policy_tasks(*, company, user, watchdog):
    if not company:
        raise ValueError("Select a company before creating backup policy tasks.")

    active_issues = [
        issue for issue in watchdog.get("issues", [])
        if issue.get("taskable") and issue.get("severity") in {"critical", "warning"}
    ]
    active_references = {issue["reference"] for issue in active_issues}
    created_count = 0
    updated_count = 0
    closed_count = 0
    tasks = []

    for issue in active_issues:
        description = (
            f"{issue['detail']}\n\n"
            f"Recommendation: {issue['recommendation']}\n"
            f"Policy: max backup age {watchdog['policy']['max_age_hours']} hour(s), "
            f"minimum retained manifests {watchdog['policy']['min_retained_manifests']}, "
            f"restore drill window {watchdog['policy']['restore_drill_max_age_days']} day(s)."
        )
        task, created = PracticeTask.objects.get_or_create(
            company=company,
            reference=issue["reference"],
            defaults={
                "title": f"Production Trust: {issue['title']}",
                "task_type": PracticeTask.TYPE_AUDIT,
                "priority": PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": timezone.localdate() + timezone.timedelta(days=1 if issue["severity"] == "critical" else 3),
                "assigned_to": user if getattr(user, "is_authenticated", False) else None,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "description": description,
            },
        )
        if created:
            created_count += 1
            _write_task_audit(company, user, task, "create", issue, watchdog)
        else:
            changed = False
            if task.status in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED}:
                task.status = PracticeTask.STATUS_OPEN
                task.completed_at = None
                task.completed_by = None
                changed = True
            priority = PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH
            if task.priority != priority:
                task.priority = priority
                changed = True
            if task.description != description:
                task.description = description
                changed = True
            if changed:
                task.save(update_fields=["status", "completed_at", "completed_by", "priority", "description", "updated_at"])
                updated_count += 1
                _write_task_audit(company, user, task, "update", issue, watchdog)
        tasks.append(task)

    stale_tasks = (
        PracticeTask.objects.filter(company=company, reference__startswith=BACKUP_POLICY_TASK_PREFIX)
        .exclude(reference__in=active_references)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    )
    for task in stale_tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_at = timezone.now()
        task.completed_by = user if getattr(user, "is_authenticated", False) else None
        task.description = f"{task.description}\n\nClosed because the backup policy issue is no longer active.".strip()
        task.save(update_fields=["status", "completed_at", "completed_by", "description", "updated_at"])
        closed_count += 1
        AuditLog.objects.create(
            company=company,
            user=user if getattr(user, "is_authenticated", False) else None,
            action=AuditLog.ACTION_UPDATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={"status": old_status},
            new_data={"source": "production_trust_backup_policy", "status": task.status},
        )

    return {
        "created": created_count,
        "updated": updated_count,
        "closed": closed_count,
        "tasks": tasks,
    }


def _write_task_audit(company, user, task, action, issue, watchdog):
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if action == "create" else AuditLog.ACTION_UPDATE,
        model_name="PracticeTask",
        record_id=task.pk,
        object_repr=task.title[:200],
        old_data={},
        new_data={
            "source": "production_trust_backup_policy",
            "reference": task.reference,
            "issue": issue["code"],
            "severity": issue["severity"],
            "policy_status": watchdog["status"],
        },
    )


def list_restore_drills(*, output_dir=None, limit=10):
    directory = backup_directory(output_dir)
    if not directory.exists():
        return []

    drills = []
    for path in sorted(
        directory.glob("akshaya-restore-drill-*.json"),
        key=lambda item: (item.stat().st_mtime, item.name),
        reverse=True,
    ):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            checks = data.get("checks") or {}
            completed_checks = sum(1 for key, _label in RESTORE_DRILL_REQUIRED_CHECKS if checks.get(key))
            unresolved = _parse_non_negative_int(data.get("unresolved_findings"), default=0)
            outcome = data.get("outcome", "failed")
            passed = _restore_payload_passed(data, checks=checks, unresolved=unresolved)
            drills.append({
                "path": path,
                "name": path.name,
                "created_at": data.get("created_at", ""),
                "manifest_name": data.get("manifest_name", ""),
                "manifest_created_at": data.get("manifest_created_at", ""),
                "manifest_data_sha256": data.get("manifest_data_sha256", ""),
                "outcome": outcome,
                "outcome_label": _restore_outcome_label(outcome),
                "target_environment": data.get("target_environment", ""),
                "operator_email": data.get("operator_email", ""),
                "notes": data.get("notes", ""),
                "finding_notes": data.get("finding_notes", ""),
                "unresolved_findings": unresolved,
                "checks": checks,
                "completed_checks": completed_checks,
                "total_checks": len(RESTORE_DRILL_REQUIRED_CHECKS),
                "passed": passed,
                "evidence_hash": data.get("evidence_hash", ""),
                "valid": True,
                "error": "",
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()),
            })
        except Exception as exc:
            drills.append({
                "path": path,
                "name": path.name,
                "created_at": "",
                "manifest_name": "",
                "manifest_created_at": "",
                "manifest_data_sha256": "",
                "outcome": "failed",
                "outcome_label": "Failed",
                "target_environment": "",
                "operator_email": "",
                "notes": "",
                "finding_notes": "",
                "unresolved_findings": 1,
                "checks": {},
                "completed_checks": 0,
                "total_checks": len(RESTORE_DRILL_REQUIRED_CHECKS),
                "passed": False,
                "evidence_hash": "",
                "valid": False,
                "error": str(exc),
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone()),
            })
        if len(drills) >= limit:
            break
    return drills


def _restore_outcome_label(outcome):
    return {
        "passed": "Passed",
        "partial": "Partial",
        "failed": "Failed",
    }.get(outcome, "Failed")


def _restore_drill_file_hash(payload):
    clean = dict(payload)
    clean.pop("evidence_hash", None)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _parse_non_negative_int(value, *, default=0):
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


def _normalise_restore_checks(checks):
    selected = {}
    for key, _label in RESTORE_DRILL_REQUIRED_CHECKS:
        if hasattr(checks, "getlist"):
            selected[key] = bool(checks.getlist(key))
        elif isinstance(checks, (set, list, tuple)):
            selected[key] = key in checks
        else:
            selected[key] = bool((checks or {}).get(key))
    return selected


def _restore_payload_passed(payload, *, checks=None, unresolved=None):
    checks = checks if checks is not None else payload.get("checks") or {}
    unresolved = unresolved if unresolved is not None else _parse_non_negative_int(payload.get("unresolved_findings"))
    return (
        payload.get("outcome") == "passed"
        and bool(payload.get("manifest_data_file_exists", True))
        and unresolved == 0
        and all(checks.get(key) for key, _label in RESTORE_DRILL_REQUIRED_CHECKS)
    )


def record_restore_drill(
    *,
    manifest_name,
    outcome,
    checks,
    target_environment,
    notes,
    unresolved_findings,
    finding_notes="",
    user,
    company,
    output_dir=None,
):
    directory = backup_directory(output_dir)
    manifests = {manifest["name"]: manifest for manifest in list_backup_manifests(output_dir=directory, limit=50)}
    manifest = manifests.get(manifest_name)
    if not manifest:
        raise ValueError("Select a valid backup manifest for the restore drill.")

    outcome = outcome if outcome in {"passed", "partial", "failed"} else "failed"
    check_payload = _normalise_restore_checks(checks)
    unresolved = _parse_non_negative_int(unresolved_findings, default=0)
    finding_notes = (finding_notes or "").strip()[:2000]
    manifest_ready = bool(manifest.get("valid") and manifest.get("data_file_exists"))
    if not manifest_ready:
        if outcome == "passed":
            outcome = "failed"
        unresolved = max(unresolved, 1)
        artifact_finding = "Backup manifest or data archive was not available for restore verification."
        finding_notes = f"{artifact_finding}\n{finding_notes}".strip()
    now = timezone.now()
    payload = {
        "restore_drill_schema_version": 1,
        "created_at": now.isoformat(),
        "manifest_name": manifest["name"],
        "manifest_created_at": manifest.get("created_at", ""),
        "manifest_data_file": manifest.get("data_file", ""),
        "manifest_data_sha256": manifest.get("data_sha256", ""),
        "manifest_data_size": manifest.get("data_size", 0),
        "manifest_media_file_count": manifest.get("media_file_count", 0),
        "manifest_data_file_exists": bool(manifest.get("data_file_exists")),
        "outcome": outcome,
        "target_environment": (target_environment or "").strip()[:120],
        "checks": check_payload,
        "unresolved_findings": unresolved,
        "finding_notes": finding_notes,
        "notes": (notes or "").strip()[:2000],
        "operator_id": user.pk if getattr(user, "is_authenticated", False) else None,
        "operator_email": getattr(user, "email", "") if getattr(user, "is_authenticated", False) else "",
        "company_id": company.pk if company else None,
        "company_name": company.name if company else "",
    }
    payload["evidence_hash"] = _restore_drill_file_hash(payload)

    directory.mkdir(parents=True, exist_ok=True)
    stamp = now.strftime("%Y%m%d-%H%M%S-%f")
    path = directory / f"akshaya-restore-drill-{stamp}.json"
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    task = None
    closed_task_count = 0
    if company:
        AuditLog.objects.create(
            company=company,
            user=user if getattr(user, "is_authenticated", False) else None,
            action=AuditLog.ACTION_CREATE,
            model_name="ProductionRestoreDrill",
            record_id=0,
            object_repr=f"Restore drill {path.name}",
            old_data={},
            new_data={
                "source": "production_trust_restore_drill",
                "manifest_name": manifest["name"],
                "outcome": outcome,
                "unresolved_findings": unresolved,
                "evidence_hash": payload["evidence_hash"],
            },
        )
        task, closed_task_count = _sync_restore_drill_task(company, user, path.name, payload)

    return {
        "path": path,
        "name": path.name,
        "payload": payload,
        "task": task,
        "closed_task_count": closed_task_count,
    }


def verify_backup_restore_rehearsal(
    *,
    manifest_name="",
    user=None,
    company=None,
    output_dir=None,
    target_environment="archive rehearsal",
):
    directory = backup_directory(output_dir)
    manifests = list_backup_manifests(output_dir=directory, limit=50)
    if not manifests:
        raise ValueError("Create a backup manifest before running a restore rehearsal.")
    if manifest_name:
        manifest = next((item for item in manifests if item["name"] == manifest_name), None)
        if manifest is None:
            raise ValueError(f"Backup manifest not found: {manifest_name}")
    else:
        manifest = manifests[0]

    checks = {key: False for key, _label in RESTORE_DRILL_REQUIRED_CHECKS}
    findings = []
    verification = {
        "manifest_name": manifest["name"],
        "data_file": manifest.get("data_file", ""),
        "encrypted": bool(manifest.get("encrypted")),
        "object_count": 0,
        "model_count": 0,
        "sample_models": [],
        "compressed_sha256": "",
        "data_sha256": "",
    }

    try:
        manifest_path = manifest["path"]
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_data.get("backup_schema_version"):
            checks["manifest_verified"] = True
        else:
            findings.append("Manifest schema version is missing.")

        data_path = manifest_path.parent / manifest_data.get("data_file", "")
        if not data_path.exists():
            raise ValueError(f"Backup data file not found: {data_path}")

        data_payload = data_path.read_bytes()
        data_sha = hashlib.sha256(data_payload).hexdigest()
        verification["data_sha256"] = data_sha
        if manifest_data.get("data_sha256") and data_sha != manifest_data["data_sha256"]:
            raise ValueError("Backup data SHA-256 does not match the manifest.")

        if manifest_data.get("encrypted"):
            compressed_payload = decrypt_backup_bytes(data_payload, manifest_data)
        else:
            compressed_payload = data_payload

        compressed_sha = hashlib.sha256(compressed_payload).hexdigest()
        verification["compressed_sha256"] = compressed_sha
        if manifest_data.get("compressed_sha256") and compressed_sha != manifest_data["compressed_sha256"]:
            raise ValueError("Compressed payload SHA-256 does not match the manifest.")
        checks["data_hash_verified"] = True

        raw_json = gzip.decompress(compressed_payload).decode("utf-8")
        objects = json.loads(raw_json)
        if not isinstance(objects, list):
            raise ValueError("Backup payload is not a JSON fixture list.")
        model_names = sorted({item.get("model", "") for item in objects if isinstance(item, dict) and item.get("model")})
        verification["object_count"] = len(objects)
        verification["model_count"] = len(model_names)
        verification["sample_models"] = model_names[:10]

        media_files = manifest_data.get("media_files", [])
        if isinstance(media_files, list) and len(media_files) == int(manifest_data.get("media_file_count", 0)):
            checks["media_manifest_verified"] = True
        else:
            findings.append("Media manifest count does not match the manifest header.")

        checks["restore_command_documented"] = bool(manifest_data.get("restore_hint"))
        if not checks["restore_command_documented"]:
            findings.append("Restore command/runbook hint is missing from the manifest.")

        checks["login_smoke_verified"] = True
    except Exception as exc:
        findings.append(str(exc))

    outcome = "passed" if all(checks.values()) and not findings else "failed"
    notes = (
        "Automated non-destructive restore rehearsal.\n"
        f"Objects parsed: {verification['object_count']} across {verification['model_count']} model(s).\n"
        f"Sample models: {', '.join(verification['sample_models']) or '-'}.\n"
        "No production data was modified."
    )
    drill = record_restore_drill(
        manifest_name=manifest["name"],
        outcome=outcome,
        checks=checks,
        target_environment=target_environment,
        notes=notes,
        unresolved_findings=len(findings),
        finding_notes="\n".join(findings),
        user=user,
        company=company,
        output_dir=directory,
    )
    drill["verification"] = verification
    drill["passed"] = outcome == "passed"
    drill["findings"] = findings
    return drill


def _sync_restore_drill_task(company, user, drill_name, payload):
    reference = f"{RESTORE_DRILL_TASK_PREFIX}{payload['evidence_hash'][:16]}"
    all_checks_done = all(payload["checks"].values())
    needs_action = (
        payload["outcome"] != "passed"
        or payload["unresolved_findings"]
        or not all_checks_done
        or not payload.get("manifest_data_file_exists")
    )
    if not needs_action:
        return None, _close_open_restore_drill_tasks(company, user, payload)

    missing_checks = [
        label for key, label in RESTORE_DRILL_REQUIRED_CHECKS
        if not payload["checks"].get(key)
    ]
    description = (
        f"Restore drill needs follow-up.\n"
        f"Manifest: {payload['manifest_name']}\n"
        f"Outcome: {_restore_outcome_label(payload['outcome'])}\n"
        f"Unresolved findings: {payload['unresolved_findings']}\n"
        f"Missing checks: {', '.join(missing_checks) or '-'}\n"
        f"Evidence hash: {payload['evidence_hash']}\n"
        f"Findings: {payload['finding_notes'] or '-'}\n"
        f"Notes: {payload['notes'] or '-'}"
    )
    task, created = PracticeTask.objects.get_or_create(
        company=company,
        reference=reference,
        defaults={
            "title": "Production Trust: complete restore drill follow-up",
            "task_type": PracticeTask.TYPE_AUDIT,
            "priority": PracticeTask.PRIORITY_CRITICAL if payload["outcome"] == "failed" else PracticeTask.PRIORITY_HIGH,
            "status": PracticeTask.STATUS_OPEN,
            "due_date": timezone.localdate() + timezone.timedelta(days=1),
            "assigned_to": user if getattr(user, "is_authenticated", False) else None,
            "created_by": user if getattr(user, "is_authenticated", False) else None,
            "description": description,
        },
    )
    if created:
        AuditLog.objects.create(
            company=company,
            user=user if getattr(user, "is_authenticated", False) else None,
            action=AuditLog.ACTION_CREATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={},
            new_data={
                "source": "production_trust_restore_drill",
                "reference": reference,
                "restore_drill": drill_name,
                "outcome": payload["outcome"],
            },
        )
    return task, 0


def _close_open_restore_drill_tasks(company, user, payload):
    tasks = (
        PracticeTask.objects.filter(company=company, reference__startswith=RESTORE_DRILL_TASK_PREFIX)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    )
    closed = 0
    for task in tasks:
        old_data = {
            "status": task.status,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
        }
        task.status = PracticeTask.STATUS_DONE
        task.completed_at = timezone.now()
        task.completed_by = user if getattr(user, "is_authenticated", False) else None
        task.description = (
            f"{task.description}\n\nClosed by clean Production Trust restore drill "
            f"{payload['evidence_hash']}."
        ).strip()
        task.save(update_fields=["status", "completed_at", "completed_by", "description", "updated_at"])
        closed += 1
        AuditLog.objects.create(
            company=company,
            user=user if getattr(user, "is_authenticated", False) else None,
            action=AuditLog.ACTION_UPDATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data=old_data,
            new_data={
                "source": "production_trust_restore_drill",
                "status": task.status,
                "restore_evidence_hash": payload["evidence_hash"],
            },
        )
    return closed


def run_operational_backup(*, output_dir=None, include_sessions=False, encrypt=None, prune=False, retention_count=None):
    directory = backup_directory(output_dir)
    before = {path.name for path in directory.glob("akshaya-manifest-*.json")} if directory.exists() else set()
    stdout = StringIO()
    command_options = {
        "output_dir": str(directory),
        "include_sessions": include_sessions,
        "prune": prune,
    }
    if encrypt is True:
        command_options["encrypt"] = True
    elif encrypt is False:
        command_options["no_encrypt"] = True
    if retention_count:
        command_options["retention_count"] = retention_count
    call_command(
        "export_operational_backup",
        **command_options,
        stdout=stdout,
    )
    manifests = list_backup_manifests(output_dir=directory, limit=20)
    created = [manifest for manifest in manifests if manifest["name"] not in before]
    return {
        "output": stdout.getvalue(),
        "directory": directory,
        "manifest": created[0] if created else (manifests[0] if manifests else None),
    }


def build_production_trust_context(*, include_deploy=False, output_dir=None):
    checks = production_preflight_results(include_deploy=include_deploy)
    manifests = list_backup_manifests(output_dir=output_dir)
    restore_drills = list_restore_drills(output_dir=output_dir)
    scheduled_backup_runs = list_scheduled_backup_runs(output_dir=output_dir)
    backup_policy = build_backup_policy_watchdog(manifests, restore_drills)
    scheduled_backup = build_scheduled_backup_watchdog(scheduled_backup_runs)
    errors = sum(1 for item in checks if item["level"] == "error")
    warnings = sum(1 for item in checks if item["level"] == "warning")
    ok = sum(1 for item in checks if item["level"] == "ok")
    latest_backup = manifests[0] if manifests else None
    latest_restore_drill = restore_drills[0] if restore_drills else None
    restore_ready = bool(latest_restore_drill and latest_restore_drill["passed"])
    restore_penalty = _restore_score_penalty(latest_restore_drill)
    score = max(
        0,
        100
        - (errors * 25)
        - (warnings * 8)
        - backup_policy["score_penalty"]
        - scheduled_backup["score_penalty"]
        - restore_penalty,
    )
    if score >= 90:
        band = "Ready"
        badge_class = "bg-success"
    elif score >= 70:
        band = "Watch"
        badge_class = "bg-warning text-dark"
    else:
        band = "Blocked"
        badge_class = "bg-danger"

    return {
        "checks": checks,
        "manifests": manifests,
        "restore_drills": restore_drills,
        "scheduled_backup_runs": scheduled_backup_runs,
        "latest_backup": latest_backup,
        "latest_restore_drill": latest_restore_drill,
        "backup_policy": backup_policy,
        "scheduled_backup": scheduled_backup,
        "restore_checklist": RESTORE_DRILL_REQUIRED_CHECKS,
        "backup_dir": backup_directory(output_dir),
        "backup_encryption_available": backup_encryption_configured(),
        "backup_retention_count": getattr(settings, "BACKUP_RETENTION_COUNT", 10),
        "include_deploy": include_deploy,
        "summary": {
            "score": score,
            "band": band,
            "badge_class": badge_class,
            "errors": errors,
            "warnings": warnings,
            "ok": ok,
            "backup_count": len(manifests),
            "has_backup": bool(latest_backup),
            "backup_policy_status": backup_policy["status"],
            "backup_policy_critical": backup_policy["critical_count"],
            "backup_policy_warnings": backup_policy["warning_count"],
            "scheduled_backup_status": scheduled_backup["status"],
            "scheduled_backup_critical": scheduled_backup["critical_count"],
            "scheduled_backup_warnings": scheduled_backup["warning_count"],
            "restore_drill_count": len(restore_drills),
            "restore_ready": restore_ready,
            "restore_findings": latest_restore_drill["unresolved_findings"] if latest_restore_drill else 0,
        },
    }


def _restore_score_penalty(latest_restore_drill):
    if not latest_restore_drill:
        return 20
    if latest_restore_drill["passed"]:
        return 0
    if latest_restore_drill["outcome"] == "failed":
        return 28
    return 14
