from __future__ import annotations
from typing import Any
from .types import ResponsePlan
from .restaurant_engine import RestaurantEngine
from .dispatcher_engine import DispatcherEngine

class DomainRouter:
    def __init__(self):
        self.dispatcher = DispatcherEngine()
        self.restaurant = RestaurantEngine()

    def plan(self, state: Any, transcript: str) -> ResponsePlan:
        # If we're in dispatcher phase, route there
        if getattr(state, "phase", "") == "dispatcher":
            return self.dispatcher.plan(state, transcript)

        # Default (Taj demo): restaurant engine
        return self.restaurant.plan(state, transcript)

