"""Settings CRUD — global platform configuration.

Persisted via the ``Setting`` model (key/value JSON). Tracked keys:

  * ``command_timeout_seconds`` — timeout for ``code.run_command`` (default 300)
  * ``security_mode``           — global default ``insecure`` | ``secure``
  * ``command_allowlist``       — allow-list of command prefixes (secure mode only)
  * ``command_denylist``        — regex deny-list (always enforced)

Unknown keys are accepted (they round-trip through the API) so future
settings don't need a code change — but only the ones above are read by
the engine.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..core import security

router = APIRouter(prefix="/api/settings", tags=["settings"])

_KNOWN = {"command_timeout_seconds", "security_mode",
          "command_allowlist", "command_denylist",
          "rag_provider", "retro_score_weights",
          "github_sync_enabled", "github_repo", "github_webhook_secret",
          "tts_provider", "stt_provider", "openai_api_key",
          "tts_voice", "edge_voice", "edge_voices"}


class SettingPatch(BaseModel):
    value: Any


@router.get("")
def get_all_settings():
    """Return the effective settings (DB overrides → compiled defaults)."""
    from ..core.rag import _default_provider
    from ..core import voice
    rag = security.get_setting("rag_provider")
    return {
        **security.all_settings(),
        "rag_provider": rag if isinstance(rag, dict) else _default_provider(),
        "github_sync_enabled": security.get_setting("github_sync_enabled", False),
        "github_repo": security.get_setting("github_repo", ""),
        "github_webhook_secret": security.get_setting("github_webhook_secret", ""),
        "tts_provider": voice._resolve_tts_config()[0],
        "stt_provider": voice.get_stt_provider(),
        "openai_api_key": security.get_setting("openai_api_key", ""),
        "tts_voice": security.get_setting("tts_voice", voice.DEFAULT_VOICE),
        "edge_voice": security.get_setting("edge_voice", voice.DEFAULT_EDGE_VOICE),
        "edge_voices": security.get_setting("edge_voices", {}) or {},
        "openai_key_configured": bool(voice.get_openai_api_key()),
        # exposed for the UI so it can show "(default)" badges
        "_defaults": {
            "command_timeout_seconds": security.DEFAULT_COMMAND_TIMEOUT,
            "security_mode":           security.DEFAULT_SECURITY_MODE,
            "command_allowlist":       security.DEFAULT_ALLOWLIST,
            "command_denylist":        security.DEFAULT_DENYLIST,
            "rag_provider":            _default_provider(),
            "tts_provider":            "openai",
            "stt_provider":            "openai",
            "tts_voice":               voice.DEFAULT_VOICE,
            "edge_voice":              voice.DEFAULT_EDGE_VOICE,
        },
    }


@router.put("/{key}")
def update_setting(key: str, body: SettingPatch):
    """Set or replace a setting's value. Validates the known keys' shape."""
    if key == "command_timeout_seconds":
        try:
            v = int(body.value)
        except Exception:
            raise HTTPException(400, "command_timeout_seconds must be an integer")
        if v < 5 or v > 3600:
            raise HTTPException(400, "command_timeout_seconds must be 5–3600")
        security.set_setting(key, v)
    elif key == "security_mode":
        if body.value not in ("insecure", "secure"):
            raise HTTPException(400, "security_mode must be 'insecure' or 'secure'")
        security.set_setting(key, body.value)
    elif key in ("command_allowlist", "command_denylist"):
        if not isinstance(body.value, list) or not all(isinstance(x, str) for x in body.value):
            raise HTTPException(400, f"{key} must be a list of strings")
        security.set_setting(key, body.value)
    elif key == "rag_provider":
        # Loose validation — just ensure it's an object and `kind` is one of the supported ones.
        if not isinstance(body.value, dict):
            raise HTTPException(400, "rag_provider must be a JSON object")
        kind = body.value.get("kind")
        if kind not in ("disabled", "http", "mcp"):
            raise HTTPException(400, "rag_provider.kind must be one of: disabled | http | mcp")
        if kind == "http":
            if not body.value.get("base_url"):
                raise HTTPException(400, "rag_provider(http) requires base_url")
            endpoints = body.value.get("endpoints") or {}
            for op in ("search", "upsert", "delete"):
                if op not in endpoints:
                    raise HTTPException(400, f"rag_provider.endpoints.{op} is required")
        security.set_setting(key, body.value)
    elif key == "retro_score_weights":
        # Dedicated table — validate and write to RetroScoreWeights (not generic store).
        # The UI should prefer PUT /api/retro-score-weights; this handler exists for
        # completeness so the settings UI doesn't have to know about the split.
        from .retro_scores import RetroWeightsIn, set_retro_score_weights
        from ..db import get_session as _gs
        if not isinstance(body.value, dict):
            raise HTTPException(400, "retro_score_weights must be a JSON object {dim: weight}")
        s = next(_gs())
        try:
            return set_retro_score_weights(RetroWeightsIn(weights=body.value), s=s)
        finally:
            s.close()
    elif key == "github_sync_enabled":
        if not isinstance(body.value, bool):
            raise HTTPException(400, "github_sync_enabled must be a boolean")
        security.set_setting(key, body.value)
    elif key == "github_repo":
        if not isinstance(body.value, str):
            raise HTTPException(400, "github_repo must be a string")
        if body.value and "/" not in body.value:
            raise HTTPException(400, "github_repo must be in 'owner/repo' format")
        security.set_setting(key, body.value)
    elif key == "github_webhook_secret":
        if not isinstance(body.value, str):
            raise HTTPException(400, "github_webhook_secret must be a string")
        security.set_setting(key, body.value)
    elif key == "tts_provider":
        if body.value not in ("openai", "edge"):
            raise HTTPException(400, "tts_provider must be 'openai' or 'edge'")
        security.set_setting(key, body.value)
    elif key == "stt_provider":
        if body.value not in ("openai", "local", "edge", "faster-whisper"):
            raise HTTPException(400, "stt_provider must be 'openai' or 'local'")
        security.set_setting(key, body.value)
    elif key == "openai_api_key":
        if not isinstance(body.value, str):
            raise HTTPException(400, "openai_api_key must be a string")
        security.set_setting(key, body.value)
    elif key == "tts_voice":
        from ..core import voice as _voice
        if body.value not in _voice.SUPPORTED_VOICES:
            raise HTTPException(400, f"tts_voice must be one of {_voice.SUPPORTED_VOICES}")
        security.set_setting(key, body.value)
    elif key == "edge_voice":
        if not isinstance(body.value, str) or not body.value:
            raise HTTPException(400, "edge_voice must be a non-empty string")
        security.set_setting(key, body.value)
    elif key == "edge_voices":
        if not isinstance(body.value, dict) or not all(isinstance(v, str) for v in body.value.values()):
            raise HTTPException(400, "edge_voices must be a JSON object of lang -> voice id")
        security.set_setting(key, body.value)
    else:
        # Unknown setting: accept verbatim (forward-compatible)
        security.set_setting(key, body.value)
    return get_all_settings()


@router.post("/reset")
def reset_settings():
    """Delete every override — fall back to compiled defaults."""
    return security.reset_settings()
