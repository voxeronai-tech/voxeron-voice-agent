# src/api/intent.py
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional, Set, List


@dataclass(frozen=True)
class LangDecision:
    target: Optional[str]          # "en" | "nl" | None
    confidence: str                # "high" | "med" | "low"
    reason: str                    # human readable for logs
    explicit: bool                 # explicit vs implicit


def norm_simple(s: str) -> str:
    """
    Lowercase + remove punctuation => spaces + collapse whitespace.
    Good for intent detection.
    """
    s = (s or "").lower()
    cleaned = []
    for ch in s:
        cleaned.append(ch if (ch.isalnum() or ch.isspace()) else " ")
    return " ".join("".join(cleaned).split()).strip()


def contains_devanagari(s: str) -> bool:
    return any("\u0900" <= ch <= "\u097F" for ch in (s or ""))


# -------------------------
# Cheap deterministic intents (demo safe)
# -------------------------

# Order summary, cart overview
_NL_ORDER_SUMMARY_MARKERS: List[str] = [
    "mijn bestelling",
    "wat heb ik besteld",
    "wat is mijn bestelling",
    "welk gerecht heb ik besteld",
    "wat heb ik tot nu toe besteld",
    "overzicht",
    "samenvatting",
    "mandje",
    "winkelmandje",
]
_EN_ORDER_SUMMARY_MARKERS: List[str] = [
    "my order",
    "order summary",
    "what did i order",
    "what have i ordered",
    "what is my order",
    "cart",
    "basket",
    "overview",
]

# Category request and discovery
_NL_MENU_CUES = ("menu", "op het menu", "hebben jullie", "wat hebben jullie", "welke", "wat zijn")
_EN_MENU_CUES = ("menu", "what do you have", "which", "what are", "do you have")

# Negative intent triggers, used to prevent false additions
_NEG_TRIGGERS = (
    "geen",
    "niet",
    "zonder",
    "liever niet",
    "wil ik niet",
    "dont",
    "don't",
    "do not",
    "no",
    "remove",
    "cancel",
)

# More paging
_MORE_MARKERS_NL = ("nog meer", "meer", "meer opties", "kun je meer", "wat nog meer", "meer noemen")
_MORE_MARKERS_EN = ("more", "more options", "what else", "list more", "anything else")


def detect_order_summary_intent(text: str, lang: str) -> bool:
    t = norm_simple(text)
    if not t:
        return False
    lang_n = (lang or "en").lower()
    if lang_n == "nl":
        return any(m in t for m in _NL_ORDER_SUMMARY_MARKERS)
    return any(m in t for m in _EN_ORDER_SUMMARY_MARKERS)


def detect_more_intent(text: str, lang: str) -> bool:
    t = norm_simple(text)
    if not t:
        return False
    lang_n = (lang or "en").lower()
    if lang_n == "nl":
        return any(m in t for m in _MORE_MARKERS_NL)
    return any(m in t for m in _MORE_MARKERS_EN)


def detect_negative_intent(text: str) -> bool:
    """
    Conservative: if any negative trigger appears anywhere, return True.
    SessionController should then avoid deterministic add and let LLM handle it
    or route to remove logic if you implement it.
    """
    t = norm_simple(text)
    if not t:
        return False
    return any(m in t for m in _NEG_TRIGGERS)


def detect_explicit_remove_intent(text: str, lang: str) -> bool:
    """
    True only when the user explicitly asked to remove/cancel something.
    This is stricter than detect_negative_intent().
    """
    t = norm_simple(text)
    if not t:
        return False

    lang_n = (lang or "en").lower()

    if lang_n == "nl":
        remove_markers = (
            "verwijder",
            "haal weg",
            "schrap",
            "annuleer",
            "niet meer",
            "laat maar",
            "kan weg",
            "eraf",
            "minus",
        )
    else:
        remove_markers = (
            "remove",
            "cancel",
            "delete",
            "take off",
            "drop",
            "minus",
            "no longer",
        )

    return any(m in t for m in remove_markers)


def detect_generic_nan_request(text: str) -> bool:
    """
    Detects a generic request for nan/naan/naam without specifying a subtype.
    STT safe: catches nan/naan/naam.
    """
    t = norm_simple(text)
    if not t:
        return False

    toks = t.split()

    # must contain nan/naan/naam token-ish
    has_nan = any(k in toks for k in ("nan", "naan", "naam"))
    if not has_nan:
        return False

    # if user already specified a variant, it's NOT generic
    variant_markers = ("garlic", "knoflook", "cheese", "kaas", "keema", "peshawari")
    if any(v in t for v in variant_markers):
        return False

    return True


def detect_category_request(text: str) -> Optional[str]:
    """
    Very cheap category detection. Returns canonical category key or None.
    Keep conservative, no ML, just token cues.
    """
    t = norm_simple(text)
    if not t:
        return None

    # A cue that user is asking discovery or menu question
    cue = any(c in t for c in _NL_MENU_CUES) or any(c in t for c in _EN_MENU_CUES)
    if not cue:
        cue = True if any(k in t for k in ("lam", "lamb", "kip", "chicken", "biryani", "vega", "vegetar", "vegetarian", "paneer")) else False
    if not cue:
        return None

    if any(k in t for k in ("lam", "lamb")):
        return "lamb"
    if any(k in t for k in ("kip", "chicken")):
        return "chicken"
    if "biryani" in t:
        return "biryani"
    if any(k in t for k in ("vega", "vegetar", "vegetarian", "paneer")):
        return "vegetarian"
    return None


# -------------------------
# Language switching (INERTIA)
# -------------------------

def infer_user_language(text: str) -> Optional[str]:
    """
    Implicit language markers. Keep conservative.
    EN/NL-only policy: we only infer Dutch implicitly during language_select.
    """
    if not text:
        return None

    t = norm_simple(text)
    if not t:
        return None
    toks = set(t.split())

    dutch_markers = {"ik", "wil", "graag", "alsjeblieft", "alstublieft", "twee", "geen", "maar", "met", "zonder", "en"}

    if len(toks & dutch_markers) >= 3:
        return "nl"

    return None


def detect_language_intent(
    transcript: str,
    *,
    phase: str,
    current_lang: str,
    allow_auto_detect: bool = True,
) -> LangDecision:
    """
    Single source of truth for language switching.

    Policy:
      - explicit switches allowed ANYTIME (EN/NL only)
      - implicit switches allowed ONLY during language_select (EN/NL only)

    Important:
      - Do NOT flip language in-chat based on single tokens like "ja".
      - Hindi is disabled.
    """
    raw = transcript or ""
    t = norm_simple(raw)
    if not t:
        return LangDecision(None, "low", "empty transcript", False)

    toks = t.split()
    tokset: Set[str] = set(toks)

    DUTCH_TOKENS = {"nederlands", "dutch", "nederland", "nederlandse", "nederlandsche", "netherlands"}
    EN_TOKENS = {"english", "engels"}

    INTENT_WORDS = {
        "switch", "naar", "spreek", "speak", "taal", "language", "in",
        "wil", "want", "liever", "prefer", "please", "alsjeblieft", "alstublieft",
        "doen", "kan", "kunnen",
    }

    def looks_like_language_pick() -> bool:
        if len(toks) <= 5:
            return True
        if "switch" in tokset or "naar" in tokset:
            return True
        if (tokset & INTENT_WORDS) and (tokset & (DUTCH_TOKENS | EN_TOKENS)):
            return True
        if "in" in tokset and (tokset & (DUTCH_TOKENS | EN_TOKENS)):
            return True
        return False

    # Hindi hard-disabled
    if contains_devanagari(raw) and looks_like_language_pick():
        return LangDecision(None, "high", "Devanagari detected but Hindi is disabled", True)

    # Explicit token-based picks
    if (tokset & DUTCH_TOKENS) and looks_like_language_pick():
        return LangDecision("nl", "high", "Dutch token + pick intent", True)
    if (tokset & EN_TOKENS) and looks_like_language_pick():
        return LangDecision("en", "high", "English token + pick intent", True)

    # Implicit detection (ONLY at language_select)
    if allow_auto_detect and phase == "language_select":
        imp = infer_user_language(raw)
        if imp:
            return LangDecision(imp, "med", "implicit language markers during language_select", False)

    return LangDecision(None, "low", "no language intent detected", False)

