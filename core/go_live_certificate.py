import hashlib
import json

from django.conf import settings
from django.utils import timezone

from .models import AuditLog, PracticeTask
from .system_observability import build_system_observability


GO_LIVE_TASK_PREFIX = "GOLIVE:"


GATE_DEFINITIONS = [
    ("database_round_trip", "Runtime", "Database Round Trip", "Database", "Round trip"),
    ("pending_migrations", "Runtime", "Pending Migrations", "Database", "Pending migrations"),
    ("django_checks", "Deploy", "Django System Checks", "Django", None),
    ("cache", "Runtime", "Cache Read/Write", "Cache", "Read/write"),
    ("media", "Storage", "Media Storage", "Storage", "Media writable"),
    ("disk", "Storage", "Disk Capacity", "Storage", "Disk capacity"),
    ("staticfiles", "Deploy", "Static Files", "Static Files", "Static root"),
    ("workers", "Workers", "Worker/Broker Configuration", "Workers", None),
    ("backup_policy", "Recovery", "Backup Policy", "Backups", "Backup policy"),
    ("scheduled_backup", "Recovery", "Scheduled/Offsite Backups", "Backups", "Scheduled/offsite policy"),
    ("provider_retries", "Integrations", "Provider Retry Queue", "Integrations", "Provider retry queue"),
]


def build_go_live_certificate(*, company=None, include_deploy=True, as_of=None):
    as_of = as_of or timezone.now()
    observability = build_system_observability(
        company=company,
        include_deploy=include_deploy,
        as_of=as_of,
    )
    gates = [_build_gate(observability["checks"], definition) for definition in GATE_DEFINITIONS]
    blockers = [gate for gate in gates if gate["status"] == "blocked"]
    warnings = [gate for gate in gates if gate["status"] == "watch"]
    score = max(0, 100 - len(blockers) * 15 - len(warnings) * 5)

    if blockers:
        status = "blocked"
        status_label = "Blocked"
        badge_class = "bg-danger"
        summary = f"{len(blockers)} blocker(s) must be resolved before go-live."
    elif warnings:
        status = "conditional"
        status_label = "Conditional"
        badge_class = "bg-warning text-dark"
        summary = f"{len(warnings)} warning(s) remain; go-live needs explicit sign-off."
    else:
        status = "certified"
        status_label = "Certified"
        badge_class = "bg-success"
        summary = "All mandatory runtime, deployment, recovery, and integration gates are clean."

    certificate = {
        "generated_at": as_of,
        "include_deploy": include_deploy,
        "company": company,
        "status": status,
        "status_label": status_label,
        "badge_class": badge_class,
        "score": score,
        "summary": summary,
        "gates": gates,
        "blockers": blockers,
        "warnings": warnings,
        "totals": {
            "gates": len(gates),
            "ready": sum(1 for gate in gates if gate["status"] == "ready"),
            "watch": len(warnings),
            "blocked": len(blockers),
        },
        "observability": {
            "status": observability["status"],
            "status_label": observability["status_label"],
            "score": observability["score"],
            "totals": observability["totals"],
        },
    }
    certificate["certificate_id"] = _certificate_id(certificate)
    certificate["remediation_pack"] = build_deployment_remediation_pack(certificate)
    return certificate


def create_go_live_remediation_tasks(company, user, certificate):
    if not company:
        raise ValueError("Select a company before creating Go-Live remediation tasks.")

    active_gates = [gate for gate in certificate.get("gates", []) if gate["status"] in {"blocked", "watch"}]
    active_refs = {f"{GO_LIVE_TASK_PREFIX}{company.pk}:{gate['code']}" for gate in active_gates}
    created = 0
    updated = 0
    closed = 0

    for gate in active_gates:
        reference = f"{GO_LIVE_TASK_PREFIX}{company.pk}:{gate['code']}"
        priority = PracticeTask.PRIORITY_CRITICAL if gate["status"] == "blocked" else PracticeTask.PRIORITY_HIGH
        description = _go_live_task_description(gate, certificate)
        task, was_created = PracticeTask.objects.get_or_create(
            company=company,
            reference=reference,
            defaults={
                "title": f"Go-Live: {gate['name']}",
                "task_type": PracticeTask.TYPE_AUDIT,
                "priority": priority,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": timezone.localdate() if gate["status"] == "blocked" else timezone.localdate() + timezone.timedelta(days=2),
                "assigned_to": user if getattr(user, "is_authenticated", False) else None,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "description": description,
            },
        )
        if was_created:
            created += 1
            _audit_go_live_task(company, user, task, "create", gate, certificate)
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
            _audit_go_live_task(company, user, task, "update", gate, certificate)

    stale_tasks = (
        PracticeTask.objects.filter(company=company, reference__startswith=f"{GO_LIVE_TASK_PREFIX}{company.pk}:")
        .exclude(reference__in=active_refs)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    )
    for task in stale_tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_at = timezone.now()
        task.completed_by = user if getattr(user, "is_authenticated", False) else None
        task.description = f"{task.description}\n\nClosed because the Go-Live Certificate gate is now ready.".strip()
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
            new_data={"source": "go_live_certificate", "status": task.status},
        )

    return {"created": created, "updated": updated, "closed": closed}


def build_deployment_remediation_pack(certificate=None):
    return {
        "environment": _environment_items(),
        "commands": _command_items(),
        "certificate_id": certificate.get("certificate_id") if certificate else "",
    }


def go_live_certificate_payload(certificate):
    return {
        "ok": certificate["status"] == "certified",
        "can_go_live": certificate["status"] in {"certified", "conditional"},
        "certificate_id": certificate["certificate_id"],
        "generated_at": certificate["generated_at"].isoformat(),
        "include_deploy": certificate["include_deploy"],
        "company": {
            "id": certificate["company"].pk,
            "name": certificate["company"].name,
        } if certificate.get("company") else None,
        "status": certificate["status"],
        "status_label": certificate["status_label"],
        "score": certificate["score"],
        "summary": certificate["summary"],
        "totals": certificate["totals"],
        "gates": certificate["gates"],
        "observability": certificate["observability"],
        "remediation_pack": certificate["remediation_pack"],
    }


def _build_gate(checks, definition):
    code, area, gate_name, component, check_name = definition
    matches = [
        check for check in checks
        if check.get("component") == component and (check_name is None or check.get("name") == check_name)
    ]
    if not matches:
        return {
            "code": code,
            "area": area,
            "name": gate_name,
            "status": "blocked",
            "status_label": "Blocked",
            "badge_class": "bg-danger",
            "message": "No diagnostic evidence was produced for this gate.",
            "recommendation": "Run System Observability and confirm this dependency is instrumented.",
            "evidence": f"{component} / {check_name or 'all'}",
        }

    worst = sorted(matches, key=lambda item: _level_rank(item.get("level")))[0]
    level = worst.get("level")
    if level == "critical":
        status = "blocked"
        status_label = "Blocked"
        badge_class = "bg-danger"
    elif level in {"warning", "info"}:
        status = "watch"
        status_label = "Watch"
        badge_class = "bg-warning text-dark"
    else:
        status = "ready"
        status_label = "Ready"
        badge_class = "bg-success"

    return {
        "code": code,
        "area": area,
        "name": gate_name,
        "status": status,
        "status_label": status_label,
        "badge_class": badge_class,
        "message": worst.get("message", ""),
        "recommendation": _recommendation(worst, status),
        "evidence": f"{worst.get('component')} / {worst.get('name')}",
    }


def _recommendation(check, status):
    if check.get("hint"):
        return check["hint"]
    if status == "blocked":
        return "Resolve this gate before production go-live."
    if status == "watch":
        return "Review and explicitly sign off before go-live."
    return "No action required."


def _go_live_task_description(gate, certificate):
    return (
        f"Certificate: {certificate['certificate_id']}\n"
        f"Area: {gate['area']}\n"
        f"Gate: {gate['name']}\n"
        f"Status: {gate['status_label']}\n"
        f"Evidence: {gate['evidence']}\n"
        f"Result: {gate['message']}\n\n"
        f"Required action: {gate['recommendation']}\n"
        f"Go-live score: {certificate['score']}% ({certificate['status_label']})."
    )


def _audit_go_live_task(company, user, task, action, gate, certificate):
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if action == "create" else AuditLog.ACTION_UPDATE,
        model_name="PracticeTask",
        record_id=task.pk,
        object_repr=task.title[:200],
        old_data={},
        new_data={
            "source": "go_live_certificate",
            "certificate_id": certificate["certificate_id"],
            "gate": gate["code"],
            "status": gate["status"],
            "score": certificate["score"],
            "reference": task.reference,
        },
    )


def _environment_items():
    return [
        _env_item("DEBUG", "False", "Deploy", "debug_off", not bool(settings.DEBUG), "Disable verbose errors and development behavior."),
        _env_item("ALLOWED_HOSTS", "app domain(s)", "Deploy", "hosts", _has_nonlocal_host(), "Restrict requests to production domain names."),
        _env_item("CSRF_TRUSTED_ORIGINS", "https://app-domain", "Deploy", "csrf", bool(getattr(settings, "CSRF_TRUSTED_ORIGINS", [])), "Allow secure form posts from the production origin."),
        _env_item("DATABASE_URL", "managed PostgreSQL URL", "Runtime", "database", _database_is_managed(), "Move accounting data off local SQLite."),
        _env_item("CELERY_BROKER_URL", "managed Redis URL", "Workers", "broker", _broker_is_managed(), "Run background work through production Redis."),
        _env_item("CELERY_RESULT_BACKEND", "managed Redis URL", "Workers", "backend", _backend_is_managed(), "Store task results outside the web process."),
        _env_item("CELERY_TASK_ALWAYS_EAGER", "False", "Workers", "eager", not bool(getattr(settings, "CELERY_TASK_ALWAYS_EAGER", False)), "Do not execute production jobs inside web requests."),
        _env_item("BACKUP_ENCRYPTION_KEY or BACKUP_ENCRYPTION_PASSPHRASE", "set securely", "Recovery", "backup_key", _backup_secret_configured(), "Encrypt operational backup archives."),
        _env_item("BACKUP_ENCRYPTION_REQUIRED", "True", "Recovery", "backup_required", bool(getattr(settings, "BACKUP_ENCRYPTION_REQUIRED", False)), "Block unencrypted backup evidence."),
        _env_item("BACKUP_ENCRYPTION_DEFAULT", "True", "Recovery", "backup_default", bool(getattr(settings, "BACKUP_ENCRYPTION_DEFAULT", False)), "Make encrypted backups the default path."),
        _env_item("BACKUP_SCHEDULE_ENABLED", "True", "Recovery", "backup_schedule", bool(getattr(settings, "BACKUP_SCHEDULE_ENABLED", False)), "Enable automated backup cadence."),
        _env_item("BACKUP_OFFSITE_DIR", "persistent offsite mount/path", "Recovery", "offsite_dir", bool(getattr(settings, "BACKUP_OFFSITE_DIR", "")), "Keep recovery evidence outside the app working directory."),
        _env_item("BACKUP_OFFSITE_REQUIRED", "True", "Recovery", "offsite_required", bool(getattr(settings, "BACKUP_OFFSITE_REQUIRED", False)), "Treat missing offsite evidence as a go-live issue."),
        _env_item("SECURE_SSL_REDIRECT", "True", "Security", "ssl_redirect", _secure_setting_enabled("SECURE_SSL_REDIRECT"), "Redirect traffic to HTTPS."),
        _env_item("SECURE_HSTS_PRELOAD", "True after domain validation", "Security", "hsts_preload", _secure_setting_enabled("SECURE_HSTS_PRELOAD"), "Prepare browser-side HTTPS enforcement after validation."),
    ]


def _env_item(name, target, area, code, ready, action):
    return {
        "name": name,
        "target": target,
        "area": area,
        "code": code,
        "ready": bool(ready),
        "status": "ready" if ready else "needs_action",
        "badge_class": "bg-success" if ready else "bg-warning text-dark",
        "action": action,
    }


def _command_items():
    return [
        {"name": "Apply migrations", "command": "python manage.py migrate", "area": "Runtime"},
        {"name": "Collect static files", "command": "python manage.py collectstatic --noinput", "area": "Deploy"},
        {"name": "Run backup drill", "command": "python manage.py export_operational_backup --prune", "area": "Recovery"},
        {"name": "Verify restore rehearsal", "command": "python manage.py verify_restore_rehearsal --fail-on-finding", "area": "Recovery"},
        {"name": "Run scheduled backup evidence", "command": "python manage.py run_scheduled_operational_backup", "area": "Recovery"},
        {"name": "Generate certificate", "command": "python manage.py go_live_certificate --fail-on-blocker", "area": "Certificate"},
        {"name": "Generate evidence pack", "command": "python manage.py go_live_evidence_pack --company-id <id>", "area": "Certificate"},
        {"name": "Run live smoke", "command": "python manage.py live_smoke_check --json", "area": "Certificate"},
    ]


def _has_nonlocal_host():
    hosts = getattr(settings, "ALLOWED_HOSTS", [])
    return any(host and host not in {"127.0.0.1", "localhost", "*"} for host in hosts)


def _database_is_managed():
    engine = settings.DATABASES["default"].get("ENGINE", "")
    name = str(settings.DATABASES["default"].get("NAME", ""))
    return "postgresql" in engine and "sqlite" not in name.lower()


def _broker_is_managed():
    return _url_is_managed(getattr(settings, "CELERY_BROKER_URL", ""))


def _backend_is_managed():
    return _url_is_managed(getattr(settings, "CELERY_RESULT_BACKEND", ""))


def _url_is_managed(value):
    value = (value or "").lower()
    if not value:
        return False
    return "localhost" not in value and "127.0.0.1" not in value


def _backup_secret_configured():
    return bool(getattr(settings, "BACKUP_ENCRYPTION_KEY", "") or getattr(settings, "BACKUP_ENCRYPTION_PASSPHRASE", ""))


def _secure_setting_enabled(name):
    return bool(getattr(settings, name, False))


def _level_rank(level):
    return {"critical": 0, "warning": 1, "info": 2, "ok": 3}.get(level, 4)


def _certificate_id(certificate):
    payload = {
        "company": certificate["company"].pk if certificate.get("company") else None,
        "status": certificate["status"],
        "score": certificate["score"],
        "gates": [
            {
                "code": gate["code"],
                "status": gate["status"],
                "message": gate["message"],
            }
            for gate in certificate["gates"]
        ],
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return f"GLC-{digest[:16].upper()}"
