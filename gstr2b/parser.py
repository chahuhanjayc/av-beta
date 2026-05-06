import json
from decimal import Decimal
from datetime import datetime

class GSTR2BParser:
    @staticmethod
    def parse_json(file_content):
        data = json.loads(file_content)
        entries = []
        seen_keys = set()
        
        # GSTR-2B JSON structure usually has 'b2b', 'cdnr', etc.
        sections = ['b2b', 'b2ba']
        for section in sections:
            if section in data:
                for supplier in data[section]:
                    ctin = supplier.get('ctin') # Supplier GSTIN
                    trade_name = supplier.get('trdnm', '')
                    for inv in supplier.get('inv', []):
                        inv_num = inv.get('inum')
                        inv_dt_str = inv.get('dt')
                        inv_date = GSTR2BParser._parse_date(inv_dt_str)
                        
                        # Unique key to prevent duplicates within the same file
                        unique_key = (ctin, inv_num, inv_date)
                        if unique_key in seen_keys:
                            continue
                        seen_keys.add(unique_key)

                        item_rows = inv.get('itms', [])
                        tax_data = GSTR2BParser._calculate_tax_split(item_rows)
                        
                        entries.append({
                            'gstin': ctin,
                            'supplier_name': trade_name,
                            'invoice_number': inv_num,
                            'invoice_date': inv_date,
                            'taxable_value': tax_data['taxable'],
                            'invoice_value': Decimal(str(inv.get('val', 0))),
                            'tax_amount': tax_data['total'],
                            'cgst': tax_data['cgst'],
                            'sgst': tax_data['sgst'],
                            'igst': tax_data['igst'],
                        })
        return entries

    @staticmethod
    def _parse_date(date_str):
        if not date_str:
            return None
        # Common GSTR formats: DD-MM-YYYY, YYYY-MM-DD
        for fmt in ('%d-%m-%Y', '%Y-%m-%d', '%d/%m/%Y'):
            try:
                return datetime.strptime(date_str, fmt).date()
            except ValueError:
                continue
        # If all fail, return None so it can be handled by the caller
        return None

    @staticmethod
    def _calculate_tax_split(items):
        taxable_value = Decimal('0.00')
        cgst = Decimal('0.00')
        sgst = Decimal('0.00')
        igst = Decimal('0.00')
        for item in items:
            it_details = item.get('itm_det', {})
            taxable = Decimal(str(it_details.get('txval', 0)))
            taxable_value += taxable
            igst += Decimal(str(it_details.get('iamt', 0))) # IGST
            cgst += Decimal(str(it_details.get('camt', 0))) # CGST
            sgst += Decimal(str(it_details.get('samt', 0))) # SGST
        return {
            'taxable': taxable_value,
            'cgst': cgst,
            'sgst': sgst,
            'igst': igst,
            'total': cgst + sgst + igst
        }
