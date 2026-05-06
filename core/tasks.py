from celery import shared_task

from .production_trust import run_scheduled_backup


@shared_task(name="core.tasks.scheduled_operational_backup")
def scheduled_operational_backup():
    result = run_scheduled_backup(mode="celery")
    manifest = result.get("manifest") or {}
    evidence = result.get("scheduled_evidence") or {}
    payload = evidence.get("payload") or {}
    return {
        "manifest": manifest.get("name", ""),
        "evidence": evidence.get("name", ""),
        "status": payload.get("status", "failed"),
        "offsite_status": payload.get("offsite_status", ""),
        "encrypted": bool(manifest.get("encrypted")),
    }
