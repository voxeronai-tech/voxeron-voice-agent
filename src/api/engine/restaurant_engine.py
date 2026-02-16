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

    def _get_bestsellers(self, st: Any, limit: int = 3, category_hint: Optional[str] = None) -> List[str]:
        """
        Generic, menu-driven bestseller suggestions.
        - Uses MenuSnapshot.items_by_id and MenuItem.tags (dict)
        - No domain hardcoding
        - Deterministic fallback: stable menu order
        """
        menu = getattr(st, "menu", None)
        if not menu:
            return []
        items_by_id = getattr(menu, "items_by_id", None)
        if not isinstance(items_by_id, dict) or not items_by_id:
            return []

        items = list(items_by_id.values())

        # Optional coarse filter by category hint if the tenant encodes it in tags
        if category_hint:
            ch = category_hint.strip().lower()
            if ch:
                def _has_cat(it: Any) -> bool:
                    tags = getattr(it, "tags", None) or {}
                    if not isinstance(tags, dict):
                        return False
                    cat = tags.get("category")
                    if isinstance(cat, str) and cat.strip().lower() == ch:
                        return True
                    cats = tags.get("categories")
                    if isinstance(cats, list) and any(isinstance(x, str) and x.strip().lower() == ch for x in cats):
                        return True
                    return False

                filtered = [it for it in items if _has_cat(it)]
                if filtered:
                    items = filtered

        def _score(it: Any) -> tuple:
            tags = getattr(it, "tags", None) or {}
            if not isinstance(tags, dict):
                tags = {}

            bestseller_flag = 1 if tags.get("bestseller") or tags.get("popular") or tags.get("most_popular") else 0

            pop = tags.get("popularity_score") or tags.get("popularity")
            try:
                pop_val = float(pop) if pop is not None else 0.0
            except Exception:
                pop_val = 0.0

            rank = tags.get("sales_rank") or tags.get("rank")
            try:
                rank_val = int(rank) if rank is not None else 10**9
            except Exception:
                rank_val = 10**9

            return (bestseller_flag, pop_val, -rank_val)

        items_sorted = sorted(items, key=_score, reverse=True)

        out: List[str] = []
        for it in items_sorted:
            name = (getattr(it, "name", None) or "").strip()
            if not name or name in out:
                continue
            out.append(name)
            if len(out) >= limit:
                break

        if not out:
            for it in items:
                name = (getattr(it, "name", None) or "").strip()
                if not name or name in out:
                    continue
                out.append(name)
                if len(out) >= limit:
                    break

        return out

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

    def _strip_variant_question_tail(self, q: str, lang: str) -> str:
        qq = (q or "").strip()
        low = qq.lower()

        if lang != "nl":
            tails = ("which one would you like?", "which would you like?", "would you like that?")
        else:
            tails = ("welke wil je?", "wil je die?")

        for tail in tails:
            if low.endswith(tail):
                return qq[: -len(tail)].rstrip(" .,:;!?")
        return qq

    def _join_variant_questions(self, qs: list[str], lang: str) -> str:
        qs = [q.strip() for q in (qs or []) if q and q.strip()]
        if not qs:
            return ""
        if len(qs) == 1:
            return qs[0]

        q1 = qs[0].rstrip()
        q2 = self._strip_variant_question_tail(qs[1], lang)
        if q2:
            q2 = q2[:1].lower() + q2[1:]

        return f"{q1} Also, {q2}." if lang != "nl" else f"{q1} En ook, {q2}."

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

        def _match_naan_from_menu(menu_obj: Any, transcript_text: str) -> Optional[str]:
            """
            Menu-driven variant resolution.
            Finds the best matching naan item_id by token overlap with display_name.
            Domain stays in engine; controller remains blind.
            """
            if not menu_obj:
                return None

            tt = (norm_simple(transcript_text) or "").strip()
            if not tt:
                return None

            # tokens that carry "variant" meaning; strip filler + the keyword itself
            stop = {
                "naan", "nan", "the", "a", "an", "please", "pls", "i", "want", "would", "like",
                "do", "you", "have", "on", "menu", "which", "wat", "welke",
            }
            toks = [w for w in tt.split() if w and w not in stop]
            if not toks:
                return None

            best_iid: Optional[str] = None
            best_score = 0

            for _n, iid in getattr(menu_obj, "name_choices", []) or []:
                dn = (menu_obj.display_name(iid) or "").strip().lower()
                if not dn:
                    continue
                if ("naan" not in dn) and ("nan" not in dn):
                    continue

                score = 0
                for tok in toks:
                    if tok in dn:
                        score += 1

                if score > best_score:
                    best_score = score
                    best_iid = iid

            return best_iid if best_score > 0 else None

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
                action=PlanAction.REPLY,
                reply=("Okay, cancelled." if lang != "nl" else "Oké, geannuleerd."),
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
                    action=PlanAction.CLARIFY,
                    reply=self._ask_variant_from_menu(getattr(st, "menu", None), "naan", lang),
                    lang=lang,
                    pending_choice=choice,
                    pending_qty=qty,
                    stt_hint=stt_hint,
                    consumed=True,
                    debug={"reason": "naan_variant_no_menu_reprompt"},
                )

            variant = "keema" if is_keema else "plain"
            iid = self._find_naan_item_id(menu, variant)

            if iid:
                st.order.add(iid, qty)

            self._clear_pending_gate(st, choice)
            said = menu.display_name(iid) if (iid and menu) else ("naan" if lang != "nl" else "naan")
            return ResponsePlan(
                action=PlanAction.REPLY,
                reply=(f"Okay, {qty}x {said}." if lang != "nl" else f"Oké, {qty}x {said}."),
                lang=lang,
                pending_choice=None,
                pending_qty=qty,
                stt_hint=None,
                consumed=True,
                debug={"reason": "naan_variant_resolved_fast", "variant": variant, "added": bool(iid), "qty": qty},
            )

        # Menu-driven resolution for other variants (garlic/cheese/peshawari/chili/etc.)
        if menu:
            iid = _match_naan_from_menu(menu, transcript)
            if iid:
                st.order.add(iid, qty)
                self._clear_pending_gate(st, choice)
                said = menu.display_name(iid) if menu else "naan"
                return ResponsePlan(
                    action=PlanAction.REPLY,
                    reply=(f"Okay, {qty}x {said}." if lang != "nl" else f"Oké, {qty}x {said}."),
                    lang=lang,
                    pending_choice=None,
                    pending_qty=qty,
                    stt_hint=None,
                    consumed=True,
                    debug={"reason": "naan_variant_resolved_menu_match", "added": True, "qty": qty, "item_id": iid},
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
        # 1) Informational query example (generic, menu-driven)
        # ----------------------------------------------------------
        if tnorm and ((" menu " in t) or (" kaart " in t)) and any(
            x in tnorm for x in ("dish", "dishes", "gerechten", "recommend", "suggest", "popular", "bestseller", "top")
        ):
            picks = self._get_bestsellers(st, limit=3)
            if picks:
                items = ", ".join(picks)
                msg = (
                    f"Even kijken op de kaart. Populaire opties zijn {items}. Wat wil je?"
                    if lang == "nl"
                    else f"Let me check the menu for you. Popular options include {items}. What would you like?"
                )
            else:
                msg = "Wat heb je in gedachten?" if lang == "nl" else "What are you in the mood for?"
            return ResponsePlan(action=PlanAction.REPLY, reply=msg, lang=lang, debug={"reason": "menu_bestsellers"})

        # ----------------------------------------------------------
        # 2) Gate creation: VARIANT BUNDLE (Optima-style, max 2 asks)
        # If multiple items require a variant selection, ask up to 2 questions in one prompt.
        # One gate must be able to resolve multiple answers from a single user utterance.
        # ----------------------------------------------------------
        needs_biryani_variant = False
        biryani_qty = 1
        if " biryani " in t:
            has_biryani_variant = any(
                x in t
                for x in (
                    " chicken ", " lamb ", " vegetarian ", " veg ", " vegetarisch ",
                    " mix ", " mushroom ", " prawn ", " king prawn ", " garnaal ",
                    " tikka ",
                )
            )
            if not has_biryani_variant:
                needs_biryani_variant = True
                biryani_qty = self._qty_near_keyword(transcript, "biryani")

        needs_naan_variant = False
        naan_qty = 1
        if (" naan " in t) or (" nan " in t):
            has_naan_variant = any(
                x in t
                for x in (
                    " garlic ", " knoflook ",
                    " cheese ", " kaas ",
                    " keema ", " kheema ",
                    " peshawari ",
                    " plain ", " regular ", " normal ",
                    " gewoon ", " normaal ", " standaard ",
                )
            )
            if not has_naan_variant:
                needs_naan_variant = True
                naan_qty = max(self._qty_near_keyword(transcript, "naan"), self._qty_near_keyword(transcript, "nan"))

        if needs_biryani_variant or needs_naan_variant:
            # Build up to TWO questions per prompt
            questions: list[str] = []

            if needs_biryani_variant:
                questions.append(
                    "Could you please tell me which type of biryani you would like? We have Chicken, Lamb, and Vegetable Biryani."
                    if lang != "nl"
                    else "Welke biryani wilt u? We hebben kip, lam, en vegetarisch."
                )

            if needs_naan_variant:
                naan_q = self._ask_variant_from_menu(getattr(st, "menu", None), "naan", lang)
                # Keep Optima pacing: second question prefixed with "Also,"
                questions.append(
                    ("Also, " + naan_q) if lang != "nl" else ("En, " + naan_q)
                )

            questions = questions[:2]
            reply = " ".join([q.strip() for q in questions if q.strip()])

            # Store bundle requirements on state (engine-owned)
            bundle: dict[str, int] = {}
            if needs_biryani_variant:
                bundle["biryani_variant"] = max(1, int(biryani_qty or 1))
            if needs_naan_variant:
                bundle["nan_variant"] = max(1, int(naan_qty or 1))
            setattr(st, "pending_variant_bundle", bundle)

            stt_hint = (
                "User is selecting missing variants. Return only the chosen option words.\n"
                "Biryani: chicken, lamb, vegetable.\n"
                "Naan: plain, garlic, keema, cheese, peshawari (or any naan option said)."
                if lang != "nl"
                else
                "Gebruiker kiest ontbrekende varianten. Geef alleen de gekozen optie-woorden.\n"
                "Biryani: kip, lam, vegetarisch.\n"
                "Naan: plain, garlic, keema, cheese, peshawari (of een genoemde naan-optie)."
            )

            return ResponsePlan(
                action=PlanAction.CLARIFY,
                reply=("Certainly. " + reply) if lang != "nl" else ("Zeker. " + reply),
                lang=lang,
                pending_choice="variant_bundle",
                pending_qty=1,
                stt_hint=stt_hint,
                consumed=True,
                debug={"reason": "variant_bundle_gate_set", "bundle": bundle},
            )

        # ----------------------------------------------------------
        # Default: NOOP, let SessionController do deterministic ordering
        # ----------------------------------------------------------
        return ResponsePlan(action=PlanAction.NOOP, reply="", lang=lang, debug={"reason": "restaurant_noop_default"})

