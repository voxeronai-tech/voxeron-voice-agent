"""
Facade for GitHub issue #3 path expectation.

Keep orchestrator imports stable while allowing:
  from src.api.parser.deterministic_parser import DeterministicParser
"""
from __future__ import annotations

from src.api.orchestrator.deterministic_parser import DeterministicParser

__all__ = ["DeterministicParser"]
