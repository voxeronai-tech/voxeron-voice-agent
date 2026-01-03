#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p src/api

cat > src/api/server.py <<'PY'
"""
Voxeron Taj Mahal Voice Agent (Baseline + TenantManager wiring)

Key guarantees:
- WebSocket /ws_pcm receives PCM16 mono @ 16kHz, 20ms frames (320 samples)
- VAD segments utterances using simple energy gate (env-tuned)
- STT: OpenAI /v1/audio/transcriptions (AsyncOpenAI SDK ok)
- LLM: OpenAI /v1/chat/completions (AsyncOpenAI SDK ok)
- TTS: RAW HTTP POST to /v1/audio/speech (supports 'instructions' even if SDK rejects it)
- State: remembers current order; can answer "what's my order"
- Agentic: category listing + category stickiness for follow-ups ("Welke zijn lekker?")
- Tenant: optional tenants/<tenant>/tenant.json, phonetics.json, rules.json via TenantManager
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import re
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI

from .intent import detect_language_intent, norm_simple
from .menu_store import MenuStore, MenuSnapshot
from .tenant_manager import TenantManager, TenantConfig

# -------------------------
# Env / config
# -------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=LOG_LEVEL)
logger = logging.getLogger("taj-agent")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")

OPENAI_TTS_VOICE_EN = os.getenv("OPENAI_TTS_VOICE_EN", "cedar")
OPENAI_TTS_VOICE_NL = os.getenv("OPENAI_TTS_VOICE_NL", "marin")

OPENAI_TTS_INSTRUCTIONS_EN = os.getenv("OPENAI_TTS_INSTRUCTIONS_EN", "").strip()
OPENAI_TTS_INSTRUCTIONS_NL = os.getenv("OPENAI_TTS_INSTRUCTIONS_NL", "").strip()

TENANTS_DIR = os.getenv("TENANTS_DIR", "tenants")
TENANT_RULES_ENABLED = os.getenv("TENANT_RULES_ENABLED", "0") == "1"  # start false => parallel diff logging only
TENANT_STT_PROMPT_ENABLED = os.getenv("TENANT_STT_PROMPT_ENABLED", "1") == "1"
TENANT_TTS_INSTRUCTIONS_ENABLED = os.getenv("TENANT_TTS_INSTRUCTIONS_ENABLED", "1") == "1"

DATABASE_URL = os.getenv("DATABASE_URL", "")
MENU_TTL_SECONDS = int(os.getenv("MENU_TTL_SECONDS", "180"))
MENU_SCHEMA = os.getenv("MENU_SCHEMA", "public")

SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
FRAME_MS = int(os.getenv("AUDIO_FRAME_MS", "20"))
FRAME_SAMPLES = int(SAMPLE_RATE * (FRAME_MS / 1000.0))  # 320 @ 16kHz/20ms

# VAD tuning (backend)
STARTUP_IGNORE_SEC = float(os.getenv("STARTUP_IGNORE_SEC", "1.5"))
ENERGY_FLOOR = float(os.getenv("ENERGY_FLOOR", "0.006"))
ENERGY_CONFIRM_FRAMES = int(os.getenv("ENERGY_CONFIRM_FRAMES", "3"))
SPEECH_CONFIRM_FRAMES = int(os.getenv("SPEECH_CONFIRM_FRAMES", "3"))
MIN_UTTERANCE_MS = int(os.getenv("MIN_UTTERANCE_MS", "900"))
SILENCE_END_MS = int(os.getenv("SILENCE_END_MS", "650"))

TTS_BARGE_RMS = float(os.getenv("TTS_BARGE_RMS", "0.020"))  # used by frontend primarily
TTS_GRACE_MS = int(os.getenv("TTS_GRACE_MS", "120"))

# -------------------------
# Clients / stores
# -------------------------
client = AsyncOpenAI(api_key=OPENAI_API_KEY)
tenant_manager = TenantManager(TENANTS_DIR)
menu_store: Optional[MenuStore] = None

app = FastAPI()


# -------------------------
# Utilities: audio
# -------------------------
def pcm16_to_wav(pcm16: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16)
    return buf.getvalue()


def rms_pcm16(frame: bytes) -> float:
    # frame is little-endian int16 mono
    if not frame:
        return 0.0
    n = len(frame) // 2
    if n <= 0:
        return 0.0
    # manual int16 decode (fast enough)
    ssum = 0.0
    for i in range(0, len(frame), 2):
        v = int.from_bytes(frame[i:i+2], "little", signed=True)
        fv = v / 32768.0
        ssum += fv * fv
    return (ssum / max(1, n)) ** 0.5


# -------------------------
# Utilities: language / text
# -------------------------
def choose_voice(lang: str, state: "SessionState") -> str:
    # prefer tenant voice mapping
    cfg = state.tenant_cfg
    if cfg and cfg.tts_voices and lang in cfg.tts_voices:
        return cfg.tts_voices[lang]
    if lang == "nl":
        return OPENAI_TTS_VOICE_NL
    return OPENAI_TTS_VOICE_EN


def choose_tts_instructions(lang: str, state: "SessionState") -> str:
    if not TENANT_TTS_INSTRUCTIONS_ENABLED:
        return ""
    cfg = state.tenant_cfg
    if cfg and cfg.tts_instructions and lang in cfg.tts_instructions:
        return (cfg.tts_instructions.get(lang) or "").strip()
    # fallback env
    if lang == "nl":
        return OPENAI_TTS_INSTRUCTIONS_NL
    return OPENAI_TTS_INSTRUCTIONS_EN


def enforce_output_language(text: str, lang: str) -> str:
    # Keep minimal: we rely on system prompt + instructions.
    return text.strip()


# -------------------------
# Menu / intent helpers
# -------------------------
CATEGORY_PATTERNS = [
    ("lamb", ["lam", "lams", "lamsgerecht", "lamsgerechten", "lamb", "lamgerechten"]),
    ("chicken", ["kip", "kipgerecht", "kipgerechten", "chicken"]),
    ("biryani", ["biryani"]),
    ("vegetarian", ["vega", "vegetarisch", "vegetarian", "paneer"]),
]

def detect_category_request(text: str) -> Optional[str]:
    t = norm_simple(text)
    if not t:
        return None
    # must look like "which/what do you have" or "what lamb dishes" etc
    cue = any(w in t for w in ["welke", "wat", "have", "hebben", "op het menu", "menu"])
    if not cue:
        # still allow: "lamsgerechten?"
        cue = True if any(k in t for _, keys in CATEGORY_PATTERNS for k in keys) else False
    if not cue:
        return None
    for cat, keys in CATEGORY_PATTERNS:
        if any(k in t for k in keys):
            return cat
    return None


def is_category_followup_preference(text: str) -> bool:
    t = norm_simple(text)
    if not t:
        return False
    cues = [
        "welke zijn lekker", "welke is lekker", "welke raad je aan", "wat raad je aan",
        "welke is het beste", "welke zijn het beste", "aanrader",
        "welke is pittig", "welke zijn pittig", "heel heet", "scherp", "spicy",
        "which are good", "which do you recommend", "best one", "tasty",
        "welke zijn heel lekker", "welke is heel lekker",
    ]
    return any(c in t for c in cues)


def list_items_by_category_heuristic(menu: MenuSnapshot, cat: str) -> List[str]:
    # We may not have category names; heuristic by name keywords
    # Use normalized name, map to display names.
    names: List[str] = []
    for _norm_name, item_id in menu.name_choices:
        disp = menu.display_name(item_id)
        dn = disp.lower()
        if cat == "lamb":
            if "lamb" in dn or "lam" in dn:
                names.append(disp)
        elif cat == "chicken":
            if "chicken" in dn or "kip" in dn:
                names.append(disp)
        elif cat == "biryani":
            if "biryani" in dn:
                names.append(disp)
        elif cat == "vegetarian":
            if any(x in dn for x in ["veg", "veget", "paneer", "dahl", "dal"]):
                names.append(disp)
    # dedupe, stable
    out = []
    seen = set()
    for n in names:
        if n not in seen:
            out.append(n); seen.add(n)
    return out[:30]


# -------------------------
# Order parsing (deterministic, baseline-safe)
# -------------------------
QTY_MAP_NL = {
    "een": 1, "één": 1, "1": 1,
    "twee": 2, "2": 2,
    "drie": 3, "3": 3,
    "vier": 4, "4": 4,
    "vijf": 5, "5": 5,
}
QTY_MAP_EN = {
    "one": 1, "1": 1,
    "two": 2, "2": 2,
    "three": 3, "3": 3,
    "four": 4, "4": 4,
    "five": 5, "5": 5,
}

def extract_qty(text: str, lang: str) -> Optional[int]:
    t = norm_simple(text)
    toks = t.split()
    m = QTY_MAP_NL if lang == "nl" else QTY_MAP_EN
    for tok in toks:
        if tok in m:
            return m[tok]
    return None


def detect_order_summary_request(text: str, lang: str) -> bool:
    t = norm_simple(text)
    if not t:
        return False
    if lang == "nl":
        return any(p in t for p in ["wat is mijn bestelling", "mijn bestelling", "wat heb ik besteld", "in z n geheel", "overzicht"])
    return any(p in t for p in ["what is my order", "my order", "order summary", "what did i order"])


def parse_add_item(menu: MenuSnapshot, text: str) -> List[Tuple[str, int]]:
    """
    Use alias_map for exact-ish recognition. This avoids LLM hallucinating item IDs.
    Strategy:
      - find all alias keys that appear as whole words in normalized text
      - pick best/longest matches
    """
    t = " " + norm_simple(text) + " "
    hits: List[Tuple[str, str]] = []
    for alias, item_id in menu.alias_map.items():
        a = alias.strip()
        if len(a) < 3:
            continue
        if f" {a} " in t:
            hits.append((a, item_id))
    # prefer longer aliases first to avoid partial collisions
    hits.sort(key=lambda x: len(x[0]), reverse=True)

    chosen: List[Tuple[str, int]] = []
    used_item_ids = set()
    qty = 1
    # allow "two butter chicken and two naan" => same sentence, but qty is global; good enough for demo
    # if you want per-item qty later, we can do local window parsing.
    q = extract_qty(text, "nl") or extract_qty(text, "en")
    if q:
        qty = q

    for _alias, item_id in hits:
        if item_id in used_item_ids:
            continue
        used_item_ids.add(item_id)
        chosen.append((item_id, qty))
    return chosen


# -------------------------
# Session state
# -------------------------
@dataclass
class OrderState:
    # item_id -> qty
    items: Dict[str, int] = field(default_factory=dict)

    def add(self, item_id: str, qty: int) -> None:
        if qty <= 0:
            return
        self.items[item_id] = int(self.items.get(item_id, 0) + qty)

    def summary(self, menu: MenuSnapshot) -> str:
        if not self.items:
            return ""
        parts = []
        for item_id, qty in self.items.items():
            parts.append(f"{qty}x {menu.display_name(item_id)}")
        return ", ".join(parts)


@dataclass
class SessionState:
    tenant_ref: str = "default"
    tenant_id: str = ""
    tenant_name: str = ""
    tenant_cfg: Optional[TenantConfig] = None

    lang: str = "en"
    phase: str = "language_select"  # language_select | chat
    order: OrderState = field(default_factory=OrderState)

    is_processing: bool = False
    turn_id: int = 0

    # category stickiness
    last_category: Optional[str] = None
    last_category_items: List[str] = field(default_factory=list)

    # activity/heartbeat
    last_activity_ts: float = 0.0
    heartbeat_task: Optional[asyncio.Task] = None

    # task handles
    proc_task: Optional[asyncio.Task] = None
    tts_task: Optional[asyncio.Task] = None


# -------------------------
# WS send helpers
# -------------------------
async def send_json(ws: WebSocket, obj: Dict[str, Any]) -> None:
    await ws.send_text(json.dumps(obj, ensure_ascii=False))

async def send_user_text(ws: WebSocket, text: str) -> None:
    await send_json(ws, {"type": "user_text", "text": text})

async def send_agent_text(ws: WebSocket, text: str) -> None:
    await send_json(ws, {"type": "agent_text", "text": text})

async def send_thinking(ws: WebSocket) -> None:
    await send_json(ws, {"type": "thinking"})

async def clear_thinking(ws: WebSocket) -> None:
    await send_json(ws, {"type": "clear_thinking"})

async def tts_end(ws: WebSocket) -> None:
    await send_json(ws, {"type": "tts_end"})

async def clear_audio_queue(ws: WebSocket) -> None:
    await send_json(ws, {"type": "clear_audio_queue"})


# -------------------------
# STT
# -------------------------
_current_state: Optional[SessionState] = None

async def transcribe_pcm(pcm16: bytes, lang: Optional[str]) -> str:
    if not pcm16:
        return ""
    wav_bytes = pcm16_to_wav(pcm16, SAMPLE_RATE)
    f = io.BytesIO(wav_bytes)
    f.name = "audio.wav"

    kwargs: Dict[str, Any] = {"model": OPENAI_STT_MODEL, "file": f}

    # Tenant prompt bias (menu vocabulary)
    if TENANT_STT_PROMPT_ENABLED:
        try:
            st = _current_state
            cfg = st.tenant_cfg if st else None
            if cfg and cfg.stt_prompt_base:
                kwargs["prompt"] = str(cfg.stt_prompt_base)
        except Exception:
            pass

    if lang in ("en", "nl", "hi"):
        kwargs["language"] = lang

    try:
        resp = await client.audio.transcriptions.create(**kwargs)
        return (getattr(resp, "text", "") or "").strip()
    except Exception as e:
        logger.exception("STT failed: %s", e)
        return ""


# -------------------------
# TTS (raw HTTP to avoid SDK arg restrictions)
# -------------------------
async def tts_mp3_bytes(text: str, voice: str, instructions: str) -> bytes:
    url = "https://api.openai.com/v1/audio/speech"
    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    payload: Dict[str, Any] = {
        "model": OPENAI_TTS_MODEL,
        "voice": voice,
        "input": text,
    }
    # Only include when non-empty (some deployments reject unknown fields)
    if instructions:
        payload["instructions"] = instructions

    async with httpx.AsyncClient(timeout=60) as hc:
        r = await hc.post(url, headers=headers, json=payload)
        if r.status_code != 200:
            raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:500]}")
        return r.content


async def stream_tts_mp3(ws: WebSocket, state: SessionState, text: str) -> None:
    # Cancel previous TTS if any
    if state.tts_task and not state.tts_task.done():
        state.tts_task.cancel()

    async def _run() -> None:
        try:
            voice = choose_voice(state.lang, state)
            instr = choose_tts_instructions(state.lang, state)
            # safety: if "instructions" breaks on a future API change, retry without it
            try:
                audio = await tts_mp3_bytes(text, voice, instr)
            except Exception:
                audio = await tts_mp3_bytes(text, voice, "")
            # stream in chunks so browser can buffer
            CHUNK = 12000
            for i in range(0, len(audio), CHUNK):
                await ws.send_bytes(audio[i:i+CHUNK])
            await tts_end(ws)
        except asyncio.CancelledError:
            return
        except Exception as e:
            logger.exception("TTS failed: %s", e)

    state.tts_task = asyncio.create_task(_run())
    await state.tts_task


# -------------------------
# LLM (with strict JSON output request, but robust fallback)
# -------------------------
LLM_SYSTEM_BASE = """
You are a helpful restaurant ordering agent for the current tenant.

Rules:
- Always respond in the user's current language (lang).
- You must not invent menu items. Use MENU_CONTEXT only.
- You must remember the CURRENT_CART and never claim it's empty if it's not.
- If the user asks a follow-up like "Welke zijn lekker?" after a category list, ONLY recommend from that category list in MENU_CONTEXT.
- Keep replies concise and natural.

Output format:
Return JSON only:
{
  "reply": "text to say to user",
  "add": [{"item_name": "string", "qty": 1}],
  "remove": [{"item_name": "string", "qty": 1}]
}
If you don't want to add/remove anything, use empty arrays.
""".strip()


def build_llm_messages(state: SessionState, user_text: str, menu_context: str) -> List[Dict[str, str]]:
    cart = state.order.summary(state.menu) if hasattr(state, "menu") and state.menu else ""
    cart_str = cart if cart else "Empty"
    sys = (
        LLM_SYSTEM_BASE
        + f"\n\nlang={state.lang}"
        + f"\nCURRENT_CART: [{cart_str}]"
        + f"\nMENU_CONTEXT:\n{menu_context}"
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user_text},
    ]


async def llm_turn(state: SessionState, user_text: str, menu_context: str) -> Dict[str, Any]:
    msgs = build_llm_messages(state, user_text, menu_context)
    resp = await client.chat.completions.create(
        model=OPENAI_CHAT_MODEL,
        messages=msgs,
        temperature=0.3,
    )
    txt = (resp.choices[0].message.content or "").strip()
    # try parse JSON
    try:
        return json.loads(txt)
    except Exception:
        # fallback: plain text
        return {"reply": txt, "add": [], "remove": []}


# -------------------------
# Heartbeat
# -------------------------
async def heartbeat_loop(ws: WebSocket, state: SessionState) -> None:
    try:
        while True:
            await asyncio.sleep(1.0)
            idle = time.time() - float(state.last_activity_ts or 0.0)
            cfg = state.tenant_cfg

            idle_sec = 10
            msg_en = "Still there? What would you like to order next?"
            msg_nl = "Ben je er nog? Wat wil je hierna bestellen?"

            if cfg and cfg.rules:
                hb = (cfg.rules.get("heartbeat") or {})
                idle_sec = int(hb.get("idle_seconds") or idle_sec)
                msg_en = str(hb.get("en") or msg_en)
                msg_nl = str(hb.get("nl") or msg_nl)

            if idle >= idle_sec and (not state.is_processing) and state.phase in ("language_select", "chat"):
                state.last_activity_ts = time.time()
                msg = msg_nl if state.lang == "nl" else msg_en
                msg = enforce_output_language(msg, state.lang)
                await send_agent_text(ws, msg)
                await stream_tts_mp3(ws, state, msg)
    except asyncio.CancelledError:
        return
    except Exception:
        return


# -------------------------
# Core processing: utterance -> reply
# -------------------------
async def process_utterance(ws: WebSocket, state: SessionState, pcm: bytes) -> None:
    if state.is_processing:
        return
    state.is_processing = True
    state.turn_id += 1

    try:
        await send_thinking(ws)

        # STT
        global _current_state
        _current_state = state
        stt_lang = state.lang if state.lang in ("en", "nl", "hi") else None
        transcript = await transcribe_pcm(pcm, stt_lang)
        _current_state = None

        if not transcript:
            await clear_thinking(ws)
            return

        # optional tenant normalization (parallel-run baseline diff unless enabled)
        baseline_norm = transcript.strip()
        try:
            if state.tenant_cfg:
                tenant_norm = tenant_manager.normalize_text(state.tenant_cfg, state.lang, transcript)
                if TENANT_RULES_ENABLED:
                    transcript = tenant_norm
                else:
                    transcript = baseline_norm
                    if tenant_norm != baseline_norm:
                        logger.info("[tenant_norm][diff] baseline=%r tenant=%r", baseline_norm, tenant_norm)
        except Exception:
            transcript = baseline_norm

        logger.info("STT: %s", transcript)
        await send_user_text(ws, transcript)

        # Language switching logic (your src/api/intent.py single source of truth)
        decision = detect_language_intent(
            transcript,
            phase=state.phase,
            current_lang=state.lang,
            allow_auto_detect=True,
        )
        logger.info(
            "[lang] transcript=%r norm=%r decision=%s/%s explicit=%s reason=%s current=%s phase=%s",
            transcript,
            norm_simple(transcript),
            decision.target, decision.confidence, decision.explicit, decision.reason,
            state.lang, state.phase,
        )
        if decision.target and decision.target != state.lang:
            state.lang = decision.target
            state.phase = "chat"
            msg = "Prima, we gaan verder in het Nederlands. Top. Wat wil je graag bestellen?" if state.lang == "nl" else "Great. What would you like to order?"
            await clear_thinking(ws)
            await send_agent_text(ws, msg)
            await stream_tts_mp3(ws, state, msg)
            return
        if state.phase == "language_select":
            state.phase = "chat"

        # If user asks: "what is my order"
        if state.menu and detect_order_summary_request(transcript, state.lang):
            summary = state.order.summary(state.menu)
            if not summary:
                msg = "Je hebt nog geen bestelling geplaatst. Wat wil je bestellen?" if state.lang == "nl" else "You haven't placed an order yet. What would you like?"
            else:
                msg = f"Je bestelling is: {summary}." if state.lang == "nl" else f"Your order is: {summary}."
            await clear_thinking(ws)
            await send_agent_text(ws, msg)
            await stream_tts_mp3(ws, state, msg)
            return

        # Menu context construction with category stickiness
        menu_context = ""
        match_text = transcript

        cat = detect_category_request(match_text)
        if state.menu and cat:
            items = list_items_by_category_heuristic(state.menu, cat)
            state.last_category = cat
            state.last_category_items = items[:]
            if items:
                menu_context = "\n".join([f"- {x}" for x in items])
            else:
                menu_context = "No matching items found in menu snapshot."
        else:
            if state.menu and state.last_category and state.last_category_items and is_category_followup_preference(match_text):
                menu_context = "\n".join([f"- {x}" for x in state.last_category_items])
            else:
                # default: give a small snapshot of menu candidates (or all aliases is too big)
                if state.menu:
                    # just show a small top list by naive substring/alias hits
                    # (LLM is constrained to MENU_CONTEXT so keep it not too huge)
                    # We'll include 20 random-ish choices from name_choices (stable slice).
                    items = [state.menu.display_name(iid) for _, iid in state.menu.name_choices[:25]]
                    menu_context = "\n".join([f"- {x}" for x in items]) if items else "Menu empty."
                else:
                    menu_context = "Menu empty."

        # Deterministic add-items from alias map (keeps state correct even if LLM says something dumb)
        if state.menu:
            adds = parse_add_item(state.menu, transcript)
            for item_id, qty in adds:
                state.order.add(item_id, qty)

        # LLM reply (still useful for natural language + recommendations)
        out = await llm_turn(state, transcript, menu_context)
        reply = (out.get("reply") or "").strip()

        # If reply contradicts cart ("empty") while we have items, fix it
        if state.menu:
            summary = state.order.summary(state.menu)
            if summary and re.search(r"\bgeen bestelling\b|\bno order\b|\bnothing ordered\b", reply.lower()):
                reply = ("Je bestelling is: " + summary + ". Wil je nog iets toevoegen?") if state.lang == "nl" else ("Your order is: " + summary + ". Would you like to add anything?")

        if not reply:
            reply = "Wil je nog iets toevoegen?" if state.lang == "nl" else "Would you like to add anything else?"

        reply = enforce_output_language(reply, state.lang)

        await clear_thinking(ws)
        await send_agent_text(ws, reply)
        await stream_tts_mp3(ws, state, reply)

    except asyncio.CancelledError:
        return
    except Exception as e:
        logger.exception("process_utterance failed: %s", e)
        try:
            await clear_thinking(ws)
        except Exception:
            pass
    finally:
        state.is_processing = False


# -------------------------
# VAD loop: frames -> utterances
# -------------------------
class VAD:
    def __init__(self):
        self.in_speech = False
        self.speech_frames = 0
        self.silence_frames = 0
        self.buf = bytearray()
        self.started_at = 0.0

    def reset(self):
        self.in_speech = False
        self.speech_frames = 0
        self.silence_frames = 0
        self.buf = bytearray()
        self.started_at = 0.0

    def feed(self, frame: bytes, energy: float) -> Optional[bytes]:
        """
        Returns utterance bytes when speech ends, else None.
        """
        is_voice = energy >= ENERGY_FLOOR
        if not self.in_speech:
            if is_voice:
                self.speech_frames += 1
                if self.speech_frames >= SPEECH_CONFIRM_FRAMES:
                    self.in_speech = True
                    self.started_at = time.time()
                    self.buf.extend(frame)
                    self.silence_frames = 0
            else:
                self.speech_frames = 0
            return None

        # in speech
        self.buf.extend(frame)
        if is_voice:
            self.silence_frames = 0
        else:
            self.silence_frames += 1

        silence_ms = self.silence_frames * FRAME_MS
        utter_ms = int((time.time() - self.started_at) * 1000.0)

        if silence_ms >= SILENCE_END_MS and utter_ms >= MIN_UTTERANCE_MS:
            out = bytes(self.buf)
            self.reset()
            return out
        return None


# -------------------------
# App lifecycle
# -------------------------
@app.on_event("startup")
async def _startup() -> None:
    global menu_store
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY missing. STT/TTS/LLM will fail.")
    if DATABASE_URL:
        menu_store = MenuStore(DATABASE_URL, ttl_seconds=MENU_TTL_SECONDS, schema=MENU_SCHEMA)
        await menu_store.start()
    else:
        logger.warning("DATABASE_URL missing; MenuStore disabled.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global menu_store
    if menu_store:
        await menu_store.close()
    menu_store = None


# -------------------------
# WebSocket endpoint
# -------------------------
@app.websocket("/ws_pcm")
async def ws_pcm(ws: WebSocket) -> None:
    await ws.accept()

    state = SessionState()
    state.tenant_ref = ws.query_params.get("tenant") or "default"
    state.last_activity_ts = time.time()

    # Tenant config: folder mapping (default -> taj_mahal)
    tenant_folder = state.tenant_ref
    if tenant_folder == "default":
        tenant_folder = "taj_mahal"
    try:
        state.tenant_cfg = tenant_manager.load_tenant(tenant_folder)
    except Exception:
        state.tenant_cfg = None

    # Resolve menu snapshot from Neon
    snap: Optional[MenuSnapshot] = None
    try:
        if menu_store:
            snap = await menu_store.get_snapshot(state.tenant_ref, lang="en")
            state.tenant_id = snap.tenant_id
            state.tenant_name = snap.tenant_name
            # default language from tenant cfg else DB default else en
            state.lang = (state.tenant_cfg.base_language if state.tenant_cfg else snap.default_language) or "en"
        else:
            state.lang = (state.tenant_cfg.base_language if state.tenant_cfg else "en") or "en"
    except Exception as e:
        logger.warning("Menu snapshot failed; running with empty menu. err=%s", str(e))
        state.lang = (state.tenant_cfg.base_language if state.tenant_cfg else "en") or "en"

    # Attach menu snapshot to state for downstream helpers
    state.menu = snap  # type: ignore[attr-defined]

    logger.info("Tenant resolved: %s (%s)", state.tenant_name or "n/a", state.tenant_id or "n/a")

    # Start heartbeat
    try:
        state.heartbeat_task = asyncio.create_task(heartbeat_loop(ws, state))
    except Exception:
        state.heartbeat_task = None

    # Greeting
    greet = 'Hi, welcome to Taj Mahal. You can start ordering now. If you want Dutch, say "Nederlands".'
    if state.lang == "nl":
        greet = 'Hoi, welkom bij Taj Mahal. Je kunt nu bestellen. Als je Engels wilt, zeg "English".'
    await send_agent_text(ws, greet)
    await stream_tts_mp3(ws, state, greet)

    vad = VAD()
    started = time.time()

    try:
        while True:
            msg = await ws.receive()
            state.last_activity_ts = time.time()

            if msg.get("text") is not None:
                try:
                    obj = json.loads(msg["text"])
                except Exception:
                    obj = {}
                mtype = obj.get("type")

                if mtype == "end_call":
                    break
                if mtype == "barge_in":
                    # stop playback; frontend handles local stop; server just stops its TTS task
                    if state.tts_task and not state.tts_task.done():
                        state.tts_task.cancel()
                    await clear_audio_queue(ws)
                    continue
                continue

            frame = msg.get("bytes")
            if not frame:
                continue

            # ignore first seconds
            if (time.time() - started) < STARTUP_IGNORE_SEC:
                continue

            e = rms_pcm16(frame)
            # debug VAD logs if needed
            # logger.info("[vad] e=%.4f", e)

            utter = vad.feed(frame, e)
            if utter:
                # cancel previous proc if overlapping
                if state.proc_task and not state.proc_task.done():
                    try:
                        state.proc_task.cancel()
                    except Exception:
                        pass
                state.proc_task = asyncio.create_task(process_utterance(ws, state, utter))

    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.exception("ws loop error: %s", e)
    finally:
        try:
            if state.heartbeat_task and not state.heartbeat_task.done():
                state.heartbeat_task.cancel()
        except Exception:
            pass
        try:
            if state.proc_task and not state.proc_task.done():
                state.proc_task.cancel()
        except Exception:
            pass
        try:
            if state.tts_task and not state.tts_task.done():
                state.tts_task.cancel()
        except Exception:
            pass
        try:
            await ws.close()
        except Exception:
            pass
PY

chmod +x tools/drop_server_py_v2.sh
echo "✅ Wrote src/api/server.py"
echo "Run backend: ./scripts/start_backend.sh"
