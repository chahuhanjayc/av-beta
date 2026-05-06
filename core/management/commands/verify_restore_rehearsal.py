import json

from django.core.management.base import BaseCommand, CommandError

from core.models import Company
from core.production_trust import verify_backup_restore_rehearsal


class Command(BaseCommand):
    help = "Verify that a backup archive can be opened, checked, decompressed, and parsed without restoring into production."

    def add_arguments(self, parser):
        parser.add_argument("--manifest-name", default="", help="Manifest filename. Defaults to latest manifest.")
        parser.add_argument("--output-dir", help="Backup directory. Defaults to BASE_DIR/backups.")
        parser.add_argument("--company-id", type=int, help="Attach audit/task evidence to this company.")
        parser.add_argument("--target-environment", default="archive rehearsal")
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        parser.add_argument("--fail-on-finding", action="store_true", help="Exit non-zero if verification records findings.")

    def handle(self, *args, **options):
        company = None
        if options["company_id"]:
            company = Company.objects.filter(pk=options["company_id"]).first()
            if not company:
                raise CommandError(f"Company id {options['company_id']} not found.")

        try:
            result = verify_backup_restore_rehearsal(
                manifest_name=options["manifest_name"],
                output_dir=options["output_dir"],
                target_environment=options["target_environment"],
                company=company,
            )
        except Exception as exc:
            raise CommandError(str(exc)) from exc

        payload = {
            "ok": result["passed"],
            "name": result["name"],
            "manifest_name": result["payload"]["manifest_name"],
            "evidence_hash": result["payload"]["evidence_hash"],
            "findings": result["findings"],
            "verification": result["verification"],
        }
        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            style = self.style.SUCCESS if payload["ok"] else self.style.ERROR
            self.stdout.write(style(
                f"Restore rehearsal {'passed' if payload['ok'] else 'recorded findings'}: {payload['name']}"
            ))
            self.stdout.write(
                f"Objects: {payload['verification']['object_count']} across {payload['verification']['model_count']} model(s)."
            )
            for finding in payload["findings"]:
                self.stdout.write(self.style.ERROR(f"Finding: {finding}"))

        if options["fail_on_finding"] and not payload["ok"]:
            raise SystemExit(1)
