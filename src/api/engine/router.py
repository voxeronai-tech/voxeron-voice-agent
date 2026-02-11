from __future__ import annotations

import logging
from typing import Any

from .types import ResponsePlan, PlanAction
from .restaurant_engine import RestaurantEngine
from .dispatcher_engine import DispatcherEngine

logger = logging.getLogger(__name__)


class DomainRouter:
    def __init__(self) -> None:
        self.dispatcher = DispatcherEngine()
        self.restaurant = RestaurantEngine()

    def plan(self, state: Any, transcript: str) -> ResponsePlan:
        phase = getattr(state, "phase", "") or ""

        # Dispatcher phase: ALWAYS dispatcher
        if phase == "dispatcher":
            p = self.dispatcher.plan(state, transcript)
            if p is None:
                logger.error("CRITICAL: DispatcherEngine.plan returned None (phase=%s)", phase)
                return ResponsePlan(action=PlanAction.NOOP, reply="", lang=getattr(state, "lang", "en") or "en")
            return p

        # Chat phase (default Taj demo): restaurant engine
        p = self.restaurant.plan(state, transcript)
        if p is None:
            logger.error("CRITICAL: RestaurantEngine.plan returned None (phase=%s)", phase)
            return ResponsePlan(action=PlanAction.NOOP, reply="", lang=getattr(state, "lang", "en") or "en")
        return p
