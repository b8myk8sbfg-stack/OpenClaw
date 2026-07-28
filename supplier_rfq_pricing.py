"""
Supplier reply pricing — PIAB (SGD) vs standard RM brands.
"""

from __future__ import annotations

import os
import re

import requests
from dotenv import load_dotenv

load_dotenv()

MARKUP_DIVISOR = float(os.getenv("OPENCLAW_MARKUP_DIVISOR", "0.72"))
PIAB_MARKUP_DIVISOR = float(os.getenv("OPENCLAW_PIAB_MARKUP_DIVISOR", "0.72"))
PIAB_SALES_TAX = float(os.getenv("OPENCLAW_PIAB_SALES_TAX", "0.10"))
PIAB_COURIER_RM = float(os.getenv("OPENCLAW_PIAB_COURIER_RM", "300"))
SGD_TO_RM_DEFAULT = float(os.getenv("OPENCLAW_SGD_TO_RM_RATE_DEFAULT", "3.16"))


def _parse_amount(raw: str) -> float | None:
    text = str(raw or "").upper()
    text = text.replace(",", "")
    match = re.search(r"\d+(?:\.\d+)?", text)
    if not match:
        return None
    try:
        return float(match.group(0))
    except ValueError:
        return None


def parse_supplier_price_amount(raw: str) -> tuple[float | None, str]:
    """Return (amount, currency) where currency is SGD or RM."""
    upper = str(raw or "").upper()
    amount = _parse_amount(upper)
    if amount is None:
        return None, "RM"
    if "SGD" in upper or "S$" in upper:
        return amount, "SGD"
    return amount, "RM"


def get_sgd_to_rm_rate() -> float:
    env_rate = os.getenv("OPENCLAW_SGD_TO_RM_RATE", "").strip()
    if env_rate:
        try:
            return float(env_rate)
        except ValueError:
            pass

    try:
        response = requests.get(
            "https://api.frankfurter.app/latest?from=SGD&to=MYR",
            timeout=5,
        )
        if response.ok:
            rate = response.json().get("rates", {}).get("MYR")
            if rate:
                return float(rate)
    except Exception:
        pass

    return SGD_TO_RM_DEFAULT


def calc_piab_unit_sell_rm(sgd_unit_price: float, exchange_rate: float | None = None) -> float:
    rate = exchange_rate if exchange_rate is not None else get_sgd_to_rm_rate()
    rm_cost = float(sgd_unit_price) * rate
    rm_with_markup = rm_cost / PIAB_MARKUP_DIVISOR
    return rm_with_markup * (1 + PIAB_SALES_TAX)


def calc_customer_unit_price(
    supplier_amount: float,
    currency: str,
    brand: str,
    *,
    exchange_rate: float | None = None,
) -> float:
    brand = str(brand or "").upper().strip()
    currency = str(currency or "RM").upper().strip()

    if brand == "PIAB" and currency == "SGD":
        return calc_piab_unit_sell_rm(supplier_amount, exchange_rate=exchange_rate)

    cost_rm = float(supplier_amount)
    if currency == "SGD":
        rate = exchange_rate if exchange_rate is not None else get_sgd_to_rm_rate()
        cost_rm = float(supplier_amount) * rate

    return cost_rm / MARKUP_DIVISOR


def piab_order_courier_rm(brand: str) -> float:
    return PIAB_COURIER_RM if str(brand or "").upper().strip() == "PIAB" else 0.0


def parse_supplier_reply_items(section: str, brand: str = "UNKNOWN") -> list[dict]:
    """Parse supplier copy-paste reply blocks into priced customer line items."""
    brand = str(brand or "UNKNOWN").upper().strip()
    items = []

    block_pattern = re.compile(
        r"(\d+)\)\s*(.*?)\n"
        r"\s*Qty\s*:\s*(\d+)\s*\n"
        r"\s*Price\s*:\s*([^\n]*)\n"
        r"\s*Lead\s*Time\s*:\s*([^\n]*)",
        re.I | re.S,
    )

    for idx, desc, qty, supplier_price_raw, lead_time in block_pattern.findall(section):
        desc = re.sub(r"\s+", " ", desc).strip()
        qty = int(qty)
        supplier_price_raw = supplier_price_raw.strip()
        lead_time = lead_time.strip()

        if not supplier_price_raw or not lead_time:
            continue
        if "SAMPLE ITEM" in desc.upper() or "[ANY ITEM" in desc.upper():
            continue

        supplier_amount, currency = parse_supplier_price_amount(supplier_price_raw)
        if supplier_amount is None:
            continue

        customer_unit_price = calc_customer_unit_price(
            supplier_amount, currency, brand
        )
        customer_subtotal = customer_unit_price * qty

        items.append(
            {
                "idx": int(idx),
                "desc": desc,
                "qty": qty,
                "supplier_cost_raw": supplier_price_raw,
                "supplier_cost": supplier_amount,
                "supplier_currency": currency,
                "customer_unit_price": customer_unit_price,
                "customer_subtotal": customer_subtotal,
                "lead_time": lead_time,
            }
        )

    return items


def build_customer_update_from_supplier(ref: str, brand: str, parsed_items: list[dict]) -> str:
    brand = str(brand or "UNKNOWN").upper().strip()
    msg = (
        f"Hi, we have received supplier update for your inquiry.\n\n"
        f"Ref: {ref}\nBrand: {brand}\n\n"
    )

    total = 0.0
    for item in parsed_items:
        total += item["customer_subtotal"]
        msg += f"{item['idx']}) {item['desc']}\n"
        msg += f"Qty: {item['qty']}\n"
        msg += f"Unit Price: RM {item['customer_unit_price']:,.2f}\n"
        msg += f"Lead Time: {item['lead_time']}\n"
        msg += f"Subtotal: RM {item['customer_subtotal']:,.2f}\n\n"

    courier = piab_order_courier_rm(brand)
    if courier:
        total += courier
        msg += f"Transport & Courier: RM {courier:,.2f}\n\n"

    msg += f"Total: RM {total:,.2f}\n\nThank you."
    return msg
