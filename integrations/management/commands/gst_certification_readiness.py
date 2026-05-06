import json

from django.core.management.base import BaseCommand

from core.models import Company
from integrations.readiness import build_gst_certification_readiness


class Command(BaseCommand):
    help = "Report GST API/GSP certification readiness for a company."

    def add_arguments(self, parser):
        parser.add_argument("--company", type=int, help="Company id to evaluate.")
        parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")

    def handle(self, *args, **options):
        company = None
        if options.get("company"):
            company = Company.objects.get(pk=options["company"])
        else:
            company = Company.objects.order_by("name").first()

        result = build_gst_certification_readiness(company)
        if company:
            result["company"] = {"id": company.pk, "name": company.name, "gstin": company.gstin or ""}

        if options["json"]:
            self.stdout.write(json.dumps(result, indent=2, default=str))
            return

        self.stdout.write(self.style.HTTP_INFO(result["summary"]))
        for check in result["checks"]:
            style = self.style.SUCCESS
            if check["level"] == "error":
                style = self.style.ERROR
            elif check["level"] in {"warning", "manual"}:
                style = self.style.WARNING
            self.stdout.write(style(f"{check['level'].upper()}: {check['name']} - {check['message']}"))
