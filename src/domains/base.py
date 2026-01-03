# src/domains/base.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Protocol


# -----------------------------
# Core Types
# -----------------------------

class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"


class CaseStatus(str, Enum):
    OPEN = "open"
    ESCALATED = "escalated"
    CALLBACK_REQUESTED = "callback_requested"
    SCHEDULED = "scheduled"
    CLOSED = "closed"


class ActionType(str, Enum):
    SET_PRIORITY = "SET_PRIORITY"
    SET_STATUS = "SET_STATUS"
    SAVE_TRIAGE_FACTS = "SAVE_TRIAGE_FACTS"
    OVERRIDE_REPLY = "OVERRIDE_REPLY"
    REQUEST_DISPATCHER_CALLBACK = "REQUEST_DISPATCHER_CALLBACK"
    REQUEST_TOOL = "REQUEST_TOOL"
    REPLY_TEMPLATE = "REPLY_TEMPLATE"


@dataclass(frozen=True)
class IntentFrame:
    """
    Output of Interpret phase.
    Keep it domain-agnostic, domains can place extra keys in `meta`.
    """
    lang: str                    # "nl" | "en" | "tr"
    utterance: str               # normalized text
    dispatcher_intent: bool = False
    emergency_flags: Dict[str, bool] = field(default_factory=dict)
    confidence: float = 0.85
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DomainAction:
    """
    Output of Decide phase.
    Apply phase is platform-owned.
    """
    type: ActionType
    payload: Dict[str, Any] = field(default_factory=dict)


@dataclass
class RenderPlan:
    """
    Optional structured render output (if you don't want free-form strings).
    Platform can decide how to use this.
    """
    mode: str  # e.g. "template", "override", "text"
    lang: str
    text: Optional[str] = None
    template: Optional[str] = None
    data: Dict[str, Any] = field(default_factory=dict)


# -----------------------------
# BaseDomain Contract (ADR-001)
# -----------------------------

class BaseDomain(Protocol):
    """
    Domains implement Interpret -> Decide.
    Apply + Render are platform-owned.
    Render() is optional here; many platforms keep rendering outside the domain.
    """
    domain_type: str

    def interpret(self, text: str, ctx: Dict[str, Any]) -> IntentFrame:
        ...

    def decide(self, frame: IntentFrame, case_state: Dict[str, Any]) -> List[DomainAction]:
        ...

