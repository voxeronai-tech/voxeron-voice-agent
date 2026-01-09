from __future__ import annotations

import re
import time
from typing import Dict, Optional, Tuple

from .parser_types import ParserResult, ParserStatus, ReasonCode, MatchKind


class DeterministicParser:
    """
    Sprint-1 deterministic parser (RC3 hardened):
      - normalizes BOTH utterance and alias keys
      - exact alias match (O(1) dict lookup)
      - stable typed result
      - no side effects
    """

    def __init__(self, alias_map: Dict[str, str]):
        # Pre-normalize keys once at init
        self.alias_map: Dict[str, str] = {}
        for k, v in (alias_map or {}).items():
            nk = self._norm(k)
            if nk:
                self.alias_map[nk] = v

    def parse(self, utterance: str) -> ParserResult:
        start = time.perf_counter()

        norm = self._norm(utterance)
        if not norm:
            return self._result(
                status=ParserStatus.NO_MATCH,
                reason=ReasonCode.EMPTY_INPUT,
                entity=None,
                kind=MatchKind.ENTITY,
                value=None,
                start=start,
            )

        matched: Optional[str] = self.alias_map.get(norm)
        if matched is not None:
            # If you later adopt special intent tokens, you can type them here.
            kind, value, entity = self._classify(matched)
            return self._result(
                status=ParserStatus.MATCH,
                reason=ReasonCode.EXACT_ALIAS_MATCH,
                entity=entity,
                kind=kind,
                value=value,
                start=start,
            )

        return self._result(
            status=ParserStatus.NO_MATCH,
            reason=ReasonCode.NO_ALIAS_FOUND,
            entity=None,
            kind=MatchKind.ENTITY,
            value=None,
            start=start,
        )

    @staticmethod
    def _classify(matched: str) -> Tuple[MatchKind, Optional[str], Optional[str]]:
        """
        Backward compatible behavior:
          - default: treat matched string as an ENTITY name.
        Optional convention support:
          - "__INTENT__:pickup" -> kind=INTENT, value="pickup"
          - "__VALUE__:something" -> kind=VALUE, value="something"
        """
        m = (matched or "").strip()
        if m.startswith("__INTENT__:"):
            return (MatchKind.INTENT, m.split(":", 1)[1].strip() or None, None)
        if m.startswith("__VALUE__:"):
            return (MatchKind.VALUE, m.split(":", 1)[1].strip() or None, None)
        return (MatchKind.ENTITY, None, m)

    @staticmethod
    def _norm(text: str) -> str:
        """
        STT-safe normalizer:
        - lowercase
        - remove punctuation
        - collapse whitespace
        - unify 'pick up' -> 'pickup'
        """
        if not text:
            return ""
        t = text.strip().lower()

        # Unify common STT variants
        t = t.replace("pick up", "pickup")
        t = t.replace("take away", "takeaway")

        # Replace non-word (punctuation etc.) with spaces
        t = re.sub(r"[^\w\s]", " ", t, flags=re.UNICODE)
        t = re.sub(r"\s+", " ", t, flags=re.UNICODE).strip()
        return t

    @staticmethod
    def _result(
        *,
        status: ParserStatus,
        reason: ReasonCode,
        entity: Optional[str],
        kind: MatchKind,
        value: Optional[str],
        start: float,
    ) -> ParserResult:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        return ParserResult(
            status=status,
            reason=reason,
            matched_entity=entity,
            execution_time_ms=round(elapsed_ms, 3),
            matched_kind=kind,
            matched_value=value,
        )

