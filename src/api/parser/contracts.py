# src/api/parser/contracts.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


# -------------------------
# Enums
# -------------------------

class ParseStatus(str, Enum):
    MATCH = "MATCH"
    PARTIAL_MATCH = "PARTIAL_MATCH"
    NO_MATCH = "NO_MATCH"
    ERROR = "ERROR"


class ParseIntent(str, Enum):
    ADD_ITEM = "ADD_ITEM"
    REMOVE_ITEM = "REMOVE_ITEM"
    SET_QUANTITY = "SET_QUANTITY"
    QUERY_MENU = "QUERY_MENU"
    QUERY_ORDER_SUMMARY = "QUERY_ORDER_SUMMARY"
    CHECKOUT = "CHECKOUT"
    CONFIRM_YES = "CONFIRM_YES"
    CONFIRM_NO = "CONFIRM_NO"
    PROVIDE_NAME = "PROVIDE_NAME"
    SET_FULFILLMENT = "SET_FULFILLMENT"
    CLOSE_CALL = "CLOSE_CALL"
    UNKNOWN = "UNKNOWN"


class ParseReason(str, Enum):
    OK = "OK"

    NO_MATCH_GENERIC = "NO_MATCH_GENERIC"
    NO_MATCH_EMPTY = "NO_MATCH_EMPTY"
    NO_MATCH_TOO_SHORT = "NO_MATCH_TOO_SHORT"
    NO_MATCH_UNSUPPORTED_LANGUAGE = "NO_MATCH_UNSUPPORTED_LANGUAGE"
    NO_MATCH_AMBIGUOUS = "NO_MATCH_AMBIGUOUS"
    NO_MATCH_OOV_MENU = "NO_MATCH_OOV_MENU"

    PARTIAL_MISSING_ITEM = "PARTIAL_MISSING_ITEM"
    PARTIAL_MISSING_QUANTITY = "PARTIAL_MISSING_QUANTITY"
    PARTIAL_MISSING_VARIANT = "PARTIAL_MISSING_VARIANT"
    PARTIAL_NEEDS_CLARIFICATION = "PARTIAL_NEEDS_CLARIFICATION"

    ERROR_EXCEPTION = "ERROR_EXCEPTION"
    ERROR_INVALID_CONTEXT = "ERROR_INVALID_CONTEXT"


# -------------------------
# Normalization + telemetry payloads
# -------------------------

@dataclass(frozen=True)
class NormalizationTrace:
    raw_transcript: str
    normalized_transcript: str
    changed: bool
    applied_aliases: List[str] = field(default_factory=list)
    lang_inferred: Optional[str] = None
    notes: List[str] = field(default_factory=list)


@dataclass(frozen=True)
class UtteranceTelemetryPayload:
    utterance_redacted: str
    pii_redacted: bool
    truncation: str  # "NONE" | "HEAD_TAIL_50_50"


# -------------------------
# Context snapshot (parser is stateless, but accepts context)
# -------------------------

@dataclass(frozen=True)
class ParserContext:
    cart_summary: str = ""
    pending_slot: Optional[str] = None
    last_intent: Optional[ParseIntent] = None
    menu_snapshot_id: Optional[str] = None


# -------------------------
# Result contract
# -------------------------

@dataclass(frozen=True)
class ParserResult:
    version: int
    status: ParseStatus
    intent: ParseIntent
    reason: ParseReason
    confidence: float
    domain: str
    normalization: NormalizationTrace
    telemetry: UtteranceTelemetryPayload

    entities: Dict[str, Any] = field(default_factory=dict)
    delta: Dict[str, Any] = field(default_factory=dict)
    next_action: Optional[str] = None
A
A
A
A
A
A
A
A
A
A
A
A

