# src/domains/emergency_abt.py
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from .base import (
    ActionType,
    CaseStatus,
    DomainAction,
    IntentFrame,
    Priority,
)


class ABTEmergencyDomain:
    """
    ABT (Alphabouwtechniek) Emergency Plumbing Domain.

    ADR-001:
      - Interpret: deterministic (regex/keyword)
      - Decide: deterministic policy -> DomainActions only
      - Apply: platform owns DB + tool execution + audit log
      - Render: platform owns final response; P0 must bypass LLM using OVERRIDE_REPLY text
    """

    domain_type = "service_emergency"

    # -----------------------------
    # P0 Safety Scripts (Render bypass LLM)
    # -----------------------------
    P0_SAFETY_SCRIPT = {
        "nl": "DIT IS EEN SPOEDGEVAL. Open ramen, gebruik geen schakelaars, verlaat het pand en bel direct 112 of de netbeheerder.",
        "en": "THIS IS AN EMERGENCY. Open windows, do not use light switches, leave the building immediately and call emergency services.",
        "tr": "BU BİR ACİL DURUMDUR. Pencereleri açın, elektrik şalterlerine dokunmayın, binayı hemen terk edin ve acil servisleri arayın.",
    }

    # -----------------------------
    # Dispatcher intent triggers (domain scoped)
    # -----------------------------
    DISPATCHER_TRIGGERS = {
        "nl": [
            r"\b(iemand\s+spreken|mens|medewerker|planner|dispatcher|doorverbinden|terugbellen|bel\s+me\s+terug|terugbel)\b",
        ],
        "en": [
            r"\b(speak\s+to\s+(a\s+)?person|human|operator|dispatcher|planner|transfer|call\s+me\s+back|callback)\b",
        ],
        "tr": [
            r"\b(biriyle\s+konuşmak\s+istiyorum|insan|yetkili|planlama|bağla(r\s+mısınız)?|beni\s+geri\s+ara|geri\s+ara)\b",
        ],
    }

    # -----------------------------
    # Emergency + plumbing triggers (NL/EN/TR)
    # Turkish plumbing terms are included here (ABT-only triggers).
    # -----------------------------
    EMERGENCY_TRIGGERS = {
        # P0 hazards
        "gas_smell": {
            "nl": [r"\b(gaslucht|gas\s+ruik|ruik\s+gas|gaslek|gas\s+lekt)\b"],
            "en": [r"\b(smell\s+gas|gas\s+leak|leaking\s+gas)\b"],
            "tr": [r"\b(gaz\s+kokusu|gaz\s+kaçağı)\b"],
        },
        "co_alarm": {
            "nl": [r"\b(co[- ]?alarm|koolmonoxide|koolstofmonoxide)\b"],
            "en": [r"\b(co\s+alarm|carbon\s+monoxide)\b"],
            "tr": [r"\b(karbonmonoksit|co\s+alarm)\b"],
        },
        "electrical_risk": {
            "nl": [r"\b(meterkast|stopcontact|kortsluit(ing)?|vonken|stroomkast|elektra)\b"],
            "en": [r"\b(fuse\s+box|breaker|outlet|sparks|short\s+circuit|electrical)\b"],
            "tr": [r"\b(sigorta\s+kutusu|priz|kısa\s+devre|kıvılcım|elektrik)\b"],
        },
        "water_present": {
            "nl": [r"\b(water|nat|plas|lekkage|lekt)\b"],
            "en": [r"\b(water|wet|leak|leaking)\b"],
            "tr": [r"\b(su|ıslak|sızıntı)\b"],
        },

        # Plumbing urgency (P1/P2)
        "flooding": {
            "nl": [r"\b(overstrom(ing)?|ondergelopen|water\s+staat|stroomt\s+binnen)\b"],
            "en": [r"\b(flooding|flooded|water\s+coming\s+in|overflowing)\b"],
            "tr": [r"\b(su\s+bas(ması)?|taştı|ev\s+su\s+aldı)\b"],
        },
        "active_leak": {
            "nl": [r"\b(leiding\s+gesprongen|spuit|stroomt|blijft\s+lopen|grote\s+lekkage)\b"],
            "en": [r"\b(burst\s+pipe|gushing|pouring|keeps\s+running|major\s+leak)\b"],
            "tr": [r"\b(boru\s+patladı|fışkırıyor|akıyor|büyük\s+sızıntı)\b"],
        },
        "no_water": {
            "nl": [r"\b(geen\s+water|water\s+doet\s+het\s+niet|helemaal\s+geen\s+water)\b"],
            "en": [r"\b(no\s+water|water\s+is\s+off|no\s+running\s+water)\b"],
            "tr": [r"\b(su\s+yok|sular\s+kesik|musluk(lar)?dan\s+su\s+gelmiyor)\b"],
        },
        "sewage_backflow": {
            "nl": [r"\b(riool|terugslag|wc\s+loopt\s+over|poepwater|vies\s+water)\b"],
            "en": [r"\b(sewage|backflow|toilet\s+overflow|waste\s+water)\b"],
            "tr": [r"\b(kanalizasyon|geri\s+tepme|tuvalet\s+taştı|pis\s+su)\b"],
        },

        # Regular/minor (P3)
        "minor_leak": {
            "nl": [r"\b(druppelt|kleine\s+lekkage|onder\s+de\s+gootsteen|sifon)\b"],
            "en": [r"\b(dripping|small\s+leak|under\s+the\s+sink|trap)\b"],
            "tr": [
                r"\b(damlıyor|küçük\s+sızıntı|lavabo\s+altı)\b",
                # Turkish plumbing words (ABT-only triggers)
                r"\b(tesisatçı|musluk|vana|gider|lavabo|tuvalet)\b",
            ],
        },
    }

    # Optional explicit language switch phrases
    LANGUAGE_SWITCH = {
        "nl": [r"\b(nederlands|dutch)\b"],
        "en": [r"\b(english)\b"],
        "tr": [r"\b(türkçe|turkce)\b"],
    }

    # -----------------------------
    # Helpers
    # -----------------------------
    @staticmethod
    def _norm(text: str) -> str:
        t = (text or "").strip().lower()
        t = re.sub(r"\s+", " ", t)
        return t

    @staticmethod
    def _match_any(patterns: List[str], text_norm: str) -> bool:
        return any(re.search(p, text_norm) for p in patterns)

    @staticmethod
    def _normalize_ctx_lang(ctx_lang: Optional[str]) -> Optional[str]:
        if not ctx_lang:
            return None
        x = ctx_lang.strip().lower()
        if x in ("nl", "dutch", "nl-nl"):
            return "nl"
        if x in ("en", "english", "en-us", "en-gb"):
            return "en"
        if x in ("tr", "turkish", "tr-tr"):
            return "tr"
        return None

    def _detect_lang(self, ctx: Dict[str, Any], utterance: str) -> str:
        # 1) Use ctx first (tenant default/session)
        ctx_lang = self._normalize_ctx_lang(ctx.get("lang") or ctx.get("default_language"))
        if ctx_lang:
            # allow explicit switch utterances to override
            for lang, pats in self.LANGUAGE_SWITCH.items():
                if self._match_any(pats, utterance):
                    return lang
            return ctx_lang

        # 2) Heuristic fallback
        if re.search(r"[çğıİöşü]", utterance):
            return "tr"
        if re.search(r"\b(please|emergency|call me back|burst pipe|flooding)\b", utterance):
            return "en"

        # explicit switch phrases (even without ctx)
        for lang, pats in self.LANGUAGE_SWITCH.items():
            if self._match_any(pats, utterance):
                return lang

        return "nl"

    @staticmethod
    def _true_flags(flags: Dict[str, bool]) -> List[str]:
        return [k for k, v in flags.items() if v]

    # -----------------------------
    # Interpret
    # -----------------------------
    def interpret(self, text: str, ctx: Dict[str, Any]) -> IntentFrame:
        utterance = self._norm(text)
        lang = self._detect_lang(ctx, utterance)

        dispatcher_intent = self._match_any(self.DISPATCHER_TRIGGERS.get(lang, []), utterance)

        flags: Dict[str, bool] = {}
        for flag_name, per_lang in self.EMERGENCY_TRIGGERS.items():
            pats = per_lang.get(lang, [])
            flags[flag_name] = self._match_any(pats, utterance) if pats else False

        return IntentFrame(
            lang=lang,
            utterance=utterance,
            dispatcher_intent=dispatcher_intent,
            emergency_flags=flags,
            confidence=0.85,
            meta={"domain": "abt_emergency", "tenant_ref": ctx.get("tenant_ref")},
        )

    # -----------------------------
    # Decide (deterministic policy -> actions only)
    # -----------------------------
    def decide(self, frame: IntentFrame, case_state: Dict[str, Any]) -> List[DomainAction]:
        f = frame.emergency_flags
        actions: List[DomainAction] = []

        gas = bool(f.get("gas_smell"))
        co = bool(f.get("co_alarm"))
        elec = bool(f.get("electrical_risk"))
        water = bool(f.get("water_present"))

        # P0: Gas OR CO OR (Electrical risk AND water present)
        if gas or co or (elec and water):
            actions.append(DomainAction(ActionType.SET_PRIORITY, {"priority": Priority.P0.value}))
            actions.append(DomainAction(ActionType.SET_STATUS, {"status": CaseStatus.ESCALATED.value}))
            actions.append(
                DomainAction(
                    ActionType.OVERRIDE_REPLY,
                    {
                        "mode": "SAFETY_SCRIPT",
                        "lang": frame.lang,
                        "text": self.P0_SAFETY_SCRIPT.get(frame.lang, self.P0_SAFETY_SCRIPT["en"]),
                    },
                )
            )
            actions.append(DomainAction(ActionType.REQUEST_DISPATCHER_CALLBACK, {"urgent": True}))
            actions.append(DomainAction(ActionType.SAVE_TRIAGE_FACTS, {"flags": self._true_flags(f), "priority": Priority.P0.value}))
            return actions  # short-circuit

        flooding = bool(f.get("flooding"))
        active_leak = bool(f.get("active_leak"))
        sewage = bool(f.get("sewage_backflow"))
        no_water = bool(f.get("no_water"))
        minor = bool(f.get("minor_leak"))

        # Priority assignment (tunable)
        if flooding or active_leak or sewage:
            priority = Priority.P1
            window_hours = 24
        elif no_water:
            priority = Priority.P2
            window_hours = 48
        elif minor:
            priority = Priority.P3
            window_hours = 24 * 7
        else:
            priority = Priority.P4
            window_hours = 24 * 14

        actions.append(DomainAction(ActionType.SET_PRIORITY, {"priority": priority.value}))
        actions.append(DomainAction(ActionType.SET_STATUS, {"status": CaseStatus.OPEN.value}))
        actions.append(DomainAction(ActionType.SAVE_TRIAGE_FACTS, {"flags": self._true_flags(f), "priority": priority.value}))

        # Dispatcher request (domain-scoped)
        if frame.dispatcher_intent:
            actions.append(DomainAction(ActionType.SET_STATUS, {"status": CaseStatus.CALLBACK_REQUESTED.value}))
            actions.append(DomainAction(ActionType.REQUEST_DISPATCHER_CALLBACK, {"urgent": priority in (Priority.P1, Priority.P2)}))
            actions.append(DomainAction(ActionType.REPLY_TEMPLATE, {"template": "DISPATCHER_CALLBACK_ACK", "lang": frame.lang, "priority": priority.value}))
            return actions

        # Scheduling request for P1–P3 (platform executes)
        if priority in (Priority.P1, Priority.P2, Priority.P3):
            actions.append(
                DomainAction(
                    ActionType.REQUEST_TOOL,
                    {
                        "name": "scheduling.free_busy",
                        "payload": {
                            "window_hours": window_hours,
                            "priority": priority.value,
                            "constraints": {"tenant_ref": "abt"},
                        },
                    },
                )
            )
            actions.append(DomainAction(ActionType.REPLY_TEMPLATE, {"template": "TRIAGE_THEN_PROPOSE_SLOTS", "lang": frame.lang, "priority": priority.value}))
        else:
            actions.append(DomainAction(ActionType.REPLY_TEMPLATE, {"template": "ADVICE_OFFER_CALLBACK", "lang": frame.lang, "priority": priority.value}))

        return actions


def build_abt_domain() -> ABTEmergencyDomain:
    return ABTEmergencyDomain()

