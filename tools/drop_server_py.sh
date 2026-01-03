#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p src/api

cat > src/api/server.py <<'PY'
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import time
import wave
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from openai import AsyncOpenAI
from rapidfuzz import fuzz, process as rf_process

from .intent import detect_language_intent, norm_simple
from .menu_store import MenuStore, MenuSnapshot
from .tenant_manager import TenantManager, TenantConfig

# =========================================================
# Logging
# =========================================================
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO))
logger = logging.getLogger("taj-agent")

# =========================================================
# Env
# =========================================================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
OPENAI_STT_MODEL = os.getenv("OPENAI_STT_MODEL", "gpt-4o-mini-transcribe")
OPENAI_CHAT_MODEL = os.getenv("OPENAI_CHAT_MODEL", "gpt-4o-mini")
OPENAI_TTS_MODEL = os.getenv("OPENAI_TTS_MODEL", "gpt-4o-mini-tts")

OPENAI_TTS_VOICE_EN = os.getenv("OPENAI_TTS_VOICE_EN", "cedar")
OPENAI_TTS_VOICE_NL = os.getenv("OPENAI_TTS_VOICE_NL", "marin")

# These may exist in .env (demo-safe)
OPENAI_TTS_INSTRUCTIONS_EN = os.getenv("OPENAI_TTS_INSTRUCTIONS_EN", "").strip()
OPENAI_TTS_INSTRUCTIONS_NL = os.getenv("OPENAI_TTS_INSTRUCTIONS_NL", "").strip()

DATABASE_URL = os.getenv("DATABASE_URL", "")
DEMO_TENANT_REF = os.getenv("DEMO_TENANT_REF", "default")

TENANTS_DIR = os.getenv("TENANTS_DIR", "tenants")

# Tenant feature flags (keep baseline safe)
TENANT_RULES_ENABLED = os.getenv("TENANT_RULES_ENABLED", "0") == "1"   # start OFF, compare in logs
TENANT_STT_PROMPT_ENABLED = os.getenv("TENANT_STT_PROMPT_ENABLED", "1") == "1"
TENANT_TTS_INSTRUCTIONS_ENABLED = os.getenv("TENANT_TTS_INSTRUCTIONS_ENABLED", "1") == "1"

# VAD / segmentation
STARTUP_IGNORE_SEC = float(os.getenv("STARTUP_IGNORE_SEC", "1.5"))
ENERGY_FLOOR = float(os.getenv("ENERGY_FLOOR", "0.006"))
ENERGY_CONFIRM_FRAMES = int(os.getenv("ENERGY_CONFIRM_FRAMES", "3"))
SPEECH_CONFIRM_FRAMES = int(os.getenv("SPEECH_CONFIRM_FRAMES", "3"))
MIN_UTTERANCE_MS = int(os.getenv("MIN_UTTERANCE_MS", "900"))
SILENCE_END_MS = int(os.getenv("SILENCE_END_MS", "650"))

# Audio framing expectations from frontend
SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
FRAME_MS = int(os.getenv("AUDIO_FRAME_MS", "20"))
FRAME_SAMPLES = int(SAMPLE_RATE * (FRAME_MS / 1000.0))
FRAME_BYTES = FRAME_SAMPLES * 2  # int16

# Heartbeat
DEFAULT_IDLE_SECONDS = 10

# =========================================================
# Clients / Stores
# =========================================================
if not OPENAI_API_KEY:
    logger.warning("OPENAI_API_KEY missing - server will not be able to STT/TTS/LLM.")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)
tenant_manager = TenantManager(TENANTS_DIR)

menu_store: Optional[MenuStore] = None

app = FastAPI()

# =========================================================
# Utilities
# =========================================================
def pcm16_to_wav(pcm16: bytes, sr: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(pcm16)
    return buf.getvalue()


def choose_voice(lang: str) -> str:
    if lang == "nl":
        return OPENAI_TTS_VOICE_NL
    return OPENAI_TTS_VOICE_EN


def choose_tts_instructions(state: "SessionState") -> str:
    """
    Choose TTS instructions (accent/prosody) from tenant config first,
    fall back to .env demo instructions.
    """
    if not TENANT_TTS_INSTRUCTIONS_ENABLED:
        return ""

    cfg = state.tenant_cfg
    if cfg:
        ins = (cfg.tts_instructions or {}).get(state.lang) or ""
        ins = str(ins).strip()
        if ins:
            return ins

    # fallback
    if state.lang == "nl":
        return OPENAI_TTS_INSTRUCTIONS_NL
    return OPENAI_TTS_INSTRUCTIONS_EN


def tr(lang: str, key: str) -> str:
    # minimal baseline phrasing
    table = {
        "greet": {
            "en": 'Hi, welcome to Taj Mahal. You can start ordering now. If you want Dutch, say "Nederlands".',
            "nl": "Hoi, welkom bij Taj Mahal. Je kunt nu bestellen. Als je Engels wilt, zeg “English”.",
        },
        "ask_next": {
            "en": "What would you like to order next?",
            "nl": "Wat wil je hierna bestellen?",
        },
        "still_there": {
            "en": "Still there? What would you like to order next?",
            "nl": "Ben je er nog? Wat wil je hierna bestellen?",
        },
    }
    return table.get(key, {}).get(lang, table.get(key, {}).get("en", ""))


def enforce_output_language(text: str, lang: str) -> str:
    # Keep it simple: we rely on LLM prompt + TTS voice; don’t over-normalize.
    return (text or "").strip()


# =========================================================
# Session State
# =========================================================
@dataclass
class OrderItem:
    item_id: str
    name: str
    qty: int


@dataclass
class OrderState:
    items: Dict[str, OrderItem] = field(default_factory=dict)

    def add(self, item_id: str, name: str, qty: int) -> None:
        if qty <= 0:
            return
        if item_id in self.items:
            self.items[item_id].qty += qty
        else:
            self.items[item_id] = OrderItem(item_id=item_id, name=name, qty=qty)

    def set_qty(self, item_id: str, name: str, qty: int) -> None:
        if qty <= 0:
            self.items.pop(item_id, None)
            return
        self.items[item_id] = OrderItem(item_id=item_id, name=name, qty=qty)

    def summary(self) -> str:
        if not self.items:
            return "Empty"
        parts = [f"{it.qty}x {it.name}" for it in self.items.values()]
        return ", ".join(parts)

    def has_name_like(self, needle: str) -> bool:
        n = (needle or "").lower()
        for it in self.items.values():
            if n in it.name.lower():
                return True
        return False


@dataclass
class SessionState:
    tenant_ref: str = "default"
    tenant_id: str = ""
    tenant_name: str = ""
    tenant_cfg: Optional[TenantConfig] = None

    lang: str = "en"
    phase: str = "language_select"  # language_select -> chat

    # Menu snapshot
    menu: Optional[MenuSnapshot] = None

    # Audio accumulation
    started_ts: float = 0.0
    last_activity_ts: float = 0.0

    # VAD flags
    in_speech: bool = False
    speech_frames: int = 0
    silence_ms: int = 0

    # Turn control
    is_processing: bool = False
    proc_task: Optional[asyncio.Task] = None
    tts_task: Optional[asyncio.Task] = None
    heartbeat_task: Optional[asyncio.Task] = None

    # buffers
    pcm_buf: bytearray = field(default_factory=bytearray)

    # order state
    order: OrderState = field(default_factory=OrderState)


# =========================================================
# WebSocket messaging
# =========================================================
async def send_json(ws: WebSocket, obj: Dict[str, Any]) -> None:
    await ws.send_text(json.dumps(obj))


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


# =========================================================
# TTS (RAW HTTP) with safe fallback
# =========================================================
async def tts_bytes_raw_http(
    *,
    text: str,
    voice: str,
    model: str,
    instructions: str = "",
) -> bytes:
    """
    Uses raw HTTP to bypass SDK parameter restrictions (instructions, response_format, etc.).
    Falls back by retrying without instructions on error.
    """
    if not OPENAI_API_KEY:
        return b""

    url = "https://api.openai.com/v1/audio/speech"
    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}

    async def _post(payload: Dict[str, Any]) -> bytes:
        async with httpx.AsyncClient(timeout=30.0) as c:
            r = await c.post(url, headers=headers, json=payload)
            if r.status_code != 200:
                raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:600]}")
            return r.content

    base_payload: Dict[str, Any] = {
        "model": model,
        "voice": voice,
        "input": text,
        # This is the API param (NOT "format")
        "response_format": "mp3",
    }

    if instructions:
        try:
            p = dict(base_payload)
            p["instructions"] = instructions
            return await _post(p)
        except Exception as e:
            logger.error("TTS with instructions failed, retrying without instructions. err=%s", e)

    # fallback: no instructions
    try:
        return await _post(base_payload)
    except Exception as e:
        logger.exception("TTS failed (raw HTTP): %s", e)
        return b""


async def stream_tts_mp3(ws: WebSocket, state: SessionState, text: str) -> None:
    """
    Generate MP3 bytes and stream as binary frames to frontend.
    """
    if state.tts_task and not state.tts_task.done():
        try:
            state.tts_task.cancel()
        except Exception:
            pass

    async def _run() -> None:
        voice = choose_voice(state.lang)
        instructions = choose_tts_instructions(state)
        audio = await tts_bytes_raw_http(
            text=text,
            voice=voice,
            model=OPENAI_TTS_MODEL,
            instructions=instructions,
        )
        if audio:
            await ws.send_bytes(audio)
        await tts_end(ws)

    state.tts_task = asyncio.create_task(_run())


# =========================================================
# STT (SDK) + tenant prompt bias
# =========================================================
async def transcribe_pcm(state: SessionState, pcm16: bytes, lang: Optional[str]) -> str:
    if not pcm16:
        return ""

    wav_bytes = pcm16_to_wav(pcm16, SAMPLE_RATE)
    f = io.BytesIO(wav_bytes)
    f.name = "audio.wav"

    kwargs: Dict[str, Any] = {
        "model": OPENAI_STT_MODEL,
        "file": f,
    }

    # Tenant STT prompt bias (menu vocab). Keep it safe.
    if TENANT_STT_PROMPT_ENABLED and state.tenant_cfg and state.tenant_cfg.stt_prompt_base:
        kwargs["prompt"] = str(state.tenant_cfg.stt_prompt_base)

    # Whisper-style language hint (if supported)
    if lang in {"en", "nl", "hi"}:
        kwargs["language"] = lang

    try:
        resp = await client.audio.transcriptions.create(**kwargs)
        return (getattr(resp, "text", "") or "").strip()
    except Exception as e:
        logger.exception("STT failed: %s", e)
        return ""


# =========================================================
# Menu candidate retrieval
# =========================================================
def menu_candidates(snap: Optional[MenuSnapshot], text: str, k: int = 10) -> List[Tuple[str, str, int]]:
    """
    Returns list of (display_name, item_id, score).
    1) exact alias_map hit
    2) fuzzy match against canonical names
    """
    if not snap:
        return []

    q = " ".join((text or "").lower().split()).strip()
    if not q:
        return []

    # direct alias hit
    if q in snap.alias_map:
        item_id = snap.alias_map[q]
        return [(snap.display_name(item_id), item_id, 100)]

    choices = [name for (name, _item_id) in snap.name_choices]
    results = rf_process.extract(q, choices, scorer=fuzz.token_set_ratio, limit=k)
    out: List[Tuple[str, str, int]] = []
    # results: [(choice_name, score, idx)]
    for choice_name, score, idx in results:
        item_id = snap.name_choices[idx][1]
        out.append((snap.display_name(item_id), item_id, int(score)))
    return out


def detect_category_request(text: str, lang: str) -> Optional[str]:
    t = norm_simple(text)
    if not t:
        return None
    # Lamb category cues (your demo focus)
    if any(w in t for w in ["lam", "lams", "lamsgerecht", "lamsgerechten", "lamb"]):
        return "lamb"
    if any(w in t for w in ["kip", "chicken"]):
        return "chicken"
    if any(w in t for w in ["veget", "vega", "paneer"]):
        return "veg"
    if "biryani" in t:
        return "biryani"
    return None


def list_items_by_category_heuristic(snap: Optional[MenuSnapshot], category: str) -> List[str]:
    """
    DB schema may not have category names; do a pragmatic heuristic on item names/keywords.
    """
    if not snap:
        return []
    cat = category.lower()
    hits: List[str] = []
    for it in snap.items_by_id.values():
        name = (it.name or "").lower()
        if cat == "lamb" and ("lamb" in name or "lam" in name):
            hits.append(it.name)
        elif cat == "chicken" and "chicken" in name:
            hits.append(it.name)
        elif cat == "veg" and any(x in name for x in ["paneer", "veg", "veget", "dal", "chana"]):
            hits.append(it.name)
        elif cat == "biryani" and "biryani" in name:
            hits.append(it.name)
    # stable order
    hits = sorted(set(hits))
    return hits


# =========================================================
# LLM turn (baseline + better state-awareness)
# =========================================================
LLM_SYSTEM_BASE = """You are a helpful restaurant order-taking voice agent for Taj Mahal.
You must be concise and natural, like a real waiter.

CRITICAL:
- Do NOT upsell items that are already in the cart.
- If user says they already ordered an item, acknowledge and move on.
- If user asks for a menu category (e.g., lamb dishes), list the FULL set provided in MENU_CONTEXT.
- If unsure, ask one short clarification question.

Return natural language only (no JSON).
"""


def build_llm_messages(state: SessionState, user_text: str, menu_context: str) -> List[Dict[str, str]]:
    cart = state.order.summary()
    sys = (
        LLM_SYSTEM_BASE
        + f"\nLANGUAGE={state.lang}\n"
        + f"CURRENT_CART=[{cart}]\n"
        + f"MENU_CONTEXT:\n{menu_context}\n"
    )
    return [
        {"role": "system", "content": sys},
        {"role": "user", "content": user_text},
    ]


async def llm_reply(state: SessionState, user_text: str, menu_context: str) -> str:
    msgs = build_llm_messages(state, user_text, menu_context)
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_CHAT_MODEL,
            messages=msgs,
            temperature=0.4,
        )
        out = (resp.choices[0].message.content or "").strip()
        return out
    except Exception as e:
        logger.exception("LLM failed: %s", e)
        # safe fallback
        return tr(state.lang, "ask_next")


# =========================================================
# Heartbeat (silence for 10 sec)
# =========================================================
async def heartbeat_loop(ws: WebSocket, state: SessionState) -> None:
    try:
        while True:
            await asyncio.sleep(1.0)
            idle = time.time() - float(state.last_activity_ts or 0.0)
            if idle < DEFAULT_IDLE_SECONDS:
                continue
            if state.is_processing:
                continue
            if state.phase not in ("language_select", "chat"):
                continue

            # tenant override
            msg = tr(state.lang, "still_there")
            if state.tenant_cfg:
                hb = (state.tenant_cfg.rules or {}).get("heartbeat") or {}
                idle_sec = int(hb.get("idle_seconds") or DEFAULT_IDLE_SECONDS)
                if idle < idle_sec:
                    continue
                msg = str(hb.get(state.lang) or msg)

            state.last_activity_ts = time.time()
            msg = enforce_output_language(msg, state.lang)
            await send_agent_text(ws, msg)
            await stream_tts_mp3(ws, state, msg)
    except asyncio.CancelledError:
        return
    except Exception:
        return


# =========================================================
# Processing one utterance
# =========================================================
async def process_utterance(ws: WebSocket, state: SessionState, pcm16: bytes) -> None:
    if state.is_processing:
        return
    state.is_processing = True
    try:
        await send_thinking(ws)

        # STT language hint (only en/nl)
        stt_lang = "nl" if state.lang == "nl" else "en"
        transcript = await transcribe_pcm(state, pcm16, stt_lang)
        if transcript:
            logger.info("STT: %s", transcript)

        # Baseline normalizer stays in server (tenant normalization can be enabled later)
        baseline_norm = transcript
        tenant_norm = baseline_norm
        if state.tenant_cfg:
            try:
                tenant_norm = tenant_manager.normalize_text(state.tenant_cfg, state.lang, baseline_norm)
            except Exception:
                tenant_norm = baseline_norm

        if not TENANT_RULES_ENABLED and tenant_norm != baseline_norm:
            logger.info("[tenant_norm][diff] baseline=%r tenant=%r", baseline_norm, tenant_norm)

        transcript = tenant_norm if TENANT_RULES_ENABLED else baseline_norm
        transcript = (transcript or "").strip()
        if not transcript:
            await clear_thinking(ws)
            return

        await send_user_text(ws, transcript)

        # Language selection intent
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
            decision.target,
            decision.confidence,
            decision.explicit,
            decision.reason,
            state.lang,
            state.phase,
        )

        if decision.target in {"en", "nl"}:
            state.lang = decision.target
            state.phase = "chat"
            msg = "Prima, we gaan verder in het Nederlands. Top. Wat wil je graag bestellen?" if state.lang == "nl" else "Great. Let's continue in English. What would you like to order?"
            await clear_thinking(ws)
            await send_agent_text(ws, msg)
            await stream_tts_mp3(ws, state, msg)
            return

        # Category intelligence: pull full list for lamb/chicken/etc
        cat = detect_category_request(transcript, state.lang)
        menu_context = ""
        if cat:
            items = list_items_by_category_heuristic(state.menu, cat)
            if items:
                menu_context = "\n".join([f"- {x}" for x in items])
            else:
                menu_context = "No matching items found in menu snapshot."

        # Otherwise: pass fuzzy candidates
        if not menu_context:
            cands = menu_candidates(state.menu, transcript, k=10)
            if cands:
                menu_context = "\n".join([f"- {name} (score={score})" for name, _id, score in cands])
            else:
                menu_context = "No strong matches."

        # Simple order-state guard: if user says they already have naan
        tnorm = norm_simple(transcript)
        if "al naan" in tnorm or "heb al naan" in tnorm or "had al naan" in tnorm:
            reply = "Klopt, je had al naan. Wil je er ook rijst bij, of wil je nog iets anders?" if state.lang == "nl" else "Right, you already have naan. Would you like rice as well, or anything else?"
        else:
            reply = await llm_reply(state, transcript, menu_context)

        reply = enforce_output_language(reply, state.lang)

        await clear_thinking(ws)
        await send_agent_text(ws, reply)
        await stream_tts_mp3(ws, state, reply)

    finally:
        state.is_processing = False


# =========================================================
# FastAPI lifecycle
# =========================================================
@app.on_event("startup")
async def _startup() -> None:
    global menu_store
    if DATABASE_URL:
        menu_store = MenuStore(DATABASE_URL, ttl_seconds=180, schema="public")
        try:
            await menu_store.start()
        except Exception as e:
            logger.warning("MenuStore start failed; continuing without DB menu. err=%s", e)
            menu_store = None
    else:
        logger.warning("DATABASE_URL missing; MenuStore disabled.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global menu_store
    if menu_store:
        try:
            await menu_store.close()
        except Exception:
            pass
    menu_store = None


# =========================================================
# WebSocket endpoint
# =========================================================
@app.websocket("/ws_pcm")
async def ws_pcm(ws: WebSocket) -> None:
    await ws.accept()
    state = SessionState()
    state.started_ts = time.time()
    state.last_activity_ts = time.time()

    # tenant ref from query param
    tenant_ref = ws.query_params.get("tenant") or DEMO_TENANT_REF or "default"
    state.tenant_ref = tenant_ref

    # Tenant file config: default -> taj_mahal folder
    tenant_folder = tenant_ref
    if tenant_folder == "default":
        tenant_folder = "taj_mahal"

    try:
        state.tenant_cfg = tenant_manager.load_tenant(tenant_folder)
    except Exception:
        state.tenant_cfg = None

    # Load menu snapshot from DB if available
    if menu_store:
        try:
            state.menu = await menu_store.get_snapshot(state.tenant_ref, lang="nl" if state.lang == "nl" else "en")
            state.tenant_id = state.menu.tenant_id
            state.tenant_name = state.menu.tenant_name
            logger.info("Tenant resolved: %s (%s)", state.tenant_name, state.tenant_id)
        except Exception as e:
            logger.warning("Menu snapshot failed; continuing with empty menu. err=%s", e)
            state.menu = None
            logger.info("Tenant resolved: Taj Mahal Restaurant (n/a)")
    else:
        logger.info("Tenant resolved: Taj Mahal Restaurant (n/a)")

    # Start heartbeat loop
    try:
        state.heartbeat_task = asyncio.create_task(heartbeat_loop(ws, state))
    except Exception:
        state.heartbeat_task = None

    # Greet
    greet = enforce_output_language(tr("en", "greet"), "en")
    await send_agent_text(ws, greet)
    await stream_tts_mp3(ws, state, greet)

    # Main receive loop
    try:
        ignore_until = state.started_ts + STARTUP_IGNORE_SEC
        while True:
            msg = await ws.receive()
            state.last_activity_ts = time.time()

            if "text" in msg and msg["text"]:
                try:
                    j = json.loads(msg["text"])
                except Exception:
                    j = {}

                # frontend control messages
                if j.get("type") == "end_call":
                    return
                if j.get("type") == "barge_in":
                    # clear TTS task (frontend will stop playback)
                    if state.tts_task and not state.tts_task.done():
                        try:
                            state.tts_task.cancel()
                        except Exception:
                            pass
                    continue

            if "bytes" not in msg or msg["bytes"] is None:
                continue

            frame = msg["bytes"]
            if len(frame) != FRAME_BYTES:
                # ignore malformed frames
                continue

            # ignore first seconds (mic setup noise)
            if time.time() < ignore_until:
                continue

            # simple energy VAD on int16
            # compute mean abs
            # (fast enough; avoid numpy dependency)
            s = 0
            for i in range(0, len(frame), 2):
                v = int.from_bytes(frame[i:i+2], "little", signed=True)
                s += abs(v)
            mean_abs = s / max(1, FRAME_SAMPLES)
            # normalized-ish energy
            energy = mean_abs / 32768.0

            if energy >= ENERGY_FLOOR:
                state.speech_frames += 1
                state.silence_ms = 0
                if not state.in_speech and state.speech_frames >= SPEECH_CONFIRM_FRAMES:
                    state.in_speech = True
                    logger.info("[vad] speech_start")
                if state.in_speech:
                    state.pcm_buf.extend(frame)
            else:
                # silence
                if state.in_speech:
                    state.silence_ms += FRAME_MS
                    state.pcm_buf.extend(frame)  # keep tail
                    if state.silence_ms >= SILENCE_END_MS:
                        logger.info("[vad] speech_end")
                        pcm = bytes(state.pcm_buf)
                        utter_ms = int(len(pcm) / 2 / SAMPLE_RATE * 1000)
                        state.pcm_buf.clear()
                        state.in_speech = False
                        state.speech_frames = 0
                        state.silence_ms = 0

                        if utter_ms >= MIN_UTTERANCE_MS:
                            # cancel previous proc task if still running
                            if state.proc_task and not state.proc_task.done():
                                try:
                                    state.proc_task.cancel()
                                except Exception:
                                    pass
                            state.proc_task = asyncio.create_task(process_utterance(ws, state, pcm))
                else:
                    state.speech_frames = 0

    except WebSocketDisconnect:
        return
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
PY

echo "✅ Dropped src/api/server.py"
