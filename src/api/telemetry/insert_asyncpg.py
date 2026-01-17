from __future__ import annotations

import json
import os
from typing import Optional

import logging

from .emitter import TelemetryEvent

logger = logging.getLogger("taj-agent")

_pool = None
_init_started = False

TELEMETRY_SCHEMA = os.getenv("TELEMETRY_SCHEMA", "public")
TELEMETRY_TABLE = os.getenv("TELEMETRY_TABLE", "telemetry_events")
DATABASE_URL = os.getenv("DATABASE_URL", "")


async def _get_pool():
    global _pool, _init_started
    if _pool is not None:
        return _pool
    if _init_started:
        return _pool
    _init_started = True

    if not DATABASE_URL:
        return None

    try:
        import asyncpg  # type: ignore
        _pool = await asyncpg.create_pool(
            dsn=DATABASE_URL,
            min_size=0,
            max_size=int(os.getenv("TELEMETRY_POOL_MAX", "2")),
            timeout=2.0,
        )
    except Exception:
        logger.debug("telemetry: failed to create asyncpg pool", exc_info=True)
        _pool = None
    return _pool


async def insert_telemetry_event(evt: TelemetryEvent) -> None:
    """
    Best-effort insert. Must NEVER raise.
    Assumes telemetry_events table columns match TelemetryEvent fields.
    """
    try:
        pool = await _get_pool()
        if not pool:
            return

        sql = f"""
        INSERT INTO "{TELEMETRY_SCHEMA}"."{TELEMETRY_TABLE}"
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
    except Exception:
        logger.debug("telemetry: insert failed", exc_info=True)

