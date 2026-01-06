from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import httpx
from openai import AsyncOpenAI

from .audio import pcm16_to_wav

logger = logging.getLogger("taj-agent")


# -------------------------
# Structured Intent schema (LLM -> deterministic router)
# -------------------------
@dataclass
class IntentItem:
    name: str
    qty: int = 1


@dataclass
class IntentResult:
    intent: str
    items: List[IntentItem]
    category: Optional[str]
    reply: Optional[str]
    confidence: float
    raw: Dict[str, Any]

    @staticmethod
    def empty() -> "IntentResult":
        return IntentResult(
            intent="UNKNOWN",
            items=[],
            category=None,
            reply=None,
            confidence=0.0,
            raw={},
        )


# -------------------------
# Small helpers
# -------------------------
_INTENT_SET = {
    "ADD",
    "REPLACE",
    "REMOVE",
    "ORDER_SUMMARY",
    "CATEGORY_QUERY",
    "MORE",
    "TASTY",
    "GREETING",
    "END",
    "CLARIFY",
    "UNKNOWN",
}

_CATEGORY_SET = {"lamb", "chicken", "biryani", "vegetarian"}


def _clamp_int(x: Any, lo: int, hi: int, default: int) -> int:
    try:
        v = int(x)
    except Exception:
        t = (default or "").strip()
        logger.info("STT_RESULT len=%s text=%r", len(t), t[:120])

        return default
    return max(lo, min(hi, v))


def _clamp_float(x: Any, lo: float, hi: float, default: float) -> float:
    try:
        v = float(x)
    except Exception:
        return default
    return max(lo, min(hi, v))


def _safe_json_loads(s: Any) -> Optional[Dict[str, Any]]:
    if not s:
        return None
    if isinstance(s, dict):
        return s
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _norm_lang(lang: Optional[str]) -> Optional[str]:
    """
    Normalize language hints to something we can pass to STT safely.

    OpenAI accepts ISO-like language tags for transcription. We keep this permissive:
    - 'en', 'nl', 'tr', 'hi', etc.
    - We also allow short tags like 'en-US' (we'll pass through).
    """
    if not lang:
        return None
    l = str(lang).strip()
    if not l:
        return None
    # Keep it short-ish and sane
    if len(l) > 16:
        return None
    return l


class OpenAIClient:
    def __init__(
        self,
        api_key: str,
        stt_model: str,
        chat_model: str,
        tts_model: str,
        sample_rate: int,
    ):
        self.api_key = api_key
        self.stt_model = stt_model
        self.chat_model = chat_model
        self.tts_model = tts_model
        self.sample_rate = int(sample_rate)
        self.sdk = AsyncOpenAI(api_key=api_key)

    # -------------------------
    # STT
    # -------------------------
    async def transcribe_pcm(
        self,
        pcm16: bytes,
        lang: Optional[str],
        prompt: Optional[str] = None,
        *,
        debug_tag: Optional[str] = None,
    ) -> str:
        """
        Transcribe PCM16 audio to text.

        IMPORTANT:
        - If `lang` is provided, we pass it through as `language` to reduce language drift.
        - `prompt` is optional and can bias the decoder (use carefully per slot).
        - `debug_tag` is only for logging to trace which STT path was used.
        """
        if not pcm16:
            return ""

        wav_bytes = pcm16_to_wav(pcm16, self.sample_rate)
        f = io.BytesIO(wav_bytes)
        f.name = "audio.wav"

        kwargs: Dict[str, Any] = {"model": self.stt_model, "file": f}

        if prompt:
            kwargs["prompt"] = str(prompt)

        stt_lang = _norm_lang(lang)
        if stt_lang:
            # Make this permissive. The API supports many tags; restricting to a small set
            # causes silent "no language" behavior and drift.
            kwargs["language"] = stt_lang

        # Debug log for field validation during RC3
        try:
            if debug_tag or stt_lang or prompt:
                logger.info(
                    "STT_CALL tag=%s model=%s lang=%s prompt_len=%s",
                    debug_tag or "",
                    self.stt_model,
                    stt_lang or "",
                    len(prompt) if prompt else 0,
                )

                pcm_bytes = len(pcm16 or b"")
                est_ms = int((pcm_bytes / 32000.0) * 1000.0) if pcm_bytes else 0
                logger.info("STT_PCM bytes=%s est_ms=%s", pcm_bytes, est_ms)
        except Exception:
            pass

        resp = await self.sdk.audio.transcriptions.create(**kwargs)
        text = (getattr(resp, "text", "") or "").strip()

        # Debug log for result visibility during RC3/RC1-3 testing
        try:
            if debug_tag or stt_lang or prompt:
                logger.info("STT_RESULT len=%s text=%r", len(text), text[:120])
        except Exception:
            pass

        return text

    # -------------------------
    # Chat (freeform)
    # -------------------------
    async def chat(self, messages: List[Dict[str, str]], temperature: float = 0.3) -> str:
        resp = await self.sdk.chat.completions.create(
            model=self.chat_model,
            messages=messages,
            temperature=float(temperature),
        )
        return (resp.choices[0].message.content or "").strip()

    # -------------------------
    # TTS
    # -------------------------
    async def tts_mp3_bytes(self, text: str, voice: str, instructions: str) -> bytes:
        url = "https://api.openai.com/v1/audio/speech"
        headers = {"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"}
        payload: Dict[str, Any] = {"model": self.tts_model, "voice": voice, "input": text}
        if instructions:
            payload["instructions"] = instructions

        async with httpx.AsyncClient(timeout=60) as hc:
            r = await hc.post(url, headers=headers, json=payload)
            if r.status_code != 200:
                raise RuntimeError(f"TTS HTTP {r.status_code}: {r.text[:500]}")
            return r.content

    # -------------------------
    # OPTIONAL: ultra-fast intent helpers (no LLM)
    # -------------------------
    def fast_yes_no(self, text: str) -> Optional[str]:
        """
        Returns 'AFFIRM'/'NEGATE' for ultra-short exact responses, else None.
        This is optional sugar for SessionController; safe to ignore.
        """
        t = (text or "").strip().lower()
        if not t:
            return None
        # keep this deliberately tiny (fast-path)
        if t in {"yes", "yeah", "yep", "ok", "okay", "sure", "ja", "prima", "oke"}:
            return "AFFIRM"
        if t in {"no", "nope", "nee"}:
            return "NEGATE"
        return None

    # -------------------------
    # Structured intent call (Semantic Router) - TOOL CALLING (strict)
    # -------------------------
    async def get_structured_intent(
        self,
        *,
        text: str,
        lang: str,
        menu_context: str,
        current_cart: str,
        last_offer: Optional[str] = None,
        last_category: Optional[str] = None,
        last_listed: Optional[List[str]] = None,
    ) -> IntentResult:
        """
        Semantic Router: classify user's text into a small intent set and extract parameters.

        This method MUST NOT mutate state. Deterministic engine applies order math.
        Uses tool/function calling with strict schema.
        """
        t = (text or "").strip()
        if not t:
            return IntentResult.empty()

        lang = (lang or "en").strip().lower()

        last_listed_str = ""
        if last_listed:
            last_listed_str = "\n".join([f"- {x}" for x in last_listed[:12]])

        system = f"""
You are the Semantic Router for a restaurant voice ordering system (Taj Mahal).

Your job:
- Determine the user's intent (ADD / REPLACE / REMOVE / ORDER_SUMMARY / CATEGORY_QUERY / MORE / TASTY / GREETING / END / CLARIFY / UNKNOWN)
- Extract item names and quantities when relevant.
- NEVER invent menu items. Use MENU_CONTEXT only.
- When the user says "instead of / actually / change / I meant / in plaats van / ik bedoelde" => REPLACE (overwrite qty, do not add).
- When user asks what is in their order/cart => ORDER_SUMMARY.
- When user asks what's available in a category => CATEGORY_QUERY (category in lamb/chicken/biryani/vegetarian if clear).
- When user asks "more" after a list => MORE.
- When user asks "which is tasty/popular" after a list => TASTY.
- If unclear what item or quantity => CLARIFY with a short question in the same language.
Language: {lang}
""".strip()

        user = f"""
USER_TEXT:
{t}

CURRENT_CART:
{current_cart or "Empty"}

LAST_OFFER:
{last_offer or ""}

LAST_CATEGORY:
{last_category or ""}

LAST_LISTED_ITEMS:
{last_listed_str}

MENU_CONTEXT:
{menu_context or "Menu empty."}
""".strip()

        tools = [
            {
                "type": "function",
                "function": {
                    "name": "resolve_order_intent",
                    "strict": True,
                    "description": "Return the user's intent and any referenced menu items with quantities.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "intent": {"type": "string", "enum": sorted(list(_INTENT_SET))},
                            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                            "items": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "name": {"type": "string"},
                                        "qty": {"type": "integer", "minimum": 1, "maximum": 20},
                                    },
                                    "required": ["name", "qty"],
                                    "additionalProperties": False,
                                },
                            },
                            "category": {
                                "type": ["string", "null"],
                                "enum": [None, "lamb", "chicken", "biryani", "vegetarian"],
                            },
                            "reply": {"type": ["string", "null"]},
                        },
                        "required": ["intent", "confidence", "items", "category", "reply"],
                        "additionalProperties": False,
                    },
                },
            }
        ]

        try:
            resp = await self.sdk.chat.completions.create(
                model=self.chat_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                tools=tools,
                tool_choice={"type": "function", "function": {"name": "resolve_order_intent"}},
            )
        except Exception as e:
            logger.warning("get_structured_intent tool-call failed: %s", e)
            return IntentResult.empty()

        msg = resp.choices[0].message
        raw_obj: Dict[str, Any] = {}

        # Tool call path
        try:
            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                return IntentResult.empty()

            args = tool_calls[0].function.arguments
            obj = _safe_json_loads(args)
            if not isinstance(obj, dict):
                return IntentResult.empty()

            raw_obj = obj
        except Exception:
            return IntentResult.empty()

        intent = str(raw_obj.get("intent") or "UNKNOWN").strip().upper()
        if intent not in _INTENT_SET:
            intent = "UNKNOWN"

        conf = _clamp_float(raw_obj.get("confidence"), 0.0, 1.0, 0.0)

        items: List[IntentItem] = []
        items_raw = raw_obj.get("items") or []
        if isinstance(items_raw, list):
            for it in items_raw[:6]:
                if not isinstance(it, dict):
                    continue
                name = str(it.get("name") or "").strip()
                if not name:
                    continue
                qty = _clamp_int(it.get("qty"), 1, 20, 1)
                items.append(IntentItem(name=name, qty=qty))

        category = raw_obj.get("category", None)
        if category is not None:
            c = str(category).strip().lower()
            category = c if c in _CATEGORY_SET else None
        else:
            category = None

        reply = raw_obj.get("reply", None)
        if reply is not None:
            r = str(reply).strip()
            reply = r if r else None

        return IntentResult(
            intent=intent,
            items=items,
            category=category,
            reply=reply,
            confidence=conf,
            raw=raw_obj,
        )
