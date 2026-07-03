"""
Analyze customer inquiry photos/screenshots using GPT-4o vision + Tavily web search.
Returns structured part lines compatible with openclaw_inquiry_engine.process_inquiry_text().
"""

import base64
import json
import os
import re
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from openai import OpenAI
from tavily import TavilyClient

load_dotenv()

VERSION = "v1.00-IMAGE-VISION-WEB-VERIFY"

VISION_MODEL = os.getenv("OPENAI_VISION_MODEL", "gpt-4o")
TEXT_MODEL = os.getenv("OPENAI_TEXT_MODEL", "gpt-4o")
MAX_WEB_VERIFY_ITEMS = int(os.getenv("IMAGE_INQUIRY_MAX_WEB_VERIFY", "5"))

KNOWN_BRANDS = (
    "OMRON", "SMC", "BURKERT", "BÜRKERT", "LEGRIS", "PANASONIC", "PISCO", "THK",
    "LOCTITE", "KEYENCE", "FESTO", "SICK", "IFM", "PARKER", "ABB", "SIEMENS",
    "MITSUBISHI", "YASKAWA", "SCHNEIDER", "DANFOSS", "EATON", "PHOENIX",
)

VISION_SYSTEM = (
    "You extract industrial automation parts from photos and screenshots. "
    "Customers send labels, nameplates, BOM lists, chat screenshots, or product photos. "
    "Return ONLY valid JSON, no markdown."
)

VISION_USER = """Analyze this image for industrial parts inquiry.

Also consider any caption text the customer typed:
{caption}

Extract every distinct part/model/order code you can read. For each item return:
- part_no: the clearest model/part/order number (required)
- brand: manufacturer if visible or strongly implied, else "UNKNOWN"
- qty: integer quantity if visible, else 1
- description: short product description if visible
- confidence: high | medium | low

Return JSON exactly like:
{{"items": [{{"part_no": "E3Z-T61", "brand": "OMRON", "qty": 2, "description": "photo sensor", "confidence": "high"}}], "notes": "optional notes"}}

Rules:
- Prefer exact printed model numbers over guesses.
- For BURKERT / BÜRKERT labels, use the numeric ID-No as part_no (example: ID-No 00126094).
- Read barcode numbers when visible; include voltage on solenoids/relays (example: 230V 50Hz).
- Do not invent part numbers.
- If nothing readable, return {{"items": [], "notes": "reason"}}.
"""

REFINE_SYSTEM = (
    "You refine industrial part numbers using OCR output and short web search snippets. "
    "Return ONLY valid JSON."
)

REFINE_USER = """Original extraction:
{original}

Web search snippets:
{snippets}

Return corrected JSON:
{{"part_no": "...", "brand": "...", "qty": 1, "description": "...", "confidence": "high|medium|low"}}

Use web snippets only to confirm spelling/brand. Do not invent parts not supported by the image or search.
If still uncertain, keep confidence low but preserve the best visible part number.
"""


def _strip_json_fence(text: str) -> str:
    text = str(text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = _strip_json_fence(text)
    return json.loads(cleaned)


def _mime_for_path(path: str) -> str:
    ext = os.path.splitext(path)[1].lower()
    if ext in (".jpg", ".jpeg"):
        return "image/jpeg"
    if ext == ".webp":
        return "image/webp"
    return "image/png"


def _normalize_brand(brand: str) -> str:
    brand = str(brand or "UNKNOWN").strip().upper()
    brand = brand.replace("BÜRKERT", "BURKERT")
    if brand in KNOWN_BRANDS or brand.replace("BÜRKERT", "BURKERT") in KNOWN_BRANDS:
        return brand.replace("BÜRKERT", "BURKERT")
    return brand if brand else "UNKNOWN"


def _normalize_item(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    part_no = re.sub(r"\s+", " ", str(raw.get("part_no") or "")).strip().upper()
    if not part_no or len(re.sub(r"[^A-Z0-9]", "", part_no)) < 3:
        return None

    qty_raw = raw.get("qty", 1)
    try:
        qty = max(1, int(qty_raw))
    except Exception:
        qty = 1

    return {
        "part_no": part_no,
        "brand": _normalize_brand(raw.get("brand")),
        "qty": qty,
        "description": str(raw.get("description") or "").strip(),
        "confidence": str(raw.get("confidence") or "medium").strip().lower(),
    }


def _build_inquiry_text(items: List[Dict[str, Any]]) -> str:
    lines = []

    for item in items:
        part_no = item["part_no"]
        qty = item["qty"]
        brand = item.get("brand", "UNKNOWN")

        if brand and brand != "UNKNOWN":
            lines.append(f"BRAND : {brand} PART NO. : {part_no} QUANTITY : {qty}")
        else:
            lines.append(f"{part_no} Qty:{qty}")

    return "\n".join(lines)


def _vision_extract(client: OpenAI, image_b64: str, mime: str, caption_text: str) -> Dict[str, Any]:
    data_url = f"data:{mime};base64,{image_b64}"

    response = client.chat.completions.create(
        model=VISION_MODEL,
        messages=[
            {"role": "system", "content": VISION_SYSTEM},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": VISION_USER.format(caption=caption_text or "(none)")},
                    {"type": "image_url", "image_url": {"url": data_url, "detail": "high"}},
                ],
            },
        ],
        temperature=0.1,
        max_tokens=1200,
    )

    content = response.choices[0].message.content or ""
    print(f"🖼️ [IMAGE] Vision raw response:\n{content}")

    parsed = _parse_json_object(content)
    items = []

    for raw in parsed.get("items", []):
        item = _normalize_item(raw)
        if item:
            items.append(item)

    return {"items": items, "notes": str(parsed.get("notes") or "").strip()}


def _web_snippets(tavily: TavilyClient, item: Dict[str, Any]) -> str:
    brand = item.get("brand", "UNKNOWN")
    part_no = item.get("part_no", "")
    desc = item.get("description", "")

    query = f"{brand} {part_no} {desc} industrial automation part number".strip()
    print(f"🌐 [IMAGE] Web verify search: {query}")

    try:
        result = tavily.search(query=query, search_depth="basic", max_results=3)
    except Exception as e:
        print(f"⚠️ [IMAGE] Tavily search failed: {e}")
        return ""

    chunks = []
    for hit in result.get("results", []):
        title = hit.get("title", "")
        content = hit.get("content", "")
        url = hit.get("url", "")
        chunks.append(f"{title}\n{content}\n{url}")

    return "\n\n---\n\n".join(chunks)


def _refine_item_with_web(client: OpenAI, item: Dict[str, Any], snippets: str) -> Dict[str, Any]:
    if not snippets.strip():
        return item

    response = client.chat.completions.create(
        model=TEXT_MODEL,
        messages=[
            {"role": "system", "content": REFINE_SYSTEM},
            {
                "role": "user",
                "content": REFINE_USER.format(
                    original=json.dumps(item, ensure_ascii=False),
                    snippets=snippets[:4000],
                ),
            },
        ],
        temperature=0.1,
        max_tokens=400,
    )

    content = response.choices[0].message.content or ""
    refined = _normalize_item(_parse_json_object(content))
    return refined or item


def _verify_items_with_web(
    client: OpenAI,
    tavily: TavilyClient,
    items: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    verified = []
    web_calls = 0

    for item in items:
        needs_web = (
            item.get("confidence") in ("low", "medium")
            or item.get("brand") in ("UNKNOWN", "")
        )

        if needs_web and web_calls < MAX_WEB_VERIFY_ITEMS:
            snippets = _web_snippets(tavily, item)
            item = _refine_item_with_web(client, item, snippets)
            web_calls += 1

        verified.append(item)

    return verified


def analyze_inquiry_image(image_path: str, caption_text: str = "") -> Dict[str, Any]:
    """
    Analyze a saved image file and return inquiry text for the OpenClaw engine.

    Returns:
        {
            "items": [...],
            "inquiry_text": "PART Qty:1\\n...",
            "notes": "...",
            "source": "image"
        }
    """
    if not os.path.exists(image_path):
        raise FileNotFoundError(f"Image not found: {image_path}")

    if not os.getenv("OPENAI_API_KEY"):
        raise RuntimeError("OPENAI_API_KEY is not set")

    print("")
    print("=" * 90)
    print(f"🖼️ [IMAGE] START IMAGE INQUIRY ANALYSIS ({VERSION})")
    print(f"   File: {image_path}")
    print(f"   Caption: {caption_text or '(none)'}")
    print("=" * 90)

    with open(image_path, "rb") as f:
        image_b64 = base64.standard_b64encode(f.read()).decode("utf-8")

    mime = _mime_for_path(image_path)
    client = OpenAI()
    tavily = TavilyClient(api_key=os.environ["TAVILY_API_KEY"]) if os.getenv("TAVILY_API_KEY") else None

    vision_result = _vision_extract(client, image_b64, mime, caption_text)
    items = vision_result.get("items", [])
    notes = vision_result.get("notes", "")

    if items and tavily:
        items = _verify_items_with_web(client, tavily, items)

    inquiry_text = _build_inquiry_text(items)

    print("🖼️ [IMAGE] Extracted items:")
    for item in items:
        print(
            f"   - {item.get('brand')} {item.get('part_no')} | Qty: {item.get('qty')} | "
            f"Conf: {item.get('confidence')}"
        )
    print(f"🖼️ [IMAGE] Inquiry text:\n{inquiry_text or '(empty)'}")
    print("=" * 90)
    print("✅ [IMAGE] END IMAGE INQUIRY ANALYSIS")
    print("=" * 90)

    return {
        "items": items,
        "inquiry_text": inquiry_text,
        "notes": notes,
        "source": "image",
        "image_path": image_path,
    }
