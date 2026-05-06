import hashlib
import json
from pathlib import Path

from django.conf import settings
from django.utils import timezone

from integrations.models import IntegrationRequestLog, StatutoryExportLog

from .models import AuditLog, GSTEvidenceDocument, PracticeTask
from .production_trust import backup_directory, list_backup_manifests, list_restore_drills, list_scheduled_backup_runs


VAULT_TASK_PREFIX = "EVIDENCEVAULT:"


def vault_directory(output_dir=None):
    return Path(output_dir or getattr(settings, "EVIDENCE_VAULT_DIR", settings.BASE_DIR / "evidence_vault"))


def company_vault_path(company, *, output_dir=None):
    return vault_directory(output_dir) / str(company.pk) / "vault-ledger.jsonl"


def list_vault_entries(company, *, output_dir=None, limit=100):
    path = company_vault_path(company, output_dir=output_dir)
    if not path.exists():
        return []
    entries = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entries.append(json.loads(line))
            except json.JSONDecodeError as exc:
                entries.append({
                    "sequence": len(entries) + 1,
                    "category": "corrupt",
                    "title": "Unreadable vault entry",
                    "source_key": f"corrupt:{len(entries) + 1}",
                    "entry_hash": "",
                    "previous_hash": entries[-1]["entry_hash"] if entries else "",
                    "error": str(exc),
                    "valid_json": False,
                })
    if limit:
        return entries[-limit:]
    return entries


def seal_evidence_vault(company, user=None, *, output_dir=None, backup_dir=None):
    all_entries = list_vault_entries(company, output_dir=output_dir, limit=0)
    existing_keys = {entry.get("source_key") for entry in all_entries}
    previous_hash = all_entries[-1].get("entry_hash", "") if all_entries else ""
    sequence = len(all_entries)
    created = 0
    skipped = 0
    path = company_vault_path(company, output_dir=output_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    with path.open("a", encoding="utf-8") as handle:
        for candidate in _vault_candidates(company, backup_dir=backup_dir):
            if candidate["source_key"] in existing_keys:
                skipped += 1
                continue
            sequence += 1
            entry = _build_vault_entry(
                company,
                user,
                candidate,
                sequence=sequence,
                previous_hash=previous_hash,
            )
            previous_hash = entry["entry_hash"]
            handle.write(json.dumps(entry, sort_keys=True, default=str) + "\n")
            existing_keys.add(entry["source_key"])
            created += 1

    verification = verify_vault_chain(company, output_dir=output_dir)
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE,
        model_name="EvidenceVault",
        record_id=0,
        object_repr="Evidence vault seal",
        old_data={},
        new_data={
            "source": "evidence_vault",
            "created": created,
            "skipped": skipped,
            "status": verification["status"],
            "head_hash": verification["head_hash"],
        },
    )
    return {"created": created, "skipped": skipped, "verification": verification, "path": path}


def verify_vault_chain(company, *, output_dir=None):
    entries = list_vault_entries(company, output_dir=output_dir, limit=0)
    issues = []
    previous_hash = ""
    valid_entries = 0
    for index, entry in enumerate(entries, start=1):
        if entry.get("valid_json") is False:
            issues.append(_vault_issue("invalid_json", "critical", index, "Vault entry is not valid JSON."))
            previous_hash = entry.get("entry_hash", "")
            continue
        if entry.get("sequence") != index:
            issues.append(_vault_issue("sequence_gap", "critical", index, "Vault sequence does not match ledger order."))
        if entry.get("previous_hash", "") != previous_hash:
            issues.append(_vault_issue("chain_break", "critical", index, "Previous hash does not match prior entry."))
        expected_hash = _entry_hash(entry)
        if entry.get("entry_hash") != expected_hash:
            issues.append(_vault_issue("entry_hash_mismatch", "critical", index, "Entry hash does not match entry content."))
        artifact_issue = _verify_artifact(entry)
        if artifact_issue:
            issues.append(_vault_issue(artifact_issue["code"], artifact_issue["severity"], index, artifact_issue["message"]))
        previous_hash = entry.get("entry_hash", "")
        valid_entries += 1

    critical_count = sum(1 for issue in issues if issue["severity"] == "critical")
    warning_count = sum(1 for issue in issues if issue["severity"] == "warning")
    if critical_count:
        status = "Broken"
        badge_class = "bg-danger"
    elif warning_count:
        status = "Watch"
        badge_class = "bg-warning text-dark"
    elif entries:
        status = "Sealed"
        badge_class = "bg-success"
    else:
        status = "Empty"
        badge_class = "bg-secondary"
    return {
        "status": status,
        "badge_class": badge_class,
        "entries": len(entries),
        "valid_entries": valid_entries,
        "issues": issues,
        "critical_count": critical_count,
        "warning_count": warning_count,
        "head_hash": previous_hash,
        "ledger_path": company_vault_path(company, output_dir=output_dir),
        "has_taskable_issues": bool(critical_count or warning_count),
    }


def create_evidence_vault_tasks(company, user, verification):
    active_issues = verification.get("issues", [])
    active_refs = {f"{VAULT_TASK_PREFIX}{issue['code'].upper()}:{issue['sequence']}" for issue in active_issues}
    created = 0
    updated = 0
    closed = 0

    for issue in active_issues:
        reference = f"{VAULT_TASK_PREFIX}{issue['code'].upper()}:{issue['sequence']}"
        description = (
            f"Evidence vault issue at sequence {issue['sequence']}.\n"
            f"Issue: {issue['message']}\n"
            f"Ledger: {verification['ledger_path']}\n"
            f"Head hash: {verification['head_hash'] or '-'}"
        )
        task, was_created = PracticeTask.objects.get_or_create(
            company=company,
            reference=reference,
            defaults={
                "title": f"Evidence Vault: {issue['message'][:90]}",
                "task_type": PracticeTask.TYPE_AUDIT,
                "priority": PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH,
                "status": PracticeTask.STATUS_OPEN,
                "due_date": timezone.localdate() + timezone.timedelta(days=1),
                "assigned_to": user if getattr(user, "is_authenticated", False) else None,
                "created_by": user if getattr(user, "is_authenticated", False) else None,
                "description": description,
            },
        )
        if was_created:
            created += 1
            _audit_vault_task(company, user, task, "create", issue, verification)
        elif task.status in {PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED} or task.description != description:
            task.status = PracticeTask.STATUS_OPEN
            task.completed_at = None
            task.completed_by = None
            task.description = description
            task.priority = PracticeTask.PRIORITY_CRITICAL if issue["severity"] == "critical" else PracticeTask.PRIORITY_HIGH
            task.save(update_fields=["status", "completed_at", "completed_by", "description", "priority", "updated_at"])
            updated += 1
            _audit_vault_task(company, user, task, "update", issue, verification)

    stale_tasks = (
        PracticeTask.objects.filter(company=company, reference__startswith=VAULT_TASK_PREFIX)
        .exclude(reference__in=active_refs)
        .exclude(status__in=[PracticeTask.STATUS_DONE, PracticeTask.STATUS_CANCELLED])
    )
    for task in stale_tasks:
        old_status = task.status
        task.status = PracticeTask.STATUS_DONE
        task.completed_at = timezone.now()
        task.completed_by = user if getattr(user, "is_authenticated", False) else None
        task.description = f"{task.description}\n\nClosed because the Evidence Vault verification issue is no longer active.".strip()
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
            new_data={"source": "evidence_vault", "status": task.status},
        )

    return {"created": created, "updated": updated, "closed": closed}


def _vault_candidates(company, *, backup_dir=None):
    for doc in GSTEvidenceDocument.objects.filter(company=company).select_related("uploaded_by").order_by("uploaded_at", "pk"):
        file_path = Path(doc.file.path) if doc.file and hasattr(doc.file, "path") else None
        yield {
            "source_key": f"GSTEvidenceDocument:{doc.pk}",
            "category": "GST",
            "source_model": "GSTEvidenceDocument",
            "source_id": doc.pk,
            "title": doc.title,
            "reference": doc.arn_ack_number or doc.challan_reference or doc.external_reference,
            "artifact_path": file_path,
            "artifact_name": Path(doc.file.name).name if doc.file else "",
            "metadata": {
                "evidence_type": doc.evidence_type,
                "return_type": doc.return_type,
                "period_start": doc.period_start.isoformat(),
                "period_end": doc.period_end.isoformat(),
            },
        }

    for log in StatutoryExportLog.objects.filter(company=company).select_related("generated_by").order_by("created_at", "pk"):
        yield {
            "source_key": f"StatutoryExportLog:{log.pk}",
            "category": "Statutory Export",
            "source_model": "StatutoryExportLog",
            "source_id": log.pk,
            "title": log.file_name,
            "reference": log.portal_reference,
            "artifact_name": log.file_name,
            "artifact_sha256": log.file_sha256,
            "metadata": {
                "export_type": log.export_type,
                "status": log.status,
                "period_start": log.period_start.isoformat() if log.period_start else "",
                "period_end": log.period_end.isoformat() if log.period_end else "",
                "row_count": log.row_count,
                "amount_total": str(log.amount_total),
            },
        }

    for log in IntegrationRequestLog.objects.filter(company=company).select_related("requested_by").order_by("created_at", "pk"):
        yield {
            "source_key": f"IntegrationRequestLog:{log.pk}",
            "category": "Integration Request",
            "source_model": "IntegrationRequestLog",
            "source_id": log.pk,
            "title": f"{log.get_service_display()} {log.request_id}",
            "reference": str(log.request_id),
            "artifact_name": log.provider,
            "artifact_sha256": log.request_digest,
            "metadata": {
                "service": log.service,
                "status": log.status,
                "voucher_id": log.voucher_id,
                "error": log.error_message,
            },
        }

    directory = backup_directory(backup_dir)
    for manifest in list_backup_manifests(output_dir=directory, limit=20):
        yield _file_candidate("Production Backup", "BackupManifest", manifest["name"], manifest["path"], metadata={
            "data_file": manifest["data_file"],
            "encrypted": manifest["encrypted"],
            "data_sha256": manifest["data_sha256"],
        })
    for drill in list_restore_drills(output_dir=directory, limit=20):
        yield _file_candidate("Restore Drill", "RestoreDrillEvidence", drill["name"], drill["path"], metadata={
            "outcome": drill["outcome"],
            "evidence_hash": drill["evidence_hash"],
        })
    for run in list_scheduled_backup_runs(output_dir=directory, limit=20):
        yield _file_candidate("Scheduled Backup", "ScheduledBackupEvidence", run["name"], run["path"], metadata={
            "status": run["status"],
            "offsite_status": run["offsite_status"],
            "evidence_hash": run["evidence_hash"],
        })


def _file_candidate(category, source_model, name, path, *, metadata=None):
    return {
        "source_key": f"{source_model}:{name}",
        "category": category,
        "source_model": source_model,
        "source_id": name,
        "title": name,
        "reference": name,
        "artifact_path": Path(path),
        "artifact_name": Path(path).name,
        "metadata": metadata or {},
    }


def _build_vault_entry(company, user, candidate, *, sequence, previous_hash):
    artifact = _artifact_snapshot(candidate)
    entry = {
        "vault_schema_version": 1,
        "sequence": sequence,
        "created_at": timezone.now().isoformat(),
        "company_id": company.pk,
        "company_name": company.name,
        "sealed_by": getattr(user, "email", "") if getattr(user, "is_authenticated", False) else "",
        "category": candidate["category"],
        "source_model": candidate["source_model"],
        "source_id": str(candidate["source_id"]),
        "source_key": candidate["source_key"],
        "title": candidate["title"],
        "reference": candidate.get("reference", ""),
        "artifact": artifact,
        "metadata": candidate.get("metadata") or {},
        "previous_hash": previous_hash,
    }
    entry["entry_hash"] = _entry_hash(entry)
    return entry


def _artifact_snapshot(candidate):
    path = candidate.get("artifact_path")
    if path and path.exists():
        return {
            "name": candidate.get("artifact_name") or path.name,
            "path": str(path),
            "size": path.stat().st_size,
            "sha256": _file_sha256(path),
            "hash_only": False,
        }
    return {
        "name": candidate.get("artifact_name", ""),
        "path": str(path) if path else "",
        "size": 0,
        "sha256": candidate.get("artifact_sha256", ""),
        "hash_only": True,
    }


def _entry_hash(entry):
    clean = dict(entry)
    clean.pop("entry_hash", None)
    return hashlib.sha256(json.dumps(clean, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def _verify_artifact(entry):
    artifact = entry.get("artifact") or {}
    if artifact.get("hash_only"):
        if not artifact.get("sha256"):
            return {"code": "hash_missing", "severity": "warning", "message": "Hash-only evidence has no hash."}
        return None
    path_value = artifact.get("path", "")
    if not path_value:
        return {"code": "artifact_path_missing", "severity": "critical", "message": "Artifact path is missing."}
    path = Path(path_value)
    if not path.exists():
        return {"code": "artifact_missing", "severity": "critical", "message": "Artifact file is missing."}
    if artifact.get("size") != path.stat().st_size:
        return {"code": "artifact_size_mismatch", "severity": "critical", "message": "Artifact size changed after sealing."}
    if artifact.get("sha256") != _file_sha256(path):
        return {"code": "artifact_hash_mismatch", "severity": "critical", "message": "Artifact SHA-256 changed after sealing."}
    return None


def _vault_issue(code, severity, sequence, message):
    return {
        "code": code,
        "severity": severity,
        "sequence": sequence,
        "message": message,
    }


def _audit_vault_task(company, user, task, action, issue, verification):
    AuditLog.objects.create(
        company=company,
        user=user if getattr(user, "is_authenticated", False) else None,
        action=AuditLog.ACTION_CREATE if action == "create" else AuditLog.ACTION_UPDATE,
        model_name="PracticeTask",
        record_id=task.pk,
        object_repr=task.title[:200],
        old_data={},
        new_data={
            "source": "evidence_vault",
            "reference": task.reference,
            "issue": issue["code"],
            "severity": issue["severity"],
            "vault_status": verification["status"],
        },
    )


def _file_sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
