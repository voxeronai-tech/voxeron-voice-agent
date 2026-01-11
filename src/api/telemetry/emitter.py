# src/api/telemetry/emitter.py
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Tuple

from src.api.parser.types import ParserResult

logger = logging.getLogger("taj-agent")

MAX_UTTERANCE = 100

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"\b(\+?\d[\d\s().-]{6,}\d)\b")
_NUM_RE = re.compile(r"\b\d+\b")


@dataclass(frozen=True)
class TelemetryContext:
    session_id: str
    tenant_id: str
    domain: str


@dataclass(frozen=True)
class TelemetryEvent:
    ts: datetime
    session_id: str
    tenant_id: str
    domain: str
    parser_status: str
    parser_reason: str
    utterance_redacted: str
    pii_redacted: bool
    truncation: str
    execution_time_ms: float
    confidence: float


def _head_tail_100(s: str) -> Tuple[str, str, bool]:
    """
    Enforce <= 100 chars using head+tail preservation.
    Use 48 + ' … ' + 48 = 99 chars total (48+3+48).
    """
    s = (s or "").strip()
    if len(s) <= MAX_UTTERANCE:
        return s, "NONE", False

    head = s[:48]
    tail = s[-48:]
    out = f"{head} … {tail}"
    # Out is 99 chars; safety clamp in case of unexpected length.
    out = out[:MAX_UTTERANCE]
    return out, "HEAD_TAIL_48_48", True


def redact_pii_mvp(text: str) -> Tuple[str, bool, str]:
    """
    MVP PII redaction + head/tail truncation.
    Returns (utterance_redacted<=100, pii_redacted_flag, truncation_strategy)
    """
    raw = (text or "").strip()
    if not raw:
        return "", False, "NONE"

    red = raw
    red = _EMAIL_RE.sub("[REDACTED_EMAIL]", red)
    red = _PHONE_RE.sub("[REDACTED_PHONE]", red)
    red = _NUM_RE.sub("[REDACTED_NUM]", red)

    changed = (red != raw)

    red2, trunc, trunc_changed = _head_tail_100(red)
    changed = changed or trunc_changed

    return red2, changed, trunc


InsertFn = Callable[[TelemetryEvent], Awaitable[None]]


class TelemetryEmitter:
    """
    Fire-and-forget emitter.
    Never blocks runtime. Best-effort only.
    """

    def __init__(self, insert_fn: InsertFn, *, timeout_s: float = 0.25) -> None:
        self._insert_fn = insert_fn
        self._timeout_s = timeout_s

    def emit_parser_no_match(
        self,
        *,
        ctx: TelemetryContext,
        utterance: str,
        parser_result: ParserResult,
    ) -> None:
        """
        Sync entrypoint, safe to call from sync orchestrator.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No running loop, do not block, just skip.
            logger.debug("telemetry: no running loop, skip emit")
            return

        try:
            utter_red, pii_redacted, trunc = redact_pii_mvp(utterance)
            evt = TelemetryEvent(
                ts=datetime.now(timezone.utc),
                session_id=ctx.session_id,
                tenant_id=ctx.tenant_id,
                domain=ctx.domain,
                parser_status=str(parser_result.status.value),
                parser_reason=str(parser_result.reason_code.value),
                utterance_redacted=utter_red,
                pii_redacted=pii_redacted,
                truncation=trunc,
                execution_time_ms=float(parser_result.execution_time_ms or 0.0),
                confidence=float(parser_result.confidence or 0.0),
            )
            loop.create_task(self._insert_with_timeout(evt))
        except Exception:
            logger.debug("telemetry: failed to schedule emit", exc_info=True)

    async def _insert_with_timeout(self, evt: TelemetryEvent) -> None:
        try:
            await asyncio.wait_for(self._insert_fn(evt), timeout=self._timeout_s)
        except Exception:
            logger.debug("telemetry: insert failed", exc_info=True)
