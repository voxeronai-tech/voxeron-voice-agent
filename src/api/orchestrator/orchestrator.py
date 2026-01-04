# src/api/orchestrator/orchestrator.py
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable, Dict, Optional

from .deterministic_parser import DeterministicParser
from .parser_types import ParserResult, ParserStatus


class OrchestratorRoute(str, Enum):
    DETERMINISTIC = "deterministic"
    AGENT = "agent"


@dataclass(frozen=True)
class OrchestratorDecision:
    route: OrchestratorRoute
    parser_result: ParserResult
    response_text: Optional[str] = None
    matched_entity: Optional[str] = None


class CognitiveOrchestrator:
    """
    Sprint-1 Orchestrator:
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
            txt = self._responder(pr)
            return OrchestratorDecision(
                route=OrchestratorRoute.DETERMINISTIC,
                parser_result=pr,
                response_text=txt,
                matched_entity=pr.matched_entity,
            )

        return OrchestratorDecision(
            route=OrchestratorRoute.AGENT,
            parser_result=pr,
            response_text=None,
            matched_entity=None,
        )

    @staticmethod
    def _default_responder(pr: ParserResult) -> str:
        entity = pr.matched_entity or "that"
        return f"Got it — {entity}."

