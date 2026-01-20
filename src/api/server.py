# src/api/server.py
"""
Voxeron Voice Agent - Transport Layer

server.py responsibilities:
- WebSocket IO (PCM frames + JSON control messages)
- VAD segmentation (via services.audio)
- Session lifecycle (per WS: state + controller)
- MenuStore snapshot load + TenantManager config load

All business logic lives in SessionController.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime
from typing import Any, Dict, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import JSONResponse

from .menu_store import MenuStore, MenuSnapshot
from .tenant_manager import TenantManager
from .session_controller import SessionController, SessionState, SESSION_CONTROLLER_VERSION
from .services.audio import VAD, rms_pcm16
from .services.openai_client import OpenAIClient


def _load_dotenv_if_present() -> None:
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv()
    except Exception:
        pass


_load_dotenv_if_present()


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
TENANT_RULES_ENABLED = os.getenv("TENANT_RULES_ENABLED", "0") == "1"
TENANT_STT_PROMPT_ENABLED = os.getenv("TENANT_STT_PROMPT_ENABLED", "1") == "1"
TENANT_TTS_INSTRUCTIONS_ENABLED = os.getenv("TENANT_TTS_INSTRUCTIONS_ENABLED", "1") == "1"

DATABASE_URL = os.getenv("DATABASE_URL", "")
MENU_TTL_SECONDS = int(os.getenv("MENU_TTL_SECONDS", "180"))
MENU_SCHEMA = os.getenv("MENU_SCHEMA", "public")

SAMPLE_RATE = int(os.getenv("AUDIO_SAMPLE_RATE", "16000"))
FRAME_MS = int(os.getenv("AUDIO_FRAME_MS", "20"))

STARTUP_IGNORE_SEC = float(os.getenv("STARTUP_IGNORE_SEC", "1.5"))
ENERGY_FLOOR = float(os.getenv("ENERGY_FLOOR", "0.006"))
SPEECH_CONFIRM_FRAMES = int(os.getenv("SPEECH_CONFIRM_FRAMES", "3"))
MIN_UTTERANCE_MS = int(os.getenv("MIN_UTTERANCE_MS", "900"))
SILENCE_END_MS = int(os.getenv("SILENCE_END_MS", "650"))

HEARTBEAT_IDLE_SEC_DEFAULT = int(os.getenv("HEARTBEAT_IDLE_SEC", "28"))
HEARTBEAT_IDLE_SEC_MIN = int(os.getenv("HEARTBEAT_IDLE_SEC_MIN", "25"))
HEARTBEAT_CHECK_EVERY_SEC = float(os.getenv("HEARTBEAT_CHECK_EVERY_SEC", "1.0"))
HEARTBEAT_GRACE_AFTER_GREETING_SEC = float(os.getenv("HEARTBEAT_GRACE_AFTER_GREETING_SEC", "10.0"))

HEARTBEAT_DEBUG_EVERY_SEC = float(os.getenv("HEARTBEAT_DEBUG_EVERY_SEC", "5.0"))
COUNT_AUDIO_AS_ACTIVITY = os.getenv("COUNT_AUDIO_AS_ACTIVITY", "0") == "1"


app = FastAPI()
tenant_manager = TenantManager(TENANTS_DIR)
menu_store: Optional[MenuStore] = None

oa = OpenAIClient(
    api_key=OPENAI_API_KEY,
    stt_model=OPENAI_STT_MODEL,
    chat_model=OPENAI_CHAT_MODEL,
    tts_model=OPENAI_TTS_MODEL,
    sample_rate=SAMPLE_RATE,
)


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


def choose_voice(lang: str, state: SessionState) -> str:
    cfg = state.tenant_cfg
    if cfg and getattr(cfg, "tts_voices", None) and lang in cfg.tts_voices:
        return cfg.tts_voices[lang]
    if lang == "nl":
        return OPENAI_TTS_VOICE_NL
    return OPENAI_TTS_VOICE_EN


def choose_tts_instructions(lang: str, state: SessionState) -> str:
    if not TENANT_TTS_INSTRUCTIONS_ENABLED:
        return ""
    cfg = state.tenant_cfg
    if cfg and getattr(cfg, "tts_instructions", None) and lang in cfg.tts_instructions:
        return (cfg.tts_instructions.get(lang) or "").strip()
    if lang == "nl":
        return OPENAI_TTS_INSTRUCTIONS_NL
    return OPENAI_TTS_INSTRUCTIONS_EN


def enforce_output_language(text: str, lang: str) -> str:
    return (text or "").strip()


def greeting_text_taj(lang: str) -> str:
    hour = datetime.now().hour
    if lang == "nl":
        if hour < 12:
            return "Goedemorgen, Taj Mahal Bussum. Hoe kan ik u helpen?"
        if hour < 18:
            return "Goedemiddag, Taj Mahal Bussum. Hoe kan ik u helpen?"
        return "Goedenavond, Taj Mahal Bussum. Hoe kan ik u helpen?"
    else:
        if hour < 12:
            return "Good morning, Taj Mahal Bussum. How can I help you today?"
        if hour < 18:
            return "Good afternoon, Taj Mahal Bussum. How can I help you today?"
        return "Good evening, Taj Mahal Bussum. How can I help you today?"


def _tenant_greeting_from_rules(state: SessionState) -> Optional[str]:
    cfg = state.tenant_cfg
    if not cfg or not getattr(cfg, "rules", None):
        return None
    rules = cfg.rules or {}
    disp = rules.get("dispatcher") or {}
    greet = disp.get("greeting") or {}
    if isinstance(greet, dict):
        msg = greet.get(state.lang) or greet.get("en")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
    return None


def _resolve_tenant_ref(requested_tenant: str) -> str:
    t = (requested_tenant or "default").strip()
    return "taj_mahal" if t == "default" else t


def _is_dispatcher_tenant(tenant_ref: str, tenant_cfg) -> bool:
    if tenant_ref == "voxeron_main":
        return True
    if tenant_cfg and getattr(tenant_cfg, "domain_type", None) == "dispatcher":
        return True
    if tenant_cfg and getattr(tenant_cfg, "rules", None):
        if (tenant_cfg.rules or {}).get("domain_type") == "dispatcher":
            return True
    return False


@app.get("/tenant_config")
async def tenant_config(tenant: str = "default") -> JSONResponse:
    tenant_ref = _resolve_tenant_ref(tenant)
    try:
        cfg = tenant_manager.load_tenant(tenant_ref)
    except Exception as e:
        raise HTTPException(status_code=404, detail=f"Tenant not found: {tenant_ref}. err={e}")

    base_lang = getattr(cfg, "base_language", "en") or "en"
    supported = getattr(cfg, "supported_langs", None) or ["en"]

    domain_type = getattr(cfg, "domain_type", None)
    if not domain_type and getattr(cfg, "rules", None):
        domain_type = (cfg.rules or {}).get("domain_type")
    domain_type = domain_type or "restaurant"

    tmp_state = SessionState()
    tmp_state.tenant_cfg = cfg
    tmp_state.lang = base_lang if base_lang in ("en", "nl", "tr") else "en"

    greet = _tenant_greeting_from_rules(tmp_state)
    if not greet and tenant_ref == "taj_mahal":
        greet = greeting_text_taj(tmp_state.lang)
    if not greet:
        greet = (
            "Hi, this is Voxeron. Which service do you need?"
            if tmp_state.lang != "nl"
            else "Hoi, u spreekt met Voxeron. Met welke dienst kan ik u helpen?"
        )

    return JSONResponse(
        {
            "tenant_ref": tenant_ref,
            "tenant_name": getattr(cfg, "tenant_name", tenant_ref),
            "base_language": base_lang,
            "supported_langs": supported,
            "domain_type": domain_type,
            "greeting": greet,
        }
    )


async def heartbeat_loop(ws: WebSocket, controller: SessionController) -> None:
    st = controller.state
    try:
        last_heartbeat_ts = 0.0
        connected_ts = time.time()
        setattr(st, "_hb_last_log_ts", 0.0)

        while True:
            await asyncio.sleep(HEARTBEAT_CHECK_EVERY_SEC)

            cfg = st.tenant_cfg
            idle_sec = HEARTBEAT_IDLE_SEC_DEFAULT
            msg_en = "Still there?"
            msg_nl = "Ben je er nog?"
            msg_tr = "Orada mısınız?"

            if cfg and getattr(cfg, "rules", None):
                hb = (cfg.rules.get("heartbeat") or {})
                try:
                    idle_sec = int(hb.get("idle_seconds") or idle_sec)
                except Exception:
                    pass
                msg_en = str(hb.get("en") or msg_en)
                msg_nl = str(hb.get("nl") or msg_nl)
                msg_tr = str(hb.get("tr") or msg_tr)

            idle_sec = max(int(idle_sec), int(HEARTBEAT_IDLE_SEC_MIN))

            if (time.time() - connected_ts) < HEARTBEAT_GRACE_AFTER_GREETING_SEC:
                continue

            if st.is_processing or getattr(st, "is_speaking", False):
                continue

            if getattr(st, "pending_choice", None):
                continue

            # ✅ Offer is pending -> do NOT heartbeat
            if getattr(st, "offered_item_id", None):
                continue

            last_user_end = float(getattr(st, "last_user_utter_end_ts", 0.0) or 0.0)
            last_agent_end = float(getattr(st, "last_agent_speech_end_ts", 0.0) or 0.0)
            last_activity = float(getattr(st, "last_activity_ts", 0.0) or 0.0)

            anchor = max([last_user_end, last_agent_end, float(last_heartbeat_ts or 0.0), last_activity])
            idle = time.time() - anchor

            now_ts = time.time()
            last_log = float(getattr(st, "_hb_last_log_ts") or 0.0)
            if (now_ts - last_log) >= HEARTBEAT_DEBUG_EVERY_SEC:
                setattr(st, "_hb_last_log_ts", now_ts)
                logger.info(
                    "[hb] idle=%.1fs / idle_sec=%s anchor=%.0f (user_end=%.0f agent_end=%.0f activity=%.0f hb=%.0f) "
                    "processing=%s speaking=%s pending_choice=%s offered_item_id=%s phase=%s lang=%s",
                    idle,
                    idle_sec,
                    anchor,
                    last_user_end,
                    last_agent_end,
                    last_activity,
                    last_heartbeat_ts,
                    st.is_processing,
                    getattr(st, "is_speaking", False),
                    getattr(st, "pending_choice", None),
                    getattr(st, "offered_item_id", None),
                    st.phase,
                    st.lang,
                )

            if idle >= idle_sec and st.phase in ("language_select", "chat", "dispatcher"):
                if st.lang == "nl":
                    msg = msg_nl
                elif st.lang == "tr":
                    msg = msg_tr
                else:
                    msg = msg_en

                msg = enforce_output_language(msg, st.lang)
                last_heartbeat_ts = time.time()
                st.last_activity_ts = last_heartbeat_ts

                await send_agent_text(ws, msg)
                await controller.stream_tts_mp3(ws, msg)

    except asyncio.CancelledError:
        return
    except Exception:
        return


@app.on_event("startup")
async def _startup() -> None:
    global menu_store
    logger.info("[startup] SessionController version=%s", SESSION_CONTROLLER_VERSION)

    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY missing. STT/TTS/LLM will fail.")
    if DATABASE_URL:
        menu_store = MenuStore(DATABASE_URL, ttl_seconds=MENU_TTL_SECONDS, schema=MENU_SCHEMA)
        await menu_store.start()
        logger.info("MenuStore started (Neon)")
    else:
        logger.warning("DATABASE_URL missing; MenuStore disabled.")


@app.on_event("shutdown")
async def _shutdown() -> None:
    global menu_store
    if menu_store:
        await menu_store.close()
    menu_store = None


async def _handle_ws(ws: WebSocket) -> None:
    await ws.accept()

    state = SessionState()

    requested_tenant = (ws.query_params.get("tenant") or "default").strip()
    state.tenant_ref = _resolve_tenant_ref(requested_tenant)

    now = time.time()
    state.last_activity_ts = now
    state.last_agent_speech_end_ts = now
    setattr(state, "last_user_utter_end_ts", now)

    try:
        state.tenant_cfg = tenant_manager.load_tenant(state.tenant_ref)
    except Exception:
        state.tenant_cfg = None

    is_dispatcher = _is_dispatcher_tenant(state.tenant_ref, state.tenant_cfg)

    snap: Optional[MenuSnapshot] = None
    try:
        if (not is_dispatcher) and menu_store:
            snap = await menu_store.get_snapshot(state.tenant_ref, lang="en")
            if snap:
                state.tenant_id = snap.tenant_id
                state.tenant_name = snap.tenant_name
            state.lang = (state.tenant_cfg.base_language if state.tenant_cfg else "en") or "en"
        else:
            state.tenant_id = ""
            state.tenant_name = getattr(state.tenant_cfg, "tenant_name", "Voxeron") if state.tenant_cfg else "Voxeron"
            state.lang = (state.tenant_cfg.base_language if state.tenant_cfg else "en") or "en"
    except Exception as e:
        logger.warning("Snapshot failed; running without menu. err=%s", str(e))
        state.lang = (state.tenant_cfg.base_language if state.tenant_cfg else "en") or "en"

    state.menu = snap
    logger.info("Tenant resolved: %s (%s)", state.tenant_name or "n/a", state.tenant_id or "n/a")

    controller = SessionController(
        state=state,
        tenant_manager=tenant_manager,
        menu_store=menu_store,
        oa=oa,
        tenant_rules_enabled=TENANT_RULES_ENABLED,
        tenant_stt_prompt_enabled=TENANT_STT_PROMPT_ENABLED,
        tenant_tts_instructions_enabled=TENANT_TTS_INSTRUCTIONS_ENABLED,
        choose_voice=choose_voice,
        choose_tts_instructions=choose_tts_instructions,
        enforce_output_language=enforce_output_language,
        send_user_text=send_user_text,
        send_agent_text=send_agent_text,
        send_thinking=send_thinking,
        clear_thinking=clear_thinking,
        tts_end=tts_end,
    )
    
    # S1-4A lifecycle telemetry (fire-and-forget): call started
    try:
        controller._emit_lifecycle("CALL_STARTED")
    except Exception:
        pass

    try:
        state.heartbeat_task = asyncio.create_task(heartbeat_loop(ws, controller))
    except Exception:
        state.heartbeat_task = None

    greet = _tenant_greeting_from_rules(state)
    if not greet and state.tenant_ref == "taj_mahal":
        greet = greeting_text_taj(state.lang)
    if not greet:
        greet = "Hi, this is Voxeron. Which service do you need?"

    if is_dispatcher:
        state.phase = "dispatcher"

    await send_agent_text(ws, greet)
    await controller.stream_tts_mp3(ws, greet)

    state.last_agent_speech_end_ts = time.time()
    state.last_activity_ts = state.last_agent_speech_end_ts

    vad = VAD(
        frame_ms=FRAME_MS,
        energy_floor=ENERGY_FLOOR,
        speech_confirm_frames=SPEECH_CONFIRM_FRAMES,
        silence_end_ms=SILENCE_END_MS,
        min_utterance_ms=MIN_UTTERANCE_MS,
    )
    started = time.time()

    # RC3: merge split user turns across short pauses (prevents agent interrupting)
    pending_utter: bytes | None = None
    pending_deadline_ts: float = 0.0

    # Base grace window, tuned for normal speech pauses
    PAUSE_MERGE_SEC = 1.8

    # Extra patience if the buffered utter is "short" (likely stutter / incomplete thought)
    PAUSE_MERGE_SEC_FRAGMENT = 3.2
    FRAGMENT_MAX_BYTES = 16000  # ~0.5s at 16kHz * 16-bit mono (adjust if your PCM differs)

    # Server-side barge-in on audio energy (in case client doesn't send "barge_in")
    BARGE_IN_RMS = 450.0  # tune, start ~350-600 depending on mic/noise

    try:
        while True:
            # ✅ Robust disconnect handling: Starlette can either raise WebSocketDisconnect,
            # or return a disconnect frame, and/or raise RuntimeError if receive() is called
            # after a disconnect message has been processed.
            try:
                msg = await ws.receive()
            except WebSocketDisconnect:
                logger.info("ws disconnect (WebSocketDisconnect) — exiting loop")
                break
            except RuntimeError as e:
                if 'Cannot call "receive" once a disconnect message has been received' in str(e):
                    logger.info("ws disconnect (runtime after disconnect) — exiting loop")
                    break
                raise

            if not msg:
                logger.info("ws receive returned empty message — exiting loop")
                break

            if msg.get("type") == "websocket.disconnect":
                logger.info("ws disconnect frame — exiting loop")
                break

            if msg.get("text") is not None:
                try:
                    obj = json.loads(msg["text"])
                except Exception:
                    obj = {}
                mtype = obj.get("type")

                if mtype == "end_call":
                    break

                if mtype == "barge_in":
                    tnow = time.time()
                    state.last_activity_ts = tnow
                    state.last_agent_speech_end_ts = tnow

                    if state.tts_task and not state.tts_task.done():
                        state.tts_task.cancel()
                    await clear_audio_queue(ws)
                    continue

                continue

            frame = msg.get("bytes")
            if not frame:
                continue

            if (time.time() - started) < STARTUP_IGNORE_SEC:
                continue

            if COUNT_AUDIO_AS_ACTIVITY:
                state.last_activity_ts = time.time()

            e = rms_pcm16(frame)
            # B) Hard barge-in (server-side): if user starts talking while TTS is playing, stop TTS immediately.
            # This is independent of the client "barge_in" text event.
            if state.tts_task and not state.tts_task.done() and e >= BARGE_IN_RMS:
                tnow = time.time()
                state.last_activity_ts = tnow
                state.last_agent_speech_end_ts = tnow
                try:
                    state.tts_task.cancel()
                except Exception:
                    pass
                await clear_audio_queue(ws)
                # Do NOT continue; still feed VAD so we capture the user's utterance.
            # Flush pending utter if the pause-merge window elapsed
            if pending_utter is not None and time.time() >= pending_deadline_ts:
                tnow = time.time()
                state.last_activity_ts = tnow
                setattr(state, "last_user_utter_end_ts", tnow)

                if state.proc_task and not state.proc_task.done():
                    try:
                        state.proc_task.cancel()
                    except Exception:
                        pass

                state.proc_task = asyncio.create_task(controller.process_utterance(ws, pending_utter))
                pending_utter = None
                pending_deadline_ts = 0.0

            utter = vad.feed(frame, e)
            if utter:
                tnow = time.time()
                state.last_activity_ts = tnow
                setattr(state, "last_user_utter_end_ts", tnow)

                # RC3: merge short pauses into a single user turn
                if pending_utter is None:
                    pending_utter = utter
                else:
                    pending_utter += utter
                # Adaptive grace window: short utterances are often stutters/fragments,
                # so wait longer before flushing to STT to avoid interrupting the user.
                grace = PAUSE_MERGE_SEC_FRAGMENT if (pending_utter and len(pending_utter) <= FRAGMENT_MAX_BYTES) else PAUSE_MERGE_SEC
                pending_deadline_ts = tnow + grace
                continue

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


@app.websocket("/ws_pcm")
async def ws_pcm(ws: WebSocket) -> None:
    await _handle_ws(ws)


@app.websocket("/ws")
async def ws_alias(ws: WebSocket) -> None:
    await _handle_ws(ws)
