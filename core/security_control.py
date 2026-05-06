from django.conf import settings
from django.contrib.auth import get_user_model
from django.utils import timezone

from .models import AuditLog, PracticeTask, UserCompanyAccess


SECURITY_TASK_PREFIX = "SECURITYCTRL:"


def build_security_control(company):
    now = timezone.now()
    stale_cutoff = now - timezone.timedelta(days=90)
    accesses = list(
        UserCompanyAccess.objects.filter(company=company)
        .select_related("user")
        .order_by("role", "user__email")
    )
    active_accesses = [access for access in accesses if access.user.is_active]
    inactive_accesses = [access for access in accesses if not access.user.is_active]
    admin_accesses = [access for access in active_accesses if access.role == "Admin"]
    staff_accesses = [access for access in active_accesses if access.user.is_staff or access.user.is_superuser]
    dormant_accesses = [
        access for access in active_accesses
        if not access.user.last_login or access.user.last_login < stale_cutoff
    ]
    superusers = list(get_user_model().objects.filter(is_superuser=True, is_active=True).order_by("email"))
    issues = []

    def add_issue(code, severity, title, detail, recommendation, *, count=1, taskable=True):
        issues.append({
            "code": code,
            "severity": severity,
            "severity_rank": {"critical": 0, "warning": 1, "info": 2}.get(severity, 3),
            "badge_class": _security_badge_class(severity),
            "title": title,
            "detail": detail,
            "recommendation": recommendation,
            "count": count,
            "taskable": taskable,
            "reference": f"{SECURITY_TASK_PREFIX}{company.pk}:{code.upper()}",
        })

    if not admin_accesses:
        add_issue(
            "no_active_admin",
            "critical",
            "No active company admin",
            "This company has no active user with the Admin role.",
            "Assign at least one accountable company administrator.",
        )
    if len(admin_accesses) > 3:
        add_issue(
            "too_many_admins",
            "warning",
            "Too many company admins",
            f"{len(admin_accesses)} active users have Admin access.",
            "Keep Admin role limited to accountable owners and move routine users to Accountant or Viewer.",
            count=len(admin_accesses),
        )
    if inactive_accesses:
        add_issue(
            "inactive_users_have_access",
            "warning",
            "Inactive users still have company roles",
            f"{len(inactive_accesses)} inactive user(s) retain company access mappings.",
            "Remove stale company role mappings for inactive users.",
            count=len(inactive_accesses),
        )
    if dormant_accesses:
        add_issue(
            "dormant_access",
            "warning",
            "Dormant user access needs review",
            f"{len(dormant_accesses)} active user(s) have never logged in or have not logged in for 90+ days.",
            "Confirm whether these users still need access, then remove or downgrade stale roles.",
            count=len(dormant_accesses),
        )
    if staff_accesses and not getattr(settings, "REQUIRE_STAFF_MFA", False):
        add_issue(
            "staff_mfa_disabled",
            "critical",
            "Staff MFA is disabled",
            f"{len(staff_accesses)} staff/superuser account(s) can access this company without mandatory MFA.",
            "Set REQUIRE_STAFF_MFA=True before production or client rollout.",
            count=len(staff_accesses),
        )
    if getattr(settings, "ALLOW_PUBLIC_REGISTRATION", False):
        add_issue(
            "public_registration_enabled",
            "warning",
            "Public registration is enabled",
            "New users can self-register without an invite gate.",
            "Disable public registration or use controlled invite onboarding.",
        )
    if len(superusers) > 1:
        add_issue(
            "multiple_superusers",
            "warning",
            "Multiple active superusers",
            f"{len(superusers)} active superuser account(s) exist.",
            "Keep break-glass superuser access minimal and reviewed.",
            count=len(superusers),
        )
    if not AuditLog.objects.filter(company=company).exists():
        add_issue(
            "no_audit_activity",
            "info",
            "No audit activity recorded",
            "No immutable audit log entries exist for this company yet.",
            "Run normal operational workflows and confirm key actions are logged.",
            taskable=False,
        )

    critical_count = sum(1 for issue in issues if issue["severity"] == "critical")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    if critical_count:
        status = "Blocked"
        badge_class = "bg-danger"
        score = max(0, 100 - critical_count * 25 - warning_count * 8)
    elif warning_count:
        status = "Watch"
        badge_class = "bg-warning text-dark"
        score = max(0, 100 - warning_count * 8)
    else:
        status = "Ready"
        badge_class = "bg-success"
        score = 100

    issues.sort(key=lambda issue: (issue["severity_rank"], issue["code"]))
    return {
        "status": status,
        "badge_class": badge_class,
        "score": score,
        "issues": issues,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "info_count": sum(1 for issue in issues if issue["severity"] == "info"),
        "accesses": [_access_row(access, stale_cutoff) for access in accesses],
        "staff_accesses": [_access_row(access, stale_cutoff) for access in staff_accesses],
        "superusers": superusers,
        "summary": {
            "total_access": len(accesses),
            "active_access": len(active_accesses),
            "admins": len(admin_accesses),
            "inactive_access": len(inactive_accesses),
            "dormant_access": len(dormant_accesses),
            "staff_access": len(staff_accesses),
            "superusers": len(superusers),
            "mfa_required": bool(getattr(settings, "REQUIRE_STAFF_MFA", False)),
            "public_registration": bool(getattr(settings, "ALLOW_PUBLIC_REGISTRATION", False)),
        },
        "has_taskable_issues": any(issue["taskable"] and issue["severity"] in {"critical", "warning"} for issue in issues),
    }


def create_security_control_tasks(company, user, assessment):
    active_issues = [
        issue for issue in assessment.get("issues", [])
        if issue.get("taskable") and issue.get("severity") in {"critical", "warning"}
    ]
    active_refs = {issue["reference"] for issue in active_issues}
    created = 0
    updated = 0
    closed = 0

    for issue in active_issues:
        description = (
            f"{issue['detail']}\n\n"
            f"Recommendation: {issue['recommendation']}\n"
            f"Current security score: {assessment['score']}% ({assessment['status']})."
        )
        task, was_created = PracticeTask.objects.get_or_create(
            company=company,
            reference=issue["reference"],
            defaults={
                "title": f"Security Control: {issue['title']}",
                "task_type": PracticeTask.TYPE_AUDIT,
                "priority": PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": timezone.localdate() + timezone.timedelta(days=1 if issue["severity"] == "critical" else 3),
                "assigned_to": user if getattr(user, "is_authenticated", False) else None,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "description": description,
            },
        )
        if was_created:
            created += 1
            _audit_security_task(company, user, task, "create", issue, assessment)
        elif task.status in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED} or task.description != description:
            task.status = PracticeTask.STATUS_OPEN
            task.completed_at = None
            task.completed_by = None
            task.description = description
            task.priority = PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH
            task.save(update_fields=["status", "completed_at", "completed_by", "description", "priority", "updated_at"])
            updated += 1
            _audit_security_task(company, user, task, "update", issue, assessment)

    stale_tasks = (
        PracticeTask.objects.filter(company=company, reference__startswith=f"{SECURITY_TASK_PREFIX}{company.pk}:")
        .exclude(reference__in=active_refs)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    )
    for task in stale_tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_at = timezone.now()
        task.completed_by = user if getattr(user, "is_authenticated", False) else None
        task.description = f"{task.description}\n\nClosed because the Security Control issue is no longer active.".strip()
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
            new_data={"source": "security_control", "status": task.status},
        )
    return {"created": created, "updated": updated, "closed": closed}


def _access_row(access, stale_cutoff):
    user = access.user
    dormant = not user.last_login or user.last_login < stale_cutoff
    return {
        "id": access.pk,
        "email": user.email,
        "name": user.get_full_name(),
        "role": access.role,
        "is_active": user.is_active,
        "is_staff": user.is_staff,
        "is_superuser": user.is_superuser,
        "last_login": user.last_login,
        "date_joined": user.date_joined,
        "created_at": access.created_at,
        "dormant": dormant,
    }


def _security_badge_class(severity):
    if severity == "critical":
        return "bg-danger"
    if severity == "warning":
        return "bg-warning text-dark"
    return "bg-info text-dark"


def _audit_security_task(company, user, task, action, issue, assessment):
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if action == "create" else AuditLog.ACTION_UPDATE,
        model_name="PracticeTask",
        record_id=task.pk,
        object_repr=task.title[:200],
        old_data={},
        new_data={
            "source": "security_control",
            "reference": task.reference,
            "issue": issue["code"],
            "severity": issue["severity"],
            "security_status": assessment["status"],
            "security_score": assessment["score"],
        },
    )
