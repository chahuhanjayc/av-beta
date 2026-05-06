import xml.etree.ElementTree as ET
from datetime import datetime
from decimal import Decimal

def vouchers_to_tally_xml(vouchers):
    """
    Converts a queryset of Voucher objects to Tally-compatible XML (TallyPrime).
    """
    envelope = ET.Element("ENVELOPE")
    
    header = ET.SubElement(envelope, "HEADER")
    ET.SubElement(header, "TALLYREQUEST").text = "Import Data"
    
    body = ET.SubElement(envelope, "BODY")
    import_data = ET.SubElement(body, "IMPORTDATA")
    
    request_desc = ET.SubElement(import_data, "REQUESTDESC")
    ET.SubElement(request_desc, "REPORTNAME").text = "Vouchers"
    
    request_data = ET.SubElement(import_data, "REQUESTDATA")
    
    for v in vouchers:
        tally_msg = ET.SubElement(request_data, "TALLYMESSAGE", {"xmlns:UDF": "TallyUDF"})
        
        # Determine Tally Voucher Type
        vtype = v.voucher_type
        if vtype == "Sales": v_type_tally = "Sales"
        elif vtype == "Purchase": v_type_tally = "Purchase"
        elif vtype == "Payment": v_type_tally = "Payment"
        elif vtype == "Receipt": v_type_tally = "Receipt"
        elif vtype == "Contra": v_type_tally = "Contra"
        else: v_type_tally = "Journal"

        voucher = ET.SubElement(tally_msg, "VOUCHER", {
            "VCHTYPE": v_type_tally,
            "ACTION": "Create",
            "OBJVIEW": "Accounting Voucher View"
        })
        
        # Basic Info
        ET.SubElement(voucher, "DATE").text = v.date.strftime("%Y%m%d")
        ET.SubElement(voucher, "VOUCHERTYPENAME").text = v_type_tally
        ET.SubElement(voucher, "VOUCHERNUMBER").text = v.number
        ET.SubElement(voucher, "NARRATION").text = v.narration or ""
        ET.SubElement(voucher, "ASSEDGERNAME").text = "None" # Default
        
        # Items (Ledger Entries)
        for item in v.items.all():
            ledger_entry = ET.SubElement(voucher, "ALLLEDGERENTRIES.LIST")
            ET.SubElement(ledger_entry, "LEDGERNAME").text = item.ledger.name
            ET.SubElement(ledger_entry, "ISDEEMEDPOSITIVE").text = "Yes" if item.entry_type == 'DR' else "No"
            
            # Tally logic: Debit is negative in XML, Credit is positive.
            amt = item.amount if item.entry_type == 'CR' else -item.amount
            ET.SubElement(ledger_entry, "AMOUNT").text = str(amt)
            
            if item.narration:
                ET.SubElement(ledger_entry, "ADDITIONALDESCRIPTION").text = item.narration

    # Return as string
    return ET.tostring(envelope, encoding="unicode")
