# VOXERON — ACTIVE HANDOVER (CANONICAL)

Last updated: 2026-01-27  
Scope authority: `docs/SCOPE.md`

---

## Canonical Context Rule (MANDATORY)

Reality is defined by:
- the current Git repository state (branch + HEAD),
- committed documentation in this repository.

Chat memory, verbal agreements, and local notes are **not authoritative**.

If it is not committed, it does not exist.

This file is the **single operational handover document** for continuing work.

---

## Repository & Status

- Repository: `voxeron-voice-agent`
- Active branch: `feature/S1-4-telemetry-emitter`
- Status: clean, pushed, tests green

```bash
git status -sb
pytest -q
````

---

## Where We Are (Truth Summary)

The deterministic core of Voxeron is **stable and protected by regression tests**.

Completed and locked:

* Deterministic-first execution model
* Typed parser contract
* Deterministic parser MVP (aliases + quantities)
* SessionController stabilization (RC3)
* Confirmation / refusal decision-loop hardening
* Golden offline regression harness

We are **no longer** building parsing capability or conversation logic.

We are now finishing **infrastructure-level observability**.

---

## Last Completed Milestone

### Decision Loop Hardening — Confirm / Refusal Integrity

What was fixed:

* Explicit refusal handling during confirmation (`pending_confirm`)
* Refusal clears latch and returns to ORDERING
* No LLM fallback on refusal
* No confirmation hallucination

Regression protection:

* B1 golden transcript
  `tests/regression/taj_confirm_refusal_returns_to_ordering.json`

This closed a critical transactional integrity gap.

---

## Active Sprint

### S1-4B — Telemetry as Truth

**Goal**
Make the Decision Loop auditable.

The system must emit structured, deterministic telemetry at all critical decision points so failures are diagnosable from data, not intuition.

**In Scope**

* Telemetry for:

  * parser `NO_MATCH`
  * confirmation requested
  * confirmation accepted
  * confirmation refused
* Fire-and-forget, non-blocking emission
* Privacy-safe (PII redaction MVP)

**Explicitly Out of Scope**

* Any behavior or logic changes
* STT prompt tuning or persona work
* UI, dashboards, or analytics pipelines

**Canonical issue**

* GitHub: *S1-4 TelemetryEmitter — Decision Loop Telemetry*

---

## How to Continue (MANDATORY FLOW)

1. Work **only** on the active S1-4 telemetry issue
2. Do **not** modify decision logic or behavior
3. Wire telemetry at decision points in `SessionController`
4. Keep commits:

   * one concern per commit
   * small and reviewable
5. Golden regressions must remain green at all times

---

## Verification

```bash
pytest -q
pytest -q tests/regression/test_golden_transcripts.py
```

Telemetry must never block runtime execution.

---

## Architectural Guardrails (NON-NEGOTIABLE)

* Deterministic first, LLM last
* No tenant-specific logic in `SessionController`
* No domain knowledge embedded in controller conditionals
* Domain intelligence lives in:

  * MenuSnapshot
  * Metadata
  * Tenant configuration
* Offline golden tests must run without DB or network
* One RC issue per branch
* One behavioral change per commit

---

## What Not To Do

Until S1-4B is complete, **do not**:

* Tune STT prompts or voice style
* Add new intents or parsing logic
* Introduce analytics or dashboards
* Refactor working decision code

We are building **infrastructure**, not a demo.

---

## Engineering Principle (Final)

> If it is not committed to Git, it does not exist.

Chats are execution tools, not memory.