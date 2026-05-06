import pandas as pd
import json
import re
from decimal import Decimal, InvalidOperation


GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
PHONE_DIGIT_RE = re.compile(r"\D+")

class SmartParser:
    MAPPING_KEYWORDS = {
        'ledger': ['particulars', 'ledger', 'account name', 'account', 'ledger name'],
        'date': ['date', 'voucher date', 'txn date'],
        'debit': ['debit', 'dr', 'amount (dr)', 'debit amount'],
        'credit': ['credit', 'cr', 'amount (cr)', 'credit amount'],
        'amount': ['amount', 'net amount', 'transaction amount'],
        'drcr': ['dr/cr', 'dr cr', 'debit/credit', 'debit credit'],
        'vch_type': ['voucher type', 'vch type', 'type'],
        'vch_no': ['voucher no', 'vch no', 'number', 'reference'],
        'narration': ['narration', 'description', 'remark'],
        'gstin': ['gstin', 'gst no', 'gst number', 'gst registration'],
        'pan': ['pan', 'pan no', 'pan number'],
        'email': ['email', 'e-mail', 'mail'],
        'whatsapp': ['whatsapp', 'mobile', 'phone', 'contact number'],
        'group': ['group', 'account group', 'under', 'ledger group'],
    }

    OPENING_KEYWORDS = ['opening', 'b/f', 'balance b/f', 'opening balance']

    def __init__(self, file_path, file_type):
        self.file_path = file_path
        self.file_type = file_type
        self.df = None

    def load_data(self):
        if self.file_type == 'excel':
            self.df = pd.read_excel(self.file_path)
        else:
            self.df = pd.read_csv(self.file_path)
        
        # Clean column names (strip whitespace, lowercase)
        self.df.columns = [str(c).strip().lower() for c in self.df.columns]
        # Fill NaN for consistent grouping
        self.df = self.df.fillna('')
        return self.df

    def detect_columns(self):
        if self.df is None:
            self.load_data()
        
        detected = {}
        columns = self.df.columns
        
        for key, keywords in self.MAPPING_KEYWORDS.items():
            for col in columns:
                if any(kw in col for kw in keywords):
                    detected[key] = col
                    break
        return detected

    def group_vouchers(self, mapping):
        if self.df is None:
            self.load_data()
        
        vch_no_col = mapping.get('vch_no')
        date_col = mapping.get('date')
        narration_col = mapping.get('narration')
        vch_type_col = mapping.get('vch_type')
        
        # Decide grouping key
        if vch_no_col and self.df[vch_no_col].astype(str).str.strip().any():
            group_cols = [vch_no_col]
            group_iterator = self.df.groupby(group_cols, sort=False)
        elif date_col:
            group_cols = [date_col]
            if narration_col:
                group_cols.append(narration_col)
            group_iterator = self.df.groupby(group_cols, sort=False)
        else:
            group_iterator = ((index, row.to_frame().T) for index, row in self.df.iterrows())
        
        vouchers = []
        for _, group in group_iterator:
            # Skip if any row in group looks like an opening balance
            if any(self._is_opening_row(row, mapping) for _, row in group.iterrows()):
                continue

            items = []
            vch_meta = {}
            
            for _, row in group.iterrows():
                data = self.normalize_row(row, mapping)
                if data['ledger'] and (data['debit'] or data['credit']):
                    items.append(data)
                
                if not vch_meta:
                    vch_meta = {
                        'date': data['date'],
                        'vch_type': data['vch_type'],
                        'vch_no': data['vch_no'],
                        'narration': row.get(narration_col, "Imported from Tally")
                    }
            
            if items:
                vouchers.append({
                    'meta': vch_meta,
                    'items': items
                })
        
        return vouchers

    def get_opening_balances(self, mapping):
        if self.df is None:
            self.load_data()
        
        opening_balances = []
        for _, row in self.df.iterrows():
            if self._is_opening_row(row, mapping):
                data = self.normalize_row(row, mapping)
                if data['ledger'] and (data['debit'] or data['credit']):
                    opening_balances.append(data)
        
        return opening_balances

    def _is_opening_row(self, row, mapping):
        vch_type_col = mapping.get('vch_type')
        narration_col = mapping.get('narration')
        
        # Check keywords in vch_type or narration
        for col in [vch_type_col, narration_col]:
            if col:
                val = str(row.get(col, '')).lower()
                if any(kw in val for kw in self.OPENING_KEYWORDS):
                    return True
        
        # Also check if it's a single entry row without a Vch No (often opening)
        # but keywords are more reliable.
        return False

    def get_preview_data(self, mapping, limit=20):
        if self.df is None:
            self.load_data()
        preview_df = self.df.head(limit).copy()
        return preview_df.to_dict(orient='records')

    def normalize_row(self, row, mapping):
        debit = self._parse_amount(row.get(mapping.get('debit'), 0))
        credit = self._parse_amount(row.get(mapping.get('credit'), 0))
        if not debit and not credit and mapping.get('amount'):
            debit, credit = self._split_amount_by_direction(
                row.get(mapping.get('amount'), 0),
                row.get(mapping.get('drcr'), ''),
            )

        return {
            'date': row.get(mapping.get('date')),
            'ledger': row.get(mapping.get('ledger')),
            'debit': float(debit),
            'credit': float(credit),
            'vch_type': row.get(mapping.get('vch_type'), 'Journal'),
            'vch_no': row.get(mapping.get('vch_no'), ''),
        }

    def _parse_amount(self, val):
        try:
            if not val or str(val).strip() == '': return 0.0
            raw = str(val).strip()
            negative = raw.startswith("(") and raw.endswith(")")
            amount = Decimal(raw.replace(',', '').replace('(', '').replace(')', '').replace('Dr', '').replace('Cr', '').replace('DR', '').replace('CR', '').strip())
            if negative:
                amount = -amount
            return amount
        except (InvalidOperation, ValueError):
            return Decimal("0.00")

    def _split_amount_by_direction(self, amount_value, direction_value):
        amount = abs(self._parse_amount(amount_value))
        direction = str(direction_value or amount_value or '').strip().lower()
        if 'cr' in direction or direction.endswith('c'):
            return Decimal("0.00"), amount
        if 'dr' in direction or direction.endswith('d'):
            return amount, Decimal("0.00")
        parsed = self._parse_amount(amount_value)
        if parsed < 0:
            return Decimal("0.00"), abs(parsed)
        return parsed, Decimal("0.00")

    def build_quality_report(self, mapping):
        vouchers = self.group_vouchers(mapping)
        seen_vouchers = {}
        duplicates = []
        unbalanced = []
        unknown_types = []
        allowed_types = {
            "Payment", "Receipt", "Sales", "Purchase", "Sales Return",
            "Purchase Return", "Contra", "Journal", "Stock Transfer",
        }

        for index, group in enumerate(vouchers):
            meta = group['meta']
            vch_no = str(meta.get('vch_no') or '').strip()
            if vch_no:
                seen_vouchers.setdefault(vch_no, 0)
                seen_vouchers[vch_no] += 1
                if seen_vouchers[vch_no] == 2:
                    duplicates.append(vch_no)

            debit = sum(Decimal(str(item['debit'])) for item in group['items'])
            credit = sum(Decimal(str(item['credit'])) for item in group['items'])
            if abs(debit - credit) > Decimal("0.01"):
                unbalanced.append({
                    "id": index,
                    "vch_no": vch_no,
                    "debit": float(debit),
                    "credit": float(credit),
                })

            voucher_type = str(meta.get('vch_type') or 'Journal').strip()
            if voucher_type and voucher_type not in allowed_types and voucher_type not in unknown_types:
                unknown_types.append(voucher_type)

        row_health = self._build_row_health(mapping)
        opening_balances = self.get_opening_balances(mapping)
        opening_debit = sum(Decimal(str(item['debit'])) for item in opening_balances)
        opening_credit = sum(Decimal(str(item['credit'])) for item in opening_balances)

        issues = []
        if duplicates:
            issues.append(self._issue(
                "duplicate_vouchers",
                "high",
                "Duplicate voucher numbers",
                "Same voucher number appears more than once. Import will skip duplicate Tally references already seen.",
                [{"voucher_no": value} for value in duplicates],
                count=len(duplicates),
            ))
        if unbalanced:
            issues.append(self._issue(
                "unbalanced_vouchers",
                "critical",
                "Unbalanced vouchers",
                "Debit and credit totals do not match for these vouchers. They will be skipped until corrected.",
                [
                    {
                        "voucher_no": item["vch_no"],
                        "debit": item["debit"],
                        "credit": item["credit"],
                        "difference": round(item["debit"] - item["credit"], 2),
                    }
                    for item in unbalanced
                ],
                count=len(unbalanced),
            ))
        if unknown_types:
            issues.append(self._issue(
                "unknown_voucher_types",
                "medium",
                "Unknown voucher types",
                "Voucher types outside the app's standard list may need mapping or review after import.",
                [{"voucher_type": value} for value in unknown_types],
                count=len(unknown_types),
            ))
        if abs(opening_debit - opening_credit) > Decimal("0.01") and opening_balances:
            issues.append(self._issue(
                "opening_balance_difference",
                "high",
                "Opening balance difference",
                "Opening balance debit and credit totals do not match. Review the trial balance before relying on imported opening balances.",
                [{
                    "debit": float(opening_debit),
                    "credit": float(opening_credit),
                    "difference": float(opening_debit - opening_credit),
                }],
                count=1,
            ))
        issues.extend(row_health["issues"])

        cleanup_score = self._score_issues(issues)
        return {
            "total_vouchers": len(vouchers),
            "duplicate_vouchers": duplicates[:50],
            "duplicate_voucher_count": len(duplicates),
            "unbalanced_vouchers": unbalanced[:50],
            "unbalanced_voucher_count": len(unbalanced),
            "unknown_voucher_types": unknown_types[:50],
            "opening_balance_count": len(opening_balances),
            "opening_debit": float(opening_debit),
            "opening_credit": float(opening_credit),
            "opening_difference": float(opening_debit - opening_credit),
            "detected_columns": mapping,
            "ledger_count": row_health["ledger_count"],
            "group_count": row_health["group_count"],
            "file_total_debit": float(row_health["file_total_debit"]),
            "file_total_credit": float(row_health["file_total_credit"]),
            "file_total_difference": float(row_health["file_total_debit"] - row_health["file_total_credit"]),
            "issues": issues,
            "cleanup_issue_count": sum(issue["count"] for issue in issues),
            "blocking_issue_count": sum(issue["count"] for issue in issues if issue["severity"] in {"critical", "high"}),
            "cleanup_score": cleanup_score,
            "cleanup_band": self._quality_band(cleanup_score),
        }

    def _row_value(self, row, col):
        if not col:
            return ""
        return row.get(col, "")

    def _row_number(self, index):
        try:
            return int(index) + 2
        except (TypeError, ValueError):
            return str(index)

    def _sample(self, row_number, **values):
        sample = {"row": row_number}
        for key, value in values.items():
            if value not in (None, ""):
                sample[key] = str(value)
        return sample

    def _issue(self, key, severity, title, message, samples, count=None):
        return {
            "key": key,
            "severity": severity,
            "title": title,
            "message": message,
            "count": count if count is not None else len(samples),
            "samples": samples[:10],
        }

    def _build_row_health(self, mapping):
        if self.df is None:
            self.load_data()

        ledger_col = mapping.get("ledger")
        date_col = mapping.get("date")
        debit_col = mapping.get("debit")
        credit_col = mapping.get("credit")
        amount_col = mapping.get("amount")
        drcr_col = mapping.get("drcr")
        gstin_col = mapping.get("gstin")
        pan_col = mapping.get("pan")
        email_col = mapping.get("email")
        whatsapp_col = mapping.get("whatsapp")
        group_col = mapping.get("group")

        ledgers = set()
        groups = set()
        gstin_to_ledgers = {}
        blank_ledger_rows = []
        rows_without_amount = []
        both_side_rows = []
        negative_amount_rows = []
        invalid_date_rows = []
        invalid_gstin_rows = []
        invalid_pan_rows = []
        invalid_email_rows = []
        invalid_whatsapp_rows = []
        duplicate_gstin_rows = []
        file_total_debit = Decimal("0.00")
        file_total_credit = Decimal("0.00")

        for index, row in self.df.iterrows():
            row_no = self._row_number(index)
            ledger = str(self._row_value(row, ledger_col) or "").strip()
            voucher_no = self._row_value(row, mapping.get("vch_no"))
            if ledger:
                ledgers.add(ledger.casefold())
            else:
                blank_ledger_rows.append(self._sample(row_no, voucher_no=voucher_no))

            group = str(self._row_value(row, group_col) or "").strip()
            if group:
                groups.add(group.casefold())

            debit = self._parse_amount(self._row_value(row, debit_col))
            credit = self._parse_amount(self._row_value(row, credit_col))
            amount_raw = self._row_value(row, amount_col)
            if not debit and not credit and amount_col:
                debit, credit = self._split_amount_by_direction(
                    amount_raw,
                    self._row_value(row, drcr_col),
                )

            file_total_debit += Decimal(str(debit))
            file_total_credit += Decimal(str(credit))

            has_debit_raw = bool(str(self._row_value(row, debit_col) or "").strip())
            has_credit_raw = bool(str(self._row_value(row, credit_col) or "").strip())
            has_amount_raw = bool(str(amount_raw or "").strip())
            if ledger and not has_debit_raw and not has_credit_raw and not has_amount_raw:
                rows_without_amount.append(self._sample(row_no, ledger=ledger, voucher_no=voucher_no))
            if debit and credit:
                both_side_rows.append(self._sample(row_no, ledger=ledger, voucher_no=voucher_no, debit=debit, credit=credit))
            if debit < 0 or credit < 0:
                negative_amount_rows.append(self._sample(row_no, ledger=ledger, voucher_no=voucher_no, debit=debit, credit=credit))

            date_value = self._row_value(row, date_col)
            if date_col and str(date_value or "").strip() and pd.isna(pd.to_datetime(date_value, errors="coerce")):
                invalid_date_rows.append(self._sample(row_no, ledger=ledger, voucher_no=voucher_no, value=date_value))

            gstin = str(self._row_value(row, gstin_col) or "").strip().upper()
            if gstin:
                if not GSTIN_RE.match(gstin):
                    invalid_gstin_rows.append(self._sample(row_no, ledger=ledger, value=gstin))
                elif ledger:
                    gstin_to_ledgers.setdefault(gstin, set()).add(ledger.casefold())

            pan = str(self._row_value(row, pan_col) or "").strip().upper()
            if pan and not PAN_RE.match(pan):
                invalid_pan_rows.append(self._sample(row_no, ledger=ledger, value=pan))

            email = str(self._row_value(row, email_col) or "").strip()
            if email and not EMAIL_RE.match(email):
                invalid_email_rows.append(self._sample(row_no, ledger=ledger, value=email))

            whatsapp = str(self._row_value(row, whatsapp_col) or "").strip()
            if whatsapp and not self._valid_phone(whatsapp):
                invalid_whatsapp_rows.append(self._sample(row_no, ledger=ledger, value=whatsapp))

        for gstin, ledger_names in gstin_to_ledgers.items():
            if len(ledger_names) > 1:
                duplicate_gstin_rows.append({"gstin": gstin, "ledger_count": len(ledger_names)})

        issues = []
        issue_specs = [
            ("blank_ledger_rows", "critical", "Rows without ledger", "Rows without a ledger cannot be mapped into books.", blank_ledger_rows),
            ("rows_without_amount", "high", "Ledger rows without amount", "Rows have a ledger but no debit, credit, or amount value.", rows_without_amount),
            ("both_debit_credit", "high", "Rows with both debit and credit", "A single row contains both debit and credit amounts. Split or correct the row before import.", both_side_rows),
            ("negative_amounts", "medium", "Negative debit/credit values", "Negative values inside debit/credit columns can reverse accounting meaning. Review these rows.", negative_amount_rows),
            ("invalid_dates", "high", "Invalid dates", "Some voucher dates could not be parsed.", invalid_date_rows),
            ("invalid_gstin", "high", "Invalid GSTIN values", "GSTIN values must follow the Indian 15-character GSTIN format.", invalid_gstin_rows),
            ("duplicate_gstin_ledgers", "medium", "Same GSTIN on multiple ledgers", "One GSTIN is linked to multiple ledger names. Merge or map carefully to avoid duplicate party masters.", duplicate_gstin_rows),
            ("invalid_pan", "medium", "Invalid PAN values", "PAN values should follow the 10-character Indian PAN format.", invalid_pan_rows),
            ("invalid_email", "medium", "Invalid email addresses", "Email addresses in the import file are not usable for client/vendor communication.", invalid_email_rows),
            ("invalid_whatsapp", "medium", "Invalid WhatsApp numbers", "WhatsApp numbers should contain 10 to 15 usable digits.", invalid_whatsapp_rows),
        ]
        for key, severity, title, message, samples in issue_specs:
            if samples:
                issues.append(self._issue(key, severity, title, message, samples))

        return {
            "ledger_count": len(ledgers),
            "group_count": len(groups),
            "file_total_debit": file_total_debit,
            "file_total_credit": file_total_credit,
            "issues": issues,
        }

    def _valid_phone(self, value):
        digits = PHONE_DIGIT_RE.sub("", value)
        if value.strip().startswith("+"):
            normalized_digits = digits
        elif len(digits) == 10:
            normalized_digits = f"91{digits}"
        elif len(digits) == 11 and digits.startswith("0"):
            normalized_digits = f"91{digits[1:]}"
        else:
            normalized_digits = digits
        return 10 <= len(normalized_digits) <= 15

    def _score_issues(self, issues):
        penalties = {"critical": 14, "high": 9, "medium": 4, "low": 2}
        caps = {"critical": 45, "high": 30, "medium": 18, "low": 8}
        total_penalty = Decimal("0")
        for issue in issues:
            severity = issue.get("severity", "low")
            count = Decimal(str(issue.get("count") or 0))
            total_penalty += min(count * penalties.get(severity, 2), caps.get(severity, 8))
        return max(0, int(100 - total_penalty))

    def _quality_band(self, score):
        if score >= 90:
            return "Excellent"
        if score >= 75:
            return "Good"
        if score >= 50:
            return "Needs Cleanup"
        return "High Risk"
