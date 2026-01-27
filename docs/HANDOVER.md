# VOXERON — ACTIVE HANDOVER (CANONICAL)

Last updated: 2026-01-27  
Scope authority: docs/SCOPE.md

---

## Canonical Context Rule (MANDATORY)

Reality is defined by:
- the current Git repository state (branch + HEAD)
- committed documentation in this repository

Chat memory, verbal agreements, and local notes are NOT authoritative.

If it is not committed, it does not exist.

This file is the single operational handover document for continuing work.

---

## Repository and Status

- Repository: voxeron-voice-agent
- Active branch: feature/S1-4-telemetry-emitter
- Status: clean, pushed, tests green

Verification commands:
- git status -sb
- pytest -q

---

## Where We Are (Truth Summary)

The deterministic core of Voxeron is stable and protected by regression tests.

Completed and locked:
- Deterministic-first execution model
- Typed parser contract
- Deterministic parser MVP (aliases and quantities)
- SessionController stabilization (RC3)
- Confirmation and refusal decision-loop hardening
- Offline golden regression harness

We are no longer building parsing capability or conversational logic.

We are finishing infrastructure-level observability.

---

## Last Completed Milestone

### Decision Loop Hardening — Confirmation and Refusal Integrity

What was fixed:
- Explicit refusal handling during confirmation (pending_confirm)
- Refusal clears the latch and returns to ORDERING
- No LLM fallback on refusal
- No confirmation hallucination

Regression protection:
- B1 golden transcript  
  tests/regression/taj_confirm_refusal_returns_to_ordering.json

---

## Active Sprint

### S1-4B — Telemetry as Truth

Goal:
Make the Decision Loop auditable.

In scope:
- Telemetry for parser NO_MATCH
- Telemetry for confirmation requested
- Telemetry for confirmation accepted
- Telemetry for confirmation refused
- Fire-and-forget, non-blocking emission
- Privacy-safe (PII redaction MVP)

Out of scope:
- Any behavior or logic changes
- STT prompt tuning or persona work
- UI, dashboards, or analytics pipelines

Canonical issue:
- GitHub issue: S1-4 TelemetryEmitter — Decision Loop Telemetry

---

## How to Continue (MANDATORY FLOW)

1. Work only on the active S1-4 telemetry issue
2. Do not modify decision logic or behavior
3. Wire telemetry at decision points in SessionController
4. Keep commits small and reviewable
5. Golden regressions must remain green at all times

---

## Verification

Required:
- pytest -q
- pytest -q tests/regression/test_golden_transcripts.py

Telemetry must never block runtime execution.

---

## Architectural Guardrails (NON-NEGOTIABLE)

- Deterministic first, LLM last
- No tenant-specific logic in SessionController
- No domain knowledge embedded in controller conditionals
- Domain intelligence lives in MenuSnapshot, metadata, and tenant configuration
- Offline golden tests must run without database or network access
- One RC issue per branch
- One behavioral change per commit

---

## Engineering Principle (Final)

If it is not committed to Git, it does not exist.

Chats are execution tools, not memory.
