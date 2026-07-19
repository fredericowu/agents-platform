"""Text-to-speech and speech-to-text — mirrors the AW workspace's
``src/api/tts.py`` / ``src/api/stt.py`` provider logic, adapted to
agents-platform's ``Setting`` key/value store (see ``security.py``)
instead of ``aw.json``.

STT provider (``stt_provider`` setting):
  ``"openai"`` (default) — OpenAI Whisper API (cloud).
  ``"local"``            — local faster-whisper model (offline, tiny).

TTS provider (``tts_provider`` setting):
  ``"openai"`` (default) — OpenAI audio.speech endpoint (auto multilingual).
  ``"edge"``             — Microsoft Edge TTS, per-language voice map.

Both TTS paths return OGG/Opus bytes so Telegram's ``sendVoice`` renders
a real waveform bubble.
"""

from __future__ import annotations

import asyncio
import io
import logging
import os
import re
import threading
import time
from typing import Optional

import httpx

from . import security

log = logging.getLogger("ap.voice")

WHISPER_URL = "https://api.openai.com/v1/audio/transcriptions"
WHISPER_MODEL = "whisper-1"
TTS_URL = "https://api.openai.com/v1/audio/speech"

SUPPORTED_VOICES = ("alloy", "echo", "fable", "onyx", "nova", "shimmer")
DEFAULT_VOICE = "alloy"
DEFAULT_MODEL = "tts-1"
DEFAULT_EDGE_VOICE = "pt-BR-AntonioNeural"

_EDGE_VOICE_FALLBACKS: dict[str, str] = {
    "pt": "pt-BR-AntonioNeural",
    "en": "en-US-AndrewMultilingualNeural",
    "es": "es-MX-JorgeNeural",
    "it": "it-IT-DiegoNeural",
    "fr": "fr-FR-HenriNeural",
    "de": "de-DE-ConradNeural",
}

_LANGUAGE_NAME_TO_ISO = {
    "arabic": "ar", "chinese": "zh", "dutch": "nl", "english": "en",
    "french": "fr", "german": "de", "hindi": "hi", "italian": "it",
    "japanese": "ja", "korean": "ko", "polish": "pl", "portuguese": "pt",
    "russian": "ru", "spanish": "es", "turkish": "tr", "ukrainian": "uk",
}


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------

def _read_aw_json_openai_key() -> str:
    """Fallback: read ``workspace_agent.openai_api_key`` from the AW ``aw.json``.

    The Telegram/voice stack was migrated out of awserv into agents-platform,
    but the OpenAI key still lives in awserv's ``aw.json``. Rather than force a
    manual re-entry, read it directly as a last-resort source. Path can be
    overridden with ``AW_CONFIG_PATH``.
    """
    path = os.environ.get("AW_CONFIG_PATH") or "/opt/agentic-workspace/src/config/aw.json"
    try:
        import json
        with open(path, "r", encoding="utf-8") as fh:
            cfg = json.load(fh)
        return str((cfg.get("workspace_agent") or {}).get("openai_api_key", "") or "").strip()
    except Exception as exc:  # noqa: BLE001 — best-effort fallback
        log.debug("could not read openai_api_key from aw.json (%s): %s", path, exc)
        return ""


def get_openai_api_key() -> str:
    """Look up the OpenAI API key.

    Resolution order: ``Setting`` override → ``OPENAI_API_KEY`` env →
    awserv ``aw.json`` (``workspace_agent.openai_api_key``).
    """
    key = str(security.get_setting("openai_api_key", "") or "").strip()
    if key:
        return key
    env_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if env_key:
        return env_key
    return _read_aw_json_openai_key()


def get_stt_provider() -> str:
    """Canonical values: ``"openai"`` or ``"local"``.

    Legacy aliases ``"edge"`` / ``"faster-whisper"`` normalise to ``"local"``.
    """
    provider = str(security.get_setting("stt_provider", "openai") or "").strip().lower()
    if provider in ("edge", "faster-whisper", "local"):
        return "local"
    return "openai"


def _resolve_tts_config() -> tuple[str, str, str, dict]:
    """Return ``(provider, openai_voice, edge_voice, edge_voices_map)``."""
    provider = str(security.get_setting("tts_provider", "openai") or "openai")
    oi_voice = str(security.get_setting("tts_voice", DEFAULT_VOICE) or DEFAULT_VOICE)
    e_voice = str(security.get_setting("edge_voice", DEFAULT_EDGE_VOICE) or DEFAULT_EDGE_VOICE)
    e_voices = security.get_setting("edge_voices", {}) or {}
    if not isinstance(e_voices, dict):
        e_voices = {}
    return provider, oi_voice, e_voice, e_voices


def _pick_edge_voice(language: str, map_: dict, singular_default: str) -> str:
    lang = (language or "").split("-")[0].lower()
    if lang and map_.get(lang):
        return map_[lang]
    if map_.get("_default"):
        return map_["_default"]
    if lang and lang in _EDGE_VOICE_FALLBACKS:
        return _EDGE_VOICE_FALLBACKS[lang]
    return singular_default


def normalize_stt_language(language: str) -> str:
    lang = (language or "").strip().lower().replace("_", "-")
    if not lang:
        return ""
    lang = lang.split(",", 1)[0].split(";", 1)[0].strip()
    primary = lang.split("-", 1)[0]
    if len(primary) == 2 and primary.isalpha():
        return primary
    return _LANGUAGE_NAME_TO_ISO.get(lang) or _LANGUAGE_NAME_TO_ISO.get(primary) or ""


# ---------------------------------------------------------------------------
# Markdown pre-processing (for TTS)
# ---------------------------------------------------------------------------

def _strip_markdown_for_tts(text: str) -> str:
    text = re.sub(r"```[^\n]*\n(.*?)```", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"^#{1,6}\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*{3}(.+?)\*{3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{3}(.+?)_{3}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"\*{2}(.+?)\*{2}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"_{2}(.+?)_{2}", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)\*(.+?)\*(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"~~(.+?)~~", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`(.+?)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", text)
    text = re.sub(r"!\[([^\]]*)\]\([^\)]*\)", r"\1", text)
    text = re.sub(r"^>\s?", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-_*]{3,}\s*$", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[\s]*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# faster-whisper (local) STT
# ---------------------------------------------------------------------------

_fw_model: object = None
_fw_model_lock = threading.Lock()


def _faster_whisper_transcribe(audio: bytes, filename: str) -> Optional[tuple[str, str]]:
    global _fw_model  # noqa: PLW0603

    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        log.error("STT: faster-whisper is not installed")
        return None

    ext = os.path.splitext(filename)[-1].lstrip(".") or "ogg"
    tmp_path = f"/tmp/ap_voice_input.{ext}"
    try:
        with open(tmp_path, "wb") as fh:
            fh.write(audio)
    except OSError as exc:
        log.exception("STT: failed to write temp audio file: %s", exc)
        return None

    try:
        with _fw_model_lock:
            if _fw_model is None:
                log.info("STT: loading faster-whisper model=tiny")
                _fw_model = WhisperModel("tiny", device="cpu", compute_type="int8")
        segments, info = _fw_model.transcribe(tmp_path, beam_size=5)
        language = (info.language or "").lower()
        text = " ".join(seg.text for seg in segments).strip()
        return text, language
    except Exception as exc:
        log.exception("STT: faster-whisper transcription failed: %s", exc)
        return None
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# STT router
# ---------------------------------------------------------------------------

def _openai_transcribe(audio: bytes, filename: str, language: str = "") -> Optional[tuple[str, str]]:
    """Transcribe via the OpenAI Whisper API. Returns ``None`` on any failure."""
    api_key = get_openai_api_key()
    if not api_key:
        log.warning("STT: no OpenAI API key configured (setting: openai_api_key / aw.json)")
        return None

    data = {"model": WHISPER_MODEL, "response_format": "verbose_json"}
    language_hint = normalize_stt_language(language)
    if language_hint:
        data["language"] = language_hint

    # Retry on timeouts/connection errors only — a fresh BytesIO per attempt
    # since the stream is consumed on send, and the request itself already
    # failed once so there's nothing to lose from a couple more tries before
    # falling back to local faster-whisper.
    max_attempts = 3
    for attempt in range(1, max_attempts + 1):
        files = {"file": (filename, io.BytesIO(audio), "application/octet-stream")}
        try:
            with httpx.Client(timeout=120.0) as client:
                resp = client.post(
                    WHISPER_URL,
                    headers={"Authorization": f"Bearer {api_key}"},
                    files=files,
                    data=data,
                )
                if resp.status_code >= 300:
                    log.warning("Whisper returned %s: %s", resp.status_code, resp.text[:300])
                    return None
                body = resp.json()
                text = (body.get("text") or "").strip()
                detected = normalize_stt_language(body.get("language") or "") or language_hint
                return text, detected
        except httpx.TimeoutException as exc:
            log.warning("Whisper request timed out (attempt %d/%d): %s", attempt, max_attempts, exc)
            if attempt == max_attempts:
                return None
            time.sleep(2 * attempt)
        except (httpx.HTTPError, ValueError) as exc:
            log.exception("Whisper request failed: %s", exc)
            return None
    return None


def transcribe(audio: bytes, filename: str = "voice.oga", language: str = "") -> Optional[tuple[str, str]]:
    """Transcribe an audio blob. Returns ``(text, detected_language)`` or ``None``.

    When the configured provider is ``openai`` and the request fails for **any**
    reason (missing key, quota/HTTP error, network error), we automatically fall
    back to the local faster-whisper model so a voice message is never silently
    dropped just because the cloud provider is unavailable.
    """
    if not audio:
        log.warning("STT: empty audio buffer passed to transcribe")
        return None

    provider = get_stt_provider()
    log.info("STT: using provider=%s filename=%s", provider, filename)

    if provider == "local":
        return _faster_whisper_transcribe(audio, filename)

    result = _openai_transcribe(audio, filename, language)
    if result is not None:
        return result

    log.warning("STT: OpenAI transcription failed — falling back to local faster-whisper")
    return _faster_whisper_transcribe(audio, filename)


# ---------------------------------------------------------------------------
# OpenAI TTS
# ---------------------------------------------------------------------------

async def _openai_synthesize_async(
    text: str, *, voice: str = DEFAULT_VOICE, model: str = DEFAULT_MODEL,
    response_format: str = "opus",
) -> Optional[bytes]:
    api_key = get_openai_api_key()
    if not api_key:
        log.warning("TTS: no OpenAI API key configured (setting: openai_api_key)")
        return None
    if voice not in SUPPORTED_VOICES:
        log.warning("TTS: voice %r not in %s — falling back to %s", voice, SUPPORTED_VOICES, DEFAULT_VOICE)
        voice = DEFAULT_VOICE
    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                TTS_URL,
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={"model": model, "input": text, "voice": voice, "response_format": response_format},
            )
            if resp.status_code >= 300:
                log.warning("TTS OpenAI returned %s: %s", resp.status_code, resp.text[:300])
                return None
            return resp.content
    except httpx.HTTPError as exc:
        log.exception("TTS OpenAI request failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Edge TTS
# ---------------------------------------------------------------------------

async def _edge_synthesize_async(text: str, voice: str) -> Optional[bytes]:
    try:
        import edge_tts  # type: ignore
    except ImportError:
        log.warning("TTS: edge_tts not installed — pip install edge-tts")
        return None

    mp3_chunks: list[bytes] = []
    try:
        communicate = edge_tts.Communicate(text, voice)
        async for chunk in communicate.stream():
            if chunk["type"] == "audio":
                mp3_chunks.append(chunk["data"])
    except Exception as exc:
        log.exception("edge_tts stream failed for voice %r: %s", voice, exc)
        return None

    if not mp3_chunks:
        log.warning("edge_tts returned no audio for voice %r", voice)
        return None

    return await _mp3_to_ogg(b"".join(mp3_chunks))


async def _mp3_to_ogg(mp3_bytes: bytes) -> Optional[bytes]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-i", "pipe:0", "-c:a", "libopus", "-b:a", "32k", "-f", "ogg", "pipe:1",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        ogg_bytes, stderr = await proc.communicate(input=mp3_bytes)
        if proc.returncode != 0:
            log.warning("ffmpeg transcode failed (rc=%d): %s", proc.returncode, stderr.decode(errors="replace")[:300])
            return None
        if not ogg_bytes:
            log.warning("ffmpeg produced empty OGG output")
            return None
        return ogg_bytes
    except FileNotFoundError:
        log.error("TTS: ffmpeg not found — cannot transcode Edge TTS output")
        return None
    except Exception as exc:
        log.exception("ffmpeg subprocess error: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Public TTS API
# ---------------------------------------------------------------------------

async def synthesize_async(
    text: str, *, voice: Optional[str] = None, language: str = "",
    model: str = DEFAULT_MODEL, response_format: str = "opus",
) -> Optional[bytes]:
    """Synthesize speech, routing by the configured ``tts_provider`` setting.

    ``language`` (ISO 639-1) picks the per-language Edge voice; ignored for
    OpenAI, whose voices auto-detect language from the text. Falls back to
    the other provider once if the primary fails, so a reply is never silent.
    """
    if not text:
        return None
    text = _strip_markdown_for_tts(text)
    if not text:
        return None
    provider, cfg_oi_voice, cfg_edge_voice, cfg_edge_voices = _resolve_tts_config()

    if provider == "edge":
        active_voice = voice or _pick_edge_voice(language, cfg_edge_voices, cfg_edge_voice)
        audio = await _edge_synthesize_async(text, active_voice)
        if audio is None:
            log.warning("TTS: edge returned None for voice %r — retrying once", active_voice)
            audio = await _edge_synthesize_async(text, active_voice)
        if audio is None and get_openai_api_key():
            log.warning("TTS: edge failed twice — falling back to OpenAI")
            audio = await _openai_synthesize_async(text, voice=cfg_oi_voice, model=model, response_format=response_format)
        return audio

    active_voice = voice or cfg_oi_voice
    audio = await _openai_synthesize_async(text, voice=active_voice, model=model, response_format=response_format)
    if audio is None:
        fb_voice = _pick_edge_voice(language, cfg_edge_voices, cfg_edge_voice)
        log.warning("TTS: openai failed — falling back to edge voice %r", fb_voice)
        audio = await _edge_synthesize_async(text, fb_voice)
    return audio
