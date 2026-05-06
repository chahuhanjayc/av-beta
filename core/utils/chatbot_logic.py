"""
core/utils/chatbot_logic.py
Rule-based logic engine for AV Assistant.
Strict accounting style. No conversational fluff.
"""

INTENT_MAP = {
    "ACCOUNTING_BASICS": {
        "keywords": ["journal", "ledger", "voucher", "trial balance", "balance sheet"],
        "responses": {
            "journal": "Journal:\nBook of original entry. Transactions recorded chronologically.\nAV: Accounting -> New Voucher -> Journal",
            "ledger": "Ledger:\nPrincipal book containing individual accounts.\nAV: Accounting -> Ledgers",
            "voucher": "Voucher:\nDocument supporting an accounting entry.\nAV supports: Sales, Purchase, Receipt, Payment, Journal",
            "trial balance": "Trial Balance:\nStatement showing Dr/Cr balances of all ledgers.\nAV: Reports -> Trial Balance",
            "balance sheet": "Balance Sheet:\nFinancial position (Assets, Liabilities, Equity) at a point in time.\nAV: Reports -> Balance Sheet"
        }
    },
    "ENTRIES": {
        "responses": {
            "sales": "Sales Entry:\nDr Cash/Bank/Debtor\nCr Sales",
            "purchase": "Purchase Entry:\nDr Purchase\nDr Input GST\nCr Cash/Bank/Creditor",
            "journal": "Journal Entry:\nUsed for adjustments/non-cash items.\nDr Expense/Asset\nCr Liability/Provision",
            "bank": "Bank Entry (Payment):\nDr Expense/Creditor\nCr Bank",
            "payment": "Payment Entry:\nDr Ledger/Party\nCr Cash/Bank",
            "receipt": "Receipt Entry:\nDr Cash/Bank\nCr Income/Party"
        }
    },
    "GST": {
        "responses": {
            "main": "GST:\nIntra-state -> CGST + SGST\nInter-state -> IGST",
            "cgst": "CGST:\nCentral GST. Applied on intra-state sales.",
            "sgst": "SGST:\nState GST. Applied on intra-state sales.",
            "igst": "IGST:\nIntegrated GST. Applied on inter-state transactions.",
            "input": "Input Tax:\nTax paid on purchases (ITC).\nAV: Recorded via Purchase Voucher.",
            "output": "Output Tax:\nTax collected on sales.\nAV: Recorded via Sales Voucher.",
            "hsn": "HSN:\nHarmonized System of Nomenclature for goods classification.\nAV: Set in Stock Items."
        }
    },
    "APP_USAGE": {
        "responses": {
            "create voucher": "Steps:\n1. Accounting -> New Voucher (or press 'N')\n2. Select ledger and amount\n3. Ensure Dr = Cr\n4. Enter to save",
            "add ledger": "Steps:\n1. Accounting -> Ledgers -> Create\n2. OR press 'Alt+C' during voucher entry",
            "inventory": "Steps:\n1. Create items in Inventory\n2. Select in Sales/Purchase vouchers\n3. Check Stock Summary for levels",
            "search": "Press 'Ctrl+K' to open Universal Search.\nSearch for: Reports, Ledgers, Vouchers.",
            "ctrlk": "Command Palette:\nInstant access to reports and records."
        }
    }
}

def get_bot_reply(message):
    msg = message.lower().strip()
    
    # 1. Flexible Entry Matching
    if "entry" in msg or "entries" in msg:
        if "sales" in msg:    return INTENT_MAP["ENTRIES"]["responses"]["sales"]
        if "purchase" in msg: return INTENT_MAP["ENTRIES"]["responses"]["purchase"]
        if "journal" in msg:  return INTENT_MAP["ENTRIES"]["responses"]["journal"]
        if "bank" in msg:     return INTENT_MAP["ENTRIES"]["responses"]["bank"]
        if "payment" in msg:  return INTENT_MAP["ENTRIES"]["responses"]["payment"]
        if "receipt" in msg:  return INTENT_MAP["ENTRIES"]["responses"]["receipt"]
    
    # 2. GST Matching
    if "gst" in msg:
        if "cgst" in msg: return INTENT_MAP["GST"]["responses"]["cgst"]
        if "sgst" in msg: return INTENT_MAP["GST"]["responses"]["sgst"]
        if "igst" in msg: return INTENT_MAP["GST"]["responses"]["igst"]
        if "input" in msg: return INTENT_MAP["GST"]["responses"]["input"]
        if "output" in msg: return INTENT_MAP["GST"]["responses"]["output"]
        if "hsn" in msg: return INTENT_MAP["GST"]["responses"]["hsn"]
        return INTENT_MAP["GST"]["responses"]["main"]

    # 3. App Usage / Steps Matching
    if any(k in msg for k in ["how", "steps", "create", "add", "use"]):
        if "voucher" in msg:   return INTENT_MAP["APP_USAGE"]["responses"]["create voucher"]
        if "ledger" in msg:    return INTENT_MAP["APP_USAGE"]["responses"]["add ledger"]
        if "inventory" in msg or "item" in msg: return INTENT_MAP["APP_USAGE"]["responses"]["inventory"]
        if "search" in msg or "find" in msg: return INTENT_MAP["APP_USAGE"]["responses"]["search"]

    if "ctrl" in msg and "k" in msg:
        return INTENT_MAP["APP_USAGE"]["responses"]["ctrlk"]

    # 4. Keyword direct fallback (Accounting Basics)
    for keyword in INTENT_MAP["ACCOUNTING_BASICS"]["keywords"]:
        if keyword in msg:
            return INTENT_MAP["ACCOUNTING_BASICS"]["responses"][keyword]

    # 5. Partial matches for entries if "entry" word is missing
    if "sales" in msg:    return INTENT_MAP["ENTRIES"]["responses"]["sales"]
    if "purchase" in msg: return INTENT_MAP["ENTRIES"]["responses"]["purchase"]

    return "Not found.\nTry:\n- sales entry\n- gst\n- voucher\n- ledger\n- steps"
