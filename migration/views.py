import hashlib
import os
import json
import csv
import hashlib
import pandas as pd
from datetime import timedelta
from urllib.parse import urlencode
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db import models, transaction
from django.http import HttpResponse, JsonResponse
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.http import require_POST
from .models import ImportSession
from .forms import MigrationUploadForm
from .parser import SmartParser
from core.decorators import write_required
from core.models import AuditLog, PracticeTask, UserCompanyAccess
from core.operating_readiness import build_company_operating_readiness
from integrations.models import IntegrationConnector
from ledger.models import Ledger, AccountGroup
from vouchers.models import Voucher, VoucherItem
from decimal import Decimal


APPROVAL_CHECKLIST = [
    ("backup_taken", "Backup/export evidence taken"),
    ("period_verified", "Source period and company GUID verified"),
    ("duplicate_reviewed", "Duplicate file, voucher, and period risks reviewed"),
    ("ledger_mapping_reviewed", "Suspense and ignored ledger decisions reviewed"),
    ("opening_balances_verified", "Opening balances compared with audited trial balance"),
]


def _cleanup_issue(key, severity, title, message, samples, count=None):
    return {
        "key": key,
        "severity": severity,
        "title": title,
        "message": message,
        "count": count if count is not None else len(samples),
        "samples": samples[:10],
    }


def _score_cleanup_issues(issues):
    penalties = {"critical": 14, "high": 9, "medium": 4, "low": 2}
    caps = {"critical": 45, "high": 30, "medium": 18, "low": 8}
    penalty = Decimal("0")
    for issue in issues:
        severity = issue.get("severity", "low")
        count = Decimal(str(issue.get("count") or 0))
        penalty += min(count * penalties.get(severity, 2), caps.get(severity, 8))
    return max(0, int(100 - penalty))


def _cleanup_band(score):
    if score >= 90:
        return "Excellent"
    if score >= 75:
        return "Good"
    if score >= 50:
        return "Needs Cleanup"
    return "High Risk"


def _hash_uploaded_file(file_obj):
    digest = hashlib.sha256()
    position = file_obj.tell() if hasattr(file_obj, "tell") else None
    for chunk in file_obj.chunks():
        digest.update(chunk)
    if position is not None and hasattr(file_obj, "seek"):
        file_obj.seek(position)
    return digest.hexdigest()


def _import_fingerprint(session):
    parts = [
        str(session.company_id),
        session.source_system or "",
        session.source_company_guid or "",
        session.source_period_start.isoformat() if session.source_period_start else "",
        session.source_period_end.isoformat() if session.source_period_end else "",
        session.source_file_hash or "",
    ]
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _sync_control_summary(session, duplicate_session=None):
    return {
        "source_system": session.get_source_system_display(),
        "sync_mode": session.get_sync_mode_display(),
        "source_company_guid": session.source_company_guid,
        "source_period_start": session.source_period_start.isoformat() if session.source_period_start else "",
        "source_period_end": session.source_period_end.isoformat() if session.source_period_end else "",
        "source_file_hash": session.source_file_hash,
        "import_fingerprint": session.import_fingerprint,
        "duplicate_source_session_id": duplicate_session.pk if duplicate_session else None,
    }


def _periods_overlap(a_start, a_end, b_start, b_end):
    if not all([a_start, a_end, b_start, b_end]):
        return False
    return a_start <= b_end and b_start <= a_end


def _risk_issue(key, severity, title, message, count=1, samples=None):
    return {
        "key": key,
        "severity": severity,
        "title": title,
        "message": message,
        "count": count,
        "samples": samples or [],
    }


def _score_sync_risks(issues):
    penalties = {"critical": 24, "high": 14, "medium": 7, "low": 3}
    score = 100
    for issue in issues:
        score -= min(issue.get("count", 1) * penalties.get(issue.get("severity"), 3), 40)
    return max(score, 0)


def _sync_risk_band(score):
    if score >= 90:
        return "Clean"
    if score >= 75:
        return "Review"
    if score >= 50:
        return "High Risk"
    return "Blocked"


def _normalise_ledger_key(value):
    return str(value or "").strip().casefold()


def _ledger_names_for_ids(company, ids):
    clean_ids = {int(value) for value in ids if str(value or "").isdigit()}
    if not clean_ids:
        return {}
    return {
        ledger.pk: ledger.name
        for ledger in Ledger.objects.filter(company=company, pk__in=clean_ids)
    }


def _mapping_decision_payload(decision, ledger_names):
    decision = decision or {}
    action = str(decision.get("action") or "").strip() or "create"
    ledger_id = decision.get("id") if action == "map" else None
    ledger_id_int = int(ledger_id) if str(ledger_id or "").isdigit() else None
    if action == "map":
        target = (ledger_names.get(ledger_id_int) or f"Ledger #{ledger_id}") if ledger_id else "-"
    elif action == "ignore":
        target = "Ignored"
    else:
        target = "Create new"
    return {
        "action": action,
        "id": ledger_id_int,
        "target": target,
    }


def _prior_mapping_session(session):
    candidates = ImportSession.objects.filter(
        company=session.company,
        source_system=session.source_system,
        ledger_mapping__isnull=False,
    ).exclude(pk=session.pk)
    if session.source_company_guid:
        candidates = candidates.filter(source_company_guid=session.source_company_guid)
    return candidates.order_by("-created_at").first()


def _build_mapping_drift_report(session):
    current_mapping = session.ledger_mapping or {}
    prior = _prior_mapping_session(session)
    if not prior or not current_mapping:
        return {
            "available": bool(current_mapping),
            "prior_session_id": prior.pk if prior else None,
            "changed_count": 0,
            "new_count": 0,
            "missing_count": 0,
            "stable_count": 0,
            "changed": [],
            "new_ledgers": [],
            "missing_ledgers": [],
        }

    prior_mapping = prior.ledger_mapping or {}
    ids = []
    for decision in list(current_mapping.values()) + list(prior_mapping.values()):
        if (decision or {}).get("action") == "map" and (decision or {}).get("id"):
            ids.append((decision or {}).get("id"))
    ledger_names = _ledger_names_for_ids(session.company, ids)

    current_by_key = {_normalise_ledger_key(name): (name, decision) for name, decision in current_mapping.items()}
    prior_by_key = {_normalise_ledger_key(name): (name, decision) for name, decision in prior_mapping.items()}
    changed = []
    stable_count = 0

    for key, (current_name, current_decision) in current_by_key.items():
        if key not in prior_by_key:
            continue
        prior_name, prior_decision = prior_by_key[key]
        current_payload = _mapping_decision_payload(current_decision, ledger_names)
        prior_payload = _mapping_decision_payload(prior_decision, ledger_names)
        if current_payload["action"] == prior_payload["action"] and current_payload["id"] == prior_payload["id"]:
            stable_count += 1
            continue
        changed.append({
            "ledger": current_name or prior_name,
            "previous_action": prior_payload["action"],
            "previous_target": prior_payload["target"],
            "current_action": current_payload["action"],
            "current_target": current_payload["target"],
        })

    new_ledgers = [
        {"ledger": current_by_key[key][0]}
        for key in sorted(set(current_by_key) - set(prior_by_key))
    ]
    missing_ledgers = [
        {"ledger": prior_by_key[key][0]}
        for key in sorted(set(prior_by_key) - set(current_by_key))
    ]
    return {
        "available": True,
        "prior_session_id": prior.pk,
        "changed_count": len(changed),
        "new_count": len(new_ledgers),
        "missing_count": len(missing_ledgers),
        "stable_count": stable_count,
        "changed": changed[:20],
        "new_ledgers": new_ledgers[:20],
        "missing_ledgers": missing_ledgers[:20],
    }


def _session_parser(session):
    try:
        file_path = session.file.path
    except (NotImplementedError, ValueError):
        return None
    if not file_path or not os.path.exists(file_path):
        return None
    parser = SmartParser(file_path, session.file_type)
    parser.load_data()
    return parser


def _source_ref_for_group(group):
    meta = group.get("meta") or {}
    source_ref = str(meta.get("vch_no") or "").strip()
    if source_ref:
        return source_ref
    fingerprint = json.dumps(
        {
            "date": str(meta.get("date") or ""),
            "narration": str(meta.get("narration") or ""),
            "items": group.get("items") or [],
        },
        sort_keys=True,
        default=str,
    )
    return f"AUTO-{hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:16]}"


def _voucher_group_signature(group):
    meta = group.get("meta") or {}
    items = group.get("items") or []
    debit = sum(Decimal(str(item.get("debit") or 0)) for item in items)
    credit = sum(Decimal(str(item.get("credit") or 0)) for item in items)
    item_signature = sorted(
        [
            {
                "ledger": str(item.get("ledger") or "").strip().casefold(),
                "debit": f"{Decimal(str(item.get('debit') or 0)):.2f}",
                "credit": f"{Decimal(str(item.get('credit') or 0)):.2f}",
            }
            for item in items
        ],
        key=lambda item: (item["ledger"], item["debit"], item["credit"]),
    )
    payload = {
        "date": str(meta.get("date") or ""),
        "voucher_type": str(meta.get("vch_type") or "Journal").strip(),
        "debit": f"{debit:.2f}",
        "credit": f"{credit:.2f}",
        "items": item_signature,
    }
    return {
        **payload,
        "digest": hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest(),
        "item_count": len(items),
    }


def _existing_tally_voucher_signature(voucher):
    items = []
    debit = Decimal("0.00")
    credit = Decimal("0.00")
    for item in voucher.items.all():
        amount = item.amount or Decimal("0.00")
        if item.entry_type == "DR":
            debit += amount
        else:
            credit += amount
        items.append({
            "ledger": item.ledger.name.strip().casefold(),
            "debit": f"{amount:.2f}" if item.entry_type == "DR" else "0.00",
            "credit": f"{amount:.2f}" if item.entry_type == "CR" else "0.00",
        })
    payload = {
        "date": voucher.date.isoformat() if voucher.date else "",
        "voucher_type": voucher.voucher_type,
        "debit": f"{debit:.2f}",
        "credit": f"{credit:.2f}",
        "items": sorted(items, key=lambda item: (item["ledger"], item["debit"], item["credit"])),
    }
    return {
        **payload,
        "digest": hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest(),
        "item_count": len(items),
    }


def _current_voucher_groups(session):
    parser = _session_parser(session)
    if not parser or not session.detected_mapping:
        return [], "Source file is not available for delta analysis."
    try:
        groups = parser.group_vouchers(session.detected_mapping)
    except Exception as exc:
        return [], f"Could not parse source vouchers for delta analysis: {exc}"
    return groups, ""


def _build_voucher_delta_report(session):
    groups, unavailable_reason = _current_voucher_groups(session)
    if unavailable_reason:
        return {
            "available": False,
            "reason": unavailable_reason,
            "new_count": 0,
            "duplicate_count": 0,
            "changed_count": 0,
            "missing_existing_count": 0,
            "current_count": 0,
            "existing_count": 0,
            "new": [],
            "duplicates": [],
            "changed": [],
            "missing_existing": [],
        }

    current = {}
    for group in groups:
        ref = _source_ref_for_group(group)
        current[ref] = {
            "ref": ref,
            "meta": group.get("meta") or {},
            "signature": _voucher_group_signature(group),
        }

    existing_qs = (
        Voucher.objects.filter(company=session.company, source_system="tally")
        .prefetch_related("items__ledger")
        .order_by("date", "id")
    )
    if session.source_period_start and session.source_period_end:
        existing_qs = existing_qs.filter(date__range=(session.source_period_start, session.source_period_end))
    existing = {
        voucher.source_reference: {
            "voucher": voucher,
            "signature": _existing_tally_voucher_signature(voucher),
        }
        for voucher in existing_qs
        if voucher.source_reference
    }

    new = []
    duplicates = []
    changed = []
    for ref, item in current.items():
        if ref not in existing:
            new.append({
                "voucher_no": ref,
                "date": str(item["meta"].get("date") or ""),
                "voucher_type": item["signature"]["voucher_type"],
            })
            continue
        existing_item = existing[ref]
        if item["signature"]["digest"] == existing_item["signature"]["digest"]:
            duplicates.append({
                "voucher_no": ref,
                "date": existing_item["voucher"].date.isoformat(),
                "voucher_id": existing_item["voucher"].pk,
            })
        else:
            changed.append({
                "voucher_no": ref,
                "existing_voucher_id": existing_item["voucher"].pk,
                "existing_date": existing_item["signature"]["date"],
                "current_date": str(item["meta"].get("date") or ""),
                "existing_debit": existing_item["signature"]["debit"],
                "current_debit": item["signature"]["debit"],
                "existing_credit": existing_item["signature"]["credit"],
                "current_credit": item["signature"]["credit"],
            })

    missing_existing = [
        {
            "voucher_no": ref,
            "voucher_id": item["voucher"].pk,
            "date": item["voucher"].date.isoformat(),
        }
        for ref, item in existing.items()
        if ref not in current
    ]

    return {
        "available": True,
        "reason": "",
        "new_count": len(new),
        "duplicate_count": len(duplicates),
        "changed_count": len(changed),
        "missing_existing_count": len(missing_existing),
        "current_count": len(current),
        "existing_count": len(existing),
        "new": new[:20],
        "duplicates": duplicates[:20],
        "changed": changed[:20],
        "missing_existing": missing_existing[:20],
    }


def _build_sync_risk_report(session, report=None):
    report = report or session.validation_report or {}
    issues = []
    previous_sessions = ImportSession.objects.filter(company=session.company).exclude(pk=session.pk)

    duplicate_file_sessions = list(
        previous_sessions.filter(source_file_hash=session.source_file_hash)
        .exclude(source_file_hash="")
        .order_by("-created_at")[:5]
    )
    if duplicate_file_sessions:
        issues.append(_risk_issue(
            "duplicate_source_file",
            "critical",
            "Same source file already imported",
            "This file hash matches an earlier import session. Confirming again can duplicate vouchers.",
            count=len(duplicate_file_sessions),
            samples=[{"session": item.pk, "status": item.get_status_display()} for item in duplicate_file_sessions],
        ))

    duplicate_fingerprint_sessions = list(
        previous_sessions.filter(import_fingerprint=session.import_fingerprint)
        .exclude(import_fingerprint="")
        .order_by("-created_at")[:5]
    )
    if duplicate_fingerprint_sessions:
        issues.append(_risk_issue(
            "duplicate_sync_fingerprint",
            "critical",
            "Same sync fingerprint already exists",
            "Source system, company GUID, period, and file hash match another import session.",
            count=len(duplicate_fingerprint_sessions),
            samples=[{"session": item.pk, "status": item.get_status_display()} for item in duplicate_fingerprint_sessions],
        ))

    period_conflicts = []
    if session.source_company_guid and session.source_period_start and session.source_period_end:
        candidates = previous_sessions.filter(
            source_system=session.source_system,
            source_company_guid=session.source_company_guid,
            source_period_start__isnull=False,
            source_period_end__isnull=False,
        )
        for candidate in candidates:
            if _periods_overlap(
                session.source_period_start,
                session.source_period_end,
                candidate.source_period_start,
                candidate.source_period_end,
            ):
                period_conflicts.append(candidate)
    if period_conflicts:
        issues.append(_risk_issue(
            "period_overlap",
            "high",
            "Tally company period overlaps another import",
            "The same source company GUID has an overlapping period in another session.",
            count=len(period_conflicts),
            samples=[
                {
                    "session": item.pk,
                    "period": f"{item.source_period_start} to {item.source_period_end}",
                    "status": item.get_status_display(),
                }
                for item in period_conflicts[:5]
            ],
        ))

    existing_tally_vouchers = 0
    if session.source_period_start and session.source_period_end:
        existing_tally_vouchers = Voucher.objects.filter(
            company=session.company,
            source_system="tally",
            date__range=(session.source_period_start, session.source_period_end),
        ).count()
    if existing_tally_vouchers:
        severity = "high" if session.sync_mode == ImportSession.SYNC_REPLACE_PERIOD else "medium"
        issues.append(_risk_issue(
            "existing_tally_vouchers_in_period",
            severity,
            "Books already contain Tally vouchers for this period",
            "Review before confirming. Replace-period mode needs explicit CA approval and backup evidence.",
            count=existing_tally_vouchers,
            samples=[{"voucher_count": existing_tally_vouchers}],
        ))

    if session.sync_mode == ImportSession.SYNC_REPLACE_PERIOD:
        issues.append(_risk_issue(
            "replace_period_mode",
            "high",
            "Replace-period sync mode selected",
            "Use this only after backup/export evidence is available and the period boundary is verified.",
            count=1,
            samples=[{
                "period": f"{session.source_period_start or '-'} to {session.source_period_end or '-'}",
            }],
        ))

    ledger_summary = report.get("ledger_mapping_summary") or {}
    if ledger_summary.get("new"):
        issues.append(_risk_issue(
            "new_ledgers_to_suspense",
            "medium",
            "New ledgers will be created under Suspense",
            "Map material ledgers before import to reduce cleanup after migration.",
            count=ledger_summary.get("new", 0),
        ))
    if ledger_summary.get("ignored"):
        issues.append(_risk_issue(
            "ignored_ledgers",
            "high",
            "Ignored ledgers will drop rows",
            "Rows mapped to ignored ledgers will not enter books.",
            count=ledger_summary.get("ignored", 0),
        ))
    if session.duplicate_voucher_count:
        issues.append(_risk_issue(
            "duplicate_voucher_numbers",
            "high",
            "Duplicate voucher numbers in source file",
            "Duplicate source vouchers need review before confirmation.",
            count=session.duplicate_voucher_count,
        ))
    if session.unbalanced_voucher_count:
        issues.append(_risk_issue(
            "unbalanced_vouchers",
            "critical",
            "Unbalanced vouchers detected",
            "Unbalanced vouchers will be skipped and need correction.",
            count=session.unbalanced_voucher_count,
        ))
    if session.opening_balances_count:
        issues.append(_risk_issue(
            "opening_balance_rows",
            "medium",
            "Opening balance rows detected",
            "Opening balances will be posted through an adjustment voucher; compare with audited trial balance.",
            count=session.opening_balances_count,
        ))

    mapping_drift = report.get("mapping_drift") or _build_mapping_drift_report(session)
    if mapping_drift.get("changed_count"):
        issues.append(_risk_issue(
            "mapping_drift_changed",
            "high",
            "Ledger mapping changed since prior import",
            "One or more source ledgers now point to a different target/action than the previous Tally import.",
            count=mapping_drift["changed_count"],
            samples=mapping_drift.get("changed", []),
        ))
    if mapping_drift.get("new_count") and mapping_drift.get("prior_session_id"):
        issues.append(_risk_issue(
            "mapping_drift_new_ledgers",
            "medium",
            "New source ledgers since prior import",
            "New Tally ledger names appeared in this file and should be reviewed before confirming.",
            count=mapping_drift["new_count"],
            samples=mapping_drift.get("new_ledgers", []),
        ))

    voucher_delta = report.get("voucher_delta") or _build_voucher_delta_report(session)
    if voucher_delta.get("changed_count"):
        issues.append(_risk_issue(
            "voucher_delta_changed",
            "high",
            "Changed existing Tally vouchers",
            "This file contains voucher references that already exist but have changed date, amount, type, or line composition.",
            count=voucher_delta["changed_count"],
            samples=voucher_delta.get("changed", []),
        ))
    if voucher_delta.get("missing_existing_count") and session.sync_mode == ImportSession.SYNC_REPLACE_PERIOD:
        issues.append(_risk_issue(
            "voucher_delta_missing_existing",
            "high",
            "Existing Tally vouchers missing from replacement file",
            "Replace-period mode would need a decision for Tally vouchers already in books but absent from the current source file.",
            count=voucher_delta["missing_existing_count"],
            samples=voucher_delta.get("missing_existing", []),
        ))

    score = _score_sync_risks(issues)
    critical_count = sum(issue.get("count", 0) for issue in issues if issue.get("severity") == "critical")
    high_count = sum(issue.get("count", 0) for issue in issues if issue.get("severity") == "high")
    return {
        "score": score,
        "band": _sync_risk_band(score),
        "issues": issues,
        "issue_count": sum(issue.get("count", 0) for issue in issues),
        "critical_count": critical_count,
        "high_count": high_count,
        "duplicate_file_sessions": [item.pk for item in duplicate_file_sessions],
        "period_conflict_sessions": [item.pk for item in period_conflicts],
        "existing_tally_vouchers": existing_tally_vouchers,
    }


def _approval_blockers(session, report):
    blockers = []
    seen = set()
    sync_risk = report.get("sync_risk") or {}
    for issue in sync_risk.get("issues") or []:
        if issue.get("severity") not in {"critical", "high"}:
            continue
        key = issue.get("key") or issue.get("title")
        if key in seen:
            continue
        seen.add(key)
        blockers.append({
            "key": key,
            "title": issue.get("title", "Sync risk"),
            "severity": issue.get("severity", "high"),
            "count": issue.get("count", 0),
        })

    for issue in report.get("issues") or []:
        if issue.get("severity") not in {"critical", "high"}:
            continue
        key = issue.get("key") or issue.get("title")
        if key in seen:
            continue
        seen.add(key)
        blockers.append({
            "key": key,
            "title": issue.get("title", "Import cleanup risk"),
            "severity": issue.get("severity", "high"),
            "count": issue.get("count", 0),
        })

    if session.sync_mode == ImportSession.SYNC_REPLACE_PERIOD and "replace_period_mode" not in seen:
        blockers.append({
            "key": "replace_period_mode",
            "title": "Replace-period sync mode selected",
            "severity": "high",
            "count": 1,
        })
    return blockers


def _approval_snapshot_payload(session, report, blockers):
    sync_risk = report.get("sync_risk") or {}
    return {
        "source_system": session.source_system,
        "sync_mode": session.sync_mode,
        "source_company_guid": session.source_company_guid,
        "source_period_start": session.source_period_start.isoformat() if session.source_period_start else "",
        "source_period_end": session.source_period_end.isoformat() if session.source_period_end else "",
        "source_file_hash": session.source_file_hash,
        "import_fingerprint": session.import_fingerprint,
        "sync_risk": {
            "score": sync_risk.get("score", 100),
            "band": sync_risk.get("band", "Clean"),
            "critical_count": sync_risk.get("critical_count", 0),
            "high_count": sync_risk.get("high_count", 0),
            "issues": [
                {
                    "key": issue.get("key"),
                    "severity": issue.get("severity"),
                    "count": issue.get("count", 0),
                }
                for issue in sync_risk.get("issues") or []
            ],
        },
        "cleanup_score": report.get("cleanup_score", 100),
        "cleanup_issue_count": report.get("cleanup_issue_count", 0),
        "blocking_issue_count": report.get("blocking_issue_count", 0),
        "ledger_mapping_summary": report.get("ledger_mapping_summary") or {},
        "mapping_drift": {
            "changed_count": (report.get("mapping_drift") or {}).get("changed_count", 0),
            "new_count": (report.get("mapping_drift") or {}).get("new_count", 0),
            "missing_count": (report.get("mapping_drift") or {}).get("missing_count", 0),
            "prior_session_id": (report.get("mapping_drift") or {}).get("prior_session_id"),
        },
        "voucher_delta": {
            "new_count": (report.get("voucher_delta") or {}).get("new_count", 0),
            "duplicate_count": (report.get("voucher_delta") or {}).get("duplicate_count", 0),
            "changed_count": (report.get("voucher_delta") or {}).get("changed_count", 0),
            "missing_existing_count": (report.get("voucher_delta") or {}).get("missing_existing_count", 0),
        },
        "duplicate_voucher_count": session.duplicate_voucher_count,
        "unbalanced_voucher_count": session.unbalanced_voucher_count,
        "opening_balances_count": session.opening_balances_count,
        "approval_blockers": blockers,
    }


def _approval_evidence_hash(session, snapshot, checklist, note, approved_at, approved_by_id):
    payload = {
        "session": session.pk,
        "snapshot": snapshot,
        "checklist": checklist,
        "note": note,
        "approved_at": approved_at.isoformat() if approved_at else "",
        "approved_by_id": approved_by_id,
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _approval_checklist_items(checklist=None):
    checklist = checklist or {}
    return [
        {
            "key": key,
            "label": label,
            "checked": bool(checklist.get(key)),
        }
        for key, label in APPROVAL_CHECKLIST
    ]


def _has_independent_reviewer(session):
    return UserCompanyAccess.objects.filter(
        company=session.company,
        role__in=["Admin", "Accountant"],
    ).exclude(user=session.user).exists()


def _companies_for_user(user):
    if user.is_superuser:
        from core.models import Company
        return Company.objects.all().order_by("name")
    from core.models import Company
    return (
        Company.objects.filter(user_access__user=user)
        .distinct()
        .order_by("name")
    )


def _can_manage_company(user, company):
    if user.is_superuser:
        return True
    return UserCompanyAccess.objects.filter(
        user=user,
        company=company,
        role__in=["Admin", "Accountant"],
    ).exists()


def _build_approval_gate(session, report):
    blockers = _approval_blockers(session, report)
    sync_risk = report.get("sync_risk") or {}
    required = bool(
        blockers
        or sync_risk.get("band") in {"High Risk", "Blocked"}
        or sync_risk.get("critical_count", 0)
    )
    snapshot = _approval_snapshot_payload(session, report, blockers)
    approved = session.approval_status == ImportSession.APPROVAL_APPROVED
    stale = bool(required and approved and session.approval_snapshot != snapshot)
    checklist_items = _approval_checklist_items(session.approval_checklist)
    checked_count = sum(1 for item in checklist_items if item["checked"])
    can_confirm = (not required) or (approved and not stale)
    if not required:
        status_label = "Not required"
    elif approved and stale:
        status_label = "Approval stale"
    elif approved:
        status_label = "Approved"
    elif session.approval_status == ImportSession.APPROVAL_REVOKED:
        status_label = "Revoked"
    else:
        status_label = "Required"

    return {
        "required": required,
        "can_confirm": can_confirm,
        "status": session.approval_status,
        "status_label": status_label,
        "approved": approved,
        "stale": stale,
        "checked_count": checked_count,
        "total_checks": len(APPROVAL_CHECKLIST),
        "checklist": checklist_items,
        "blockers": blockers[:10],
        "approval_note": session.approval_note,
        "approved_by": session.approved_by.email if session.approved_by else "",
        "approved_at": session.approved_at.isoformat() if session.approved_at else "",
        "approval_evidence_hash": session.approval_evidence_hash,
        "independent_reviewer_available": _has_independent_reviewer(session),
    }


def _enhance_quality_report_with_mapping(session):
    report = dict(session.validation_report or {})
    issues = [
        issue for issue in report.get("issues", [])
        if issue.get("key") not in {"new_suspense_ledgers", "ignored_ledgers"}
    ]
    ledger_mapping = session.ledger_mapping or {}
    create_names = [
        str(name) for name, decision in ledger_mapping.items()
        if decision.get("action") == "create"
    ]
    mapped_names = [
        str(name) for name, decision in ledger_mapping.items()
        if decision.get("action") == "map"
    ]
    ignored_names = [
        str(name) for name, decision in ledger_mapping.items()
        if decision.get("action") == "ignore"
    ]

    if create_names:
        issues.append(_cleanup_issue(
            "new_suspense_ledgers",
            "medium",
            "New ledgers going to Suspense",
            "New ledgers will be created under Suspense Accounts until the CA maps them to the correct group.",
            [{"ledger": name} for name in create_names],
            count=len(create_names),
        ))
    if ignored_names:
        issues.append(_cleanup_issue(
            "ignored_ledgers",
            "high",
            "Ignored ledgers",
            "Rows mapped to ignored ledgers will not be imported. Confirm this is intentional.",
            [{"ledger": name} for name in ignored_names],
            count=len(ignored_names),
        ))

    score = _score_cleanup_issues(issues)
    report["issues"] = issues
    report["cleanup_score"] = score
    report["cleanup_band"] = _cleanup_band(score)
    report["cleanup_issue_count"] = sum(issue.get("count", 0) for issue in issues)
    report["blocking_issue_count"] = sum(
        issue.get("count", 0)
        for issue in issues
        if issue.get("severity") in {"critical", "high"}
    )
    report["ledger_mapping_summary"] = {
        "mapped": len(mapped_names),
        "new": len(create_names),
        "ignored": len(ignored_names),
        "total": len(ledger_mapping),
    }
    report["sync_control"] = report.get("sync_control") or _sync_control_summary(session)
    report["mapping_drift"] = _build_mapping_drift_report(session)
    report["voucher_delta"] = _build_voucher_delta_report(session)
    report["sync_risk"] = _build_sync_risk_report(session, report)
    report["approval_gate"] = _build_approval_gate(session, report)
    session.validation_report = report
    return report


def _tally_exit_band(score):
    if score >= 90:
        return "Ready"
    if score >= 75:
        return "Controlled"
    if score >= 55:
        return "Needs Cleanup"
    return "Blocked"


def _tally_exit_badge(score):
    if score >= 90:
        return "bg-success"
    if score >= 75:
        return "bg-primary"
    if score >= 55:
        return "bg-warning text-dark"
    return "bg-danger"


def _tally_connector_summary(company):
    connector = IntegrationConnector.objects.filter(
        company=company,
        connector_type=IntegrationConnector.TYPE_TALLY,
    ).first()
    if not connector:
        return {
            "ready": False,
            "label": "Not configured",
            "badge_class": "bg-danger",
        }
    if connector.is_ready:
        return {
            "ready": True,
            "label": connector.get_status_display(),
            "badge_class": connector.status_badge_class,
        }
    return {
        "ready": False,
        "label": connector.get_status_display(),
        "badge_class": connector.status_badge_class,
    }


def _parallel_gate(code, severity, title, passed, message, count=1):
    return {
        "code": code,
        "severity": "ok" if passed else severity,
        "title": title,
        "message": "Clear" if passed else message,
        "count": 0 if passed else count,
        "passed": bool(passed),
    }


def _parallel_run_score(gates):
    penalties = {"critical": 25, "high": 12, "medium": 5}
    score = 100
    for gate in gates:
        if gate["passed"]:
            continue
        score -= min(gate.get("count", 1) * penalties.get(gate.get("severity"), 3), 40)
    return max(score, 0)


def _parallel_run_band(score, critical_count):
    if critical_count:
        return "Blocked"
    if score >= 90:
        return "Ready"
    if score >= 75:
        return "Controlled"
    if score >= 55:
        return "Needs Rehearsal"
    return "Blocked"


def _build_parallel_run_report(company, sessions, latest, latest_report, operating, tally_connector):
    now = timezone.now()
    confirmed = sessions.filter(status="confirmed")
    latest_confirmed = confirmed.order_by("-created_at").first()
    incremental_confirmed = confirmed.filter(sync_mode__in=[ImportSession.SYNC_INCREMENTAL, ImportSession.SYNC_REPLACE_PERIOD]).exists()
    mapping_drift = (latest_report or {}).get("mapping_drift") or {}
    voucher_delta = (latest_report or {}).get("voucher_delta") or {}
    delta_available = voucher_delta.get("available", True)
    open_sync_tasks = PracticeTask.objects.filter(company=company).filter(
        models.Q(reference__startswith="IMPORTCLEAN:")
        | models.Q(reference__startswith="TALLYSYNC:")
        | models.Q(reference__startswith="TALLYEXIT:")
    ).exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED]).count()

    gates = [
        _parallel_gate(
            "confirmed_import",
            "critical",
            "At least one confirmed import",
            confirmed.exists(),
            "Confirm one migration import before the client can exit Tally.",
        ),
        _parallel_gate(
            "latest_import_confirmed",
            "high",
            "Latest import confirmed",
            bool(latest and latest.status == "confirmed"),
            "The latest Tally export is still open or failed; close the current migration decision.",
        ),
        _parallel_gate(
            "recent_parallel_run",
            "high",
            "Recent parallel run",
            bool(latest_confirmed and latest_confirmed.created_at >= now - timedelta(days=30)),
            "Run and confirm a fresh Tally parallel import within the last 30 days.",
        ),
        _parallel_gate(
            "incremental_sync_rehearsed",
            "high",
            "Incremental sync rehearsed",
            incremental_confirmed,
            "Confirm at least one incremental or replace-period sync, not only a one-time import.",
        ),
        _parallel_gate(
            "voucher_delta_clear",
            "high",
            "Voucher delta clear",
            bool(delta_available and not voucher_delta.get("changed_count") and not voucher_delta.get("missing_existing_count")),
            "Changed or missing Tally vouchers remain in the delta review.",
            count=(voucher_delta.get("changed_count") or 0) + (voucher_delta.get("missing_existing_count") or 1),
        ),
        _parallel_gate(
            "mapping_drift_clear",
            "high",
            "Ledger mapping stable",
            not mapping_drift.get("changed_count"),
            "Ledger mapping changed since the prior import and needs CA review.",
            count=mapping_drift.get("changed_count") or 1,
        ),
        _parallel_gate(
            "sync_tasks_clear",
            "high",
            "Sync debt clear",
            open_sync_tasks == 0,
            "Open migration cleanup, approval, or sync-risk tasks remain.",
            count=open_sync_tasks or 1,
        ),
        _parallel_gate(
            "tally_connector_ready",
            "critical",
            "Repeatable sync path ready",
            tally_connector["ready"],
            "Configure the Tally sync/import connector for repeatable parallel-run evidence.",
        ),
        _parallel_gate(
            "operating_readiness_clear",
            "critical",
            "Operating blockers clear",
            operating.get("critical_count", 0) == 0,
            "Operating readiness still has critical blockers.",
            count=operating.get("critical_count", 0) or 1,
        ),
    ]
    failed = [gate for gate in gates if not gate["passed"]]
    critical_count = sum(1 for gate in failed if gate["severity"] == "critical")
    high_count = sum(1 for gate in failed if gate["severity"] == "high")
    score = _parallel_run_score(gates)
    return {
        "score": score,
        "band": _parallel_run_band(score, critical_count),
        "badge_class": _tally_exit_badge(score),
        "gates": gates,
        "failed_gates": failed,
        "critical_count": critical_count,
        "high_count": high_count,
        "latest_confirmed": latest_confirmed,
        "open_sync_tasks": open_sync_tasks,
    }


def _switch_url(company, url_name, args=None):
    target = reverse(url_name, args=args or [])
    return f"{reverse('core:switch_company', args=[company.pk])}?next={target}"


def _build_tally_exit_row(company, user):
    sessions = ImportSession.objects.filter(company=company).select_related("user", "approved_by")
    latest = sessions.order_by("-created_at").first()
    confirmed_count = sessions.filter(status="confirmed").count()
    pending_count = sessions.exclude(status__in=["confirmed", "failed"]).count()
    tally_connector = _tally_connector_summary(company)
    operating = build_company_operating_readiness(company, user)
    top_gaps = []
    report = {}

    if latest:
        report = _enhance_quality_report_with_mapping(latest)
        latest.save(update_fields=["validation_report"])
        cleanup_score = report.get("cleanup_score", 100)
        sync_score = (report.get("sync_risk") or {}).get("score", 100)
        approval_gate = report.get("approval_gate") or {}
        migration_score = min(cleanup_score, sync_score)
        if latest.status != "confirmed":
            migration_score = min(migration_score, 72)
        if approval_gate.get("required") and not approval_gate.get("can_confirm"):
            migration_score = min(migration_score, 50)
        top_gaps.extend(report.get("issues") or [])
        top_gaps.extend((report.get("sync_risk") or {}).get("issues") or [])
        top_gaps = sorted(
            top_gaps,
            key=lambda issue: (
                {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(issue.get("severity"), 4),
                -(issue.get("count") or 0),
                issue.get("title") or "",
            ),
        )[:4]
        latest_url = _switch_url(
            company,
            "migration:summary" if latest.status == "confirmed" else "migration:preview",
            [latest.pk],
        )
    else:
        cleanup_score = 0
        sync_score = 0
        approval_gate = {}
        migration_score = 35
        top_gaps.append({
            "key": "no_import_session",
            "severity": "critical",
            "title": "No Tally import session",
            "message": "Upload the first Tally/Excel export before client exit can be certified.",
            "count": 1,
        })
        latest_url = ""

    if not tally_connector["ready"]:
        migration_score = min(migration_score, 82)
        top_gaps.append({
            "key": "tally_connector_not_ready",
            "severity": "high",
            "title": "Tally sync path not ready",
            "message": "Configure the Tally sync/import connector for repeatable migration evidence.",
            "count": 1,
        })

    parallel_run = _build_parallel_run_report(company, sessions, latest, report, operating, tally_connector)
    for gate in parallel_run["failed_gates"][:3]:
        top_gaps.append({
            "key": f"parallel_{gate['code']}",
            "severity": gate["severity"],
            "title": gate["title"],
            "message": gate["message"],
            "count": gate["count"],
        })
    top_gaps = sorted(
        top_gaps,
        key=lambda issue: (
            {"critical": 0, "high": 1, "medium": 2, "low": 3}.get(issue.get("severity"), 4),
            -(issue.get("count") or 0),
            issue.get("title") or "",
        ),
    )[:5]
    exit_score = min(migration_score, operating["score"], parallel_run["score"])
    return {
        "company": company,
        "latest": latest,
        "session_count": sessions.count(),
        "confirmed_count": confirmed_count,
        "pending_count": pending_count,
        "cleanup_score": cleanup_score,
        "sync_score": sync_score,
        "parallel_run": parallel_run,
        "operating_score": operating["score"],
        "exit_score": exit_score,
        "band": _tally_exit_band(exit_score),
        "badge_class": _tally_exit_badge(exit_score),
        "top_gaps": top_gaps[:5],
        "approval_gate": approval_gate,
        "tally_connector": tally_connector,
        "can_manage": _can_manage_company(user, company),
        "sessions_url": _switch_url(company, "migration:sessions"),
        "upload_url": _switch_url(company, "migration:upload"),
        "latest_url": latest_url,
    }


def _filter_tally_exit_rows(rows, params):
    q = (params.get("q") or "").strip().lower()
    band = (params.get("band") or "all").strip()
    if q:
        rows = [
            row for row in rows
            if q in row["company"].name.lower() or q in (row["company"].gstin or "").lower()
        ]
    if band != "all":
        rows = [row for row in rows if row["band"].lower().replace(" ", "_") == band]
    return rows, q, band


def _tally_exit_context(user, params):
    rows = [_build_tally_exit_row(company, user) for company in _companies_for_user(user)]
    rows, q, band = _filter_tally_exit_rows(rows, params)
    rows.sort(key=lambda row: (row["exit_score"], -len(row["top_gaps"]), row["company"].name))
    totals = {
        "clients": len(rows),
        "avg_score": round(sum(row["exit_score"] for row in rows) / len(rows)) if rows else 0,
        "ready": sum(1 for row in rows if row["band"] == "Ready"),
        "controlled": sum(1 for row in rows if row["band"] == "Controlled"),
        "needs_cleanup": sum(1 for row in rows if row["band"] == "Needs Cleanup"),
        "blocked": sum(1 for row in rows if row["band"] == "Blocked"),
        "open_sessions": sum(row["pending_count"] for row in rows),
        "confirmed_sessions": sum(row["confirmed_count"] for row in rows),
        "writable_clients": sum(1 for row in rows if row["can_manage"]),
    }
    export_query = {"q": q, "band": band}
    return {
        "rows": rows,
        "totals": totals,
        "q": q,
        "band_filter": band,
        "band_options": [
            ("all", "All Clients"),
            ("blocked", "Blocked"),
            ("needs_cleanup", "Needs Cleanup"),
            ("controlled", "Controlled"),
            ("ready", "Ready"),
        ],
        "export_query": urlencode({key: value for key, value in export_query.items() if value and value != "all"}),
    }


def _create_exit_task(company, user, *, reference, title, detail, priority=PracticeTask.PRIORITY_HIGH):
    task, created = PracticeTask.objects.get_or_create(
        company=company,
        reference=reference,
        defaults={
            "title": title,
            "task_type": PracticeTask.TYPE_OTHER,
            "priority": priority,
            "status": PracticeTask.STATUS_OPEN,
            "due_date": timezone.localdate() + timedelta(days=5),
            "assigned_to": user,
            "created_by": user,
            "description": detail,
        },
    )
    if not created and task.status in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED}:
        task.status = PracticeTask.STATUS_OPEN
        task.assigned_to = user
        task.save(update_fields=["status", "assigned_to", "updated_at"])
    return created


def _create_tally_exit_tasks(rows, user):
    created = 0
    existing = 0
    skipped = 0
    for row in rows:
        company = row["company"]
        if not row["can_manage"]:
            skipped += 1
            continue
        if not row["latest"]:
            was_created = _create_exit_task(
                company,
                user,
                reference=f"TALLYEXIT:{company.pk}:UPLOAD",
                title="Tally exit: Upload first migration export",
                detail="Upload the first Tally/Excel export so migration risk, cleanup, and approval can be reviewed.",
                priority=PracticeTask.PRIORITY_CRITICAL,
            )
            created += 1 if was_created else 0
            existing += 0 if was_created else 1
            continue

        cleanup_created, cleanup_existing = _create_cleanup_tasks_for_session(row["latest"], user)
        created += cleanup_created
        existing += cleanup_existing

        for issue in ((row["latest"].validation_report or {}).get("sync_risk") or {}).get("issues") or []:
            if issue.get("severity") not in {"critical", "high"}:
                continue
            was_created = _create_exit_task(
                company,
                user,
                reference=f"TALLYSYNC:{row['latest'].pk}:{issue.get('key')}",
                title=f"Tally sync risk: {issue.get('title')}",
                detail=issue.get("message") or "Review the Tally sync risk before confirming import.",
                priority=PracticeTask.PRIORITY_CRITICAL if issue.get("severity") == "critical" else PracticeTask.PRIORITY_HIGH,
            )
            created += 1 if was_created else 0
            existing += 0 if was_created else 1

        if row["approval_gate"].get("required") and not row["approval_gate"].get("can_confirm"):
            was_created = _create_exit_task(
                company,
                user,
                reference=f"TALLYEXIT:{row['latest'].pk}:APPROVAL",
                title="Tally exit: Complete CA approval gate",
                detail="High-risk migration import needs maker-checker approval before confirmation.",
                priority=PracticeTask.PRIORITY_CRITICAL,
            )
            created += 1 if was_created else 0
            existing += 0 if was_created else 1

        for gate in row["parallel_run"]["failed_gates"]:
            if gate["severity"] not in {"critical", "high"}:
                continue
            if gate["code"] == "sync_tasks_clear":
                continue
            was_created = _create_exit_task(
                company,
                user,
                reference=f"TALLYPARALLEL:{company.pk}:{gate['code']}",
                title=f"Tally parallel run: {gate['title']}",
                detail=gate["message"],
                priority=PracticeTask.PRIORITY_CRITICAL if gate["severity"] == "critical" else PracticeTask.PRIORITY_HIGH,
            )
            created += 1 if was_created else 0
            existing += 0 if was_created else 1

    return {"created": created, "existing": existing, "skipped": skipped}


def _tally_exit_csv_response(rows):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="tally-exit-control.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Company",
        "GSTIN",
        "Exit Score",
        "Band",
        "Cleanup Score",
        "Sync Risk Score",
        "Parallel Run Score",
        "Parallel Run Band",
        "Operating Score",
        "Sessions",
        "Confirmed Sessions",
        "Open Sessions",
        "Tally Connector",
        "Top Gaps",
    ])
    for row in rows:
        writer.writerow([
            row["company"].name,
            row["company"].gstin or "",
            row["exit_score"],
            row["band"],
            row["cleanup_score"],
            row["sync_score"],
            row["parallel_run"]["score"],
            row["parallel_run"]["band"],
            row["operating_score"],
            row["session_count"],
            row["confirmed_count"],
            row["pending_count"],
            row["tally_connector"]["label"],
            "; ".join(issue.get("title", "") for issue in row["top_gaps"]),
        ])
    return response


@login_required
def tally_exit_control(request):
    context = _tally_exit_context(request.user, request.GET if request.method == "GET" else request.POST)
    if request.method == "POST":
        result = _create_tally_exit_tasks(context["rows"], request.user)
        if result["created"]:
            messages.success(request, f"Created {result['created']} Tally exit task(s).")
        elif result["existing"]:
            messages.info(request, "Tally exit tasks already exist for the current filters.")
        else:
            messages.info(request, "No Tally exit tasks were needed for the current filters.")
        if result["created"] and result["existing"]:
            messages.info(request, f"{result['existing']} existing task(s) were left unchanged.")
        if result["skipped"]:
            messages.warning(request, f"{result['skipped']} read-only client(s) were skipped.")
        redirect_url = reverse("migration:exit_control")
        if context["export_query"]:
            redirect_url = f"{redirect_url}?{context['export_query']}"
        return redirect(redirect_url)
    if request.GET.get("export") == "csv":
        return _tally_exit_csv_response(context["rows"])
    return render(request, "migration/exit_control.html", context)


@login_required
@write_required
def import_sessions(request):
    sessions = list(
        ImportSession.objects.filter(company=request.current_company)
        .select_related("user")
        .order_by("-created_at")[:50]
    )
    for session in sessions:
        session.enhanced_report = _enhance_quality_report_with_mapping(session)
    return render(request, 'migration/sessions.html', {
        'sessions': sessions,
    })


def _priority_for_cleanup_issue(issue):
    severity = issue.get("severity")
    if severity == "critical":
        return PracticeTask.PRIORITY_CRITICAL
    if severity == "high":
        return PracticeTask.PRIORITY_HIGH
    if severity == "medium":
        return PracticeTask.PRIORITY_NORMAL
    return PracticeTask.PRIORITY_LOW


def _due_date_for_cleanup_issue(issue):
    severity = issue.get("severity")
    days = 1 if severity == "critical" else 3 if severity == "high" else 7
    return timezone.localdate() + timezone.timedelta(days=days)


def _create_cleanup_tasks_for_session(session, user):
    report = _enhance_quality_report_with_mapping(session)
    created_count = 0
    existing_count = 0
    for issue in report.get("issues", []):
        if not issue.get("count"):
            continue
        samples = issue.get("samples") or []
        sample_text = "\n".join(
            f"- {', '.join(f'{key}: {value}' for key, value in sample.items())}"
            for sample in samples[:5]
        )
        task, created = PracticeTask.objects.get_or_create(
            company=session.company,
            reference=f"IMPORTCLEAN:{session.pk}:{issue['key']}",
            defaults={
                "title": f"Import cleanup: {issue['title']}",
                "task_type": PracticeTask.TYPE_OTHER,
                "priority": _priority_for_cleanup_issue(issue),
                "status": PracticeTask.STATUS_OPEN,
                "due_date": _due_date_for_cleanup_issue(issue),
                "created_by": user,
                "description": (
                    f"Import session #{session.pk}\n"
                    f"Severity: {issue.get('severity')}\n"
                    f"Count: {issue.get('count')}\n\n"
                    f"{issue.get('message')}\n\n"
                    f"Samples:\n{sample_text or '-'}"
                ),
            },
        )
        if created:
            created_count += 1
            AuditLog.objects.create(
                company=session.company,
                user=user,
                action=AuditLog.ACTION_CREATE,
                model_name="PracticeTask",
                record_id=task.pk,
                object_repr=task.title[:200],
                old_data={},
                new_data={
                    "source": "migration_cleanup",
                    "import_session_id": session.pk,
                    "issue_key": issue["key"],
                    "issue_count": issue.get("count"),
                },
            )
        else:
            existing_count += 1
    session.save(update_fields=["validation_report"])
    return created_count, existing_count

@login_required
@write_required
def upload_migration(request):
    if request.method == 'POST':
        form = MigrationUploadForm(request.POST, request.FILES)
        if form.is_valid():
            session = form.save(commit=False)
            session.user = request.user
            session.company = request.current_company
            session.source_file_hash = _hash_uploaded_file(session.file)
            session.import_fingerprint = _import_fingerprint(session)
            
            ext = os.path.splitext(session.file.name)[1].lower()
            if ext in ['.xlsx', '.xls']:
                session.file_type = 'excel'
            elif ext == '.csv':
                session.file_type = 'csv'
            else:
                messages.error(request, "Unsupported file type.")
                return redirect('migration:upload')
            
            session.save()
            duplicate_session = (
                ImportSession.objects.filter(
                    company=request.current_company,
                    source_file_hash=session.source_file_hash,
                )
                .exclude(pk=session.pk)
                .order_by("-created_at")
                .first()
            )
            if duplicate_session:
                messages.warning(
                    request,
                    f"This source file matches import session #{duplicate_session.pk}. Review before confirming to avoid duplicate vouchers.",
                )
            
            # Initial Parse
            parser = SmartParser(session.file.path, session.file_type)
            mapping = parser.detect_columns()
            session.detected_mapping = mapping
            session.total_rows = len(parser.df)
            
            # Identify Unique Ledgers for Mapping
            ledger_col = mapping.get('ledger')
            if not ledger_col:
                messages.error(request, "Could not detect Ledger column. Check headers.")
                return redirect('migration:upload')
                
            file_ledgers = parser.df[ledger_col].unique().tolist()
            file_ledgers = [str(l).strip() for l in file_ledgers if l]
            
            # Initial Mapping State
            initial_mapping = {}
            existing_ledgers = {l.name.lower(): l.id for l in Ledger.objects.filter(company=request.current_company)}
            
            for l_name in file_ledgers:
                if l_name.lower() in existing_ledgers:
                    initial_mapping[l_name] = {'action': 'map', 'id': existing_ledgers[l_name.lower()]}
                else:
                    initial_mapping[l_name] = {'action': 'create', 'id': None}
            
            session.ledger_mapping = initial_mapping
            quality_report = parser.build_quality_report(mapping)
            quality_report["sync_control"] = _sync_control_summary(session, duplicate_session)
            session.validation_report = quality_report
            session.duplicate_voucher_count = quality_report.get('duplicate_voucher_count', 0)
            session.unbalanced_voucher_count = quality_report.get('unbalanced_voucher_count', 0)
            _enhance_quality_report_with_mapping(session)
            session.status = 'parsed'
            session.save()
            
            return redirect('migration:map_ledgers', pk=session.pk)
    else:
        form = MigrationUploadForm()
    
    return render(request, 'migration/upload.html', {'form': form})

@login_required
@write_required
def map_ledgers(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    existing_ledgers = Ledger.objects.filter(company=request.current_company).order_by('name')
    
    if request.method == 'POST':
        mapping = {}
        for l_name in session.ledger_mapping.keys():
            action = request.POST.get(f'action_{l_name}')
            target_id = request.POST.get(f'target_{l_name}')
            mapping[l_name] = {
                'action': action,
                'id': target_id if action == 'map' else None
            }
        session.ledger_mapping = mapping
        approval_reset = session.approval_status == ImportSession.APPROVAL_APPROVED
        if approval_reset:
            session.approval_status = ImportSession.APPROVAL_PENDING
            session.approval_checklist = {}
            session.approval_note = ""
            session.approval_snapshot = {}
            session.approval_evidence_hash = ""
            session.approved_by = None
            session.approved_at = None
            session.approval_revoked_by = request.user
            session.approval_revoked_at = timezone.now()
            session.approval_revoke_note = "Approval reset because ledger mapping changed."
        session.save()
        if approval_reset:
            AuditLog.objects.create(
                company=session.company,
                user=request.user,
                action=AuditLog.ACTION_UPDATE,
                model_name="ImportSession",
                record_id=session.pk,
                object_repr=f"Import session #{session.pk} approval reset",
                old_data={"approval_status": ImportSession.APPROVAL_APPROVED},
                new_data={"approval_status": session.approval_status, "reason": session.approval_revoke_note},
            )
            messages.warning(request, "Previous CA approval was reset because ledger mapping changed.")
        
        parser = SmartParser(session.file.path, session.file_type)
        session.raw_preview = parser.get_preview_data(session.detected_mapping)
        vch_groups = parser.group_vouchers(session.detected_mapping)
        session.vouchers_count = len(vch_groups)
        opening_balances = parser.get_opening_balances(session.detected_mapping)
        session.opening_balances_count = len(opening_balances)
        session.detected_opening_balances = opening_balances[:20]
        quality_report = parser.build_quality_report(session.detected_mapping)
        quality_report["sync_control"] = _sync_control_summary(session)
        session.validation_report = quality_report
        session.duplicate_voucher_count = quality_report.get('duplicate_voucher_count', 0)
        session.unbalanced_voucher_count = quality_report.get('unbalanced_voucher_count', 0)
        _enhance_quality_report_with_mapping(session)
        session.save()
        
        return redirect('migration:preview', pk=session.pk)

    return render(request, 'migration/map_ledgers.html', {
        'session': session,
        'existing_ledgers': existing_ledgers
    })

@login_required
@write_required
def preview_migration(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    _enhance_quality_report_with_mapping(session)
    session.save(update_fields=["validation_report"])
    return render(request, 'migration/preview.html', {
        'session': session,
        'mapping': session.detected_mapping,
        'preview_data': session.raw_preview,
        'opening_balances': session.detected_opening_balances,
        'ledger_mapping': session.ledger_mapping
    })

@login_required
@write_required
def confirm_import(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    if request.method != 'POST': return redirect('migration:preview', pk=pk)
    if session.status == 'confirmed':
        messages.info(request, "This import session has already been confirmed.")
        return redirect('migration:summary', pk=session.pk)
    report = _enhance_quality_report_with_mapping(session)
    session.save(update_fields=["validation_report"])
    approval_gate = report.get("approval_gate") or {}
    if not approval_gate.get("can_confirm", True):
        messages.error(
            request,
            "CA approval is required before this high-risk import can be confirmed.",
        )
        return redirect('migration:preview', pk=session.pk)

    try:
        parser = SmartParser(session.file.path, session.file_type)
        mapping = session.detected_mapping
        l_map = session.ledger_mapping
        company = request.current_company
        
        suspense_group, _ = AccountGroup.objects.get_or_create(company=company, name="Suspense Accounts", defaults={'nature': 'Asset'})
        equity_group, _ = AccountGroup.objects.get_or_create(company=company, name="Equity", defaults={'nature': 'Equity'})
        adjustment_ledger, _ = Ledger.objects.get_or_create(company=company, name="Opening Balance Adjustment", defaults={'account_group': equity_group})
        
        def get_mapped_ledger(file_ledger_name):
            decision = l_map.get(str(file_ledger_name).strip())
            if not decision or decision['action'] == 'ignore': return None
            if decision['action'] == 'map': return Ledger.objects.get(id=decision['id'], company=company)
            ledger, _ = Ledger.objects.get_or_create(company=company, name=str(file_ledger_name).strip(), defaults={'account_group': suspense_group})
            return ledger

        opening_count = 0
        success_vch = 0
        skipped_rows = []
        total_dr = Decimal('0.00')
        total_cr = Decimal('0.00')

        with transaction.atomic():
            # 1. Opening Balances
            opening_data = parser.get_opening_balances(mapping)
            if opening_data:
                opening_vch = Voucher.objects.create(company=company, voucher_type='Journal', number=f"OPN-{session.id}", date=session.created_at.date(), narration=f"Opening Balances Session #{session.id}")
                for ob in opening_data:
                    ledger = get_mapped_ledger(ob['ledger'])
                    if not ledger: continue
                    
                    amt = Decimal(str(ob['debit'] or ob['credit']))
                    ledger.opening_balance = amt if ob['credit'] > 0 else -amt
                    ledger.save()
                    
                    e_type = 'DR' if ob['debit'] > 0 else 'CR'
                    VoucherItem.objects.create(voucher=opening_vch, ledger=ledger, entry_type=e_type, amount=amt)
                    VoucherItem.objects.create(voucher=opening_vch, ledger=adjustment_ledger, entry_type='CR' if e_type == 'DR' else 'DR', amount=amt)
                    total_dr += amt; total_cr += amt
                    opening_count += 1

            # 2. Vouchers
            seen_source_refs = set()
            for group_idx, group in enumerate(parser.group_vouchers(mapping)):
                meta = group['meta']; items = group['items']
                source_ref = str(meta.get('vch_no') or '').strip()
                if not source_ref:
                    fingerprint = json.dumps(
                        {
                            "date": str(meta.get('date') or ''),
                            "narration": str(meta.get('narration') or ''),
                            "items": items,
                        },
                        sort_keys=True,
                        default=str,
                    )
                    source_ref = f"AUTO-{hashlib.sha1(fingerprint.encode('utf-8')).hexdigest()[:16]}"
                if source_ref:
                    if source_ref in seen_source_refs or Voucher.objects.filter(
                        company=company,
                        source_system='tally',
                        source_reference=source_ref,
                    ).exists():
                        skipped_rows.append({
                            'id': group_idx,
                            'vch_no': source_ref,
                            'date': str(meta['date']),
                            'reason': f'Duplicate Tally voucher reference: {source_ref}',
                            'items': [{'ledger': str(item['ledger']), 'debit': float(item['debit']), 'credit': float(item['credit'])} for item in items]
                        })
                        continue
                    seen_source_refs.add(source_ref)

                valid_items = []
                for item in items:
                    ledger = get_mapped_ledger(item['ledger'])
                    if ledger: valid_items.append((ledger, item))
                
                if not valid_items: continue
                
                g_dr = sum(Decimal(str(i[1]['debit'])) for i in valid_items)
                g_cr = sum(Decimal(str(i[1]['credit'])) for i in valid_items)
                
                if abs(g_dr - g_cr) > Decimal('0.01'):
                    skipped_rows.append({
                        'id': group_idx,
                        'vch_no': meta['vch_no'],
                        'date': str(meta['date']),
                        'reason': f'Unbalanced: DR {g_dr} / CR {g_cr}',
                        'items': [{'ledger': str(i[1]['ledger']), 'debit': float(i[1]['debit']), 'credit': float(i[1]['credit'])} for i in valid_items]
                    })
                    continue
                
                vch = Voucher.objects.create(
                    company=company,
                    voucher_type=meta['vch_type'] or 'Journal',
                    number=meta['vch_no'] or "",
                    date=pd.to_datetime(meta['date']).date() if meta['date'] else session.created_at.date(),
                    narration=meta['narration'],
                    source_system='tally',
                    source_reference=source_ref,
                )
                for ledger, item in valid_items:
                    amt = Decimal(str(item['debit'] or item['credit']))
                    e_type = 'DR' if item['debit'] > 0 else 'CR'
                    VoucherItem.objects.create(voucher=vch, ledger=ledger, entry_type=e_type, amount=amt)
                    if e_type == 'DR': total_dr += amt
                    else: total_cr += amt
                success_vch += 1

        session.vouchers_count = success_vch
        session.opening_balances_count = opening_count
        session.total_debit = total_dr; session.total_credit = total_cr
        session.skipped_rows = skipped_rows
        session.status = 'confirmed'; session.save()
        return redirect('migration:summary', pk=session.pk)
        
    except Exception as e:
        session.status = 'failed'; session.save()
        messages.error(request, f"Import failed: {str(e)}")
        return redirect('migration:preview', pk=session.pk)

@login_required
@write_required
def import_summary(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    _enhance_quality_report_with_mapping(session)
    session.save(update_fields=["validation_report"])
    existing_ledgers = Ledger.objects.filter(company=request.current_company).order_by('name')
    is_balanced = abs(session.total_debit - session.total_credit) < Decimal('0.01')
    return render(request, 'migration/summary.html', {
        'session': session, 
        'is_balanced': is_balanced,
        'existing_ledgers': existing_ledgers
    })


@login_required
@write_required
@require_POST
def approve_import(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    if session.status == 'confirmed':
        messages.info(request, "This import has already been confirmed.")
        return redirect('migration:summary', pk=session.pk)

    report = _enhance_quality_report_with_mapping(session)
    gate = report.get("approval_gate") or {}
    if not gate.get("required"):
        messages.info(request, "This import does not require CA approval.")
        session.save(update_fields=["validation_report"])
        return redirect('migration:preview', pk=session.pk)

    checklist = {key: request.POST.get(key) == "on" for key, _label in APPROVAL_CHECKLIST}
    missing = [label for key, label in APPROVAL_CHECKLIST if not checklist.get(key)]
    note = (request.POST.get("approval_note") or "").strip()
    if missing:
        messages.error(request, "Complete every CA approval checklist item before approving.")
        session.save(update_fields=["validation_report"])
        return redirect('migration:preview', pk=session.pk)
    if len(note) < 10:
        messages.error(request, "Add a meaningful approval note before approving.")
        session.save(update_fields=["validation_report"])
        return redirect('migration:preview', pk=session.pk)
    if session.user_id == request.user.id and gate.get("independent_reviewer_available"):
        messages.error(request, "Independent maker-checker approval is required for this import.")
        session.save(update_fields=["validation_report"])
        return redirect('migration:preview', pk=session.pk)

    blockers = _approval_blockers(session, report)
    snapshot = _approval_snapshot_payload(session, report, blockers)
    approved_at = timezone.now()
    session.approval_status = ImportSession.APPROVAL_APPROVED
    session.approval_checklist = checklist
    session.approval_note = note
    session.approval_snapshot = snapshot
    session.approved_by = request.user
    session.approved_at = approved_at
    session.approval_revoked_by = None
    session.approval_revoked_at = None
    session.approval_revoke_note = ""
    session.approval_evidence_hash = _approval_evidence_hash(
        session,
        snapshot,
        checklist,
        note,
        approved_at,
        request.user.pk,
    )
    _enhance_quality_report_with_mapping(session)
    session.save(update_fields=[
        "approval_status",
        "approval_checklist",
        "approval_note",
        "approval_snapshot",
        "approval_evidence_hash",
        "approved_by",
        "approved_at",
        "approval_revoked_by",
        "approval_revoked_at",
        "approval_revoke_note",
        "validation_report",
    ])
    AuditLog.objects.create(
        company=session.company,
        user=request.user,
        action=AuditLog.ACTION_UPDATE,
        model_name="ImportSession",
        record_id=session.pk,
        object_repr=f"Import session #{session.pk} approved",
        old_data={"approval_status": ImportSession.APPROVAL_PENDING},
        new_data={
            "approval_status": session.approval_status,
            "approval_evidence_hash": session.approval_evidence_hash,
            "risk_score": snapshot["sync_risk"]["score"],
            "risk_band": snapshot["sync_risk"]["band"],
            "blockers": blockers,
        },
    )
    messages.success(request, "CA approval recorded. This import can now be confirmed.")
    return redirect('migration:preview', pk=session.pk)


@login_required
@write_required
@require_POST
def revoke_import_approval(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    if session.status == 'confirmed':
        messages.error(request, "Confirmed import approvals cannot be revoked.")
        return redirect('migration:summary', pk=session.pk)
    if session.approval_status != ImportSession.APPROVAL_APPROVED:
        messages.info(request, "There is no active approval to revoke.")
        return redirect('migration:preview', pk=session.pk)

    note = (request.POST.get("revoke_note") or "Approval revoked before import confirmation.").strip()
    old_hash = session.approval_evidence_hash
    session.approval_status = ImportSession.APPROVAL_REVOKED
    session.approval_revoked_by = request.user
    session.approval_revoked_at = timezone.now()
    session.approval_revoke_note = note
    _enhance_quality_report_with_mapping(session)
    session.save(update_fields=[
        "approval_status",
        "approval_revoked_by",
        "approval_revoked_at",
        "approval_revoke_note",
        "validation_report",
    ])
    AuditLog.objects.create(
        company=session.company,
        user=request.user,
        action=AuditLog.ACTION_UPDATE,
        model_name="ImportSession",
        record_id=session.pk,
        object_repr=f"Import session #{session.pk} approval revoked",
        old_data={"approval_status": ImportSession.APPROVAL_APPROVED, "approval_evidence_hash": old_hash},
        new_data={"approval_status": session.approval_status, "reason": note},
    )
    messages.warning(request, "CA approval revoked. The import is blocked until approved again.")
    return redirect('migration:preview', pk=session.pk)


@login_required
@write_required
def download_import_template(request):
    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="akshaya_vistara_tally_import_template.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Date",
        "Voucher No",
        "Voucher Type",
        "Ledger",
        "Debit",
        "Credit",
        "Narration",
        "GSTIN",
        "PAN",
        "Email",
        "WhatsApp",
        "Account Group",
    ])
    writer.writerow([
        "2026-04-01",
        "S-001",
        "Sales",
        "ABC Traders",
        "1180.00",
        "",
        "Sales invoice S-001",
        "27AAAAA0000A1Z5",
        "AAAAA0000A",
        "accounts@example.com",
        "+919876543210",
        "Sundry Debtors",
    ])
    writer.writerow([
        "2026-04-01",
        "S-001",
        "Sales",
        "Sales Ledger",
        "",
        "1000.00",
        "Sales invoice S-001",
        "",
        "",
        "",
        "",
        "Sales Accounts",
    ])
    writer.writerow([
        "2026-04-01",
        "S-001",
        "Sales",
        "Output GST 18%",
        "",
        "180.00",
        "Sales invoice S-001",
        "",
        "",
        "",
        "",
        "Duties & Taxes",
    ])
    return response


@login_required
@write_required
def download_cleanup_issues(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    report = _enhance_quality_report_with_mapping(session)
    session.save(update_fields=["validation_report"])

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="import_cleanup_session_{session.pk}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Issue",
        "Severity",
        "Count",
        "Message",
        "Sample Row",
        "Sample Ledger",
        "Sample Voucher",
        "Sample Value",
        "Sample Debit",
        "Sample Credit",
        "Sample Difference",
    ])
    for issue in report.get("issues", []):
        samples = issue.get("samples") or [{}]
        for sample in samples:
            writer.writerow([
                issue.get("title", ""),
                issue.get("severity", ""),
                issue.get("count", 0),
                issue.get("message", ""),
                sample.get("row", ""),
                sample.get("ledger", ""),
                sample.get("voucher_no", ""),
                sample.get("value", ""),
                sample.get("debit", ""),
                sample.get("credit", ""),
                sample.get("difference", ""),
            ])
    return response


@login_required
@write_required
def download_sync_risk(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    report = _enhance_quality_report_with_mapping(session)
    session.save(update_fields=["validation_report"])
    risk = report.get("sync_risk", {})
    gate = report.get("approval_gate", {})

    response = HttpResponse(content_type="text/csv")
    response["Content-Disposition"] = f'attachment; filename="tally_sync_risk_session_{session.pk}.csv"'
    writer = csv.writer(response)
    writer.writerow([
        "Session",
        "Source System",
        "Sync Mode",
        "Source Company GUID",
        "Source Period Start",
        "Source Period End",
        "File SHA256",
        "Risk Score",
        "Risk Band",
        "Approval Required",
        "Approval Status",
        "Approval Evidence Hash",
        "Approved By",
        "Issue",
        "Severity",
        "Count",
        "Message",
        "Sample",
    ])
    issues = risk.get("issues") or []
    if not issues:
        writer.writerow([
            session.pk,
            session.get_source_system_display(),
            session.get_sync_mode_display(),
            session.source_company_guid,
            session.source_period_start or "",
            session.source_period_end or "",
            session.source_file_hash,
            risk.get("score", 100),
            risk.get("band", "Clean"),
            "Yes" if gate.get("required") else "No",
            gate.get("status_label", "Not required"),
            gate.get("approval_evidence_hash", ""),
            gate.get("approved_by", ""),
            "No material sync risks",
            "",
            0,
            "",
            "",
        ])
        return response

    for issue in issues:
        samples = issue.get("samples") or [{}]
        for sample in samples:
            writer.writerow([
                session.pk,
                session.get_source_system_display(),
                session.get_sync_mode_display(),
                session.source_company_guid,
                session.source_period_start or "",
                session.source_period_end or "",
                session.source_file_hash,
                risk.get("score", 100),
                risk.get("band", "Clean"),
                "Yes" if gate.get("required") else "No",
                gate.get("status_label", "Not required"),
                gate.get("approval_evidence_hash", ""),
                gate.get("approved_by", ""),
                issue.get("title", ""),
                issue.get("severity", ""),
                issue.get("count", 0),
                issue.get("message", ""),
                json.dumps(sample, default=str),
            ])
    return response


@login_required
@write_required
@require_POST
def create_cleanup_tasks(request, pk):
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    created_count, existing_count = _create_cleanup_tasks_for_session(session, request.user)
    if created_count:
        messages.success(request, f"Created {created_count} import cleanup task(s).")
    else:
        messages.info(request, "Cleanup tasks already exist for the current import issues.")
    if existing_count:
        messages.info(request, f"{existing_count} existing cleanup task(s) were left unchanged.")

    next_page = request.POST.get("next") or "preview"
    if next_page == "summary":
        return redirect('migration:summary', pk=session.pk)
    return redirect('migration:preview', pk=session.pk)

@login_required
@write_required
def reprocess_row(request, pk):
    if request.method != 'POST': return JsonResponse({'success': False})
    
    session = get_object_or_404(ImportSession, pk=pk, company=request.current_company)
    data = json.loads(request.body)
    row_id = data.get('row_id')
    corrected_items = data.get('items')
    vch_date = data.get('date')
    vch_no = data.get('vch_no')

    try:
        total_dr = sum(Decimal(str(i['debit'])) for i in corrected_items)
        total_cr = sum(Decimal(str(i['credit'])) for i in corrected_items)

        if abs(total_dr - total_cr) > Decimal('0.01'):
            return JsonResponse({'success': False, 'error': f'Still unbalanced: DR {total_dr} / CR {total_cr}'})

        with transaction.atomic():
            vch = Voucher.objects.create(
                company=request.current_company,
                voucher_type='Journal',
                number=vch_no or "",
                date=pd.to_datetime(vch_date).date() if vch_date else session.created_at.date(),
                narration="Fixed & Reprocessed during import"
            )
            for item in corrected_items:
                ledger = Ledger.objects.get(id=item['ledger_id'], company=request.current_company)
                amt = Decimal(str(item['debit'] or item['credit']))
                e_type = 'DR' if Decimal(str(item['debit'])) > 0 else 'CR'
                VoucherItem.objects.create(voucher=vch, ledger=ledger, entry_type=e_type, amount=amt)
            
            # Update session totals
            session.vouchers_count += 1
            session.total_debit += total_dr
            session.total_credit += total_cr
            
            # Remove from skipped_rows
            new_skipped = [r for r in session.skipped_rows if str(r.get('id')) != str(row_id)]
            session.skipped_rows = new_skipped
            session.save()

        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)})
