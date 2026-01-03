# src/api/text.py
from __future__ import annotations

import re

_WS_RX = re.compile(r"\s+")
_KEEP_RX = re.compile(r"[^a-z0-9\s]+")

def norm_text(s: str) -> str:
    """
    Normalizes menu strings for alias matching.
    - lowercases
    - removes punctuation
    - collapses whitespace
    """
    t = (s or "").strip().lower()
    t = _KEEP_RX.sub(" ", t)
    t = _WS_RX.sub(" ", t).strip()
    return t
