from __future__ import annotations

import asyncio
import pytest

from src.api.menu_store import MenuSnapshot, MenuItem
from src.api.session_controller import SessionController, SessionState

async def _noop_async(*_args, **_kwargs):
    return None


class DummyTenantManager:
    def load_tenant(self, tenant_ref: str):
        return None

    def apply_aliases(self, tenant_cfg, transcript: str, lang: str):
        return transcript, []

    def strip_affirmation_prefix(self, tenant_cfg, transcript: str, lang: str):
        return transcript, False, None


class FakeWebSocket:
    async def send_text(self, _msg: str) -> None:
        return None


class FakeOpenAIClient:
    """
    Deterministic STT for tests: each transcribe_pcm() pops the next scripted transcript.
    """
    def __init__(self, transcripts):
        self._q = list(transcripts)

    async def transcribe_pcm(self, _pcm: bytes, _lang=None, prompt=None) -> str:
        return self._q.pop(0) if self._q else ""

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


def _mk_menu_snapshot() -> MenuSnapshot:
    """
    Minimal menu that reproduces:
      - leaf item resolvable: Butter Chicken
      - ambiguous head: "biryani" with options like lamb/chicken/vegetarian
      - leaf items exist: Lamb Biryani, Chicken Biryani, Vegetarian Biryani
    """
    snap = MenuSnapshot(
        tenant_id="taj_test",
        tenant_name="Taj Test",
        default_language="english",
    )

    items = [
        MenuItem(
            item_id="butter_chicken",
            name="Butter Chicken",
            description="",
            price_pickup=0.0,
            price_delivery=0.0,
            category_id=None,
            is_available=True,
            tags={},
        ),
        MenuItem(
            item_id="lamb_biryani",
            name="Lamb Biryani",
            description="",
            price_pickup=0.0,
            price_delivery=0.0,
            category_id=None,
            is_available=True,
            tags={},
        ),
        MenuItem(
            item_id="chicken_biryani",
            name="Chicken Biryani",
            description="",
            price_pickup=0.0,
            price_delivery=0.0,
            category_id=None,
            is_available=True,
            tags={},
        ),
        MenuItem(
            item_id="veg_biryani",
            name="Vegetarian Biryani",
            description="",
            price_pickup=0.0,
            price_delivery=0.0,
            category_id=None,
            is_available=True,
            tags={},
        ),
    ]

    # Build the minimal fields parse_add_item relies on
    # MenuStore uses norm_text(); parse_add_item works with snapshot.alias_map/name_choices.
    from src.api.text import norm_text  # local import to avoid import-order issues

    for it in items:
        snap.items_by_id[it.item_id] = it
        nn = norm_text(it.name)
        snap.name_choices.append((nn, it.item_id))
        snap.alias_map[nn] = it.item_id

    # RC1-4: disambiguation metadata
    snap.ambiguity_options = {
        "biryani": ["lamb", "chicken", "vegetarian"]
    }

    return snap


def test_disambiguation_does_not_drop_other_items():
    st = SessionState()
    st.tenant_ref = "taj_mahal"
    st.lang = "en"
    st.menu = _mk_menu_snapshot()

    # Sanity: ensure aliases exist for deterministic matcher
    assert "butter chicken" in st.menu.alias_map
    assert "lamb biryani" in st.menu.alias_map

    oa = FakeOpenAIClient([
        "two butter chicken and two biryani",
        "lamb",
    ])

    controller = SessionController(
        state=st,
        tenant_manager=DummyTenantManager(),
        menu_store=None,  # we inject st.menu directly
        oa=oa,
        tenant_rules_enabled=False,
        tenant_stt_prompt_enabled=False,
        tenant_tts_instructions_enabled=False,
        choose_voice=lambda *_a, **_k: None,
        choose_tts_instructions=lambda *_a, **_k: "",
        enforce_output_language=lambda x, _lang: x,
        send_user_text=_noop_async,
        send_agent_text=_noop_async,
        send_thinking=_noop_async,
        clear_thinking=_noop_async,
        tts_end=_noop_async,
    )

    ws = FakeWebSocket()

    async def run():
        await controller.process_utterance(ws, b"dummy-pcm")
        await controller.process_utterance(ws, b"dummy-pcm")

    asyncio.run(run())

    items = dict(getattr(getattr(st, "order", None), "items", {}) or {})

    assert items.get("butter_chicken") == 2, f"Expected 2x Butter Chicken, got items={items}"
    assert items.get("lamb_biryani") == 2, f"Expected 2x Lamb Biryani, got items={items}"
