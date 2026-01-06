# src/api/orchestrator/orchestrator.py
from __future__ import annotations

import logging
logger = logging.getLogger("taj-agent")

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional

from .deterministic_parser import DeterministicParser
from src.api.parser.types import ParserResult, ParserStatus

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
    Sprint-1 Orchestrator (RC3 hardened):
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
                matched_kind=getattr(pr, "matched_kind", MatchKind.ENTITY),
                matched_entity=getattr(pr, "matched_entity", None),
                matched_value=getattr(pr, "matched_value", None),
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
        # If you start returning INTENT kinds, you can customize response per intent here.
        entity = pr.matched_entity or "that"
        return f"Got it — {entity}."

