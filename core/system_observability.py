import shutil
import time
from pathlib import Path

from django.conf import settings
from django.core.cache import cache
from django.core.checks import ERROR, WARNING, run_checks
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.utils import timezone

from .production_trust import (
    build_backup_policy_watchdog,
    build_scheduled_backup_watchdog,
    list_backup_manifests,
    list_restore_drills,
    list_scheduled_backup_runs,
)
from .models import AuditLog, PracticeTask


OBSERVABILITY_TASK_PREFIX = "SYSOBS:"


def build_system_observability(*, company=None, include_deploy=False, as_of=None):
    generated_at = as_of or timezone.now()
    checks = []
    checks.extend(_django_system_checks(include_deploy=include_deploy))
    checks.append(_timed_check("Database", "Round trip", _database_round_trip, critical=True))
    checks.append(_timed_check("Database", "Pending migrations", _pending_migrations, critical=True))
    checks.append(_timed_check("Cache", "Read/write", _cache_round_trip, critical=False))
    checks.append(_timed_check("Storage", "Media writable", _media_writable, critical=True))
    checks.append(_disk_capacity_check())
    checks.append(_staticfiles_check())
    checks.append(_celery_configuration_check())
    checks.extend(_backup_evidence_checks())
    checks.append(_provider_retry_queue_check())

    critical_count = sum(1 for item in checks if item["level"] == "critical")
    warning_count = sum(1 for item in checks if item["level"] == "warning")
    ok_count = sum(1 for item in checks if item["level"] == "ok")
    info_count = sum(1 for item in checks if item["level"] == "info")
    score = max(0, 100 - critical_count * 18 - warning_count * 6)
    if critical_count:
        status = "critical"
        status_label = "Critical"
        badge_class = "bg-danger"
    elif warning_count:
        status = "degraded"
        status_label = "Degraded"
        badge_class = "bg-warning text-dark"
    else:
        status = "healthy"
        status_label = "Healthy"
        badge_class = "bg-success"

    checks.sort(key=lambda item: (
        {"critical": 0, "warning": 1, "ok": 2, "info": 3}.get(item["level"], 4),
        item["component"],
        item["name"],
    ))
    totals = {
        "critical": critical_count,
        "warning": warning_count,
        "ok": ok_count,
        "info": info_count,
        "check_count": len(checks),
        "total": len(checks),
    }
    taskable_checks = _taskable_checks(company, checks)
    return {
        "generated_at": generated_at,
        "as_of": generated_at,
        "include_deploy": include_deploy,
        "company": company,
        "status": status,
        "status_label": status_label,
        "badge_class": badge_class,
        "score": score,
        "checks": checks,
        "taskable_checks": taskable_checks,
        "has_taskable_checks": bool(taskable_checks),
        "summary": totals,
        "totals": totals,
        "runtime": {
            "debug": bool(settings.DEBUG),
            "database_engine": settings.DATABASES["default"].get("ENGINE", ""),
            "media_root": str(settings.MEDIA_ROOT),
            "static_root": str(settings.STATIC_ROOT),
            "celery_eager": bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)),
            "backup_schedule_enabled": bool(getattr(settings, "BACKUP_SCHEDULE_ENABLED", False)),
        },
    }


def create_system_observability_tasks(company, user, report):
    if not company:
        raise ValueError("Select a company before creating System Observability tasks.")

    active_checks = report.get("taskable_checks") or _taskable_checks(company, report.get("checks", []))
    active_refs = {item["reference"] for item in active_checks}
    created = 0
    updated = 0
    closed = 0

    for item in active_checks:
        priority = PracticeTask.PRIORITY_CRITICAL if item["level"] == "critical" else PracticeTask.PRIORITY_HIGH
        description = _task_description(item, report)
        task, was_created = PracticeTask.objects.get_or_create(
            company=company,
            reference=item["reference"],
            defaults={
                "title": f"System Observability: {item['component']} - {item['name']}",
                "task_type": PracticeTask.TYPE_AUDIT,
                "priority": priority,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": timezone.localdate() if item["level"] == "critical" else timezone.localdate() + timezone.timedelta(days=2),
                "assigned_to": user if getattr(user, "is_authenticated", False) else None,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "description": description,
            },
        )
        if was_created:
            created += 1
            _audit_observability_task(company, user, task, "create", item, report)
            continue

        changed = False
        if task.status in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED}:
            task.status = PracticeTask.STATUS_OPEN
            task.completed_at = None
            task.completed_by = None
            changed = True
        if task.priority != priority:
            task.priority = priority
            changed = True
        if task.description != description:
            task.description = description
            changed = True
        if changed:
            task.save(update_fields=["status", "completed_at", "completed_by", "priority", "description", "updated_at"])
            updated += 1
            _audit_observability_task(company, user, task, "update", item, report)

    stale_tasks = (
        PracticeTask.objects.filter(company=company, reference__startswith=f"{OBSERVABILITY_TASK_PREFIX}{company.pk}:")
        .exclude(reference__in=active_refs)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    )
    for task in stale_tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_at = timezone.now()
        task.completed_by = user if getattr(user, "is_authenticated", False) else None
        task.description = f"{task.description}\n\nClosed because the System Observability diagnostic is no longer active.".strip()
        task.save(update_fields=["status", "completed_at", "completed_by", "description", "updated_at"])
        closed += 1
        AuditLog.objects.create(
            company=company,
            user=user if getattr(user, "is_authenticated", False) else None,
            action=AuditLog.ACTION_UPDATE,
            model_name="PracticeTask",
            record_id=task.pk,
            object_repr=task.title[:200],
            old_data={"status": old_status},
            new_data={"source": "system_observability", "status": task.status},
        )

    return {"created": created, "updated": updated, "closed": closed}


def observability_public_payload(observability):
    return {
        "ok": observability["status"] != "critical",
        "status": observability["status"],
        "status_label": observability["status_label"],
        "score": observability["score"],
        "generated_at": observability["generated_at"].isoformat(),
        "as_of": observability["as_of"].isoformat(),
        "summary": observability["summary"],
        "totals": observability["totals"],
        "checks": [
            {
                "component": item["component"],
                "name": item["name"],
                "level": item["level"],
                "badge_class": item["badge_class"],
                "message": item["message"],
                "hint": item["hint"],
                "duration_ms": item.get("duration_ms"),
                "metrics": item.get("metrics", {}),
            }
            for item in observability["checks"]
        ],
    }


def _taskable_checks(company, checks):
    if not company:
        return []
    return [
        {
            **item,
            "reference": f"{OBSERVABILITY_TASK_PREFIX}{company.pk}:{_check_code(item)}",
        }
        for item in checks
        if item.get("level") in {"critical", "warning"}
    ]


def _check_code(item):
    raw = f"{item.get('component', '')}:{item.get('name', '')}".lower()
    return "".join(ch if ch.isalnum() or ch in ("_", "-") else "_" for ch in raw)[:70]


def _task_description(item, report):
    hint = item.get("hint") or "Review and resolve this production diagnostic before go-live."
    return (
        f"Component: {item['component']}\n"
        f"Check: {item['name']}\n"
        f"Severity: {item['level'].title()}\n"
        f"Result: {item['message']}\n\n"
        f"Next action: {hint}\n"
        f"System Observability score: {report.get('score', 0)}% ({report.get('status_label', report.get('status', 'Unknown'))})."
    )


def _audit_observability_task(company, user, task, action, item, report):
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if action == "create" else AuditLog.ACTION_UPDATE,
        model_name="PracticeTask",
        record_id=task.pk,
        object_repr=task.title[:200],
        old_data={},
        new_data={
            "source": "system_observability",
            "component": item["component"],
            "check": item["name"],
            "severity": item["level"],
            "score": report.get("score"),
            "reference": task.reference,
        },
    )


def _django_system_checks(*, include_deploy=False):
    checks = []
    issues = run_checks(include_deployment_checks=include_deploy)
    for issue in issues:
        level = "critical" if issue.level >= ERROR else "warning" if issue.level >= WARNING else "info"
        checks.append(_check(
            "Django",
            issue.id or "system_check",
            level,
            issue.msg,
            issue.hint or "",
        ))
    if not checks:
        checks.append(_check("Django", "System checks", "ok", "Django system checks passed.", ""))
    return checks


def _timed_check(component, name, callback, *, critical):
    start = time.perf_counter()
    try:
        message, hint, metrics = callback()
    except Exception as exc:
        return _check(
            component,
            name,
            "critical" if critical else "warning",
            str(exc),
            "Investigate this dependency before production rollout.",
            duration_ms=_elapsed_ms(start),
        )
    item = _check(component, name, "ok", message, hint, duration_ms=_elapsed_ms(start))
    item["metrics"] = metrics
    return item


def _database_round_trip():
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
        cursor.fetchone()
    return "Database round trip succeeded.", "", {}


def _pending_migrations():
    executor = MigrationExecutor(connection)
    plan = executor.migration_plan(executor.loader.graph.leaf_nodes())
    if plan:
        names = [f"{migration.app_label}.{migration.name}" for migration, _backwards in plan[:5]]
        raise RuntimeError(f"{len(plan)} pending migration(s): {', '.join(names)}")
    return "No pending migrations.", "", {"pending": 0}


def _cache_round_trip():
    key = "system_observability_cache"
    value = f"ok-{time.time_ns()}"
    cache.set(key, value, timeout=30)
    if cache.get(key) != value:
        raise RuntimeError("Cache read did not return the written value.")
    return "Cache read/write succeeded.", "", {}


def _media_writable():
    media_root = Path(settings.MEDIA_ROOT)
    media_root.mkdir(parents=True, exist_ok=True)
    cleanup_error = ""
    probe = media_root / "observability-probe.tmp"
    try:
        with probe.open("w", encoding="utf-8") as handle:
            handle.write("ok")
        if probe.read_text(encoding="utf-8") != "ok":
            raise RuntimeError("MEDIA_ROOT probe file could not be read back.")
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError as exc:
            cleanup_error = str(exc)
    message = "MEDIA_ROOT is writable."
    hint = "Probe cleanup was deferred by the filesystem." if cleanup_error else ""
    metrics = {"path": str(media_root), "cleanup_error": cleanup_error}
    return message, hint, metrics


def _disk_capacity_check():
    usage = shutil.disk_usage(settings.BASE_DIR)
    free_percent = round((usage.free / usage.total) * 100, 2) if usage.total else 0
    if free_percent < 5:
        level = "critical"
        message = f"Disk free space is critically low: {free_percent}%."
        hint = "Increase disk/storage capacity before uploads, backups, or imports fail."
    elif free_percent < 10:
        level = "warning"
        message = f"Disk free space is low: {free_percent}%."
        hint = "Plan storage cleanup or capacity expansion."
    else:
        level = "ok"
        message = f"Disk free space is {free_percent}%."
        hint = ""
    item = _check("Storage", "Disk capacity", level, message, hint)
    item["metrics"] = {
        "total": usage.total,
        "used": usage.used,
        "free": usage.free,
        "free_percent": free_percent,
    }
    return item


def _staticfiles_check():
    static_root = Path(settings.STATIC_ROOT)
    if settings.DEBUG:
        return _check("Static Files", "Static root", "ok", "DEBUG mode uses local static files.", "")
    if static_root.exists() and any(static_root.iterdir()):
        return _check("Static Files", "Static root", "ok", "STATIC_ROOT contains collected files.", "")
    return _check(
        "Static Files",
        "Static root",
        "warning",
        "STATIC_ROOT is empty or missing.",
        "Run collectstatic during deployment.",
    )


def _celery_configuration_check():
    broker = getattr(settings, "CELERY_BROKER_URL", "")
    if getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False):
        return _check(
            "Workers",
            "Celery mode",
            "warning",
            "Celery tasks are configured to run eagerly.",
            "Disable CELERY_TASK_ALWAYS_EAGER for production workers.",
        )
    if not broker:
        return _check("Workers", "Celery broker", "warning", "CELERY_BROKER_URL is empty.", "Configure Redis or the production broker.")
    if "localhost" in broker or "127.0.0.1" in broker:
        return _check(
            "Workers",
            "Celery broker",
            "warning",
            "Celery broker points to localhost.",
            "Use a managed Redis/broker URL in production.",
        )
    return _check("Workers", "Celery broker", "ok", "Celery broker is configured.", "")


def _backup_evidence_checks():
    checks = []
    try:
        manifests = list_backup_manifests(limit=20)
        restore_drills = list_restore_drills(limit=20)
        scheduled_runs = list_scheduled_backup_runs(limit=20)
        backup_policy = build_backup_policy_watchdog(manifests, restore_drills)
        scheduled_policy = build_scheduled_backup_watchdog(scheduled_runs)
    except Exception as exc:
        checks.append(_check(
            "Backups",
            "Backup evidence",
            "critical",
            f"Backup evidence could not be inspected: {exc}",
            "Open Production Trust and verify backup evidence paths and permissions.",
        ))
        return checks
    checks.append(_policy_check("Backups", "Backup policy", backup_policy))
    checks.append(_policy_check("Backups", "Scheduled/offsite policy", scheduled_policy))
    return checks


def _policy_check(component, name, policy):
    if policy["critical_count"]:
        level = "critical"
    elif policy["warning_count"]:
        level = "warning"
    else:
        level = "ok"
    issue = policy["issues"][0] if policy.get("issues") else None
    message = issue["title"] if issue else f"{name} is clean."
    hint = issue["recommendation"] if issue else ""
    item = _check(component, name, level, message, hint)
    item["metrics"] = {
        "score": policy.get("score"),
        "critical": policy.get("critical_count"),
        "warning": policy.get("warning_count"),
        "status": policy.get("status"),
    }
    return item


def _provider_retry_queue_check():
    try:
        from integrations.models import IntegrationRetryJob
    except Exception as exc:
        return _check("Integrations", "Provider retry queue", "warning", str(exc), "Check integrations app availability.")

    open_statuses = [
        IntegrationRetryJob.STATUS_PENDING,
        IntegrationRetryJob.STATUS_IN_PROGRESS,
        IntegrationRetryJob.STATUS_FAILED,
    ]
    now = timezone.now()
    try:
        open_jobs = IntegrationRetryJob.objects.filter(status__in=open_statuses)
        open_count = open_jobs.count()
        due_count = open_jobs.filter(next_attempt_at__lte=now).count()
        failed_count = open_jobs.filter(status=IntegrationRetryJob.STATUS_FAILED).count()
    except Exception as exc:
        return _check(
            "Integrations",
            "Provider retry queue",
            "critical",
            f"Provider retry queue could not be inspected: {exc}",
            "Apply integration migrations and re-run diagnostics.",
        )
    if failed_count or due_count >= 10:
        level = "critical"
        message = f"{open_count} open retry job(s), {due_count} due, {failed_count} failed."
        hint = "Open Provider Go-Live Readiness and process or resolve retry jobs."
    elif open_count:
        level = "warning"
        message = f"{open_count} provider retry job(s) are open."
        hint = "Monitor the provider retry queue."
    else:
        level = "ok"
        message = "No open provider retry jobs."
        hint = ""
    item = _check("Integrations", "Provider retry queue", level, message, hint)
    item["metrics"] = {"open": open_count, "due": due_count, "failed": failed_count}
    return item


def _check(component, name, level, message, hint="", *, duration_ms=None):
    return {
        "component": component,
        "name": name,
        "level": level,
        "badge_class": {
            "critical": "bg-danger",
            "warning": "bg-warning text-dark",
            "ok": "bg-success",
            "info": "bg-info text-dark",
        }.get(level, "bg-secondary"),
        "message": message,
        "hint": hint,
        "duration_ms": duration_ms,
        "metrics": {},
    }


def _elapsed_ms(start):
    return round((time.perf_counter() - start) * 1000, 2)
