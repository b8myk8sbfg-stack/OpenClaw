"""
WhatsApp message classification for OpenClaw.

Detects media type (text, image, voice, PDF, Office docs, etc.) and business
intent (RFQ, purchase order, technical support, etc.). Stores classifications
for incremental few-shot learning via Copilot.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import OpenAI
from selenium.webdriver.common.by import By

from message_learning_store import (
    apply_correction,
    few_shot_examples,
    record_classification_example,
    save_last_classification_context,
)

BASE_DIR = "/Users/evon/OpenClaw"
CLASSIFICATION_LOG = os.path.join(BASE_DIR, "whatsapp_message_classifications.csv")
LEGACY_CORRECTIONS = os.path.join(BASE_DIR, "whatsapp_classification_corrections.csv")
LEGACY_LEARNING = os.path.join(BASE_DIR, "whatsapp_classification_learning.json")

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")

VERSION = "v1.01-JUNK-AD-LEARNING"

MEDIA_TYPES = (
    "text",
    "image",
    "voice",
    "video",
    "pdf",
    "office_word",
    "office_excel",
    "office_powerpoint",
    "document",
    "sticker",
    "unknown",
)

INTENT_TYPES = (
    "rfq_inquiry",
    "purchase_order",
    "technical_support",
    "delivery_tracking",
    "payment_invoice",
    "supplier_reply",
    "order_confirmation",
    "complaint",
    "greeting",
    "general_chat",
    "junk_ad",
    "unknown",
)

LOG_FIELDS = [
    "timestamp",
    "contact_name",
    "customer_contact",
    "media_type",
    "media_filename",
    "message_preview",
    "intent",
    "confidence",
    "reasoning",
    "has_image",
    "handler",
    "status",
]

OFFICE_EXTENSIONS = {
    ".doc": "office_word",
    ".docx": "office_word",
    ".xls": "office_excel",
    ".xlsx": "office_excel",
    ".xlsm": "office_excel",
    ".ppt": "office_powerpoint",
    ".pptx": "office_powerpoint",
}


@dataclass
class MediaInfo:
    media_type: str = "text"
    filename: str = ""
    has_image: bool = False
    has_voice: bool = False
    has_video: bool = False
    has_document: bool = False
    caption: str = ""
    raw_indicators: List[str] = field(default_factory=list)


@dataclass
class ClassificationResult:
    media_type: str
    intent: str
    confidence: float
    reasoning: str = ""
    media_filename: str = ""
    handler: str = "monitor_only"
    suggested_reply: str = ""
    media_info: Optional[MediaInfo] = None

    def summary(self) -> str:
        return (
            f"Media: {self.media_type}"
            + (f" ({self.media_filename})" if self.media_filename else "")
            + f"\nIntent: {self.intent} ({self.confidence:.0%} confidence)"
            + (f"\nReason: {self.reasoning}" if self.reasoning else "")
            + f"\nHandler: {self.handler}"
        )


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _guess_media_from_filename(filename: str) -> str:
    name = str(filename or "").strip().lower()
    if not name:
        return "document"
    if name.endswith(".pdf"):
        return "pdf"
    for ext, media in OFFICE_EXTENSIONS.items():
        if name.endswith(ext):
            return media
    if re.search(r"\.(png|jpe?g|gif|webp|bmp)$", name):
        return "image"
    if re.search(r"\.(mp3|ogg|opus|m4a|wav|aac)$", name):
        return "voice"
    if re.search(r"\.(mp4|mov|avi|mkv|webm)$", name):
        return "video"
    return "document"


def _extract_filename_from_bubble(bubble) -> str:
    if bubble is None:
        return ""

    selectors = [
        '[data-testid="document-thumb"] span',
        '[data-icon="document"] + span',
        'span[data-testid="document-caption"]',
        'div[role="button"] span[title]',
        'span[title*="."]',
    ]

    for selector in selectors:
        try:
            for element in bubble.find_elements(By.CSS_SELECTOR, selector):
                for attr in ("title", "aria-label"):
                    value = (element.get_attribute(attr) or "").strip()
                    if value and "." in value:
                        return value
                text = (element.text or "").strip()
                if text and "." in text and len(text) < 180:
                    return text
        except Exception:
            continue

    try:
        bubble_text = (bubble.text or "").strip()
        for line in bubble_text.splitlines():
            line = line.strip()
            if re.search(r"\.[a-z0-9]{2,5}$", line, re.I) and len(line) < 180:
                return line
    except Exception:
        pass

    return ""


def detect_bubble_media(bubble, caption_text: str = "") -> MediaInfo:
    """Inspect a WhatsApp bubble DOM node and infer media type."""
    info = MediaInfo(caption=caption_text or "")

    if bubble is None:
        if caption_text.strip():
            info.media_type = "text"
        else:
            info.media_type = "unknown"
        return info

    voice_selectors = [
        '[data-testid="audio-play"]',
        '[data-testid="ptt-play-button"]',
        '[data-icon="ptt"]',
        '[data-icon="audio-play"]',
        'audio',
    ]
    doc_selectors = [
        '[data-testid="document-thumb"]',
        '[data-icon="document"]',
        '[data-icon="document-pdf"]',
        '[data-icon="document-xls"]',
        '[data-icon="document-ppt"]',
        '[data-icon="document-doc"]',
    ]
    image_selectors = [
        'img[src]',
        '[data-testid="image-thumb"]',
        '[data-testid="image-thumb"] img',
    ]
    video_selectors = ['video[src]', '[data-testid="video-thumb"]']

    for selector in voice_selectors:
        try:
            if bubble.find_elements(By.CSS_SELECTOR, selector):
                info.has_voice = True
                info.raw_indicators.append(f"voice:{selector}")
                break
        except Exception:
            continue

    for selector in doc_selectors:
        try:
            if bubble.find_elements(By.CSS_SELECTOR, selector):
                info.has_document = True
                info.raw_indicators.append(f"document:{selector}")
                break
        except Exception:
            continue

    for selector in video_selectors:
        try:
            elements = bubble.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                info.has_video = True
                info.raw_indicators.append(f"video:{selector}")
                break
        except Exception:
            continue

    for selector in image_selectors:
        try:
            for element in bubble.find_elements(By.CSS_SELECTOR, selector):
                src = (element.get_attribute("src") or "").lower()
                if any(token in src for token in ("emoji", "avatar", "gif", "sticker")):
                    continue
                size = element.size or {}
                area = int(size.get("width") or 0) * int(size.get("height") or 0)
                if area >= 80:
                    info.has_image = True
                    info.raw_indicators.append(f"image:{selector}")
                    break
            if info.has_image:
                break
        except Exception:
            continue

    info.filename = _extract_filename_from_bubble(bubble)

    if info.has_voice:
        info.media_type = "voice"
    elif info.has_document:
        info.media_type = _guess_media_from_filename(info.filename)
    elif info.has_video:
        info.media_type = "video"
    elif info.has_image:
        info.media_type = "image"
    elif caption_text.strip():
        info.media_type = "text"
    else:
        info.media_type = "unknown"

    return info


def _apply_corrections(message_text: str, media_type: str) -> Optional[ClassificationResult]:
    intent = apply_correction(message_text, media_type, "whatsapp", INTENT_TYPES)
    if not intent:
        return None
    return ClassificationResult(
        media_type=media_type,
        intent=intent,
        confidence=0.99,
        reasoning="Matched manual correction rule.",
        handler=_handler_for_intent(intent),
        suggested_reply=_default_reply(intent, media_type),
    )


def _few_shot_examples(limit: int = 8) -> List[Dict[str, str]]:
    return few_shot_examples(channel="whatsapp", limit=limit)


def _heuristic_intent(message_text: str, media_info: MediaInfo) -> Optional[ClassificationResult]:
    text = f"{message_text or ''} {media_info.filename or ''}".upper()

    junk_markers = (
        "UNSUBSCRIBE", "SPECIAL OFFER", "LIMITED TIME", "PROMOTION",
        "APPLY TO BE META VERIFIED", "GET THE VERIFIED BADGE",
        "CLICK HERE TO WIN", "CONGRATULATIONS YOU WON",
    )
    if any(marker in text for marker in junk_markers):
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="junk_ad",
            confidence=0.9,
            reasoning="Promotional or spam-like message markers.",
            media_filename=media_info.filename,
            handler="skip",
        )

    if re.search(r"WA-\d{8}-[A-Z0-9]+-[A-Z0-9]+", text):
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="supplier_reply",
            confidence=0.95,
            reasoning="Contains OpenClaw supplier reference code.",
            media_filename=media_info.filename,
            handler="supplier_reply",
        )

    po_markers = [
        "PURCHASE ORDER", "P/O", "P.O.", " PO ", "PO#", "PO NUMBER",
        "PURCHASE REQUISITION", "PR NO", "PR#",
    ]
    if any(marker in text for marker in po_markers):
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="purchase_order",
            confidence=0.88,
            reasoning="Message mentions purchase order keywords.",
            media_filename=media_info.filename,
            handler="purchase_order",
        )

    support_markers = [
        "NOT WORKING", "FAULTY", "DEFECT", "BROKEN", "TROUBLESHOOT",
        "TECHNICAL SUPPORT", "WARRANTY", "REPAIR", "ERROR CODE", "HOW TO",
        "MANUAL", "WIRING", "CONNECTION", "SPEC", "DATASHEET",
    ]
    if any(marker in text for marker in support_markers):
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="technical_support",
            confidence=0.82,
            reasoning="Message looks like a technical support request.",
            media_filename=media_info.filename,
            handler="technical_support",
        )

    delivery_markers = ["TRACKING", "DELIVERY", "SHIPMENT", "COURIER", "AWB", "DISPATCH", "ETA"]
    if any(marker in text for marker in delivery_markers):
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="delivery_tracking",
            confidence=0.8,
            reasoning="Message asks about delivery or tracking.",
            media_filename=media_info.filename,
            handler="delivery_tracking",
        )

    invoice_markers = ["INVOICE", "PAYMENT", "RECEIPT", "TAX INVOICE", "BANK IN", "REMITTANCE"]
    if any(marker in text for marker in invoice_markers):
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="payment_invoice",
            confidence=0.8,
            reasoning="Message relates to payment or invoicing.",
            media_filename=media_info.filename,
            handler="payment_invoice",
        )

    inquiry_markers = [
        "RFQ", "QUOTE", "QUOTATION", "ENQ", "QTY", "PRICE", "PART NO",
        "MODEL", "OMRON", "SMC", "BURKERT", "FESTO",
    ]
    if any(marker in text for marker in inquiry_markers):
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="rfq_inquiry",
            confidence=0.75,
            reasoning="Message contains RFQ or part inquiry keywords.",
            media_filename=media_info.filename,
            handler="rfq_inquiry",
        )

    greeting_markers = ["HI", "HELLO", "GOOD MORNING", "GOOD AFTERNOON", "THANK YOU", "THANKS"]
    compact = re.sub(r"[^A-Z ]", "", text).strip()
    if compact in greeting_markers or (len(compact) < 20 and any(g in compact for g in greeting_markers)):
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="greeting",
            confidence=0.7,
            reasoning="Short greeting or thank-you message.",
            media_filename=media_info.filename,
            handler="greeting",
        )

    if media_info.media_type in ("pdf", "office_excel", "office_word") and not message_text.strip():
        return ClassificationResult(
            media_type=media_info.media_type,
            intent="purchase_order",
            confidence=0.65,
            reasoning="Document attachment without caption — often a PO or formal order.",
            media_filename=media_info.filename,
            handler="purchase_order",
        )

    if media_info.media_type == "voice":
        if message_text.strip():
            pass  # transcript available — let other rules classify content
        else:
            return ClassificationResult(
                media_type="voice",
                intent="unknown",
                confidence=0.55,
                reasoning="Voice note — transcript not available yet.",
                media_filename=media_info.filename,
                handler="voice_note",
            )

    return None


def _handler_for_intent(intent: str) -> str:
    mapping = {
        "rfq_inquiry": "rfq_inquiry",
        "purchase_order": "purchase_order",
        "technical_support": "technical_support",
        "delivery_tracking": "delivery_tracking",
        "payment_invoice": "payment_invoice",
        "supplier_reply": "supplier_reply",
        "order_confirmation": "order_confirmation",
        "complaint": "complaint",
        "greeting": "greeting",
        "general_chat": "monitor_only",
        "junk_ad": "skip",
        "unknown": "monitor_only",
    }
    return mapping.get(intent, "monitor_only")


def _default_reply(intent: str, media_type: str) -> str:
    if intent == "purchase_order":
        return (
            "Hi, thank you for sending your purchase order.\n\n"
            "Our team is reviewing the document and will confirm shortly."
        )
    if intent == "technical_support":
        return (
            "Hi, thank you for reaching out.\n\n"
            "Our technical team has received your support request and will assist you shortly."
        )
    if intent == "delivery_tracking":
        return (
            "Hi, thank you for your message.\n\n"
            "We are checking the delivery status and will update you shortly."
        )
    if intent == "payment_invoice":
        return (
            "Hi, thank you for your message regarding payment/invoice.\n\n"
            "Our accounts team will review and respond shortly."
        )
    if intent == "greeting":
        return (
            "Hi, thank you for contacting Robomatics.\n\n"
            "How may we assist you today?"
        )
    if media_type == "voice":
        return (
            "Hi, we received your voice message.\n\n"
            "For faster processing, please also send the part numbers or details as text if possible."
        )
    if intent == "general_chat":
        return (
            "Hi, thank you for your message.\n\n"
            "Our team will review and get back to you shortly."
        )
    return ""


def _classify_with_copilot(message_text: str, media_info: MediaInfo) -> Optional[ClassificationResult]:
    if not str(message_text or "").strip() and media_info.media_type in ("text", "unknown"):
        return None

    examples = _few_shot_examples()
    example_lines = []
    for ex in examples:
        example_lines.append(
            f'- media={ex.get("media_type")} text="{ex.get("message_preview", "")[:120]}" '
            f'-> intent={ex.get("intent")}'
        )
    examples_block = "\n".join(example_lines) if example_lines else "(none yet)"

    system_prompt = f"""You classify incoming WhatsApp messages for an industrial automation trading company (Robomatics).

Return STRICT JSON only:
{{"intent": "<one of {list(INTENT_TYPES)}>", "confidence": 0.0-1.0, "reasoning": "short reason"}}

Intent guide:
- rfq_inquiry: customer asking price/availability for parts
- purchase_order: PO, formal order document, PR
- technical_support: product fault, wiring, specs, how-to
- delivery_tracking: shipment, courier, ETA
- payment_invoice: invoice, payment, receipt
- supplier_reply: reply containing WA-YYYYMMDD-BRAND-XXXX ref
- order_confirmation: confirming an existing order
- complaint: dissatisfaction, wrong item, delay complaint
- greeting: hello/thanks only
- junk_ad: ads, promotions, spam, unrelated marketing
- general_chat: other business chat
- unknown: cannot tell

Learned examples:
{examples_block}
"""

    user_prompt = (
        f"Media type: {media_info.media_type}\n"
        f"Filename: {media_info.filename or '(none)'}\n"
        f"Message:\n{message_text or '(empty / attachment only)'}"
    )

    try:
        client = OpenAI(
            base_url=COPILOT_BASE_URL,
            api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
            timeout=25.0,
            max_retries=1,
        )
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1]).strip()
        parsed = json.loads(raw)
        intent = str(parsed.get("intent") or "unknown").strip().lower()
        if intent not in INTENT_TYPES:
            intent = "unknown"
        confidence = float(parsed.get("confidence") or 0.5)
        reasoning = str(parsed.get("reasoning") or "").strip()
        return ClassificationResult(
            media_type=media_info.media_type,
            intent=intent,
            confidence=max(0.0, min(confidence, 1.0)),
            reasoning=reasoning or "Copilot classification.",
            media_filename=media_info.filename,
            handler=_handler_for_intent(intent),
            suggested_reply=_default_reply(intent, media_info.media_type),
            media_info=media_info,
        )
    except Exception as exc:
        print(f"⚠️ [CLASSIFIER] Copilot classification failed: {exc}")
        return None


def classify_whatsapp_message(
    message_text: str,
    media_info: Optional[MediaInfo] = None,
    use_ai: bool = True,
) -> ClassificationResult:
    media_info = media_info or MediaInfo(media_type="text" if message_text.strip() else "unknown")

    corrected = _apply_corrections(message_text, media_info.media_type)
    if corrected:
        corrected.media_info = media_info
        corrected.suggested_reply = corrected.suggested_reply or _default_reply(
            corrected.intent, media_info.media_type
        )
        return corrected

    heuristic = _heuristic_intent(message_text, media_info)
    if heuristic and heuristic.confidence >= 0.85:
        heuristic.media_info = media_info
        heuristic.suggested_reply = heuristic.suggested_reply or _default_reply(
            heuristic.intent, media_info.media_type
        )
        record_classification_example(
            message_text, media_info.media_type, heuristic.intent, heuristic.confidence,
            channel="whatsapp",
        )
        return heuristic

    ai_result = _classify_with_copilot(message_text, media_info) if use_ai else None

    if ai_result and heuristic:
        if ai_result.confidence >= heuristic.confidence:
            final = ai_result
        else:
            final = heuristic
            final.reasoning = f"Heuristic override: {heuristic.reasoning}"
    elif ai_result:
        final = ai_result
    elif heuristic:
        final = heuristic
    else:
        final = ClassificationResult(
            media_type=media_info.media_type,
            intent="unknown",
            confidence=0.4,
            reasoning="No strong signals detected.",
            media_filename=media_info.filename,
            handler="monitor_only",
            suggested_reply=_default_reply("unknown", media_info.media_type),
            media_info=media_info,
        )

    final.media_info = media_info
    final.suggested_reply = final.suggested_reply or _default_reply(final.intent, media_info.media_type)
    record_classification_example(
        message_text, media_info.media_type, final.intent, final.confidence, channel="whatsapp",
    )
    return final


def ensure_classification_log() -> List[str]:
    if not os.path.exists(CLASSIFICATION_LOG):
        with open(CLASSIFICATION_LOG, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
    return LOG_FIELDS


def log_classification(
    contact_name: str,
    customer_contact: str,
    message_text: str,
    result: ClassificationResult,
    status: str = "CLASSIFIED",
) -> None:
    fields = ensure_classification_log()
    preview = re.sub(r"\s+", " ", str(message_text or "")).strip()[:500]

    with open(CLASSIFICATION_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow({
            "timestamp": now_iso(),
            "contact_name": contact_name or "",
            "customer_contact": customer_contact or "",
            "media_type": result.media_type,
            "media_filename": result.media_filename or "",
            "message_preview": preview,
            "intent": result.intent,
            "confidence": f"{result.confidence:.2f}",
            "reasoning": result.reasoning or "",
            "has_image": "yes" if result.media_info and result.media_info.has_image else "no",
            "handler": result.handler,
            "status": status,
        })

    save_last_classification_context(
        contact_name=contact_name,
        customer_contact=customer_contact,
        message_text=message_text,
        media_type=result.media_type,
        intent=result.intent,
    )


def build_classification_monitor_message(
    contact_name: str,
    customer_contact: str,
    message_text: str,
    result: ClassificationResult,
) -> str:
    filename_line = f"\nAttachment: {result.media_filename}" if result.media_filename else ""
    return (
        "[OpenClaw Message Classification]\n"
        f"Contact: {contact_name or '-'}\n"
        f"Customer: {customer_contact or contact_name or '-'}\n"
        f"Media: {result.media_type}{filename_line}\n"
        f"Intent: {result.intent} ({result.confidence:.0%})\n"
        f"Handler: {result.handler}\n"
        f"Reason: {result.reasoning or '-'}\n\n"
        "Incoming Message:\n"
        f"{message_text or '(empty / attachment only)'}\n\n"
        "Suggested Auto-Reply:\n"
        f"{result.suggested_reply or '(none)'}\n\n"
        "Teach the AI (reply in this chat):\n"
        "correct: purchase_order\n"
        "correct: rfq_inquiry | part of message"
    )
