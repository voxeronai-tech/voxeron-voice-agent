from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional


class ParserStatus(str, Enum):
    MATCH = "MATCH"
    NO_MATCH = "NO_MATCH"
    PARTIAL = "PARTIAL"
    AMBIGUOUS = "AMBIGUOUS"


class ReasonCode(str, Enum):
    OK = "OK"
    NO_ALIAS = "NO_ALIAS"
    AMBIGUOUS = "AMBIGUOUS"
    EMPTY_INPUT = "EMPTY_INPUT"
    PARSE_ERROR = "PARSE_ERROR"


@dataclass(frozen=True)
class ParserResult:
    status: ParserStatus
    reason_code: ReasonCode

    matched_entity: Optional[Any] = None
    confidence: float = 1.0
    execution_time_ms: float = 0.0

