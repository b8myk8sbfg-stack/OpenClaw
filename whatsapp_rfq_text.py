"""Pure helpers for detecting standalone WhatsApp text RFQs (no Selenium)."""

from __future__ import annotations

import re


def is_trivial_ack(text) -> bool:
    compact = re.sub(r"[^a-zA-Z]", "", str(text or "")).upper()
    return compact in {
        "YA",
        "YAA",
        "YUP",
        "YES",
        "SURE",
        "OK",
        "OKAY",
        "K",
        "KK",
        "NOTED",
        "THANKS",
        "THX",
        "TY",
        "GOOD",
        "FINE",
        "ALRIGHT",
        "SAME",
        "YEP",
        "YEAH",
        "ROGER",
        "COPY",
        "GOTIT",
        "RECEIVED",
    }


def text_has_explicit_part_number(text) -> bool:
    """
    True when a text bubble names a part number (standalone RFQ — do not pair with an older image).
    E.g. 'Quote me 2 pcs of SMC C96SDB40-50C'
    """
    text_u = str(text or "").upper().strip()
    if not text_u:
        return False

    brand_part = re.search(
        r"\b(SMC|OMRON|BURKERT|BÜRKERT|FESTO|SICK|IFM|PANASONIC|KEYENCE|LEGRIS|PISCO|"
        r"PARKER|ABB|SIEMENS|THK|LOCTITE)\b",
        text_u,
    )
    if brand_part:
        if re.search(r"\bSMC[-\s/]+[A-Z0-9][A-Z0-9\-]+", text_u):
            return True
        if re.search(r"[A-Z]{1,4}[-]?\d{2,}[A-Z0-9\-/]*", text_u):
            return True
        if re.search(r"\b[A-Z]\d{2}[A-Z]{2,}\d{2,}[A-Z0-9\-]*\b", text_u):
            return True

    if re.search(r"\bSMC[-\s/]+[A-Z0-9][A-Z0-9\-]+", text_u):
        return True
    if re.search(r"\b[A-Z]{2,}(?:-[A-Z0-9]{1,4})+-\d{1,4}[A-Z0-9\-]*\b", text_u):
        return True
    if re.search(r"\b[A-Z]\d{2}[A-Z]{2,}\d{2,}[A-Z0-9\-]*\b", text_u):
        return True
    if re.search(r"\b[A-Z]{2,}\d{3,}[A-Z0-9#\-/]+\b", text_u):
        return True
    if re.search(r"\b\d{1,2}[-][A-Z]{2,}\d{2,}[A-Z0-9\-]*\b", text_u):
        return True
    if re.search(r"\b[A-Z]{1,4}-\d{2,}(?:-\d{1,4}[A-Z0-9\-]*)?\b", text_u):
        return True
    return False


def is_standalone_text_rfq(text) -> bool:
    """Text bubble that is its own RFQ (not a caption for a paired image)."""
    if text_has_explicit_part_number(text):
        return True
    text_u = str(text or "").upper().strip()
    if not text_u:
        return False
    if not re.search(
        r"\b(QUOTE|QUOTATION|RFQ|ENQ|ADD QUOTE|PLS QUOTE|KINDLY QUOTE|QUOTE ME|MORNING PLS)\b",
        text_u,
    ):
        return False
    if re.search(r"\bSMC[-\s/]+[A-Z0-9][A-Z0-9\-]+", text_u):
        return True
    if re.search(r"\b[A-Z]{2,}(?:-[A-Z0-9]{1,4})+-\d{1,4}[A-Z0-9\-]*\b", text_u):
        return True
    if re.search(r"\b[A-Z]{1,4}-\d{2,}(?:-\d{1,4}[A-Z0-9\-]*)?\b", text_u):
        return True
    return False


def collect_trailing_text_rfqs(working):
    """Consecutive trailing text bubbles that are each standalone RFQs."""
    trailing = []
    for unit in reversed(working or []):
        if unit.get("kind") != "text":
            break
        text = unit.get("text") or ""
        if is_trivial_ack(text):
            break
        if is_standalone_text_rfq(text):
            trailing.insert(0, unit)
        else:
            break
    return trailing
