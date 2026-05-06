from django.core.mail import EmailMessage
from django.conf import settings
from django.template.loader import render_to_string

def send_ledger_email(user, pdf_content, filename):
    """
    Sends an email to the portal user with their ledger statement attached.
    """
    subject = "Ledger Statement & Balance Confirmation - Akshaya Vistara"
    body = render_to_string("portal/email/ledger_confirmation.txt", {
        "user": user,
    })
    
    email = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[user.email],
    )
    
    # Attach the generated PDF
    email.attach(filename, pdf_content, "application/pdf")
    
    # Send the email
    email.send(fail_silently=False)
