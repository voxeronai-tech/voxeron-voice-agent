from .types import ResponsePlan, PlanAction


class DispatcherEngine:
    def plan(self, state, transcript: str) -> ResponsePlan:
        lang = getattr(state, "lang", "en") or "en"
        t = (transcript or "").strip()
        t_norm = t.lower()

        # ---- Connect-tick greeting (one-time) ----
        if not t:
            if getattr(state, "dispatcher_greeted", False):
                return ResponsePlan(action=PlanAction.NOOP, reply="", lang=lang)

            setattr(state, "dispatcher_greeted", True)

            greet = (
                "Hi, thank you for calling Vox8. Which service do you need?"
                if lang != "nl"
                else "Hoi, u spreekt met Vox Eight. Met welke dienst kan ik u helpen?"
            )
            return ResponsePlan(action=PlanAction.REPLY, reply=greet, lang=lang)

        # ---- Routing signals ----
        turkish = ["tesisat", "tesisatçı", "tamir", "sızınt", "boru", "su bas", "gaz kok"]
        plumber_nl = ["loodgieter", "lekkage", "spoed", "water", "leiding", "verstopping", "afvoer"]
        plumber_en = ["plumber", "leak", "burst", "pipe", "flood", "repair", "emergency"]

        food_nl = ["eten", "bestellen", "indiaas", "restaurant", "afhalen", "bezorgen"]
        food_en = ["food", "hungry", "order", "restaurant", "indian"]

        connect_reply = "Okay — connecting you now." if lang != "nl" else "Prima — ik verbind u nu door."

        if (
            any(k in t_norm for k in turkish)
            or any(k in t_norm for k in plumber_nl)
            or any(k in t_norm for k in plumber_en)
        ):
            return ResponsePlan(
                action=PlanAction.HOTSWAP_TENANT,
                reply=connect_reply,
                lang=lang,
                hotswap_tenant_ref="abt",
            )

        if any(k in t_norm for k in food_nl) or any(k in t_norm for k in food_en):
            return ResponsePlan(
                action=PlanAction.HOTSWAP_TENANT,
                reply=connect_reply,
                lang=lang,
                hotswap_tenant_ref="taj_mahal",
            )

        # ---- No match: ask again ----
        ask = (
            "Which service do you need, restaurant ordering or an emergency plumber?"
            if lang != "nl"
            else "Welke dienst heeft u nodig, eten bestellen of een spoed loodgieter?"
        )
        return ResponsePlan(action=PlanAction.CLARIFY, reply=ask, lang=lang)
