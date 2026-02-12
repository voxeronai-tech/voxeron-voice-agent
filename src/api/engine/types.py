# src/api/engine/types.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class PlanAction(str, Enum):
    """What the engine wants the controller to do next."""
    REPLY = "reply"                 # speak reply
    CLARIFY = "clarify"             # ask for missing slot / choice
    UPDATE_CART = "update_cart"     # cart mutation already applied deterministically
    HOTSWAP_TENANT = "hotswap_tenant"
    NOOP = "noop"                   # do nothing (e.g., incomplete utterance)
    END_CALL = "end_call"


@dataclass(frozen=True)
class ChoiceResolution:
    """
    Generic choice resolution payload.

    The controller must not interpret semantics here.
    It only forwards this payload to an engine-owned applier (or router),
    and clears the pending gate.
    """
    key: str                        # e.g. "nan_variant" (engine-defined)
    value: str                      # e.g. "plain" | "keema" | "cancel"
    qty: int = 1
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CartOp:
    """
    Generic cart operation. Controller stays menu-blind.

    op:
      - "add"     => add qty to item_id
      - "set_qty" => set qty for item_id (qty<=0 removes)
    """
    op: str                         # "add" | "set_qty"
    item_id: str
    qty: int = 1
    meta: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ResponsePlan:
    action: PlanAction = PlanAction.REPLY
    reply: str = ""
    lang: str = "en"

    # structured intent outputs (optional)
    hotswap_tenant_ref: Optional[str] = None

    # flow control / slots
    pending_choice: Optional[str] = None   # engine-defined, e.g. "nan_variant"
    pending_qty: int = 1

    # Generic cart operations (controller stays menu-blind)
    cart_ops: List[CartOp] = field(default_factory=list)

    # If set, controller should apply via engine/applier and clear pending_choice
    resolved_choice: Optional[ChoiceResolution] = None

    # optional debug metadata
    debug: Dict[str, Any] = field(default_factory=dict)
    consumed: bool = False
