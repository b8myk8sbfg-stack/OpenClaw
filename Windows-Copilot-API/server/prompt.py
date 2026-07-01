"""Convert OpenAI ``messages`` into a Copilot prompt and optional image.

Copilot's protocol has no role/system channel — it takes one prompt string per
turn — so we collapse the whole conversation into one piece of text.
"""

import base64
import binascii
import re
from typing import Any, List, Optional, Tuple, Union

from .schemas import ChatMessage

_DATA_IMAGE_RE = re.compile(
    r"^data:(image/(?:png|jpeg|jpg));base64,(.+)$",
    re.IGNORECASE | re.DOTALL,
)
MAX_IMAGE_BYTES = 20 * 1024 * 1024


def content_text(content: Optional[Union[str, List[Any]]]) -> str:
    """Extract plain text from a message's content (string or content-parts)."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    parts = []
    for part in content:
        if isinstance(part, dict):
            if part.get("type") == "text":
                parts.append(part.get("text", ""))
    return "\n".join(p for p in parts if p)


def _image_bytes(content: Optional[Union[str, List[Any]]]) -> List[bytes]:
    """Decode OpenAI data-URL image parts from one message.

    Remote URLs are deliberately rejected: fetching arbitrary client-provided
    URLs from a local personal server would introduce an SSRF risk. Clients can
    send the same image as a base64 data URL instead.
    """
    if not isinstance(content, list):
        return []

    images = []
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "image_url":
            continue
        image_url = part.get("image_url")
        url = image_url.get("url") if isinstance(image_url, dict) else image_url
        if not isinstance(url, str):
            raise ValueError("image_url.url must be a string")
        match = _DATA_IMAGE_RE.match(url)
        if not match:
            raise ValueError(
                "only base64 PNG/JPEG data URLs are supported for image_url"
            )
        try:
            data = base64.b64decode(match.group(2), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("image_url contains invalid base64 data") from exc
        if not data:
            raise ValueError("image_url contains an empty image")
        if len(data) > MAX_IMAGE_BYTES:
            raise ValueError(f"image exceeds the {MAX_IMAGE_BYTES // (1024 * 1024)} MB limit")
        mime = match.group(1).lower()
        valid_signature = (
            (mime == "image/png" and data.startswith(b"\x89PNG\r\n\x1a\n"))
            or (mime in ("image/jpeg", "image/jpg") and data.startswith(b"\xff\xd8"))
        )
        if not valid_signature:
            raise ValueError("image data does not match its declared PNG/JPEG type")
        images.append(data)
    return images


def messages_to_prompt(messages: List[ChatMessage]) -> str:
    """Flatten an OpenAI ``messages`` array into a single Copilot prompt."""
    system = "\n\n".join(
        content_text(m.content) for m in messages if m.role == "system" and m.content
    )
    convo = [m for m in messages if m.role != "system"]

    if len(convo) == 1 and convo[0].role == "user":
        body = content_text(convo[0].content)  # simple single-turn request
    else:
        lines = []
        for m in convo:
            label = "User" if m.role == "user" else "Assistant"
            lines.append(f"{label}: {content_text(m.content)}")
        lines.append("Assistant:")  # cue Copilot to continue
        body = "\n".join(lines)

    if system and body:
        return f"{system}\n\n{body}"
    return system or body


def messages_to_prompt_and_image(messages: List[ChatMessage]) -> Tuple[str, Optional[bytes]]:
    """Return flattened text plus the single image accepted by Copilot chat."""
    images = []
    for message in messages:
        images.extend(_image_bytes(message.content))
    if len(images) > 1:
        raise ValueError("Copilot currently accepts one input image per request")
    return messages_to_prompt(messages), images[0] if images else None
