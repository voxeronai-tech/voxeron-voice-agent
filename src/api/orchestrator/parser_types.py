from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Optional


class ParserStatus(str, Enum):
    MATCH = "match"
    NO_MATCH = "no_match"


class ReasonCode(str, Enum):
    EXACT_ALIAS_MATCH = "exact_alias_match"
    NO_ALIAS_FOUND = "no_alias_found"
    AMBIGUOUS = "ambiguous"
    EMPTY_INPUT = "empty_input"


class MatchKind(str, Enum):
    """
    Optional semantic typing for a match.
    Backward compatible: if you don't use it, ignore it.
    """
    ENTITY = "entity"   # default (legacy) meaning: matched_entity is the target
    INTENT = "intent"   # if you map aliases to an intent keyword (e.g. "__PICKUP__")
    VALUE = "value"     # if you map aliases to an arbitrary value payload


@dataclass(frozen=True)
class ParserResult:
    status: ParserStatus
    reason: ReasonCode

    # Legacy field kept for backward compatibility (your code already uses this)
    matched_entity: Optional[str]

    execution_time_ms: float

    # New optional fields (safe defaults so old construction still works)
    matched_kind: MatchKind = MatchKind.ENTITY
    matched_value: Optional[str] = None

