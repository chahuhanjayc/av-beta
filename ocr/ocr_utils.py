import re
import io
import os
import logging
import zipfile
import hashlib
from datetime import datetime
from typing import Dict, Any, List

try:
    import fitz # PyMuPDF
except ImportError:
    fitz = None

try:
    import pdfplumber
except ImportError:
    pdfplumber = None

try:
    import pytesseract
except ImportError:
    pytesseract = None

try:
    from PIL import Image, ImageOps, ImageEnhance
except ImportError:
    Image = None

logger = logging.getLogger(__name__)

_PARSER_VERSION = "2026.04.13.ROBUST"

# ---------------------------------------------------------------------------
# 1. FILE & TEXT EXTRACTION LAYER
# ---------------------------------------------------------------------------

def read_file_safely(file_obj) -> bytes:
    try:
        pdf_bytes = file_obj.read()
        return pdf_bytes
    finally:
        if hasattr(file_obj, "seek"):
            file_obj.seek(0)

def extract_primary_text(pdf_bytes: bytes, page_limit: int = 1) -> str:
    if not fitz: return ""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            pages = []
            for i in range(min(len(doc), page_limit)):
                pages.append(doc[i].get_text())
            return "\n".join(pages)
    except Exception as e:
        logger.error(f"PyMuPDF error: {e}")
    return ""

def extract_ocr_text(pdf_bytes: bytes, page_limit: int = 1) -> str:
    if not pytesseract or not fitz: return ""
    try:
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            full_ocr = []
            for i in range(min(len(doc), page_limit)):
                page = doc[i]
                # 4.0 zoom factor (approx 300 DPI) for better resolution on fuzzy images
                pix = page.get_pixmap(matrix=fitz.Matrix(4.0, 4.0))
                img_data = pix.tobytes("png")
                
                if Image:
                    img = Image.open(io.BytesIO(img_data))
                    
                    # Preprocessing for fuzzy/low-contrast images
                    img = img.convert('L')  # Grayscale
                    img = ImageOps.autocontrast(img)
                    enhancer = ImageEnhance.Contrast(img)
                    img = enhancer.enhance(2.0) # Boost contrast
                    
                    # Tesseract with specific config for better page segmentation
                    text = pytesseract.image_to_string(img, config='--oem 3 --psm 3')
                    full_ocr.append(text)
                else:
                    # Fallback if PIL is missing
                    import PIL.Image
                    img = PIL.Image.open(io.BytesIO(img_data))
                    full_ocr.append(pytesseract.image_to_string(img))
                    
            return "\n".join(full_ocr)
    except Exception as e:
        logger.error(f"OCR Rendering error: {e}")
    return ""

# ---------------------------------------------------------------------------
# 2. PARSING LOGIC (RULE-BASED)
# ---------------------------------------------------------------------------

def parse_vendor(text: str) -> str:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    suffixes = ["LIMITED", "PVT", "LLP", "INC", "PBC", "LTD", "LLC", "RETAIL", "INDUSTRIES", "SOLUTIONS"]
    
    # Priority 1: Match with suffixes
    for line in lines[:20]:
        if any(s in line.upper() for s in suffixes):
            # Avoid lines that are clearly not the vendor name
            if not any(k in line.upper() for k in ["TOTAL", "SAVINGS", "TAX AMOUNT", "SUBTOTAL", "CASHIER"]):
                return line
                
    # Priority 2: Brand names / First capitalized lines
    for line in lines[:12]:
        if len(line) > 3 and line[0].isupper():
            exclude = ["INVOICE", "TAX", "DATE", "BILL", "GSTIN", "TOTAL", "SAVINGS", "CASHIER", "RECEIPT", "STORE", "TENDER", "TERMINAL"]
            if not any(s in line.upper() for s in exclude):
                # Avoid lines that look like numbers or dates
                if not re.search(r"\d{2}-\d{2}-\d{4}", line) and not re.search(r"^\d+[\d.,\s]*$", line):
                    return line
    return ""

def parse_gstin(text: str) -> str:
    # Look for standard pattern, allowing for minor OCR noise
    # Common OCR misreads for 'Z' at 14th pos: 2, 7, I, 1
    m = re.search(r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z0-9]{1}[A-Z0-9]{1}[Z27I1]{1}[A-Z0-9]{1})\b", text.upper())
    if m:
        res = m.group(1)
        # Normalize the 14th character back to 'Z' if it was misread
        if res[13] in "27I1":
            res = res[:13] + "Z" + res[14:]
        return res
    
    # Relaxed search near keyword
    m2 = re.search(r"(?:Registration Number|GSTIN)[:\s]*([0-9A-Z]{14,15})", text, re.IGNORECASE)
    if m2: return m2.group(1).strip().replace(" ", "")
    
    return ""

def parse_invoice_no(text: str) -> str:
    m = re.search(r"(?i)\b(?:Invoice|Bill|Inv)\s+(?:No|Num|Number|#)[\s.:]*([A-Z0-9\-/]+)", text)
    if m: return m.group(1)
    m2 = re.search(r"(?i)\b(?:Invoice|Bill|Inv)\s*[:.]\s*([A-Z0-9\-/]+)", text)
    return m2.group(1) if m2 else ""

def parse_date(text: str) -> str:
    m = re.search(r"\b(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})\b", text)
    if m:
        d, m, y = m.group(1), m.group(2), m.group(3)
        y = f"20{y}" if len(y) == 2 else y
        try: return datetime.strptime(f"{d.zfill(2)}-{m.zfill(2)}-{y}", "%d-%m-%Y").strftime("%Y-%m-%d")
        except: pass
    return ""

def parse_total_amount(text: str) -> float:
    m = re.search(r"(?i)GRAND\s*TOTAL[\s:\u20b9$Rs.]*([\d,]+\.\d{2})", text)
    if m: return float(re.sub(r"[^\d.]", "", m.group(1)))
    keywords = [r"Grand\s+Total", r"Total\s+Amount", r"Net\s+Payable", r"Total", r"Sub-Total"]
    pattern = r"(?i)(" + "|".join(keywords) + r")[\s:\u20b9$Rs.]*([\d,]+\.\d{2})"
    matches = re.findall(pattern, text)
    amts = [float(re.sub(r"[^\d.]", "", m[1])) for m in matches]
    return max(amts) if amts else 0.0

def extract_line_items_from_text(text: str) -> List[Dict]:
    """Fallback for retail receipts where pdfplumber table extraction fails."""
    items = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    for i in range(len(lines) - 1):
        # Pattern: Match EAN_CODE (13 digits), HSN (8 digits), QTY, MRP, NET
        # Example: 8907367035961 33072000 1 325.00 162.50
        m = re.search(r"(\d{13})\s+(\d{8,})\s+(\d+)\s+([\d,.]+)\s+([\d,.]+)", lines[i+1])
        if m:
            ean, hsn, qty, mrp, net = m.groups()
            items.append({
                "name": lines[i],
                "hsn": hsn,
                "quantity": qty,
                "rate": mrp,
                "tax_rate": "",
                "amount": net
            })
    return items

def extract_table_items(pdf_bytes: bytes) -> List[Dict]:
    if not pdfplumber: return []
    items = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            table = pdf.pages[0].extract_table()
            if table:
                headers = [str(h or "").lower() for h in table[0]]
                idx_n, idx_h, idx_q, idx_r, idx_t, idx_a = -1, -1, -1, -1, -1, -1
                for i, h in enumerate(headers):
                    if any(k in h for k in ["desc", "item", "particular"]): idx_n = i
                    elif "hsn" in h or "sac" in h: idx_h = i
                    elif "qty" in h or "quant" in h: idx_q = i
                    elif "rate" in h or "price" in h: idx_r = i
                    elif "tax" in h or "%" in h: idx_t = i
                    elif "amt" in h or "value" in h or "total" in h: idx_a = i
                if idx_n != -1:
                    for row in table[1:]:
                        if not row[idx_n]: continue
                        tax_val = str(row[idx_t] or "").replace("%", "").strip() if idx_t != -1 else ""
                        items.append({
                            "name": str(row[idx_n]).replace("\n", " ").strip(),
                            "hsn": str(row[idx_h]).strip() if idx_h != -1 else "",
                            "quantity": str(row[idx_q]).strip() if idx_q != -1 else "1",
                            "rate": str(row[idx_r]).strip() if idx_r != -1 else "0.00",
                            "tax_rate": tax_val,
                            "amount": str(row[idx_a]).strip() if idx_a != -1 else "0.00"
                        })
    except: pass
    return items

def extract_bank_statement_rows(pdf_bytes: bytes) -> List[Dict]:
    """
    Extract bank statement rows from PDF/Image using pdfplumber or OCR.
    Returns list of dicts: {row_number, date, description, debit, credit, balance}
    """
    rows = []
    if not pdfplumber: return []
    
    try:
        from decimal import Decimal
        from datetime import datetime
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            row_count = 1
            for page in pdf.pages:
                table = page.extract_table()
                if not table: continue
                
                # Identify columns from headers
                headers = [str(h or "").lower() for h in table[0]]
                idx_d, idx_p, idx_dr, idx_cr, idx_bal = -1, -1, -1, -1, -1
                
                for i, h in enumerate(headers):
                    if any(k in h for k in ["date", "value d", "txn d"]): idx_d = i
                    elif any(k in h for k in ["particular", "description", "narration", "remitt"]): idx_p = i
                    elif any(k in h for k in ["debit", "withdrawal", "payment"]): idx_dr = i
                    elif any(k in h for k in ["credit", "deposit", "receipt"]): idx_cr = i
                    elif any(k in h for k in ["balance", "bal"]): idx_bal = i
                
                if idx_d == -1 or idx_p == -1: continue
                
                for row in table[1:]:
                    if not row[idx_d] or not row[idx_p]: continue
                    
                    def _clean_amt(val):
                        if not val: return Decimal("0.00")
                        try:
                            s = str(val).replace(",", "").replace("\u20b9", "").replace("Rs.", "").strip()
                            if s.startswith("(") and s.endswith(")"): s = "-" + s[1:-1]
                            return Decimal(s)
                        except: return Decimal("0.00")

                    raw_date = str(row[idx_d]).strip().replace("\n", " ")
                    parsed_date = None
                    # Common date formats in bank statements
                    for fmt in ["%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%Y-%m-%d", "%d-%b-%y", "%d/%m/%y"]:
                        try:
                            parsed_date = datetime.strptime(raw_date, fmt).date()
                            break
                        except: continue
                    
                    if not parsed_date:
                        # Try relaxed parsing for noisy OCR
                        m = re.search(r"(\d{1,2})[-/](\d{1,2}|[a-z]{3})[-/](\d{2,4})", raw_date, re.I)
                        if m:
                            try:
                                d, m_val, y = m.groups()
                                y = f"20{y}" if len(y) == 2 else y
                                if m_val.isdigit():
                                    parsed_date = datetime.strptime(f"{d}-{m_val}-{y}", "%d-%m-%Y").date()
                                else:
                                    parsed_date = datetime.strptime(f"{d}-{m_val}-{y}", "%d-%b-%Y").date()
                            except: pass

                    if not parsed_date: continue # Skip if no date found

                    rows.append({
                        "row_number":  row_count,
                        "date":        parsed_date,
                        "description": str(row[idx_p]).replace("\n", " ").strip(),
                        "debit":       _clean_amt(row[idx_dr]) if idx_dr != -1 else Decimal("0.00"),
                        "credit":      _clean_amt(row[idx_cr]) if idx_cr != -1 else Decimal("0.00"),
                        "balance":     _clean_amt(row[idx_bal]) if idx_bal != -1 else None,
                    })
                    row_count += 1
    except Exception as e:
        logger.error(f"Bank Statement OCR error: {e}")
        
    return rows

def parse_gst_certificate(text: str) -> Dict[str, Any]:
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    
    # 1. GSTIN - Broad search
    gstin = parse_gstin(text)
    
    # 2. Legal Name - Scan forward logic
    legal_name = ""
    for i, line in enumerate(lines):
        if "Legal Name" in line:
            # If value is on same line after colon or just after label
            val = ""
            if ":" in line:
                val = line.split(":", 1)[1].strip()
            else:
                # Some OCR reads "Legal Name Company Ltd"
                val = line.replace("Legal Name", "").strip()
            
            if len(val) > 2: 
                legal_name = val
                break
            
            # Else look at next 2 lines
            for j in range(i+1, min(i+3, len(lines))):
                if len(lines[j]) > 2 and not re.search(r"^\d\.", lines[j]) and "Trade Name" not in lines[j]:
                    legal_name = lines[j]
                    break
            if legal_name: break

    # 3. Address - Capture until next label
    address = ""
    for i, line in enumerate(lines):
        if "Principal Place of Business" in line or "Address of Principal" in line:
            addr_parts = []
            if ":" in line:
                p = line.split(":", 1)[1].strip()
                if p: addr_parts.append(p)
            else:
                # Handle cases where address starts on same line without colon
                p = re.sub(r".*(Principal Place of Business|Address of Principal)", "", line, flags=re.I).strip()
                if p: addr_parts.append(p)

            for j in range(i + 1, min(i + 8, len(lines))):
                # Stop if we hit a new numeric point or a major label
                if re.match(r"^\d\.", lines[j]) or any(k in lines[j] for k in ["Jurisdiction", "Signature", "Date of Liability", "Date of Registration"]):
                    break
                addr_parts.append(lines[j])
            address = " ".join(addr_parts)
            break

    # Clean up diagonal watermark artifacts (common words)
    noise = [
        "Goods and Services Tax", "Government of India", "Tax Invoice", "Page",
        "Registration Certificate", "Form GST REG-06", "Registration Number",
        "Principal Place of Business"
    ]
    for word in noise:
        legal_name = re.sub(word, "", legal_name, flags=re.IGNORECASE).strip()
        address = re.sub(word, "", address, flags=re.IGNORECASE).strip()

    # Final cleanup of common artifacts
    legal_name = re.sub(r"^[.:\s]+", "", legal_name).strip()

    return {
        "legal_name": legal_name or "Unknown GST Party",
        "gstin": gstin,
        "address": address,
        "doc_type": "gst_certificate"
    }

def parse_pan_card(text: str) -> Dict[str, Any]:
    """Extract Name, Father's Name, DOB, and PAN from PAN card OCR text."""
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    conf = {}
    
    # 1. PAN Number
    pan = ""
    pan_m = re.search(r"\b([A-Z]{5}[0-9]{4}[A-Z]{1})\b", text.upper())
    if pan_m: 
        pan = pan_m.group(1)
        conf["pan"] = "high"
    else:
        conf["pan"] = "low"
    
    # 2. DOB
    dob = ""
    dob_m = re.search(r"\b(\d{2}/\d{2}/\d{4})\b", text)
    if dob_m:
        try:
            dob = datetime.strptime(dob_m.group(1), "%d/%m/%Y").strftime("%Y-%m-%d")
            conf["dob"] = "high"
        except: 
            conf["dob"] = "low"
    else:
        conf["dob"] = "low"

    # 3. Name & Father's Name
    name = ""
    father_name = ""
    for i, line in enumerate(lines):
        if "INCOME TAX" in line.upper() or "CARD" in line.upper():
            potential_name_lines = lines[i+1 : i+5]
            for pl in potential_name_lines:
                if len(pl) > 3 and pl.replace(" ", "").isalpha():
                    if not name: 
                        name = pl
                        conf["name"] = "medium"
                    elif not father_name: 
                        father_name = pl
                        conf["father_name"] = "medium"
                        break
            break
    
    if not name: conf["name"] = "low"
    if not father_name: conf["father_name"] = "low"
            
    return {
        "name": name,
        "father_name": father_name,
        "dob": dob,
        "pan": pan,
        "confidence": conf,
        "doc_type": "pan_card"
    }

def parse_bank_details(text: str) -> Dict[str, Any]:
    """Extract Name, A/C No and IFSC from Bank Passbook/Cheque/Letter OCR text."""
    conf = {}
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    
    # 1. IFSC Code
    ifsc = ""
    ifsc_m = re.search(r"\b([A-Z]{4}0[A-Z0-9]{6})\b", text.upper())
    if ifsc_m: 
        ifsc = ifsc_m.group(1)
        conf["ifsc_code"] = "high"
    else:
        conf["ifsc_code"] = "low"
    
    # 2. Account Number
    acc_no = ""
    # Look for IDBI style "Account No. : 0019104000148825"
    acc_m = re.search(r"(?i)(?:Account|A/C|Acc)(?:\s+No|Number)?[\s.]*[:\s.]+([0-9]{9,18})", text)
    if acc_m: 
        acc_no = acc_m.group(1)
        conf["account_number"] = "high"
    else:
        conf["account_number"] = "low"
    
    # 3. Name (Experimental for Bank Letters/Cheques)
    name = ""
    for i, line in enumerate(lines):
        # IDBI Welcome Letter Pattern: Name starts after SpeedPost/Address block
        # Or look for line that is just Name in uppercase
        if "CUSTOMER ID" in line.upper() or "ACCOUNT NO" in line.upper():
            # Look backwards for name
            for j in range(max(0, i-10), i):
                prev_line = lines[j].strip()
                if len(prev_line) > 5 and prev_line.isupper() and not any(k in prev_line for k in ["IDBI", "SPEEDPOST", "TEL", "FAX", "EMAIL"]):
                    # Avoid address lines (usually have numbers or comma)
                    if not re.search(r"\d", prev_line) and "," not in prev_line:
                        name = prev_line
                        conf["name"] = "medium"
                        break
            if name: break

    # Fallback for name: look for specific IDBI letter name block
    if not name:
        for i, line in enumerate(lines):
            if "SPEEDPOST" in line.upper() and i + 1 < len(lines):
                # Next few lines might be Name and Address
                next_line = lines[i+1].strip()
                # Skip numeric lines (like SpeedPost tracking or Customer ID)
                if not re.search(r"^\d", next_line) and len(next_line) > 3:
                    name = next_line
                    conf["name"] = "medium"
                    break

    # 4. Bank Name Identification
    bank_name = "Unknown Bank"
    if "IDBI" in text.upper(): bank_name = "IDBI Bank"
    elif "HDFC" in text.upper(): bank_name = "HDFC Bank"
    elif "ICICI" in text.upper(): bank_name = "ICICI Bank"
    elif "SBI" in text.upper() or "STATE BANK" in text.upper(): bank_name = "State Bank of India"
    elif "AXIS" in text.upper(): bank_name = "Axis Bank"

    return {
        "name": name,
        "legal_name": name,
        "bank_name": bank_name,
        "vendor_name": bank_name,
        "account_number": acc_no,
        "ifsc_code": ifsc,
        "confidence": conf,
        "doc_type": "bank_details"
    }

def parse_payslip(text: str) -> Dict[str, Any]:
    """Extract Name, PAN, Bank, UAN, and Salary from Payslip OCR text."""
    conf = {}
    
    # 1. Name
    name = ""
    name_m = re.search(r"(?i)Name\s*:\s*([^[\n\r]+)", text)
    if name_m:
        name = name_m.group(1).strip()
        conf["name"] = "high"
    
    # 2. PAN
    pan = ""
    pan_m = re.search(r"\b([A-Z]{5}[0-9]{4}[A-Z]{1})\b", text.upper())
    if pan_m:
        pan = pan_m.group(1)
        conf["pan"] = "high"
    
    # 3. Bank Account
    acc_no = ""
    acc_m = re.search(r"(?i)(?:Account|A/C)\s+(?:No|Number)?[\s.:]+([0-9]{9,18})", text)
    if acc_m:
        acc_no = acc_m.group(1)
        conf["account_number"] = "high"
    
    # 4. PF UAN
    uan = ""
    uan_m = re.search(r"(?i)UAN[\s.:]+(\d{12})", text)
    if uan_m:
        uan = uan_m.group(1)
        conf["uan"] = "high"
    
    # 5. Dates
    join_date = ""
    jd_m = re.search(r"(?i)Join\s+Date\s*:\s*(\d{2}\s+[A-Za-z]{3}\s+\d{4})", text)
    if jd_m:
        try:
            join_date = datetime.strptime(jd_m.group(1), "%d %b %Y").strftime("%Y-%m-%d")
            conf["join_date"] = "high"
        except: pass

    # 6. Components
    basic = 0.0
    hra = 0.0
    
    # Look for table values
    basic_m = re.search(r"(?i)BASIC\s+(\d+)", text)
    if basic_m: basic = float(basic_m.group(1))
    
    hra_m = re.search(r"(?i)HRA\s+(\d+)", text)
    if hra_m: hra = float(hra_m.group(1))

    return {
        "name": name,
        "pan": pan,
        "account_number": acc_no,
        "uan": uan,
        "join_date": join_date,
        "basic_salary": basic,
        "hra": hra,
        "doc_type": "payslip",
        "confidence": conf
    }

# ---------------------------------------------------------------------------
# 3. PIPELINE
# ---------------------------------------------------------------------------

def process_pdf(pdf_bytes: bytes, doc_type: str = "invoice") -> Dict[str, Any]:
    text = extract_primary_text(pdf_bytes, page_limit=2)
    
    # Auto-detect doc type
    gst_keywords = ["REGISTRATION CERTIFICATE", "GST REG-06", "FORM GST REG", "GOODS AND SERVICES TAX"]
    pan_keywords = ["INCOME TAX", "PERMANENT ACCOUNT", " आयकर विभाग"]
    bank_keywords = ["IFSC CODE", "ACCOUNT NO", "BANK STATEMENT", "BRANCH NAME", "CUSTOMER ID", "CHEQUE BOOK", "ACCOUNT NUMBER", "IBKL0", "IDBI BANK", "PASSBOOK", "SAVINGS ACCOUNT", " IFSC ", " A/C "]
    payslip_keywords = ["PAYSLIP", "SALARY SLIP", "EARNINGS", "DEDUCTIONS", "NET PAY"]
    
    def detect_type(t):
        t_up = t.upper()
        if any(k in t_up for k in gst_keywords): return "gst_certificate"
        if any(k in t_up for k in pan_keywords): return "pan_card"
        if any(k in t_up for k in payslip_keywords): return "payslip"
        if any(k in t_up for k in bank_keywords) or "ACCOUNT NO" in t_up.replace(".", " "): return "bank_details"
        return None

    detected = detect_type(text)
    if detected: doc_type = detected

    # If we haven't detected a specific type yet, or if it's still "invoice", 
    # and the primary text is very short (possible scan), try OCR text for detection
    if (doc_type == "invoice" or not detected) and len(text) < 100:
        ocr_text = extract_ocr_text(pdf_bytes, page_limit=1)
        if ocr_text:
            detected = detect_type(ocr_text)
            if detected: 
                doc_type = detected
                text = ocr_text # Use OCR text for parsing as well

    if doc_type == "gst_certificate":
        res = parse_gst_certificate(text)
        if len(res["legal_name"]) < 3 or not res["gstin"]:
            ocr_text = extract_ocr_text(pdf_bytes, page_limit=2)
            if ocr_text: res = parse_gst_certificate(ocr_text)
        return res
    
    if doc_type == "pan_card":
        ocr_text = extract_ocr_text(pdf_bytes, page_limit=1)
        return parse_pan_card(ocr_text)
    
    if doc_type == "bank_details":
        res = parse_bank_details(text)
        if not res["account_number"] or not res["name"]:
            ocr_text = extract_ocr_text(pdf_bytes, page_limit=1)
            if ocr_text: res = parse_bank_details(ocr_text)
        return res
    
    if doc_type == "payslip":
        return parse_payslip(text)

    vendor = parse_vendor(text)
    gstin = parse_gstin(text)
    invoice_no = parse_invoice_no(text)
    date = parse_date(text)
    total_amount = parse_total_amount(text)
    
    if (total_amount <= 0 and not vendor):
        ocr_text = extract_ocr_text(pdf_bytes, page_limit=1)
        if ocr_text:
            # Check for GST certificate even in OCR fallback for "invoice"
            if any(k in ocr_text for k in gst_keywords):
                return parse_gst_certificate(ocr_text)

            text = text + "\n" + ocr_text
            if not vendor: vendor = parse_vendor(ocr_text)
            if not gstin: gstin = parse_gstin(ocr_text)
            if not invoice_no: invoice_no = parse_invoice_no(ocr_text)
            if not date: date = parse_date(ocr_text)
            if total_amount <= 0: total_amount = parse_total_amount(ocr_text)

    # Fallbacks
    if not vendor:
        v_lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 3]
        # Smarter fallback: skip lines with known labels
        exclude = ["TOTAL", "SAVINGS", "TAX", "INVOICE", "BILL", "CASHIER", "GSTIN", "RECEIPT", "STORE"]
        for v in v_lines[:10]:
            if not any(e in v.upper() for e in exclude):
                vendor = v
                break
        if not vendor:
            vendor = v_lines[0] if v_lines else "Unknown Vendor"

    if total_amount <= 0:
        all_nums = [float(n) for n in re.findall(r"\d+\.\d{2}", text)]
        total_amount = max(all_nums) if all_nums else 0.0

    line_items = extract_table_items(pdf_bytes)
    # New: Fallback to text-based extraction for retail receipts
    if not line_items:
        line_items = extract_line_items_from_text(text)

    conf = "high" if (vendor and total_amount > 0 and vendor != "Unknown Vendor") else "medium"
    
    narration = f"Purchase bill from {vendor}"
    if invoice_no: narration += f" (Inv #{invoice_no})"
    if date: narration += f" dated {date}"
    narration += f" — ₹{total_amount}"

    return {
        "vendor": vendor, "vendor_name": vendor, "gstin": gstin,
        "invoice_no": invoice_no, "date": date, "total_amount": total_amount,
        "narration": narration, "line_items": line_items, "doc_type": "invoice",
        "confidence": conf, "vendor_confidence": conf,
        "gstin_confidence": "high" if gstin else "low",
        "date_confidence": "high" if date else "low",
        "amount_confidence": "high" if total_amount > 0 else "low",
    }

def find_matching_po(company, vendor_ledger, items):
    """
    Search for an open Purchase Order for the given vendor and items.
    Returns: (matched_po, mismatch_qty, mismatch_rate)
    """
    from orders.models import Order, OrderItem
    from decimal import Decimal

    # Find open POs for the vendor
    open_pos = Order.objects.filter(
        company=company,
        order_type='Purchase',
        party_ledger=vendor_ledger,
        status__in=['Confirmed', 'Partially Fulfilled']
    ).prefetch_related('items')

    for po in open_pos:
        qty_mismatch = False
        rate_mismatch = False
        po_items = list(po.items.all())
        
        # Simple match: if any item name, qty, or rate differs
        for item in items:
            name = item.get('name', '').lower()
            try:
                qty = Decimal(str(item.get('quantity', '0')))
                rate = Decimal(str(item.get('rate', '0')))
            except:
                continue

            # Find matching item in PO
            po_item = next((pi for pi in po_items if pi.stock_item and pi.stock_item.name.lower() in name or name in pi.stock_item.name.lower()), None)
            
            if po_item:
                if abs(po_item.pending_qty - qty) > Decimal("0.001"):
                    qty_mismatch = True
                if abs(po_item.rate - rate) > Decimal("0.01"):
                    rate_mismatch = True
            else:
                # Item not in PO
                qty_mismatch = True
        
        return po, qty_mismatch, rate_mismatch

    return None, False, False

def process_pdf_submission(submission) -> None:
    pdf_bytes = read_file_safely(submission.file)
    result = process_pdf(pdf_bytes)
    if hasattr(submission, 'company'):
        from ledger.models import Ledger
        ledger = Ledger.objects.filter(company=submission.company, name__icontains=result['vendor'][:10], is_active=True).first()
        if ledger:
            result['vendor_ledger_id'] = ledger.pk
            result['vendor_ledger_name'] = ledger.name

            # Match with Purchase Order
            po, q_mis, r_mis = find_matching_po(submission.company, ledger, result.get("line_items", []))
            if po:
                result['po_reference_id'] = po.pk
                result['po_number'] = po.number
                result['po_mismatch_qty'] = q_mis
                result['po_mismatch_rate'] = r_mis

    submission.parsed_json = result
    submission.extracted_items = result.get("line_items", [])
    submission.status = "Pending"
    submission.save()

def parse_pdf_pipeline(file_obj) -> Dict[str, Any]:
    """Compatibility wrapper for standalone testing script."""
    pdf_bytes = read_file_safely(file_obj)
    res = process_pdf(pdf_bytes)
    res["status"] = "success"
    return res

def split_pdf_and_create_submissions(parent_submission):
    """
    Takes a multi-page PDF submission, splits each page into a 1-page PDF,
    and creates new OCRSubmissions for each page.
    """
    if not fitz: return 0
    from .models import OCRSubmission
    from django.core.files.base import ContentFile
    
    count = 0
    try:
        pdf_bytes = parent_submission.file.read()
        parent_submission.file.seek(0)
        
        with fitz.open(stream=pdf_bytes, filetype="pdf") as doc:
            if len(doc) <= 1: return 0
            
            for i in range(len(doc)):
                new_doc = fitz.open()
                new_doc.insert_pdf(doc, from_page=i, to_page=i)
                new_pdf_bytes = new_doc.tobytes()
                new_doc.close()
                
                # Create a filename
                base_name = os.path.splitext(parent_submission.filename)[0]
                new_filename = f"{base_name}_page_{i+1}.pdf"
                
                # Check hash to avoid duplicates if re-split
                hasher = hashlib.sha256(new_pdf_bytes)
                file_hash = hasher.hexdigest()
                
                if not OCRSubmission.objects.filter(company=parent_submission.company, file_hash=file_hash).exists():
                    sub = OCRSubmission.objects.create(
                        company=parent_submission.company,
                        file_hash=file_hash,
                        status=OCRSubmission.STATUS_PENDING
                    )
                    sub.file.save(new_filename, ContentFile(new_pdf_bytes), save=True)
                    count += 1
        
        # Mark parent as processed/split
        parent_submission.status = "Rejected" # Or a new status like "Split"
        parent_submission.ocr_error = f"Split into {count} pages."
        parent_submission.save()
        
    except Exception as e:
        logger.error(f"PDF Split error: {e}")
        
    return count

def process_bulk_zip(zip_submission):
    """
    Extracts all images/PDFs from a ZIP file and creates OCRSubmissions.
    """
    from .models import OCRSubmission
    from django.core.files.base import ContentFile
    
    count = 0
    try:
        with zipfile.ZipFile(io.BytesIO(zip_submission.file.read())) as zf:
            for info in zf.infolist():
                if info.is_dir(): continue
                ext = os.path.splitext(info.filename)[1].lower()
                if ext not in [".jpg", ".jpeg", ".png", ".pdf", ".tiff"]:
                    continue
                
                file_data = zf.read(info.filename)
                hasher = hashlib.sha256(file_data)
                file_hash = hasher.hexdigest()
                
                if not OCRSubmission.objects.filter(company=zip_submission.company, file_hash=file_hash).exists():
                    sub = OCRSubmission.objects.create(
                        company=zip_submission.company,
                        file_hash=file_hash,
                        status=OCRSubmission.STATUS_PENDING
                    )
                    sub.file.save(os.path.basename(info.filename), ContentFile(file_data), save=True)
                    count += 1
                    
        # Mark ZIP as processed
        zip_submission.status = "Rejected"
        zip_submission.ocr_error = f"Extracted {count} files from ZIP."
        zip_submission.save()
        
    except Exception as e:
        logger.error(f"ZIP Bulk error: {e}")
        
    return count
