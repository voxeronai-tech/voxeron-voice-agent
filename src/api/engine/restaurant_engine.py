from __future__ import annotations

from typing import Any, List

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

    def plan(self, state: Any, transcript: str) -> ResponsePlan:
        st = state
        tnorm = norm_simple(transcript)

        # 1) Pending naan variant: answer spicy question + reprompt
        if getattr(st, "pending_choice", None) == "nan_variant" and getattr(st, "menu", None):
            if self._is_spicy_query(transcript) or self._looks_like_question(transcript):
                if st.lang == "nl":
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
                    reply=f"{info} {nan_variant_question(st.lang, verbose=False)}",
                    lang=st.lang,
                    pending_choice="nan_variant",
                    pending_qty=getattr(st, "pending_qty", 1),
                    debug={"reason": "naan_spicy_question_override"},
                )

        # 2) “What lamb dishes…” → crisp top-3 + ask preference
        if tnorm and "lamb" in tnorm and "menu" in tnorm and any(x in tnorm for x in ("dish", "dishes", "gerechten")):
            top3 = self._top3_lamb(st)
            items = ", ".join(top3)

            if st.lang == "nl":
                msg = f"Even kijken. We hebben bijvoorbeeld {items}. Zegt een van deze u iets?"
            else:
                msg = (
                    f"Let me check the menu for you. We have a few great lamb dishes like {items}. "
                    "Do any of those sound good?"
                )

            st.last_category = "lamb"
            st.last_category_items = top3
            return ResponsePlan(action=PlanAction.REPLY, reply=msg, lang=st.lang, debug={"reason": "top3_lamb"})

        # Default: let existing controller continue (for now)
        return ResponsePlan(action=PlanAction.NOOP, reply="", lang=getattr(st, "lang", "en"))
