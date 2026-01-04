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


@dataclass(frozen=True)
class ParserResult:
    status: ParserStatus
    reason: ReasonCode
    matched_entity: Optional[str]
    execution_time_ms: float
