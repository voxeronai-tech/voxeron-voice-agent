# PARSER_CONTRACT.md

Last updated: 2026-01-11 (Europe/Amsterdam)  
Scope: S1-3 DeterministicParser + S1-4 TelemetryEmitter integration protocol  
Architectural North Star: Single Decision Boundary (ParserResult)

---

## 1. Purpose

This document defines the **ParserResult Protocol** used to decouple:
- **SessionController** (orchestration, latching, action)
from
- **DeterministicParser** (normalization, extraction, match decisions)
and
- **TelemetryEmitter** (PII-safe event capture on non-MATCH).

The Controller MUST treat ParserResult as authoritative and immutable.

---

## 2. Core Principles

1) **Single Decision Boundary**  
Every user turn produces one ParserResult. The Controller branches only on:
- `status`, `intent`, `reason`, and optionally `next_action`.

2) **Stateless but Context-Aware**  
The parser stores no internal memory but accepts a context snapshot:
`(transcript, tenant_id, domain, lang, context)`.

3) **Immutability**  
ParserResult and all nested objects MUST be frozen/immutable. The Controller must not mutate them.

4) **Explicit Reasons**  
Telemetry dashboards MUST group by `ParseReason` enum. If a new failure mode is discovered, add a new enum member.

---

## 3. Status

- `MATCH`: deterministic parse succeeded
- `PARTIAL_MATCH`: detected intent but missing required info (variant, quantity, item, etc.)
- `NO_MATCH`: nothing reliable detected
- `ERROR`: parser exception or invalid input; must never bubble up

---

## 4. Intent

Minimal stable set (expand only when needed):
- ADD_ITEM
- REMOVE_ITEM
- SET_QUANTITY
- QUERY_MENU
- QUERY_ORDER_SUMMARY
- CHECKOUT
- CONFIRM_YES
- CONFIRM_NO
- PROVIDE_NAME
- SET_FULFILLMENT
- CLOSE_CALL
- UNKNOWN (only when status != MATCH)

---

## 5. Reason (Telemetry dimension)

The ParseReason enum is the only accepted taxonomy for dashboards:
- OK
- NO_MATCH_GENERIC
- NO_MATCH_EMPTY
- NO_MATCH_TOO_SHORT
- NO_MATCH_UNSUPPORTED_LANGUAGE
- NO_MATCH_AMBIGUOUS
- NO_MATCH_OOV_MENU
- PARTIAL_MISSING_ITEM
- PARTIAL_MISSING_QUANTITY
- PARTIAL_MISSING_VARIANT
- PARTIAL_NEEDS_CLARIFICATION
- ERROR_EXCEPTION
- ERROR_INVALID_CONTEXT

---

## 6. NormalizationTrace

Records the normalization pipeline (aliases + language handling):
- raw_transcript (internal only)
- normalized_transcript
- changed
- applied_aliases (list of rule identifiers)
- lang_inferred (optional)
- notes (optional)

---

## 7. UtteranceTelemetryPayload (PII-safe)

Stored/emitted by TelemetryEmitter:
- utterance_redacted: <= 100 chars
- pii_redacted: bool
- truncation: "NONE" or "HEAD_TAIL_50_50"

### Head+Tail rule
If `len(text) > 100`:  
Store `text[:50] + " … " + text[-50:]` (total <= 103 including marker, but enforce <=100 in implementation by reducing head/tail accordingly or using a 1-char marker).

Implementation MUST enforce final length <= 100.

---

## 8. ParserContext (snapshot)

A minimal context snapshot to interpret ellipses:
- cart_summary (string, or structured list later)
- pending_slot (e.g., "pending_confirm", "pending_variant")
- last_intent
- menu_snapshot_id

---

## 9. ParserResult object

Required:
- version: int
- status: ParseStatus
- intent: ParseIntent
- reason: ParseReason
- confidence: float (0..1 heuristic)
- domain: str
- normalization: NormalizationTrace
- telemetry: UtteranceTelemetryPayload

Optional:
- entities: dict (deterministic extraction results)
- delta: dict (order mutation payload if applicable)
- next_action: str (e.g. "ASK_VARIANT", "ASK_QUANTITY")

---

## 10. Controller obligations

- Never mutate ParserResult
- For status != MATCH: MUST emit telemetry event (S1-4)
- For PARTIAL_MATCH: MUST follow next_action prompt path deterministically

