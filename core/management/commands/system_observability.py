import json

from django.core.management.base import BaseCommand

from core.system_observability import build_system_observability, observability_public_payload


class Command(BaseCommand):
    help = "Run deep runtime observability diagnostics."

    def add_arguments(self, parser):
        parser.add_argument(
            "--deploy",
            action="store_true",
            help="Include Django deployment checks.",
        )
        parser.add_argument(
            "--json",
            action="store_true",
            help="Emit machine-readable JSON.",
        )
        parser.add_argument(
            "--fail-on-critical",
            action="store_true",
            help="Exit non-zero when a critical diagnostic is present.",
        )

    def handle(self, *args, **options):
        report = build_system_observability(include_deploy=options["deploy"])
        payload = observability_public_payload(report)

        if options["json"]:
            self.stdout.write(json.dumps(payload, indent=2))
        else:
            self.stdout.write(self.style.SUCCESS(
                f"System Observability: {report['status_label']} ({report['score']}%)"
            ))
            for item in report["checks"]:
                style = (
                    self.style.ERROR if item["level"] == "critical"
                    else self.style.WARNING if item["level"] == "warning"
                    else self.style.SUCCESS
                )
                duration = f" [{item['duration_ms']} ms]" if item.get("duration_ms") is not None else ""
                self.stdout.write(style(
                    f"{item['level'].upper()}: {item['component']} / {item['name']} - {item['message']}{duration}"
                ))
                if item.get("hint"):
                    self.stdout.write(f"  {item['hint']}")

        if options["fail_on_critical"] and report["totals"]["critical"]:
            raise SystemExit(1)
