from __future__ import annotations

import re
from typing import Optional

# MVP scope: 1..10 only
_EN = {
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}

_NL = {
    "een": 1,
    "één": 1,
    "twee": 2,
    "drie": 3,
    "vier": 4,
    "vijf": 5,
    "zes": 6,
    "zeven": 7,
    "acht": 8,
    "negen": 9,
    "tien": 10,
}


def extract_quantity_1_to_10(text: str) -> Optional[int]:
    """
    Deterministically extract a quantity in the range 1..10 from free text.

    Rules (MVP):
    - Accept digits 1..10 as standalone tokens.
    - Accept EN words one..ten and NL words een/één..tien.
    - Reject 0, 11+, and composite numbers.
    - Return the first unambiguous match, else None.
    """
    if not text:
        return None

    t = _norm(text)

    # Digits first (strict token boundary)
    m = re.search(r"\b(10|[1-9])\b", t)
    if m:
        val = int(m.group(1))
        return val if 1 <= val <= 10 else None

    # Word tokens
    for tok in t.split():
        if tok in _EN:
            return _EN[tok]
        if tok in _NL:
            return _NL[tok]

    return None


def _norm(text: str) -> str:
    # lowercase + strip punctuation to spaces (keep unicode letters like é)
    t = text.lower().strip()
    t = re.sub(r"[^\w\sé]", " ", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t
