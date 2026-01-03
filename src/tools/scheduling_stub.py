# src/tools/scheduling_stub.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Dict, List


async def scheduling_free_busy(payload: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
    """
    Stub tool for demo/testing.
    Expected payload:
      { "window_hours": int, "priority": "P1|P2|P3", "constraints": {...} }
    Returns:
      { "status": "ok", "slots": [str, ...] }
    """
    window_hours = int(payload.get("window_hours", 24))
    now = datetime.utcnow()

    # Create 3 mock slots inside the requested window
    candidate_hours = [2, 6, 24, 48, 72]
    slots: List[str] = []
    for h in candidate_hours:
        if h <= window_hours:
            start = now + timedelta(hours=h)
            slots.append(start.strftime("%Y-%m-%d %H:%M UTC"))

    return {"status": "ok", "slots": slots}

