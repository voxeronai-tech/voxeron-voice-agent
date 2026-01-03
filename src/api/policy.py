# src/api/policy.py
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


_NL_FOLLOWUP_RX = re.compile(
    r"(?i)\b("
    r"welke\s+(?:zijn|is)\s+(?:\w+\s+){0,4}?(?:lekker|lekkerste|goed|aan\s+te\s+raden|beste|beste\s+keuze)\b"
    r"|wat\s+(?:is|zijn)\s+(?:\w+\s+){0,4}?(?:lekker|lekkerste|goed|beste)\b"
    r"|wat\s+raad\s+je\s+aan\b"
    r"|welke\s+raad\s+je\s+aan\b"
    r"|welke\s+zou\s+je\s+kiezen\b"
    r"|wat\s+zou\s+jij\s+kiezen\b"
    r"|aanrader(?:s)?\b"
    r"|favoriet(?:en)?\b"
    r"|beste\s+optie\b"
    r"|top\s*\d+\b"
    r")"
)

_EN_FOLLOWUP_RX = re.compile(
    r"(?i)\b("
    r"which\s+(?:are|is)\s+(?:\w+\s+){0,4}?(?:tasty|good|best)\b"
    r"|what\s+do\s+you\s+recommend\b"
    r"|which\s+do\s+you\s+recommend\b"
    r"|which\s+would\s+you\s+choose\b"
    r"|what\s+would\s+you\s+choose\b"
    r"|what'?s\s+the\s+best\b"
    r"|best\s+option\b"
    r"|top\s*\d+\b"
    r"|any\s+favorites?\b"
    r")"
)

_FOLLOWUP_MARKERS_NL = (
    "welke zijn lekker",
    "welke is lekker",
    "welke raad je aan",
    "wat raad je aan",
    "welke zou je kiezen",
    "wat zou jij kiezen",
    "welke zijn aan te raden",
    "welke is aan te raden",
    "aanrader",
    "aanraders",
    "favoriet",
    "favorieten",
    "beste keuze",
    "beste optie",
    "wat is lekker",
    "wat zijn lekker",
    "wat is goed",
    "wat zijn goed",
    "lekkerste",
    "top 3",
    "top3",
    "top 5",
    "top5",
)
_FOLLOWUP_MARKERS_EN = (
    "which are tasty",
    "which is tasty",
    "which do you recommend",
    "what do you recommend",
    "which would you choose",
    "what would you choose",
    "what's the best",
    "whats the best",
    "best one",
    "best option",
    "top 3",
    "top3",
    "top 5",
    "top5",
    "any favorite",
    "any favorites",
)

_MORE_MARKERS_NL = (
    "meer",
    "nog meer",
    "meer opties",
    "wat nog meer",
    "kun je meer noemen",
)
_MORE_MARKERS_EN = (
    "more",
    "more options",
    "what else",
    "list more",
)


@dataclass
class OrderItem:
    name: str
    qty: int = 1


@dataclass
class Order:
    items: List[OrderItem] = field(default_factory=list)

    def add(self, name: str, qty: int = 1) -> None:
        name_n = (name or "").strip()
        if not name_n:
            return
        q = int(qty) if qty is not None else 1
        for it in self.items:
            if it.name.lower() == name_n.lower():
                it.qty += q
                return
        self.items.append(OrderItem(name=name_n, qty=q))


@dataclass
class SessionPolicyState:
    lang: str = "en"
    order: Order = field(default_factory=Order)
    last_category: Optional[str] = None
    last_category_items: List[str] = field(default_factory=list)


def cart_summary(order: Order) -> str:
    if not order.items:
        return "Empty"
    parts = [f"{it.qty}x {it.name}" for it in order.items if it.qty > 0 and it.name]
    return ", ".join(parts) if parts else "Empty"


def nan_variant_question(lang: str, verbose: bool = True) -> str:
    """
    Default to Optima-style minimal choice.
    If user explicitly asks for options, controller can call its dynamic list-mode.
    """
    lang_n = (lang or "en").lower()

    if lang_n == "nl":
        if verbose:
            # minimal, mature flow
            return "Zeker. Wil je gewone naan of garlic naan?"
        return "Gewone naan of garlic naan?"

    if verbose:
        return "Certainly. Would you like plain naan or garlic naan?"
    return "Plain naan or garlic naan?"


def post_cart_followup(lang: str, summary: str, last_added_hint: Optional[str] = None) -> str:
    """
    Optima-style: don't constantly repeat the cart.
    Use short next-step prompts.

    last_added_hint:
      - "main": user likely added a main dish -> suggest rice/naan
      - "naan": user added naan -> suggest sides/starters
      - None: generic
    """
    lang_n = (lang or "en").lower()
    hint = (last_added_hint or "").strip().lower()

    if lang_n == "nl":
        if hint == "main":
            return "Wil je daar rijst of naan bij?"
        if hint == "naan":
            return "Wil je er nog iets bij, bijvoorbeeld rijst, dahl, of een samosa?"
        return "Wil je nog iets toevoegen of wijzigen?"

    if hint == "main":
        return "Would you like rice or naan with that?"
    if hint == "naan":
        return "Would you like anything else, for example rice, dal, or a samosa?"
    return "Anything else you'd like to add?"


def system_guard_for_llm(state: SessionPolicyState) -> str:
    """
    IMPORTANT: This guard used to force repeating the cart after every change.
    That conflicts with the Optima-style flow (less cart-pushy).

    New rule: confirm briefly; only repeat the full cart when user asks,
    or when there's ambiguity that needs confirmation.
    """
    summary = cart_summary(state.order)
    lang = (state.lang or "en").lower()

    if lang == "nl":
        return (
            "JE BENT EEN RESTAURANT-OBER.\n"
            f"HUIDIGE BESTELLING (SOURCE OF TRUTH): [{summary}]\n"
            "KRITIEKE REGELS:\n"
            "1) Als de bestelling NIET leeg is, mag je NOOIT zeggen dat er nog geen bestelling is geplaatst.\n"
            "2) Verwijder NOOIT items uit de bestelling tenzij de gebruiker EXPLICIET vraagt om te verwijderen/annuleren/schrappen.\n"
            "3) Bevestig wijzigingen kort en ga door met de volgende logische vraag (bijv. pittigheid, afhalen/bezorgen, naam).\n"
            "   Herhaal de VOLLEDIGE bestelling alleen als de klant erom vraagt, of als je iets moet verifiëren.\n"
            "Als de klant om een overzicht vraagt, herhaal exact de items uit HUIDIGE BESTELLING.\n"
        )

    return (
        "YOU ARE A RESTAURANT WAITER.\n"
        f"CURRENT CART (SOURCE OF TRUTH): [{summary}]\n"
        "CRITICAL RULES:\n"
        "1) If the cart is NOT empty, you must NEVER claim the user has not placed an order.\n"
        "2) You must NEVER suggest removing items unless the user explicitly asked to remove/cancel/delete.\n"
        "3) Confirm changes briefly and move to the next logical question (e.g. spice level, pickup/delivery, name).\n"
        "   Only repeat the FULL cart if the user asks for it, or if you must verify ambiguity.\n"
        "If the user asks for the order summary, repeat the items exactly from CURRENT CART.\n"
    )


def is_followup_recommendation(text: str, lang: str) -> bool:
    t = (text or "").strip()
    if not t:
        return False

    lang_n = (lang or "en").lower()
    t_lc = t.lower()

    if lang_n == "nl":
        if _NL_FOLLOWUP_RX.search(t):
            return True
        return any(m in t_lc for m in _FOLLOWUP_MARKERS_NL)

    if _EN_FOLLOWUP_RX.search(t):
        return True
    return any(m in t_lc for m in _FOLLOWUP_MARKERS_EN)


def is_more_request(text: str, lang: str) -> bool:
    t = (text or "").strip().lower()
    if not t:
        return False
    if (lang or "en").lower() == "nl":
        return any(m in t for m in _MORE_MARKERS_NL)
    return any(m in t for m in _MORE_MARKERS_EN)


def set_last_category(state: SessionPolicyState, category: str, items: Sequence[str]) -> None:
    state.last_category = (category or "").strip() or None
    state.last_category_items = [str(x).strip() for x in (items or []) if str(x).strip()]


def restricted_recommendation_pool(
    state: SessionPolicyState,
    user_text: str,
    full_menu_items: Sequence[str],
) -> Tuple[List[str], str]:
    lang_n = (state.lang or "en").lower()
    if state.last_category and state.last_category_items and is_followup_recommendation(user_text, lang_n):
        return list(state.last_category_items), f"sticky:{state.last_category}"
    return [str(x) for x in (full_menu_items or [])], "general"


def sticky_guard_for_llm(state: SessionPolicyState, pool: Sequence[str], reason: str) -> str:
    lang = (state.lang or "en").lower()
    items = ", ".join([str(x) for x in (pool or [])][:30]) if pool else ""

    if lang == "nl":
        if reason.startswith("sticky:"):
            return (
                "FOLLOW-UP REGEL: De gebruiker stelt een vervolg-vraag binnen dezelfde categorie.\n"
                f"JE MAG ALLEEN aanbevelen uit deze lijst: [{items}]\n"
                "Noem geen gerechten buiten deze lijst.\n"
            )
        return "AANBEVELING REGEL: Gebruik de beschikbare menu-lijst die je krijgt.\n"

    if reason.startswith("sticky:"):
        return (
            "FOLLOW-UP RULE: The user is asking a follow-up within the same category.\n"
            f"YOU MAY ONLY recommend from this allowed list: [{items}]\n"
            "Do not mention items outside this list.\n"
        )

    return "RECOMMENDATION RULE: Use the available menu list provided.\n"

