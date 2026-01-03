# src/platform/apply_actions.py
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import asyncpg

from src.domains.base import ActionType, DomainAction
from src.platform.interaction_logger import InteractionLogger, InteractionLogInput, redact_pii


# -----------------------------
# Tool registry (platform-owned)
# -----------------------------

class ToolRegistry:
    """
    Tools are async callables:
      async def tool(payload: dict, ctx: dict) -> dict
    """

    def __init__(self):
        self._tools: Dict[str, Any] = {}

    def register(self, name: str, fn: Any) -> None:
        self._tools[name] = fn

    def has(self, name: str) -> bool:
        return name in self._tools

    async def call(self, name: str, payload: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        if name not in self._tools:
            raise KeyError(f"Tool not registered: {name}")
        return await self._tools[name](payload, ctx)


# -----------------------------
# Render output (platform-owned)
# -----------------------------

@dataclass
class RenderOutput:
    text: str
    mode: str  # "override" | "template" | "text"
    lang: str
    meta: Dict[str, Any]


# -----------------------------
# CaseStore (aligned to schema)
# -----------------------------

class CaseStore:
    """
    public.cases columns (from your \d):
      - case_id uuid pk
      - tenant_id uuid not null
      - domain_type text not null
      - external_id text null
      - status text not null default 'open'
      - priority text not null default 'P3'
      - metadata jsonb not null default {}
      - created_at timestamptz not null default now()
      - updated_at timestamptz not null default now()
    """

    def __init__(self, pool: asyncpg.Pool):
        self.pool = pool

    async def update_case(self, case_id: str, *, status: Optional[str] = None, priority: Optional[str] = None) -> None:
        sets: List[str] = []
        args: List[Any] = []
        idx = 1

        if status is not None:
            sets.append(f"status = ${idx}")
            args.append(status)
            idx += 1

        if priority is not None:
            sets.append(f"priority = ${idx}")
            args.append(priority)
            idx += 1

        if not sets:
            return

        # Always bump updated_at for auditability
        sets.append("updated_at = now()")

        args.append(case_id)
        q = f"UPDATE public.cases SET {', '.join(sets)} WHERE case_id = ${idx}"

        async with self.pool.acquire() as conn:
            await conn.execute(q, *args)


# -----------------------------
# Apply Actions (platform-owned)
# -----------------------------

async def apply_actions(
    *,
    pool: asyncpg.Pool,
    logger: InteractionLogger,
    tools: ToolRegistry,
    tenant_ctx: Dict[str, Any],              # {tenant_id, tenant_ref, domain_type, default_language, ...}
    case_state: Dict[str, Any],              # must contain at least {case_id, turn_id}
    transcript_raw: Optional[str],
    frame_payload: Dict[str, Any],           # serialized IntentFrame (frame.__dict__)
    actions: List[DomainAction],
) -> RenderOutput:
    """
    Apply phase (ADR-001):
      - update cases.status/priority
      - execute REQUEST_TOOL actions
      - compute render output (P0 safety override bypasses LLM)
      - write audit row to public.interactions (with redaction)
    """
    t0 = time.perf_counter()

    case_id = str(case_state["case_id"])
    turn_id = int(case_state.get("turn_id", 1))

    lang = (
        frame_payload.get("lang")
        or tenant_ctx.get("lang")
        or tenant_ctx.get("default_language")
        or "nl"
    )

    case_store = CaseStore(pool)

    # What actually happened (store as JSON arrays per schema expectations)
    actions_taken: List[Dict[str, Any]] = []
    tool_calls: List[Dict[str, Any]] = []

    # Render selection
    render_mode = "text"
    render_text = ""

    override_reply: Optional[Dict[str, Any]] = None
    reply_template: Optional[Dict[str, Any]] = None

    new_priority: Optional[str] = None
    new_status: Optional[str] = None

    # 1) Apply deterministic case updates + collect render intents
    for a in actions:
        if a.type == ActionType.SET_PRIORITY:
            new_priority = a.payload.get("priority")
            actions_taken.append({"type": "SET_PRIORITY", "priority": new_priority})
        elif a.type == ActionType.SET_STATUS:
            new_status = a.payload.get("status")
            actions_taken.append({"type": "SET_STATUS", "status": new_status})
        elif a.type == ActionType.OVERRIDE_REPLY:
            override_reply = a.payload
            actions_taken.append({"type": "OVERRIDE_REPLY", "mode": override_reply.get("mode")})
        elif a.type == ActionType.REPLY_TEMPLATE:
            reply_template = a.payload
            actions_taken.append({"type": "REPLY_TEMPLATE", "template": reply_template.get("template")})
        elif a.type == ActionType.REQUEST_DISPATCHER_CALLBACK:
            actions_taken.append({"type": "REQUEST_DISPATCHER_CALLBACK", "urgent": bool(a.payload.get("urgent", False))})
        elif a.type == ActionType.SAVE_TRIAGE_FACTS:
            actions_taken.append({"type": "SAVE_TRIAGE_FACTS", "payload": a.payload})
        elif a.type == ActionType.REQUEST_TOOL:
            # executed below
            pass
        else:
            actions_taken.append({"type": "UNKNOWN_ACTION", "value": str(a.type)})

    # 2) Persist case updates (platform truth)
    await case_store.update_case(case_id, status=new_status, priority=new_priority)

    # 3) Execute tools (platform-owned)
    tool_results: Dict[str, Any] = {}
    for a in actions:
        if a.type != ActionType.REQUEST_TOOL:
            continue

        name = a.payload.get("name")
        payload = a.payload.get("payload", {}) or {}
        if not name:
            tool_calls.append({"name": None, "ok": False, "error": "missing_tool_name"})
            continue

        call_ctx = {
            "tenant": tenant_ctx,
            "case_id": case_id,
            "turn_id": turn_id,
            "lang": lang,
        }

        if not tools.has(name):
            tool_calls.append({"name": name, "payload": payload, "ok": False, "error": "tool_not_registered"})
            continue

        try:
            result = await tools.call(name, payload, call_ctx)
            tool_results[name] = result
            tool_calls.append({"name": name, "payload": payload, "ok": True, "result": _safe_tool_result(result)})
        except Exception as e:
            tool_calls.append({"name": name, "payload": payload, "ok": False, "error": str(e)})

    # 4) Render (platform-owned). P0 safety override MUST bypass LLM.
    if override_reply and override_reply.get("mode") == "SAFETY_SCRIPT":
        render_mode = "override"
        render_text = (override_reply.get("text") or "").strip()
        lang = override_reply.get("lang", lang)
    elif reply_template:
        render_mode = "template"
        render_text = _render_template(reply_template, tool_results, lang)
    else:
        render_text = _fallback_response(lang)

    # 5) Write interaction audit trail (aligned to schema)
    raw = transcript_raw
    redacted, pii_flags = redact_pii(raw or "")

    latency_ms = int((time.perf_counter() - t0) * 1000)

    decision_payload = {
        "tenant": {
            "tenant_id": tenant_ctx.get("tenant_id"),
            "tenant_ref": tenant_ctx.get("tenant_ref"),
            "domain_type": tenant_ctx.get("domain_type"),
        },
        "frame": frame_payload,
        "actions": [{"type": a.type.value, "payload": a.payload} for a in actions],
    }

    await logger.log_interaction(
        InteractionLogInput(
            case_id=case_id,
            turn_id=turn_id,
            transcript_raw=raw,
            transcript_redacted=redacted,
            pii_flags=pii_flags,
            decision_payload=decision_payload,
            actions_taken=actions_taken,
            tool_calls=tool_calls,
            latency_ms=latency_ms,
        )
    )

    return RenderOutput(
        text=render_text,
        mode=render_mode,
        lang=lang,
        meta={
            "case_id": case_id,
            "turn_id": turn_id,
            "latency_ms": latency_ms,
            "tool_results_keys": list(tool_results.keys()),
        },
    )


def _safe_tool_result(result: Any) -> Dict[str, Any]:
    """
    Keep tool result logs minimal and non-PII for demo safety.
    """
    if not isinstance(result, dict):
        return {"type": str(type(result))}
    whitelist = {"status", "message", "slots", "confirmation_id"}
    return {k: result[k] for k in result.keys() if k in whitelist}


def _render_template(tpl: Dict[str, Any], tool_results: Dict[str, Any], lang: str) -> str:
    """
    Deterministic template rendering (MVP).
    You can swap to a proper localization engine later.
    """
    template = tpl.get("template")

    if template == "DISPATCHER_CALLBACK_ACK":
        if lang == "tr":
            return "Anladım. Planlamadaki bir yetkili sizi geri arayacak. Lütfen numaranızı doğrulayın."
        if lang == "en":
            return "Understood. A dispatcher will call you back shortly. Please confirm your phone number."
        return "Begrepen. Onze planner belt je zo terug. Kun je je telefoonnummer bevestigen?"

    if template == "TRIAGE_THEN_PROPOSE_SLOTS":
        slots = (tool_results.get("scheduling.free_busy") or {}).get("slots") or []
        if not slots:
            if lang == "tr":
                return "Şu an uygun bir zaman göremiyorum. Sizi geri arayacağız om het snelste moment te bevestigen."
            if lang == "en":
                return "I don’t see an available slot right now. I’ll arrange a callback to confirm the earliest option."
            return "Ik zie nu geen beschikbaar slot. Ik laat je terugbellen om het snelste moment te bevestigen."

        s1 = slots[0]
        s2 = slots[1] if len(slots) > 1 else None

        if lang == "tr":
            return f"İki seçenek: {s1}" + (f" veya {s2}. Hangisi uygun?" if s2 else ". Bu saat uygun mu?")
        if lang == "en":
            return f"Two options: {s1}" + (f" or {s2}. Which works best?" if s2 else ". Does that work?")
        return f"Ik kan twee opties aanbieden: {s1}" + (f" of {s2}. Welke past het best?" if s2 else ". Past dat?")

    if template == "ADVICE_OFFER_CALLBACK":
        if lang == "tr":
            return "Dit klinkt niet als direct spoed. Ik kan advies geven of een afspraak/terugbelmoment plannen. Wat heeft je voorkeur?"
        if lang == "en":
            return "This doesn’t sound like an immediate emergency. I can give advice or schedule an appointment/callback. What do you prefer?"
        return "Dit klinkt niet als direct spoed. Ik kan advies geven of een afspraak/terugbelmoment plannen. Wat heeft je voorkeur?"

    return _fallback_response(lang)


def _fallback_response(lang: str) -> str:
    if lang == "tr":
        return "Teşekkürler. Kısaca sorunu anlatır mısınız?"
    if lang == "en":
        return "Thanks. Could you briefly describe the issue?"
    return "Dank je. Kun je kort vertellen wat er aan de hand is?"

