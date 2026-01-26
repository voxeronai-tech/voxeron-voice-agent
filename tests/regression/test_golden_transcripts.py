# tests/regression/test_golden_transcripts.py
from __future__ import annotations

import asyncio
import json
from pathlib import Path

from tests.helpers.voice_simulator import (
    GoldenConversation,
    Turn,
    run_golden,
    FakeOpenAIClient,
)

from src.api.session_controller import SessionController, SessionState
from tests.helpers.menu_fixtures import taj_minimal_menu_snapshot


class DummyTenantManager:
    """Golden tests run without tenant rules/config. Provide safe no-ops."""

    def load_tenant(self, tenant_ref: str):
        return None

    def apply_aliases(self, tenant_cfg, transcript: str, lang: str):
        return transcript, []

    def strip_affirmation_prefix(self, tenant_cfg, transcript: str, lang: str):
        return transcript, False, None


class FakeMenuStore:
    """Deterministic menu snapshots for golden tests (no DB dependency)."""

    async def get_snapshot(self, tenant_ref: str, lang: str = "en"):
        if tenant_ref == "taj_mahal":
            return taj_minimal_menu_snapshot(lang=lang)
        return None


async def _noop_async(*_args, **_kwargs):
    return None


def controller_factory(st: SessionState, oa: FakeOpenAIClient) -> SessionController:
    return SessionController(
        state=st,
        tenant_manager=DummyTenantManager(),
        menu_store=FakeMenuStore(),
        oa=oa,
        tenant_rules_enabled=False,
        tenant_stt_prompt_enabled=False,
        tenant_tts_instructions_enabled=False,
        choose_voice=lambda *_args, **_kwargs: None,
        choose_tts_instructions=lambda *_args, **_kwargs: "",
        enforce_output_language=lambda x, _lang: x,
        send_user_text=_noop_async,
        send_agent_text=_noop_async,
        send_thinking=_noop_async,
        clear_thinking=_noop_async,
        tts_end=_noop_async,
    )


def load_convs(p: Path) -> list[GoldenConversation]:
    obj = json.loads(p.read_text(encoding="utf-8"))

    # Case A: file contains a LIST of conversations
    if isinstance(obj, list):
        convs: list[GoldenConversation] = []
        for c in obj:
            turns = [Turn(user=t["user"]) for t in c["turns"]]
            convs.append(
                GoldenConversation(
                    tenant_ref=c["tenant_ref"],
                    turns=turns,
                    expect=c.get("expect", {}),
                    init=c.get("init", {}),
                )
            )
        return convs

    # Case B: file contains a SINGLE conversation object
    turns = [Turn(user=t["user"]) for t in obj["turns"]]
    return [
        GoldenConversation(
            tenant_ref=obj["tenant_ref"],
            turns=turns,
            expect=obj.get("expect", {}),
            init=obj.get("init", {}),
        )
    ]


def test_golden_transcripts_smoke():
    asyncio.run(_test_golden_transcripts_smoke())


async def _test_golden_transcripts_smoke():
    root = Path("tests/regression")
    files: list[Path] = []

    # Include JSON files that are either a dict with "turns" or a list of dicts with "turns"
    for p in sorted(root.glob("*.json")):
        obj = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(obj, dict) and "turns" in obj:
            files.append(p)
        elif isinstance(obj, list) and obj and isinstance(obj[0], dict) and "turns" in obj[0]:
            files.append(p)

    assert files, "No golden transcript JSON files found in tests/regression/"

    for f in files:
        convs = load_convs(f)
        for conv in convs:
            out = await run_golden(conv=conv, controller_factory=controller_factory)
            exp = conv.expect or {}

            if "pending_disambiguation" in exp:
                assert out["pending_disambiguation"] == exp["pending_disambiguation"], f"{f}: pending_disambiguation"

            if "order_finalized" in exp:
                assert out["order_finalized"] == exp["order_finalized"], f"{f}: order_finalized"

            if "customer_name" in exp:
                assert (out.get("customer_name") or "") == (exp["customer_name"] or ""), f"{f}: customer_name"

            if "items" in exp:
                assert out["items"] == exp["items"], f"{f}: items"

            if exp.get("items_nonempty"):
                assert out["items"], f"{f}: expected items but cart is empty"
