#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Voxeron Quality Pack setup @ $ROOT"

# -----------------------------
# 0) Ensure we are in a venv
# -----------------------------
if [[ -z "${VIRTUAL_ENV:-}" ]]; then
  echo "❌ No venv detected. Activate your venv first:"
  echo "   source venv/bin/activate"
  exit 1
fi

# -----------------------------
# 1) Install python test deps (inside venv)
# -----------------------------
python -m pip install -U pip >/dev/null
python -m pip install -U pytest jsonschema >/dev/null
echo "✅ Installed pytest + jsonschema in venv"

# -----------------------------
# 2) Create test layout
# -----------------------------
mkdir -p tests/unit tests/integration tests/e2e
mkdir -p src/api

# -----------------------------
# 3) Make src importable for pytest
# -----------------------------
cat > tests/conftest.py <<'PY'
import sys
from pathlib import Path

# Add repo root so "import src...." works
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PY

cat > pytest.ini <<'INI'
[pytest]
addopts = -q
testpaths = tests
INI

# -----------------------------
# 4) Drop policy module: the "sticky memory + cart tethering guard"
# -----------------------------
cat > src/api/policy.py <<'PY'
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


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
        for it in self.items:
            if it.name.lower() == name_n.lower():
                it.qty += int(qty)
                return
        self.items.append(OrderItem(name=name_n, qty=int(qty)))


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
    This is the system guard string you inject into EVERY LLM call.
    The key: it makes it impossible (or at least very hard) for the LLM to claim an empty cart.
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
_FOLLOWUP_MARKERS_NL = (
    "welke zijn lekker",
    "welke is lekker",
    "welke raad je aan",
    "wat raad je aan",
    "welke zou je kiezen",
    "welke zijn aan te raden",
    "welke is aan te raden",
)
_FOLLOWUP_MARKERS_EN = (
    "which are tasty",
    "which is tasty",
    "which do you recommend",
    "what do you recommend",
    "which would you choose",
    "what's the best",
)


def is_followup_recommendation(text: str, lang: str) -> bool:
    t = (text or "").lower().strip()
    if not t:
        return False

    if (lang or "en").lower() == "nl":
        return any(m in t for m in _FOLLOWUP_MARKERS_NL)

    return any(m in t for m in _FOLLOWUP_MARKERS_EN)


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
    lang = (state.lang or "en").lower()
    if state.last_category and state.last_category_items and is_followup_recommendation(user_text, lang):
        return list(state.last_category_items), f"sticky:{state.last_category}"
    return [str(x) for x in (full_menu_items or [])], "general"


def sticky_guard_for_llm(state: SessionPolicyState, pool: Sequence[str], reason: str) -> str:
    """
    System instruction to force the LLM to recommend only from the allowed pool.
    """
    lang = (state.lang or "en").lower()
    items = ", ".join(pool[:30]) if pool else ""

    if lang == "nl":
        if reason.startswith("sticky:"):
            return (
                "FOLLOW-UP REGEL: De gebruiker stelt een vervolg-vraag binnen dezelfde categorie.\n"
                f"JE MAG ALLEEN aanbevelen uit deze lijst: [{items}]\n"
                "Noem geen gerechten buiten deze lijst.\n"
            )
        return (
            "AANBEVELING REGEL: Gebruik de beschikbare menu-lijst die je krijgt.\n"
        )

    if reason.startswith("sticky:"):
        return (
            "FOLLOW-UP RULE: The user is asking a follow-up within the same category.\n"
            f"YOU MAY ONLY recommend from this allowed list: [{items}]\n"
            "Do not mention items outside this list.\n"
        )

    return "RECOMMENDATION RULE: Use the available menu list provided.\n"
PY

# -----------------------------
# 5) Must-pass tests
# -----------------------------
cat > tests/unit/test_order_persistence.py <<'PY'
from src.api.policy import SessionPolicyState, system_guard_for_llm


def test_order_persistence_guard_never_claims_empty():
    state = SessionPolicyState(lang="nl")
    state.order.add("Butter Chicken", 2)
    state.order.add("Naan", 2)

    guard = system_guard_for_llm(state)

    assert "HUIDIGE BESTELLING" in guard
    assert "2x Butter Chicken" in guard
    assert "2x Naan" in guard
    # Hard constraint: must not allow "no order" narrative
    assert "NOOIT" in guard
PY

cat > tests/unit/test_category_stickiness.py <<'PY'
from src.api.policy import (
    SessionPolicyState,
    set_last_category,
    restricted_recommendation_pool,
    sticky_guard_for_llm,
)


def test_category_stickiness_followup_restricts_pool():
    state = SessionPolicyState(lang="nl")
    full_menu = [
        "Butter Chicken",
        "Vegetable Samosa",
        "Lamb Karahi",
        "Lamb Pasanda",
        "Chicken Biryani",
    ]

    # Turn 1: user asked for lamb category
    set_last_category(state, "Lamb", ["Lamb Karahi", "Lamb Pasanda"])

    # Turn 2: follow-up "which are tasty?"
    pool, reason = restricted_recommendation_pool(
        state=state,
        user_text="Oeh dat is heel veel. Welke zijn heel lekker?",
        full_menu_items=full_menu,
    )

    assert reason.startswith("sticky:")
    assert pool == ["Lamb Karahi", "Lamb Pasanda"]

    guard = sticky_guard_for_llm(state, pool, reason)
    assert "ALLEEN" in guard
    assert "Lamb Karahi" in guard
    assert "Lamb Pasanda" in guard
PY

# -----------------------------
# 6) Minimal integration placeholder (no external APIs)
# -----------------------------
cat > tests/integration/test_imports_smoke.py <<'PY'
def test_imports_smoke():
    # If this fails, your pythonpath/layout is broken
    import src.api.tenant_manager  # noqa: F401
    import src.api.policy  # noqa: F401
PY

echo
echo "✅ Quality Pack dropped."
echo "Run:"
echo "  pytest"
echo
echo "Must-pass:"
echo "  pytest tests/unit/test_order_persistence.py tests/unit/test_category_stickiness.py"
