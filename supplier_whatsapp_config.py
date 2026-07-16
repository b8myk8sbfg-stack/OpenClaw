"""
External supplier WhatsApp destinations and purchasing-line routing.

Purchasing sender (your line): 60167027683
Destinations: phone number OR WhatsApp group name per brand.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()

CONFIG_PATH = os.getenv(
    "OPENCLAW_SUPPLIER_WHATSAPP_CONFIG",
    "/Users/evon/OpenClaw/supplier_whatsapp_numbers.json",
)

PURCHASING_WHATSAPP_BRANDS = frozenset({
    "OMRON",
})


@dataclass(frozen=True)
class SupplierDestination:
    kind: str  # "phone" or "group"
    value: str
    label: str

    @property
    def is_group(self) -> bool:
        return self.kind == "group"


def _load_json_config() -> dict:
    if not os.path.isfile(CONFIG_PATH):
        return {}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            return json.load(handle) or {}
    except (OSError, json.JSONDecodeError):
        return {}


def _normalize_phone(raw: str) -> str:
    return "".join(ch for ch in str(raw or "") if ch.isdigit())


def _parse_destination_entry(brand: str, entry) -> SupplierDestination | None:
    brand = str(brand or "").upper().strip()
    if isinstance(entry, dict):
        kind = str(entry.get("type") or entry.get("kind") or "phone").strip().lower()
        if kind == "group":
            name = str(entry.get("name") or entry.get("group") or "").strip()
            if name:
                return SupplierDestination(kind="group", value=name, label=name)
            return None
        phone = _normalize_phone(entry.get("phone") or entry.get("number") or "")
        if phone:
            return SupplierDestination(kind="phone", value=phone, label=f"+{phone}")
        return None

    raw = str(entry or "").strip()
    if not raw:
        return None
    if raw.lower().startswith("group:"):
        name = raw.split(":", 1)[1].strip()
        return SupplierDestination(kind="group", value=name, label=name)
    phone = _normalize_phone(raw)
    if phone:
        return SupplierDestination(kind="phone", value=phone, label=f"+{phone}")
    return SupplierDestination(kind="group", value=raw, label=raw)


def get_purchasing_sender_phone() -> str:
    cfg = _load_json_config()
    raw = (
        os.getenv("OPENCLAW_PURCHASING_WHATSAPP_PHONE", "").strip()
        or str(cfg.get("purchasing_sender_phone") or "").strip()
        or "60167027683"
    )
    return _normalize_phone(raw)


def get_supplier_destination(brand: str) -> SupplierDestination | None:
    """
    External supplier destination for a brand.
    Phone env: OPENCLAW_{BRAND}_SUPPLIER_WHATSAPP
    Group env: OPENCLAW_{BRAND}_SUPPLIER_WHATSAPP_GROUP
    """
    brand = str(brand or "").upper().strip()

    group_env = os.getenv(f"OPENCLAW_{brand}_SUPPLIER_WHATSAPP_GROUP", "").strip()
    if group_env:
        return SupplierDestination(kind="group", value=group_env, label=group_env)

    phone_env = os.getenv(f"OPENCLAW_{brand}_SUPPLIER_WHATSAPP", "").strip()
    if phone_env and not phone_env.lower().startswith("group:"):
        phone = _normalize_phone(phone_env)
        if phone:
            return SupplierDestination(kind="phone", value=phone, label=f"+{phone}")

    cfg = _load_json_config()
    external = cfg.get("external_suppliers") or {}
    entry = external.get(brand) or external.get(brand.title())
    if entry:
        return _parse_destination_entry(brand, entry)
    return None


def get_external_supplier_whatsapp(brand: str) -> str:
    """Backward-compatible: returns phone digits only (empty for groups)."""
    dest = get_supplier_destination(brand)
    if dest and dest.kind == "phone":
        return dest.value
    return ""


def uses_purchasing_whatsapp(brand: str) -> bool:
    brand = str(brand or "").upper().strip()
    if brand in PURCHASING_WHATSAPP_BRANDS:
        return True
    cfg = _load_json_config()
    extra = cfg.get("purchasing_whatsapp_brands") or []
    return brand in {str(b).upper().strip() for b in extra}


def list_purchasing_whatsapp_brands() -> list[str]:
    cfg = _load_json_config()
    extra = {str(b).upper().strip() for b in (cfg.get("purchasing_whatsapp_brands") or [])}
    return sorted(PURCHASING_WHATSAPP_BRANDS | extra)
