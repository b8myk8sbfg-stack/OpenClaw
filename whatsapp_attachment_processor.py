"""
Download and extract content from WhatsApp voice notes and document attachments.
"""

from __future__ import annotations

import base64
import datetime
import json
import os
import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from selenium.webdriver.common.by import By
from openai import OpenAI

BASE_DIR = "/Users/evon/OpenClaw"
VOICE_CAPTURE_DIR = os.path.join(BASE_DIR, "logs/wa_voice_capture")
DOC_CAPTURE_DIR = os.path.join(BASE_DIR, "logs/wa_document_capture")

COPILOT_BASE_URL = os.getenv("COPILOT_BASE_URL", "http://127.0.0.1:8000/v1")
COPILOT_MODEL = os.getenv("COPILOT_MODEL", "copilot")
WHISPER_MODEL = os.getenv("OPENCLAW_WHISPER_MODEL", "base")

VERSION = "v1.00-WA-ATTACHMENT-PROCESSOR"

BLOB_TO_BASE64_JS = """
const el = arguments[0];
const src = el.currentSrc || el.src || '';
if (!src) return null;
if (src.startsWith('data:')) return src.split(',')[1];
const response = await fetch(src);
const blob = await response.blob();
return await new Promise((resolve, reject) => {
  const reader = new FileReader();
  reader.onloadend = () => resolve(reader.result.split(',')[1]);
  reader.onerror = reject;
  reader.readAsDataURL(blob);
});
"""


def _safe_contact(contact_name: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", str(contact_name or "contact"))[:60]


def _timestamp_slug() -> str:
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def _find_voice_element(bubble):
    if bubble is None:
        return None
    selectors = [
        "audio",
        '[data-testid="audio-play"]',
        '[data-testid="ptt-play-button"]',
        '[data-icon="audio-play"]',
        '[data-icon="ptt"]',
    ]
    for selector in selectors:
        try:
            elements = bubble.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                for element in elements:
                    if element.tag_name.lower() == "audio":
                        return element
                    parent = element
                    for _ in range(4):
                        try:
                            audio = parent.find_element(By.CSS_SELECTOR, "audio")
                            if audio:
                                return audio
                        except Exception:
                            pass
                        try:
                            parent = parent.find_element(By.XPATH, "..")
                        except Exception:
                            break
                return elements[0]
        except Exception:
            continue
    return None


def _find_document_element(bubble):
    if bubble is None:
        return None
    selectors = [
        '[data-testid="document-thumb"]',
        '[data-icon="document"]',
        '[data-icon="document-pdf"]',
        '[data-icon="document-xls"]',
        '[data-icon="document-doc"]',
        '[data-icon="document-ppt"]',
    ]
    for selector in selectors:
        try:
            elements = bubble.find_elements(By.CSS_SELECTOR, selector)
            if elements:
                return elements[0]
        except Exception:
            continue
    return None


def download_voice_from_bubble(driver, bubble, contact_name: str) -> Optional[str]:
    element = _find_voice_element(bubble)
    if element is None:
        return None

    os.makedirs(VOICE_CAPTURE_DIR, exist_ok=True)
    out_path = os.path.join(
        VOICE_CAPTURE_DIR,
        f"{_timestamp_slug()}_{_safe_contact(contact_name)}.ogg",
    )

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            element,
        )
        time.sleep(0.5)
        b64 = driver.execute_async_script(BLOB_TO_BASE64_JS, element)
        if not b64:
            audio_el = element if element.tag_name.lower() == "audio" else None
            if audio_el is None:
                try:
                    audio_el = element.find_element(By.CSS_SELECTOR, "audio")
                except Exception:
                    audio_el = None
            if audio_el:
                b64 = driver.execute_async_script(BLOB_TO_BASE64_JS, audio_el)

        if not b64:
            print("⚠️ [VOICE] Could not read voice note blob URL.")
            return None

        with open(out_path, "wb") as f:
            f.write(base64.b64decode(b64))
        print(f"🎤 [VOICE] Saved voice note: {out_path}")
        return out_path
    except Exception as exc:
        print(f"❌ [VOICE] Download failed: {exc}")
        return None


def transcribe_audio(audio_path: str) -> str:
    if not audio_path or not os.path.exists(audio_path):
        return ""

    try:
        import whisper  # type: ignore

        print(f"🎤 [VOICE] Transcribing with Whisper model '{WHISPER_MODEL}'...")
        model = whisper.load_model(WHISPER_MODEL)
        result = model.transcribe(audio_path, fp16=False)
        text = str(result.get("text") or "").strip()
        if text:
            print(f"🎤 [VOICE] Transcript: {text[:200]}")
        return text
    except ImportError:
        pass
    except Exception as exc:
        print(f"⚠️ [VOICE] Whisper transcription failed: {exc}")

    ffmpeg = subprocess.run(
        ["which", "ffmpeg"],
        capture_output=True,
        text=True,
    )
    if ffmpeg.returncode != 0:
        print("⚠️ [VOICE] ffmpeg not found. Install ffmpeg + openai-whisper for transcription.")
        return ""

    try:
        result = subprocess.run(
            ["whisper", audio_path, "--model", WHISPER_MODEL, "--output_format", "txt"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        txt_path = re.sub(r"\.[^.]+$", ".txt", audio_path)
        if os.path.exists(txt_path):
            with open(txt_path, "r", encoding="utf-8") as f:
                return f.read().strip()
        if result.stdout.strip():
            return result.stdout.strip()
    except FileNotFoundError:
        print("⚠️ [VOICE] whisper CLI not found. pip install openai-whisper")
    except Exception as exc:
        print(f"⚠️ [VOICE] whisper CLI failed: {exc}")

    return ""


def download_document_from_bubble(
    driver,
    bubble,
    contact_name: str,
    filename: str = "",
) -> Optional[str]:
    element = _find_document_element(bubble)
    if element is None:
        return None

    os.makedirs(DOC_CAPTURE_DIR, exist_ok=True)
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", filename or "document")[:80]
    if not safe_name or safe_name == "document":
        safe_name = "document.bin"
    out_path = os.path.join(
        DOC_CAPTURE_DIR,
        f"{_timestamp_slug()}_{_safe_contact(contact_name)}_{safe_name}",
    )

    try:
        driver.execute_script(
            "arguments[0].scrollIntoView({block: 'center', inline: 'center'});",
            element,
        )
        time.sleep(0.5)
        element.click()
        time.sleep(2)

        download_selectors = [
            '[data-testid="download"]',
            '[data-icon="download"]',
            'span[data-icon="download"]',
            '[aria-label="Download"]',
            '[title="Download"]',
        ]
        for selector in download_selectors:
            try:
                buttons = driver.find_elements(By.CSS_SELECTOR, selector)
                for btn in buttons:
                    if btn.is_displayed():
                        btn.click()
                        time.sleep(3)
                        break
            except Exception:
                continue

        downloaded = _pick_newest_download(safe_name)
        if downloaded:
            os.replace(downloaded, out_path)
            print(f"📄 [DOC] Saved document: {out_path}")
            return out_path

        b64 = driver.execute_async_script(BLOB_TO_BASE64_JS, element)
        if b64:
            with open(out_path, "wb") as f:
                f.write(base64.b64decode(b64))
            print(f"📄 [DOC] Saved document via blob: {out_path}")
            return out_path

        print("⚠️ [DOC] Could not download document attachment.")
        return None
    except Exception as exc:
        print(f"❌ [DOC] Document download failed: {exc}")
        return None
    finally:
        try:
            close_buttons = driver.find_elements(By.CSS_SELECTOR, '[data-testid="x"]')
            for btn in close_buttons:
                if btn.is_displayed():
                    btn.click()
                    break
        except Exception:
            pass


def _pick_newest_download(preferred_name: str) -> Optional[str]:
    download_dirs = [
        os.path.expanduser("~/Downloads"),
        os.path.join(BASE_DIR, "logs/wa_document_capture"),
    ]
    candidates = []
    for directory in download_dirs:
        if not os.path.isdir(directory):
            continue
        for name in os.listdir(directory):
            path = os.path.join(directory, name)
            if not os.path.isfile(path):
                continue
            if preferred_name and preferred_name.lower() in name.lower():
                candidates.append(path)
            elif name.lower().endswith((
                ".pdf", ".xlsx", ".xls", ".docx", ".doc", ".pptx", ".ppt", ".csv"
            )):
                candidates.append(path)
    if not candidates:
        return None
    candidates.sort(key=lambda p: os.path.getmtime(p), reverse=True)
    newest = candidates[0]
    if time.time() - os.path.getmtime(newest) > 120:
        return None
    return newest


def extract_text_from_document(file_path: str) -> str:
    if not file_path or not os.path.exists(file_path):
        return ""

    lower = file_path.lower()
    try:
        if lower.endswith(".pdf"):
            from pypdf import PdfReader

            reader = PdfReader(file_path)
            pages = []
            for page in reader.pages[:20]:
                pages.append(page.extract_text() or "")
            return "\n".join(pages).strip()

        if lower.endswith((".xlsx", ".xls", ".xlsm")):
            import pandas as pd

            frames = pd.read_excel(file_path, sheet_name=None, header=None)
            chunks = []
            for _name, df in frames.items():
                chunks.append(df.fillna("").astype(str).to_string(index=False, header=False))
            return "\n\n".join(chunks).strip()

        if lower.endswith((".docx",)):
            from docx import Document

            doc = Document(file_path)
            return "\n".join(p.text for p in doc.paragraphs if p.text.strip()).strip()

        if lower.endswith((".csv",)):
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                return f.read(50000).strip()

        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            return f.read(10000).strip()
    except Exception as exc:
        print(f"⚠️ [DOC] Text extraction failed for {file_path}: {exc}")
        return ""


def extract_items_from_document_text(document_text: str, file_path: str = "") -> List[Dict[str, Any]]:
    text = str(document_text or "").strip()
    if not text:
        return []

    client = OpenAI(
        base_url=COPILOT_BASE_URL,
        api_key=os.getenv("COPILOT_API_KEY", "local-copilot-proxy"),
        timeout=45.0,
        max_retries=1,
    )
    prompt = (
        "Extract industrial parts / PO line items from this document text.\n"
        "Return STRICT JSON array: "
        '[{"part_no":"...", "qty":1, "brand":"UNKNOWN"}]\n'
        "Use positive integer qty. Do not invent part numbers.\n\n"
        f"Filename: {os.path.basename(file_path or '')}\n"
        f"Document text:\n{text[:12000]}"
    )
    try:
        response = client.chat.completions.create(
            model=COPILOT_MODEL,
            messages=[
                {"role": "system", "content": "You extract structured PO/RFQ line items."},
                {"role": "user", "content": prompt},
            ],
        )
        raw = (response.choices[0].message.content or "").strip()
        if raw.startswith("```"):
            raw = "\n".join(raw.splitlines()[1:-1]).strip()
        parsed = json.loads(raw)
        if not isinstance(parsed, list):
            return []
        items = []
        for row in parsed:
            if not isinstance(row, dict):
                continue
            part_no = str(row.get("part_no") or "").strip().upper()
            try:
                qty = int(row.get("qty") or 1)
            except (TypeError, ValueError):
                qty = 1
            if part_no and qty > 0:
                items.append({
                    "part_no": part_no,
                    "qty": qty,
                    "brand": str(row.get("brand") or "UNKNOWN").strip().upper(),
                })
        return items
    except Exception as exc:
        print(f"⚠️ [DOC] Copilot line-item extraction failed: {exc}")
        return []


def enrich_message_from_attachments(
    driver,
    bubble,
    contact_name: str,
    latest_message: str,
    media_info,
) -> Dict[str, Any]:
    """
    Return enriched text plus optional extracted document items and local file paths.
    """
    result = {
        "text": latest_message or "",
        "voice_path": "",
        "document_path": "",
        "document_text": "",
        "document_items": [],
        "transcript": "",
    }

    media_type = getattr(media_info, "media_type", "text")

    if media_type == "voice" or getattr(media_info, "has_voice", False):
        voice_path = download_voice_from_bubble(driver, bubble, contact_name)
        if voice_path:
            result["voice_path"] = voice_path
            transcript = transcribe_audio(voice_path)
            result["transcript"] = transcript
            if transcript:
                prefix = f"[Voice transcript]\n{transcript}"
                result["text"] = f"{prefix}\n\n{latest_message}".strip() if latest_message else prefix

    doc_types = ("pdf", "office_word", "office_excel", "office_powerpoint", "document")
    if media_type in doc_types or getattr(media_info, "has_document", False):
        doc_path = download_document_from_bubble(
            driver, bubble, contact_name, filename=getattr(media_info, "filename", "")
        )
        if doc_path:
            result["document_path"] = doc_path
            doc_text = extract_text_from_document(doc_path)
            result["document_text"] = doc_text
            if doc_text:
                prefix = f"[Document: {os.path.basename(doc_path)}]\n{doc_text[:4000]}"
                result["text"] = f"{prefix}\n\n{latest_message}".strip() if latest_message else prefix
            result["document_items"] = extract_items_from_document_text(doc_text, doc_path)

    return result
