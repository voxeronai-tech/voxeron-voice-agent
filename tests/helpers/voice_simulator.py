from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from src.api.session_controller import SessionController, SessionState

@dataclass(frozen=True)
class Turn:
    user: str


@dataclass(frozen=True)
class GoldenConversation:
    tenant_ref: str
    turns: List[Turn]
    expect: Dict[str, Any]
    init: Dict[str, Any]


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent_text: List[str] = []

    async def send_text(self, msg: str) -> None:
        self.sent_text.append(msg)


class FakeOpenAIClient:
    """
    Deterministic STT for golden transcripts:
    each transcribe_pcm() pops the next scripted transcript.
    """
    def __init__(self, transcripts: List[str]) -> None:
        self._q = list(transcripts)

    async def transcribe_pcm(self, _pcm: bytes, _lang=None, prompt=None) -> str:
        if self._q:
            t = self._q.pop(0)
            print("[FakeSTT] transcribe_pcm =>", repr(t))
            return t
        print("[FakeSTT] transcribe_pcm => <empty>")
        return ""

    def fast_yes_no(self, text: str):
        t = (text or "").strip().lower()
        if t in {"yes", "yeah", "yep", "ok", "okay", "oke", "oké", "sure", "correct", "ja", "prima", "klopt"}:
            return "AFFIRM"
        if t in {"no", "nope", "nah", "nee", "neen", "klopt niet", "incorrect"}:
            return "NEGATE"
        return None

    async def chat(self, _msgs, temperature: float = 0.2) -> str:
        return "OK"

    async def tts_mp3_bytes(self, _text: str, _voice=None, _instr: str = "") -> bytes:
        return b""


async def run_golden(
    *,
    conv: GoldenConversation,
    controller_factory: Callable[[SessionState, FakeOpenAIClient], SessionController],
) -> Dict[str, Any]:
    """
    Runs a golden conversation through the REAL process_utterance() path,
    but with fake STT transcripts (no mic/audio dependency).
    """
    st = SessionState()
    st.tenant_ref = conv.tenant_ref

    # Apply initial state overrides (critical for slot-specific tests)
    for k, v in (conv.init or {}).items():
        setattr(st, k, v)

    transcripts = [t.user for t in conv.turns]
    oa = FakeOpenAIClient(transcripts=transcripts)

    controller = controller_factory(st, oa)
    ws = FakeWebSocket()

    print("[CTX] tenant_cfg:", bool(st.tenant_cfg), "lang:", st.lang, "phase:", st.phase)

    # Ensure tenant context (cfg + menu snapshot) is loaded in offline golden tests.
    # In production this is done in the websocket lifecycle; here we do it explicitly.
    await controller._load_tenant_context(st.tenant_ref)

    print("[MENU] keys:", list((getattr(st.menu, "items_by_id", {}) or {}).keys()))
    print("[MENU] amb:", getattr(st.menu, "ambiguity_options", None))
    print("[MENU] alias butter:", (getattr(st.menu, "alias_map", {}) or {}).get("butter chicken"))

    for i, _ in enumerate(conv.turns):
        print(
            f"[TURN {i}] BEFORE phase={st.phase} pending_fulfillment={st.pending_fulfillment} "
            f"fulfillment_mode={st.fulfillment_mode} order={st.order.items}"
        )
        await controller.process_utterance(ws, b"dummy-pcm")
        print(
            f"[TURN {i}] AFTER  phase={st.phase} pending_fulfillment={st.pending_fulfillment} "
            f"fulfillment_mode={st.fulfillment_mode} order={st.order.items} "
            f"pending_disambiguation={getattr(st,'pending_disambiguation',None)}"
        )

    print("WS sent_text:", ws.sent_text)
    print("ORDER ITEMS:", st.order.items)
    print("PENDING_DISAMBIG:", getattr(st, "pending_disambiguation", None))
    print("MENU LOADED:", bool(getattr(st, "menu", None)))

    items = dict(getattr(getattr(st, "order", None), "items", {}) or {})
    pending_disambiguation = bool(getattr(st, "pending_disambiguation", None))
    order_finalized = bool(getattr(st, "order_finalized", False))
    customer_name = getattr(st, "customer_name", None)

    return {
        "items": items,
        "pending_disambiguation": pending_disambiguation,
        "order_finalized": order_finalized,
        "customer_name": customer_name,
    }
