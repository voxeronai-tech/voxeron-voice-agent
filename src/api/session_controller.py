# src/api/session_controller.py
from __future__ import annotations

import asyncio
import json
import logging
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
)
from .services.openai_client import OpenAIClient

logger = logging.getLogger("taj-agent")

SESSION_CONTROLLER_VERSION = "2026-01-04T-intent-first-soft-stt-lang-taj-fulfillment-stt-override"

# Fulfillment prompts (strong, anti-bias)
FULFILLMENT_STT_PROMPT_EN = (
    "The user is answering: pickup or delivery. "
    "Return only what the user said. Likely answers: pickup, pick up, for pickup, takeaway, collection, delivery, for delivery."
)

FULFILLMENT_STT_PROMPT_NL = (
    "De gebruiker antwoordt op: afhalen of bezorgen. "
    "Geef alleen wat de gebruiker zei. Waarschijnlijke antwoorden: afhalen, ophalen, meenemen, to go, bezorgen, bezorging."
)

NAME_STT_PROMPT = (
    "The user is giving their name for a restaurant order. "
    "This is likely a short name (e.g., Jerry, Marcel, Tom, Sara). "
    "Transcribe the name exactly. "
    "Do NOT turn it into commands like 'ik wil bestellen', 'pickup', or 'delivery'."
)

# Temporary deterministic alias overlay for Taj (until Tenant Overlay / Discovery Engine lands)
TAJ_EXTRA_ALIASES: Dict[str, str] = {
    "tikken": "Chicken Tikka",
    "tikka": "Chicken Tikka",
    "tika": "Chicken Tikka",
    "bestellen": "__GLOBAL_ORDER__",  # useful for intent guard (not a menu item)
}


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

    # Output language (Taj: stable EN unless explicit "Nederlands")
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

    pending_fulfillment: bool = False
    fulfillment_mode: Optional[str] = None  # "pickup" / "delivery"

    pending_name: bool = False
    customer_name: Optional[str] = None

    # Offer/selection memory
    offered_item_id: Optional[str] = None
    offered_label: Optional[str] = None

    # Language hysteresis (for STT hinting; does NOT automatically change output language for Taj)
    stt_lang_hint: Optional[str] = None
    lang_candidate: Optional[str] = None
    lang_candidate_count: int = 0


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


def _safe_json_loads(s: str) -> Optional[Dict[str, Any]]:
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
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
    obj = _safe_json_loads(txt)
    return obj if obj else {"reply": txt, "add": [], "remove": []}


# -------------------------
# Naan keyword parsing (SCOPED)
# -------------------------
_NAAN_TOKENS = {"naan", "nan", "naam"}
_PLAIN_LIKE = {
    "plain", "regular", "normal", "gewoon", "normaal", "standaard",
    "plainer", "plainar", "planar", "plano", "playn", "plean",
}
_VARIANT_TOKS = {
    "garlic": {"garlic", "knoflook"},
    "butter": {"butter", "boter"},
    "cheese": {"cheese", "kaas"},
    "keema": {"keema", "kheema"},
    "peshawari": {"peshawari"},
}


def _extract_nan_variant_keyword_scoped(text: str) -> Optional[str]:
    t = norm_simple(text)
    if not t:
        return None
    toks = t.split()
    naan_idxs = [i for i, tok in enumerate(toks) if tok in _NAAN_TOKENS]
    if not naan_idxs:
        return None

    def window(i: int, r: int = 3) -> List[str]:
        lo = max(0, i - r)
        hi = min(len(toks), i + r + 1)
        return toks[lo:hi]

    for idx in naan_idxs:
        w = window(idx, 3)
        if any(tok in _PLAIN_LIKE for tok in w):
            return "plain"

    for idx in naan_idxs:
        w = set(window(idx, 3))
        for canonical, variants in _VARIANT_TOKS.items():
            if w.intersection(variants):
                return canonical

    joined = " " + " ".join(toks) + " "
    for canonical, variants in _VARIANT_TOKS.items():
        for v in variants:
            if f" {v} naan " in joined or f" naan {v} " in joined or f" {v} nan " in joined or f" nan {v} " in joined:
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
    # UX strings
    # -------------------------
    def _say_anything_else(self) -> str:
        return "Anything else you'd like to add?" if self.state.lang != "nl" else "Wil je nog iets toevoegen?"

    def _say_pickup_or_delivery(self) -> str:
        return "Is this for pickup or delivery?" if self.state.lang != "nl" else "Is dit om af te halen of om te bezorgen?"

    # -------------------------
    # Deterministic guards
    # -------------------------
    def _is_obvious_out_of_scope(self, text: str) -> bool:
        t = norm_simple(text)
        return any(x in t for x in ["weather", "weer", "temperature", "temperatuur", "forecast", "regen", "sunny", "zonnig"])

    def _is_order_summary_query(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        keys = [
            " what is the order ", " current order ", " total order ", " order now ", " order summary ",
            " wat is de order ", " wat is die order ", " wat is mijn bestelling ", " huidige bestelling ",
            " wat is de current order ", " wat is de bestelling ", " wat heb ik besteld ", " wat is de order nou ",
        ]
        return any(k in t for k in keys)

    def _is_refusal_like(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        refusal = [
            " no ", " nope ", " nah ", " i won't ", " i will not ", " dont ", " don't ", " rather not ",
            " nee ", " neen ", " wil ik niet ", " geen ", " liever niet ", " weiger ",
        ]
        return any(r in t for r in refusal)

    def _is_ordering_intent_global(self, text: str) -> bool:
        """
        IMPORTANT: protect short names from prompt-bias hallucination.
        - single word: only trigger if it's EXACTLY a command word.
        - multi-word: allow intent phrases.
        """
        raw = (text or "").strip().lower()
        words = [w for w in raw.split() if w]
        if not words:
            return False

        if len(words) == 1:
            return raw in {"bestellen", "order", "ordering", "start", "begin"}

        t = " " + norm_simple(text) + " "
        hits = [
            " ik wil bestellen ",
            " wil bestellen ",
            " i want to order ",
            " i'd like to order ",
            " i would like to order ",
            " i want to order food ",
        ]
        return any(h in t for h in hits)

    def _is_language_command(self, text: str) -> Optional[str]:
        t = " " + norm_simple(text) + " "
        if " nederlands " in t or " dutch " in t:
            return "nl"
        if " english " in t or " engels " in t:
            return "en"
        return None

    def _looks_like_name_answer(self, text: str) -> bool:
        t_raw = (text or "").strip()
        tn = norm_simple(t_raw)
        if not tn:
            return False
        if self._is_refusal_like(text):
            return False
        if self._is_order_summary_query(text):
            return False
        if self._is_ordering_intent_global(text):
            return False
        if "?" in t_raw:
            return False
        bad = {"yes", "yeah", "ok", "okay", "sure", "ja", "oke", "oké", "prima", "good", "thanks", "thank you"}
        if tn in bad:
            return False
        return len(t_raw.split()) <= 3

    # -------------------------
    # Menu helpers
    # -------------------------
    def _is_nan_item(self, menu: MenuSnapshot, item_id: str) -> bool:
        dn = (menu.display_name(item_id) or "").lower()
        return ("naan" in dn) or ("nan" in dn) or ("naam" in dn)

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
            ("garlic", ["garlic", "knoflook"]),
            ("plain", ["naan", "nan", "plain", "regular", "normal", "gewoon", "standaard"]),
            ("butter", ["butter", "boter"]),
            ("cheese", ["cheese", "kaas"]),
            ("keema", ["keema", "kheema"]),
            ("peshawari", ["peshawari"]),
        ]

        def score(label: str) -> int:
            ll = label.lower().strip()
            if ll in {"nan", "naan"}:
                return 120
            for i, (_k, toks) in enumerate(prefs):
                if any(t in ll for t in toks):
                    return 100 - i
            return 0

        items.sort(key=lambda x: (score(x[0]), -len(x[0])), reverse=True)
        return items

    def _naan_optima_prompt(self, *, list_mode: str = "short", with_main: Optional[str] = None) -> str:
        st = self.state
        menu = st.menu
        opts = self._naan_options_from_menu(menu) if menu else []
        max_n = 2 if list_mode == "short" else 4

        if not opts:
            if st.lang != "nl":
                return (
                    f"Certainly. Would you like plain naan or garlic naan with your {with_main}?"
                    if with_main
                    else "Which naan would you like, plain or garlic?"
                )
            return (
                f"Zeker. Wil je plain naan of garlic naan bij je {with_main}?"
                if with_main
                else "Welke naan wil je, plain of garlic?"
            )

        picked = opts[:max_n]
        labels = [p[0] for p in picked]

        if st.lang != "nl":
            if with_main and len(labels) >= 2:
                return f"Certainly. Would you like {labels[0]} or {labels[1]} with your {with_main}?"
            if len(labels) >= 2:
                return f"Would you like {labels[0]} or {labels[1]}?"
            return f"We have {labels[0]}. Would you like that?"
        else:
            if with_main and len(labels) >= 2:
                return f"Zeker. Wil je {labels[0]} of {labels[1]} bij je {with_main}?"
            if len(labels) >= 2:
                return f"Wil je {labels[0]} of {labels[1]}?"
            return f"We hebben {labels[0]}. Wil je die?"

    def _find_naan_item_for_variant(self, menu: MenuSnapshot, variant: str) -> Optional[str]:
        if not menu or not variant:
            return None
        v = variant.lower().strip()
        opts = self._naan_options_from_menu(menu)
        if not opts:
            return None

        best: Optional[str] = None
        best_score = -10

        for label, iid in opts:
            ll = label.lower().strip()
            s = 0

            if v == "plain":
                if ll in {"nan", "naan"}:
                    s += 50
                if any(t in ll for t in ["plain", "regular", "normal", "gewoon", "standaard"]):
                    s += 20

            if v == "garlic" and any(t in ll for t in ["garlic", "knoflook"]):
                s += 25
            if v == "butter" and any(t in ll for t in ["butter", "boter"]):
                s += 25
            if v == "cheese" and any(t in ll for t in ["cheese", "kaas"]):
                s += 25
            if v == "keema" and "keema" in ll:
                s += 25
            if v == "peshawari" and "peshawari" in ll:
                s += 25

            if v in ll:
                s += 8

            if s > best_score:
                best_score = s
                best = iid

        return best if best_score >= 0 else None

    def _parse_fulfillment(self, text: str) -> Optional[str]:
        t = norm_simple(text)
        if any(x in t for x in ["pickup", "pick up", "takeaway", "collection", "for pickup", "afhalen", "ophalen", "meenemen", "to go", "togo"]):
            return "pickup"
        if any(x in t for x in ["delivery", "deliver", "for delivery", "bezorgen", "bezorging"]):
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

        if tenant_ref == "taj_mahal":
            if st.lang not in ("en", "nl"):
                st.lang = "en"
            st.stt_lang_hint = None
            st.lang_candidate = None
            st.lang_candidate_count = 0

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

    async def process_utterance(self, ws: WebSocket, pcm: bytes) -> None:
        st = self.state
        if st.is_processing:
            return
        st.is_processing = True
        st.turn_id += 1

        try:
            await self.send_thinking(ws)

            # ----------------------------------------------------------
            # STT MODE SELECTION (critical fix)
            # If we're slot-filling, do NOT use tenant stt_prompt_base.
            # Use dedicated prompts to avoid "bestellen" bias.
            # ----------------------------------------------------------
            transcript = ""

            # 1) Name capture: primary pass with NAME prompt (no tenant base prompt)
            if st.pending_name:
                try:
                    transcript = await self.oa.transcribe_pcm(pcm, None, prompt=NAME_STT_PROMPT)
                except Exception:
                    transcript = ""

            # 2) Fulfillment capture: primary pass with fulfillment prompt(s)
            elif st.pending_fulfillment:
                # We do NOT trust the tenant base prompt here. We try EN and NL prompts and pick the one that parses.
                candidates: List[Tuple[str, str]] = []  # (txt, label)
                try:
                    txt = await self.oa.transcribe_pcm(pcm, None, prompt=FULFILLMENT_STT_PROMPT_EN)
                    candidates.append(((txt or "").strip(), "fulfill_en_any"))
                except Exception:
                    pass
                try:
                    txt = await self.oa.transcribe_pcm(pcm, "en", prompt=FULFILLMENT_STT_PROMPT_EN)
                    candidates.append(((txt or "").strip(), "fulfill_en_forced"))
                except Exception:
                    pass
                try:
                    txt = await self.oa.transcribe_pcm(pcm, None, prompt=FULFILLMENT_STT_PROMPT_NL)
                    candidates.append(((txt or "").strip(), "fulfill_nl_any"))
                except Exception:
                    pass
                try:
                    txt = await self.oa.transcribe_pcm(pcm, "nl", prompt=FULFILLMENT_STT_PROMPT_NL)
                    candidates.append(((txt or "").strip(), "fulfill_nl_forced"))
                except Exception:
                    pass

                picked = ""
                for txt, label in candidates:
                    if not txt:
                        continue
                    mode = self._parse_fulfillment(txt)
                    if mode:
                        logger.info("STT(fulfillment_pick=%s): %s -> %s", label, txt, mode)
                        picked = txt
                        break

                transcript = picked or (candidates[0][0] if candidates else "")

            # 3) Normal speech: allow Taj multilingual STT; tenant prompt base allowed
            else:
                # Taj: multilingual STT unless explicit NL output selected
                stt_lang: Optional[str] = None
                if st.tenant_ref == "taj_mahal":
                    stt_lang = "nl" if st.lang == "nl" else None
                else:
                    stt_lang = st.lang if st.lang in ("en", "nl", "tr") else None

                stt_prompt = None
                if self.tenant_stt_prompt_enabled and st.tenant_cfg and getattr(st.tenant_cfg, "stt_prompt_base", None):
                    stt_prompt = str(st.tenant_cfg.stt_prompt_base)

                try:
                    transcript = await self.oa.transcribe_pcm(pcm, stt_lang, prompt=stt_prompt)
                except Exception:
                    transcript = ""

            if not transcript or not transcript.strip():
                await self.clear_thinking(ws)
                msg = (
                    "Sorry — I didn’t catch that. Could you repeat?"
                    if st.lang != "nl"
                    else "Sorry — ik verstond het niet. Kun je het herhalen?"
                )
                await self._speak(ws, msg)
                return

            st.last_activity_ts = time.time()
            transcript = transcript.strip()

            logger.info("STT: %s", transcript)
            await self.send_user_text(ws, transcript)
            tnorm = " " + norm_simple(transcript) + " "

            # ==========================================================
            # 1) Global intent guard (Intent-First)
            # IMPORTANT FIX:
            # - do NOT clear pending_fulfillment / pending_name based on possibly-biased STT
            # - only clear slots when we're NOT currently slot-filling
            # ==========================================================
            is_ordering_intent = self._is_ordering_intent_global(transcript)
            lang_cmd = self._is_language_command(transcript)

            if is_ordering_intent and not (st.pending_fulfillment or st.pending_name):
                if st.pending_name or st.pending_fulfillment:
                    logger.info(
                        "[v0.7.1] Global Guard: ordering intent intercepted; clearing slots (pending_name=%s pending_fulfillment=%s)",
                        st.pending_name,
                        st.pending_fulfillment,
                    )
                st.pending_name = False
                st.pending_fulfillment = False
            elif is_ordering_intent and (st.pending_fulfillment or st.pending_name):
                logger.info(
                    "[v0.7.1] Global Guard: ordering intent seen during slot-fill; IGNORE (pending_name=%s pending_fulfillment=%s)",
                    st.pending_name,
                    st.pending_fulfillment,
                )

            # ==========================================================
            # 2) Language command handling (Taj explicit only)
            # ==========================================================
            if lang_cmd and lang_cmd != st.lang:
                logger.info("[lang] explicit switch %s -> %s", st.lang, lang_cmd)
                st.lang = lang_cmd
                if st.tenant_ref == "taj_mahal" and st.lang == "nl":
                    st.stt_lang_hint = "nl"

            allow_auto = not (st.tenant_ref == "taj_mahal" and st.lang != "nl")
            _ = detect_language_intent(
                transcript,
                phase=st.phase,
                current_lang=st.lang,
                allow_auto_detect=allow_auto,
            )

            # ==========================================================
            # 3) Dispatcher routing
            # ==========================================================
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
                        "Hi! Welcome to Taj Mahal Bussum. You can start ordering now. If you want Dutch, say 'Nederlands'."
                        if st.lang != "nl"
                        else "Welkom bij Taj Mahal Bussum. Je kunt nu bestellen."
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

            # ==========================================================
            # 4) Deterministic prompt-dump filter
            # ==========================================================
            if _looks_like_stt_prompt_dump(transcript):
                await self.clear_thinking(ws)
                msg = (
                    "Begrepen. Zeg gewoon wat je wilt bestellen, bijvoorbeeld: ‘twee butter chicken en één naan’."
                    if st.lang == "nl"
                    else "Got it. Just tell me what you'd like to order, for example: ‘two butter chicken and one naan’."
                )
                await self._speak(ws, msg)
                return

            # ==========================================================
            # 5) Slot handling (Intent-aware, non-greedy)
            # ==========================================================
            # Fulfillment slot (now robust against 'bestellen' hallucination)
            if st.pending_fulfillment:
                if self._is_obvious_out_of_scope(transcript):
                    await self.clear_thinking(ws)
                    await self._speak(ws, "I can help with the order — is this for pickup or delivery?")
                    return

                mode = self._parse_fulfillment(transcript)

                await self.clear_thinking(ws)
                if not mode:
                    # Never fall through to LLM while pending fulfillment
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

            # Name slot
            if st.pending_name:
                await self.clear_thinking(ws)

                if self._is_order_summary_query(transcript) and st.menu:
                    cart = st.order.summary(st.menu) or "Empty"
                    if st.lang != "nl":
                        await self._speak(ws, f"Your current order is: {cart}. What name should I put the order under?")
                    else:
                        await self._speak(ws, f"Je huidige bestelling is: {cart}. Op welke naam mag ik de bestelling zetten?")
                    return

                if self._is_obvious_out_of_scope(transcript):
                    await self._speak(ws, "Before I continue — what name should I put the order under?")
                    return

                if self._is_refusal_like(transcript):
                    await self._speak(ws, "No problem. What name should I put the order under?")
                    return

                if not self._looks_like_name_answer(transcript):
                    msg = "Could you please tell me your name?" if st.lang != "nl" else "Wat is je naam?"
                    await self._speak(ws, msg)
                    return

                st.customer_name = transcript.strip()
                st.pending_name = False
                msg = f"Thank you, {st.customer_name}. {self._say_anything_else()}" if st.lang != "nl" else f"Dank je, {st.customer_name}. {self._say_anything_else()}"
                await self._speak(ws, msg)
                return

            # ==========================================================
            # 6) Ordering logic (Deterministic add + naan scoping)
            # ==========================================================
            add_qty = (_extract_qty_first(transcript, "en") or _extract_qty_first(transcript, "nl") or 1)
            effective_qty = add_qty

            cart_before = st.order.summary(st.menu) if st.menu else ""
            added_any = False
            added_ids: List[str] = []

            if st.menu:
                if st.tenant_ref == "taj_mahal":
                    tok = norm_simple(transcript).strip()
                    if tok in TAJ_EXTRA_ALIASES and TAJ_EXTRA_ALIASES[tok] != "__GLOBAL_ORDER__":
                        target_name = TAJ_EXTRA_ALIASES[tok].lower()
                        for _n, iid in st.menu.name_choices:
                            dn = (st.menu.display_name(iid) or "").lower()
                            if target_name in dn:
                                st.order.add(iid, max(1, int(effective_qty or 1)))
                                added_any = True
                                added_ids.append(iid)
                                break

                adds = parse_add_item(st.menu, transcript, qty=effective_qty)

                mentions_nan = (" naan " in (" " + norm_simple(transcript) + " ")) or detect_generic_nan_request(transcript)
                variant = _extract_nan_variant_keyword_scoped(transcript)
                has_variant = bool(variant)

                naan_opts = self._naan_options_from_menu(st.menu)
                logger.info(
                    "naan_check mentions_nan=%s has_variant=%s variant=%s naan_opts=%d opts=%s",
                    mentions_nan, has_variant, variant, len(naan_opts), [x[0] for x in naan_opts[:5]],
                )

                if mentions_nan and (not has_variant):
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

                    await self.clear_thinking(ws)
                    await self._speak(ws, self._naan_optima_prompt(list_mode="short", with_main="Butter Chicken" if "butter chicken" in norm_simple(transcript) else None))
                    return

                if mentions_nan and has_variant:
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

            # ==========================================================
            # 7) LLM fallback
            # ==========================================================
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
