from __future__ import annotations

import asyncio
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional, Tuple

logger = logging.getLogger("taj-agent")

MAX_UTTERANCE = 100

_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"\b(\+?\d[\d\s().-]{6,}\d)\b")
_LONG_NUM_RE = re.compile(r"\b\d{5,}\b")  # keep small numbers (qty), redact only long sequences


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
    s = (s or "").strip()
    if len(s) <= MAX_UTTERANCE:
        return s, "NONE", False

    head = s[:48]
    tail = s[-48:]
    out = f"{head} … {tail}"
    out = out[:MAX_UTTERANCE]
    return out, "HEAD_TAIL_48_48", True


def redact_pii_mvp(text: str) -> Tuple[str, bool, str]:
    raw = (text or "").strip()
    if not raw:
        return "", False, "NONE"

    red = raw
    red = _EMAIL_RE.sub("[REDACTED_EMAIL]", red)
    red = _PHONE_RE.sub("[REDACTED_PHONE]", red)
    red = _LONG_NUM_RE.sub("[REDACTED_NUM]", red)

    changed = (red != raw)
    red2, trunc, trunc_changed = _head_tail_100(red)
    changed = changed or trunc_changed

    return red2, changed, trunc


InsertFn = Callable[[TelemetryEvent], Awaitable[None]]


class TelemetryEmitter:
    """
    Fire-and-forget emitter.
    Never blocks runtime. Best-effort only.

    NOTE: SessionController currently instantiates TelemetryEmitter() with no args.
    So we provide a default asyncpg-backed insert_fn when none is passed.
    """

    def __init__(self, insert_fn: Optional[InsertFn] = None, *, timeout_s: float = 0.25) -> None:
        self._timeout_s = timeout_s
        self._enabled = os.getenv("TELEMETRY_ENABLED", "1").strip() not in ("0", "false", "False")
        self._insert_fn: InsertFn = insert_fn or self._default_asyncpg_insert

        self._pool = None
        self._pool_lock = asyncio.Lock()

        self._schema = os.getenv("TELEMETRY_SCHEMA", "public").strip() or "public"
        self._table = os.getenv("TELEMETRY_TABLE", "telemetry_events").strip() or "telemetry_events"
        self._db_url = os.getenv("DATABASE_URL", "").strip()

    def emit_parser_no_match(
        self,
        *,
        ctx: TelemetryContext,
        utterance: str,
        parser_result,  # keep untyped here to avoid import coupling
    ) -> None:
        """
        Called by orchestrator. Must never raise.
        Expects parser_result to expose:
          - status.value
          - reason_code.value
          - execution_time_ms
          - confidence
        """
        if not self._enabled:
            return

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.debug("telemetry: no running loop, skip emit")
            return

        try:
            utter_red, pii_redacted, trunc = redact_pii_mvp(utterance)

            evt = TelemetryEvent(
                ts=datetime.now(timezone.utc),
                session_id=str(getattr(ctx, "session_id", "") or "unknown"),
                tenant_id=str(getattr(ctx, "tenant_id", "") or "unknown"),
                domain=str(getattr(ctx, "domain", "") or "unknown"),
                parser_status=str(getattr(getattr(parser_result, "status", None), "value", "UNKNOWN")),
                parser_reason=str(getattr(getattr(parser_result, "reason_code", None), "value", "UNKNOWN")),
                utterance_redacted=utter_red,
                pii_redacted=bool(pii_redacted),
                truncation=str(trunc),
                execution_time_ms=float(getattr(parser_result, "execution_time_ms", 0.0) or 0.0),
                confidence=float(getattr(parser_result, "confidence", 0.0) or 0.0),
            )
            loop.create_task(self._insert_with_timeout(evt))
        except Exception:
            logger.debug("telemetry: failed to schedule emit", exc_info=True)
            
    def emit_reason_only(
        self,
        *,
        ctx: TelemetryContext,
        utterance: str,
        parser_status: str,
        parser_reason: str,
        execution_time_ms: float = 0.0,
        confidence: float = 0.0,
    ) -> None:
        if not self._enabled:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            utter_red, pii_redacted, trunc = redact_pii_mvp(utterance)
            evt = TelemetryEvent(
                ts=datetime.now(timezone.utc),
                session_id=ctx.session_id,
                tenant_id=ctx.tenant_id,
                domain=ctx.domain,
                parser_status=str(parser_status),
                parser_reason=str(parser_reason),
                utterance_redacted=utter_red,
                pii_redacted=pii_redacted,
                truncation=trunc,
                execution_time_ms=float(execution_time_ms or 0.0),
                confidence=float(confidence or 0.0),
            )
            loop.create_task(self._insert_with_timeout(evt))
        except Exception:
            logger.debug("telemetry: failed to schedule emit_reason_only", exc_info=True)

    async def _insert_with_timeout(self, evt: TelemetryEvent) -> None:
        try:
            await asyncio.wait_for(self._insert_fn(evt), timeout=self._timeout_s)
        except Exception:
            logger.debug("telemetry: insert failed", exc_info=True)

    async def _ensure_pool(self):
        if self._pool is not None:
            return self._pool
        if not self._db_url:
            return None

        async with self._pool_lock:
            if self._pool is not None:
                return self._pool
            try:
                import asyncpg  # type: ignore
            except Exception:
                logger.debug("telemetry: asyncpg not available", exc_info=True)
                return None

            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self._db_url,
                    min_size=0,
                    max_size=int(os.getenv("TELEMETRY_POOL_MAX", "2")),
                    timeout=2.0,
                )
            except Exception:
                logger.debug("telemetry: failed to create pool", exc_info=True)
                self._pool = None

        return self._pool

    async def _default_asyncpg_insert(self, evt: TelemetryEvent) -> None:
        """
        Default insert that matches the TelemetryEvent columns 1:1.

        If your DB table has a different shape, this will fail silently (debug log),
        which is safe but you won't see rows until the schema matches.
        """
        pool = await self._ensure_pool()
        if not pool:
            return

        sql = f"""
        INSERT INTO "{self._schema}"."{self._table}"
        (
          ts,
          session_id,
          tenant_id,
          domain,
          parser_status,
          parser_reason,
          utterance_redacted,
          pii_redacted,
          truncation,
          execution_time_ms,
          confidence
        )
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11)
        """

        async with pool.acquire() as con:
            await con.execute(
                sql,
                evt.ts,
                evt.session_id,
                evt.tenant_id,
                evt.domain,
                evt.parser_status,
                evt.parser_reason,
                evt.utterance_redacted,
                evt.pii_redacted,
                evt.truncation,
                evt.execution_time_ms,
                evt.confidence,
            )
