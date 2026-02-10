# src/api/engine/restaurant_engine.py
from __future__ import annotations

from typing import Any, List, Optional

from .types import PlanAction, ResponsePlan
from ..intent import norm_simple
from ..policy import nan_variant_question


class RestaurantEngine:
    _Q_WORDS = ("which", "what", "welke", "wat", "hoe", "variety", "soorten", "wat voor")
    _SPICY_WORDS = ("spicy", "very spicy", "hot", "heet", "pittig", "heel pittig")

    def _looks_like_question(self, text: str) -> bool:
        raw = (text or "").strip()
        if not raw:
            return False
        if "?" in raw:
            return True
        tn = norm_simple(raw)
        return bool(tn) and any(w in tn for w in self._Q_WORDS)

    def _is_spicy_query(self, text: str) -> bool:
        t = norm_simple(text)
        return bool(t) and any(x in t for x in self._SPICY_WORDS)

    def _top3_lamb(self, state: Any) -> List[str]:
        ranked = ["Lamb Dhansak", "Lamb Biryani", "Lamb Korma"]
        menu = getattr(state, "menu", None)
        if not menu:
            return ranked
        available = {menu.display_name(iid) for _n, iid in getattr(menu, "name_choices", [])}
        return [x for x in ranked if x in available] or ranked

    def _find_naan_item_id(self, menu: Any, variant: str) -> Optional[str]:
        """
        Deterministic lookup based on display name only.
        Avoids imports into infrastructure modules.
        """
        v = (variant or "").lower().strip()
        choices = getattr(menu, "name_choices", []) or []

        # Collect candidate display names once
        items: List[tuple[str, str]] = []
        for _n, iid in choices:
            dn = (menu.display_name(iid) or "").strip()
            if not dn:
                continue
            items.append((iid, dn.lower()))

        if v == "keema":
            for iid, dn in items:
                if "naan" in dn and "keema" in dn:
                    return iid
            # fallback: some menus spell "kheema"
            for iid, dn in items:
                if "naan" in dn and "kheema" in dn:
                    return iid
            return None

        # plain/default
        for iid, dn in items:
            if "naan" not in dn:
                continue
            # exclude flavored variants if possible
            if any(x in dn for x in ("keema", "kheema", "garlic", "peshawari", "cheese", "chili", "stuffed")):
                continue
            return iid

        # fallback: any naan item
        for iid, dn in items:
            if "naan" in dn:
                return iid
        return None

    def _resolve_pending_nan_variant(self, st: Any, transcript: str) -> ResponsePlan | None:
        if getattr(st, "pending_choice", None) != "nan_variant":
            return None

        lang = getattr(st, "lang", "en")
        menu = getattr(st, "menu", None)

        tnorm = norm_simple(transcript) or ""
        t = f" {tnorm} "

        is_keema = any(x in t for x in (" keema ", " kheema "))
        is_plain = any(x in t for x in (
            " none ", " plain ", " regular ", " normal ",
            " gewoon ", " normaal ", " standaard ",
            " naan ", " nan ",
            " nee ", " nee hoor ",
        ))
        is_cancel = any(x in t for x in (
            " cancel ", " stop ", " nothing ", " niks ", " niets ",
            " dat is alles ", " dat was alles ", " klaar ",
            " no thanks ", " no thank you ",
        ))

        # If user is asking a question, keep the clarification loop
        if self._is_spicy_query(transcript) or self._looks_like_question(transcript):
            if lang == "nl":
                info = (
                    "Naans zijn meestal niet pittig, het is brood om pittige curry te balanceren. "
                    "Keema naan kan wat kruidiger zijn, peshawari is juist wat zoeter."
                )
            else:
                info = (
                    "Naans are usually not spicy — they’re bread to balance spicy curries. "
                    "Keema naan can be a bit more spiced, and peshawari is sweet."
                )
            return ResponsePlan(
                action=PlanAction.CLARIFY,
                reply=f"{info} {nan_variant_question(lang, verbose=False)}",
                lang=lang,
                pending_choice="nan_variant",
                pending_qty=max(1, int(getattr(st, "pending_qty", 1) or 1)),
                debug={"reason": "naan_spicy_question_override"},
            )

        # If user is answering the choice (or canceling it), resolve deterministically
        if is_cancel:
            st.pending_choice = None
            return ResponsePlan(
                action=PlanAction.UPDATE_CART,
                reply="",
                lang=lang,
                pending_choice=None,
                pending_qty=max(1, int(getattr(st, "pending_qty", 1) or 1)),
                debug={"reason": "naan_variant_cancelled"},
            )

        if is_keema or is_plain:
            if not menu:
                # No menu loaded => just clear the gate and continue.
                st.pending_choice = None
                return ResponsePlan(
                    action=PlanAction.NOOP,
                    reply="",
                    lang=lang,
                    pending_choice=None,
                    pending_qty=max(1, int(getattr(st, "pending_qty", 1) or 1)),
                    debug={"reason": "naan_variant_cleared_no_menu"},
                )

            variant = "keema" if is_keema else "plain"
            iid = self._find_naan_item_id(menu, variant)
            qty = max(1, int(getattr(st, "pending_qty", 1) or 1))

            if iid:
                st.order.add(iid, qty)

            st.pending_choice = None
            return ResponsePlan(
                action=PlanAction.UPDATE_CART,
                reply="",
                lang=lang,
                pending_choice=None,
                pending_qty=qty,
                debug={"reason": "naan_variant_resolved", "variant": variant, "added": bool(iid)},
            )

        # Not understood => let controller continue (it may ask pickup/delivery etc.)
        return None

    def plan(self, state: Any, transcript: str) -> ResponsePlan:
        st = state

        # 0) Resolve pending choice FIRST
        resolved = self._resolve_pending_nan_variant(st, transcript)
        if resolved is not None:
            return resolved

        tnorm = norm_simple(transcript)

        # 1) “What lamb dishes…” → crisp top-3 + ask preference
        if tnorm and "lamb" in tnorm and "menu" in tnorm and any(x in tnorm for x in ("dish", "dishes", "gerechten")):
            top3 = self._top3_lamb(st)
            items = ", ".join(top3)
            if getattr(st, "lang", "en") == "nl":
                msg = f"Even kijken. We hebben bijvoorbeeld {items}. Zegt een van deze u iets?"
            else:
                msg = (
                    f"Let me check the menu for you. We have a few great lamb dishes like {items}. "
                    "Do any of those sound good?"
                )
            st.last_category = "lamb"
            st.last_category_items = top3
            return ResponsePlan(action=PlanAction.REPLY, reply=msg, lang=getattr(st, "lang", "en"), debug={"reason": "top3_lamb"})

        return ResponsePlan(action=PlanAction.NOOP, reply="", lang=getattr(st, "lang", "en"))
