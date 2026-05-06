from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from core.demo_workspace import seed_demo_workspace


class Command(BaseCommand):
    help = "Create or refresh the polished sales demo workspace."

    def add_arguments(self, parser):
        parser.add_argument(
            "--user-email",
            help="Grant demo workspace access to this active user.",
        )

    def handle(self, *args, **options):
        user = self._resolve_user(options.get("user_email"))
        result = seed_demo_workspace(user=user)

        if user:
            self.stdout.write(self.style.SUCCESS(f"Granted demo access to {user.email}."))
        else:
            self.stdout.write(self.style.WARNING("No user access was granted. Seeded company data only."))

        self.stdout.write(self.style.SUCCESS(f"Primary demo company: {result.primary_company.name}"))
        for key, value in result.counts.items():
            self.stdout.write(f"{key}: {value}")

    def _resolve_user(self, email):
        User = get_user_model()
        if email:
            user = User.objects.filter(email__iexact=email, is_active=True).first()
            if not user:
                raise CommandError(f"Active user {email} was not found.")
            return user

        return (
            User.objects.filter(is_active=True, is_superuser=True).order_by("email").first()
            or User.objects.filter(is_active=True).order_by("email").first()
        )
