from __future__ import annotations

from dataclasses import dataclass
from typing import List


@dataclass
class DisambiguationContext:
    parent_label: str
    options: List[str]
    qty: int = 1
    original_intent: str = "ADD_ITEM"

    @classmethod
    def from_dict(cls, data: dict) -> "DisambiguationContext":
        return cls(
            parent_label=str(data.get("parent_label", "item") or "item"),
            options=list(data.get("options", []) or []),
            qty=int(data.get("qty", 1) or 1),
            original_intent=str(data.get("original_intent", "ADD_ITEM") or "ADD_ITEM"),
        )
