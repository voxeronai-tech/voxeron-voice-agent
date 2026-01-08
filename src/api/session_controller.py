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
)
from .services.openai_client import OpenAIClient

# Optional (RC3): Cognitive Orchestrator (safe import)
try:
    from .orchestrator.orchestrator import CognitiveOrchestrator, OrchestratorRoute
    from .orchestrator.parser_types import MatchKind
except Exception:  # pragma: no cover
    CognitiveOrchestrator = None  # type: ignore
    OrchestratorRoute = None  # type: ignore
    MatchKind = None  # type: ignore


logger = logging.getLogger("taj-agent")

SESSION_CONTROLLER_VERSION = "2026-01-04T-rc3-stable-taj-lang-slot-stt-naan-choice-orchestrator-hook"

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


NAAN_VARIANT_STT_PROMPT_EN = (
    "The user is choosing a naan type. "
    "Return only what the user said. Likely answers: plain, regular, normal, garlic, cheese, keema, peshawari. "
    "If you hear Dutch words like 'klein' or 'gewoon', return them as-is."
)

NAAN_VARIANT_STT_PROMPT_NL = (
    "De gebruiker kiest een naan-variant. "
    "Geef alleen wat de gebruiker zei. Waarschijnlijke antwoorden: plain, gewoon, normaal, garlic/knoflook, cheese/kaas, keema, peshawari. "
    "Als je 'klein' hoort, geef 'klein' terug."
)

# Temporary deterministic alias overlay for Taj (until Tenant Overlay / Discovery Engine lands)
TAJ_EXTRA_ALIASES: Dict[str, str] = {
    "tikken": "Chicken Tikka",
    "tikka": "Chicken Tikka",
    "tika": "Chicken Tikka",
    "bestellen": "__GLOBAL_ORDER__",  # intent guard (not a menu item)
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
    lang_locked: bool = True  # Taj: lock language for the session (explicit switch only)
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

    # RC3: hold qty across split utterances, e.g. 'one ... [pause] ... biryani'
    pending_qty_hold: Optional[int] = None
    pending_qty_deadline: float = 0.0

    pending_fulfillment: bool = False
    fulfillment_mode: Optional[str] = None  # "pickup" / "delivery"

    pending_name: bool = False
    customer_name: Optional[str] = None

    # Checkout
    order_complete: bool = False

    # Checkout / confirmation (RC3)
    pending_confirm: bool = False
    order_finalized: bool = False
    # Optima-style mid-flow cart confirmation (after resolving variants like naan)
    pending_cart_check: bool = False
    cart_check_snapshot: Optional[str] = None  # human-readable short summary for confirmation prompt

    # RC3: merge incomplete prefixes like "I'd like ..." across short pauses
    pending_prefix: Optional[str] = None
    pending_prefix_deadline: float = 0.0

    # Offer/selection memory
    offered_item_id: Optional[str] = None
    offered_label: Optional[str] = None

    offered_ts: float = 0.0

    # RC3: remember last add so user can correct "No, X" right after
    last_added: List[Tuple[str, int]] = field(default_factory=list)
    last_added_ts: float = 0.0

    # Language hysteresis (for STT hinting; does NOT automatically change output language for Taj)
    stt_lang_hint: Optional[str] = None
    lang_candidate: Optional[str] = None
    lang_candidate_count: int = 0


QTY_MAP_NL = {"een": 1, "één": 1, "1": 1, "twee": 2, "2": 2, "drie": 3, "3": 3, "vier": 4, "4": 4}
QTY_MAP_EN = {"one": 1, "1": 1, "two": 2, "2": 2, "three": 3, "3": 3, "four": 4, "4": 4}




def _strip_name_prefix(transcript: str) -> str:
    t = transcript.strip()
    low = t.lower().strip()
    for pref in ["my name is", "name is", "i am", "this is", "mijn naam is", "ik ben", "dit is"]:
        if low.startswith(pref):
            return t[len(pref):].strip(" ,.-:")
    return t

def _extract_fulfillment_mode(norm_text: str) -> Optional[str]:
    """Fast keyword-based fulfillment detection on normalized text."""
    t = " " + norm_text + " "
    # English + Dutch, plus common variants
    if any(k in t for k in [" pickup ", " pick up ", " afhalen ", " ophalen "]):
        return "pickup"
    if any(k in t for k in [" delivery ", " bezorgen ", " bezorging "]):
        return "delivery"
    return None

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


_QTY_WORDS: Dict[str, int] = {
    # NL
    "een": 1,
    "één": 1,
    "twee": 2,
    "drie": 3,
    "vier": 4,
    "vijf": 5,
    "zes": 6,
    "zeven": 7,
    "acht": 8,
    "negen": 9,
    "tien": 10,
    # EN
    "a": 1,
    "an": 1,
    "one": 1,
    "two": 2,
    "three": 3,
    "four": 4,
    "five": 5,
    "six": 6,
    "seven": 7,
    "eight": 8,
    "nine": 9,
    "ten": 10,
}


def _qty_near_span(t_norm: str, span_start: int, span_end: int) -> Optional[int]:
    """Deterministic local quantity detector near an alias match span."""
    toks = [(m.group(0), m.start(), m.end()) for m in re.finditer(r"[A-Za-zÀ-ÿ0-9]+", t_norm)]
    if not toks:
        return None

    idx = None
    for i, (_tok, s0, e0) in enumerate(toks):
        if not (e0 <= span_start or s0 >= span_end):
            idx = i
            break
    if idx is None:
        return None

    left = max(0, idx - 3)
    right = min(len(toks) - 1, idx + 1)

    # Prefer left-side qty ("two naan", "twee naan")
    for i in range(idx - 1, left - 1, -1):
        tok = toks[i][0].lower()
        if tok.isdigit():
            q = int(tok)
            return q if 1 <= q <= 20 else None
        if tok in _QTY_WORDS:
            return _QTY_WORDS[tok]

    # Also accept "naan x2" / "naan 2x"
    for i in range(idx, right + 1):
        tok = toks[i][0].lower()
        if tok.endswith("x") and tok[:-1].isdigit():
            q = int(tok[:-1])
            return q if 1 <= q <= 20 else None
        if tok.startswith("x") and tok[1:].isdigit():
            q = int(tok[1:])
            return q if 1 <= q <= 20 else None

    return None


def parse_add_item(menu: MenuSnapshot, text: str, *, qty: int) -> List[Tuple[str, int]]:
    """
    Deterministic alias matcher.
    RC3 fix: prevent overlapping alias double-adds (e.g. "lamb tikka biryani" + "lamb tikka").
    Strategy:
      - find all alias matches with spans
      - sort longest-first
      - accept only non-overlapping spans
      - de-duplicate by item_id
    """
    raw = norm_simple(text)
    if not raw:
        return []

    t = " " + raw + " "
    candidates: List[Tuple[int, int, str, str]] = []  # (start, end, alias, item_id)

    for alias, item_id in (menu.alias_map or {}).items():
        a = alias.strip()
        if len(a) < 3:
            continue
        pat = r"(?<!\w)" + re.escape(a) + r"(?!\w)"
        for mm in re.finditer(pat, t, flags=re.UNICODE):
            candidates.append((mm.start(), mm.end(), a, item_id))

    if not candidates:
        return []

    candidates.sort(key=lambda x: (-(x[1] - x[0]), x[0]))

    chosen_spans: List[Tuple[str, int, int]] = []  # (item_id, start, end)
    used_item_ids = set()
    used_spans: List[Tuple[int, int]] = []

    q = max(1, int(qty or 1))

    def overlaps(s1: int, e1: int, s2: int, e2: int) -> bool:
        return not (e1 <= s2 or e2 <= s1)

    for s0, e0, _alias, item_id in candidates:
        if item_id in used_item_ids:
            continue
        if any(overlaps(s0, e0, s2, e2) for (s2, e2) in used_spans):
            continue
        used_item_ids.add(item_id)
        used_spans.append((s0, e0))
        chosen_spans.append((item_id, s0, e0))

    if not chosen_spans:
        return []

    multi_item = (len({iid for (iid, _s, _e) in chosen_spans}) > 1)

    out: List[Tuple[str, int]] = []
    for item_id, s0, e0 in chosen_spans:
        local_q = _qty_near_span(t, s0, e0)
        if local_q is not None:
            out.append((item_id, local_q))
        else:
            out.append((item_id, 1 if multi_item else q))

    return out


LLM_SYSTEM_BASE = """
You are a helpful restaurant ordering agent for the current tenant.

Rules:
- Always respond in the user's current language (lang), even if the user uses words from another language.
- Do NOT switch languages unless the user explicitly asks (e.g., says 'Nederlands' or 'English').
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

    # RC3: enforce output language when language is locked (prevents NL drift on "ja/maak het")
    if getattr(state, "lang_locked", False):
        if getattr(state, "lang", "en") == "nl":
            sys += "\n\nOUTPUT_LANGUAGE: nl\nReturn the reply in Dutch only. Do not switch languages."
        else:
            sys += "\n\nOUTPUT_LANGUAGE: en\nReturn the reply in English only. Do not switch languages."

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
            if (
                f" {v} naan " in joined
                or f" naan {v} " in joined
                or f" {v} nan " in joined
                or f" nan {v} " in joined
            ):
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
            " wat is de orde ", " wat is de orde nou ", " wat is de bestelling nou ", " wat is mijn order nu ", " wat is mijn bestelling nu ",
        ]
        return any(k in t for k in keys)

    def _is_checkout_intent(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        keys = [
            " that's all ", " thatll be all ", " that'll be all ", " all good ", " all set ",
            " that's it ", " thats it ", " finished ", " done ", " complete ",
            " klaar ", " klaar hoor ", " dat is alles ", " dat is alles hoor ", " klaar ermee ",
            " order is complete ", " place the order ", " confirm the order ",
        ]
        return any(k in t for k in keys)

    def _is_affirm(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        keys = [" yes ", " yeah ", " yep ", " ok ", " okay ", " oke ", " oké ", " sure ", " correct ", " klopt ", " ja ", " prima ", " graag ", " indeed "]
        return any(k in t for k in keys)

    def _is_negative(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        keys = [" no ", " nope ", " nah ", " nee ", " neen ", " liever niet ", " incorrect ", " klopt niet ", " not correct "]
        return any(k in t for k in keys)

    def _is_refusal_like(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        refusal = [
            " no ", " nope ", " nah ", " i won't ", " i will not ", " dont ", " don't ", " rather not ",
            " nee ", " neen ", " wil ik niet ", " geen ", " liever niet ", " weiger ",
        ]
        return any(r in t for r in refusal)

    def _is_done_intent(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        done = [
            " that will be all ", " that'll be all ", " thats all ", " that's all ", " all good ", " that's it ",
            " no that's all ", " no thats all ", " no thank you ", " no thanks ", " nothing else ",
            " klaar ", " klaar hoor ", " dat is alles ", " dat was alles ", " nee dat is alles ", " nee hoor dat is alles ",
            " bestelling is klaar ", " order is complete ", " order complete ",
        ]
        return any(d in t for d in done)

    def _is_close_call_intent(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        close = [
            " close the call ", " end the call ", " hang up ", " bye ", " goodbye ", " thanks bye ", " thank you bye ",
            " ophangen ", " beëindigen ", " einde gesprek ", " doei ", " dag ",
        ]
        return any(c in t for c in close)

    def _is_pickup_delivery_mention(self, text: str) -> bool:
        # Used to avoid re-asking pickup/delivery once already known
        t = norm_simple(text)
        return any(x in t for x in ["pickup", "pick up", "takeaway", "collection", "afhalen", "ophalen", "meenemen", "bezorgen", "bezorging", "delivery"])

    def _is_yes_like(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        yes = [
            " yes ", " yeah ", " yep ", " yup ", " ok ", " okay ", " sure ", " please ", " sounds good ",
            " ja ", " jazeker ", " zeker ", " oké ", " oke ", " prima ", " graag ", "top","toppie", "okidoki"
        ]
        return any(y in t for y in yes)

    def _is_total_amount_query(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        keys = [
            " total amount ", " total price ", " total cost ", " how much ", " what is the total ",
            " totaal bedrag ", " totaalprijs ", " totale prijs ", " hoeveel kost ", " wat is het totaal ",
        ]
        return any(k in t for k in keys)

    def _is_order_complete_intent(self, text: str) -> bool:
        t = " " + norm_simple(text) + " "
        keys = [
            " order is complete ", " that's all ", " that is all ", " done ", " finish ", " finished ", " complete the order ", " place the order ",
            " dat is alles ", " klaar ", " afronden ", " bestelling is klaar ", " rond de bestelling af ",
        ]
        return any(k in t for k in keys)

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
    def _clean_customer_name(self, text: str) -> str:
        """Normalize name answers, accept prefixes like 'my name is' / 'mijn naam is'."""
        t = (text or "").strip()
        tn = norm_simple(t)
        # Strip common prefixes in EN/NL
        prefixes = [
            "my name is ",
            "name is ",
            "i am ",
            "it's ",
            "its ",
            "mijn naam is ",
            "ik ben ",
            "naam is ",
        ]
        for p in prefixes:
            if tn.startswith(p):
                # remove from original string by length of prefix in normalized form approx
                # easiest: remove from tn and then map back by taking remainder from original split
                # We'll just take remainder from original by splitting on spaces count.
                words = t.split()
                p_words = p.strip().split()
                if len(words) > len(p_words):
                    return " ".join(words[len(p_words):]).strip(" .,")
        return t.strip(" .,")

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

    def _check_naan_request(self, menu: MenuSnapshot, transcript: str) -> Tuple[bool, bool, Optional[str]]:
        """Detect whether the user is talking about naan and whether a specific variant was captured.

        Returns: (mentions_nan, has_variant, variant_keyword)
        variant_keyword is one of: plain/garlic/cheese/keema/peshawari (or None)
        """
        t = " " + norm_simple(transcript) + " "
        mentions_nan = (" naan " in t) or (" nan " in t) or (" naam " in t)

        variant = _extract_nan_variant_keyword_scoped(transcript)
        has_variant = variant is not None

        # If STT says something like "lamb naan" while we're in a naan context, treat it as "plain naan"
        # ONLY if the menu doesn't actually contain something like "lamb naan".
        if not has_variant and mentions_nan and (" lamb " in t or " lam " in t):
            # check for a real "lamb naan" option
            opts = self._naan_options_from_menu(menu) if menu else []
            has_lamb_naan = any("lamb" in (label or "").lower() for label, _ in opts)
            if not has_lamb_naan:
                variant = "plain"
                has_variant = True

        logger.info(
            "naan_check mentions_nan=%s has_variant=%s variant=%s naan_opts=%s opts=%s",
            mentions_nan,
            has_variant,
            variant,
            len(self._naan_options_from_menu(menu)) if menu else 0,
            [o[0] for o in (self._naan_options_from_menu(menu) if menu else [])],
        )
        return mentions_nan, has_variant, variant
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

        # Prefer UX terms plain/garlic if labels are generic "Nan"/"Nan Garlic"
        if st.lang != "nl":
            # If menu uses "Nan", rewrite the speech prompt to plain/garlic
            if any(l.lower() in {"nan", "naan"} for l in labels) and any("garlic" in l.lower() for l in labels):
                return f"Certainly. Would you like plain naan or garlic naan{' with your ' + with_main if with_main else ''}?"
            if with_main and len(labels) >= 2:
                return f"Certainly. Would you like {labels[0]} or {labels[1]} with your {with_main}?"
            if len(labels) >= 2:
                return f"Would you like {labels[0]} or {labels[1]}?"
            return f"We have {labels[0]}. Would you like that?"
        else:
            if any(l.lower() in {"nan", "naan"} for l in labels) and any("garlic" in l.lower() for l in labels):
                return f"Zeker. Wil je plain naan of garlic naan{' bij je ' + with_main if with_main else ''}?"
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

    def _taj_overlay_alias_map(self, menu: MenuSnapshot) -> Dict[str, str]:
        """
        Map TAJ_EXTRA_ALIASES (display-name style) to actual item_ids where possible.
        Returns alias -> item_id mapping.
        """
        out: Dict[str, str] = {}
        if not menu:
            return out

        # Build reverse lookup by display name (lower)
        name_to_id: Dict[str, str] = {}
        for _n, iid in menu.name_choices:
            dn = (menu.display_name(iid) or "").strip().lower()
            if dn:
                name_to_id[dn] = iid

        for alias, target in TAJ_EXTRA_ALIASES.items():
            if target == "__GLOBAL_ORDER__":
                continue
            iid = name_to_id.get(target.lower())
            if iid:
                out[alias] = iid
        return out

    def _maybe_orchestrator_match_item(self, menu: MenuSnapshot, transcript: str, qty: int) -> Optional[str]:
        """
        Optional RC3: deterministic parser BEFORE any LLM.
        This is intentionally conservative: only for short utterances.
        Returns item_id if matched.
        """
        if CognitiveOrchestrator is None or OrchestratorRoute is None:
            return None
        if not menu:
            return None

        # Only run for short utterances to avoid double-add with parse_add_item
        if len((transcript or "").strip().split()) > 3:
            return None

        alias_map: Dict[str, str] = dict(menu.alias_map or {})
        if self.state.tenant_ref == "taj_mahal":
            alias_map.update(self._taj_overlay_alias_map(menu))

        orch = CognitiveOrchestrator(alias_map=alias_map)
        decision = orch.decide(transcript)

        if decision.route != OrchestratorRoute.DETERMINISTIC:
            return None

        # RC1-3 safety: only concrete item IDs (strings) are addable here.
        # Deterministic payloads (e.g. qty updates) must NOT be added to cart.
        item_id = decision.parser_result.matched_entity
        if not isinstance(item_id, str) or not item_id:
            return None

        # RC3: do NOT mutate cart here.
        # The caller decides whether/when to add to avoid double-adds.
        return item_id

    def _maybe_orchestrator_apply_qty_update(self, menu: MenuSnapshot, transcript: str) -> Optional[Tuple[str, int]]:
        """
        RC1-3: deterministic qty/update intent BEFORE LLM.

        Returns (item_id, new_qty) if applied, else None.

        Conservative target selection:
          1) if we have a recent last_added item, update that
          2) else if exactly one item in cart, update that
          3) else do nothing (let LLM clarify)
        """
        if CognitiveOrchestrator is None or OrchestratorRoute is None:
            return None
        if not menu:
            return None

        # Build alias map (kept consistent with _maybe_orchestrator_match_item)
        alias_map: Dict[str, str] = dict(menu.alias_map or {})
        if self.state.tenant_ref == "taj_mahal":
            alias_map.update(self._taj_overlay_alias_map(menu))

        orch = CognitiveOrchestrator(alias_map=alias_map)
        decision = orch.decide(transcript)
        if decision.route != OrchestratorRoute.DETERMINISTIC:
            return None

        payload = decision.parser_result.matched_entity
        if not (isinstance(payload, dict) and (payload.get("action") == "SET_QTY")):
            return None

        try:
            new_qty = int(payload.get("quantity") or 0)
        except Exception:
            return None
        if new_qty <= 0:
            return None

        st = self.state
        items = getattr(st.order, "items", None)
        if not isinstance(items, dict) or not items:
            return None

        # 1) Prefer last-added item if available
        try:
            last_added = getattr(st, "last_added", None)
            if isinstance(last_added, list) and last_added:
                iid = last_added[0][0]
                if isinstance(iid, str) and iid in items:
                    items[iid] = new_qty
                    return (iid, new_qty)
        except Exception:
            pass

        # 2) If only one item, update it
        if len(items) == 1:
            iid = next(iter(items.keys()))
            if isinstance(iid, str):
                items[iid] = new_qty
                return (iid, new_qty)

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
            try:
                snap = await self.menu_store.get_snapshot(tenant_ref, lang="en")
            except Exception as e:
                # Never let DB/menu issues crash the audio pipeline.
                # Hot-swap failures should degrade gracefully (stay alive, allow retry).
                logger.exception("[tenant] get_snapshot failed tenant=%s: %s", tenant_ref, e)
                snap = None
        st.menu = snap
        if snap:
            st.tenant_id = snap.tenant_id
            st.tenant_name = snap.tenant_name

        # Taj: stable output language, no auto-flip
        if tenant_ref == "taj_mahal":
            if st.lang not in ("en", "nl"):
                st.lang = "en"
            # Lock STT language to the session language for stability (explicit switches only)
            st.lang_locked = True
            st.stt_lang_hint = st.lang
            st.lang_candidate = None
            st.lang_candidate_count = 0

    async def _speak(self, ws: WebSocket, text: str) -> None:
        # RC3: remember last agent text and capture single-item offers for generic acceptance ("Yes, add one")
        st = self.state
        st.last_agent_text = text
        try:
            if st.menu:
                low = (text or "").lower()
                is_offer = any(p in low for p in [
                    "would you like to add", "would you like me to add", "do you want to add",
                    "shall i add", "should i add", "want me to add",
                ])
                if is_offer:
                    offer_ids = [iid for (iid, _q) in parse_add_item(st.menu, text, qty=1)]
                    offer_ids = list(dict.fromkeys(offer_ids))  # de-dupe, preserve order
                    if len(offer_ids) == 1:
                        st.offered_item_id = offer_ids[0]
                        st.offered_label = st.menu.display_name(offer_ids[0]) or None
                        st.offered_ts = time.time()
                        logger.info("[offer] item_id=%s label=%s", st.offered_item_id, st.offered_label)
        except Exception:
            logger.exception("[offer] capture failed")

        await self.send_agent_text(ws, text)
        try:
            await self.stream_tts_mp3(ws, text)
        except Exception as e:
            # RC3: never let TTS failures crash the session
            logger.exception("RC3: TTS failed (continuing without audio)")

            # ensure we don't stay in a bad speaking state
            try:
                await self.clear_thinking(ws)
            except Exception:
                pass

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

    async def _handle_pending_nan_variant(self, ws: WebSocket, transcript: str) -> bool:
        st = self.state
        # While a naan variant is pending, this handler owns the turn.
        # We must NOT "leak" into pickup/delivery unless the latch is resolved.
        t_raw = transcript or ""
        t_norm = " " + norm_simple(t_raw) + " "

        # --- Slot-scoped acoustic forgiveness ---
        # STT often mishears "plain" as "lamb/lam/lamsfilet" in this specific slot.
        # In the naan slot we accept these as "plain" even if "naan" isn't present.
        if re.search(r"\b(lamb|lam|lamsfilet|lams)\b", t_norm, flags=re.UNICODE):
            logger.info("RC3: naan_slot acoustic alias: lamb->plain (slot scoped)")
            t_norm = re.sub(r"\b(lamb|lam|lamsfilet|lams)\b", " plain ", t_norm, flags=re.UNICODE)

        # STT also mishears "plain" as Dutch "klein/klijn" in this slot.
        # Accept it as plain, slot-scoped only.
        if re.search(r"\b(klein|klijn)\b", t_norm, flags=re.UNICODE):
            logger.info("RC3: naan_slot acoustic alias: klein->plain (slot scoped)")
            t_norm = re.sub(r"\b(klein|klijn)\b", " plain ", t_norm, flags=re.UNICODE)

        # If user asks a question about naan spiciness, answer but keep the latch.
        t_low = t_norm.strip().lower()
        if ("spicy" in t_low or "pittig" in t_low or "heet" in t_low) and ("naan" in t_low or "nan" in t_low):
            await self._speak(ws, self._say_naan_not_spicy())
            # keep pending_choice, do not reprompt immediately
            return True

        # Primary extractor expects a "naan/nan" token; use it first.
        variant = _extract_nan_variant_keyword_scoped(t_norm)

        # Slot-scoped fallback: accept variant keywords even without 'naan' token.
        if not variant:
            if re.search(r"\b(plain|normal|regular|gewoon|simpel|standard)\b", t_norm):
                variant = "plain"
            elif re.search(r"\b(garlic|knoflook)\b", t_norm):
                variant = "garlic"
            elif re.search(r"\b(cheese|kaas)\b", t_norm):
                variant = "cheese"
            elif re.search(r"\b(peshawari)\b", t_norm):
                variant = "peshawari"
            elif re.search(r"\b(keema)\b", t_norm):
                variant = "keema"

        if variant:
            item_id = self._find_naan_item_for_variant(st.menu, variant)
            if item_id:
                st.order.add(item_id, max(1, int(st.pending_qty or 1)))
                st.pending_choice = None
                st.pending_qty = 1
                st.nan_prompt_count = 0
                await self.clear_thinking(ws)

                # Optima-style: confirm cart first, then keep initiative with "Anything else".
                cart = st.order.summary(st.menu) if st.menu else ""
                st.pending_cart_check = True
                st.cart_check_snapshot = cart
                await self._speak(
                    ws,
                    f"Okay, so that's {cart}. Is that correct?" if st.lang != "nl" else f"Oké, dus dat is: {cart}. Klopt dat?",
                )
                return True
        # Sticky latch: if we couldn't resolve, re-prompt (but do not advance the phase)
        st.nan_prompt_count += 1
        await self._speak(ws, self._naan_optima_prompt(list_mode="short"))
        return True

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
                    stt_lang = st.lang if st.lang in ("en", "nl") else "en"
                    transcript = await self.oa.transcribe_pcm(pcm, stt_lang, prompt=NAME_STT_PROMPT)
                except Exception:
                    transcript = ""

            elif st.pending_fulfillment:
                # Language lock: force STT language for fulfillment (prevents NL bleed when user speaks English).
                stt_lang = st.lang if st.lang in ("en", "nl") else "en"
                stt_prompt = FULFILLMENT_STT_PROMPT_NL if stt_lang == "nl" else FULFILLMENT_STT_PROMPT_EN

                try:
                    transcript = await self.oa.transcribe_pcm(pcm, stt_lang, prompt=stt_prompt)
                except Exception:
                    transcript = ""

                # RC3: Fulfillment slot is STRICT.
                # Only accept pickup/delivery. If STT drifts to generic "order" phrases,
                # treat it as NO ANSWER so the slot stays active and we reprompt.
                if transcript:
                    tn = " " + norm_simple(transcript) + " "

                    if re.search(r"\b(pickup|pick up|afhalen|afhaal)\b", tn):
                        transcript = "pickup"
                    elif re.search(r"\b(delivery|bezorgen|bezorging|thuisbezorg)\b", tn):
                        transcript = "delivery"
                    else:
                        # Ignore common STT bias outputs in this slot.
                        if re.search(
                            r"\b(ik wil bestellen|bestellen|i want to order|want to order|place an order)\b",
                            tn,
                        ):
                            transcript = ""

            elif st.pending_choice == "nan_variant":
                candidates: List[Tuple[str, str]] = []  # (txt, label)

                # Language lock: do NOT let STT auto-detect here (it causes Dutch bleed).
                stt_lang = st.lang if st.lang in ("en", "nl") else "en"
                stt_prompt = NAAN_VARIANT_STT_PROMPT_NL if stt_lang == "nl" else NAAN_VARIANT_STT_PROMPT_EN
                try:
                    txt = await self.oa.transcribe_pcm(pcm, stt_lang, prompt=stt_prompt)
                    candidates.append(((txt or "").strip(), f"naan_{stt_lang}_forced"))
                except Exception:
                    pass

                picked = ""
                for cand, label in candidates:
                    if not cand:
                        continue
                    c = " " + norm_simple(cand) + " "
                    if re.search(r"\b(plain|regular|normal|gewoon|standard|garlic|knoflook|cheese|kaas|keema|peshawari|klein|klijn|lamb|lam|lamsfilet|lams)\b", c):
                        logger.info("STT(naan_pick=%s): %s", label, cand)
                        picked = cand
                        break

                transcript = picked or (candidates[0][0] if candidates else "")

            # 3) Normal speech: allow Taj multilingual STT; tenant prompt base allowed
            else:
                stt_lang: Optional[str] = None
                if st.tenant_ref == "taj_mahal":
                    stt_lang = "nl" if st.lang == "nl" else "en"
                else:
                    stt_lang = st.lang if st.lang in ("en", "nl", "tr") else None

                stt_prompt = None
                if self.tenant_stt_prompt_enabled and st.tenant_cfg and getattr(st.tenant_cfg, "stt_prompt_base", None):
                    stt_prompt = str(st.tenant_cfg.stt_prompt_base)

                try:
                    transcript = await self.oa.transcribe_pcm(pcm, stt_lang, prompt=stt_prompt)
                except Exception:
                    transcript = ""

            # Normalize transcript early
            transcript = (transcript or "").strip()
            now_ts = time.time()

            # RC3: merge short incomplete prefixes like "I'd like ..." across a brief pause
            if getattr(st, "pending_prefix", None) and now_ts < float(getattr(st, "pending_prefix_deadline", 0.0) or 0.0):
                transcript = f"{st.pending_prefix} {transcript}".strip()
                st.pending_prefix = None
                st.pending_prefix_deadline = 0.0

            # If user utterance is just a prefix, don't interrupt; hold and wait for next chunk
            if transcript.lower() in {"i'd like", "i would like", "i want", "i'd like to", "i want to"}:
                st.pending_prefix = transcript
                st.pending_prefix_deadline = now_ts + 2.0
                await self.clear_thinking(ws)
                return

            # Optima-style cart check confirmation
            if getattr(st, "pending_cart_check", False):
                # Retry cap: never get stuck in confirmation loops if STT returns empty/unclear.
                retries = int(getattr(st, "cart_check_retries", 0) or 0)

                # Always show what STT heard during cart confirmation (helps debugging + UI transcript)
                logger.info("STT: %s", transcript)

                yn = self.oa.fast_yes_no(transcript) if hasattr(self.oa, "fast_yes_no") else None
                # RC fix: allow "yes, ..." / "ja, ..." during cart_check and keep processing the remainder
                t0 = (transcript or "").strip()
                t0_low = t0.lower()

                m = re.match(
                    r"^(yes|yeah|yep|ok|okay|sure|ja|oke|prima)\b[\s,!.:-]+(.+)$",
                    t0_low,
                )
                if m:
                    yn = "AFFIRM"
                    transcript = m.group(2).strip()

                else:
                    m2 = re.match(
                        r"^(no|nope|nah|nee)\b[\s,!.:-]+(.+)$",
                        t0_low,
                    )
                    if m2:
                        yn = "NEGATE"
                        transcript = m2.group(2).strip()

                if yn == "AFFIRM":
                    st.pending_cart_check = False
                    st.cart_check_snapshot = None
                    st.cart_check_retries = 0
                    await self.clear_thinking(ws)
                    await self._speak(ws, self._say_anything_else())
                    return

                if yn == "NEGATE":
                    st.pending_cart_check = False
                    st.cart_check_snapshot = None
                    st.cart_check_retries = 0
                    await self.clear_thinking(ws)

                    # If they said "No, ..." and continued with the change request,
                    # keep processing the remainder in the normal flow.
                    if (transcript or "").strip():
                        pass  # fall through
                    else:
                        await self._speak(
                            ws,
                            "No problem. What would you like to change?"
                            if st.lang != "nl"
                            else "Geen probleem. Wat wil je aanpassen?",
                        )
                        return
                # Non-binary input during cart check:
                # - If it contains a real action (add/change/remove/item mention), exit latch and process normally.
                # - Otherwise, keep latch and reprompt yes/no (prevents accidental checkout on 'dat is alles').
                if yn not in ("AFFIRM", "NEGATE"):
                    t_low = (transcript or "").lower()

                    has_action_words = any(k in t_low for k in (
                        # EN
                        " add", "another", " extra", " forgot", " instead of", " change", " make it", " remove", " cancel",
                        " can you add", " could you add", " please add",
                        # NL
                        " voeg", " nog een", " extra", " vergeten", " in plaats van", " verander", " maak", " haal weg", " annuleer",
                    ))

                    action_items = []
                    try:
                        if st.menu:
                            action_items = parse_add_item(st.menu, transcript, qty=1)
                    except Exception:
                        action_items = []

                    mentions_nan2 = False
                    has_variant2 = False
                    try:
                        if st.menu:
                            mentions_nan2, has_variant2, _v2 = self._check_naan_request(st.menu, transcript)
                    except Exception:
                        pass

                    actionable = bool(has_action_words or action_items or (mentions_nan2 and ("naan" in t_low or "nan" in t_low or has_variant2)))

                    if actionable:
                        # Exit latch and let normal flow handle the new intent (no yes/no nag loop)
                        st.pending_cart_check = False
                        st.cart_check_snapshot = None
                        st.cart_check_retries = 0
                        # fall through
                    else:
                        retries += 1
                        st.cart_check_retries = retries

                        cart = st.cart_check_snapshot or (st.order.summary(st.menu) if st.menu else "")
                        await self.clear_thinking(ws)

                        if retries >= 3:
                            # Give up on explicit confirmation; continue the flow safely.
                            st.pending_cart_check = False
                            st.cart_check_snapshot = None
                            st.cart_check_retries = 0
                            if not st.fulfillment_mode:
                                st.pending_fulfillment = True
                                await self._speak(ws, self._say_pickup_or_delivery())
                            else:
                                await self._speak(ws, self._say_anything_else())
                            return

                        if retries == 2:
                            msg = "Please say yes or no." if st.lang != "nl" else "Zeg alsjeblieft ja of nee."
                            await self._speak(ws, msg)
                            return

                        await self._speak(
                            ws,
                            f"Okay, so that's {cart}. Is that correct?"
                            if st.lang != "nl"
                            else f"Oké, dus dat is: {cart}. Klopt dat?",
                        )
                        return

                # AFFIRM/NEGATE handled above; if we got here, fall through


                    if retries >= 3:
                        # Give up on explicit confirmation; continue the flow safely.
                        st.pending_cart_check = False
                        st.cart_check_snapshot = None
                        st.cart_check_retries = 0
                        if not st.fulfillment_mode:
                            st.pending_fulfillment = True
                            await self._speak(ws, self._say_pickup_or_delivery())
                        else:
                            await self._speak(ws, self._say_anything_else())
                        return

                    if retries == 2:
                        msg = "Please say yes or no." if st.lang != "nl" else "Zeg alsjeblieft ja of nee."
                        await self._speak(ws, msg)
                        return

                    await self._speak(
                        ws,
                        f"Sorry, just to confirm: {cart}. Is that correct?"
                        if st.lang != "nl"
                        else f"Sorry, even checken: {cart}. Klopt dat?",
                    )
                    return

            if not transcript:
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
            # RC3: if output language is locked to English, normalize common Dutch "that's all" phrases
            # that STT may produce from accented English ("that'll be all" -> "dat zal alles zijn").
            # Common NL bleed artifacts when user speaks English with accent
            if st.lang == "en":
                transcript = re.sub(r"\bdat zal( alles)? zijn\b", "that'll be all", transcript, flags=re.IGNORECASE)
                transcript = re.sub(r"\bdat zal het zijn\b", "that'll be all", transcript, flags=re.IGNORECASE)
                transcript = re.sub(r"\bdat zal alles zijn\b", "that'll be all", transcript, flags=re.IGNORECASE)
                transcript = re.sub(r"\ben\b", "and", transcript, flags=re.IGNORECASE)

            # RC3: acoustic alias for Dutch 'lam' -> 'lamb' to improve deterministic matching.
            transcript = re.sub(r"\blam\b", "lamb", transcript, flags=re.IGNORECASE)

            logger.info("STT: %s", transcript)
            await self.send_user_text(ws, transcript)
            tnorm = " " + norm_simple(transcript) + " "

            now = time.time()

            # RC3: fast correction, if user says "No, X" right after an accidental add, rollback last add then apply correction
            if st.menu and st.last_added and (now - st.last_added_ts) < 12.0:
                t_low = (transcript or "").strip().lower()
                if t_low.startswith(("no", "nee", "noo", "nope")):
                    target_adds = parse_add_item(st.menu, transcript, qty=1)
                    if target_adds:
                        # rollback previous add(s)
                        for rid, rq in st.last_added:
                            cur = st.order.items.get(rid, 0)
                            newq = max(0, cur - rq)
                            if newq <= 0:
                                st.order.items.pop(rid, None)
                            else:
                                st.order.items[rid] = newq
                        st.last_added = []

                        corr_qty = _extract_qty_first(transcript, "en") or _extract_qty_first(transcript, "nl") or (target_adds[0][1] or 1)
                        tid = target_adds[0][0]
                        corr_qty_i = max(1, int(corr_qty))
                        st.order.add(tid, corr_qty_i)
                        st.last_added = [(tid, corr_qty_i)]
                        st.last_added_ts = now

                        # clear any pending offer, it has been superseded by a correction
                        st.offered_item_id = None
                        st.offered_label = None
                        st.offered_ts = 0.0

                        await self._speak(ws, f"Sorry, updated. I've added {st.menu.display_name(tid)}. {self._say_anything_else()}")
                        return

            # RC3: accept last offered menu item if user gives a generic confirmation like "yes, add one"
            if st.menu and st.offered_item_id and (now - st.offered_ts) < 25.0:
                mentioned = parse_add_item(st.menu, transcript, qty=1)
                if not mentioned:
                    low = norm_simple(transcript or "")
                    tokens = set(low.split())
                    # simple heuristics: confirmation/add verbs, optional qty
                    is_confirm = any(w in tokens for w in ["yes","yeah","yep","sure","please","ok","okay","ja","jazeker","correct","klopt"])
                    is_add = ("add" in low) or ("toevoeg" in low)
                    q = _extract_qty_first(transcript, "en") or _extract_qty_first(transcript, "nl") or 1
                    if is_confirm or is_add:
                        iid = st.offered_item_id
                        qi = max(1, int(q))
                        st.order.add(iid, qi)
                        st.last_added = [(iid, qi)]
                        st.last_added_ts = now

                        label = st.offered_label or (st.menu.display_name(iid) or "that item")
                        st.offered_item_id = None
                        st.offered_label = None
                        st.offered_ts = 0.0

                        await self._speak(ws, f"I've added {label} to your order. {self._say_anything_else()}")
                        return

            # If order already completed, keep it closed (RC3 deterministic)
            if getattr(st, "order_complete", False):
                await self.clear_thinking(ws)
                bye = "Thanks for your order. Goodbye!" if st.lang != "nl" else "Bedankt voor je bestelling. Tot ziens!"
                await self._speak(ws, bye)
                return


            # ==========================================================
            # 1) Global intent guard (Intent-First)
            # IMPORTANT FIX:
            # - do NOT clear pending_fulfillment / pending_name based on possibly-biased STT
            # - only clear slots when we're NOT currently slot-filling
            # ==========================================================
            is_ordering_intent = self._is_ordering_intent_global(transcript)
            lang_cmd = self._is_language_command(transcript)

            if is_ordering_intent and not (st.pending_fulfillment or st.pending_name):
                st.pending_name = False
                st.pending_fulfillment = False
            elif is_ordering_intent and (st.pending_fulfillment or st.pending_name):
                logger.info(
                    "[guard] ordering intent during slot-fill; IGNORE (pending_name=%s pending_fulfillment=%s)",
                    st.pending_name,
                    st.pending_fulfillment,
                )

            # ==========================================================
            # 2) Language command handling (Taj explicit only)
            # ==========================================================
            if lang_cmd and lang_cmd != st.lang:
                logger.info("[lang] explicit switch %s -> %s", st.lang, lang_cmd)
                st.lang = lang_cmd
                # Lock language after an explicit choice (prevents "ja"/"klein" from steering responses)
                st.lang_locked = True
                # Prefer forcing STT to the chosen language for stability
                st.stt_lang_hint = lang_cmd

            # Do NOT auto-switch Taj language. Non-Taj can still auto-detect.
            if st.tenant_ref != "taj_mahal":
                _ = detect_language_intent(
                    transcript,
                    phase=st.phase,
                    current_lang=st.lang,
                    allow_auto_detect=True,
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
                    # RC3/RC4: Taj demo defaults to English, switch to Dutch only on explicit 'Nederlands'.
                    st.lang = "en"
                    st.stt_lang_hint = "en"
                    st.lang_candidate = None
                    st.lang_candidate_count = 0
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
            if st.pending_choice == "nan_variant":
                consumed = await self._handle_pending_nan_variant(ws, transcript)
                if consumed:
                    return

            if st.pending_fulfillment:
                if self._is_obvious_out_of_scope(transcript):
                    await self.clear_thinking(ws)
                    await self._speak(ws, "I can help with the order — is this for pickup or delivery?")
                    return

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
                name_clean = self._clean_customer_name(transcript)
                if not self._looks_like_name_answer(name_clean):
                    msg = "Great. What name should I put the order under?" if st.lang != "nl" else "Prima. Op welke naam mag ik de bestelling zetten?"
                    await self.clear_thinking(ws)
                    await self._speak(ws, msg)
                    return
                st.customer_name = name_clean

                st.pending_name = False
                msg = (
                    f"Thank you, {st.customer_name}. {self._say_anything_else()}"
                    if st.lang != "nl"
                    else f"Dank je, {st.customer_name}. {self._say_anything_else()}"
                )
                await self._speak(ws, msg)
                return

            # ==========================================================
            # 5b) Checkout / confirmation guards (RC3)
            # ==========================================================
            if st.order_finalized:
                # Order already confirmed, keep it simple.
                await self.clear_thinking(ws)
                await self._speak(ws, "Your order is already confirmed. Anything else you'd like to add?")
                return

            if st.pending_confirm:
                await self.clear_thinking(ws)
                if self._is_yes_like(transcript) or self._is_order_complete_intent(transcript):
                    st.pending_confirm = False
                    st.order_finalized = True
                    name_part = f" under {st.customer_name}" if st.customer_name else ""
                    mode_part = st.fulfillment_mode or "pickup"
                    cart = st.order.summary(st.menu) if st.menu else ""
                    if st.lang != "nl":
                        await self._speak(ws, f"Perfect. Your {mode_part} order{name_part} is confirmed. {('Order: ' + cart + '. ') if cart else ''}Thank you!")
                    else:
                        await self._speak(ws, f"Top. Je {mode_part} bestelling{name_part} is bevestigd. {('Bestelling: ' + cart + '. ') if cart else ''}Dank je wel!")
                    return
                if self._is_refusal_like(transcript):
                    st.pending_confirm = False
                    await self._speak(ws, "No problem. What would you like to change?" if st.lang != "nl" else "Prima. Wat wil je wijzigen?")
                    return

                logger.info("RC3: exit pending_confirm latch on non-binary input: %r", transcript)
                st.pending_confirm = False
                # fall through to normal flow (do NOT reprompt)

            # Total amount / price query: we don't have pricing, but we can summarize and offer to confirm.
            if self._is_total_amount_query(transcript) and st.menu:
                await self.clear_thinking(ws)
                cart = st.order.summary(st.menu) or "Empty"
                st.pending_confirm = True
                if st.lang != "nl":
                    await self._speak(ws, f"I can't calculate the total price yet, but your current order is: {cart}. Would you like me to place it?")
                else:
                    await self._speak(ws, f"Ik kan de totaalprijs nog niet berekenen, maar je huidige bestelling is: {cart}. Zal ik ’m plaatsen?")
                return

            # Explicit completion intent (when we already have pickup+name)
            if self._is_order_complete_intent(transcript) and st.menu and (st.fulfillment_mode is not None) and (st.customer_name is not None):
                await self.clear_thinking(ws)
                st.order_finalized = True
                cart = st.order.summary(st.menu) or ""
                mode_part = st.fulfillment_mode
                if st.lang != "nl":
                    await self._speak(ws, f"Perfect. Your {mode_part} order under {st.customer_name} is confirmed. {('Order: ' + cart + '. ') if cart else ''}Thank you!")
                else:
                    await self._speak(ws, f"Top. Je {mode_part} bestelling op naam van {st.customer_name} is bevestigd. {('Bestelling: ' + cart + '. ') if cart else ''}Dank je wel!")
                return

                        # ==========================================================
            # 5b) Checkout confirmation (post-slots, pre-ordering/LLM)
            # ==========================================================
            if st.order_finalized:
                if self._is_order_summary_query(transcript) and st.menu:
                    cart = st.order.summary(st.menu) or "Empty"
                    await self.clear_thinking(ws)
                    if st.lang != "nl":
                        await self._speak(ws, f"Your order is: {cart}.")
                    else:
                        await self._speak(ws, f"Je bestelling is: {cart}.")
                    return
                await self.clear_thinking(ws)
                await self._speak(
                    ws,
                    "Your order is already confirmed. If you'd like to add something, just tell me."
                    if st.lang != "nl"
                    else "Je bestelling is al bevestigd. Als je nog iets wilt toevoegen, zeg het maar.",
                )
                return

            if st.pending_confirm:
                await self.clear_thinking(ws)
                if self._is_affirm(transcript):
                    st.pending_confirm = False
                    st.order_finalized = True
                    if st.lang != "nl":
                        await self._speak(ws, f"Perfect. Your order is confirmed for pickup under {st.customer_name or 'your name'}.")
                    else:
                        await self._speak(ws, f"Top. Je bestelling is bevestigd voor afhalen op naam van {st.customer_name or 'jou'}.")
                    return
                if self._is_negative(transcript):
                    st.pending_confirm = False
                    await self._speak(
                        ws,
                        "No problem. What would you like to change?"
                        if st.lang != "nl"
                        else "Geen probleem. Wat wil je aanpassen?",
                    )
                    return
                logger.info("RC3: exit pending_confirm latch on non-binary input: %r", transcript)
                st.pending_confirm = False
                # fall through to normal flow (do NOT prompt yes/no)

            if self._is_order_summary_query(transcript) and st.menu:
                cart = st.order.summary(st.menu) or "Empty"
                await self.clear_thinking(ws)
                if st.lang != "nl":
                    await self._speak(ws, f"Your current order is: {cart}.")
                else:
                    await self._speak(ws, f"Je huidige bestelling is: {cart}.")
                return

            if self._is_checkout_intent(transcript):
                await self.clear_thinking(ws)
                if not st.menu or not st.order.items:
                    await self._speak(ws, "I don't have any items in your order yet. What would you like to order?")
                    return
                if not st.fulfillment_mode:
                    st.pending_fulfillment = True
                    await self._speak(ws, self._say_pickup_or_delivery())
                    return
                if st.fulfillment_mode == "pickup" and not st.customer_name:
                    st.pending_name = True
                    await self._speak(ws, "Great. What name should I put the order under?" if st.lang != "nl" else "Prima. Op welke naam mag ik de bestelling zetten?")
                    return
                cart = st.order.summary(st.menu) or "Empty"
                st.pending_confirm = True
                if st.lang != "nl":
                    await self._speak(ws, f"Just to confirm, your order is: {cart}. Should I place it?")
                else:
                    await self._speak(ws, f"Even checken, je bestelling is: {cart}. Zal ik hem plaatsen?")
                return

            # ==========================================================
            # 5b) Deterministic checkout / close-call (prevents LLM inventing flows)
            # ==========================================================
            if self._is_close_call_intent(transcript):
                st.order_complete = True
                await self.clear_thinking(ws)
                bye = "Thank you for your order. Goodbye!" if st.lang != "nl" else "Dank je wel voor je bestelling. Doei!"
                await self._speak(ws, bye)
                return

            # RC3.1: When we've already asked "Anything else?" a bare "no/nee" should close the order,
            # even if it doesn't include "that's all".
            if st.menu and st.order.items and norm_simple(transcript).strip() in ("no", "nee", "nope", "nah"):
                # Reuse the done-intent flow below.
                transcript = "that will be all" if st.lang != "nl" else "dat is alles"
                tnorm = " " + norm_simple(transcript) + " "

            if self._is_done_intent(transcript):
                # If pickup/delivery not yet known, we still need that before "confirming"
                if not st.fulfillment_mode:
                    st.pending_fulfillment = True
                    await self.clear_thinking(ws)
                    await self._speak(ws, self._say_pickup_or_delivery())
                    return

                # If pickup and no name yet, we still need a name
                if st.fulfillment_mode == "pickup" and not st.customer_name:
                    st.pending_name = True
                    await self.clear_thinking(ws)
                    await self._speak(ws, "Great. What name should I put the order under?" if st.lang != "nl" else "Prima. Op welke naam mag ik de bestelling zetten?")
                    return

                st.order_complete = True
                await self.clear_thinking(ws)
                cart = st.order.summary(st.menu) if st.menu else ""
                if st.lang != "nl":
                    who = f" under the name {st.customer_name}" if st.customer_name else ""
                    mode = "for pickup" if st.fulfillment_mode == "pickup" else "for delivery"
                    msg = f"Perfect, I've got your order{who} {mode}: {cart}. Thank you, goodbye!"
                else:
                    who = f" op naam van {st.customer_name}" if st.customer_name else ""
                    mode = "om af te halen" if st.fulfillment_mode == "pickup" else "voor bezorgen"
                    msg = f"Top, ik heb je bestelling{who} {mode}: {cart}. Bedankt, doei!"
                await self._speak(ws, msg)
                return

            # If user repeats pickup/delivery after it's already set, just confirm (avoid LLM pickup-time fantasies)
            if st.fulfillment_mode and self._is_pickup_delivery_mention(transcript):
                await self.clear_thinking(ws)
                if st.fulfillment_mode == "pickup":
                    if st.customer_name:
                        await self._speak(ws, f"You're set for pickup under {st.customer_name}. {self._say_anything_else()}" if st.lang != "nl" else f"Je staat op afhalen op naam van {st.customer_name}. {self._say_anything_else()}")
                    else:
                        st.pending_name = True
                        await self._speak(ws, "Great. What name should I put the order under?" if st.lang != "nl" else "Prima. Op welke naam mag ik de bestelling zetten?")
                else:
                    await self._speak(ws, "You're set for delivery. Anything else you'd like to add?" if st.lang != "nl" else "Je staat op bezorgen. Wil je nog iets toevoegen?")
                return


# ==========================================================
            # 6) Ordering logic (Deterministic add + naan scoping + orchestrator hook)
            # FIX: initialize flags unconditionally (prevents NameError on non-add turns)
            # RC3.1: Avoid accidental adds when the user is clearly asking a question about an item.
            # Example: "Is lamb tikka spicy?" should be Q&A, not an add.
            raw_q = (transcript or "").strip()
            norm_q = norm_simple(raw_q)
            is_questionish = ("?" in raw_q) or norm_q.startswith((
                "is ", "are ", "which ", "what ", "how ", "can ", "could ", "do ", "does ",
                "wat ", "welke ", "hoe ", "kan ", "kun ", "is het ", "zijn ",
            ))
            has_explicit_add_intent = any(v in (" " + norm_q + " ") for v in [
                " i want ", " i'd like ", " i would like ", " can i have ", " please add ", " add ", " order ",
                " ik wil ", " graag ", " bestel ", " voeg ", " mag ik ",
            ])
            allow_deterministic_add = (not is_questionish) or has_explicit_add_intent

            added_any = False
            added_ids: List[str] = []
            cart_before = st.order.summary(st.menu) if st.menu else ""
            items_before = dict(st.order.items)

            # Extract quantity once (support EN/NL), default 1
            raw_qty = (_extract_qty_first(transcript, "en") or _extract_qty_first(transcript, "nl"))
            add_qty = (raw_qty or 1)
            effective_qty = add_qty
            now_ts = time.time()

            # RC3.4: held-quantity for split utterances like "Yes, one ..." + next turn item
            if st.pending_qty_hold and now_ts < st.pending_qty_deadline:
                effective_qty = st.pending_qty_hold
                st.pending_qty_hold = None
                st.pending_qty_deadline = 0.0

            # Only run add/slot logic if we have a menu snapshot
            adds: List[Tuple[str, int]] = []
            mentions_nan = False
            has_variant = False
            variant = None

            if st.menu and allow_deterministic_add:
                # Optional orchestrator for very short/ambiguous items
                orch_id = self._maybe_orchestrator_match_item(st.menu, transcript, qty=effective_qty)
                if orch_id:
                    st.order.add(orch_id, max(1, int(effective_qty or 1)))
                    added_any = True
                    added_ids.append(orch_id)

                # Standard deterministic matcher (alias_map hits)
                if not added_any:
                    adds = parse_add_item(st.menu, transcript, qty=effective_qty)

                # RC3: suppress implicit re-adds when user merely repeats an item name
                # (e.g., "Lamb Karahi" during spice/fulfillment/name phases).
                # Only suppress if:
                # - exactly one item hit
                # - user did NOT express explicit add intent
                # - user did NOT state an explicit quantity in the utterance
                # - item is already present in the cart
                if adds and len(adds) == 1 and (not has_explicit_add_intent) and (raw_qty is None):
                    _iid, _q0 = adds[0]
                    if isinstance(getattr(st.order, "items", None), dict) and (st.order.items.get(_iid, 0) or 0) > 0:
                        logger.info("RC3: suppress implicit re-add on bare item mention: %s", _iid)
                        adds = []

                # Naan detection (generic + explicit mentions)
                # --- RC3: handle split edits like "one plain naan and one garlic naan" deterministically ---
                t_low = (transcript or "").lower()
                if st.menu and ("naan" in t_low or "nan" in t_low or "naam" in t_low):
                    has_plain_word = any(x in t_low for x in ["plain", "regular", "normal", "gewoon", "normaal", "standaard"])
                    has_garlic = ("garlic" in t_low) or ("knoflook" in t_low)

                    # If user says "one naan and one garlic naan", treat the first "naan" as plain/default naan.
                    has_plain = has_plain_word or ((" one naan" in t_low or " 1 naan" in t_low or " one nan" in t_low or " 1 nan" in t_low) and has_garlic)

                    # Only trigger on true split intent (both variants mentioned)
                    if has_plain and has_garlic:
                        plain_iid = self._find_naan_item_for_variant(st.menu, "plain")
                        garlic_iid = self._find_naan_item_for_variant(st.menu, "garlic")

                        if plain_iid and garlic_iid and plain_iid != garlic_iid:
                            # RC3: parse per-variant quantities deterministically (EN/NL + digits)
                            def _qty_from_tok(tok: str):
                                t = (tok or '').strip().lower()
                                if t.isdigit():
                                    q = int(t)
                                    return q if 1 <= q <= 20 else None
                                m = {
                                    # EN
                                    'one': 1, 'two': 2, 'three': 3, 'four': 4, 'five': 5,
                                    # NL
                                    'een': 1, 'één': 1, 'twee': 2, 'drie': 3, 'vier': 4, 'vijf': 5,
                                }
                                return m.get(t)

                            tlow2 = t_low
                            plain_qty = None
                            garlic_qty = None

                            # LAST match wins: this handles phrases like
                            # 'instead of two plain naan, one plain naan and one garlic naan'
                            g_all = re.findall(r"\b(\d+|one|two|three|four|five|een|één|twee|drie|vier|vijf)\b\s+(?:\w+\s+){0,2}?(garlic|knoflook)\s+(naan|nan)\b", tlow2)
                            if g_all:
                                garlic_qty = _qty_from_tok(g_all[-1][0])

                            p_all = re.findall(r"\b(\d+|one|two|three|four|five|een|één|twee|drie|vier|vijf)\b\s+(?:\w+\s+){0,2}?(plain|regular|normal|gewoon|normaal|standaard)\s+(naan|nan)\b", tlow2)
                            if p_all:
                                plain_qty = _qty_from_tok(p_all[-1][0])

                            # fallback: if user says 'one naan and one garlic naan', infer first qty as plain
                            if plain_qty is None:
                                mf = re.findall(r"\b(\d+|one|two|three|four|five|een|één|twee|drie|vier|vijf)\b", tlow2)
                                if mf:
                                    plain_qty = _qty_from_tok(mf[0])

                            plain_qty = int(plain_qty or 1)
                            garlic_qty = int(garlic_qty or 1)

                            st.order.set_qty(plain_iid, plain_qty)
                            st.order.set_qty(garlic_iid, garlic_qty)

                            # Remove any other naan ids to avoid "2x Nan" leftovers
                            for iid in list((st.order.items or {}).keys()):
                                if self._is_nan_item(st.menu, iid) and iid not in (plain_iid, garlic_iid):
                                    st.order.items.pop(iid, None)

                            # Clear naan slot state and confirm cart
                            st.pending_choice = None
                            st.pending_qty = 1
                            st.nan_prompt_count = 0
                            await self.clear_thinking(ws)

                            cart = st.order.summary(st.menu) if st.menu else ""
                            st.pending_cart_check = True
                            st.cart_check_snapshot = cart
                            await self._speak(
                                ws,
                                f"Okay, so that's {cart}. Is that correct?"
                                if st.lang != "nl"
                                else f"Oké, dus dat is: {cart}. Klopt dat?",
                            )
                            return

                # --- RC fix: handle naan quantity UPDATE without re-entering variant slot ---
                t_low = (transcript or "").lower()
                if st.menu and (" naan" in t_low or "nan" in t_low) and any(m in t_low for m in ("instead of", "make it", "change", "change to", "into", "in plaats van", "doe er", "maak er", "maak het")):
                    from src.api.parser.quantity import extract_quantity_1_to_10
                    new_qty = extract_quantity_1_to_10(transcript)
                    if isinstance(new_qty, int) and new_qty >= 1:
                        logger.info("RC3: naan_qty_update detected new_qty=%s transcript=%r", new_qty, transcript)
                        naan_ids = [iid for iid in (st.order.items or {}).keys() if self._is_nan_item(st.menu, iid)]
                        if len(naan_ids) == 1:
                            iid = naan_ids[0]
                            st.order.items[iid] = int(new_qty)
                            st.pending_choice = None
                            st.pending_qty = 1
                            st.nan_prompt_count = 0
                            await self.clear_thinking(ws)
                            cart = st.order.summary(st.menu) if st.menu else ""
                            st.pending_cart_check = True
                            st.cart_check_snapshot = cart
                            logger.info("RC3: naan_qty_update speaking cart_check cart=%r", cart)
                            await self._speak(ws, f"Okay, so that\x27s {cart}. Is that correct?" if st.lang != "nl" else f"Oké, dus dat is: {cart}. Klopt dat?")
                            return

                mentions_nan, has_variant, variant = self._check_naan_request(st.menu, transcript)

                # RC3.1: acoustic alias inside naan context ("lamb" -> "plain" if no lamb-naan exists)
                if mentions_nan and not has_variant:
                    t_lower = transcript.lower()
                    if ("lamb" in t_lower) or ("lam" in t_lower):
                        opts = self._naan_options_from_menu(st.menu)
                        has_real_lamb_naan = any("lamb" in (lbl or "").lower() for lbl, _iid in opts)
                        if not has_real_lamb_naan:
                            variant = "plain"
                            has_variant = True
                            logger.info("RC3: Applied acoustic alias Lamb->Plain in naan slot")

                # Logic A: naan mentioned but variant unclear -> latch and reprompt (do NOT advance)
                if mentions_nan and not has_variant:
                    # Add any non-naan items we did recognize (e.g. "Butter Chicken and naan")
                    for item_id, q in [x for x in adds if not self._is_nan_item(st.menu, x[0])]:
                        st.order.add(item_id, q)
                        added_any = True
                        added_ids.append(item_id)

                    st.pending_choice = "nan_variant"
                    st.pending_qty = max(1, int(effective_qty or 1))
                    st.nan_prompt_count = 0
                    await self.clear_thinking(ws)
                    await self._speak(ws, self._naan_optima_prompt(list_mode="short"))
                    return

                # Logic B: naan + variant -> add the matched naan item, then add remaining items
                if mentions_nan and has_variant:
                    iid = self._find_naan_item_for_variant(st.menu, variant or "")
                    if iid:
                        st.order.add(iid, max(1, int(effective_qty or 1)))
                        added_any = True
                        added_ids.append(iid)
                        # Remove naan from adds to avoid double counting
                        adds = [x for x in adds if x[0] != iid and not self._is_nan_item(st.menu, x[0])]

                # Add remaining items (excluding duplicates and naan already handled)
                for item_id, q in adds:
                    if item_id in added_ids:
                        continue
                    if mentions_nan and has_variant and self._is_nan_item(st.menu, item_id):
                        continue
                    st.order.add(item_id, q)
                    added_any = True
                    added_ids.append(item_id)

            # RC3.4: if user only gave a quantity and we couldn't add an item, hold it briefly
            qty = add_qty
            if st.menu and (not added_any) and qty and (not adds) and (not st.pending_choice):
                # Heuristic: quantity / acknowledgement without a dish
                tok = norm_simple(transcript).strip()
                if tok in ("one", "two", "three", "1", "2", "3", "yes", "yeah", "ja", "sure"):
                    st.pending_qty_hold = int(qty)
                    st.pending_qty_deadline = now_ts + 6.0
                    await self.clear_thinking(ws)
                    await self._speak(ws, "Sure, which item would you like to add?")
                    return

            # If cart changed, advance to fulfillment/name/anything-else prompts (deterministic)
            # RC3: store last-added delta so the user can correct immediately ("No, I meant X")
            if st.menu and added_any:
                deltas: List[Tuple[str, int]] = []
                for iid, newq in st.order.items.items():
                    oldq = items_before.get(iid, 0)
                    if newq > oldq:
                        deltas.append((iid, newq - oldq))
                if deltas:
                    st.last_added = deltas
                    st.last_added_ts = time.time()
            cart_after = st.order.summary(st.menu) if st.menu else ""
            if added_any and st.menu and cart_after and cart_after != (cart_before or ""):
                await self.clear_thinking(ws)

                # RC3: do NOT auto-start checkout (pickup/delivery) after any add.
                # Only start checkout if the user explicitly indicates they're done / want to place the order.
                t_norm = " " + norm_simple(transcript) + " "
                checkout_intent = any(k in t_norm for k in [
                    " that's all ", " that is all ", " thats all ", " that's it ", " thats it ",
                    " nothing else ", " no more ", " done ", " finish ", " finalize ",
                    " checkout ", " check out ", " place the order ", " confirm ", " complete the order ",
                    " dat is alles ", " dat was alles ", " niks meer ", " niets meer ", " klaar ",
                    " afronden ", " rond af ", " afrekenen ", " bevestig ", " bestelling plaatsen ",
                ])

                if (not st.fulfillment_mode) and checkout_intent:
                    st.pending_fulfillment = True
                    await self._speak(ws, self._say_pickup_or_delivery())
                    return

                if st.fulfillment_mode == "pickup" and not st.customer_name:
                    st.pending_name = True
                    await self._speak(ws, "Great. What name should I put the order under?")
                    return

                await self._speak(ws, self._say_anything_else())
                return

            # RC3: explicit "done/checkout" intent must bypass LLM and start fulfillment flow
            # (Prevents LLM from inventing irrelevant steps like "spice level".)
            if st.menu:
                t_norm = " " + norm_simple(transcript) + " "
                checkout_intent = any(k in t_norm for k in [
                    " that's all ", " that is all ", " thats all ", " that's it ", " thats it ",
                    " nothing else ", " no more ", " done ", " finish ", " finalize ",
                    " checkout ", " check out ", " place the order ", " confirm ", " complete the order ",
                    " dat is alles ", " dat was alles ", " niks meer ", " niets meer ", " klaar ",
                    " afronden ", " rond af ", " afrekenen ", " bevestig ", " bestelling plaatsen ",
                    " no that will be all ", " that will be all ", " dat will be all ",
                ])

                cart_now = st.order.summary(st.menu) if st.menu else ""
                if checkout_intent and cart_now:
                    await self.clear_thinking(ws)

                    if not st.fulfillment_mode:
                        st.pending_fulfillment = True
                        await self._speak(ws, self._say_pickup_or_delivery())
                        return

                    if st.fulfillment_mode == "pickup" and not st.customer_name:
                        st.pending_name = True
                        await self._speak(ws, "Great. What name should I put the order under?")
                        return

                    # If we already have fulfillment + name (or delivery), recap briefly
                    await self._speak(
                        ws,
                        f"Perfect. Your order is: {cart_now}. Anything else?"
                        if st.lang != "nl"
                        else f"Perfect. Je bestelling is: {cart_now}. Nog iets?"
                    )
                    return
            # RC1-3: apply deterministic qty updates before LLM
            if st.menu:
                applied = self._maybe_orchestrator_apply_qty_update(st.menu, transcript)
                if applied is not None:
                    _iid, new_qty = applied
                    await self.clear_thinking(ws)
                    msg = f"Got it — quantity set to {new_qty}." if st.lang != "nl" else f"Goed — aantal aangepast naar {new_qty}."
                    await self._speak(ws, msg)
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