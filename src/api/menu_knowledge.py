from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

from .intent import norm_simple
from .menu_store import MenuSnapshot


@dataclass
class TraitQuery:
    protein: Optional[str] = None  # lamb/chicken/vegetarian/biryani
    wants_spicy: bool = False
    raw: str = ""


_SPICY_WORDS = {
    "spicy", "hot", "very spicy", "extra spicy",
    "heet", "pittig", "heel heet", "erg pittig",
    # dish-style hints that often imply heat
    "madras", "vindaloo", "phall",
}

_PROTEIN_HINTS = {
    "lamb": {"lamb", "lam", "lams"},
    "chicken": {"chicken", "kip"},
    "vegetarian": {"vegetarian", "vega", "veg", "vegetarisch", "paneer"},
    "biryani": {"biryani"},
}


def extract_traits(text: str) -> TraitQuery:
    t = norm_simple(text)
    q = TraitQuery(raw=t)

    if any(w in t for w in _SPICY_WORDS):
        q.wants_spicy = True

    for protein, keys in _PROTEIN_HINTS.items():
        if any(k in t for k in keys):
            q.protein = protein
            break

    return q


def list_items_for_protein(menu: MenuSnapshot, protein: str) -> List[str]:
    """
    MVP fallback: name heuristics (used if metadata isn't available).
    """
    out: List[str] = []
    for _norm_name, iid in menu.name_choices:
        try:
            dn = (menu.display_name(iid) or "").strip()
        except Exception:
            continue
        l = dn.lower()

        if protein == "lamb":
            if "lamb" in l or "lam" in l:
                out.append(dn)
        elif protein == "chicken":
            if "chicken" in l or "kip" in l:
                out.append(dn)
        elif protein == "biryani":
            if "biryani" in l:
                out.append(dn)
        elif protein == "vegetarian":
            if any(x in l for x in ["veg", "veget", "paneer", "dahl", "dal"]):
                out.append(dn)

    seen = set()
    dedup = []
    for x in out:
        if x not in seen:
            seen.add(x)
            dedup.append(x)

    return dedup[:80]


def _spice_score(name: str) -> int:
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
) -> Tuple[Optional[str], float, str]:
    """
    Return (suggested_item_name, confidence, reasoning).

    Priority:
      1) Metadata tags (protein/heat/is_spicy)
      2) Fallback deterministic name scoring
      3) Safe-fail (ask clarifying question upstream)

    Confidence meanings:
      - >= safe_fail_threshold: safe to suggest 1-2 items (NO auto-add)
      - <  safe_fail_threshold: do not suggest a specific dish
    """
    if not menu or not q.protein:
        return None, 0.0, "no-protein"

    # ---- 1) Metadata path ----
    # If tags exist, find_by_attributes will return item_ids
    try:
        if q.wants_spicy:
            ids = menu.find_by_attributes(protein=q.protein, heat_min=int(spicy_threshold), limit=3)
            if ids:
                names = [menu.display_name(iid) for iid in ids]
                # spiciest first
                return names[0], 0.85, "metadata:protein+heat"
            # protein exists but no spicy match
            ids2 = menu.find_by_attributes(protein=q.protein, limit=3)
            if ids2:
                return None, 0.65, "metadata:protein-but-no-spicy-match"
            return None, 0.0, "metadata:no-candidates"
        else:
            ids = menu.find_by_attributes(protein=q.protein, limit=3)
            if ids:
                return menu.display_name(ids[0]), 0.75, "metadata:protein"
    except Exception:
        # fall through to heuristic
        pass

    # ---- 2) Heuristic fallback ----
    candidates = list_items_for_protein(menu, q.protein)
    if not candidates:
        return None, 0.0, "fallback:no-candidates"

    if q.wants_spicy:
        ranked = sorted(candidates, key=_spice_score, reverse=True)
        top = ranked[0]
        top_score = _spice_score(top)
        if top_score >= 55:
            return top, 0.75, "fallback:protein+spicy-keyword"
        return None, 0.65, "fallback:protein-but-spice-uncertain"

    ranked = sorted(candidates, key=lambda x: (len(x), x))
    return ranked[0], 0.65, "fallback:protein-only"

