# src/platform/interaction_logger.py
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import asyncpg

# -----------------------------
# Simple PII redaction (MVP)
# -----------------------------
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)

# NOTE: phone detection is intentionally conservative; refine later.
_PHONE_RE = re.compile(r"\b(?:\+?\d{1,3}[\s-]?)?(?:\(?\d{2,4}\)?[\s-]?)?\d{6,10}\b")

_POSTCODE_NL_RE = re.compile(r"\b\d{4}\s?[A-Z]{2}\b", re.IGNORECASE)


def redact_pii(text: str) -> Tuple[str, Dict[str, Any]]:
    """
    Conservative redaction. Schema requires pii_flags jsonb with at least:
      {"has_pii": false}
    We'll include:
      has_pii, email, phone, postcode_nl
    """
    src = text or ""
    redacted = src

    flags: Dict[str, Any] = {
        "has_pii": False,
        "email": False,
        "phone": False,
        "postcode_nl": False,
    }

    if _EMAIL_RE.search(redacted):
        flags["email"] = True
        redacted = _EMAIL_RE.sub("[REDACTED_EMAIL]", redacted)

    if _POSTCODE_NL_RE.search(redacted):
        flags["postcode_nl"] = True
        redacted = _POSTCODE_NL_RE.sub("[REDACTED_POSTCODE]", redacted)

    if _PHONE_RE.search(redacted):
        flags["phone"] = True
        redacted = _PHONE_RE.sub("[REDACTED_PHONE]", redacted)

    flags["has_pii"] = bool(flags["email"] or flags["phone"] or flags["postcode_nl"])
    return redacted, flags


# -----------------------------
# Interaction Logger
# -----------------------------

@dataclass
class InteractionLogInput:
    case_id: str
    turn_id: int
    transcript_raw: Optional[str]
    transcript_redacted: Optional[str]
    pii_flags: Dict[str, Any]
    decision_payload: Dict[str, Any]
    actions_taken: List[Dict[str, Any]]   # schema default is JSON array
    tool_calls: List[Dict[str, Any]]      # schema default is JSON array
    latency_ms: Optional[int] = None


class InteractionLogger:
    """
    Writes to public.interactions, aligned with schema:

    public.interactions columns:
      - interaction_id uuid pk default gen_random_uuid()
      - case_id uuid not null
      - turn_id int not null
      - transcript_raw text null
      - transcript_redacted text null
      - pii_flags jsonb not null default {"has_pii": false}
      - decision_payload jsonb not null default {}
      - actions_taken jsonb not null default []
      - tool_calls jsonb not null default []
      - latency_ms int null
      - created_at timestamptz not null default now()

    Unique constraint: (case_id, turn_id)
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def log_interaction(self, i: InteractionLogInput) -> None:
        q = """
        INSERT INTO public.interactions
            (case_id, turn_id, transcript_raw, transcript_redacted, pii_flags,
             decision_payload, actions_taken, tool_calls, latency_ms)
        VALUES
            ($1, $2, $3, $4,
             $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9)
        ON CONFLICT (case_id, turn_id)
        DO UPDATE SET
            transcript_raw      = EXCLUDED.transcript_raw,
            transcript_redacted = EXCLUDED.transcript_redacted,
            pii_flags           = EXCLUDED.pii_flags,
            decision_payload    = EXCLUDED.decision_payload,
            actions_taken       = EXCLUDED.actions_taken,
            tool_calls          = EXCLUDED.tool_calls,
            latency_ms          = EXCLUDED.latency_ms
        """
        async with self.pool.acquire() as conn:
            await conn.execute(
                q,
                i.case_id,
                int(i.turn_id),
                i.transcript_raw,
                i.transcript_redacted,
                json.dumps(i.pii_flags or {"has_pii": False}),
                json.dumps(i.decision_payload or {}),
                json.dumps(i.actions_taken or []),
                json.dumps(i.tool_calls or []),
                i.latency_ms,
            )

