"""Local Whisper transcription for the OpenAI-compatible /v1/audio/transcriptions route."""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from typing import Optional, Tuple

WHISPER_MODEL = os.getenv("OPENCLAW_WHISPER_MODEL", os.getenv("COPILOT_WHISPER_MODEL", "base"))
OPENCLAW_DIR = os.getenv("OPENCLAW_DIR", "/Users/evon/OpenClaw")


def guess_audio_suffix(data: bytes, filename: str = "") -> str:
    lower = (filename or "").lower()
    if lower.endswith((".opus", ".ogg", ".wav", ".mp3", ".m4a", ".webm")):
        return os.path.splitext(lower)[1]
    if data.startswith(b"OggS"):
        return ".opus"
    if data.startswith(b"RIFF"):
        return ".wav"
    if data.startswith(b"ID3") or (len(data) > 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0):
        return ".mp3"
    return ".opus"


def convert_audio_to_wav(src_path: str) -> Tuple[str, bool]:
    if src_path.lower().endswith(".wav"):
        return src_path, False
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return src_path, False
    wav_path = f"{src_path}.whisper.wav"
    result = subprocess.run(
        [ffmpeg, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", src_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", wav_path],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.returncode != 0 or not os.path.exists(wav_path):
        raise RuntimeError(f"ffmpeg decode failed: {(result.stderr or '')[-400:]}")
    return wav_path, True


def _whisper_backends_available() -> bool:
    try:
        import whisper  # type: ignore  # noqa: F401
        return True
    except ImportError:
        pass
    if OPENCLAW_DIR and os.path.isdir(OPENCLAW_DIR):
        return True
    return bool(shutil.which("whisper"))


def transcribe_audio_bytes(data: bytes, suffix: str = ".opus", model: Optional[str] = None) -> str:
    if not data or len(data) < 64:
        raise RuntimeError("audio too small or empty")
    model_name = model or WHISPER_MODEL
    if model_name in ("whisper-1", "copilot"):
        model_name = WHISPER_MODEL
    suffix = suffix if suffix.startswith(".") else f".{suffix or guess_audio_suffix(data)}"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    wav_path = tmp_path
    delete_wav = False
    try:
        wav_path, delete_wav = convert_audio_to_wav(tmp_path)
        for fn in (_transcribe_with_whisper_python, _transcribe_with_openclaw_uv, _transcribe_with_whisper_cli):
            text = fn(wav_path, model_name)
            if text:
                return text
        if not _whisper_backends_available():
            raise RuntimeError("whisper not installed")
        return ""
    finally:
        for path, drop in ((tmp_path, True), (wav_path, delete_wav)):
            if drop and path and os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError:
                    pass


def _transcribe_with_openclaw_uv(audio_path: str, model_name: str) -> str:
    if not OPENCLAW_DIR or not os.path.isdir(OPENCLAW_DIR):
        return ""
    script = "import sys, whisper; m=whisper.load_model(%r); print(m.transcribe(sys.argv[1], fp16=False).get('text','').strip())" % model_name
    result = subprocess.run(["uv", "run", "python", "-c", script, audio_path], cwd=OPENCLAW_DIR, capture_output=True, text=True, timeout=180, check=False)
    return (result.stdout or "").strip() if result.returncode == 0 else ""


def _transcribe_with_whisper_python(audio_path: str, model_name: str) -> str:
    try:
        import whisper  # type: ignore
    except ImportError:
        return ""
    model = whisper.load_model(model_name)
    return str(model.transcribe(audio_path, fp16=False).get("text") or "").strip()


def _transcribe_with_whisper_cli(audio_path: str, model_name: str) -> str:
    if not shutil.which("whisper"):
        return ""
    subprocess.run(["whisper", audio_path, "--model", model_name, "--output_format", "txt"], check=True, capture_output=True, text=True, timeout=180)
    txt_path = os.path.splitext(audio_path)[0] + ".txt"
    if os.path.exists(txt_path):
        with open(txt_path, encoding="utf-8") as f:
            return f.read().strip()
    return ""
