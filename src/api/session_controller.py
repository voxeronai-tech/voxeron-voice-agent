# src/api/session_controller.py
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Tuple

from fastapi import WebSocket

from .intent import (
    detect_language_intent,
    norm_simple,
    detect_generic_nan_request,
    detect_explicit_remove_intent,
)
from .menu_store import MenuSnapshot, MenuStore
from .tenant_manager import TenantManager, TenantConfig
from .policy import (
    SessionPolicyState,
    system_guard_for_llm,
    post_cart_followup,
)
from .services.openai_client import OpenAIClient

logger = logging.getLogger("taj-agent")

SESSION_CONTROLLER_VERSION = (
    "2026-01-03T-optima-flow-v5-full-dropin-name-extract-fix-lang-hysteresis-stronger-stt-softlock-retry"
)


class FlowState(str, Enum):
    IDLE = "idle"
    MENU_PROVIDED = "menu_provided"
    CART_UPDATED = "cart_updated"
    UPSELL_OFFERED = "upsell_offered"


@dataclass
class OrderState:
    items: Dict[str, int] = field(default_factory=dict)

    def add(self, item_id: str, qty: int) -> None:
        if qty <= 0:
            return
        self.items[item_id] = int(self.items.get(item_id, 0) + qty)

    def set_qty(self, item_id: str, qty: int) -> None:
        if qty <= 0:
            self.items.pop(item_id, None)
            return
        self.items[item_id] = int(qty)

    def summary(self, menu: MenuSnapshot) -> str:
        if not self.items:
            return ""
        parts: List[str] = []
        for item_id, qty in self.items.items():
            if int(qty or 0) <= 0:
                continue
            parts.append(f"{qty}x {menu.display_name(item_id)}")
        return ", ".join(parts)


@dataclass
class SessionState:
    tenant_ref: str = "default"
    tenant_id: str = ""
    tenant_name: str = ""
    tenant_cfg: Optional[TenantConfig] = None

    lang: str = "en"
    phase: str = "language_select"  # language_select | dispatcher | chat
    order: OrderState = field(default_factory=OrderState)

    is_processing: bool = False
    turn_id: int = 0

    last_activity_ts: float = 0.0
    heartbeat_task: Optional[asyncio.Task] = None

    proc_task: Optional[asyncio.Task] = None
    tts_task: Optional[asyncio.Task] = None

    locked_voice: Optional[str] = None
    locked_tts_instr: Optional[str] = None

    menu: Optional[MenuSnapshot] = None

    is_speaking: bool = False
    last_agent_speech_end_ts: float = 0.0

    # Naan disambiguation
    pending_choice: Optional[str] = None  # "nan_variant"
    pending_qty: int = 1
    nan_prompt_count: int = 0

    # Optima micro-flows
    pending_spice_item_id: Optional[str] = None
    pending_spice_qty: int = 0

    pending_fulfillment: bool = False
    fulfillment_mode: Optional[str] = None  # "pickup" / "delivery"

    pending_name: bool = False
    customer_name: Optional[str] = None

    # Offer/selection memory (fixes “Yes, make that two.”)
    offered_item_id: Optional[str] = None
    offered_label: Optional[str] = None

    # Language confidence/hysteresis (prevents flip-flop on one bad STT turn)
    lang_candidate: Optional[str] = None
    lang_streak: int = 0


QTY_MAP_NL = {"een": 1, "één": 1, "1": 1, "twee": 2, "2": 2, "drie": 3, "3": 3, "vier": 4, "4": 4}
QTY_MAP_EN = {"one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3, "four": 4, "4": 4}


def _extract_qty_first(text: str, lang: str) -> Optional[int]:
    t = norm_simple(text)
    toks = t.split()
    m = QTY_MAP_NL if lang == "nl" else QTY_MAP_EN
    for tok in toks:
        if tok in m:
            return int(m[tok])
    return None


def _looks_like_stt_prompt_dump(text: str) -> bool:
    t = norm_simple(text)
    if not t:
        return False
    if "menu vocabulary" in t or "languages" in t or "talen" in t:
        return True
    has_many_commas = (text.count(",") >= 5)
    has_order_verb = any(v in t for v in ["i want", "i would like", "add", "order", "ik wil", "graag", "bestel", "voeg"])
    if has_many_commas and not has_order_verb:
        return True
    return False


def parse_add_item(menu: MenuSnapshot, text: str, *, qty: int) -> List[Tuple[str, int]]:
    t = " " + norm_simple(text) + " "
    hits: List[Tuple[str, str]] = []
    for alias, item_id in menu.alias_map.items():
        a = alias.strip()
        if len(a) < 3:
            continue
        if f" {a} " in t:
            hits.append((a, item_id))
    hits.sort(key=lambda x: len(x[0]), reverse=True)

    chosen: List[Tuple[str, int]] = []
    used_item_ids = set()

    q = max(1, int(qty or 1))
    for _alias, item_id in hits:
        if item_id in used_item_ids:
            continue
        used_item_ids.add(item_id)
        chosen.append((item_id, q))
    return chosen


LLM_SYSTEM_BASE = """
You are a helpful restaurant ordering agent for the current tenant.

Rules:
- Always respond in the user's current language (lang).
- You must not invent menu items. Use MENU_CONTEXT only.
- You must remember the CURRENT_CART and never claim it's empty if it's not.
- You must NEVER suggest removing items unless the user explicitly asked to remove/cancel/delete.
- Keep replies concise and natural.

Output format:
Return JSON only:
{
  "reply": "text to say to user",
  "add": [{"item_name": "string", "qty": 1}],
  "remove": [{"item_name": "string", "qty": 1}]
}
If you don't want to add/remove anything, use empty arrays.
""".strip()


def _policy_guard_append(state: SessionState, system_text: str) -> str:
    try:
        ps = SessionPolicyState(lang=getattr(state, "lang", "en"))
        try:
            menu = getattr(state, "menu", None)
            order_obj = getattr(state, "order", None)
            items_dict = getattr(order_obj, "items", None)
            if isinstance(items_dict, dict) and menu is not None:
                for item_id, qty in items_dict.items():
                    try:
                        name = menu.display_name(item_id)
                    except Exception:
                        name = str(item_id)
                    ps.order.add(name, int(qty))
        except Exception:
            pass

        guard = system_guard_for_llm(ps)
        return (system_text or "").rstrip() + "\n\n" + guard.strip()
    except Exception:
        return system_text


def build_llm_messages(state: SessionState, user_text: str, menu_context: str) -> List[Dict[str, str]]:
    cart = state.order.summary(state.menu) if state.menu else ""
    cart_str = cart if cart else "Empty"

    sys = (
        LLM_SYSTEM_BASE
        + f"\n\nlang={state.lang}"
        + f"\nCURRENT_CART: [{cart_str}]"
        + f"\nMENU_CONTEXT:\n{menu_context}"
    )
    sys = _policy_guard_append(state, sys)
    return [{"role": "system", "content": sys}, {"role": "user", "content": user_text}]


async def llm_turn(oa: OpenAIClient, state: SessionState, user_text: str, menu_context: str) -> Dict[str, Any]:
    msgs = build_llm_messages(state, user_text, menu_context)
    txt = await oa.chat(msgs, temperature=0.2)
    try:
        return json.loads(txt)
    except Exception:
        return {"reply": txt, "add": [], "remove": []}


def _extract_nan_variant_keyword(text: str) -> Optional[str]:
    t = " " + norm_simple(text) + " "

    plain_like = [
        " plain ", " plainer ", " plainar ", " planar ", " plano ", " playn ", " plean ",
        " gewoon ", " normaal ", " standaard ", " regular ", " normal "
    ]
    if any(tok in t for tok in plain_like):
        return "plain"

    mapping = {
        "garlic": [" garlic ", " knoflook "],
        "butter": [" butter ", " boter "],
        "cheese": [" cheese ", " kaas "],
        "keema": [" keema ", " kheema "],
        "peshawari": [" peshawari "],
    }
    for canonical, toks in mapping.items():
        for tok in toks:
            if tok in t:
                return canonical
    return None


class SessionController:
    def __init__(
        self,
        *,
        state: SessionState,
        tenant_manager: TenantManager,
        menu_store: Optional[MenuStore],
        oa: OpenAIClient,
        tenant_rules_enabled: bool,
        tenant_stt_prompt_enabled: bool,
        tenant_tts_instructions_enabled: bool,
        choose_voice,
        choose_tts_instructions,
        enforce_output_language,
        send_user_text,
        send_agent_text,
        send_thinking,
        clear_thinking,
        tts_end,
    ):
        self.state = state
        self.tenant_manager = tenant_manager
        self.menu_store = menu_store
        self.oa = oa

        self.tenant_rules_enabled = bool(tenant_rules_enabled)
        self.tenant_stt_prompt_enabled = bool(tenant_stt_prompt_enabled)
        self.tenant_tts_instructions_enabled = bool(tenant_tts_instructions_enabled)

        self.choose_voice = choose_voice
        self.choose_tts_instructions = choose_tts_instructions
        self.enforce_output_language = enforce_output_language

        self.send_user_text = send_user_text
        self.send_agent_text = send_agent_text
        self.send_thinking = send_thinking
        self.clear_thinking = clear_thinking
        self.tts_end = tts_end

    # -------------------------
    # Small language helpers
    # -------------------------
    def _say_anything_else(self) -> str:
        return "Anything else you'd like to add?" if self.state.lang != "nl" else "Wil je nog iets toevoegen?"

    def _say_pickup_or_delivery(self) -> str:
        return "Is this for pickup or delivery?" if self.state.lang != "nl" else "Is dit om af te halen of om te bezorgen?"

    def _is_affirmative(self, text: str) -> bool:
        t = norm_simple(text)
        if self.state.lang == "nl":
            return t in {"ja", "zeker", "graag", "doe maar", "yes"} or t.startswith("ja ")
        return t in {"yes", "yeah", "yep", "sure", "please", "correct", "right"} or t.startswith("yes ")

    def _is_that_ref(self, text: str) -> bool:
        t = norm_simple(text)
        return any(x in t for x in ["that", "that one", "the one", "die", "die daar", "deze", "die ene", "spicy one", "the spicy one"])

    def _is_negation(self, text: str) -> bool:
        t = norm_simple(text)
        return t in {"no", "nope", "nah", "nee", "neen"} or t.startswith("no ") or t.startswith("nee ")

    # -------------------------
    # Language confidence + hysteresis (reply language)
    # -------------------------
    def _lang_scores(self, text: str) -> Dict[str, float]:
        """
        Cheap language scoring (not true probability).
        Used to decide reply language in bilingual real-world calls.
        """
        t = " " + norm_simple(text) + " "
        toks = t.split()

        nl = {
            "ik", "ben", "mijn", "naam", "niet", "afhalen", "bezorgen", "graag",
            "welke", "wat", "een", "twee", "drie", "vier", "ja", "nee", "op", "voor",
            "dit", "deze", "dat", "is"
        }
        en = {
            "i", "my", "name", "please", "pickup", "pick", "up", "delivery", "would", "like",
            "which", "what", "one", "two", "three", "four", "yes", "no", "for",
            "this", "that", "is"
        }
        tr = {
            "ben", "adim", "adım", "ismim", "lutfen", "lütfen", "evet", "hayir", "hayır", "adres", "acil"
        }

        def score(langset: set[str]) -> float:
            s = 0.0
            for w in toks:
                if w in langset:
                    s += 2.0
            # phrase bonuses
            if " my name " in t or " name is " in t:
                s += 4.0
            if " mijn naam " in t or " ik ben " in t:
                s += 4.0
            if " pickup " in t or " delivery " in t:
                s += 2.0
            if " afhalen " in t or " bezorgen " in t:
                s += 2.0
            return s

        return {"nl": score(nl), "en": score(en), "tr": score(tr)}

    def _allow_auto_lang_switch_now(self) -> bool:
        """
        If we're inside a micro-flow (name/pickup/spice/naan-choice), do NOT auto-switch reply language.
        Users often answer short fragments ("pickup", "Marcel") that STT can mis-tag.
        Explicit switching via detect_language_intent is still allowed.
        """
        st = self.state
        if st.pending_name:
            return False
        if st.pending_fulfillment:
            return False
        if st.pending_spice_item_id:
            return False
        if st.pending_choice:
            return False
        return True

    def _maybe_update_reply_language(self, transcript: str) -> None:
        """
        Switch reply language only on strong evidence, with hysteresis.
        Prevent one bad STT turn (short sentence) from flipping the whole session.
        """
        st = self.state

        if not self._allow_auto_lang_switch_now():
            st.lang_candidate = None
            st.lang_streak = 0
            return

        cfg = st.tenant_cfg
        supported = set((cfg.supported_langs if cfg else ("en", "nl", "tr")))

        scores = self._lang_scores(transcript)
        scores = {k: v for k, v in scores.items() if k in supported}
        if not scores:
            return

        items = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        best_lang, best_score = items[0]
        runner_score = items[1][1] if len(items) > 1 else 0.0

        if best_score <= 0.0:
            return

        tn = norm_simple(transcript)
        tok_count = len(tn.split())

        # Absolute minimum evidence (prevents flip on tiny phrases)
        # Example: "Dit is een pick-up" -> 4 tokens, can be wrong-STT. Require stronger proof.
        min_score = 8.0 if tok_count <= 4 else 6.0
        if best_score < min_score:
            st.lang_candidate = None
            st.lang_streak = 0
            return

        ratio = (best_score / max(0.1, runner_score)) if runner_score > 0 else 999.0
        strong = ratio >= 2.0
        ultra = ratio >= 3.5

        if not strong:
            st.lang_candidate = None
            st.lang_streak = 0
            return

        if best_lang == st.lang:
            st.lang_candidate = None
            st.lang_streak = 0
            return

        if st.lang_candidate == best_lang:
            st.lang_streak += 1
        else:
            st.lang_candidate = best_lang
            st.lang_streak = 1

        # For short utterances require 3 strong hits; otherwise 2 strong hits; or 1 ultra-hit.
        need = 3 if tok_count <= 4 else 2
        if ultra or st.lang_streak >= need:
            st.lang = best_lang
            st.lang_candidate = None
            st.lang_streak = 0

    # -------------------------
    # Name extraction (FIXED)
    # -------------------------
    def _extract_customer_name(self, text: str) -> Optional[str]:
        """
        Extract a name from common phrases (EN/NL), robust to punctuation.
        """
        raw = (text or "").strip()
        if not raw:
            return None

        # strip trailing punctuation that breaks $-anchored regex matches
        raw_clean = raw.strip().strip(" .,:;!?")
        tn = norm_simple(raw_clean)

        bad = {
            "yes", "yeah", "ok", "okay", "sure", "please", "thanks", "thank you",
            "ja", "oke", "oké", "prima", "goed", "dank je", "dankjewel", "zeker"
        }
        if tn in bad:
            return None

        patterns = [
            r"\bmy name is\s+([a-zA-Z][a-zA-Z'\- ]{0,40})$",
            r"\bname is\s+([a-zA-Z][a-zA-Z'\- ]{0,40})$",
            r"\byou can put (?:the )?name\s+([a-zA-Z][a-zA-Z'\- ]{0,40})$",
            r"\bput (?:the )?name\s+([a-zA-Z][a-zA-Z'\- ]{0,40})$",
            r"\bik ben\s+([a-zA-Z][a-zA-Z'\- ]{0,40})$",
            r"\bmijn naam is\s+([a-zA-Z][a-zA-Z'\- ]{0,40})$",
            r"\bnaam is\s+([a-zA-Z][a-zA-Z'\- ]{0,40})$",
            r"\bop naam\s+([a-zA-Z][a-zA-Z'\- ]{0,40})$",
        ]

        for pat in patterns:
            m = re.search(pat, raw_clean, flags=re.IGNORECASE)
            if m:
                name = (m.group(1) or "").strip(" .,:;!?")
                name = " ".join(name.split())
                if 1 <= len(name) <= 40:
                    parts = name.split()
                    return " ".join(parts[:2])

        # fallback: short answer (<=3 words) is probably a name
        if len(raw_clean.split()) <= 3 and len(raw_clean) <= 40:
            return raw_clean

        return None

    # -------------------------
    # Dynamic menu helpers
    # -------------------------
    def _is_nan_item(self, menu: MenuSnapshot, item_id: str) -> bool:
        dn = (menu.display_name(item_id) or "").lower()
        return ("naan" in dn) or ("nan" in dn) or ("naam" in dn)

    def _is_butter_chicken(self, menu: MenuSnapshot, item_id: str) -> bool:
        dn = (menu.display_name(item_id) or "").lower()
        return "butter chicken" in dn

    def _naan_options_from_menu(self, menu: MenuSnapshot) -> List[Tuple[str, str]]:
        if not menu:
            return []
        items: List[Tuple[str, str]] = []
        for _name, iid in menu.name_choices:
            if not self._is_nan_item(menu, iid):
                continue
            label = (menu.display_name(iid) or "").strip()
            if label:
                items.append((label, iid))
        if not items:
            return []

        prefs = [
            ("plain", ["plain", "regular", "normal", "gewoon", "standaard"]),
            ("garlic", ["garlic", "knoflook"]),
            ("butter", ["butter", "boter"]),
            ("cheese", ["cheese", "kaas"]),
            ("keema", ["keema", "kheema"]),
            ("peshawari", ["peshawari"]),
        ]

        def score(label: str) -> int:
            ll = label.lower()
            for i, (_k, toks) in enumerate(prefs):
                if any(t in ll for t in toks):
                    return 100 - i
            return 0

        items.sort(key=lambda x: (score(x[0]), -len(x[0])), reverse=True)
        return items

    def _naan_optima_prompt(self, *, list_mode: str = "short") -> str:
        st = self.state
        menu = st.menu
        if not menu:
            return "Which naan would you like, plain or garlic?" if st.lang != "nl" else "Welke naan wil je, plain of garlic?"

        opts = self._naan_options_from_menu(menu)
        if not opts:
            return "Which naan would you like, plain or garlic?" if st.lang != "nl" else "Welke naan wil je, plain of garlic?"

        max_n = 2 if list_mode == "short" else 4

        picked: List[Tuple[str, str]] = []

        def add_if_contains(tokens: List[str]) -> None:
            for (label, iid) in opts:
                l = label.lower()
                if any(t in l for t in tokens):
                    if (label, iid) not in picked:
                        picked.append((label, iid))
                        return

        add_if_contains(["plain", "regular", "normal", "gewoon", "standaard"])
        add_if_contains(["garlic", "knoflook"])
        add_if_contains(["butter", "boter"])
        add_if_contains(["cheese", "kaas"])

        for o in opts:
            if o not in picked:
                picked.append(o)
            if len(picked) >= max_n:
                break

        labels = [p[0] for p in picked[:max_n]]

        if st.lang == "nl":
            if len(labels) == 1:
                return f"We hebben {labels[0]}. Wil je die?"
            if len(labels) == 2:
                return f"Wil je {labels[0]} of {labels[1]}?"
            return f"We hebben bijvoorbeeld {', '.join(labels[:-1])}, en {labels[-1]}. Welke wil je?"
        else:
            if len(labels) == 1:
                return f"We have {labels[0]}. Would you like that?"
            if len(labels) == 2:
                return f"Would you like {labels[0]} or {labels[1]}?"
            return f"We have a few options like {', '.join(labels[:-1])}, and {labels[-1]}. Which would you prefer?"

    def _find_naan_item_for_variant(self, menu: MenuSnapshot, variant: str) -> Optional[str]:
        if not menu or not variant:
            return None
        v = variant.lower().strip()
        opts = self._naan_options_from_menu(menu)
        if not opts:
            return None

        best: Optional[str] = None
        best_score = -1
        for label, iid in opts:
            ll = label.lower()
            s = 0
            if v in ll:
                s += 10
            if v == "plain" and any(t in ll for t in ["plain", "regular", "normal", "gewoon", "standaard"]):
                s += 12
            if v == "garlic" and any(t in ll for t in ["garlic", "knoflook"]):
                s += 12
            if v == "butter" and any(t in ll for t in ["butter", "boter"]):
                s += 12
            if s > best_score:
                best_score = s
                best = iid

        return best if best_score >= 0 else None

    def _parse_spice_pref(self, text: str) -> Optional[str]:
        t = norm_simple(text)
        if any(x in t for x in ["mild", "not spicy", "niet pittig", "zacht"]):
            return "mild"
        if any(x in t for x in ["medium", "gemiddeld", "medium pittig"]):
            return "medium"
        if any(x in t for x in ["spicy", "hot", "very spicy", "heet", "pittig", "heel pittig"]):
            return "spicy"
        return None

    def _parse_fulfillment(self, text: str) -> Optional[str]:
        t = norm_simple(text)
        if any(x in t for x in ["pickup", "pick up", "afhalen", "haal ik", "kom het halen", "voor pickup", "voor pick-up"]):
            return "pickup"
        if any(x in t for x in ["delivery", "deliver", "bezorgen", "bezorging"]):
            return "delivery"
        return None

    def _is_dispatcher(self) -> bool:
        if self.state.phase == "dispatcher":
            return True
        cfg = self.state.tenant_cfg
        if cfg and getattr(cfg, "domain_type", None) == "dispatcher":
            return True
        if self.state.tenant_ref == "voxeron_main":
            return True
        return False

    def _dispatcher_route(self, text: str) -> Optional[str]:
        t = norm_simple(text)

        turkish = ["tesisat", "tesisatçı", "tamir", "sızınt", "boru", "su bas", "gaz kok"]
        plumber_nl = ["loodgieter", "lekkage", "spoed", "water", "leiding", "verstopping", "afvoer"]
        plumber_en = ["plumber", "leak", "burst", "pipe", "flood", "repair", "emergency"]
        food_nl = ["eten", "bestellen", "indiaas", "restaurant", "afhalen", "bezorgen"]
        food_en = ["food", "hungry", "order", "restaurant", "indian"]

        if any(k in t for k in turkish):
            return "abt"
        if any(k in t for k in plumber_nl) or any(k in t for k in plumber_en):
            return "abt"
        if any(k in t for k in food_nl) or any(k in t for k in food_en):
            return "taj_mahal"

        return None

    async def _load_tenant_context(self, tenant_ref: str) -> None:
        st = self.state

        st.locked_voice = None
        st.locked_tts_instr = None

        st.tenant_ref = tenant_ref

        try:
            st.tenant_cfg = self.tenant_manager.load_tenant(tenant_ref)
        except Exception:
            st.tenant_cfg = None

        st.lang = (st.tenant_cfg.base_language if st.tenant_cfg else st.lang) or st.lang

        if tenant_ref == "voxeron_main" or (st.tenant_cfg and getattr(st.tenant_cfg, "domain_type", None) == "dispatcher"):
            st.menu = None
            st.tenant_id = ""
            st.tenant_name = getattr(st.tenant_cfg, "tenant_name", "Voxeron") if st.tenant_cfg else "Voxeron"
            return

        snap: Optional[MenuSnapshot] = None
        if self.menu_store:
            snap = await self.menu_store.get_snapshot(tenant_ref, lang="en")
        st.menu = snap
        if snap:
            st.tenant_id = snap.tenant_id
            st.tenant_name = snap.tenant_name

    async def _speak(self, ws: WebSocket, text: str) -> None:
        await self.send_agent_text(ws, text)
        await self.stream_tts_mp3(ws, text)

    async def stream_tts_mp3(self, ws: WebSocket, text: str) -> None:
        st = self.state
        if st.tts_task and not st.tts_task.done():
            st.tts_task.cancel()

        async def _run() -> None:
            st.is_speaking = True
            try:
                if not st.locked_voice:
                    st.locked_voice = self.choose_voice(st.lang, st)
                if self.tenant_tts_instructions_enabled and st.locked_tts_instr is None:
                    st.locked_tts_instr = self.choose_tts_instructions(st.lang, st) or ""
                voice = st.locked_voice
                instr = st.locked_tts_instr or ""
                audio = await self.oa.tts_mp3_bytes(text, voice, instr)
                CHUNK = 12000
                for i in range(0, len(audio), CHUNK):
                    await ws.send_bytes(audio[i:i + CHUNK])
                await self.tts_end(ws)
            except asyncio.CancelledError:
                try:
                    await self.tts_end(ws)
                except Exception:
                    pass
                return
            finally:
                st.is_speaking = False
                st.last_agent_speech_end_ts = time.time()
                st.last_activity_ts = st.last_agent_speech_end_ts

        st.tts_task = asyncio.create_task(_run())
        try:
            await st.tts_task
        except asyncio.CancelledError:
            return

    def _lamb_top3(self, menu: MenuSnapshot) -> List[Tuple[str, str]]:
        if not menu:
            return []
        preferred = [
            "lamb karahi",
            "lamb pasanda",
            "lamb biryani",
            "lamb tikka",
            "lamb curry",
            "lamb dhansak",
            "korma",
            "madras",
        ]

        lambs: List[Tuple[str, str]] = []
        for _name, iid in menu.name_choices:
            dn = (menu.display_name(iid) or "").strip()
            dn_l = dn.lower()
            if "lamb" in dn_l or "lam" in dn_l:
                lambs.append((dn, iid))

        if not lambs:
            return []

        picked: List[Tuple[str, str]] = []
        for p in preferred:
            for dn, iid in lambs:
                if p in dn.lower() and (dn, iid) not in picked:
                    picked.append((dn, iid))
                if len(picked) >= 3:
                    return picked

        for dn, iid in lambs:
            if (dn, iid) not in picked:
                picked.append((dn, iid))
            if len(picked) >= 3:
                break
        return picked

    def _is_spicy_query(self, text: str) -> bool:
        t = norm_simple(text)
        return any(x in t for x in ["spicy", "very spicy", "hot", "heet", "pittig", "heel pittig", "spicy one", "the spicy one"])

    def _is_naan_options_question(self, text: str) -> bool:
        t = norm_simple(text)
        return any(
            x in t
            for x in [
                "what naan",
                "naan options",
                "all naan options",
                "any other naan",
                "what else naan",
                "are those the only",
                "only naan",
                "variety of naan",
                "which naan",
                "welke naan",
                "soorten naan",
                "alle naan opties",
                "nog meer naan",
                "andere naan",
            ]
        )

    def _is_qty_change_for_naan(self, text: str) -> Optional[int]:
        t = norm_simple(text)
        if "naan" not in t and "nan" not in t and "naam" not in t:
            return None
        q = _extract_qty_first(text, "en") or _extract_qty_first(text, "nl")
        if q and any(x in t for x in ["make it", "change it", "maak er", "doe er", "in plaats van", "instead of"]):
            return int(q)
        return None

    def _find_any_naan_in_order(self) -> List[str]:
        st = self.state
        if not st.menu:
            return []
        out: List[str] = []
        for iid, qty in st.order.items.items():
            if int(qty or 0) <= 0:
                continue
            if self._is_nan_item(st.menu, iid):
                out.append(iid)
        return out

    async def process_utterance(self, ws: WebSocket, pcm: bytes) -> None:
        st = self.state
        if st.is_processing:
            return
        st.is_processing = True
        st.turn_id += 1

        try:
            await self.send_thinking(ws)

            # -------- STT lang selection (tenant-aware) --------
            stt_mode = (getattr(st.tenant_cfg, "stt_mode", None) or "soft_lock").strip().lower()
            if stt_mode not in ("locked", "auto", "soft_lock"):
                stt_mode = "soft_lock"

            if stt_mode == "auto":
                stt_lang = None
            else:
                stt_lang = st.lang if st.lang in ("en", "nl", "tr") else None

            stt_prompt = None
            if self.tenant_stt_prompt_enabled and st.tenant_cfg and getattr(st.tenant_cfg, "stt_prompt_base", None):
                stt_prompt = str(st.tenant_cfg.stt_prompt_base)

            try:
                transcript = await self.oa.transcribe_pcm(pcm, stt_lang, prompt=stt_prompt)

                # Soft guard: if forced lang seems clearly wrong, retry once with lang=None (auto) and no prompt.
                if stt_mode in ("locked", "soft_lock") and stt_lang in ("en", "nl"):
                    tn = norm_simple(transcript or "")
                    toks = set(tn.split())

                    dutch_markers = {"ik", "ben", "het", "niet", "mijn", "naam", "afhalen", "bezorgen", "dit"}
                    en_markers = {"i", "my", "name", "please", "pickup", "delivery", "would", "like", "this"}

                    short = len(tn.split()) <= 10

                    if stt_lang == "en":
                        looks_nl = (len(toks & dutch_markers) >= 2) and (len(toks & en_markers) == 0)
                        if looks_nl and short:
                            retry = await self.oa.transcribe_pcm(pcm, None, prompt=None)
                            if retry and retry.strip():
                                transcript = retry

                    if stt_lang == "nl":
                        looks_en = (len(toks & en_markers) >= 2) and (len(toks & dutch_markers) == 0)
                        if looks_en and short:
                            retry = await self.oa.transcribe_pcm(pcm, None, prompt=None)
                            if retry and retry.strip():
                                transcript = retry

            except Exception:
                transcript = ""

            if not transcript or not transcript.strip():
                await self.clear_thinking(ws)
                msg = "Sorry — I didn’t catch that. Could you repeat?" if st.lang != "nl" else "Sorry — ik verstond het niet. Kun je het herhalen?"
                await self._speak(ws, msg)
                return

            st.last_activity_ts = time.time()
            transcript = transcript.strip()
            logger.info("STT: %s", transcript)
            await self.send_user_text(ws, transcript)

            # -------- Reply language confidence (bilingual safe) --------
            self._maybe_update_reply_language(transcript)

            # Explicit language switching still allowed (e.g., user says "Nederlands")
            decision = detect_language_intent(
                transcript,
                phase=st.phase,
                current_lang=st.lang,
                allow_auto_detect=True,
            )
            if decision.target and decision.target != st.lang:
                st.lang = decision.target
                st.lang_candidate = None
                st.lang_streak = 0

            # Dispatcher handling
            if self._is_dispatcher():
                target = self._dispatcher_route(transcript)
                if not target:
                    await self.clear_thinking(ws)
                    await self._speak(ws, "Hi, this is Voxeron. Which service do you need?")
                    return

                await self.clear_thinking(ws)

                if target == "taj_mahal":
                    await self._speak(ws, "Okay — connecting you now." if st.lang != "nl" else "Prima — ik verbind u nu door.")
                    logger.info("[hot_swap] from=%s to=%s", st.tenant_ref, target)

                    await self._load_tenant_context(target)
                    st.phase = "chat"

                    taj_greet = (
                        "Good evening, Taj Mahal Bussum. How can I help you today?"
                        if st.lang != "nl"
                        else "Goedenavond, Taj Mahal Bussum. Hoe kan ik u helpen?"
                    )
                    await self._speak(ws, taj_greet)
                    return

                if target == "abt":
                    await self._speak(ws, "Okay — connecting you now." if st.lang != "nl" else "Prima — ik verbind u nu door.")
                    logger.info("[hot_swap] from=%s to=%s", st.tenant_ref, target)
                    await self._load_tenant_context(target)
                    st.phase = "chat"
                    await self._speak(ws, "Alphabouwtechniek. Wat is er aan de hand?")
                    return

            # Restaurant mode
            if _looks_like_stt_prompt_dump(transcript):
                await self.clear_thinking(ws)
                msg = (
                    "Begrepen. Zeg gewoon wat je wilt bestellen, bijvoorbeeld: ‘twee butter chicken en één naan’."
                    if st.lang == "nl"
                    else "Got it. Just tell me what you'd like to order, for example: ‘two butter chicken and one naan’."
                )
                await self._speak(ws, msg)
                return

            tnorm = norm_simple(transcript)

            # -------------------------
            # Qty change: "make it one naan"
            # -------------------------
            if st.menu:
                q_change = self._is_qty_change_for_naan(transcript)
                if q_change is not None:
                    naans = self._find_any_naan_in_order()
                    await self.clear_thinking(ws)
                    if not naans:
                        st.pending_choice = "nan_variant"
                        st.pending_qty = max(1, int(q_change))
                        await self._speak(ws, self._naan_optima_prompt(list_mode="short"))
                        return

                    if len(naans) == 1:
                        st.order.set_qty(naans[0], int(q_change))
                        await self._speak(ws, "Alright." if st.lang != "nl" else "Helemaal goed.")
                        return

                    st.pending_choice = "nan_variant"
                    st.pending_qty = max(1, int(q_change))
                    await self._speak(ws, "Which naan should I change, plain or garlic?" if st.lang != "nl" else "Welke naan zal ik aanpassen, plain of garlic?")
                    return

            # -------------------------
            # Fix: accept offered item with "spicy one/that one" and qty
            # -------------------------
            if st.offered_item_id and st.menu:
                qty = _extract_qty_first(transcript, "en") or _extract_qty_first(transcript, "nl") or 1
                if (
                    self._is_affirmative(transcript)
                    or self._is_that_ref(transcript)
                    or any(x in tnorm for x in ["make that", "do that", "maak dat", "die maar", "i'll take", "ill take", "i will take"])
                ):
                    st.order.add(st.offered_item_id, int(qty))
                    added_name = st.offered_label or st.menu.display_name(st.offered_item_id)
                    st.offered_item_id = None
                    st.offered_label = None

                    await self.clear_thinking(ws)

                    if st.fulfillment_mode and st.customer_name:
                        await self._speak(ws, f"Got it — {qty}x {added_name}. {self._say_anything_else()}")
                        return

                    if not st.fulfillment_mode:
                        st.pending_fulfillment = True
                        await self._speak(ws, f"Got it — {qty}x {added_name}. {self._say_pickup_or_delivery()}")
                        return

                    if st.fulfillment_mode == "pickup" and not st.customer_name:
                        st.pending_name = True
                        await self._speak(ws, f"Got it — {qty}x {added_name}. What name should I put the order under?")
                        return

                    await self._speak(ws, f"Got it — {qty}x {added_name}. {self._say_anything_else()}")
                    return

            # -------- Pending spice preference --------
            if st.pending_spice_item_id and st.menu:
                pref = self._parse_spice_pref(transcript)
                await self.clear_thinking(ws)
                if not pref:
                    msg = "Certainly. Would you like your Butter Chicken mild, medium, or spicy?" if st.lang != "nl" else "Zeker. Wil je de Butter Chicken mild, medium, of pittig?"
                    await self._speak(ws, msg)
                    return

                st.pending_spice_item_id = None
                st.pending_spice_qty = 0

                if st.pending_choice == "nan_variant":
                    await self._speak(ws, self._naan_optima_prompt(list_mode="short"))
                    return

                if not st.fulfillment_mode:
                    st.pending_fulfillment = True
                    await self._speak(ws, self._say_pickup_or_delivery())
                    return

                await self._speak(ws, self._say_anything_else())
                return

            # -------- Pending fulfillment (do NOT re-ask if already set) --------
            if st.pending_fulfillment:
                mode = self._parse_fulfillment(transcript)
                await self.clear_thinking(ws)
                if not mode:
                    await self._speak(ws, self._say_pickup_or_delivery())
                    return

                st.pending_fulfillment = False
                st.fulfillment_mode = mode

                if mode == "pickup":
                    if st.customer_name:
                        await self._speak(ws, f"Great. {self._say_anything_else()}")
                        return
                    st.pending_name = True
                    msg = "Great. What name should I put the order under?" if st.lang != "nl" else "Prima. Op welke naam mag ik de bestelling zetten?"
                    await self._speak(ws, msg)
                    return

                msg = "Okay. What is the delivery address, please?" if st.lang != "nl" else "Oké. Wat is het bezorgadres?"
                await self._speak(ws, msg)
                return

            # -------- Pending name --------
            if st.pending_name:
                await self.clear_thinking(ws)
                if st.customer_name:
                    st.pending_name = False
                    await self._speak(ws, self._say_anything_else())
                    return

                name = self._extract_customer_name(transcript)
                if not name:
                    msg = "Could you please tell me your name?" if st.lang != "nl" else "Wat is je naam?"
                    await self._speak(ws, msg)
                    return

                st.customer_name = name
                st.pending_name = False

                msg = f"Thank you, {st.customer_name}. {self._say_anything_else()}" if st.lang != "nl" else f"Dank je, {st.customer_name}. {self._say_anything_else()}"
                await self._speak(ws, msg)
                return

            # Lamb dishes question
            if st.menu and ("lamb dishes" in tnorm or "lamgerechten" in tnorm or tnorm.strip() == "lamb dishes"):
                picks = self._lamb_top3(st.menu)
                names = [dn for dn, _iid in picks]
                await self.clear_thinking(ws)
                if st.lang == "nl":
                    msg = "Ik kijk even voor je. " + (f"We hebben bijvoorbeeld {', '.join(names)}. Welke klinkt goed?" if names else "We hebben verschillende lamsgerechten. Welke bedoel je?")
                else:
                    msg = "Let me check the menu for you. " + (f"We have a few great lamb dishes like {', '.join(names)}. Do any of those sound good?" if names else "We have a few lamb dishes. Which one are you looking for?")
                await self._speak(ws, msg)
                return

            # Spicy question after lamb list: offer one item and set offered_item_id
            if st.menu and self._is_spicy_query(transcript) and not st.pending_choice:
                picks = self._lamb_top3(st.menu)
                spicy_order = ["karahi", "madras", "vindaloo", "dhansak", "curry", "korma", "pasanda", "biryani", "tikka"]
                chosen: Optional[Tuple[str, str]] = None
                for key in spicy_order:
                    for dn, iid in picks:
                        if key in dn.lower():
                            chosen = (dn, iid)
                            break
                    if chosen:
                        break
                if not chosen and picks:
                    chosen = picks[0]

                await self.clear_thinking(ws)
                if chosen:
                    st.offered_item_id = chosen[1]
                    st.offered_label = chosen[0]
                    msg = f"Usually the spiciest: {chosen[0]}. Would you like that?" if st.lang != "nl" else f"Meestal het pittigst: {chosen[0]}. Wil je die?"
                    await self._speak(ws, msg)
                    return

            # Pending naan choice
            if st.pending_choice == "nan_variant" and st.menu:
                qty_hint = _extract_qty_first(transcript, "en") or _extract_qty_first(transcript, "nl")
                if qty_hint and qty_hint > 0:
                    st.pending_qty = int(qty_hint)

                if self._is_naan_options_question(transcript):
                    await self.clear_thinking(ws)
                    await self._speak(ws, self._naan_optima_prompt(list_mode="few"))
                    return

                if self._is_spicy_query(transcript):
                    await self.clear_thinking(ws)
                    info = (
                        "Naan is usually not spicy. Garlic can taste more seasoned, but it isn’t really hot."
                        if st.lang != "nl"
                        else "Naan is meestal niet pittig. Garlic kan wat kruidiger smaken, maar het is niet echt heet."
                    )
                    await self._speak(ws, f"{info} {self._naan_optima_prompt(list_mode='short')}")
                    return

                variant = _extract_nan_variant_keyword(transcript)

                if not variant:
                    if any(x in tnorm for x in ["naan", "nan", "naam"]):
                        variant = "plain"

                if not variant:
                    await self.clear_thinking(ws)
                    await self._speak(ws, self._naan_optima_prompt(list_mode="short"))
                    return

                iid = self._find_naan_item_for_variant(st.menu, variant)
                if not iid:
                    await self.clear_thinking(ws)
                    await self._speak(ws, self._naan_optima_prompt(list_mode="short"))
                    return

                st.order.add(iid, max(1, int(st.pending_qty or 1)))
                st.pending_choice = None
                st.pending_qty = 1
                st.nan_prompt_count = 0

                await self.clear_thinking(ws)

                if not st.fulfillment_mode:
                    st.pending_fulfillment = True
                    await self._speak(ws, self._say_pickup_or_delivery())
                    return

                await self._speak(ws, self._say_anything_else())
                return

            # Deterministic add logic
            add_qty = (_extract_qty_first(transcript, "en") or _extract_qty_first(transcript, "nl") or 1)
            effective_qty = add_qty

            cart_before = st.order.summary(st.menu) if st.menu else ""
            added_any = False
            added_ids: List[str] = []

            if st.menu:
                adds = parse_add_item(st.menu, transcript, qty=effective_qty)

                mentions_nan = (" naan " in (" " + tnorm + " ")) or detect_generic_nan_request(transcript)
                variant = _extract_nan_variant_keyword(transcript)
                has_variant = bool(variant)

                naan_opts = self._naan_options_from_menu(st.menu)
                naan_ambiguous = len(naan_opts) >= 2

                if self._is_naan_options_question(transcript):
                    await self.clear_thinking(ws)
                    await self._speak(ws, self._naan_optima_prompt(list_mode="few"))
                    return

                if mentions_nan and naan_ambiguous and (not has_variant):
                    non_nan_hits: List[Tuple[str, int]] = []
                    for item_id, qty in adds:
                        if not self._is_nan_item(st.menu, item_id):
                            non_nan_hits.append((item_id, qty))

                    for item_id, qty in non_nan_hits:
                        st.order.add(item_id, qty)
                        added_any = True
                        added_ids.append(item_id)

                    st.pending_choice = "nan_variant"
                    st.pending_qty = max(1, int(effective_qty or 1))
                    st.nan_prompt_count = 0

                    if any(self._is_butter_chicken(st.menu, iid) for iid in added_ids):
                        st.pending_spice_item_id = next(iid for iid in added_ids if self._is_butter_chicken(st.menu, iid))
                        st.pending_spice_qty = int(effective_qty or 1)
                        await self.clear_thinking(ws)
                        await self._speak(ws, "Certainly. And would you like your Butter Chicken mild or spicy?")
                        return

                    await self.clear_thinking(ws)
                    await self._speak(ws, self._naan_optima_prompt(list_mode="short"))
                    return

                if mentions_nan and has_variant and st.menu:
                    iid = self._find_naan_item_for_variant(st.menu, variant or "")
                    if iid:
                        st.order.add(iid, max(1, int(effective_qty or 1)))
                        added_any = True
                        added_ids.append(iid)
                        adds = [(x, q) for (x, q) in adds if x != iid and not self._is_nan_item(st.menu, x)]

                for item_id, qty in adds:
                    if mentions_nan and has_variant and self._is_nan_item(st.menu, item_id):
                        continue
                    st.order.add(item_id, qty)
                    added_any = True
                    added_ids.append(item_id)

            cart_after = st.order.summary(st.menu) if st.menu else ""
            if added_any and st.menu and cart_after and cart_after != (cart_before or ""):
                if any(self._is_butter_chicken(st.menu, iid) for iid in added_ids):
                    st.pending_spice_item_id = next(iid for iid in added_ids if self._is_butter_chicken(st.menu, iid))
                    st.pending_spice_qty = int(effective_qty or 1)
                    await self.clear_thinking(ws)
                    await self._speak(ws, "Certainly. And would you like your Butter Chicken mild or spicy?")
                    return

                await self.clear_thinking(ws)

                if not st.fulfillment_mode:
                    st.pending_fulfillment = True
                    await self._speak(ws, self._say_pickup_or_delivery())
                    return

                if st.fulfillment_mode == "pickup" and not st.customer_name:
                    st.pending_name = True
                    await self._speak(ws, "Great. What name should I put the order under?" if st.lang != "nl" else "Prima. Op welke naam mag ik de bestelling zetten?")
                    return

                await self._speak(ws, self._say_anything_else())
                return

            # LLM fallback
            menu_context = "Menu empty."
            if st.menu:
                default_items = [st.menu.display_name(iid) for _, iid in st.menu.name_choices[:80]]
                menu_context = "\n".join([f"- {x}" for x in default_items]) if default_items else "Menu empty."

            out = await llm_turn(self.oa, st, transcript, menu_context)
            reply = (out.get("reply") or "").strip()

            if reply and not detect_explicit_remove_intent(transcript, st.lang):
                if any(x in reply.lower() for x in ["remove", "cancel", "take off", "delete", "verwijder", "haal weg", "annuleer", "schrap"]):
                    reply = "Sorry — did you want to add something, or change your order?" if st.lang != "nl" else "Sorry — wil je iets toevoegen, of je bestelling wijzigen?"

            if not reply:
                reply = "How can I help you?" if st.lang != "nl" else "Waar kan ik je mee helpen?"
            reply = self.enforce_output_language(reply, st.lang)

            await self.clear_thinking(ws)
            await self._speak(ws, reply)

        except asyncio.CancelledError:
            try:
                await self.clear_thinking(ws)
            except Exception:
                pass
            return
        except Exception as e:
            logger.exception("process_utterance failed: %s", e)
            try:
                await self.clear_thinking(ws)
            except Exception:
                pass
        finally:
            st.is_processing = False

