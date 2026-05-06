import secrets
import string
from django.core.management.base import BaseCommand
from django.contrib.auth import get_user_model
from django.db import transaction
from django.utils import timezone
from core.models import Company, UserCompanyAccess
from clients.models import ClientSubscription
from clients.tasks import send_welcome_message

User = get_user_model()

class Command(BaseCommand):
    help = "Onboard a new client: creates company, primary user, and subscription."

    def add_arguments(self, parser):
        parser.add_argument("--email", type=str, required=True, help="Primary user email")
        parser.add_argument("--company-name", type=str, required=True, help="Legal name of the company")
        parser.add_argument("--plan", type=str, default="basic", choices=["basic", "pro", "enterprise"])
        parser.add_argument("--trial-days", type=int, default=14, help="Trial period in days")

    def handle(self, *args, **options):
        email = options["email"]
        company_name = options["company_name"]
        plan = options["plan"]
        trial_days = options["trial_days"]

        # 1. Generate secure random password
        alphabet = string.ascii_letters + string.digits
        temp_password = "".join(secrets.choice(alphabet) for i in range(12))

        try:
            with transaction.atomic():
                # 2. Create Company
                company = Company.objects.create(name=company_name)
                
                # 3. Create User
                user = User.objects.create_user(
                    email=email,
                    password=temp_password,
                    first_name=company_name.split()[0], # Best guess
                )
                
                # 4. Link User to Company as Admin
                UserCompanyAccess.objects.create(
                    user=user,
                    company=company,
                    role="Admin"
                )
                
                # 5. Create Subscription
                start_date = timezone.now()
                end_date = start_date + timezone.timedelta(days=trial_days)
                
                subscription = ClientSubscription.objects.create(
                    company=company,
                    primary_user=user,
                    plan=plan,
                    status=ClientSubscription.STATUS_TRIAL,
                    subscription_start=start_date,
                    subscription_end=end_date
                )

                # 6. Placeholder for WhatsApp/Email
                send_welcome_message(user, company, temp_password)

                self.stdout.write(self.style.SUCCESS(f"Successfully onboarded {company_name}"))
                self.stdout.write(f"Login: {email}")
                self.stdout.write(f"Temp Password: {temp_password}")
                self.stdout.write(f"Plan: {plan}")
                self.stdout.write(f"Expiry: {end_date.date()}")

        except Exception as e:
            self.stdout.write(self.style.ERROR(f"Error creating client: {str(e)}"))
