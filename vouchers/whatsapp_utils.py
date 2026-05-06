import logging

logger = logging.getLogger(__name__)


def send_whatsapp_approval(voucher):
    """
    Simulate sending a WhatsApp message for voucher approval.
    Real deployments should replace this with the Meta/Twilio API.
    """
    amount = voucher.total_debit()
    vendor = "Party"
    party_line = voucher.items.filter(entry_type="CR").first()
    if party_line:
        vendor = party_line.ledger.name

    message = f"Voucher Rs.{amount} to {vendor}. Reply YES to approve."
    logger.info("WhatsApp approval request for %s Admin: %s", voucher.company.name, message)

    voucher.whatsapp_sent = True
