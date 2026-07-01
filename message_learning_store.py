"""
Shared incremental learning store for WhatsApp and email classifiers.

Supports manual corrections CSV, few-shot examples JSON, last-classification
context for WhatsApp feedback commands, and confirmed training examples.
"""

from __future__ import annotations

import csv
import json
import os
import re
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = "/Users/evon/OpenClaw"
LEARNING_STORE = os.path.join(BASE_DIR, "message_classification_learning.json")
CORRECTIONS_CSV = os.path.join(BASE_DIR, "message_classification_corrections.csv")
LAST_CONTEXT_FILE = os.path.join(BASE_DIR, "whatsapp_last_classification_context.json")

FEEDBACK_PATTERN = re.compile(
    r"^correct\s*:?\s*([a-z_]+)(?:\s*\|\s*(.+))?$",
    re.I,
)


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def load_learning_store() -> Dict[str, Any]:
    if not os.path.exists(LEARNING_STORE):
        return {"examples": [], "stats": {}}
    try:
        with open(LEARNING_STORE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            data.setdefault("examples", [])
            data.setdefault("stats", {})
            return data
    except Exception:
        pass
    return {"examples": [], "stats": {}}


def save_learning_store(data: Dict[str, Any]) -> None:
    os.makedirs(BASE_DIR, exist_ok=True)
    with open(LEARNING_STORE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def load_corrections(channel: str = "") -> List[Dict[str, str]]:
    if not os.path.exists(CORRECTIONS_CSV):
        return []
    rows = []
    with open(CORRECTIONS_CSV, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if not row.get("correct_intent"):
                continue
            row_channel = str(row.get("channel") or "all").strip().lower()
            if channel and row_channel not in ("all", channel):
                continue
            rows.append(row)
    return rows


def ensure_corrections_csv() -> None:
    if os.path.exists(CORRECTIONS_CSV):
        return
    with open(CORRECTIONS_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["match_text", "media_type", "correct_intent", "channel", "notes"],
        )
        writer.writeheader()


def add_correction(
    match_text: str,
    correct_intent: str,
    media_type: str = "",
    channel: str = "all",
    notes: str = "",
) -> None:
    ensure_corrections_csv()
    with open(CORRECTIONS_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["match_text", "media_type", "correct_intent", "channel", "notes"],
        )
        writer.writerow({
            "match_text": match_text or "",
            "media_type": media_type or "",
            "correct_intent": correct_intent,
            "channel": channel or "all",
            "notes": notes or "",
        })


def apply_correction(
    message_text: str,
    media_type: str,
    channel: str,
    valid_intents: tuple,
) -> Optional[str]:
    text_norm = re.sub(r"\s+", " ", str(message_text or "")).strip().lower()
    for row in load_corrections(channel):
        pattern = str(row.get("match_text") or "").strip().lower()
        if not pattern or pattern not in text_norm:
            continue
        expected_media = str(row.get("media_type") or "").strip().lower()
        if expected_media and expected_media != media_type:
            continue
        intent = str(row.get("correct_intent") or "").strip().lower()
        if intent in valid_intents:
            return intent
    return None


def few_shot_examples(channel: str = "", limit: int = 8) -> List[Dict[str, str]]:
    store = load_learning_store()
    examples = list(store.get("examples") or [])
    if channel:
        examples = [
            x for x in examples
            if str(x.get("channel") or "whatsapp") in (channel, "all")
        ]
    confirmed = [x for x in examples if x.get("confirmed")]
    pool = confirmed or examples
    pool.sort(key=lambda x: x.get("timestamp", ""), reverse=True)
    return pool[:limit]


def record_classification_example(
    message_text: str,
    media_type: str,
    intent: str,
    confidence: float,
    channel: str = "whatsapp",
    confirmed: bool = False,
) -> None:
    store = load_learning_store()
    examples = store.setdefault("examples", [])
    preview = re.sub(r"\s+", " ", str(message_text or "")).strip()[:240]
    examples.append({
        "timestamp": now_iso(),
        "channel": channel,
        "media_type": media_type,
        "intent": intent,
        "confidence": confidence,
        "message_preview": preview,
        "confirmed": confirmed,
    })
    store["examples"] = examples[-800:]
    stats = store.setdefault("stats", {})
    stats[intent] = int(stats.get(intent, 0)) + 1
    save_learning_store(store)


def save_last_classification_context(
    contact_name: str,
    customer_contact: str,
    message_text: str,
    media_type: str,
    intent: str,
) -> None:
    preview = re.sub(r"\s+", " ", str(message_text or "")).strip()[:500]
    payload = {
        "timestamp": now_iso(),
        "contact_name": contact_name or "",
        "customer_contact": customer_contact or "",
        "message_preview": preview,
        "media_type": media_type or "text",
        "intent": intent or "unknown",
    }
    with open(LAST_CONTEXT_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def load_last_classification_context() -> Dict[str, str]:
    if not os.path.exists(LAST_CONTEXT_FILE):
        return {}
    try:
        with open(LAST_CONTEXT_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def parse_feedback_command(text: str) -> Optional[Tuple[str, str]]:
    """
    Parse WhatsApp feedback like:
      correct: purchase_order
      correct: rfq_inquiry | E3Z-T61 Qty:1
    Returns (intent, match_text) or None.
    """
    line = str(text or "").strip().splitlines()[0].strip()
    match = FEEDBACK_PATTERN.match(line)
    if not match:
        return None
    intent = match.group(1).strip().lower()
    match_text = (match.group(2) or "").strip()
    if not match_text or match_text.lower() == "last":
        ctx = load_last_classification_context()
        match_text = ctx.get("message_preview") or ""
    return intent, match_text


def apply_feedback_command(text: str, valid_intents: tuple) -> Optional[Dict[str, str]]:
    parsed = parse_feedback_command(text)
    if not parsed:
        return None

    intent, match_text = parsed
    if intent not in valid_intents:
        return None

    ctx = load_last_classification_context()
    media_type = ctx.get("media_type") or ""

    add_correction(
        match_text=match_text,
        correct_intent=intent,
        media_type=media_type,
        channel="whatsapp",
        notes=f"WhatsApp feedback {now_iso()}",
    )
    record_classification_example(
        message_text=match_text,
        media_type=media_type or "text",
        intent=intent,
        confidence=1.0,
        channel="whatsapp",
        confirmed=True,
    )
    return {
        "intent": intent,
        "match_text": match_text,
        "media_type": media_type,
        "previous_intent": ctx.get("intent") or "",
        "contact_name": ctx.get("contact_name") or "",
    }
