import json

from django.core.management.base import BaseCommand, CommandError

from core.go_live_evidence_pack import build_go_live_evidence_pack, write_go_live_evidence_pack
from core.models import Company


class Command(BaseCommand):
    help = "Generate a signed Go-Live Evidence Pack JSON artifact for a company."

    def add_arguments(self, parser):
        parser.add_argument("--company-id", type=int, required=True)
        parser.add_argument("--runtime-only", action="store_true", help="Skip Django deployment checks.")
        parser.add_argument("--output-dir", default=None, help="Directory where the JSON pack is written.")
        parser.add_argument("--backup-dir", default=None, help="Backup evidence directory to inspect.")
        parser.add_argument("--json", action="store_true", help="Emit the pack JSON to stdout after writing it.")
        parser.add_argument("--fail-on-blocker", action="store_true", help="Exit non-zero when certificate blockers exist.")

    def handle(self, *args, **options):
        company = Company.objects.filter(pk=options["company_id"]).first()
        if not company:
            raise CommandError(f"Company id {options['company_id']} not found.")

        pack = build_go_live_evidence_pack(
            company=company,
            include_deploy=not options["runtime_only"],
            backup_dir=options["backup_dir"],
        )
        path = write_go_live_evidence_pack(pack, output_dir=options["output_dir"])

        if options["json"]:
            self.stdout.write(json.dumps({"path": str(path), **pack}, indent=2, sort_keys=True, default=str))
        else:
            style = self.style.SUCCESS if pack["signoff"]["blockers"] == 0 else self.style.ERROR
            self.stdout.write(style(
                f"{pack['pack_id']} written to {path} ({pack['signoff']['status_label']}, {pack['signoff']['score']}%)"
            ))
            self.stdout.write(f"SHA-256: {pack['sha256']}")

        if options["fail_on_blocker"] and pack["signoff"]["blockers"]:
            raise SystemExit(1)
