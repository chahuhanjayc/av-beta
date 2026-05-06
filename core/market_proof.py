import csv
import hashlib
import json
import re
from datetime import timedelta
from urllib.parse import urlencode

from django.http import HttpResponse
from django.urls import reverse
from django.utils import timezone

from clients.models import ClientSubscription
from integrations.models import IntegrationRequestLog, StatutoryExportLog
from integrations.provider_readiness import build_provider_go_live_readiness
from portal.models import ClientDocumentRequest, PortalUser
from vouchers.models import Voucher

from .models import AuditLog, Company, GSTEvidenceDocument, PracticeTask, UserCompanyAccess
from .market_external_evidence import build_external_evidence_signals
from .market_case_studies import build_case_study_signals
from .pilot_feedback import build_pilot_feedback_signals
from .production_trust import build_production_trust_context


MARKET_PROOF_TASK_PREFIX = "MARKETPROOF:"
MARKET_PROOF_PACK_SCHEMA_VERSION = 1
SEVERITY_WEIGHT = {"critical": 24, "warning": 10, "info": 4}
SEVERITY_PRIORITY = {
    "critical": PracticeTask.PRIORITY_CRITICAL,
    "warning": PracticeTask.PRIORITY_HIGH,
    "info": PracticeTask.PRIORITY_NORMAL,
}


def build_market_proof_pack(user, params=None, *, current_company=None):
    params = params or {}
    today = timezone.localdate()
    companies = list(_companies_for_user(user))
    subscriptions = {
        item.company_id: item
        for item in ClientSubscription.objects.filter(company__in=companies)
    }
    platform = build_platform_proof(current_company=current_company)
    rows = [
        build_company_market_proof_row(
            company,
            user,
            today=today,
            subscription=subscriptions.get(company.pk),
        )
        for company in companies
    ]

    q = (params.get("q") or "").strip().lower()
    band_filter = (params.get("band") or "all").strip()
    if q:
        rows = [
            row for row in rows
            if q in row["company"].name.lower() or q in (row["company"].gstin or "").lower()
        ]
    if band_filter != "all":
        rows = [row for row in rows if row["band_key"] == band_filter]

    rows.sort(key=lambda row: (
        row["sort_rank"],
        row["score"],
        -row["critical_count"],
        -row["proof_signal_count"],
        row["company"].name,
    ))
    client_average = round(sum(row["score"] for row in rows) / len(rows)) if rows else 0
    overall_score = round((client_average * 0.65) + (platform["score"] * 0.35)) if rows else platform["score"]
    totals = {
        "clients": len(rows),
        "client_avg_score": client_average,
        "overall_score": overall_score,
        "market_ready": sum(1 for row in rows if row["band_key"] == "market_ready"),
        "proven_pilot": sum(1 for row in rows if row["band_key"] == "proven_pilot"),
        "needs_proof": sum(1 for row in rows if row["band_key"] == "needs_proof"),
        "blocked": sum(1 for row in rows if row["band_key"] == "blocked"),
        "critical_gates": sum(row["critical_count"] for row in rows) + platform["critical_count"],
        "warning_gates": sum(row["warning_count"] for row in rows) + platform["warning_count"],
        "feedback_blockers": sum(row["feedback_signals"]["open_blocker_count"] for row in rows),
        "publishable_case_studies": sum(row["case_study_signals"]["publishable_count"] for row in rows),
        "proof_signals": sum(row["proof_signal_count"] for row in rows),
        "verified_external_evidence": sum(row["external_evidence_signals"]["verified_count"] for row in rows),
        "external_evidence_missing": sum(row["external_evidence_signals"]["missing_required_count"] for row in rows),
        "provider_cert_missing": sum(row["provider_cert_missing"] for row in rows),
        "manageable_clients": sum(1 for row in rows if row["can_manage"]),
    }
    return {
        "rows": rows,
        "platform": platform,
        "totals": totals,
        "q": q,
        "band_filter": band_filter,
        "band_options": [
            ("all", "All Clients"),
            ("blocked", "Blocked"),
            ("needs_proof", "Needs Proof"),
            ("proven_pilot", "Proven Pilot"),
            ("market_ready", "Market Ready"),
        ],
        "export_query": urlencode({key: value for key, value in {"q": q, "band": band_filter}.items() if value and value != "all"}),
    }


def build_platform_proof(*, current_company=None):
    trust = build_production_trust_context()
    summary = trust["summary"]
    gates = []

    def add_gate(code, title, passed, *, severity, detail, action_label, action_url, taskable=True):
        gates.append({
            "code": code,
            "reference": _global_reference(current_company, code),
            "title": title,
            "passed": bool(passed),
            "severity": severity,
            "detail": detail,
            "action_label": action_label,
            "action_url": action_url,
            "taskable": bool(taskable and current_company),
        })

    production_trust_url = reverse("core:production_trust_center")
    operations_url = reverse("core:operations_monitor")
    system_url = reverse("core:system_observability")

    add_gate(
        "preflight_clean",
        "Production preflight clean",
        summary["errors"] == 0,
        severity="critical",
        detail=f"{summary['errors']} error(s), {summary['warnings']} warning(s) in production checks.",
        action_label="Open Production Trust",
        action_url=production_trust_url,
    )
    add_gate(
        "backup_policy_ready",
        "Backup policy ready",
        summary["has_backup"] and summary["backup_policy_critical"] == 0,
        severity="critical",
        detail=f"Backup policy {summary['backup_policy_status']}; {summary['backup_policy_critical']} critical, {summary['backup_policy_warnings']} warning.",
        action_label="Open Production Trust",
        action_url=production_trust_url,
    )
    add_gate(
        "restore_drill_passed",
        "Restore drill passed",
        summary["restore_ready"],
        severity="critical",
        detail=f"{summary['restore_drill_count']} restore drill(s), {summary['restore_findings']} open finding(s).",
        action_label="Open Production Trust",
        action_url=production_trust_url,
    )
    add_gate(
        "scheduled_backup_evidence",
        "Scheduled backup evidence",
        summary["scheduled_backup_critical"] == 0 and summary["scheduled_backup_status"] in {"Ready", "Watch"},
        severity="warning",
        detail=f"Scheduled backup {summary['scheduled_backup_status']}; {summary['scheduled_backup_critical']} critical, {summary['scheduled_backup_warnings']} warning.",
        action_label="Open System Observability",
        action_url=system_url,
    )
    add_gate(
        "operations_visible",
        "Operations cockpit visible",
        True,
        severity="info",
        detail="Operations Monitor and System Observability are available for runtime evidence.",
        action_label="Open Operations",
        action_url=operations_url,
        taskable=False,
    )

    failed = [gate for gate in gates if not gate["passed"]]
    score = max(0, min(100, summary["score"] - sum(SEVERITY_WEIGHT[gate["severity"]] for gate in failed if gate["severity"] != "info") // 2))
    return {
        "trust": trust,
        "score": score,
        "band": summary["band"],
        "badge_class": summary["badge_class"],
        "gates": gates,
        "failed_gates": failed,
        "critical_count": sum(1 for gate in failed if gate["severity"] == "critical"),
        "warning_count": sum(1 for gate in failed if gate["severity"] == "warning"),
        "task_company": current_company,
    }


def build_company_market_proof_row(company, user, *, today=None, subscription=None):
    today = today or timezone.localdate()
    now = timezone.now()
    last_30_date = today - timedelta(days=30)
    last_30_dt = now - timedelta(days=30)
    last_90_dt = now - timedelta(days=90)
    feedback = build_pilot_feedback_signals(company, today=today)
    case_studies = build_case_study_signals(company)
    external_evidence = build_external_evidence_signals(company, today=today)
    provider = build_provider_go_live_readiness(company)

    portal_user_count = PortalUser.objects.filter(linked_ledger__company=company, is_active=True).distinct().count()
    requests = ClientDocumentRequest.objects.filter(company=company)
    responded_requests = requests.filter(
        status__in=[ClientDocumentRequest.STATUS_UPLOADED, ClientDocumentRequest.STATUS_CLOSED]
    ).count()
    closed_requests = requests.filter(status=ClientDocumentRequest.STATUS_CLOSED).count()
    audit_events_30 = AuditLog.objects.filter(company=company, timestamp__gte=last_30_dt).count()
    vouchers_30 = Voucher.objects.filter(company=company, date__gte=last_30_date, date__lte=today).count()
    integration_success_30 = IntegrationRequestLog.objects.filter(
        company=company,
        status=IntegrationRequestLog.STATUS_SUCCESS,
        created_at__gte=last_30_dt,
    ).count()
    statutory_exports_90 = StatutoryExportLog.objects.filter(company=company, created_at__gte=last_90_dt).count()
    gst_evidence_90 = GSTEvidenceDocument.objects.filter(company=company, uploaded_at__gte=last_90_dt).count()
    closed_tasks_30 = PracticeTask.objects.filter(
        company=company,
        status=PracticeTask.STATUS_DONE,
        completed_at__gte=last_30_dt,
    ).count()
    open_market_tasks = PracticeTask.objects.filter(
        company=company,
        reference__startswith=f"{MARKET_PROOF_TASK_PREFIX}{company.pk}:",
    ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED]).count()

    subscription_days_left = None
    subscription_safe = False
    if subscription:
        subscription_days_left = (subscription.subscription_end.date() - today).days
        subscription_safe = (
            subscription.status in {ClientSubscription.STATUS_ACTIVE, ClientSubscription.STATUS_TRIAL}
            and subscription_days_left >= 14
        )

    gates = []

    def add_gate(code, title, passed, *, severity, detail, action_label, action_url):
        gates.append({
            "code": code,
            "reference": f"{MARKET_PROOF_TASK_PREFIX}{company.pk}:{code}",
            "title": title,
            "passed": bool(passed),
            "severity": severity,
            "detail": detail,
            "action_label": action_label,
            "action_url": action_url,
        })

    feedback_url = f"{reverse('core:pilot_feedback_register')}?{urlencode({'company': company.pk})}"
    case_study_url = f"{reverse('core:market_case_studies')}?{urlencode({'company': company.pk})}"
    provider_url = reverse("integrations:provider_readiness")
    evidence_url = reverse("integrations:evidence_center")
    external_evidence_url = f"{reverse('core:market_external_evidence')}?{urlencode({'company': company.pk, 'status': 'all'})}"
    tasks_url = f"{reverse('core:practice_tasks')}?{urlencode({'company': company.pk, 'status': 'open'})}"
    client_360_url = reverse("core:client_360", args=[company.pk])
    audit_url = reverse("core:audit_log")

    add_gate(
        "live_usage_visible",
        "Live usage visible",
        vouchers_30 > 0 or audit_events_30 >= 3 or responded_requests > 0,
        severity="critical",
        detail=f"{vouchers_30} voucher(s), {audit_events_30} audit event(s), {responded_requests} document response(s) in the proof window.",
        action_label="Open Audit Trail",
        action_url=audit_url,
    )
    add_gate(
        "pilot_feedback_captured",
        "Pilot feedback captured",
        feedback["recent_feedback_count"] > 0,
        severity="critical",
        detail=f"{feedback['recent_feedback_count']} feedback signal(s) in 30 days; latest: {feedback['latest_summary'] or '-'}",
        action_label="Open Feedback",
        action_url=feedback_url,
    )
    add_gate(
        "feedback_blockers_clear",
        "Feedback blockers clear",
        feedback["open_blocker_count"] == 0,
        severity="critical",
        detail=f"{feedback['open_blocker_count']} high/critical blocker(s), {feedback['open_negative_count']} open negative signal(s).",
        action_label="Open Feedback",
        action_url=feedback_url,
    )
    add_gate(
        "confidence_signal",
        "Client confidence signal",
        feedback["avg_confidence"] >= 7 and (feedback["positive_signal_count"] > 0 or feedback["resolved_recent_count"] > 0),
        severity="warning",
        detail=f"Average confidence {feedback['avg_confidence']}/10; {feedback['positive_signal_count']} positive signal(s), {feedback['resolved_recent_count']} recent resolution(s).",
        action_label="Open Feedback",
        action_url=feedback_url,
    )
    add_gate(
        "publishable_case_study",
        "Publishable case study",
        case_studies["publishable_count"] > 0,
        severity="critical",
        detail=f"{case_studies['publishable_count']} publishable case stud(ies), {case_studies['approved_count']} approved, {case_studies['consented_count']} consented; latest: {case_studies['latest_title'] or '-'}.",
        action_label="Open Case Studies",
        action_url=case_study_url,
    )
    add_gate(
        "commercial_outcome_visible",
        "Commercial outcome visible",
        case_studies["converted_count"] > 0,
        severity="warning",
        detail=f"{case_studies['converted_count']} converted/paid/expanded case outcome(s), {case_studies['with_metrics_count']} with metric proof.",
        action_label="Open Case Studies",
        action_url=case_study_url,
    )
    add_gate(
        "provider_certification_evidence",
        "Provider certification evidence",
        provider["certification"]["required"] > 0
        and provider["certification"]["missing"] == 0
        and provider["score"] >= 70,
        severity="critical",
        detail=f"Provider score {provider['score']}%; certification packs {provider['certification']['ready']}/{provider['certification']['required']}, missing fields {provider['certification']['missing']}.",
        action_label="Open Provider Readiness",
        action_url=provider_url,
    )
    add_gate(
        "statutory_evidence_exists",
        "Statutory evidence exists",
        gst_evidence_90 > 0 or statutory_exports_90 > 0 or integration_success_30 > 0,
        severity="warning",
        detail=f"{gst_evidence_90} GST evidence file(s), {statutory_exports_90} export log(s), {integration_success_30} provider success log(s).",
        action_label="Open Evidence Center",
        action_url=evidence_url,
    )
    add_gate(
        "external_evidence_verified",
        "External evidence verified",
        external_evidence["missing_required_count"] == 0,
        severity="warning",
        detail=f"{external_evidence['verified_count']} verified item(s); missing: {', '.join(external_evidence['missing_required_labels']) or 'none'}.",
        action_label="Open External Evidence",
        action_url=external_evidence_url,
    )
    add_gate(
        "client_access_safe",
        "Client access/commercial safe",
        subscription_safe and portal_user_count > 0,
        severity="warning",
        detail=f"{portal_user_count} portal user(s); subscription day(s) left {subscription_days_left if subscription_days_left is not None else '-'}.",
        action_label="Open Client 360",
        action_url=client_360_url,
    )
    add_gate(
        "closed_loop_proof",
        "Closed-loop proof exists",
        closed_tasks_30 > 0 or feedback["resolved_recent_count"] > 0 or closed_requests > 0,
        severity="warning",
        detail=f"{closed_tasks_30} task(s) closed, {feedback['resolved_recent_count']} feedback item(s) resolved, {closed_requests} request(s) closed.",
        action_label="Open Work Queue",
        action_url=tasks_url,
    )

    failed = [gate for gate in gates if not gate["passed"]]
    critical_count = sum(1 for gate in failed if gate["severity"] == "critical")
    warning_count = sum(1 for gate in failed if gate["severity"] == "warning")
    score = max(0, min(100, 100 - sum(SEVERITY_WEIGHT[gate["severity"]] for gate in failed)))
    band_key, band, badge_class, sort_rank = _market_band(score, critical_count, warning_count)
    proof_signal_count = (
        feedback["recent_feedback_count"]
        + case_studies["publishable_count"]
        + case_studies["with_metrics_count"]
        + case_studies["converted_count"]
        + vouchers_30
        + responded_requests
        + integration_success_30
        + statutory_exports_90
        + gst_evidence_90
        + closed_tasks_30
        + external_evidence["verified_count"]
    )
    return {
        "company": company,
        "score": score,
        "band": band,
        "band_key": band_key,
        "badge_class": badge_class,
        "sort_rank": sort_rank,
        "gates": gates,
        "failed_gates": failed,
        "top_gates": sorted(failed, key=_gate_sort_key)[:5],
        "critical_count": critical_count,
        "warning_count": warning_count,
        "passed_count": len(gates) - len(failed),
        "total_gates": len(gates),
        "feedback_signals": feedback,
        "case_study_signals": case_studies,
        "external_evidence_signals": external_evidence,
        "provider": provider,
        "provider_cert_missing": provider["certification"]["missing"],
        "portal_user_count": portal_user_count,
        "responded_requests": responded_requests,
        "closed_requests": closed_requests,
        "audit_events_30": audit_events_30,
        "vouchers_30": vouchers_30,
        "integration_success_30": integration_success_30,
        "statutory_exports_90": statutory_exports_90,
        "gst_evidence_90": gst_evidence_90,
        "closed_tasks_30": closed_tasks_30,
        "open_market_tasks": open_market_tasks,
        "proof_signal_count": proof_signal_count,
        "subscription_days_left": subscription_days_left,
        "can_manage": _can_manage_company(user, company),
    }


def create_market_proof_tasks(context, user):
    today = timezone.localdate()
    created = 0
    existing = 0
    skipped = 0

    platform = context.get("platform", {})
    task_company = platform.get("task_company")
    if task_company and _can_manage_company(user, task_company):
        for gate in platform.get("failed_gates", []):
            if not gate.get("taskable") or gate["severity"] == "info":
                continue
            was_created = _create_task_for_gate(
                company=task_company,
                gate=gate,
                user=user,
                title_prefix="Market proof platform",
                task_type=PracticeTask.TYPE_AUDIT,
                due_date=today + timedelta(days=1 if gate["severity"] == "critical" else 5),
            )
            created += 1 if was_created is True else 0
            existing += 1 if was_created is False else 0
    elif platform.get("failed_gates"):
        skipped += sum(1 for gate in platform["failed_gates"] if gate.get("taskable") and gate["severity"] != "info")

    for row in context.get("rows", []):
        if not row["can_manage"]:
            skipped += sum(1 for gate in row["failed_gates"] if gate["severity"] != "info")
            continue
        for gate in row["failed_gates"]:
            if gate["severity"] == "info":
                continue
            was_created = _create_task_for_gate(
                company=row["company"],
                gate=gate,
                user=user,
                title_prefix="Market proof",
                task_type=_task_type_for_gate(gate),
                due_date=today + timedelta(days=1 if gate["severity"] == "critical" else 5),
            )
            created += 1 if was_created is True else 0
            existing += 1 if was_created is False else 0
    return {"created": created, "existing": existing, "skipped": skipped}


def market_proof_csv_response(context):
    response = HttpResponse(content_type="text/csv; charset=utf-8")
    response["Content-Disposition"] = 'attachment; filename="market-proof-pack.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Proof Score",
        "Proof Band",
        "Critical Gates",
        "Warning Gates",
        "Top Gaps",
        "Feedback 30 Days",
        "Feedback Blockers",
        "Avg Confidence",
        "Publishable Case Studies",
        "Converted Case Studies",
        "Case Study Missing Items",
        "Verified External Evidence",
        "Missing External Evidence Categories",
        "Provider Score",
        "Provider Certification Missing",
        "Vouchers 30 Days",
        "Audit Events 30 Days",
        "Document Responses",
        "Integration Success 30 Days",
        "GST Evidence 90 Days",
        "Statutory Exports 90 Days",
        "Closed Tasks 30 Days",
    ])
    for row in context["rows"]:
        writer.writerow([
            row["company"].name,
            row["company"].gstin or "",
            row["score"],
            row["band"],
            row["critical_count"],
            row["warning_count"],
            "; ".join(gate["title"] for gate in row["top_gates"]),
            row["feedback_signals"]["recent_feedback_count"],
            row["feedback_signals"]["open_blocker_count"],
            row["feedback_signals"]["avg_confidence"],
            row["case_study_signals"]["publishable_count"],
            row["case_study_signals"]["converted_count"],
            row["case_study_signals"]["missing_item_count"],
            row["external_evidence_signals"]["verified_count"],
            "; ".join(row["external_evidence_signals"]["missing_required_labels"]),
            row["provider"]["score"],
            row["provider_cert_missing"],
            row["vouchers_30"],
            row["audit_events_30"],
            row["responded_requests"],
            row["integration_success_30"],
            row["gst_evidence_90"],
            row["statutory_exports_90"],
            row["closed_tasks_30"],
        ])
    return response


def build_market_proof_evidence_pack(user, params=None, *, current_company=None, as_of=None):
    generated_at = as_of or timezone.now()
    context = build_market_proof_pack(user, params, current_company=current_company)
    pack = {
        "pack_schema_version": MARKET_PROOF_PACK_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "generated_by": _user_snapshot(user),
        "redaction": {
            "raw_credentials_included": False,
            "raw_provider_payloads_included": False,
            "raw_client_documents_included": False,
            "artifact_strategy": "Counts, statuses, references, and SHA-256 hashes only.",
        },
        "current_company": _company_snapshot(current_company),
        "filters": {
            "q": context["q"],
            "band": context["band_filter"],
        },
        "totals": context["totals"],
        "platform": _platform_proof_snapshot(context["platform"]),
        "clients": [_client_market_proof_snapshot(row) for row in context["rows"]],
        "evidence_gaps": _evidence_gap_snapshot(context),
    }
    pack["pack_id"] = f"MPP-{_pack_hash(pack)[:16].upper()}"
    pack["sha256"] = _pack_hash(pack)
    return pack


def market_proof_evidence_pack_bytes(pack):
    return json.dumps(pack, indent=2, sort_keys=True, default=str).encode("utf-8")


def market_proof_evidence_pack_filename(pack):
    current_company = pack.get("current_company") or {}
    raw_code = current_company.get("short_code") or current_company.get("name") or "all-clients"
    code = _safe_filename_part(raw_code) or "market-proof"
    timestamp = _safe_filename_part(pack.get("generated_at", "")[:19].replace("T", "-")) or "generated"
    return f"market-proof-evidence-{code}-{timestamp}-{pack.get('pack_id', 'mpp').lower()}.json"


def market_proof_evidence_pack_response(pack):
    response = HttpResponse(market_proof_evidence_pack_bytes(pack), content_type="application/json; charset=utf-8")
    response["Content-Disposition"] = f'attachment; filename="{market_proof_evidence_pack_filename(pack)}"'
    return response


def _client_market_proof_snapshot(row):
    return {
        "company": _company_snapshot(row["company"]),
        "score": row["score"],
        "band": row["band"],
        "band_key": row["band_key"],
        "critical_count": row["critical_count"],
        "warning_count": row["warning_count"],
        "passed_count": row["passed_count"],
        "total_gates": row["total_gates"],
        "proof_signal_count": row["proof_signal_count"],
        "open_market_tasks": row["open_market_tasks"],
        "can_manage": row["can_manage"],
        "subscription_days_left": row["subscription_days_left"],
        "feedback_signals": _plain_mapping(row["feedback_signals"]),
        "case_study_signals": _plain_mapping(row["case_study_signals"]),
        "external_evidence_signals": _plain_mapping(row["external_evidence_signals"]),
        "provider": _provider_snapshot(row["provider"]),
        "live_evidence": {
            "portal_user_count": row["portal_user_count"],
            "responded_requests": row["responded_requests"],
            "closed_requests": row["closed_requests"],
            "audit_events_30": row["audit_events_30"],
            "vouchers_30": row["vouchers_30"],
            "integration_success_30": row["integration_success_30"],
            "statutory_exports_90": row["statutory_exports_90"],
            "gst_evidence_90": row["gst_evidence_90"],
            "closed_tasks_30": row["closed_tasks_30"],
        },
        "gates": [_gate_snapshot(gate) for gate in row["gates"]],
        "failed_gates": [_gate_snapshot(gate) for gate in row["failed_gates"]],
        "top_gaps": [_gate_snapshot(gate) for gate in row["top_gates"]],
    }


def _platform_proof_snapshot(platform):
    trust = platform.get("trust") or {}
    return {
        "score": platform.get("score", 0),
        "band": platform.get("band", ""),
        "critical_count": platform.get("critical_count", 0),
        "warning_count": platform.get("warning_count", 0),
        "production_trust_summary": _plain_mapping(trust.get("summary") or {}),
        "gates": [_gate_snapshot(gate) for gate in platform.get("gates", [])],
        "failed_gates": [_gate_snapshot(gate) for gate in platform.get("failed_gates", [])],
        "task_company": _company_snapshot(platform.get("task_company")),
    }


def _evidence_gap_snapshot(context):
    gaps = []
    platform = context.get("platform") or {}
    for gate in platform.get("failed_gates", []):
        gaps.append({
            "scope": "platform",
            "company": _company_snapshot(platform.get("task_company")),
            "score": platform.get("score", 0),
            **_gate_snapshot(gate),
        })
    for row in context.get("rows", []):
        for gate in row.get("failed_gates", []):
            gaps.append({
                "scope": "client",
                "company": _company_snapshot(row["company"]),
                "score": row["score"],
                **_gate_snapshot(gate),
            })
    gaps.sort(key=lambda item: (
        {"critical": 0, "warning": 1, "info": 2}.get(item["severity"], 3),
        item["scope"],
        (item["company"] or {}).get("name", ""),
        item["title"],
    ))
    return {
        "count": len(gaps),
        "critical": sum(1 for item in gaps if item["severity"] == "critical"),
        "warning": sum(1 for item in gaps if item["severity"] == "warning"),
        "items": gaps[:100],
    }


def _provider_snapshot(provider):
    provider = provider or {}
    return {
        "score": provider.get("score", 0),
        "status": provider.get("status", ""),
        "status_label": provider.get("status_label", ""),
        "summary": provider.get("summary", ""),
        "totals": _plain_mapping(provider.get("totals") or {}),
        "certification": _plain_mapping(provider.get("certification") or {}),
        "retry_summary": _plain_mapping(provider.get("retry_summary") or {}),
        "recent_failure_count": len(provider.get("recent_failures") or []),
        "open_retry_job_count": len(provider.get("open_retry_jobs") or []),
        "connectors": [_provider_connector_snapshot(row) for row in provider.get("connector_rows", [])],
    }


def _provider_connector_snapshot(row):
    certification = row.get("certification") or {}
    return {
        "type": row.get("type", ""),
        "name": row.get("name", ""),
        "purpose": row.get("purpose", ""),
        "service": row.get("service", ""),
        "state": row.get("state", ""),
        "state_label": row.get("state_label", ""),
        "score": row.get("score", 0),
        "critical_count": row.get("critical_count", 0),
        "warning_count": row.get("warning_count", 0),
        "open_retry_count": row.get("open_retry_count", 0),
        "failed_retry_count": row.get("failed_retry_count", 0),
        "connector": _provider_connector_model_snapshot(row.get("connector")),
        "checks": [_plain_mapping(check) for check in row.get("checks", [])],
        "taskable_issues": [_plain_mapping(issue) for issue in row.get("taskable_issues", [])],
        "certification": {
            "required": bool(certification.get("required")),
            "missing_count": certification.get("missing_count", 0),
            "fields": [_plain_mapping(field) for field in certification.get("fields", [])],
        },
    }


def _provider_connector_model_snapshot(connector):
    if not connector:
        return None
    return {
        "id": connector.pk,
        "label": getattr(connector, "label", str(connector)),
        "provider_name": connector.provider_name,
        "mode": connector.mode,
        "status": connector.status,
        "gstin": connector.gstin or "",
        "tan": connector.tan or "",
        "username": connector.masked_username,
        "base_url": connector.base_url or "",
        "credential_reference_recorded": bool(connector.credential_reference),
        "credential_age_days": connector.credential_age_days,
        "credential_last_rotated_at": connector.credential_last_rotated_at.isoformat() if connector.credential_last_rotated_at else "",
        "last_success_at": connector.last_success_at.isoformat() if connector.last_success_at else "",
        "last_failure_at": connector.last_failure_at.isoformat() if connector.last_failure_at else "",
    }


def _gate_snapshot(gate):
    return {
        "code": gate.get("code", ""),
        "reference": gate.get("reference", ""),
        "title": gate.get("title", ""),
        "passed": bool(gate.get("passed")),
        "severity": gate.get("severity", ""),
        "detail": gate.get("detail", ""),
        "action_label": gate.get("action_label", ""),
        "action_url": gate.get("action_url", ""),
    }


def _user_snapshot(user):
    return {
        "id": getattr(user, "pk", None),
        "email": getattr(user, "email", "") or "",
        "is_staff": bool(getattr(user, "is_staff", False)),
        "is_superuser": bool(getattr(user, "is_superuser", False)),
    }


def _company_snapshot(company):
    if not company:
        return None
    return {
        "id": company.pk,
        "name": company.name,
        "short_code": company.short_code or "",
        "gstin": company.gstin or "",
        "tan": company.tan or "",
    }


def _plain_mapping(mapping):
    return {key: _plain_value(value) for key, value in dict(mapping).items()}


def _plain_value(value):
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if isinstance(value, dict):
        return _plain_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return [_plain_value(item) for item in value]
    return str(value)


def _pack_hash(pack):
    clean = dict(pack)
    clean.pop("sha256", None)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()


def _safe_filename_part(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-").lower()


def _create_task_for_gate(*, company, gate, user, title_prefix, task_type, due_date):
    task = PracticeTask.objects.filter(
        company=company,
        reference=gate["reference"],
    ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED]).first()
    if task:
        return False
    PracticeTask.objects.create(
        company=company,
        title=f"{title_prefix}: {gate['title']}",
        task_type=task_type,
        priority=SEVERITY_PRIORITY[gate["severity"]],
        status=PracticeTask.STATUS_OPEN,
        due_date=due_date,
        assigned_to=user,
        created_by=user,
        reference=gate["reference"],
        description=f"{gate['detail']}\n\nAction: {gate['action_label']} - {gate['action_url']}",
    )
    return True


def _companies_for_user(user):
    if user.is_superuser:
        return Company.objects.all().order_by("name")
    return Company.objects.filter(user_access__user=user).distinct().order_by("name")


def _can_manage_company(user, company):
    if user.is_superuser:
        return True
    return UserCompanyAccess.objects.filter(user=user, company=company, role__in=["Admin", "Accountant"]).exists()


def _global_reference(company, code):
    company_id = company.pk if company else "none"
    return f"{MARKET_PROOF_TASK_PREFIX}GLOBAL:{company_id}:{code}"


def _market_band(score, critical_count, warning_count):
    if critical_count:
        return "blocked", "Blocked", "bg-danger", 0
    if score >= 92 and warning_count == 0:
        return "market_ready", "Market Ready", "bg-success", 3
    if score >= 78:
        return "proven_pilot", "Proven Pilot", "bg-primary", 2
    if score >= 60:
        return "needs_proof", "Needs Proof", "bg-warning text-dark", 1
    return "blocked", "Blocked", "bg-danger", 0


def _gate_sort_key(gate):
    return (
        {"critical": 0, "warning": 1, "info": 2}.get(gate["severity"], 3),
        gate["title"],
    )


def _task_type_for_gate(gate):
    if "provider" in gate["code"] or "statutory" in gate["code"]:
        return PracticeTask.TYPE_GST
    if "feedback" in gate["code"] or "confidence" in gate["code"]:
        return PracticeTask.TYPE_OTHER
    if "usage" in gate["code"] or "closed_loop" in gate["code"]:
        return PracticeTask.TYPE_AUDIT
    return PracticeTask.TYPE_OTHER
