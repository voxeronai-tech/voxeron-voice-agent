from .types import ResponsePlan, PlanAction

class DispatcherEngine:
    def plan(self, state, transcript: str) -> ResponsePlan:
        t = (transcript or "").strip()

        # First message (no user input yet) => greeting owned here
        if not t:
            lang = getattr(state, "lang", "en")
            greet = (
                "Hi, this is Voxeron. Which service do you need?"
                if lang != "nl"
                else "Hoi, u spreekt met Voxeron. Met welke dienst kan ik u helpen?"
            )
            return ResponsePlan(action=PlanAction.REPLY, reply=greet, lang=lang)

        # ... existing routing rules (Indian food -> hotswap tenant, etc.)
        return ResponsePlan(action=PlanAction.NOOP, reply="", lang=getattr(state, "lang", "en"))