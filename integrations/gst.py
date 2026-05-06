import hashlib
import json
import re
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin
from urllib.request import Request, urlopen

from django.conf import settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime

from .models import IntegrationRequestLog


class GSTIntegrationError(Exception):
    pass


class GSTConfigurationError(GSTIntegrationError):
    pass


GSTIN_RE = re.compile(r"^[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]$")
INVOICE_NO_RE = re.compile(r"^[A-Za-z0-9/-]{1,16}$")
PIN_RE = re.compile(r"^[1-9][0-9]{5}$")
VEHICLE_RE = re.compile(r"^[A-Z]{2}[0-9A-Z]{1,2}[A-Z]{1,3}[0-9]{4}$")
ZERO = Decimal("0.00")


@dataclass(frozen=True)
class GSTProviderConfig:
    provider: str
    base_url: str
    api_key: str
    api_secret: str
    timeout: int


def _decimal_to_string(value):
    if isinstance(value, Decimal):
        return str(value.quantize(Decimal("0.01")))
    return value


def _json_default(value):
    if isinstance(value, Decimal):
        return _decimal_to_string(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def dump_gst_payload_json(payload):
    return json.dumps(payload, indent=2, sort_keys=True, default=_json_default)


def _payload_digest(payload):
    serialized = json.dumps(payload, sort_keys=True, default=_json_default).encode("utf-8")
    return hashlib.sha256(serialized).hexdigest()


def _first_value(data, *keys):
    if not isinstance(data, dict):
        return ""
    for key in keys:
        if key in data and data[key]:
            return data[key]
    nested = data.get("result") or data.get("data") or {}
    if isinstance(nested, dict):
        for key in keys:
            if key in nested and nested[key]:
                return nested[key]
    return ""


def _parse_response_datetime(value):
    if not value:
        return None
    if hasattr(value, "tzinfo"):
        return value
    parsed = parse_datetime(str(value))
    if not parsed:
        return None
    if timezone.is_naive(parsed):
        parsed = timezone.make_aware(parsed)
    return parsed


class DisabledGSTProvider:
    provider_name = "disabled"

    def _raise(self):
        raise GSTConfigurationError("GST API provider is not configured.")

    def validate_gstin(self, gstin):
        self._raise()

    def generate_e_invoice(self, payload):
        self._raise()

    def generate_e_way_bill(self, payload):
        self._raise()


class MockGSTProvider:
    provider_name = "mock"

    def validate_gstin(self, gstin):
        return {
            "success": True,
            "gstin": gstin,
            "status": "Active",
            "legal_name": "Mock GST Taxpayer",
        }

    def generate_e_invoice(self, payload):
        doc_no = _payload_document_number(payload)
        irn = f"MOCKIRN{hashlib.sha1(doc_no.encode('utf-8')).hexdigest()[:24].upper()}"
        return {
            "success": True,
            "Irn": irn,
            "AckNo": f"ACK{timezone.now().strftime('%Y%m%d%H%M%S')}",
            "AckDt": timezone.now().isoformat(),
            "Status": "ACT",
            "SignedInvoice": {"Irn": irn, "DocNo": doc_no, "Source": "mock"},
            "SignedQRCode": f"MOCK-SIGNED-QR-{irn}",
        }

    def generate_e_way_bill(self, payload):
        doc_no = _payload_document_number(payload)
        return {
            "success": True,
            "EwbNo": str(int(hashlib.sha1(doc_no.encode("utf-8")).hexdigest()[:10], 16))[:12],
            "EwbDt": timezone.now().isoformat(),
            "EwbValidTill": (timezone.now() + timedelta(days=1)).isoformat(),
            "Status": "ACT",
        }


class GenericRESTGSTProvider:
    provider_name = "generic"

    def __init__(self, config):
        self.config = config

    def validate_gstin(self, gstin):
        return self._post(settings.GST_API_GSTIN_LOOKUP_PATH, {"gstin": gstin})

    def generate_e_invoice(self, payload):
        return self._post(settings.GST_API_E_INVOICE_PATH, payload)

    def generate_e_way_bill(self, payload):
        return self._post(settings.GST_API_E_WAY_BILL_PATH, payload)

    def _post(self, path, payload):
        if not self.config.base_url:
            raise GSTConfigurationError("GST_API_BASE_URL is not configured.")

        url = urljoin(self.config.base_url.rstrip("/") + "/", path.lstrip("/"))
        body = json.dumps(payload, default=_json_default).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-API-Key": self.config.api_key,
            "X-API-Secret": self.config.api_secret,
        }
        if settings.GST_API_USERNAME:
            headers["X-GST-Username"] = settings.GST_API_USERNAME
        if settings.GST_API_TAXPAYER_GSTIN:
            headers["X-GSTIN"] = settings.GST_API_TAXPAYER_GSTIN

        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.config.timeout) as response:
                response_body = response.read().decode("utf-8")
                if not response_body:
                    return {"success": True, "status_code": response.status}
                parsed = json.loads(response_body)
                if isinstance(parsed, dict):
                    parsed.setdefault("status_code", response.status)
                return parsed
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise GSTIntegrationError(f"GST API HTTP {exc.code}: {detail[:500]}") from exc
        except URLError as exc:
            raise GSTIntegrationError(f"GST API network error: {exc.reason}") from exc
        except json.JSONDecodeError as exc:
            raise GSTIntegrationError(f"GST API returned invalid JSON: {exc}") from exc


def get_gst_provider():
    provider = (settings.GST_API_PROVIDER or "").strip().lower()
    if not provider:
        return DisabledGSTProvider()
    if provider == "mock":
        return MockGSTProvider()
    config = GSTProviderConfig(
        provider=provider,
        base_url=settings.GST_API_BASE_URL,
        api_key=settings.GST_API_KEY,
        api_secret=settings.GST_API_SECRET,
        timeout=settings.GST_API_TIMEOUT_SECONDS,
    )
    return GenericRESTGSTProvider(config)


def build_e_invoice_payload(voucher):
    if voucher.voucher_type != "Sales":
        raise GSTIntegrationError("E-invoice generation is available only for Sales vouchers.")
    if voucher.status != "APPROVED":
        raise GSTIntegrationError("Approve the Sales voucher before generating an e-invoice.")
    if not voucher.company.gstin:
        raise GSTIntegrationError("Company GSTIN is required for e-invoice generation.")

    items = list(voucher.items.select_related(
        "ledger",
        "ledger__account_group",
        "stock_item",
        "stock_item__hsn_sac",
        "stock_item__tax_rate",
    ).all())
    party_line = _party_line(items)
    party = party_line.ledger if party_line else None
    if not party or not party.gstin:
        raise GSTIntegrationError("Customer GSTIN is required for e-invoice generation.")

    supply_lines = _supply_lines(items, party_line)
    if not supply_lines:
        raise GSTIntegrationError("At least one taxable sales line is required for e-invoice generation.")

    seller_state = _state_code(voucher.company.gstin)
    buyer_state = _state_code(party.gstin)
    pos = voucher.place_of_supply or buyer_state
    item_list, totals = _build_e_invoice_items(voucher, supply_lines, pos, seller_state)
    total_invoice_value = _money(party_line.amount if party_line else totals["total"])

    payload = {
        "Version": "1.1",
        "TranDtls": {
            "TaxSch": "GST",
            "SupTyp": "B2B",
            "RegRev": "Y" if voucher.reverse_charge else "N",
            "IgstOnIntra": "N",
        },
        "DocDtls": {
            "Typ": "INV",
            "No": voucher.number,
            "Dt": voucher.date.strftime("%d/%m/%Y"),
        },
        "SellerDtls": _party_details(
            gstin=voucher.company.gstin,
            legal_name=voucher.company.name,
            address=voucher.company.address or "",
            pincode=voucher.dispatch_pincode,
            state_code=seller_state,
        ),
        "BuyerDtls": _party_details(
            gstin=party.gstin,
            legal_name=party.name,
            address=party.address or "",
            pincode=voucher.ship_to_pincode,
            state_code=buyer_state,
            place_of_supply=pos,
        ),
        "ItemList": item_list,
        "ValDtls": {
            "AssVal": totals["assessable"],
            "CgstVal": _money(voucher.cgst_amount),
            "SgstVal": _money(voucher.sgst_amount),
            "IgstVal": _money(voucher.igst_amount),
            "CesVal": _money(ZERO),
            "StCesVal": _money(ZERO),
            "Discount": _money(ZERO),
            "OthChrg": _money(ZERO),
            "RndOffAmt": _money(ZERO),
            "TotInvVal": total_invoice_value,
        },
    }

    if _has_transport_details(voucher):
        payload["EwbDtls"] = _e_invoice_eway_details(voucher)

    errors = validate_e_invoice_payload(payload)
    if errors:
        raise GSTIntegrationError("E-invoice payload is incomplete: " + "; ".join(errors[:5]))
    return payload


def build_e_way_bill_payload(voucher):
    invoice_payload = build_e_invoice_payload(voucher)
    seller = invoice_payload["SellerDtls"]
    buyer = invoice_payload["BuyerDtls"]
    values = invoice_payload["ValDtls"]
    payload = {
        "supplyType": "O",
        "subSupplyType": "1",
        "subSupplyDesc": "",
        "docType": "INV",
        "docNo": invoice_payload["DocDtls"]["No"],
        "docDate": invoice_payload["DocDtls"]["Dt"],
        "fromGstin": seller["Gstin"],
        "fromTrdName": seller["LglNm"],
        "fromAddr1": seller["Addr1"],
        "fromAddr2": seller.get("Addr2", ""),
        "fromPlace": seller["Loc"],
        "fromPincode": seller["Pin"],
        "actFromStateCode": int(seller["Stcd"]),
        "fromStateCode": int(seller["Stcd"]),
        "toGstin": buyer["Gstin"],
        "toTrdName": buyer["LglNm"],
        "toAddr1": buyer["Addr1"],
        "toAddr2": buyer.get("Addr2", ""),
        "toPlace": buyer["Loc"],
        "toPincode": buyer["Pin"],
        "actToStateCode": int(buyer["Stcd"]),
        "toStateCode": int(buyer["Stcd"]),
        "transactionType": 1,
        "otherValue": 0,
        "totalValue": values["AssVal"],
        "cgstValue": values["CgstVal"],
        "sgstValue": values["SgstVal"],
        "igstValue": values["IgstVal"],
        "cessValue": 0,
        "cessNonAdvolValue": 0,
        "totInvValue": values["TotInvVal"],
        "transporterId": (voucher.transporter_id or "").strip().upper(),
        "transporterName": (voucher.transporter_name or "").strip(),
        "transDocNo": (voucher.transport_doc_no or "").strip(),
        "transDocDate": voucher.transport_doc_date.strftime("%d/%m/%Y") if voucher.transport_doc_date else "",
        "transMode": voucher.transport_mode or "",
        "transDistance": str(voucher.transport_distance_km or ""),
        "vehicleNo": (voucher.vehicle_number or "").strip().upper(),
        "vehicleType": voucher.vehicle_type or "R",
        "itemList": [
            {
                "productName": item["PrdDesc"][:100],
                "productDesc": item["PrdDesc"][:100],
                "hsnCode": int(str(item["HsnCd"]) or "0"),
                "quantity": item["Qty"],
                "qtyUnit": item["Unit"],
                "taxableAmount": item["AssAmt"],
                "sgstRate": item["GstRt"] / 2 if item["SgstAmt"] else 0,
                "cgstRate": item["GstRt"] / 2 if item["CgstAmt"] else 0,
                "igstRate": item["GstRt"] if item["IgstAmt"] else 0,
                "cessRate": 0,
                "cessNonAdvol": 0,
            }
            for item in invoice_payload["ItemList"]
        ],
    }
    if voucher.e_invoice_irn:
        payload["irn"] = voucher.e_invoice_irn
    errors = validate_e_way_bill_payload(payload)
    if errors:
        raise GSTIntegrationError("E-way bill payload is incomplete: " + "; ".join(errors[:5]))
    return payload


def build_gst_voucher_execution_context(voucher):
    e_invoice = _preflight_payload(lambda: build_e_invoice_payload(voucher))
    e_way_bill = _preflight_payload(lambda: build_e_way_bill_payload(voucher))
    return {
        "e_invoice": {
            "ready": e_invoice["ready"],
            "errors": e_invoice["errors"],
            "payload_available": e_invoice["ready"],
            "saved": bool(voucher.e_invoice_irn),
        },
        "e_way_bill": {
            "ready": e_way_bill["ready"],
            "errors": e_way_bill["errors"],
            "payload_available": e_way_bill["ready"],
            "saved": bool(voucher.e_way_bill_no),
        },
    }


def _preflight_payload(callback):
    try:
        callback()
    except GSTIntegrationError as exc:
        return {"ready": False, "errors": _integration_error_messages(exc)}
    return {"ready": True, "errors": []}


def _integration_error_messages(exc):
    message = str(exc)
    if ": " in message:
        message = message.split(": ", 1)[1]
    parts = [part.strip() for part in message.split(";") if part.strip()]
    return parts or [str(exc)]


def validate_e_invoice_payload(payload):
    errors = []
    for section in ("TranDtls", "DocDtls", "SellerDtls", "BuyerDtls", "ItemList", "ValDtls"):
        if section not in payload:
            errors.append(f"{section} is required")
    if errors:
        return errors

    doc = payload["DocDtls"]
    if doc.get("Typ") != "INV":
        errors.append("DocDtls.Typ must be INV for sales invoices")
    if not INVOICE_NO_RE.match(str(doc.get("No") or "")):
        errors.append("DocDtls.No must be 1-16 characters using letters, numbers, slash, or dash")
    if not doc.get("Dt"):
        errors.append("DocDtls.Dt is required")

    _validate_party_details(payload["SellerDtls"], "SellerDtls", errors, require_pos=False)
    _validate_party_details(payload["BuyerDtls"], "BuyerDtls", errors, require_pos=True)

    if not payload["ItemList"]:
        errors.append("ItemList must contain at least one taxable item")
    for index, item in enumerate(payload["ItemList"], 1):
        prefix = f"ItemList[{index}]"
        if not str(item.get("HsnCd") or "").strip():
            errors.append(f"{prefix}.HsnCd is required")
        if _to_decimal(item.get("AssAmt")) <= ZERO:
            errors.append(f"{prefix}.AssAmt must be positive")
        if _to_decimal(item.get("TotItemVal")) <= ZERO:
            errors.append(f"{prefix}.TotItemVal must be positive")
    if _to_decimal(payload["ValDtls"].get("TotInvVal")) <= ZERO:
        errors.append("ValDtls.TotInvVal must be positive")
    return errors


def validate_e_way_bill_payload(payload):
    errors = []
    required = [
        "supplyType",
        "subSupplyType",
        "docType",
        "docNo",
        "docDate",
        "fromGstin",
        "fromPincode",
        "toGstin",
        "toPincode",
        "transMode",
        "transDistance",
        "itemList",
    ]
    for field in required:
        if not payload.get(field):
            errors.append(f"{field} is required")
    if payload.get("fromGstin") and not GSTIN_RE.match(str(payload["fromGstin"]).upper()):
        errors.append("fromGstin is invalid")
    if payload.get("toGstin") and not GSTIN_RE.match(str(payload["toGstin"]).upper()):
        errors.append("toGstin is invalid")
    if payload.get("fromPincode") and not PIN_RE.match(str(payload["fromPincode"])):
        errors.append("fromPincode must be six digits")
    if payload.get("toPincode") and not PIN_RE.match(str(payload["toPincode"])):
        errors.append("toPincode must be six digits")
    if payload.get("docNo") and not INVOICE_NO_RE.match(str(payload["docNo"])):
        errors.append("docNo must be 1-16 characters using letters, numbers, slash, or dash")
    if payload.get("transMode") == "1" and not payload.get("vehicleNo") and not payload.get("transporterId"):
        errors.append("vehicleNo or transporterId is required for road e-way bill generation")
    if payload.get("vehicleNo") and not VEHICLE_RE.match(str(payload["vehicleNo"]).replace(" ", "").upper()):
        errors.append("vehicleNo format is invalid")
    if not payload.get("itemList"):
        errors.append("itemList is required")
    return errors


def validate_gstin(company, gstin, user=None):
    provider = get_gst_provider()
    payload = {"gstin": gstin}
    return _call_and_log(company, None, user, provider, IntegrationRequestLog.SERVICE_GSTIN, payload, lambda: provider.validate_gstin(gstin))


def generate_e_invoice_for_voucher(voucher, user=None):
    provider = get_gst_provider()
    payload = build_e_invoice_payload(voucher)
    result = _call_and_log(voucher.company, voucher, user, provider, IntegrationRequestLog.SERVICE_E_INVOICE, payload, lambda: provider.generate_e_invoice(payload))

    voucher.e_invoice_irn = _first_value(result, "Irn", "IRN", "irn") or voucher.e_invoice_irn
    voucher.e_invoice_ack_no = str(_first_value(result, "AckNo", "ack_no", "ackNo") or voucher.e_invoice_ack_no)
    voucher.e_invoice_ack_date = _parse_response_datetime(_first_value(result, "AckDt", "ack_date", "ackDt")) or voucher.e_invoice_ack_date
    voucher.e_invoice_status = str(_first_value(result, "Status", "status", "status_cd") or voucher.e_invoice_status or ("ACT" if voucher.e_invoice_irn else ""))
    signed_invoice = _first_value(result, "SignedInvoice", "signed_invoice", "signedInvoice")
    signed_qr = _first_value(result, "SignedQRCode", "SignedQrCode", "signed_qr_code", "signedQRCode")
    if signed_invoice:
        voucher.e_invoice_signed_invoice = _json_payload_value(signed_invoice)
    if signed_qr:
        voucher.e_invoice_signed_qr_code = str(signed_qr)
    voucher.save(update_fields=[
        "e_invoice_irn",
        "e_invoice_ack_no",
        "e_invoice_ack_date",
        "e_invoice_status",
        "e_invoice_signed_invoice",
        "e_invoice_signed_qr_code",
        "updated_at",
    ])
    return result


def generate_e_way_bill_for_voucher(voucher, user=None):
    provider = get_gst_provider()
    payload = build_e_way_bill_payload(voucher)
    result = _call_and_log(voucher.company, voucher, user, provider, IntegrationRequestLog.SERVICE_E_WAY_BILL, payload, lambda: provider.generate_e_way_bill(payload))

    voucher.e_way_bill_no = str(_first_value(result, "EwbNo", "EWBNo", "eway_bill_no", "ewbNo") or voucher.e_way_bill_no)
    voucher.e_way_bill_date = _parse_response_datetime(_first_value(result, "EwbDt", "eway_bill_date", "ewbDt")) or voucher.e_way_bill_date
    voucher.e_way_bill_valid_until = _parse_response_datetime(
        _first_value(result, "EwbValidTill", "ValidUpto", "valid_upto", "validUntil")
    ) or voucher.e_way_bill_valid_until
    voucher.e_way_bill_status = str(_first_value(result, "Status", "status") or voucher.e_way_bill_status or ("ACT" if voucher.e_way_bill_no else ""))
    voucher.save(update_fields=[
        "e_way_bill_no",
        "e_way_bill_date",
        "e_way_bill_status",
        "e_way_bill_valid_until",
        "updated_at",
    ])
    return result


def _call_and_log(company, voucher, user, provider, service, payload, callback):
    log = IntegrationRequestLog.objects.create(
        company=company,
        voucher=voucher,
        requested_by=user if getattr(user, "is_authenticated", False) else None,
        provider=getattr(provider, "provider_name", settings.GST_API_PROVIDER or "unknown"),
        service=service,
        status=IntegrationRequestLog.STATUS_FAILED,
        request_digest=_payload_digest(payload),
    )
    try:
        result = callback()
    except GSTConfigurationError as exc:
        log.status = IntegrationRequestLog.STATUS_CONFIG_ERROR
        log.error_message = str(exc)
        log.save(update_fields=["status", "error_message"])
        raise
    except GSTIntegrationError as exc:
        log.status = IntegrationRequestLog.STATUS_FAILED
        log.error_message = str(exc)
        log.save(update_fields=["status", "error_message"])
        raise

    log.status = IntegrationRequestLog.STATUS_SUCCESS
    log.response_code = str(_first_value(result, "status_code", "code", "status") or "")
    log.response_payload = _safe_response_payload(result)
    log.save(update_fields=["status", "response_code", "response_payload"])
    return result


def _safe_response_payload(result):
    serialized = json.dumps(result, default=_json_default)
    if len(serialized) > 5000:
        return {"truncated": True, "digest": hashlib.sha256(serialized.encode("utf-8")).hexdigest()}
    return json.loads(serialized)


def _json_payload_value(value):
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return {"raw": value}
    return {"raw": str(value)}


def _payload_document_number(payload):
    if "DocDtls" in payload:
        return str(payload["DocDtls"].get("No") or "")
    if "docNo" in payload:
        return str(payload.get("docNo") or "")
    if "document" in payload:
        return str(payload["document"].get("number") or "")
    return "UNKNOWN"


def _party_line(items):
    fallback = None
    for item in items:
        if item.entry_type != "DR" or _is_gst_ledger(item.ledger):
            continue
        if fallback is None:
            fallback = item
        if item.ledger.account_group.nature == "Asset":
            return item
    return fallback


def _supply_lines(items, party_line):
    party_line_id = party_line.id if party_line else None
    return [
        item
        for item in items
        if item.id != party_line_id
        and item.entry_type == "CR"
        and not _is_gst_ledger(item.ledger)
        and item.ledger.account_group.nature == "Income"
    ]


def _is_gst_ledger(ledger):
    name = ledger.name.upper()
    return ledger.account_group.nature == "Tax" or any(
        keyword in name
        for keyword in (
            "CGST",
            "SGST",
            "IGST",
            "UTGST",
            "VAT",
            "TAX PAYABLE",
            "INPUT TAX",
            "OUTPUT TAX",
            "GST PAYABLE",
            "GST INPUT",
            "GST OUTPUT",
        )
    )


def _build_e_invoice_items(voucher, supply_lines, pos, seller_state):
    taxable_total = sum((item.amount for item in supply_lines), ZERO)
    rate = _tax_rate(taxable_total, voucher.total_tax)
    item_list = []
    total_assessable = ZERO
    total_value = ZERO

    for index, item in enumerate(supply_lines, 1):
        assessable = _money(item.amount)
        share = item.amount / taxable_total if taxable_total else ZERO
        cgst = _money((voucher.cgst_amount or ZERO) * share)
        sgst = _money((voucher.sgst_amount or ZERO) * share)
        igst = _money((voucher.igst_amount or ZERO) * share)
        quantity = item.quantity if item.quantity and item.quantity > ZERO else Decimal("1.000")
        unit_price = item.rate if item.rate and item.rate > ZERO else item.amount / quantity
        item_total = _money(_to_decimal(assessable) + _to_decimal(cgst) + _to_decimal(sgst) + _to_decimal(igst))

        item_list.append({
            "SlNo": str(index),
            "PrdDesc": _item_description(item),
            "IsServc": "N" if item.stock_item_id else "Y",
            "HsnCd": _hsn_code(item),
            "Qty": _quantity(quantity),
            "Unit": _unit_code(item),
            "UnitPrice": _money(unit_price),
            "TotAmt": assessable,
            "Discount": _money(ZERO),
            "PreTaxVal": assessable,
            "AssAmt": assessable,
            "GstRt": _money(rate),
            "IgstAmt": igst if pos != seller_state else _money(ZERO),
            "CgstAmt": cgst if pos == seller_state else _money(ZERO),
            "SgstAmt": sgst if pos == seller_state else _money(ZERO),
            "CesRt": _money(ZERO),
            "CesAmt": _money(ZERO),
            "CesNonAdvlAmt": _money(ZERO),
            "StateCesRt": _money(ZERO),
            "StateCesAmt": _money(ZERO),
            "StateCesNonAdvlAmt": _money(ZERO),
            "OthChrg": _money(ZERO),
            "TotItemVal": item_total,
        })
        total_assessable += _to_decimal(assessable)
        total_value += _to_decimal(item_total)

    return item_list, {
        "assessable": _money(total_assessable),
        "total": _money(total_value),
    }


def _party_details(*, gstin, legal_name, address, pincode, state_code, place_of_supply=None):
    addr1, addr2 = _address_lines(address, legal_name)
    data = {
        "Gstin": (gstin or "").strip().upper(),
        "LglNm": (legal_name or "").strip()[:100],
        "TrdNm": (legal_name or "").strip()[:100],
        "Addr1": addr1,
        "Addr2": addr2,
        "Loc": _location(address),
        "Pin": int(pincode or 0),
        "Stcd": state_code or "",
    }
    if place_of_supply:
        data["Pos"] = place_of_supply
    return data


def _address_lines(address, fallback):
    cleaned = " ".join(str(address or fallback or "NA").split())
    return cleaned[:100] or "NA", cleaned[100:200]


def _location(address):
    text = str(address or "").replace("\n", ",")
    parts = [part.strip() for part in text.split(",") if part.strip()]
    return (parts[-2] if len(parts) >= 2 else parts[-1] if parts else "NA")[:50]


def _e_invoice_eway_details(voucher):
    return {
        "TransId": (voucher.transporter_id or "").strip().upper(),
        "TransName": (voucher.transporter_name or "").strip(),
        "TransMode": voucher.transport_mode or "",
        "Distance": int(voucher.transport_distance_km or 0),
        "TransDocNo": (voucher.transport_doc_no or "").strip(),
        "TransDocDt": voucher.transport_doc_date.strftime("%d/%m/%Y") if voucher.transport_doc_date else "",
        "VehNo": (voucher.vehicle_number or "").strip().upper(),
        "VehType": voucher.vehicle_type or "R",
    }


def _has_transport_details(voucher):
    return any([
        voucher.transport_mode,
        voucher.transport_distance_km,
        voucher.transporter_id,
        voucher.transporter_name,
        voucher.transport_doc_no,
        voucher.transport_doc_date,
        voucher.vehicle_number,
        voucher.vehicle_type,
    ])


def _validate_party_details(data, path, errors, *, require_pos):
    if not GSTIN_RE.match(str(data.get("Gstin") or "").upper()):
        errors.append(f"{path}.Gstin is invalid")
    if not data.get("LglNm"):
        errors.append(f"{path}.LglNm is required")
    if not data.get("Addr1"):
        errors.append(f"{path}.Addr1 is required")
    if not PIN_RE.match(str(data.get("Pin") or "")):
        errors.append(f"{path}.Pin must be a valid six-digit pincode")
    if not re.match(r"^[0-9]{2}$", str(data.get("Stcd") or "")):
        errors.append(f"{path}.Stcd is required")
    if require_pos and not re.match(r"^[0-9]{2}$", str(data.get("Pos") or "")):
        errors.append(f"{path}.Pos is required")


def _state_code(gstin):
    gstin = (gstin or "").strip()
    return gstin[:2] if len(gstin) >= 2 else ""


def _hsn_code(item):
    if item.stock_item and item.stock_item.hsn_sac:
        return item.stock_item.hsn_sac.code
    return "999999"


def _unit_code(item):
    if not item.stock_item:
        return "OTH"
    unit = (item.stock_item.unit or "NOS").upper()
    return {
        "NOS": "NOS",
        "NO": "NOS",
        "KGS": "KGS",
        "KG": "KGS",
        "BOXES": "BOX",
        "BOX": "BOX",
        "DOZEN": "DOZ",
        "PIECES": "PCS",
        "METERS": "MTR",
    }.get(unit, unit[:3])


def _item_description(item):
    if item.stock_item:
        return item.stock_item.name[:100]
    return (item.narration or item.ledger.name or "Sales")[:100]


def _tax_rate(taxable_value, total_tax):
    taxable_value = _to_decimal(taxable_value)
    total_tax = _to_decimal(total_tax)
    if taxable_value <= ZERO:
        return ZERO
    return ((total_tax / taxable_value) * Decimal("100")).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _money(value):
    return _to_decimal(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _quantity(value):
    return _to_decimal(value).quantize(Decimal("0.001"), rounding=ROUND_HALF_UP)


def _to_decimal(value):
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError):
        return ZERO
