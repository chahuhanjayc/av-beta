from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from core.production_trust import run_scheduled_backup


class Command(BaseCommand):
    help = "Run the scheduled encrypted backup path and record offsite evidence."

    def add_arguments(self, parser):
        parser.add_argument(
            "--output-dir",
            default=str(settings.BASE_DIR / "backups"),
            help="Directory where backup and scheduled evidence files will be written.",
        )
        parser.add_argument("--include-sessions", action="store_true")
        parser.add_argument("--no-encrypt", action="store_true", help="Force a plain compressed backup.")
        parser.add_argument("--no-prune", action="store_true", help="Do not apply backup retention cleanup.")
        parser.add_argument("--no-offsite", action="store_true", help="Do not copy backup artifacts to BACKUP_OFFSITE_DIR.")
        parser.add_argument("--mode", default="scheduled", help="Evidence mode label.")

    def handle(self, *args, **options):
        try:
            result = run_scheduled_backup(
                output_dir=options["output_dir"],
                include_sessions=options["include_sessions"],
                encrypt=False if options["no_encrypt"] else None,
                prune=not options["no_prune"],
                copy_offsite=not options["no_offsite"],
                mode=options["mode"],
            )
        except Exception as exc:
            raise CommandError(f"Scheduled backup failed: {exc}") from exc

        manifest = result.get("manifest") or {}
        evidence = result.get("scheduled_evidence") or {}
        payload = evidence.get("payload") or {}
        self.stdout.write(self.style.SUCCESS(f"Scheduled backup manifest: {manifest.get('name', '-')}"))
        self.stdout.write(self.style.SUCCESS(f"Scheduled evidence: {evidence.get('name', '-')}"))
        self.stdout.write(self.style.SUCCESS(f"Offsite status: {payload.get('offsite_status', '-')}"))
