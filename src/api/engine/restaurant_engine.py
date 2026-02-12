# src/api/engine/restaurant_engine.py
from __future__ import annotations

import re
from typing import Any, List, Optional

from .types import PlanAction, ResponsePlan
from ..intent import norm_simple

NAAN_VARIANT_STT_HINT_EN = (
    "The user is choosing a naan option from the menu. "
    "Return only the chosen option words."
)

NAAN_VARIANT_STT_HINT_NL = (
    "De gebruiker kiest een naan-optie van de menukaart. "
    "Geef alleen de gekozen optie-woorden terug."
)

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

    def _qty_near_keyword(self, transcript: str, keyword: str) -> int:
        """
        Minimal local qty extraction to support disambiguation gates.
        Handles patterns like:
          "two biryani", "3 naan", "three naan", "2x biryani"
        """
        t = norm_simple(transcript) or ""
        kw = (keyword or "").strip().lower()
        if not t or not kw:
            return 1

        # digits
        m = re.search(rf"\b(\d+)\s*(?:x|×)?\s+\b{re.escape(kw)}\b", t)
        if m:
            try:
                return max(1, int(m.group(1)))
            except Exception:
                return 1

        # word numbers (keep it small on purpose)
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5}
        m = re.search(rf"\b(one|two|three|four|five)\b\s+\b{re.escape(kw)}\b", t)
        if m:
            return max(1, int(words.get(m.group(1), 1)))

        return 1

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

        items: List[tuple[str, str]] = []
        for _n, iid in choices:
            dn = (menu.display_name(iid) or "").strip()
            if not dn:
                continue
            items.append((iid, dn.lower()))

        if v == "keema":
            for iid, dn in items:
                if ("naan" in dn or "nan" in dn) and "keema" in dn:
                    return iid
            for iid, dn in items:
                if ("naan" in dn or "nan" in dn) and "kheema" in dn:
                    return iid
            return None

        # plain/default
        for iid, dn in items:
            if "naan" not in dn and "nan" not in dn:
                continue
            if any(x in dn for x in ("keema", "kheema", "garlic", "peshawari", "cheese", "chili", "stuffed")):
                continue
            return iid

        # fallback: any naan
        for iid, dn in items:
            if "naan" in dn or "nan" in dn:
                return iid
        return None

    def _get_pending_qty(self, st: Any, choice: str, default: int = 1) -> int:
        qty_map = getattr(st, "pending_qty_by_choice", None)
        if not isinstance(qty_map, dict):
            return max(1, int(default or 1))
        return max(1, int(qty_map.get(choice, default) or 1))
    
    def _menu_options_for_keyword(self, menu: Any, keyword: str, limit: int = 3) -> List[str]:
        """
        Menu-driven option discovery. Single source of truth = MenuSnapshot display names.
        Keeps controller menu-blind, keeps engine deterministic.
        """
        kw = (keyword or "").lower().strip()
        if not menu or not kw:
            return []

        opts: List[str] = []
        for _n, iid in getattr(menu, "name_choices", []) or []:
            dn = (menu.display_name(iid) or "").strip()
            if not dn:
                continue
            dn_l = dn.lower()

            # match keyword (e.g., "naan"/"nan")
            if kw not in dn_l:
                continue

            opts.append(dn)

        # de-dupe while preserving menu order
        seen = set()
        uniq: List[str] = []
        for x in opts:
            k = x.lower()
            if k in seen:
                continue
            seen.add(k)
            uniq.append(x)

        return uniq[: max(1, int(limit or 3))]

    def _ask_variant_from_menu(self, menu: Any, keyword: str, lang: str) -> str:
        opts = self._menu_options_for_keyword(menu, keyword, limit=3)

        # if menu has no options, stay generic (still no hardcoding "plain/garlic")
        if not opts:
            return (f"Which {keyword} would you like?" if lang != "nl" else f"Welke {keyword} wilt u?")

        if lang != "nl":
            if len(opts) == 1:
                return f"For {keyword}, we have {opts[0]}. Would you like that?"
            return f"For {keyword}, our top options are: {', '.join(opts)}. Which one would you like?"
        else:
            if len(opts) == 1:
                return f"Voor {keyword} hebben we {opts[0]}. Wil je die?"
            return f"Voor {keyword} zijn dit de topopties: {', '.join(opts)}. Welke wil je?"


    def _pending_head(self, st: Any) -> Optional[str]:
        q = getattr(st, "pending_choices", None)
        if isinstance(q, list) and q:
            return q[0]
        return getattr(st, "pending_choice", None)

    def _clear_pending_gate(self, st: Any, choice: str) -> None:
        # FIFO: clear only if this choice is currently the head
        q = getattr(st, "pending_choices", None)
        if isinstance(q, list) and q and q[0] == choice:
            q.pop(0)
            st.pending_choice = q[0] if q else None
        else:
            # legacy fallback
            if getattr(st, "pending_choice", None) == choice:
                st.pending_choice = None

        qty_map = getattr(st, "pending_qty_by_choice", None)
        if isinstance(qty_map, dict):
            qty_map.pop(choice, None)

    def _is_cancel_intent(self, t: str) -> bool:
        return any(x in t for x in (
            " cancel ", " stop ", " nothing ", " niks ", " niets ",
            " dat is alles ", " dat was alles ", " klaar ",
            " no thanks ", " no thank you ", " never mind ", " laat maar ", " annuleer ",
        ))


    def _resolve_pending_biryani_variant(self, st: Any, transcript: str) -> ResponsePlan | None:
        choice = "biryani_variant"
        if self._pending_head(st) != choice:
            return None

        lang = getattr(st, "lang", "en") or "en"
        menu = getattr(st, "menu", None)

        tnorm = norm_simple(transcript) or ""
        t = f" {tnorm} "
        qty = self._get_pending_qty(st, choice, default=1)

        # If user is asking a question, keep the clarification loop (optional, consistent with naan)
        if self._looks_like_question(transcript):
            ask = (
                "Which biryani would you like, chicken, lamb, vegetarian, or mix?"
                if lang != "nl"
                else "Welke biryani wilt u, kip, lam, vegetarisch, of mix?"
            )
            return ResponsePlan(
                action=PlanAction.CLARIFY,
                reply=ask,
                lang=lang,
                pending_choice=choice,
                pending_qty=qty,
                consumed=True,
                debug={"reason": "biryani_variant_question_override"},
            )

        if self._is_cancel_intent(t):
            self._clear_pending_gate(st, choice)
            return ResponsePlan(
                action=PlanAction.UPDATE_CART,
                reply="",
                lang=lang,
                pending_choice=None,
                pending_qty=qty,
                consumed=True,
                debug={"reason": "biryani_variant_cancelled"},
            )

        # Resolve variant intent (order matters, handle tikka before base terms)
        pick: Optional[str] = None
        if " tikka " in t and " chicken " in t:
            pick = "chicken tikka biryani"
        elif " tikka " in t and " lamb " in t:
            pick = "lamb tikka biryani"
        elif " chicken " in t:
            pick = "chicken biryani"
        elif " lamb " in t:
            pick = "lamb biryani"
        elif any(x in t for x in (" vegetarian ", " veg ", " vegetarisch ", " groente ")):
            pick = "vegetarian biryani"
        elif " mix " in t:
            pick = "mix biryani"
        elif " mushroom " in t:
            pick = "mushroom biryani"
        elif any(x in t for x in (" king prawn ", " prawn ", " garnaal ")):
            pick = "king prawn biryani"

        if not pick:
            return ResponsePlan(
                action=PlanAction.NOOP,
                reply="",
                lang=lang,
                pending_choice=choice,
                pending_qty=qty,
                consumed=True,
                debug={"reason": "biryani_variant_not_understood"},
            )

        if not menu:
            self._clear_pending_gate(st, choice)
            return ResponsePlan(
                action=PlanAction.NOOP,
                reply="",
                lang=lang,
                pending_choice=None,
                pending_qty=qty,
                consumed=True,
                debug={"reason": "biryani_variant_cleared_no_menu"},
            )

        iid = self._find_biryani_item_id(menu, pick)
        if iid:
            st.order.add(iid, qty)

        self._clear_pending_gate(st, choice)
        return ResponsePlan(
            action=PlanAction.UPDATE_CART,
            reply="",
            lang=lang,
            pending_choice=None,
            pending_qty=qty,
            debug={"reason": "biryani_variant_resolved", "variant": pick, "added": bool(iid), "qty": qty},
        )

    def _resolve_pending_nan_variant(self, st: Any, transcript: str) -> ResponsePlan | None:
        choice = "nan_variant"
        if self._pending_head(st) != choice:
            return None

        lang = getattr(st, "lang", "en") or "en"
        menu = getattr(st, "menu", None)

        # Generic, menu-blind hint for STT biasing on the NEXT user turn
        stt_hint = NAAN_VARIANT_STT_HINT_NL if lang == "nl" else NAAN_VARIANT_STT_HINT_EN

        tnorm = norm_simple(transcript) or ""
        t = f" {tnorm} "
        qty = self._get_pending_qty(st, choice, default=1)

        is_keema = any(x in t for x in (" keema ", " kheema "))
        is_plain = any(
            x in t
            for x in (
                " none ", " plain ", " regular ", " normal ",
                " gewoon ", " normaal ", " standaard ",
                " naan ", " nan ",
                " nee ", " nee hoor ",  # treat “no (thanks)” as “plain” for naan choice gate
            )
        )

        # If user is asking a question, keep the clarification loop
        if self._is_spicy_query(transcript) or self._looks_like_question(transcript):
            info = (
                "Naans zijn meestal niet pittig, het is brood om pittige curry te balanceren. "
                "Keema naan kan wat kruidiger zijn, peshawari is juist wat zoeter."
                if lang == "nl"
                else
                "Naans are usually not spicy, they’re bread to balance spicy curries. "
                "Keema naan can be a bit more spiced, and peshawari is sweet."
            )
            return ResponsePlan(
                action=PlanAction.CLARIFY,
                reply=f"{info} {self._ask_variant_from_menu(getattr(st, 'menu', None), 'naan', lang)}",
                lang=lang,
                pending_choice=choice,
                pending_qty=qty,
                stt_hint=stt_hint,
                consumed=True,
                debug={"reason": "naan_spicy_question_override"},
            )

        # Cancel / stop while gate is open
        if self._is_cancel_intent(t):
            self._clear_pending_gate(st, choice)
            return ResponsePlan(
                action=PlanAction.UPDATE_CART,
                reply="",
                lang=lang,
                pending_choice=None,
                pending_qty=qty,
                stt_hint=None,
                consumed=True,
                debug={"reason": "naan_variant_cancelled"},
            )

        # Resolve "plain" / "keema" quickly (fast path)
        if is_keema or is_plain:
            if not menu:
                self._clear_pending_gate(st, choice)
                return ResponsePlan(
                    action=PlanAction.NOOP,
                    reply="",
                    lang=lang,
                    pending_choice=None,
                    pending_qty=qty,
                    stt_hint=None,
                    consumed=True,
                    debug={"reason": "naan_variant_cleared_no_menu"},
                )

            variant = "keema" if is_keema else "plain"
            iid = self._find_naan_item_id(menu, variant)

            if iid:
                st.order.add(iid, qty)

            self._clear_pending_gate(st, choice)
            return ResponsePlan(
                action=PlanAction.UPDATE_CART,
                reply="",
                lang=lang,
                pending_choice=None,
                pending_qty=qty,
                stt_hint=None,
                consumed=True,
                debug={"reason": "naan_variant_resolved", "variant": variant, "added": bool(iid), "qty": qty},
            )

        # Not understood: MUST reprompt (otherwise Gate-Lock creates silence)
        return ResponsePlan(
            action=PlanAction.CLARIFY,
            reply=self._ask_variant_from_menu(getattr(st, "menu", None), "naan", lang),
            lang=lang,
            pending_choice=choice,
            pending_qty=qty,
            stt_hint=stt_hint,
            consumed=True,
            debug={"reason": "naan_variant_not_understood_reprompt"},
        )
    
    def plan(self, state: Any, transcript: str) -> ResponsePlan:
        st = state
        t_raw = (transcript or "").strip()
        lang = getattr(st, "lang", "en") or "en"

        # ----------------------------------------------------------
        # Connect-tick greeting (one-time) for restaurant domain
        # ----------------------------------------------------------
        if not t_raw:
            if getattr(st, "restaurant_greeted", False):
                return ResponsePlan(action=PlanAction.NOOP, reply="", lang=lang)

            setattr(st, "restaurant_greeted", True)

            greet = (
                "Hi! Welcome to Taj Mahal Bussum. You can start ordering now. If you want Dutch, say 'Nederlands'."
                if lang != "nl"
                else "Welkom bij Taj Mahal Bussum. Je kunt nu bestellen. Als je Engels wilt, zeg 'English'."
            )
            return ResponsePlan(action=PlanAction.REPLY, reply=greet, lang=lang, debug={"reason": "restaurant_connect_greet"})

        # ----------------------------------------------------------
        # 0) Resolve pending gates FIRST
        # ----------------------------------------------------------
        resolved = self._resolve_pending_biryani_variant(st, transcript)
        if resolved is not None:
            return resolved

        resolved = self._resolve_pending_nan_variant(st, transcript)
        if resolved is not None:
            return resolved

        tnorm = norm_simple(transcript) or ""
        t = f" {tnorm} "

        # ----------------------------------------------------------
        # 1) Informational query example (keep your existing lamb helper)
        # ----------------------------------------------------------
        if tnorm and "lamb" in tnorm and "menu" in tnorm and any(x in tnorm for x in ("dish", "dishes", "gerechten")):
            top3 = self._top3_lamb(st)
            items = ", ".join(top3)
            msg = (
                f"Even kijken. We hebben bijvoorbeeld {items}. Zegt een van deze u iets?"
                if lang == "nl"
                else (
                    f"Let me check the menu for you. We have a few great lamb dishes like {items}. "
                    "Do any of those sound good?"
                )
            )
            st.last_category = "lamb"
            st.last_category_items = top3
            return ResponsePlan(action=PlanAction.REPLY, reply=msg, lang=lang, debug={"reason": "top3_lamb"})

        # ----------------------------------------------------------
        # 2) Gate creation: biryani disambiguation
        # If user mentions biryani without a specific variant, ask.
        # ----------------------------------------------------------
        if " biryani " in t:
            has_variant = any(x in t for x in (
                " chicken ", " lamb ", " vegetarian ", " veg ", " vegetarisch ",
                " mix ", " mushroom ", " prawn ", " king prawn ", " garnaal ",
                " tikka ",
            ))
            if not has_variant:
                qty = self._qty_near_keyword(transcript, "biryani")
                ask = (
                    "Which biryani would you like, chicken, lamb, or vegetarian?"
                    if lang != "nl"
                    else "Welke biryani wilt u, kip, lam, of vegetarisch?"
                )
                return ResponsePlan(
                    action=PlanAction.CLARIFY,
                    reply=ask,
                    lang=lang,
                    pending_choice="biryani_variant",
                    pending_qty=qty,
                    consumed=True,
                    debug={"reason": "biryani_variant_gate_set", "qty": qty},
                )

        # ----------------------------------------------------------
        # 3) Gate creation: naan disambiguation
        # If user mentions naan/nan without specifying variant, ask.
        # ----------------------------------------------------------
        if (" naan " in t) or (" nan " in t):
            has_variant = any(x in t for x in (
                " garlic ", " knoflook ",
                " cheese ", " kaas ",
                " keema ", " kheema ",
                " peshawari ",
                " plain ", " regular ", " normal ", " gewoon ", " normaal ", " standaard ",
            ))
            if not has_variant:
                qty = self._qty_near_keyword(transcript, "naan")
                qty = max(qty, self._qty_near_keyword(transcript, "nan"))
                return ResponsePlan(
                    action=PlanAction.CLARIFY,
                    reply=self._ask_variant_from_menu(getattr(st, "menu", None), "naan", lang),
                    lang=lang,
                    pending_choice="nan_variant",
                    pending_qty=qty,
                    consumed=True,
                    debug={"reason": "naan_variant_gate_set", "qty": qty},
                )

        # ----------------------------------------------------------
        # Default: NOOP, let SessionController do deterministic ordering
        # ----------------------------------------------------------
        return ResponsePlan(action=PlanAction.NOOP, reply="", lang=lang, debug={"reason": "restaurant_noop_default"})

