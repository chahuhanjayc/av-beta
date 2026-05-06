import hashlib
import json
import re
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from .evidence_vault import verify_vault_chain
from .go_live_certificate import build_go_live_certificate, go_live_certificate_payload
from .production_trust import list_backup_manifests, list_restore_drills, list_scheduled_backup_runs
from .system_observability import build_system_observability, observability_public_payload


GO_LIVE_PACK_SCHEMA_VERSION = 1


def go_live_pack_directory(output_dir=None):
    return Path(output_dir or getattr(settings, "GO_LIVE_PACK_DIR", settings.BASE_DIR / "go_live_packs"))


def build_go_live_evidence_pack(*, company, user=None, include_deploy=True, backup_dir=None, as_of=None):
    if not company:
        raise ValueError("Select a company before generating a Go-Live Evidence Pack.")

    generated_at = as_of or timezone.now()
    certificate = build_go_live_certificate(
        company=company,
        include_deploy=include_deploy,
        as_of=generated_at,
    )
    observability = build_system_observability(
        company=company,
        include_deploy=include_deploy,
        as_of=generated_at,
    )
    pack = {
        "pack_schema_version": GO_LIVE_PACK_SCHEMA_VERSION,
        "generated_at": generated_at.isoformat(),
        "generated_by": getattr(user, "email", "") if getattr(user, "is_authenticated", False) else "",
        "redaction": {
            "raw_credentials_included": False,
            "raw_provider_payloads_included": False,
            "artifact_strategy": "Artifact names, statuses, references, and SHA-256 hashes only.",
        },
        "company": _company_snapshot(company),
        "certificate": go_live_certificate_payload(certificate),
        "observability": observability_public_payload(observability),
        "recovery": _recovery_snapshot(backup_dir=backup_dir),
        "evidence_vault": _vault_snapshot(verify_vault_chain(company)),
        "integrations": _integration_snapshot(company, as_of=generated_at),
        "signoff": {
            "can_go_live": certificate["status"] in {"certified", "conditional"},
            "status": certificate["status"],
            "status_label": certificate["status_label"],
            "score": certificate["score"],
            "blockers": certificate["totals"]["blocked"],
            "warnings": certificate["totals"]["watch"],
            "required_actions": [
                {
                    "area": gate["area"],
                    "gate": gate["name"],
                    "status": gate["status"],
                    "action": gate["recommendation"],
                }
                for gate in certificate["gates"]
                if gate["status"] in {"blocked", "watch"}
            ],
        },
    }
    pack["pack_id"] = f"GLP-{_pack_hash(pack)[:16].upper()}"
    pack["sha256"] = _pack_hash(pack)
    return pack


def go_live_evidence_pack_bytes(pack):
    return json.dumps(pack, indent=2, sort_keys=True, default=str).encode("utf-8")


def go_live_evidence_pack_filename(pack):
    company = pack.get("company") or {}
    raw_code = company.get("short_code") or company.get("name") or f"company-{company.get('id', '')}"
    code = _safe_filename_part(raw_code) or "company"
    timestamp = _safe_filename_part(pack.get("generated_at", "")[:19].replace("T", "-")) or "generated"
    return f"go-live-evidence-{code}-{timestamp}-{pack.get('pack_id', 'GLP').lower()}.json"


def write_go_live_evidence_pack(pack, *, output_dir=None):
    directory = go_live_pack_directory(output_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / go_live_evidence_pack_filename(pack)
    path.write_bytes(go_live_evidence_pack_bytes(pack))
    return path


def _company_snapshot(company):
    return {
        "id": company.pk,
        "name": company.name,
        "short_code": company.short_code or "",
        "gstin": company.gstin or "",
        "tan": company.tan or "",
    }


def _recovery_snapshot(*, backup_dir=None):
    manifests = list_backup_manifests(output_dir=backup_dir, limit=20)
    drills = list_restore_drills(output_dir=backup_dir, limit=20)
    scheduled_runs = list_scheduled_backup_runs(output_dir=backup_dir, limit=20)
    return {
        "backup_count": len(manifests),
        "restore_drill_count": len(drills),
        "scheduled_backup_count": len(scheduled_runs),
        "latest_backup": _manifest_snapshot(manifests[0]) if manifests else None,
        "latest_restore_drill": _restore_drill_snapshot(drills[0]) if drills else None,
        "latest_scheduled_backup": _scheduled_backup_snapshot(scheduled_runs[0]) if scheduled_runs else None,
        "backups": [_manifest_snapshot(item) for item in manifests],
        "restore_drills": [_restore_drill_snapshot(item) for item in drills],
        "scheduled_backups": [_scheduled_backup_snapshot(item) for item in scheduled_runs],
    }


def _manifest_snapshot(manifest):
    return {
        "name": manifest.get("name", ""),
        "created_at": manifest.get("created_at", ""),
        "schema_version": manifest.get("schema_version"),
        "data_file": manifest.get("data_file", ""),
        "data_file_exists": bool(manifest.get("data_file_exists")),
        "encrypted": bool(manifest.get("encrypted")),
        "encryption_status": manifest.get("encryption_status", ""),
        "encryption_verified": bool(manifest.get("encryption_verified")),
        "data_size": manifest.get("data_size", 0),
        "data_sha256": manifest.get("data_sha256", ""),
        "compressed_sha256": manifest.get("compressed_sha256", ""),
    }


def _restore_drill_snapshot(drill):
    return {
        "name": drill.get("name", ""),
        "created_at": drill.get("created_at", ""),
        "manifest_name": drill.get("manifest_name", ""),
        "outcome": drill.get("outcome", ""),
        "passed": bool(drill.get("passed")),
        "completed_checks": drill.get("completed_checks", 0),
        "total_checks": drill.get("total_checks", 0),
        "unresolved_findings": drill.get("unresolved_findings", 0),
        "evidence_hash": drill.get("evidence_hash", ""),
    }


def _scheduled_backup_snapshot(run):
    return {
        "name": run.get("name", ""),
        "created_at": run.get("created_at", ""),
        "status": run.get("status", ""),
        "offsite_status": run.get("offsite_status", ""),
        "offsite_dir": run.get("offsite_dir", ""),
        "manifest_name": run.get("manifest_name", ""),
        "data_file": run.get("data_file", ""),
        "encrypted": bool(run.get("encrypted")),
        "retention_pruned": bool(run.get("retention_pruned")),
        "evidence_hash": run.get("evidence_hash", ""),
        "error": run.get("error", ""),
    }


def _vault_snapshot(verification):
    return {
        "status": verification.get("status", ""),
        "entries": verification.get("entries", 0),
        "valid_entries": verification.get("valid_entries", 0),
        "critical_count": verification.get("critical_count", 0),
        "warning_count": verification.get("warning_count", 0),
        "head_hash": verification.get("head_hash", ""),
        "issues": [
            {
                "code": issue.get("code", ""),
                "severity": issue.get("severity", ""),
                "sequence": issue.get("sequence", 0),
                "message": issue.get("message", ""),
            }
            for issue in verification.get("issues", [])
        ],
    }


def _integration_snapshot(company, *, as_of):
    from integrations.provider_readiness import build_provider_go_live_readiness
    from integrations.readiness import build_gst_certification_readiness

    provider = build_provider_go_live_readiness(company, as_of=as_of)
    certification = build_gst_certification_readiness(company)
    return {
        "provider_go_live": {
            "score": provider["score"],
            "status": provider["status"],
            "status_label": provider["status_label"],
            "summary": provider["summary"],
            "totals": provider["totals"],
            "retry_summary": provider["retry_summary"],
            "connectors": [_connector_row_snapshot(row) for row in provider["connector_rows"]],
        },
        "gst_certification": {
            "level": certification["level"],
            "summary": certification["summary"],
            "errors": certification["errors"],
            "warnings": certification["warnings"],
            "manual": certification["manual"],
            "sandbox_ready": certification["sandbox_ready"],
            "production_ready": certification["production_ready"],
            "checks": certification["checks"],
        },
    }


def _connector_row_snapshot(row):
    connector = row.get("connector")
    return {
        "type": row.get("type", ""),
        "name": row.get("name", ""),
        "service": row.get("service", ""),
        "state": row.get("state", ""),
        "state_label": row.get("state_label", ""),
        "score": row.get("score", 0),
        "critical_count": row.get("critical_count", 0),
        "warning_count": row.get("warning_count", 0),
        "open_retry_count": row.get("open_retry_count", 0),
        "failed_retry_count": row.get("failed_retry_count", 0),
        "connector": _connector_snapshot(connector),
        "checks": row.get("checks", []),
        "taskable_issues": row.get("taskable_issues", []),
    }


def _connector_snapshot(connector):
    if not connector:
        return None
    return {
        "id": connector.pk,
        "label": connector.label,
        "provider_name": connector.provider_name,
        "mode": connector.mode,
        "status": connector.status,
        "gstin": connector.gstin,
        "tan": connector.tan,
        "username": connector.masked_username,
        "base_url": connector.base_url,
        "credential_reference": connector.credential_reference,
        "credential_age_days": connector.credential_age_days,
        "credential_last_rotated_at": connector.credential_last_rotated_at.isoformat() if connector.credential_last_rotated_at else "",
        "last_success_at": connector.last_success_at.isoformat() if connector.last_success_at else "",
        "last_failure_at": connector.last_failure_at.isoformat() if connector.last_failure_at else "",
    }


def _pack_hash(pack):
    clean = dict(pack)
    clean.pop("sha256", None)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _safe_filename_part(value):
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value or "")).strip(".-").lower()
