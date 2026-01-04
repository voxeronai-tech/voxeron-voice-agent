from __future__ import annotations

import time
from typing import Dict, Optional

from .parser_types import ParserResult, ParserStatus, ReasonCode


class DeterministicParser:
    """
    Sprint-1 deterministic parser:
      - exact alias match (O(1) dict lookup)
      - no side effects
      - stable typed result
    """

    def __init__(self, alias_map: Dict[str, str]):
        self.alias_map = alias_map

    def parse(self, utterance: str) -> ParserResult:
        start = time.perf_counter()

        norm = (utterance or "").strip().lower()
        if not norm:
            return self._result(
                status=ParserStatus.NO_MATCH,
                reason=ReasonCode.EMPTY_INPUT,
                entity=None,
                start=start,
            )

        matched: Optional[str] = self.alias_map.get(norm)
        if matched:
            return self._result(
                status=ParserStatus.MATCH,
                reason=ReasonCode.EXACT_ALIAS_MATCH,
                entity=matched,
                start=start,
            )

        return self._result(
            status=ParserStatus.NO_MATCH,
            reason=ReasonCode.NO_ALIAS_FOUND,
            entity=None,
            start=start,
        )

    @staticmethod
    def _result(
        *,
        status: ParserStatus,
        reason: ReasonCode,
        entity: Optional[str],
        start: float,
    ) -> ParserResult:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return ParserResult(
            status=status,
            reason=reason,
            matched_entity=entity,
            execution_time_ms=round(elapsed_ms, 3),
        )

