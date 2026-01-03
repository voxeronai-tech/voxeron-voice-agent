#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "🔧 Applying SessionController refactor (safe, idempotent)..."

# -----------------------------
# A) Write known-good policy.py (with 'wat is lekker/goed' support)
# -----------------------------
cat > src/api/policy.py <<'PY'
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple


# Regex-based follow-up detection (robust to filler words like "heel", "echt")
_NL_FOLLOWUP_RX = re.compile(
    r"(?i)\b("
    r"welke\s+(?:zijn|is)\s+(?:\w+\s+){0,3}?(?:lekker|goed|aan\s+te\s+raden|beste)\b"
    r"|wat\s+(?:is|zijn)\s+(?:\w+\s+){0,3}?(?:lekker|goed)\b"
    r"|wat\s+raad\s+je\s+aan\b"
    r"|welke\s+raad\s+je\s+aan\b"
    r"|welke\s+zou\s+je\s+kiezen\b"
    r"|aanraders\b"
    r")"
)

_EN_FOLLOWUP_RX = re.compile(
    r"(?i)\b("
    r"which\s+(?:are|is)\s+(?:\w+\s+){0,3}?(?:tasty|good|best)\b"
    r"|what\s+do\s+you\s+recommend\b"
    r"|which\s+do\s+you\s+recommend\b"
    r"|which\s+would\s+you\s+choose\b"
    r"|what'?s\s+the\s+best\b"
    r")"
)

# Simple phrase markers as a fallback (cheap + resilient)
_FOLLOWUP_MARKERS_NL = (
    "welke zijn lekker",
    "welke is lekker",
    "welke raad je aan",
    "wat raad je aan",
    "welke zou je kiezen",
    "welke zijn aan te raden",
    "welke is aan te raden",
    "aanraders",
    "beste keuze",
    "wat is lekker",
    "wat zijn lekker",
    "wat is goed",
    "wat zijn goed",
)
_FOLLOWUP_MARKERS_EN = (
    "which are tasty",
    "which is tasty",
    "which do you recommend",
    "what do you recommend",
    "which would you choose",
    "what's the best",
    "whats the best",
    "best one",
)


# -----------------------------
# Minimal state model (server can map to this)
# -----------------------------
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


# -----------------------------
# Cart tethering (prevents "empty cart hallucination")
# -----------------------------
def cart_summary(order: Order) -> str:
    if not order.items:
        return "Empty"
    parts = [f"{it.qty}x {it.name}" for it in order.items if it.qty > 0 and it.name]
    return ", ".join(parts) if parts else "Empty"


def system_guard_for_llm(state: SessionPolicyState) -> str:
    """
    Inject into EVERY LLM call.
    Makes it very hard for the LLM to claim the cart is empty when it's not.
    """
    summary = cart_summary(state.order)
    lang = (state.lang or "en").lower()

    if lang == "nl":
        return (
            "JE BENT EEN RESTAURANT-OBER.\n"
            f"HUIDIGE BESTELLING (SOURCE OF TRUTH): [{summary}]\n"
            "KRITIEKE REGEL: Als de bestelling NIET leeg is, mag je NOOIT zeggen dat er nog geen bestelling is geplaatst.\n"
            "Als de klant om een overzicht vraagt, herhaal exact de items uit HUIDIGE BESTELLING.\n"
        )

    return (
        "YOU ARE A RESTAURANT WAITER.\n"
        f"CURRENT CART (SOURCE OF TRUTH): [{summary}]\n"
        "CRITICAL RULE: If the cart is NOT empty, you must NEVER claim the user has not placed an order.\n"
        "If the user asks for the order summary, repeat the items exactly from CURRENT CART.\n"
    )


# -----------------------------
# Category stickiness (prevents drifting to general menu)
# -----------------------------
def is_followup_recommendation(text: str, lang: str) -> bool:
    """
    Detects: "What do you recommend?" / "Welke zijn lekker?" / "Wat is lekker?"
    Strategy:
      1) Regex match (robust)
      2) Marker substring fallback (cheap, resilient)
    """
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


def set_last_category(state: SessionPolicyState, category: str, items: Sequence[str]) -> None:
    state.last_category = (category or "").strip() or None
    state.last_category_items = [str(x).strip() for x in (items or []) if str(x).strip()]


def restricted_recommendation_pool(
    state: SessionPolicyState,
    user_text: str,
    full_menu_items: Sequence[str],
) -> Tuple[List[str], str]:
    """
    Returns (pool, reason)

    - If last_category is set AND user asks a follow-up recommendation:
        pool = last_category_items
    - Else:
        pool = full_menu_items
    """
    lang_n = (state.lang or "en").lower()
    if state.last_category and state.last_category_items and is_followup_recommendation(user_text, lang_n):
        return list(state.last_category_items), f"sticky:{state.last_category}"
    return [str(x) for x in (full_menu_items or [])], "general"


def sticky_guard_for_llm(state: SessionPolicyState, pool: Sequence[str], reason: str) -> str:
    """
    System instruction to force the LLM to recommend only from the allowed pool.
    """
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
PY

# -----------------------------
# B) Create engine/session_controller.py
# -----------------------------
mkdir -p src/api/engine
: > src/api/engine/__init__.py

cat > src/api/engine/session_controller.py <<'PY'
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
PY

# -----------------------------
# C) Patch server.py to wire controller in (idempotent)
# -----------------------------
python3 - <<'PY'
from pathlib import Path
import re

p = Path("src/api/server.py")
s = p.read_text(encoding="utf-8")

# 1) Ensure import
if "from .engine.session_controller import OrderingSession, SessionPhase" not in s:
    # insert after policy import block
    m = re.search(r"from\s+\.policy\s+import\s+\(\s*.*?\)\s*\n", s, flags=re.S)
    if not m:
        raise SystemExit("❌ Could not find policy import block in server.py to insert SessionController import.")
    insert_at = m.end()
    s = s[:insert_at] + "\nfrom .engine.session_controller import OrderingSession, SessionPhase\n" + s[insert_at:]

# 2) Wire controller creation in ws_pcm (after state.menu = snap)
if "state.session = OrderingSession(state)" not in s:
    anchor = "state.menu = snap  # type: ignore[attr-defined]"
    if anchor not in s:
        raise SystemExit("❌ Could not find state.menu assignment anchor in ws_pcm.")
    s = s.replace(
        anchor,
        anchor
        + "\n\n    # SessionController (data-driven orchestration)\n"
          "    try:\n"
          "        state.phase2 = SessionPhase.IDLE  # type: ignore[attr-defined]\n"
          "        state.session = OrderingSession(state)  # type: ignore[attr-defined]\n"
          "    except Exception:\n"
          "        pass\n"
    )

# 3) Mark transitions in process_utterance:
#    - when category list happens => MENU_PROVIDED
#    - when deterministic items added => CART_PENDING
# We patch in two places with safe inserts.

# 3a) After category request block sets menu_context for cat
# find the line where we assign state.last_category = cat (already present)
if "state.session.on_menu_provided()" not in s:
    s = re.sub(
        r"(state\.last_category\s*=\s*cat\s*\n\s*state\.last_category_items\s*=\s*cat_items\[:\]\s*\n)",
        r"\1                try:\n                    sess = getattr(state, 'session', None)\n                    if sess:\n                        sess.on_menu_provided()\n                except Exception:\n                    pass\n",
        s,
        count=1,
        flags=re.M
    )

# 3b) After deterministic adds: detect marker deterministic_post_add_reply, or parse_add_item block
# We'll insert a transition whenever adds were applied.
if "sess.on_cart_changed()" not in s:
    # If your file contains added_any in locals, use that. Otherwise use presence of "adds = parse_add_item"
    s = re.sub(
        r"(adds\s*=\s*parse_add_item\(state\.menu,\s*transcript\)\s*\n\s*for\s+item_id,\s*qty\s+in\s+adds:\s*\n\s*state\.order\.add\(item_id,\s*qty\)\s*\n)",
        r"\1\n        try:\n            if adds:\n                sess = getattr(state, 'session', None)\n                if sess:\n                    sess.on_cart_changed()\n        except Exception:\n            pass\n",
        s,
        count=1,
        flags=re.M
    )

# 4) Postprocess reply right before sending (kills silence, adds upsell prompts)
if "sess.postprocess_reply(" not in s:
    # Insert just before: reply = enforce_output_language(reply, state.lang)
    pat = r"(reply\s*=\s*enforce_output_language\(reply,\s*state\.lang\)\s*\n)"
    m = re.search(pat, s)
    if not m:
        raise SystemExit("❌ Could not find enforce_output_language(reply, state.lang) line to hook postprocess.")
    hook = (
        "        # SessionController postprocess: enforce momentum + basic upsell\n"
        "        try:\n"
        "            sess = getattr(state, 'session', None)\n"
        "            if sess:\n"
        "                reply = sess.postprocess_reply(transcript, reply, menu_context)\n"
        "        except Exception:\n"
        "            pass\n\n"
    )
    s = s[:m.start()] + hook + s[m.start():]

p.write_text(s, encoding="utf-8")
print(\"✅ Patched server.py: controller wired (imports, ws init, transitions, postprocess)\")
PY

echo "✅ Done. Now run:"
echo "  python -m py_compile src/api/policy.py"
echo "  python -m py_compile src/api/engine/session_controller.py"
echo "  python -m py_compile src/api/server.py"
echo "  pytest"
