from decimal import Decimal


def calculate_gst(company_state, party_state, taxable_amount, gst_rate):
    """
    Calculate CGST/SGST or IGST based on place of supply.
    """
    taxable_amount = Decimal(str(taxable_amount))
    gst_rate = Decimal(str(gst_rate))

    if company_state == party_state:
        cgst = taxable_amount * (gst_rate / 2) / 100
        sgst = taxable_amount * (gst_rate / 2) / 100
        igst = Decimal("0.00")
    else:
        cgst = Decimal("0.00")
        sgst = Decimal("0.00")
        igst = taxable_amount * gst_rate / 100

    return {
        "cgst": cgst.quantize(Decimal("0.01")),
        "sgst": sgst.quantize(Decimal("0.01")),
        "igst": igst.quantize(Decimal("0.01")),
        "total_tax": (cgst + sgst + igst).quantize(Decimal("0.01")),
    }
