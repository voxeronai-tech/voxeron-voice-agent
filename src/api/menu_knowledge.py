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
    "biryani": ("biryani",),
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
    Extract a generic naan variant keyword (garlic/cheese/butter/keema/peshawari/plain),
    but ONLY when it appears in a tight window around a naan token.
    This prevents false positives like: "butter chicken ... and three naan" -> butter.
    """
    t = norm_simple(text)
    if not t:
        return None

    toks = [x for x in t.split() if x]
    if not toks:
        return None

    naan_toks = {"naan", "nan"}

    # Variants (highest-signal first)
    variant_map = [
        ("garlic", {"garlic", "knoflook"}),
        ("cheese", {"cheese", "kaas"}),
        ("butter", {"butter", "boter"}),
        ("keema", {"keema", "kheema"}),
        ("peshawari", {"peshawari"}),
        ("plain", {"plain", "regular", "normal", "gewoon", "normaal", "standaard"}),
    ]

    naan_positions = [i for i, tok in enumerate(toks) if tok in naan_toks]
    if not naan_positions:
        return None

    # Tight context window around naan to avoid "butter chicken ... naan" bleed.
    WIN_BEFORE = 2
    WIN_AFTER = 2

    for ni in naan_positions:
        lo = max(0, ni - WIN_BEFORE)
        hi = min(len(toks), ni + WIN_AFTER + 1)
        window = toks[lo:hi]

        for variant, vset in variant_map:
            if any(w in vset for w in window):
                # "plain" isn't an explicit variant in most flows; treat as None.
                if variant == "plain":
                    return None
                return variant

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

# -----------------------------------------------------------------------------
# Naan helpers (tenant-agnostic, menu-aware)
# -----------------------------------------------------------------------------
_NAAN_WORDS: Tuple[str, ...] = ("naan", "nan", "naam")


def is_naan_item(menu: MenuSnapshot, item_id: str) -> bool:
    if not menu or not item_id:
        return False
    dn = (menu.display_name(item_id) or "").lower()
    return any(w in dn for w in _NAAN_WORDS)


def naan_options_from_menu(menu: MenuSnapshot) -> List[Tuple[str, str]]:
    """
    Return [(label, item_id)] sorted with plain/garlic first when available.
    """
    if not menu:
        return []

    items: List[Tuple[str, str]] = []
    for _name, iid in menu.name_choices:
        if not is_naan_item(menu, iid):
            continue
        label = (menu.display_name(iid) or "").strip()
        if label:
            items.append((label, iid))

    if not items:
        return []

    prefs: List[Tuple[str, Tuple[str, ...]]] = [
        ("plain", ("naan", "nan", "plain", "regular", "normal", "gewoon", "normaal", "standaard")),
        ("garlic", ("garlic", "knoflook")),
        ("butter", ("butter", "boter")),
        ("cheese", ("cheese", "kaas")),
        ("keema", ("keema", "kheema")),
        ("peshawari", ("peshawari",)),
    ]

    def score(label: str) -> int:
        ll = label.lower().strip()
        if ll in {"nan", "naan"}:
            return 200
        for i, (_k, toks) in enumerate(prefs):
            if any(t in ll for t in toks):
                return 150 - i
        return 0

    items.sort(key=lambda x: (score(x[0]), -len(x[0])), reverse=True)
    return items


def naan_optima_prompt(menu: Optional[MenuSnapshot], lang: str, *, list_mode: str = "short", with_main: Optional[str] = None) -> str:
    """
    Minimal Optima-style prompt, optionally using real menu labels when available.
    """
    lang_n = (lang or "en").lower()
    max_n = 2 if list_mode == "short" else 4

    opts = naan_options_from_menu(menu) if menu else []
    labels = [x[0] for x in opts[:max_n]]

    if not labels:
        if lang_n == "nl":
            return f"Zeker. Wil je gewone naan of garlic naan?" if not with_main else f"Zeker. Wil je gewone naan of garlic naan bij je {with_main}?"
        return f"Certainly. Would you like plain naan or garlic naan?" if not with_main else f"Certainly. Would you like plain naan or garlic naan with your {with_main}?"

    if lang_n == "nl":
        if len(labels) >= 2:
            return f"Wil je {labels[0]} of {labels[1]}?"
        return f"We hebben {labels[0]}. Wil je die?"
    else:
        if len(labels) >= 2:
            return f"Would you like {labels[0]} or {labels[1]}?"
        return f"We have {labels[0]}. Would you like that?"


def find_naan_item_for_variant(menu: Optional[MenuSnapshot], variant: str) -> Optional[str]:
    """
    Map a variant keyword (plain/garlic/cheese/...) to the best matching naan item_id in the menu.
    """
    if not menu or not variant:
        return None

    v = variant.strip().lower()
    opts = naan_options_from_menu(menu)
    if not opts:
        return None

    variant_tokens = {
        "plain": ("naan", "nan", "plain", "regular", "normal", "gewoon", "normaal", "standaard"),
        "garlic": ("garlic", "knoflook"),
        "butter": ("butter", "boter"),
        "cheese": ("cheese", "kaas"),
        "keema": ("keema", "kheema"),
        "peshawari": ("peshawari",),
    }

    toks = variant_tokens.get(v, (v,))
    best_iid: Optional[str] = None
    best_score = -1

    for label, iid in opts:
        ll = label.lower().strip()
        s = 0
        if v == "plain" and ll in {"nan", "naan"}:
            s += 50
        if any(t in ll for t in toks):
            s += 25
        if v in ll:
            s += 8
        if s > best_score:
            best_score = s
            best_iid = iid

    return best_iid if best_score >= 0 else None
