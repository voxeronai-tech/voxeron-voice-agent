# tests/unit/test_parser_contract.py
import dataclasses
import pytest

from src.api.parser.contracts import (
    NormalizationTrace,
    ParserResult,
    ParserContext,
    ParseIntent,
    ParseReason,
    ParseStatus,
    UtteranceTelemetryPayload,
)


def test_parser_result_is_frozen():
    n = NormalizationTrace(
        raw_transcript="hi",
        normalized_transcript="hi",
        changed=False,
        applied_aliases=[],
    )
    t = UtteranceTelemetryPayload(
        utterance_redacted="hi",
        pii_redacted=False,
        truncation="NONE",
    )
    r = ParserResult(
        version=1,
        status=ParseStatus.NO_MATCH,
        intent=ParseIntent.UNKNOWN,
        reason=ParseReason.NO_MATCH_GENERIC,
        confidence=0.0,
        domain="restaurant",
        normalization=n,
        telemetry=t,
    )

    with pytest.raises(dataclasses.FrozenInstanceError):
        # type: ignore[attr-defined]
        r.confidence = 1.0


def test_reason_is_enum_member():
    assert isinstance(ParseReason.NO_MATCH_GENERIC, ParseReason)
    assert ParseReason.NO_MATCH_GENERIC.value == "NO_MATCH_GENERIC"


def test_context_is_frozen():
    ctx = ParserContext(cart_summary="x", pending_slot="pending_confirm", last_intent=ParseIntent.CHECKOUT)
    with pytest.raises(dataclasses.FrozenInstanceError):
        # type: ignore[attr-defined]
        ctx.cart_summary = "y"

