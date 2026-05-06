import json

from django.core.management.base import BaseCommand, CommandError

from core.go_live_certificate import build_go_live_certificate, go_live_certificate_payload
from core.models import Company


class Command(BaseCommand):
    help = "Generate the production go-live certificate."

    def add_arguments(self, parser):
        parser.add_argument("--company-id", type=int, help="Generate the certificate for this company.")
        parser.add_argument("--runtime-only", action="store_true", help="Skip Django deployment checks.")
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
        parser.add_argument("--fail-on-blocker", action="store_true", help="Exit non-zero when blockers exist.")

    def handle(self, *args, **options):
        company = None
        if options["company_id"]:
            company = Company.objects.filter(pk=options["company_id"]).first()
            if not company:
                raise CommandError(f"Company id {options['company_id']} not found.")

        certificate = build_go_live_certificate(
            company=company,
            include_deploy=not options["runtime_only"],
        )
        payload = go_live_certificate_payload(certificate)

        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            style = (
                self.style.SUCCESS if certificate["status"] == "certified"
                else self.style.WARNING if certificate["status"] == "conditional"
                else self.style.ERROR
            )
            self.stdout.write(style(
                f"{certificate['certificate_id']} {certificate['status_label']} ({certificate['score']}%)"
            ))
            self.stdout.write(certificate["summary"])
            for gate in certificate["gates"]:
                gate_style = (
                    self.style.SUCCESS if gate["status"] == "ready"
                    else self.style.WARNING if gate["status"] == "watch"
                    else self.style.ERROR
                )
                self.stdout.write(gate_style(f"{gate['status_label']}: {gate['area']} / {gate['name']} - {gate['message']}"))

        if options["fail_on_blocker"] and certificate["totals"]["blocked"]:
            raise SystemExit(1)
