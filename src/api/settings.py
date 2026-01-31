from __future__ import annotations

import os


def _get_bool(name: str, default: str = "0") -> bool:
    return os.getenv(name, default).strip().lower() in ("1", "true", "yes", "y", "on")


def _get_int(name: str, default: str) -> int:
    return int(os.getenv(name, default).strip() or default)


def _get_float(name: str, default: str) -> float:
    return float(os.getenv(name, default).strip() or default)


def _get_str(name: str, default: str = "") -> str:
    return (os.getenv(name, default) or default).strip()


# --------------------------------------------------
# Logging
# --------------------------------------------------
LOG_LEVEL = _get_str("LOG_LEVEL", "INFO").upper()

# Temporary debug gate for segmentation diagnostics
DEBUG_SEGMENTATION = _get_bool("DEBUG_SEGMENTATION", "0")

# --------------------------------------------------
# OpenAI
# --------------------------------------------------
OPENAI_API_KEY = _get_str("OPENAI_API_KEY", "")
OPENAI_STT_MODEL = _get_str("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")
OPENAI_CHAT_MODEL = _get_str("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_TTS_MODEL = _get_str("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")

OPENAI_TTS_VOICE_EN = _get_str("OPENAI_TTS_VOICE_EN", "cedar")
OPENAI_TTS_VOICE_NL = _get_str("OPENAI_TTS_VOICE_NL", "marin")

OPENAI_TTS_INSTRUCTIONS_EN = _get_str("OPENAI_TTS_INSTRUCTIONS_EN", "")
OPENAI_TTS_INSTRUCTIONS_NL = _get_str("OPENAI_TTS_INSTRUCTIONS_NL", "")

# --------------------------------------------------
# Tenants
# --------------------------------------------------
TENANTS_DIR = _get_str("TENANTS_DIR", "tenants")
TENANT_RULES_ENABLED = _get_bool("TENANT_RULES_ENABLED", "0")
TENANT_STT_PROMPT_ENABLED = _get_bool("TENANT_STT_PROMPT_ENABLED", "1")
TENANT_TTS_INSTRUCTIONS_ENABLED = _get_bool("TENANT_TTS_INSTRUCTIONS_ENABLED", "1")

# --------------------------------------------------
# DB/menu
# --------------------------------------------------
DATABASE_URL = _get_str("DATABASE_URL", "")
MENU_TTL_SECONDS = _get_int("MENU_TTL_SECONDS", "180")
MENU_SCHEMA = _get_str("MENU_SCHEMA", "public")

# --------------------------------------------------
# Audio/VAD
# --------------------------------------------------
AUDIO_SAMPLE_RATE = _get_int("AUDIO_SAMPLE_RATE", "16000")
AUDIO_FRAME_MS = _get_int("AUDIO_FRAME_MS", "20")

STARTUP_IGNORE_SEC = _get_float("STARTUP_IGNORE_SEC", "1.5")

ENERGY_FLOOR = _get_float("ENERGY_FLOOR", "0.006")
SPEECH_CONFIRM_FRAMES = _get_int("SPEECH_CONFIRM_FRAMES", "3")
MIN_UTTERANCE_MS = _get_int("MIN_UTTERANCE_MS", "900")
SILENCE_END_MS = _get_int("SILENCE_END_MS", "650")

# VAD pre-roll + debug (now passed explicitly into VAD, not read in audio.py)
VAD_PREROLL_MS = _get_int("VAD_PREROLL_MS", "300")
VAD_DEBUG = _get_bool("VAD_DEBUG", "0")

# --------------------------------------------------
# Utterance merge / interruption control (server policy)
# --------------------------------------------------
PAUSE_MERGE_SEC = _get_float("PAUSE_MERGE_SEC", "2.8")
PAUSE_MERGE_SEC_FRAGMENT = _get_float("PAUSE_MERGE_SEC_FRAGMENT", "3.2")
FRAGMENT_MAX_BYTES = _get_int("FRAGMENT_MAX_BYTES", "16000")

BARGE_IN_RMS = _get_float("BARGE_IN_RMS", "450.0")

# --------------------------------------------------
# Heartbeat / liveness
# --------------------------------------------------
HEARTBEAT_IDLE_SEC_DEFAULT = _get_int("HEARTBEAT_IDLE_SEC", "28")
HEARTBEAT_IDLE_SEC_MIN = _get_int("HEARTBEAT_IDLE_SEC_MIN", "25")
HEARTBEAT_CHECK_EVERY_SEC = _get_float("HEARTBEAT_CHECK_EVERY_SEC", "1.0")
HEARTBEAT_GRACE_AFTER_GREETING_SEC = _get_float("HEARTBEAT_GRACE_AFTER_GREETING_SEC", "10.0")

HEARTBEAT_DEBUG_EVERY_SEC = _get_float("HEARTBEAT_DEBUG_EVERY_SEC", "5.0")
COUNT_AUDIO_AS_ACTIVITY = _get_bool("COUNT_AUDIO_AS_ACTIVITY", "0")
