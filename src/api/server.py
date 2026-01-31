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
from contextlib import asynccontextmanager


def _load_dotenv_if_present() -> None:
    """
    Load a local .env file if python-dotenv is installed.
    IMPORTANT: Must run before importing settings so env overrides are visible.
    """
    try:
        from dotenv import load_dotenv  # type: ignore

        load_dotenv()
    except Exception:
        pass

_load_dotenv_if_present()

# Centralized settings (single source of truth for env-driven config)
from . import settings  # noqa: E402

# --------------------------------------------------
# Logging (configured once)
# --------------------------------------------------
logging.basicConfig(level=settings.LOG_LEVEL)
logger = logging.getLogger("taj-agent")
@asynccontextmanager
async def lifespan(app: FastAPI):
    global menu_store

    logger.info(
        "[startup] SessionController version=%s",
        SESSION_CONTROLLER_VERSION,
    )

    db_url = (settings.DATABASE_URL or "").strip()
    logger.info("[startup] DATABASE_URL set=%s", bool(db_url))

    if not settings.OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY missing; STT/TTS/LLM calls will fail.")

    # ---- optional MenuStore (must never block startup) ----
    if db_url:
        try:
            menu_store = MenuStore(
                db_url,
                ttl_seconds=settings.MENU_TTL_SECONDS,
                schema=settings.MENU_SCHEMA,
            )
            await menu_store.start()
            logger.info("MenuStore started")
        except Exception:
            logger.exception(
                "MenuStore failed to start; continuing without DB-backed menu"
            )
            menu_store = None
    else:
        logger.warning("DATABASE_URL missing; MenuStore disabled.")
        menu_store = None

    # ---- application runs here ----
    try:
        yield
    finally:
        if menu_store:
            try:
                await menu_store.close()
            except Exception:
                logger.exception("Error while closing MenuStore")
        menu_store = None
        logger.info("MenuStore closed")

# --------------------------------------------------
# App globals
# --------------------------------------------------
app = FastAPI(lifespan=lifespan)
tenant_manager = TenantManager(settings.TENANTS_DIR)
menu_store: Optional[MenuStore] = None

oa = OpenAIClient(
    api_key=settings.OPENAI_API_KEY,
    stt_model=settings.OPENAI_STT_MODEL,
    chat_model=settings.OPENAI_CHAT_MODEL,
    tts_model=settings.OPENAI_TTS_MODEL,
    sample_rate=settings.AUDIO_SAMPLE_RATE,
)

# --------------------------------------------------
# WS helpers
# --------------------------------------------------
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


# --------------------------------------------------
# Tenant-aware choices
# --------------------------------------------------
def choose_voice(lang: str, state: SessionState) -> str:
    cfg = state.tenant_cfg
    if cfg and getattr(cfg, "tts_voices", None) and lang in cfg.tts_voices:
        return cfg.tts_voices[lang]
    return settings.OPENAI_TTS_VOICE_NL if lang == "nl" else settings.OPENAI_TTS_VOICE_EN


def choose_tts_instructions(lang: str, state: SessionState) -> str:
    if not settings.TENANT_TTS_INSTRUCTIONS_ENABLED:
        return ""
    cfg = state.tenant_cfg
    if cfg and getattr(cfg, "tts_instructions", None) and lang in cfg.tts_instructions:
        return (cfg.tts_instructions.get(lang) or "").strip()
    return settings.OPENAI_TTS_INSTRUCTIONS_NL if lang == "nl" else settings.OPENAI_TTS_INSTRUCTIONS_EN


def enforce_output_language(text: str, lang: str) -> str:
    # Transport layer should not rewrite content; keep minimal normalization.
    return (text or "").strip()


# --------------------------------------------------
# Greetings
# --------------------------------------------------
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
    """
    Periodic liveness loop:
    - emits debug heartbeat logs
    - checks idle timeout and closes call if needed

    IMPORTANT: all timing parameters come from src.api.settings (single source of truth).
    """
    st = controller.state

    last_debug_ts = 0.0
    started_ts = time.time()

    while True:
        # Exit if websocket already closed
        try:
            await asyncio.sleep(settings.HEARTBEAT_CHECK_EVERY_SEC)
        except asyncio.CancelledError:
            return

        now = time.time()

        # heartbeat grace after greeting (prevents immediate timeout at call start)
        grace_ok = (now - started_ts) < settings.HEARTBEAT_GRACE_AFTER_GREETING_SEC
        if grace_ok:
            # Still allow debug logging during grace
            pass

        # Determine anchor (latest activity among user/agent)
        last_user_end = float(getattr(st, "last_user_utter_end_ts", 0.0) or 0.0)
        last_agent_end = float(getattr(st, "last_agent_speech_end_ts", 0.0) or 0.0)
        last_activity = float(getattr(st, "last_activity_ts", 0.0) or 0.0)

        anchor = max(last_activity, last_user_end, last_agent_end)
        idle = max(0.0, now - anchor)

        # Determine idle timeout threshold
        idle_sec = max(settings.HEARTBEAT_IDLE_SEC_MIN, settings.HEARTBEAT_IDLE_SEC_DEFAULT)

        # Debug logging (rate-limited)
        if settings.HEARTBEAT_DEBUG_EVERY_SEC > 0:
            if (now - last_debug_ts) >= settings.HEARTBEAT_DEBUG_EVERY_SEC:
                last_debug_ts = now
                logger.info(
                    "[hb] idle=%.1fs / idle_sec=%s anchor=%s (user_end=%s agent_end=%s activity=%s hb=0) "
                    "processing=%s speaking=%s pending_choice=%s offered_item_id=%s phase=%s lang=%s",
                    idle,
                    idle_sec,
                    int(anchor),
                    int(last_user_end),
                    int(last_agent_end),
                    int(last_activity),
                    bool(getattr(st, "proc_task", None) and not st.proc_task.done()),
                    bool(getattr(st, "tts_task", None) and not st.tts_task.done()),
                    getattr(st, "pending_choice", None),
                    getattr(st, "offered_item_id", None),
                    getattr(st, "phase", None),
                    getattr(st, "lang", None),
                )

        # Do not enforce idle timeout during grace window
        if grace_ok:
            continue

        # If currently processing or speaking, do not time out
        processing = bool(getattr(st, "proc_task", None) and not st.proc_task.done())
        speaking = bool(getattr(st, "tts_task", None) and not st.tts_task.done())
        if processing or speaking:
            continue

        # Idle timeout reached -> close call
        if idle >= float(idle_sec):
            try:
                await send_agent_text(ws, "Call ended due to inactivity.")
            except Exception:
                pass
            try:
                await ws.close()
            except Exception:
                pass
            return

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
    
    def _track_task(label: str, t: asyncio.Task) -> asyncio.Task:
        def _done(_t: asyncio.Task) -> None:
            try:
                _t.result()
            except asyncio.CancelledError:
                return
            except Exception:
                logger.exception("%s task crashed", label)
        t.add_done_callback(_done)
        return t

    controller = SessionController(
        state=state,
        tenant_manager=tenant_manager,
        menu_store=menu_store,
        oa=oa,
        tenant_rules_enabled=settings.TENANT_RULES_ENABLED,
        tenant_stt_prompt_enabled=settings.TENANT_STT_PROMPT_ENABLED,
        tenant_tts_instructions_enabled=settings.TENANT_TTS_INSTRUCTIONS_ENABLED,
        choose_voice=choose_voice,
        choose_tts_instructions=choose_tts_instructions,
        enforce_output_language=enforce_output_language,
        send_user_text=send_user_text,
        send_agent_text=send_agent_text,
        send_thinking=send_thinking,
        clear_thinking=clear_thinking,
        tts_end=tts_end,
    )

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
        frame_ms=settings.AUDIO_FRAME_MS,
        energy_floor=settings.ENERGY_FLOOR,
        speech_confirm_frames=settings.SPEECH_CONFIRM_FRAMES,
        silence_end_ms=settings.SILENCE_END_MS,
        min_utterance_ms=settings.MIN_UTTERANCE_MS,
        preroll_ms=settings.VAD_PREROLL_MS,
        debug=settings.VAD_DEBUG,
    )
    started = time.time()

    # RC3: merge split user turns across short pauses (prevents agent interrupting)
    pending_utter: bytes | None = None
    pending_deadline_ts: float = 0.0

    # Base grace window, tuned for normal speech pauses
    PAUSE_MERGE_SEC = settings.PAUSE_MERGE_SEC

    # Extra patience if the buffered utter is "short" (likely stutter / incomplete thought)
    PAUSE_MERGE_SEC_FRAGMENT = settings.PAUSE_MERGE_SEC_FRAGMENT
    FRAGMENT_MAX_BYTES = settings.FRAGMENT_MAX_BYTES # ~0.5s at 16kHz * 16-bit mono (adjust if your PCM differs)

    # Server-side barge-in on audio energy (in case client doesn't send "barge_in")
    # tune, start ~350-600 depending on mic/noise
    BARGE_IN_RMS = settings.BARGE_IN_RMS

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

            if (time.time() - started) < settings.STARTUP_IGNORE_SEC:
                continue

            if settings.COUNT_AUDIO_AS_ACTIVITY:
                state.last_activity_ts = time.time()

            e = rms_pcm16(frame)

            # B) Hard barge-in (server-side): if user starts talking while TTS is playing, stop TTS immediately.
            # This is independent of the client "barge_in" text event.
            if state.tts_task and not state.tts_task.done() and e >= settings.BARGE_IN_RMS:
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

                # Snapshot bytes BEFORE clearing, so we never accidentally pass None/empty
                utter_to_process = pending_utter
                pending_utter = None
                pending_deadline_ts = 0.0

                if state.proc_task and not state.proc_task.done():
                    try:
                        state.proc_task.cancel()
                    except Exception:
                        pass

                if settings.DEBUG_SEGMENTATION:
                    logger.info("SEGMENT DISPATCH: bytes=%s", len(utter_to_process))

                state.proc_task = _track_task(
                    "process_utterance",
                    asyncio.create_task(controller.process_utterance(ws, utter_to_process)),
                )

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
                is_fragment = bool(pending_utter and len(pending_utter) <= settings.FRAGMENT_MAX_BYTES)
                grace = settings.PAUSE_MERGE_SEC_FRAGMENT if is_fragment else settings.PAUSE_MERGE_SEC
                pending_deadline_ts = tnow + grace

                if settings.DEBUG_SEGMENTATION:
                    logger.info(
                        "SEGMENT ARM: pending_bytes=%d is_fragment=%s grace=%.2fs now=%.3f new_deadline=%.3f",
                        len(pending_utter) if pending_utter else 0,
                        is_fragment,
                        grace,
                        tnow,
                        pending_deadline_ts,
                    )

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
