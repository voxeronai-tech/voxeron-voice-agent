from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

class PlanAction(str, Enum):
    REPLY = "reply"               # speak reply
    CLARIFY = "clarify"           # ask for missing slot
    UPDATE_CART = "update_cart"   # deterministic add/remove already applied
    HOTSWAP_TENANT = "hotswap_tenant"
    NOOP = "noop"                 # do nothing (e.g., incomplete utterance)
    END_CALL = "end_call"

@dataclass
class ResponsePlan:
    action: PlanAction = PlanAction.REPLY
    reply: str = ""
    lang: str = "en"

    # structured intent outputs (optional)
    hotswap_tenant_ref: Optional[str] = None

    # flow control / slots
    pending_choice: Optional[str] = None  # e.g. "nan_variant"
    pending_qty: int = 1

    # optional debug metadata
    debug: Dict[str, Any] = field(default_factory=dict)

