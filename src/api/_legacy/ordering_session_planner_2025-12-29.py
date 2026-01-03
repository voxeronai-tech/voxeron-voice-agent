from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..policy import SessionPolicyState, restricted_recommendation_pool, is_followup_recommendation


class SessionPhase(str, Enum):
    IDLE = "idle"
    MENU_PROVIDED = "menu_provided"
    CART_PENDING = "cart_pending"
    UPSELL_OFFERED = "upsell_offered"


@dataclass
class ResponsePlan:
    reply: str
    phase: SessionPhase
    debug: Dict[str, Any] = None


_ACK_NL = {"ok", "oke", "oké", "prima", "goed", "top", "ja", "hallo"}
_ACK_EN = {"ok", "okay", "sure", "yes", "hi"}


def _contains_question(text: str) -> bool:
    t = (text or "").strip()
    return "?" in t or (t.lower().startswith(("wil je", "would you", "do you", "kan ik", "shall i")))


def _cart_item_names_lower(state: Any) -> List[str]:
    """
    Extract display names from state.order (item_id->qty) using state.menu.display_name.
    """
    out: List[str] = []
    menu = getattr(state, "menu", None)
    order = getattr(state, "order", None)
    items = getattr(order, "items", None)
    if not menu or not isinstance(items, dict):
        return out
    for item_id, qty in items.items():
        if int(qty or 0) <= 0:
            continue
        try:
            nm = menu.display_name(item_id)
        except Exception:
            nm = str(item_id)
        if nm:
            out.append(nm.lower())
    return out


def _menu_items_from_context(menu_context: str) -> List[str]:
    """
    MENU_CONTEXT is built like:
      - Item A
      - Item B
    """
    items: List[str] = []
    for line in (menu_context or "").splitlines():
        line = line.strip()
        if line.startswith("- "):
            items.append(line[2:].strip())
    return [x for x in items if x]


def _pick_simple_upsell(menu_items: Sequence[str], cart_lower: Sequence[str], lang: str) -> List[str]:
    """
    Very simple upsell: prefer rice/naan/drinks/dessert not already in cart.
    """
    prefer = ["rice", "rijst", "naan", "garlic", "knoflook", "lassi", "mango", "kulfi", "dessert", "bier", "beer"]
    picked: List[str] = []

    def ok_item(x: str) -> bool:
        xl = x.lower()
        if any(c in xl for c in cart_lower):
            return False
        return True

    for p in prefer:
        for it in menu_items:
            if len(picked) >= 3:
                break
            if p in it.lower() and ok_item(it) and it not in picked:
                picked.append(it)
        if len(picked) >= 3:
            break

    # fallback: any 3 not in cart
    if len(picked) < 3:
        for it in menu_items:
            if len(picked) >= 3:
                break
            if ok_item(it) and it not in picked:
                picked.append(it)

    return picked[:3]


class OrderingSession:
    """
    Deterministic flow controller.
    - Kills 'menu dump then silence' by enforcing follow-up questions.
    - Provides minimal upsell nudges.
    - Keeps LLM as phrasing engine, not flow control.
    """

    def __init__(self, state: Any):
        self.state = state
        if not getattr(self.state, "phase2", None):
            self.state.phase2 = SessionPhase.IDLE

    def on_menu_provided(self) -> None:
        self.state.phase2 = SessionPhase.MENU_PROVIDED

    def on_cart_changed(self) -> None:
        self.state.phase2 = SessionPhase.CART_PENDING

    def postprocess_reply(self, user_text: str, reply: str, menu_context: str) -> str:
        lang = (getattr(self.state, "lang", "en") or "en").lower()
        t = (user_text or "").strip().lower()
        r = (reply or "").strip()

        # 1) If we just provided a menu list, ALWAYS end with a question + keep it short.
        if self.state.phase2 == SessionPhase.MENU_PROVIDED:
            if not _contains_question(r):
                if lang == "nl":
                    r = r.rstrip(". ")
                    r += ". Welke spreekt je het meest aan? Als je wilt kan ik je top 3 aanraden, mild of pittig?"
                else:
                    r = r.rstrip(". ")
                    r += ". Which one sounds best to you? If you want, I can recommend a top 3, mild or spicy?"

        # 2) If user says a weak acknowledgement after a menu, nudge selection.
        if self.state.phase2 == SessionPhase.MENU_PROVIDED:
            if (lang == "nl" and t in _ACK_NL) or (lang != "nl" and t in _ACK_EN):
                if lang == "nl":
                    return "Top. Welke van die opties wil je proberen? Als je wilt kan ik je top 3 aanraden."
                return "Great. Which one would you like? If you want, I can suggest a top 3."

        # 3) If user asks “wat is lekker?” and we have a last category pool, enforce sticky pool deterministically.
        # We do NOT trust the LLM to pick correctly in this case.
        try:
            ps = SessionPolicyState(lang=lang)
            ps.last_category = getattr(self.state, "last_category", None)
            ps.last_category_items = list(getattr(self.state, "last_category_items", []) or [])
            full = _menu_items_from_context(menu_context)
            pool, reason = restricted_recommendation_pool(ps, user_text, full)
            if reason.startswith("sticky:") and is_followup_recommendation(user_text, lang):
                cart_lower = _cart_item_names_lower(self.state)
                picks = [x for x in pool if x.lower() not in cart_lower][:3]
                if picks:
                    if lang == "nl":
                        return f"Als aanrader uit deze categorie: {', '.join(picks)}. Welke zal ik voor je toevoegen?"
                    return f"My top picks from this category: {', '.join(picks)}. Which one should I add?"
        except Exception:
            pass

        # 4) After cart pending: if user is vague (oké/ja), upsell deterministically.
        if self.state.phase2 == SessionPhase.CART_PENDING:
            if (lang == "nl" and t in _ACK_NL) or (lang != "nl" and t in _ACK_EN):
                items = _menu_items_from_context(menu_context)
                cart_lower = _cart_item_names_lower(self.state)
                upsell = _pick_simple_upsell(items, cart_lower, lang)
                if upsell:
                    if lang == "nl":
                        return f"Top. Wil je er iets bij, bijvoorbeeld: {', '.join(upsell)}?"
                    return f"Nice. Would you like to add something, for example: {', '.join(upsell)}?"

        return r
