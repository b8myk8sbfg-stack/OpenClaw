"""Bürkert obsolete part cross-reference via Copilot + price-list quote on replacement."""

from __future__ import annotations

import json
import os
import re
from typing import Any

from dotenv import load_dotenv

load_dotenv()

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")
COPILOT_API_KEY = os.getenv("COPILOT_API_KEY", "local-copilot-proxy")
COPILOT_TIMEOUT_SECS = float(os.getenv("OPENCLAW_COPILOT_TIMEOUT_SECS", "90"))
BURKERT_OBSOLETE_LOOKUP = os.getenv("OPENCLAW_BURKERT_OBSOLETE_LOOKUP", "1").strip().lower() not in (
    "0", "false", "no", "off",
)

_BURKERT_ID_RE = re.compile(r"\b(0*\d{5,8})\b")

_COPILOT_SYSTEM = """You are a Bürkert industrial valve specialist with web search access.
Cross-reference distributor catalogs, EOL notices, and replacement matrices.

Return ONLY valid JSON (no markdown fences) with this schema:
{
  "is_obsolete": true,
  "original_part": "00134328",
  "original_specifications": {
    "type": "5281",
    "title": "Type 5281 servo-assisted solenoid valve",
    "specs": [
      {"label": "Valve type", "value": "2/2-way normally closed (NC), pilot-operated"},
      {"label": "Connection size", "value": "DN25 (G1\")"},
      {"label": "Body material", "value": "Brass"},
      {"label": "Seal material", "value": "NBR"},
      {"label": "Voltage", "value": "230 VAC 50/60 Hz"},
      {"label": "Pressure range", "value": "0.2 to 16 bar"},
      {"label": "Flow rate (Kv)", "value": "10 m³/h"}
    ]
  },
  "replacement_parts": [
    {
      "article_id": "00221858",
      "type": "6281",
      "title": "Type 6281 EV servo-assisted solenoid valve",
      "specifications": {
        "specs": [
          {"label": "Valve type", "value": "2/2-way NC, pilot-operated"},
          {"label": "Connection size", "value": "DN25 (G1\")"},
          {"label": "Body material", "value": "Brass"},
          {"label": "Seal material", "value": "NBR"},
          {"label": "Voltage", "value": "230 VAC 50/60 Hz"}
        ]
      },
      "comparison_notes": "Direct functional replacement; same connection size and voltage class.",
      "notes": "Official Bürkert successor to Type 5281 in this size range."
    }
  ],
  "comparison_summary": "One short paragraph highlighting key similarities and any differences the customer should verify.",
  "sources": ["aimfluid.nl", "Burkert catalog"],
  "confidence": "high"
}

Rules:
- IS THIS ITEM OBSOLETE? — explicitly determine obsolete/EOL/discontinued status.
- Provide FULL technical specifications for the REQUESTED obsolete part (use same spec labels where possible).
- Provide FULL technical specifications for each REPLACEMENT part using the SAME spec labels so staff can compare line-by-line.
- Include at minimum where known: valve type, connection size, body/seal material, voltage, pressure range, flow (Kv), coil/article references, dimensions, weight, media.
- If obsolete or not in current catalogs, search for the official Bürkert replacement article number(s).
- Prefer exact Bürkert article IDs (5-8 digits, often zero-padded to 8).
- List replacements most likely to be correct first.
- Use web search; cite real cross-reference data when found."""


def _enabled() -> bool:
    return BURKERT_OBSOLETE_LOOKUP


def _get_copilot_client():
    try:
        from openai import OpenAI
    except ImportError:
        return None
    return OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=COPILOT_API_KEY,
        timeout=COPILOT_TIMEOUT_SECS,
        max_retries=0,
    )


def extract_burkert_article_ids(text: str) -> list[str]:
    """Pull likely Bürkert article numbers from free text (zero-padded 8-digit preferred)."""
    seen: set[str] = set()
    ids: list[str] = []

    def add(raw: str) -> None:
        digits = re.sub(r"[^0-9]", "", str(raw or ""))
        if not digits or len(digits) < 5 or len(digits) > 8:
            return
        display = digits.zfill(8) if len(digits) <= 8 else digits
        if display not in seen:
            seen.add(display)
            ids.append(display)

    for match in _BURKERT_ID_RE.finditer(str(text or "")):
        add(match.group(1))

    return ids


def _extract_json_from_copilot(text: str) -> dict | None:
    text = (text or "").strip()
    if not text:
        return None
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(lines[1:-1] if len(lines) > 2 else lines).strip()
    match = re.search(r"\{[\s\S]*\}", text)
    if not match:
        return None
    try:
        return json.loads(match.group(0))
    except json.JSONDecodeError:
        return None


def copilot_burkert_obsolete_lookup(
    part_no: str,
    *,
    brand: str = "BURKERT",
    search_context: str = "",
    technical_specs: list | None = None,
) -> dict[str, Any] | None:
    """Ask Copilot whether a Bürkert part is obsolete and get replacement article IDs."""
    if not _enabled():
        return None

    part_no = str(part_no or "").strip().upper()
    if not part_no:
        return None

    client = _get_copilot_client()
    if not client:
        print("⚠️ [BURKERT-OBS] openai package unavailable — obsolete lookup skipped.")
        return None

    specs = technical_specs or []
    if isinstance(specs, str):
        specs = [specs]
    context_blob = "\n".join(
        x for x in [search_context, *[str(s) for s in specs if s]] if str(x).strip()
    ).strip()

    payload = {
        "part_number": part_no,
        "brand": brand,
        "question": "IS THIS ITEM OBSOLETE? If yes, what is the official Bürkert replacement?",
        "context": context_blob[:4000],
    }

    print(f"🔎 [BURKERT-OBS] Copilot obsolete/replacement search for {part_no}...")
    try:
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
                {"role": "system", "content": _COPILOT_SYSTEM},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
        )
        text = (response.choices[0].message.content or "").strip()
    except Exception as exc:
        print(f"❌ [BURKERT-OBS] Copilot obsolete lookup failed: {exc}")
        return None

    parsed = _extract_json_from_copilot(text)
    if not parsed:
        print("⚠️ [BURKERT-OBS] Copilot returned malformed JSON for obsolete lookup.")
        return None

    replacements = []
    for row in parsed.get("replacement_parts") or []:
        if not isinstance(row, dict):
            continue
        article_id = str(row.get("article_id") or row.get("part_number") or "").strip()
        if article_id:
            replacements.append(row)

    original_specs = parsed.get("original_specifications")
    if not isinstance(original_specs, dict):
        original_specs = {}
    if not original_specs.get("specs") and parsed.get("product_summary"):
        original_specs = {
            "title": str(parsed.get("product_summary") or "").strip(),
            "specs": [],
        }

    result = {
        "is_obsolete": bool(parsed.get("is_obsolete")),
        "original_part": str(parsed.get("original_part") or part_no).strip().upper(),
        "original_specifications": original_specs,
        "product_summary": str(parsed.get("product_summary") or original_specs.get("title") or "").strip(),
        "replacement_parts": replacements,
        "comparison_summary": str(parsed.get("comparison_summary") or "").strip(),
        "sources": [str(s) for s in (parsed.get("sources") or []) if s],
        "confidence": str(parsed.get("confidence") or "").strip().lower(),
        "raw_text": text,
    }

    if result["is_obsolete"] or replacements:
        print(
            f"   ✅ [BURKERT-OBS] obsolete={result['is_obsolete']} "
            f"replacements={[r.get('article_id') for r in replacements]}"
        )
    else:
        print(f"   ℹ️ [BURKERT-OBS] Copilot: part {part_no} not flagged obsolete / no replacement.")

    return result


def _normalize_spec_rows(spec_data) -> list[dict[str, str]]:
    if isinstance(spec_data, list):
        rows = spec_data
    elif isinstance(spec_data, dict):
        rows = spec_data.get("specs") or []
    else:
        rows = []

    normalized: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label = str(row.get("label") or row.get("name") or "").strip()
        value = str(row.get("value") or "").strip()
        if label and value:
            normalized.append({"label": label, "value": value})
    return normalized


def _format_spec_section(
    heading: str,
    part_id: str,
    spec_data,
    *,
    extra_lines: list[str] | None = None,
) -> list[str]:
    lines = [heading]
    if part_id:
        lines[0] = f"{heading}: {part_id}"

    title = ""
    if isinstance(spec_data, dict):
        title = str(spec_data.get("title") or "").strip()
        typ = str(spec_data.get("type") or "").strip()
        if typ and typ not in title:
            title = f"{title} (Type {typ})".strip() if title else f"Type {typ}"
    if title:
        lines.append(title)

    specs = _normalize_spec_rows(spec_data)
    if specs:
        for spec in specs:
            lines.append(f"  • {spec['label']}: {spec['value']}")
    elif isinstance(spec_data, dict):
        fallback = str(spec_data.get("description") or spec_data.get("notes") or "").strip()
        if fallback:
            lines.append(f"  • Description: {fallback}")

    for extra in extra_lines or []:
        extra = str(extra or "").strip()
        if extra:
            lines.append(f"  • {extra}")

    return lines


def format_obsolete_research_summary(
    obsolete_info: dict[str, Any],
    *,
    quoted_replacement: dict[str, Any] | None = None,
) -> str:
    """Customer-facing obsolete comparison: full specs for requested + replacement parts."""
    if not obsolete_info:
        return ""

    lines: list[str] = []
    original = str(obsolete_info.get("original_part") or "").strip()
    quoted = quoted_replacement or obsolete_info.get("quoted_replacement") or {}
    quoted_id = str(quoted.get("article_id") or quoted.get("replacement_part") or "").strip()

    if obsolete_info.get("is_obsolete"):
        lines.append("Status: Requested part is obsolete / discontinued.")
    lines.append("")

    lines.extend(
        _format_spec_section(
            "REQUESTED PART (OBSOLETE)",
            original,
            obsolete_info.get("original_specifications") or {},
        )
    )
    lines.append("")

    replacement_rows = obsolete_info.get("replacement_parts") or []
    chosen_rep = None
    for rep in replacement_rows:
        rid = str(rep.get("article_id") or "").strip()
        if quoted_id and rid.replace(" ", "") == quoted_id.replace(" ", ""):
            chosen_rep = rep
            break
    if not chosen_rep and replacement_rows:
        chosen_rep = replacement_rows[0]

    if chosen_rep or quoted_id:
        rep_id = quoted_id or str(chosen_rep.get("article_id") or "").strip()
        rep_specs = {}
        if isinstance(chosen_rep, dict):
            rep_specs = chosen_rep.get("specifications") or chosen_rep
            if isinstance(rep_specs, dict) and not rep_specs.get("specs"):
                rep_specs = {
                    "title": chosen_rep.get("title") or chosen_rep.get("description") or "",
                    "type": chosen_rep.get("type") or quoted.get("type") or "",
                    "specs": [],
                }
        elif quoted:
            rep_specs = {
                "title": quoted.get("description") or quoted.get("title") or "",
                "type": quoted.get("type") or "",
                "specs": _normalize_spec_rows(quoted.get("specs")),
            }

        extra_lines = []
        if quoted.get("price_list_description"):
            extra_lines.append(f"Price list description: {quoted['price_list_description']}")
        if isinstance(chosen_rep, dict) and chosen_rep.get("comparison_notes"):
            extra_lines.append(f"Comparison: {chosen_rep['comparison_notes']}")

        lines.extend(
            _format_spec_section(
                "RECOMMENDED REPLACEMENT (QUOTED)",
                rep_id,
                rep_specs,
                extra_lines=extra_lines,
            )
        )
        lines.append("")

    comparison = str(obsolete_info.get("comparison_summary") or "").strip()
    if comparison:
        lines.append("COMPARISON SUMMARY")
        lines.append(comparison)
        lines.append("")

    sources = obsolete_info.get("sources") or []
    if sources:
        lines.append(f"Sources: {', '.join(sources[:4])}")

    return "\n".join(line for line in lines if line is not None).strip()


def try_burkert_replacement_quote(
    part_no: str,
    qty: int = 1,
    *,
    brand: str = "BURKERT",
    search_context: str = "",
    technical_specs: list | None = None,
    markup_divisor: float = 0.72,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """
    When original part has no price list hit, ask Copilot for replacement and quote it.

    Returns (quote_row_dict, obsolete_info).
    """
    from burkert_price_list import lookup_burkert_quote

    obsolete_info = copilot_burkert_obsolete_lookup(
        part_no,
        brand=brand,
        search_context=search_context,
        technical_specs=technical_specs,
    )
    if not obsolete_info:
        return None, None

    replacement_ids: list[str] = []
    for rep in obsolete_info.get("replacement_parts") or []:
        article_id = str(rep.get("article_id") or "").strip()
        if article_id:
            replacement_ids.append(article_id)

    if not replacement_ids:
        ids_from_text = extract_burkert_article_ids(obsolete_info.get("raw_text") or "")
        original_norm = re.sub(r"[^0-9]", "", str(part_no))
        for candidate in ids_from_text:
            if re.sub(r"[^0-9]", "", candidate) != original_norm:
                replacement_ids.append(candidate)

    seen: set[str] = set()
    for replacement_id in replacement_ids:
        rid = replacement_id.upper().strip()
        if rid in seen:
            continue
        seen.add(rid)

        quote = lookup_burkert_quote(
            rid,
            qty=qty,
            markup_divisor=markup_divisor,
            search_context=search_context,
            burkert_id=rid,
        )
        if not quote or quote.get("price") == "[TBC]":
            print(f"   ⚠️ [BURKERT-OBS] Replacement {rid} not in price list.")
            continue

        customer_part = str(part_no or "").strip().upper()
        rep_display = quote.get("burkert_id_display") or rid
        desc = quote.get("desc") or f"BURKERT {rep_display}"
        desc = (
            f"{desc} (replaces obsolete {customer_part} → quoted equivalent {rep_display})"
        )

        print(
            f"   ✅ [BURKERT-OBS] Quoted replacement {rep_display} for obsolete {customer_part}: "
            f"RM {quote.get('price')} | LT {quote.get('lt')}"
        )

        quoted_replacement = {
            "article_id": rep_display,
            "type": quote.get("type") or "",
            "description": quote.get("desc") or "",
            "price_list_description": quote.get("desc") or "",
            "price": quote.get("price"),
            "lt": quote.get("lt"),
        }
        obsolete_info["quoted_replacement"] = quoted_replacement

        row = {
            "desc": desc,
            "qty": int(quote.get("qty") or qty),
            "price": quote.get("price", "[TBC]"),
            "lt": quote.get("lt", "[TBC]"),
            "pid": customer_part,
            "customer_part": customer_part,
            "replacement_part": rep_display,
            "obsolete_original": customer_part,
            "brand": "BURKERT",
            "source": "BURKERT_PRICE_LIST_REPLACEMENT",
            "needs_supplier": False,
            "obsolete_info": obsolete_info,
            "obsolete_research": format_obsolete_research_summary(
                obsolete_info,
                quoted_replacement=quoted_replacement,
            ),
        }
        return row, obsolete_info

    if obsolete_info.get("is_obsolete") or obsolete_info.get("original_specifications"):
        obsolete_info["obsolete_research"] = format_obsolete_research_summary(obsolete_info)
    return None, obsolete_info if obsolete_info.get("is_obsolete") or obsolete_info.get("product_summary") else None
