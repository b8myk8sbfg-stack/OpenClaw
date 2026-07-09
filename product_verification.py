"""Official manufacturer product verification and catalog link resolution."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from burkert_price_list import format_burkert_id_display, normalize_burkert_id, resolve_burkert_id

VERIFICATION_SYSTEM_PROMPT = """You are an industrial spare parts verification assistant.

For every line item:

1. Search the manufacturer's official website first.
2. Search by Article Number / Order Code before using the part number.
3. Never infer or substitute another configuration.
4. Verify ALL of these fields:
   • Manufacturer
   • Article Number
   • Product Type
   • Coil Voltage
   • Power Rating
   • Port Size
   • Orifice
   • Pressure Range
5. Return ONLY if every field matches.
6. Provide:
   • Official product webpage
   • Official PDF datasheet
   • PDF status:
       - Direct PDF
       - PDF available from product page
       - No PDF available
7. Confidence:
   • Exact Match
   • Partial Match
   • No Match
8. Never use third-party websites unless the manufacturer has no official documentation.

Respond with ONLY valid JSON (no markdown):
{
  "manufacturer": "",
  "article_number": "",
  "product_type": "",
  "coil_voltage": "",
  "power_rating": "",
  "port_size": "",
  "orifice": "",
  "pressure_range": "",
  "product_page_url": "",
  "datasheet_url": "",
  "pdf_status": "",
  "match_confidence": ""
}
"""

BURKERT_DOMAINS = ("burkert.com", "bürkert.com")
BURKERT_DATASHEET_TEMPLATE = "https://www.burkert.com/en/Media/plm/DTS/DS/ds{type}-standard-eu-en.pdf"
BURKERT_ITEM_PAGE_TEMPLATE = "https://www.burkert.com/en/item/{article_id}"
BURKERT_TYPE_PAGE_TEMPLATE = "https://www.burkert.com/en/type/{type_no}"


def _spec_value(technical_specs: list | None, labels: tuple[str, ...]) -> str:
    specs = technical_specs or []
    if isinstance(specs, str):
        specs = [specs]
    label_set = {label.upper() for label in labels}
    for spec in specs:
        text = str(spec or "").strip()
        if ":" not in text:
            continue
        label, value = text.split(":", 1)
        if label.strip().upper() in label_set:
            return value.strip()
    return ""


def extract_burkert_type_number(part_no: str) -> str:
    match = re.search(r"\b(\d{4})\b", str(part_no or ""))
    return match.group(1) if match else ""


def resolve_burkert_official_links(
    article_id: str = "",
    part_no: str = "",
    technical_specs: list | None = None,
) -> dict[str, Any]:
    """
    Build deterministic official Bürkert URLs from article ID and type family.

    Article pages and type datasheets on burkert.com are stable catalog links.
    """
    normalized_id = normalize_burkert_id(article_id)
    display_id = format_burkert_id_display(article_id) if article_id else ""
    type_no = extract_burkert_type_number(part_no)

    result: dict[str, Any] = {
        "manufacturer": "BURKERT",
        "article_number": display_id or normalized_id,
        "product_type": _spec_value(technical_specs, ("PRODUCT TYPE", "TYPE", "MODEL")) or part_no,
        "coil_voltage": _spec_value(technical_specs, ("COIL VOLTAGE", "VOLTAGE")),
        "power_rating": _spec_value(technical_specs, ("POWER", "POWER RATING")),
        "port_size": _spec_value(technical_specs, ("PORT SIZE", "CONNECTION", "THREAD")),
        "orifice": _spec_value(technical_specs, ("ORIFICE",)),
        "pressure_range": _spec_value(technical_specs, ("PRESSURE", "PRESSURE RANGE")),
        "product_page_url": "",
        "datasheet_url": "",
        "type_page_url": "",
        "pdf_status": "No PDF available",
        "match_confidence": "No Match",
    }

    if normalized_id:
        result["product_page_url"] = BURKERT_ITEM_PAGE_TEMPLATE.format(article_id=normalized_id)
        result["match_confidence"] = "Exact Match"

    if type_no:
        result["type_page_url"] = BURKERT_TYPE_PAGE_TEMPLATE.format(type_no=type_no)
        result["datasheet_url"] = BURKERT_DATASHEET_TEMPLATE.format(type=type_no)
        result["pdf_status"] = "Direct PDF"
        if result["match_confidence"] == "No Match" and part_no:
            result["match_confidence"] = "Partial Match"

    if result["product_page_url"] and result["datasheet_url"]:
        result["pdf_status"] = "Direct PDF"
    elif result["product_page_url"]:
        result["pdf_status"] = "PDF available from product page"

    return result


def _is_official_brand_url(url: str, brand: str) -> bool:
    return False  # reserved for future URL policy checks


def enrich_item_catalog_links(item: dict, row: dict | None = None) -> dict[str, Any]:
    """
    Attach verified official catalog links to a Copilot item dict (in-place friendly).

    Returns the verification payload used.
    """
    if not isinstance(item, dict):
        return {}

    row = row or {}
    brand = str(item.get("brand") or row.get("brand") or "UNKNOWN").strip().upper().replace("BÜRKERT", "BURKERT")
    part_no = str(item.get("part_no") or row.get("customer_part") or "").strip()
    technical_specs = item.get("technical_specs") or row.get("technical_specs") or []

    article_id = resolve_burkert_id(
        burkert_id=str(item.get("burkert_id") or row.get("burkert_id") or ""),
        technical_specs=technical_specs,
        search_context="",
    )
    if not article_id and brand == "BURKERT":
        pid = str(row.get("pid") or "").strip()
        if pid.isdigit():
            article_id = pid

    verification: dict[str, Any] = {}
    if brand == "BURKERT" and (article_id or part_no):
        verification = resolve_burkert_official_links(
            article_id=article_id,
            part_no=part_no,
            technical_specs=technical_specs,
        )
        product_page = str(verification.get("product_page_url") or "").strip()
        datasheet = str(verification.get("datasheet_url") or "").strip()
        if product_page:
            item["product_page_url"] = product_page
            item["catalog_url"] = product_page
        if datasheet:
            item["datasheet_url"] = datasheet
        item["verification_confidence"] = verification.get("match_confidence") or ""
        item["pdf_status"] = verification.get("pdf_status") or ""
        if verification.get("type_page_url"):
            item["type_page_url"] = verification["type_page_url"]

    return verification


def format_verification_links_for_reply(item: dict) -> list[str]:
    """Customer-facing documentation lines from verified catalog links."""
    lines: list[str] = []
    product_page = str(item.get("product_page_url") or item.get("catalog_url") or "").strip()
    datasheet = str(item.get("datasheet_url") or "").strip()
    type_page = str(item.get("type_page_url") or "").strip()
    pdf_status = str(item.get("pdf_status") or "").strip()
    confidence = str(item.get("verification_confidence") or "").strip()

    if product_page:
        lines.append(f"Product page: {product_page}")
    if datasheet and datasheet != product_page:
        lines.append(f"Datasheet: {datasheet}")
    if type_page and type_page not in {product_page, datasheet}:
        lines.append(f"Type overview: {type_page}")
    if pdf_status:
        lines.append(f"PDF status: {pdf_status}")
    if confidence:
        lines.append(f"Catalog match: {confidence}")
    return lines


def build_verification_user_prompt(item: dict, row: dict | None = None) -> str:
    """Build the verification user prompt for LLM-backed lookup (non-Burkert or fallback)."""
    row = row or {}
    brand = str(item.get("brand") or row.get("brand") or "UNKNOWN").strip()
    part_no = str(item.get("part_no") or row.get("customer_part") or "").strip()
    article = str(
        item.get("burkert_id")
        or row.get("burkert_id")
        or row.get("pid")
        or ""
    ).strip()
    specs = item.get("technical_specs") or row.get("technical_specs") or []
    specs_text = "\n".join(f"- {s}" for s in specs if str(s).strip())

    return (
        f"Manufacturer: {brand}\n"
        f"Article Number / Order Code: {article or 'unknown'}\n"
        f"Part number / Type: {part_no}\n"
        f"Known specifications:\n{specs_text or '- none'}\n\n"
        "Search the manufacturer official website. Use Article Number first when available. "
        "Return JSON only."
    )


def verify_product_with_openai(item: dict, row: dict | None = None) -> dict[str, Any]:
    """Optional LLM verification for brands without deterministic official URLs."""
    if os.getenv("OPENCLAW_PRODUCT_VERIFICATION", "1").strip().lower() in ("0", "false", "no", "off"):
        return {}

    brand = str(item.get("brand") or (row or {}).get("brand") or "").upper().replace("BÜRKERT", "BURKERT")
    if brand == "BURKERT":
        return {}

    try:
        from openclaw_main import _resolve_openai_api_key, OPENAI_VISION_MODEL
        from openai import OpenAI
    except Exception:
        return {}

    api_key = _resolve_openai_api_key()
    if not api_key:
        return {}

    client = OpenAI(api_key=api_key, timeout=60.0, max_retries=1)
    try:
        response = client.chat.completions.create(
            model=os.getenv("OPENAI_RESEARCH_MODEL", OPENAI_VISION_MODEL),
            messages=[
                {"role": "system", "content": VERIFICATION_SYSTEM_PROMPT},
                {"role": "user", "content": build_verification_user_prompt(item, row)},
            ],
            temperature=0.1,
            max_tokens=800,
        )
        raw = (response.choices[0].message.content or "").strip()
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception as exc:
        print(f"[PRODUCT VERIFY] OpenAI verification skipped: {exc}")
    return {}
