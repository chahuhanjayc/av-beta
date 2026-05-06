from django.conf import settings
from django.utils import timezone

from core.models import AuditLog, PracticeTask

from .models import IntegrationConnector, IntegrationRequestLog


PRODUCTION_EVIDENCE_FIELDS = (
    {
        "key": "provider_contract_reference",
        "label": "Provider Contract Ref",
        "level": "critical",
        "fix": "Record the GSP/provider contract, onboarding, or production account reference.",
    },
    {
        "key": "uat_evidence_reference",
        "label": "UAT Evidence Ref",
        "level": "critical",
        "fix": "Record the sandbox/UAT evidence pack or provider test ticket reference.",
    },
    {
        "key": "production_approval_reference",
        "label": "Production Approval Ref",
        "level": "critical",
        "fix": "Record the provider production approval, API enablement, or go-live ticket reference.",
    },
    {
        "key": "ip_whitelist_reference",
        "label": "IP/Webhook Whitelist Ref",
        "level": "warning",
        "fix": "Record whitelisted IP, webhook callback, or network allow-list evidence.",
    },
    {
        "key": "fallback_sop_reference",
        "label": "Fallback SOP Ref",
        "level": "warning",
        "fix": "Record the manual portal fallback SOP for outage or provider downtime.",
    },
    {
        "key": "support_escalation_contact",
        "label": "Support Escalation",
        "level": "warning",
        "fix": "Record the provider escalation email, ticket queue, or account manager contact.",
    },
)


CONNECTOR_CATALOG = [
    {
        "type": IntegrationConnector.TYPE_GST,
        "name": "GST Portal",
        "purpose": "GST return JSON, GSTIN lookup, notices, and filing evidence.",
        "required": ("gstin", "provider_name", "credential_reference"),
    },
    {
        "type": IntegrationConnector.TYPE_IRP,
        "name": "IRP / E-Invoice",
        "purpose": "IRN generation, signed invoice JSON, signed QR, cancellation/status evidence.",
        "required": ("gstin", "provider_name", "credential_reference"),
    },
    {
        "type": IntegrationConnector.TYPE_EWAY,
        "name": "E-Way Bill",
        "purpose": "E-way bill generation, validity tracking, and transport compliance evidence.",
        "required": ("gstin", "provider_name", "credential_reference"),
    },
    {
        "type": IntegrationConnector.TYPE_TRACES,
        "name": "TRACES",
        "purpose": "TDS/TCS challan, Form 16/16A, conso, justification and notice workflows.",
        "required": ("tan", "username", "credential_reference"),
    },
    {
        "type": IntegrationConnector.TYPE_TALLY,
        "name": "Tally Sync",
        "purpose": "Deeper import/sync from Tally exports with duplicate and period controls.",
        "required": ("provider_name",),
        "stale_days": 14,
    },
    {
        "type": IntegrationConnector.TYPE_BANK,
        "name": "Connected Banking",
        "purpose": "Bank feed import, statement sync, and reconciliation automation.",
        "required": ("provider_name", "credential_reference"),
        "stale_days": 2,
    },
]


CONNECTOR_SERVICE_MAP = {
    IntegrationConnector.TYPE_GST: IntegrationRequestLog.SERVICE_GST_RETURN,
    IntegrationConnector.TYPE_IRP: IntegrationRequestLog.SERVICE_E_INVOICE,
    IntegrationConnector.TYPE_EWAY: IntegrationRequestLog.SERVICE_E_WAY_BILL,
    IntegrationConnector.TYPE_TRACES: IntegrationRequestLog.SERVICE_TRACES,
    IntegrationConnector.TYPE_TALLY: IntegrationRequestLog.SERVICE_TALLY_SYNC,
    IntegrationConnector.TYPE_BANK: IntegrationRequestLog.SERVICE_BANK_FEED,
}


def build_gst_certification_readiness(company=None):
    checks = [
        _manual(
            "IRP/GSP onboarding",
            "Complete taxpayer/API-integrator or GSP onboarding outside Akshaya Vistara.",
            "NIC/IRP access requires registered GSTINs, API credentials, and partner/API access approval.",
        ),
        _check(
            "GST provider selected",
            bool(settings.GST_API_PROVIDER),
            "Provider is selected.",
            "Set GST_API_PROVIDER to mock for local sandbox simulation or a real provider key for sandbox/production.",
        ),
        _check(
            "Production provider",
            settings.GST_API_SANDBOX_MODE or settings.GST_API_PROVIDER != "mock",
            "Production mode is not using the mock provider.",
            "GST_API_PROVIDER=mock is only acceptable in sandbox/local testing.",
        ),
        _check(
            "Base URL",
            settings.GST_API_PROVIDER == "mock" or bool(settings.GST_API_BASE_URL),
            "GST API base URL is configured.",
            "Set GST_API_BASE_URL for the chosen GSP/IRP adapter.",
        ),
        _check(
            "API key and secret",
            settings.GST_API_PROVIDER == "mock" or (bool(settings.GST_API_KEY) and bool(settings.GST_API_SECRET)),
            "API key and secret are configured.",
            "Set GST_API_KEY and GST_API_SECRET from the provider console.",
        ),
        _check(
            "Taxpayer credentials",
            settings.GST_API_PROVIDER == "mock" or (bool(settings.GST_API_USERNAME) and bool(settings.GST_API_PASSWORD)),
            "Taxpayer/API user credentials are configured.",
            "Set GST_API_USERNAME and GST_API_PASSWORD for taxpayer authentication where the provider requires it.",
        ),
        _check(
            "Taxpayer GSTIN",
            bool(settings.GST_API_TAXPAYER_GSTIN) or settings.GST_API_SANDBOX_MODE,
            "Taxpayer GSTIN is configured or sandbox mode is active.",
            "Set GST_API_TAXPAYER_GSTIN before production API use.",
        ),
        _check(
            "E-invoice feature flag",
            settings.E_INVOICE_ENABLED,
            "E-invoice generation is enabled.",
            "Set E_INVOICE_ENABLED=True after credentials are ready.",
            warning=True,
        ),
        _check(
            "E-way bill feature flag",
            settings.E_WAY_BILL_ENABLED,
            "E-way bill generation is enabled.",
            "Set E_WAY_BILL_ENABLED=True after credentials are ready.",
            warning=True,
        ),
        _ok(
            "Signed artifact storage",
            "IRN, acknowledgement, signed invoice JSON, signed QR, e-way bill status, and validity fields are available on vouchers.",
        ),
        _ok(
            "Local payload validators",
            "E-invoice, e-way bill, and GSTR-1 portal JSON payload checks are available before provider submission.",
        ),
    ]

    if company:
        company_gstin = (company.gstin or "").strip().upper()
        taxpayer_gstin = (settings.GST_API_TAXPAYER_GSTIN or company_gstin).strip().upper()
        checks.append(_check(
            "Selected company GSTIN",
            bool(company_gstin),
            "Selected company has a GSTIN.",
            "Add GSTIN in Company Settings before GST API testing.",
        ))
        checks.append(_check(
            "Taxpayer GSTIN match",
            not settings.GST_API_TAXPAYER_GSTIN or taxpayer_gstin == company_gstin,
            "Configured taxpayer GSTIN matches the selected company.",
            "GST_API_TAXPAYER_GSTIN should match the company being used for provider calls.",
            warning=True,
        ))
        checks.extend(_recent_success_checks(company))

    level_order = {"error": 3, "warning": 2, "manual": 1, "ok": 0}
    worst = max(checks, key=lambda item: level_order[item["level"]])["level"]
    errors = sum(1 for item in checks if item["level"] == "error")
    warnings = sum(1 for item in checks if item["level"] == "warning")
    manual = sum(1 for item in checks if item["level"] == "manual")
    sandbox_ready = errors == 0 and bool(settings.GST_API_PROVIDER)
    production_ready = sandbox_ready and not settings.GST_API_SANDBOX_MODE and manual == 0 and warnings == 0

    return {
        "level": worst,
        "checks": checks,
        "errors": errors,
        "warnings": warnings,
        "manual": manual,
        "sandbox_ready": sandbox_ready,
        "production_ready": production_ready,
        "summary": _summary(errors, warnings, manual, sandbox_ready, production_ready),
    }


def build_connector_control_plane(company=None):
    if not company:
        return {
            "connectors": [],
            "ready_count": 0,
            "blocked_count": 0,
            "summary": "Select a company to configure regulatory and sync connectors.",
        }

    existing = {
        connector.connector_type: connector
        for connector in IntegrationConnector.objects.filter(company=company)
    }
    cards = []
    for item in CONNECTOR_CATALOG:
        connector = existing.get(item["type"])
        missing_fields = []
        if connector:
            for field_name in item["required"]:
                if not getattr(connector, field_name):
                    missing_fields.append(field_name.replace("_", " ").title())
            if connector.status == IntegrationConnector.STATUS_DISABLED:
                state = "disabled"
            elif connector.status == IntegrationConnector.STATUS_BLOCKED:
                state = "blocked"
            elif missing_fields:
                state = "needs_setup"
            elif connector.status in {IntegrationConnector.STATUS_READY, IntegrationConnector.STATUS_LIVE}:
                state = "ready"
            else:
                state = "needs_setup"
        else:
            state = "missing"

        production_evidence = connector_production_evidence(connector)
        cards.append({
            "type": item["type"],
            "name": item["name"],
            "purpose": item["purpose"],
            "required": item["required"],
            "connector": connector,
            "state": state,
            "missing_fields": missing_fields,
            "status_choices": IntegrationConnector.STATUS_CHOICES,
            "mode_choices": IntegrationConnector.MODE_CHOICES,
            "selected_status": connector.status if connector else IntegrationConnector.STATUS_NEEDS_SETUP,
            "selected_mode": connector.mode if connector else IntegrationConnector.MODE_MANUAL,
            "needs_gstin": "gstin" in item["required"],
            "needs_tan": "tan" in item["required"],
            "needs_username": "username" in item["required"],
            "needs_endpoint": item["type"] != IntegrationConnector.TYPE_TALLY,
            "production_evidence_fields": [
                {**field, "value": production_evidence.get(field["key"], "")}
                for field in PRODUCTION_EVIDENCE_FIELDS
            ],
        })

    ready_count = sum(1 for card in cards if card["state"] == "ready")
    blocked_count = sum(1 for card in cards if card["state"] in {"missing", "blocked", "needs_setup"})
    return {
        "connectors": cards,
        "ready_count": ready_count,
        "blocked_count": blocked_count,
        "summary": (
            f"{ready_count} of {len(cards)} regulatory/sync connectors are ready."
            if cards else "No connector catalogue is available."
        ),
    }


def connector_production_evidence(connector):
    metadata = connector.metadata if connector and isinstance(connector.metadata, dict) else {}
    evidence = {}
    for field in PRODUCTION_EVIDENCE_FIELDS:
        evidence[field["key"]] = str(metadata.get(field["key"], "") or "").strip()
    return evidence


def statutory_control_focus_choices():
    return [
        ("attention", "Needs Attention"),
        ("critical", "Critical"),
        ("warning", "Warnings"),
        ("stale", "Stale Sync"),
        ("failed", "Failures"),
        ("ready", "Ready"),
        ("disabled", "Disabled"),
        ("all", "All"),
    ]


def build_statutory_integration_control_room(companies, *, focus="attention", as_of=None):
    as_of = as_of or timezone.now()
    company_list = list(companies)
    connectors = _connectors_by_company(company_list)
    latest_logs = _latest_logs_by_company_service(company_list)
    task_refs = _open_task_references(company_list)

    company_rows = []
    connector_rows = []
    for company in company_list:
        company_connector_rows = []
        for catalog_item in CONNECTOR_CATALOG:
            row = _control_connector_row(
                company,
                catalog_item,
                connectors.get(company.pk, {}).get(catalog_item["type"]),
                latest_logs.get((company.pk, CONNECTOR_SERVICE_MAP[catalog_item["type"]])),
                as_of,
                task_refs,
            )
            company_connector_rows.append(row)
            connector_rows.append(row)
        company_rows.append(_control_company_row(company, company_connector_rows))

    company_rows = _filter_control_rows(company_rows, focus, company_level=True)
    connector_rows = _filter_control_rows(connector_rows, focus)
    company_rows.sort(key=lambda row: (row["score"], -row["critical_count"], row["company"].name.lower()))
    connector_rows.sort(key=lambda row: (row["severity_rank"], row["company"].name.lower(), row["name"]))

    return {
        "company_rows": company_rows,
        "connector_rows": connector_rows,
        "totals": _control_totals(company_rows, connector_rows),
        "focus": focus,
        "as_of": as_of,
    }


def create_statutory_integration_tasks(connector_rows, user, manageable_company_ids, selected_keys=None):
    selected_keys = {value for value in selected_keys or [] if value}
    created = 0
    existing = 0
    skipped = 0
    today = timezone.localdate()

    for row in connector_rows:
        if selected_keys and row["selection_key"] not in selected_keys:
            continue
        if row["company"].pk not in manageable_company_ids:
            skipped += 1
            continue
        if row["severity"] not in {"critical", "warning"}:
            skipped += 1
            continue

        task, was_created = PracticeTask.objects.get_or_create(
            company=row["company"],
            reference=row["task_reference"],
            defaults={
                "title": f"Fix {row['name']} integration",
                "task_type": _task_type_for_connector(row["type"]),
                "priority": PracticeTask.PRIORITY_CRITICAL if row["severity"] == "critical" else PracticeTask.PRIORITY_HIGH,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": today,
                "created_by": user,
                "description": _integration_task_description(row),
            },
        )
        if was_created:
            created += 1
            AuditLog.objects.create(
                company=row["company"],
                user=user,
                action=AuditLog.ACTION_CREATE,
                model_name="PracticeTask",
                record_id=task.pk,
                object_repr=task.title[:200],
                old_data={},
                new_data={
                    "source": "statutory_integration_control_room",
                    "connector_type": row["type"],
                    "issue": row["issue"],
                    "reference": task.reference,
                },
            )
        else:
            existing += 1

    return {"created": created, "existing": existing, "skipped": skipped}


def _connectors_by_company(companies):
    company_ids = [company.pk for company in companies]
    connectors = {}
    for connector in IntegrationConnector.objects.filter(company_id__in=company_ids):
        connectors.setdefault(connector.company_id, {})[connector.connector_type] = connector
    return connectors


def _latest_logs_by_company_service(companies):
    company_ids = [company.pk for company in companies]
    service_values = set(CONNECTOR_SERVICE_MAP.values())
    latest = {}
    logs = (
        IntegrationRequestLog.objects.filter(company_id__in=company_ids, service__in=service_values)
        .select_related("company", "voucher", "requested_by")
        .order_by("company_id", "service", "-created_at", "-id")
    )
    for log in logs:
        latest.setdefault((log.company_id, log.service), log)
    return latest


def _open_task_references(companies):
    company_ids = [company.pk for company in companies]
    return set(
        PracticeTask.objects.filter(company_id__in=company_ids, reference__startswith="INTCTL:")
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
        .values_list("reference", flat=True)
    )


def _control_connector_row(company, catalog_item, connector, latest_log, as_of, task_refs):
    required = catalog_item["required"]
    missing_fields = [
        field.replace("_", " ").title()
        for field in required
        if not connector or not getattr(connector, field)
    ]
    task_reference = f"INTCTL:{company.pk}:{catalog_item['type']}"
    issue = ""
    next_action = ""
    severity = "ready"

    if not connector:
        severity = "critical"
        issue = "Connector not configured"
        next_action = "Configure provider, credentials, and operating mode."
    elif connector.status == IntegrationConnector.STATUS_DISABLED:
        severity = "disabled"
        issue = "Disabled"
        next_action = "Enable only if this client uses this integration."
    elif connector.status == IntegrationConnector.STATUS_BLOCKED:
        severity = "critical"
        issue = connector.last_error or "Connector marked blocked"
        next_action = "Resolve blocker and move connector back to Ready or Live."
    elif missing_fields:
        severity = "critical"
        issue = f"Missing {', '.join(missing_fields)}"
        next_action = "Complete client-owned connector settings."
    else:
        severity, issue, next_action = _operational_connector_state(connector, latest_log, catalog_item, as_of)

    return {
        "company": company,
        "type": catalog_item["type"],
        "name": catalog_item["name"],
        "purpose": catalog_item["purpose"],
        "connector": connector,
        "latest_log": latest_log,
        "selection_key": f"company:{company.pk}:connector:{catalog_item['type']}",
        "task_reference": task_reference,
        "task_exists": task_reference in task_refs,
        "severity": severity,
        "severity_label": _severity_label(severity),
        "severity_rank": {"critical": 0, "warning": 1, "ready": 2, "disabled": 3}.get(severity, 4),
        "badge_class": _severity_badge_class(severity),
        "issue": issue,
        "next_action": next_action,
        "missing_fields": missing_fields,
        "last_success_at": connector.last_success_at if connector else None,
        "last_failure_at": connector.last_failure_at if connector else None,
        "last_error": (connector.last_error if connector else "") or (latest_log.error_message if latest_log else ""),
        "credential_age_days": connector.credential_age_days if connector else None,
        "has_credential_age": connector.credential_age_days is not None if connector else False,
        "mode_label": connector.get_mode_display() if connector else "",
        "status_label": connector.get_status_display() if connector else "Not Configured",
        "provider_name": connector.provider_name if connector else "",
        "credential_reference": connector.credential_reference if connector else "",
        "latest_log_status": latest_log.get_status_display() if latest_log else "",
        "latest_log_at": latest_log.created_at if latest_log else None,
    }


def _operational_connector_state(connector, latest_log, catalog_item, as_of):
    credential_age = connector.credential_age_days
    if credential_age is not None and credential_age >= 180:
        return "critical", f"Credential age {credential_age} days", "Rotate credential reference and record the new rotation date."

    if latest_log and latest_log.status in {IntegrationRequestLog.STATUS_FAILED, IntegrationRequestLog.STATUS_CONFIG_ERROR}:
        if not connector.last_success_at or latest_log.created_at >= connector.last_success_at:
            return "critical", latest_log.error_message or latest_log.get_status_display(), "Fix provider response and rerun the integration."

    if connector.last_failure_at and (not connector.last_success_at or connector.last_failure_at > connector.last_success_at):
        return "critical", connector.last_error or "Latest connector run failed", "Fix last failure and rerun the integration."

    if credential_age is not None and credential_age >= 150:
        return "warning", f"Credential age {credential_age} days", "Plan credential rotation before the 180-day control limit."

    if connector.status in {IntegrationConnector.STATUS_READY, IntegrationConnector.STATUS_LIVE}:
        if not connector.last_success_at:
            return "warning", "No successful sync recorded", "Run a sandbox or portal sync and retain the success log."
        stale_days = catalog_item.get("stale_days", 7)
        age_days = (as_of - connector.last_success_at).days
        if age_days > stale_days:
            return "warning", f"Last success {age_days} days ago", "Run sync/status check and refresh evidence."
        return "ready", "Ready", "Monitor scheduled sync and evidence."

    return "warning", connector.get_status_display(), "Move connector to Ready/Live after setup is complete."


def _control_company_row(company, rows):
    critical = sum(1 for row in rows if row["severity"] == "critical")
    warning = sum(1 for row in rows if row["severity"] == "warning")
    ready = sum(1 for row in rows if row["severity"] == "ready")
    disabled = sum(1 for row in rows if row["severity"] == "disabled")
    score = max(0, min(100, 100 - (critical * 18) - (warning * 8) - (disabled * 2)))
    primary = next((row for row in rows if row["severity"] == "critical"), None) or next(
        (row for row in rows if row["severity"] == "warning"),
        None,
    )
    return {
        "company": company,
        "score": score,
        "critical_count": critical,
        "warning_count": warning,
        "ready_count": ready,
        "disabled_count": disabled,
        "connector_count": len(rows),
        "primary_issue": primary["issue"] if primary else "All required connectors are ready.",
        "primary_action": primary["next_action"] if primary else "Monitor integrations.",
        "primary_connector": primary["name"] if primary else "",
        "needs_attention": bool(critical or warning),
    }


def _filter_control_rows(rows, focus, *, company_level=False):
    if focus == "all":
        return list(rows)
    if company_level:
        if focus == "ready":
            return [row for row in rows if not row["needs_attention"]]
        if focus in {"critical", "failed"}:
            return [row for row in rows if row["critical_count"]]
        if focus in {"warning", "stale"}:
            return [row for row in rows if row["warning_count"]]
        if focus == "disabled":
            return [row for row in rows if row["disabled_count"]]
        return [row for row in rows if row["needs_attention"]]

    if focus == "ready":
        return [row for row in rows if row["severity"] == "ready"]
    if focus == "critical":
        return [row for row in rows if row["severity"] == "critical"]
    if focus == "warning":
        return [row for row in rows if row["severity"] == "warning"]
    if focus == "stale":
        return [row for row in rows if "Last success" in row["issue"] or "No successful sync" in row["issue"]]
    if focus == "failed":
        return [row for row in rows if row["severity"] == "critical" and ("failed" in row["issue"].lower() or row["last_error"])]
    if focus == "disabled":
        return [row for row in rows if row["severity"] == "disabled"]
    return [row for row in rows if row["severity"] in {"critical", "warning"}]


def _control_totals(company_rows, connector_rows):
    company_count = len(company_rows)
    score_total = sum(row["score"] for row in company_rows)
    return {
        "company_count": company_count,
        "connector_count": len(connector_rows),
        "critical_count": sum(1 for row in connector_rows if row["severity"] == "critical"),
        "warning_count": sum(1 for row in connector_rows if row["severity"] == "warning"),
        "ready_count": sum(1 for row in connector_rows if row["severity"] == "ready"),
        "disabled_count": sum(1 for row in connector_rows if row["severity"] == "disabled"),
        "task_count": sum(1 for row in connector_rows if row["task_exists"]),
        "avg_score": round(score_total / company_count) if company_count else 0,
    }


def _task_type_for_connector(connector_type):
    if connector_type in {IntegrationConnector.TYPE_GST, IntegrationConnector.TYPE_IRP, IntegrationConnector.TYPE_EWAY}:
        return PracticeTask.TYPE_GST
    if connector_type == IntegrationConnector.TYPE_TRACES:
        return PracticeTask.TYPE_TDS
    if connector_type == IntegrationConnector.TYPE_BANK:
        return PracticeTask.TYPE_BANK
    return PracticeTask.TYPE_OTHER


def _integration_task_description(row):
    return (
        f"Company: {row['company'].name}\n"
        f"Connector: {row['name']}\n"
        f"Status: {row['severity_label']}\n"
        f"Issue: {row['issue']}\n"
        f"Next action: {row['next_action']}\n"
        f"Provider: {row['provider_name'] or '-'}\n"
        f"Mode: {row['mode_label'] or '-'}\n"
        f"Last success: {row['last_success_at'].isoformat() if row['last_success_at'] else '-'}\n"
        f"Last failure: {row['last_failure_at'].isoformat() if row['last_failure_at'] else '-'}\n"
        f"Last error: {row['last_error'] or '-'}"
    )


def _severity_label(severity):
    return {
        "critical": "Critical",
        "warning": "Warning",
        "ready": "Ready",
        "disabled": "Disabled",
    }.get(severity, "Review")


def _severity_badge_class(severity):
    return {
        "critical": "bg-danger",
        "warning": "bg-warning text-dark",
        "ready": "bg-success",
        "disabled": "bg-secondary",
    }.get(severity, "bg-secondary")


def _recent_success_checks(company):
    since = timezone.now() - timezone.timedelta(days=30)
    checks = []
    for service, label in (
        (IntegrationRequestLog.SERVICE_E_INVOICE, "Recent e-invoice success"),
        (IntegrationRequestLog.SERVICE_E_WAY_BILL, "Recent e-way bill success"),
    ):
        exists = IntegrationRequestLog.objects.filter(
            company=company,
            service=service,
            status=IntegrationRequestLog.STATUS_SUCCESS,
            created_at__gte=since,
        ).exists()
        checks.append(_check(
            label,
            exists,
            f"{label} exists in the last 30 days.",
            "Run a sandbox provider call and keep the success log for certification evidence.",
            warning=True,
        ))
    return checks


def _summary(errors, warnings, manual, sandbox_ready, production_ready):
    if production_ready:
        return "Production API readiness is green."
    if sandbox_ready:
        return "Sandbox readiness is green; production still needs manual provider/onboarding evidence."
    if errors:
        return f"Blocked by {errors} required configuration issue(s)."
    if warnings or manual:
        return f"Not blocked locally, but {warnings + manual} readiness item(s) still need review."
    return "GST readiness checks are clear."


def _check(name, passed, ok_message, hint, *, warning=False):
    if passed:
        return _ok(name, ok_message)
    return {
        "name": name,
        "level": "warning" if warning else "error",
        "message": hint,
        "hint": hint,
    }


def _ok(name, message):
    return {"name": name, "level": "ok", "message": message, "hint": ""}


def _manual(name, message, hint):
    return {"name": name, "level": "manual", "message": message, "hint": hint}
