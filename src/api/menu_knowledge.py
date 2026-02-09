from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .intent import norm_simple
from .menu_store import MenuSnapshot


# -----------------------------------------------------------------------------
# NOTE (architecture)
# -----------------------------------------------------------------------------
# This module is the right place for *menu semantics* and lightweight heuristics.
# Keep it tenant-agnostic:
# - No tenant names, no dish-specific business logic tied to a single restaurant.
# - Use generic "traits" (protein category, spice preference) + menu scanning.
#
# If you later add structured metadata to MenuSnapshot (tags/attributes),
# this file can prefer metadata and fall back to heuristics safely.
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class TraitQuery:
    """
    Parsed high-level intent, used for suggestion or follow-up prompts.

    protein:
      A coarse category (e.g., "lamb", "chicken", "vegetarian") that can be mapped
      to menu attributes or heuristic matches.
    wants_spicy:
      Whether user preference implies spicy/hot.
    raw:
      Normalized user text used for debugging/tracing.
    """
    protein: Optional[str] = None
    wants_spicy: bool = False
    raw: str = ""


# Keep lists short and generic. Avoid tenant-specific dish names.
_SPICY_MARKERS: Tuple[str, ...] = (
    "spicy", "hot", "very spicy", "extra spicy",
    "heet", "pittig", "heel heet", "erg pittig",
)

# "Styles" that often imply heat. Still reasonably generic across Indian menus.
_SPICY_STYLE_MARKERS: Tuple[str, ...] = (
    "madras", "vindaloo", "phall",
)

# Protein categories and their text markers.
# Keep these broad; don't hard-code specific menu item names.
_PROTEIN_MARKERS = {
    "lamb": ("lamb", "lam", "lams"),
    "chicken": ("chicken", "kip"),
    "vegetarian": ("vegetarian", "vegetarisch", "vega", "veg", "paneer"),
}


def extract_traits(text: str) -> TraitQuery:
    t = norm_simple(text)
    if not t:
        return TraitQuery(raw="")

    wants_spicy = any(w in t for w in _SPICY_MARKERS) or any(w in t for w in _SPICY_STYLE_MARKERS)

    protein: Optional[str] = None
    for key, markers in _PROTEIN_MARKERS.items():
        if any(m in t for m in markers):
            protein = key
            break

    return TraitQuery(protein=protein, wants_spicy=wants_spicy, raw=t)


def _dedup_keep_order(xs: Iterable[str], *, limit: int) -> List[str]:
    seen = set()
    out: List[str] = []
    for x in xs:
        if not x:
            continue
        if x in seen:
            continue
        seen.add(x)
        out.append(x)
        if len(out) >= limit:
            break
    return out

# -----------------------------------------------------------------------------
# Generic variant extraction (tenant-agnostic)
# -----------------------------------------------------------------------------
def extract_naan_variant_keyword_scoped(text: str) -> Optional[str]:
    """
    Extract a generic variant keyword (garlic/plain/cheese/etc.) from free text.
    Tenant-agnostic and menu-agnostic: this only detects the variant intent.
    """
    t = norm_simple(text)
    if not t:
        return None

    # Highest-signal variants first
    if ("garlic" in t) or ("knoflook" in t):
        return "garlic"
    if ("cheese" in t) or ("kaas" in t):
        return "cheese"
    if ("butter" in t) or ("boter" in t):
        return "butter"
    if ("keema" in t) or ("kheema" in t):
        return "keema"
    if "peshawari" in t:
        return "peshawari"

    # Default/plain signals (keep broad)
    if any(x in t for x in ("plain", "regular", "normal", "gewoon", "normaal", "standaard")):
        return "plain"

    return None



def list_items_for_protein(
    menu: MenuSnapshot,
    protein: str,
    *,
    limit: int = 80,
) -> List[str]:
    """
    Heuristic fallback if menu metadata is missing.

    Returns display names (not item_ids) because callers use this for suggestions only.
    """
    if not menu or not protein:
        return []

    protein_key = protein.strip().lower()
    markers: Sequence[str] = _PROTEIN_MARKERS.get(protein_key, ())
    if not markers:
        return []

    hits: List[str] = []
    for _norm_name, iid in menu.name_choices:
        try:
            dn = (menu.display_name(iid) or "").strip()
        except Exception:
            continue
        if not dn:
            continue

        ldn = dn.lower()
        if any(m in ldn for m in markers):
            hits.append(dn)

    return _dedup_keep_order(hits, limit=limit)


def _spice_score(name: str) -> int:
    """
    Deterministic keyword-based heat proxy.
    Used only for ranking suggestions when user asks for spicy.
    """
    n = (name or "").lower()
    score = 0
    if "phall" in n:
        score += 100
    if "vindaloo" in n:
        score += 80
    if "madras" in n:
        score += 70
    if "jalfrezi" in n:
        score += 60
    if "karahi" in n:
        score += 55
    if "bhuna" in n:
        score += 45
    if "chilli" in n or "chili" in n:
        score += 40
    return score


def suggest_substitution(
    menu: MenuSnapshot,
    q: TraitQuery,
    *,
    spicy_threshold: int = 4,
    safe_fail_threshold: float = 0.70,
    limit: int = 3,
) -> Tuple[Optional[str], float, str]:
    """
    Return (suggested_item_name, confidence, reasoning).

    Priority:
      1) Menu metadata (protein/heat) if available
      2) Heuristic menu scanning
      3) Safe-fail: no specific suggestion
    """
    if not menu or not q or not q.protein:
        return None, 0.0, "no-protein"

    protein = q.protein.strip().lower()

    # ---- 1) Metadata path ----
    # MenuSnapshot may or may not implement attribute search. Keep safe.
    try:
        if q.wants_spicy:
            ids = menu.find_by_attributes(protein=protein, heat_min=int(spicy_threshold), limit=limit)
            if ids:
                names = [menu.display_name(iid) for iid in ids if menu.display_name(iid)]
                if names:
                    return names[0], 0.85, "metadata:protein+heat"

            # Protein exists but spicy filter didn't match
            ids2 = menu.find_by_attributes(protein=protein, limit=limit)
            if ids2:
                return None, 0.65, "metadata:protein-but-no-spicy-match"
            return None, 0.0, "metadata:no-candidates"

        ids = menu.find_by_attributes(protein=protein, limit=limit)
        if ids:
            name = menu.display_name(ids[0])
            if name:
                return name, 0.75, "metadata:protein"
    except Exception:
        # fall through to heuristic
        pass

    # ---- 2) Heuristic fallback ----
    scan_limit = 80  # safety cap: avoid huge suggestion lists
    candidates = list_items_for_protein(menu, protein, limit=scan_limit)
    if not candidates:
        return None, 0.0, "fallback:no-candidates"

    if q.wants_spicy:
        ranked = sorted(candidates, key=_spice_score, reverse=True)
        top = ranked[0]
        top_score = _spice_score(top)

        if top_score >= 55:
            return top, 0.75, "fallback:protein+spicy-keyword"

        # Not confident enough to recommend a specific item
        return None, 0.65, "fallback:protein-but-spice-uncertain"

    # Protein-only, no spice preference: avoid overly confident picks
    ranked = sorted(candidates, key=lambda x: (len(x), x))
    return ranked[0], max(0.0, safe_fail_threshold - 0.05), "fallback:protein-only"
