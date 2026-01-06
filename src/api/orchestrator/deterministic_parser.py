from __future__ import annotations

import time
from typing import Dict, Optional

from src.api.parser.types import ParserResult, ParserStatus, ReasonCode


class DeterministicParser:
    """
    Deterministic alias parser.

    RC1-2 contract:
    - No MatchKind / matched_value fields
    - Return canonical ParserResult (status, reason_code, matched_entity, confidence, execution_time_ms)
    - Never raise; failures become NO_MATCH so orchestrator can fall back to LLM safely
    """

    def __init__(self, alias_map: Dict[str, str]):
        # Store normalized keys for exact lookup
        self.alias_map: Dict[str, str] = {}
        for k, v in (alias_map or {}).items():
            nk = self._norm(k)
            if nk:
                self.alias_map[nk] = v

    def parse(self, utterance: str) -> ParserResult:
        t0 = time.perf_counter()
        try:
            norm = self._norm(utterance)
            if not norm:
                return ParserResult(
                    status=ParserStatus.NO_MATCH,
                    reason_code=ReasonCode.EMPTY_INPUT,
                    matched_entity=None,
                    confidence=0.0,
                    execution_time_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )

            matched: Optional[str] = self.alias_map.get(norm)
            if matched is not None:
                return ParserResult(
                    status=ParserStatus.MATCH,
                    reason_code=ReasonCode.OK,
                    matched_entity=matched,
                    confidence=1.0,
                    execution_time_ms=round((time.perf_counter() - t0) * 1000.0, 3),
                )

            return ParserResult(
                status=ParserStatus.NO_MATCH,
                reason_code=ReasonCode.NO_ALIAS,
                matched_entity=None,
                confidence=0.0,
                execution_time_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            )

        except Exception:
            return ParserResult(
                status=ParserStatus.NO_MATCH,
                reason_code=ReasonCode.PARSE_ERROR,
                matched_entity=None,
                confidence=0.0,
                execution_time_ms=round((time.perf_counter() - t0) * 1000.0, 3),
            )

    @staticmethod
    def _norm(text: str) -> str:
        # Keep behavior stable with previous alias normalization:
        # lowercase, strip, collapse whitespace.
        if not text:
            return ""
        t = " ".join(str(text).strip().lower().split())
        return t
