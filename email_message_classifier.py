"""
Email message classification for OpenClaw — detects junk/ads/newsletters and
routes business intents before inquiry processing.
"""

from __future__ import annotations

import csv
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from openai import OpenAI

from message_learning_store import (
    apply_correction,
    few_shot_examples,
    record_classification_example,
)

BASE_DIR = "/Users/evon/OpenClaw"
EMAIL_CLASSIFICATION_LOG = os.path.join(BASE_DIR, "email_message_classifications.csv")

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")

VERSION = "v1.01-EMAIL-PO-SKIP-INQUIRY"

INTENT_TYPES = (
    "rfq_inquiry",
    "purchase_order",
    "technical_support",
    "delivery_tracking",
    "payment_invoice",
    "supplier_reply",
    "order_confirmation",
    "complaint",
    "internal",
    "junk_ad",
    "newsletter",
    "general_chat",
    "unknown",
)

# Never run RFQ / OBM quote extraction for these intents.
SKIP_INTENTS = {"junk_ad", "newsletter", "internal"}

# Business mail that is not a price inquiry — classify, log, leave to humans.
NON_INQUIRY_INTENTS = {
    "purchase_order",
    "technical_support",
    "delivery_tracking",
    "payment_invoice",
    "order_confirmation",
    "complaint",
    "general_chat",
    "supplier_reply",
}

LOG_FIELDS = [
    "timestamp",
    "sender",
    "subject",
    "message_preview",
    "intent",
    "confidence",
    "reasoning",
    "handler",
    "status",
]

JUNK_SENDER_PATTERNS = (
    r"noreply@",
    r"no-reply@",
    r"donotreply@",
    r"marketing@",
    r"newsletter@",
    r"promo@",
    r"promotions@",
    r"mailer@",
    r"bounce@",
    r"@linkedin\.com",
    r"@facebookmail\.com",
    r"@mail\.instagram\.com",
    r"@news\.",
    r"@e\.linkedin\.com",
    r"@info\.hubspotemail",
    r"@sendgrid\.net",
    r"@mcsv\.net",
    r"@mailchimp",
)

JUNK_SUBJECT_MARKERS = (
    "UNSUBSCRIBE",
    "NEWSLETTER",
    "PROMOTION",
    "PROMO ",
    "LIMITED TIME",
    "SPECIAL OFFER",
    "WEBINAR",
    "FREE TRIAL",
    "ACT NOW",
    "DON'T MISS",
    "SALE ENDS",
    "BLACK FRIDAY",
    "CYBER MONDAY",
    "YOUR WEEKLY",
    "DAILY DIGEST",
    "JOB ALERT",
    "INVITED YOU TO",
    "CONNECTION REQUEST",
    "META VERIFIED",
    "GOOGLE ADS",
    "ADWORDS",
    "MAILCHIMP",
    "HUBSPOT",
)

JUNK_BODY_MARKERS = (
    "UNSUBSCRIBE",
    "CLICK HERE TO UNSUBSCRIBE",
    "MANAGE YOUR PREFERENCES",
    "EMAIL PREFERENCES",
    "VIEW IN BROWSER",
    "YOU ARE RECEIVING THIS EMAIL BECAUSE",
    "TO STOP RECEIVING",
    "OPT OUT",
    "THIS IS A PROMOTIONAL",
    "ADVERTISEMENT",
    "SPONSORED BY",
    "ADD TO SAFE SENDERS",
)


@dataclass
class EmailClassificationResult:
    intent: str
    confidence: float
    reasoning: str = ""
    handler: str = "process"
    should_skip: bool = False

    def summary(self) -> str:
        return (
            f"Intent: {self.intent} ({self.confidence:.0%})\n"
            f"Handler: {self.handler}\n"
            f"Reason: {self.reasoning or '-'}"
        )


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _handler_for_intent(intent: str) -> str:
    if intent in SKIP_INTENTS:
        return "skip"
    if intent == "supplier_reply":
        return "supplier_reply"
    if intent == "purchase_order":
        return "purchase_order"
    if intent == "rfq_inquiry":
        return "rfq_inquiry"
    if intent in NON_INQUIRY_INTENTS:
        return intent
    return "process"


def is_email_rfq_inquiry(result: EmailClassificationResult) -> bool:
    """True only when the mailbox loop should run quote extraction."""
    intent = str(getattr(result, "intent", "") or "").strip().lower()
    handler = str(getattr(result, "handler", "") or "").strip().lower()
    if getattr(result, "should_skip", False):
        return False
    if intent in SKIP_INTENTS or intent in NON_INQUIRY_INTENTS:
        return False
    return intent == "rfq_inquiry" or handler in ("rfq_inquiry", "process")


def _looks_like_purchase_order(subject: str, body: str) -> bool:
    subject_u = str(subject or "").upper()
    body_u = str(body or "").upper()
    combined = f"{subject_u}\n{body_u}"

    if re.search(
        r"\b(?:AMENDED\s+)?P\.?\s*O\.?\b|\bPURCHASE\s+ORDER\b|\bPURCHASE\s+REQUISITION\b",
        combined,
        re.I,
    ):
        return True

    po_markers = (
        "PURCHASE ORDER",
        "PURCHASE REQUISITION",
        "AMENDED PO",
        "P/O",
        "P.O.",
        " PO ",
        "PO#",
        "PO:",
        "PO NO",
        "PO NUMBER",
        "PO TO:",
        "ACKNOWLEDGE RECEIVING OF THIS PO",
        "STATUS OF THIS PO",
        "DELIVERY OF GOODS ABOVE",
    )
    return any(m in combined for m in po_markers)


def _heuristic_classify(sender: str, subject: str, body: str) -> Optional[EmailClassificationResult]:
    sender_l = str(sender or "").lower()
    subject_u = str(subject or "").upper()
    body_u = str(body or "").upper()
    combined = f"{subject_u} {body_u}"

    for pattern in JUNK_SENDER_PATTERNS:
        if re.search(pattern, sender_l, re.I):
            return EmailClassificationResult(
                intent="junk_ad",
                confidence=0.92,
                reasoning=f"Sender matches junk pattern: {pattern}",
                handler="skip",
                should_skip=True,
            )

    subject_hits = sum(1 for m in JUNK_SUBJECT_MARKERS if m in subject_u)
    body_hits = sum(1 for m in JUNK_BODY_MARKERS if m in body_u)
    if subject_hits >= 1 and body_hits >= 1:
        return EmailClassificationResult(
            intent="junk_ad",
            confidence=0.9,
            reasoning="Promotional subject + unsubscribe/marketing body markers.",
            handler="skip",
            should_skip=True,
        )
    if body_hits >= 2:
        return EmailClassificationResult(
            intent="newsletter",
            confidence=0.88,
            reasoning="Multiple newsletter/marketing body markers.",
            handler="skip",
            should_skip=True,
        )

    if re.search(r"REQ-\d{4}-[A-Z0-9]+", combined, re.I):
        return EmailClassificationResult(
            intent="supplier_reply",
            confidence=0.95,
            reasoning="Contains OpenClaw REQ reference.",
            handler="supplier_reply",
        )

    if "ROBOMATICS.SG" in sender_l:
        if any(k in combined for k in ("RFQ", "QUOTE", "QUOTATION", "ENQ", "QTY", "PART NO")):
            return EmailClassificationResult(
                intent="rfq_inquiry",
                confidence=0.8,
                reasoning="Internal inquiry-like email.",
                handler="rfq_inquiry",
            )
        return EmailClassificationResult(
            intent="internal",
            confidence=0.85,
            reasoning="Internal Robomatics email without inquiry keywords.",
            handler="skip",
            should_skip=True,
        )

    if _looks_like_purchase_order(subject_u, body_u):
        return EmailClassificationResult(
            intent="purchase_order",
            confidence=0.94,
            reasoning="Purchase order / amended PO keywords detected — not an RFQ inquiry.",
            handler="purchase_order",
            should_skip=False,
        )

    support_markers = (
        "NOT WORKING", "FAULTY", "DEFECT", "BROKEN", "TROUBLESHOOT",
        "TECHNICAL SUPPORT", "WARRANTY", "REPAIR", "ERROR CODE",
    )
    if any(m in combined for m in support_markers):
        return EmailClassificationResult(
            intent="technical_support",
            confidence=0.82,
            reasoning="Technical support keywords detected.",
            handler="technical_support",
        )

    inquiry_markers = (
        "RFQ", "QUOTE", "QUOTATION", "ENQ", "PLEASE QUOTE", "KINDLY QUOTE",
        "REQUEST FOR QUOTE", "PRICE", "PART NO", "QTY",
    )
    if any(m in combined for m in inquiry_markers):
        return EmailClassificationResult(
            intent="rfq_inquiry",
            confidence=0.78,
            reasoning="RFQ/inquiry keywords detected.",
            handler="rfq_inquiry",
        )

    return None


def _classify_with_copilot(sender: str, subject: str, body: str) -> Optional[EmailClassificationResult]:
    examples = few_shot_examples(channel="email", limit=8)
    example_lines = [
        f'- subject="{ex.get("message_preview", "")[:80]}" -> intent={ex.get("intent")}'
        for ex in examples
    ]
    examples_block = "\n".join(example_lines) if example_lines else "(none yet)"

    system_prompt = f"""You classify incoming business emails for Robomatics (industrial automation trading).

Return STRICT JSON only:
{{"intent": "<one of {list(INTENT_TYPES)}>", "confidence": 0.0-1.0, "reasoning": "short reason"}}

Important:
- junk_ad: marketing, ads, cold sales, unrelated promotions
- newsletter: subscribed newsletters, digests, webinar invites
- rfq_inquiry: customer asking quote/price/availability ONLY
- purchase_order: PO documents, amended PO, formal orders, acknowledge PO status — NEVER rfq_inquiry
- supplier_reply: replies with REQ- reference codes
- internal: company internal non-inquiry mail
- Subjects like "AMENDED PO: M177514" or body "AMENDED PO TO:" are purchase_order, not inquiries.

Learned examples:
{examples_block}
"""

    user_prompt = (
        f"From: {sender}\n"
        f"Subject: {subject}\n\n"
        f"Body preview:\n{(body or '')[:3000]}"
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
        handler = _handler_for_intent(intent)
        return EmailClassificationResult(
            intent=intent,
            confidence=max(0.0, min(confidence, 1.0)),
            reasoning=reasoning or "Copilot email classification.",
            handler=handler,
            should_skip=intent in SKIP_INTENTS or handler == "skip",
        )
    except Exception as exc:
        print(f"⚠️ [EMAIL-CLASSIFIER] Copilot failed: {exc}")
        return None


def classify_email(sender: str, subject: str, body: str, use_ai: bool = True) -> EmailClassificationResult:
    preview = re.sub(r"\s+", " ", f"{subject} {body}"[:500]).strip()

    corrected = apply_correction(preview, "text", "email", INTENT_TYPES)
    if corrected:
        handler = _handler_for_intent(corrected)
        return EmailClassificationResult(
            intent=corrected,
            confidence=0.99,
            reasoning="Matched manual correction rule.",
            handler=handler,
            should_skip=corrected in SKIP_INTENTS or handler == "skip",
        )

    heuristic = _heuristic_classify(sender, subject, body)
    # Strong PO / junk heuristics win without waiting for Copilot.
    if heuristic and heuristic.confidence >= 0.88:
        record_classification_example(
            preview, "text", heuristic.intent, heuristic.confidence, channel="email"
        )
        return heuristic

    ai_result = _classify_with_copilot(sender, subject, body) if use_ai else None
    if ai_result and heuristic:
        final = ai_result if ai_result.confidence >= heuristic.confidence else heuristic
    elif ai_result:
        final = ai_result
    elif heuristic:
        final = heuristic
    else:
        final = EmailClassificationResult(
            intent="unknown",
            confidence=0.4,
            reasoning="No strong email signals.",
            handler="process",
        )

    # Never let AI remount a clear purchase_order as rfq_inquiry.
    if heuristic and heuristic.intent == "purchase_order" and final.intent == "rfq_inquiry":
        final = heuristic

    final.handler = _handler_for_intent(final.intent)
    final.should_skip = final.intent in SKIP_INTENTS or final.handler == "skip"

    record_classification_example(preview, "text", final.intent, final.confidence, channel="email")
    return final


def ensure_email_classification_log() -> List[str]:
    if not os.path.exists(EMAIL_CLASSIFICATION_LOG):
        with open(EMAIL_CLASSIFICATION_LOG, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
            writer.writeheader()
    return LOG_FIELDS


def log_email_classification(
    sender: str,
    subject: str,
    body: str,
    result: EmailClassificationResult,
    status: str = "CLASSIFIED",
) -> None:
    fields = ensure_email_classification_log()
    preview = re.sub(r"\s+", " ", str(body or "")).strip()[:500]
    with open(EMAIL_CLASSIFICATION_LOG, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writerow({
            "timestamp": now_iso(),
            "sender": sender or "",
            "subject": subject or "",
            "message_preview": preview,
            "intent": result.intent,
            "confidence": f"{result.confidence:.2f}",
            "reasoning": result.reasoning or "",
            "handler": result.handler,
            "status": status,
        })
