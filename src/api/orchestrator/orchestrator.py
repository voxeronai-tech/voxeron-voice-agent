# src/api/orchestrator/orchestrator.py
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional

from .deterministic_parser import DeterministicParser
from src.api.parser.types import ParserResult, ParserStatus

logger = logging.getLogger("taj-agent")


class OrchestratorRoute(str, Enum):
    DETERMINISTIC = "deterministic"
    AGENT = "agent"


@dataclass(frozen=True)
class OrchestratorDecision:
    route: OrchestratorRoute
    parser_result: ParserResult
    response_text: Optional[str] = None


class CognitiveOrchestrator:
    """
    Sprint-1 Orchestrator (RC1-2):
      - parser runs BEFORE any LLM call
      - MATCH -> deterministic response path (skip LLM)
      - NO_MATCH -> agent path (LLM)
    """

    def __init__(
        self,
        *,
        alias_map: Dict[str, str],
        deterministic_responder: Optional[Callable[[ParserResult], str]] = None,
    ):
        self._parser = DeterministicParser(alias_map=alias_map)
        self._responder = deterministic_responder or self._default_responder

    def decide(self, utterance_text: str) -> OrchestratorDecision:
        pr = self._parser.parse(utterance_text)

        if pr.status == ParserStatus.MATCH:
            logger.info(
                "RC1-2: MATCH => deterministic route (skip LLM). exec_ms=%.2f",
                pr.execution_time_ms,
            )
            txt = self._responder(pr)
            return OrchestratorDecision(
                route=OrchestratorRoute.DETERMINISTIC,
                parser_result=pr,
                response_text=txt,
            )

        logger.info(
            "RC1-2: %s => agent fallback (LLM allowed). exec_ms=%.2f",
            pr.status,
            pr.execution_time_ms,
        )
        return OrchestratorDecision(
            route=OrchestratorRoute.AGENT,
            parser_result=pr,
            response_text=None,
        )

    @staticmethod
    def _default_responder(pr: ParserResult) -> str:
        me = pr.matched_entity
        if isinstance(me, dict):
            action = (me.get('action') or '').upper()
            if action == 'SET_QTY':
                qty = me.get('quantity')
                return f"Got it — quantity set to {qty}."
            return "Got it."
        entity = me or "that"
        return f"Got it — {entity}."
