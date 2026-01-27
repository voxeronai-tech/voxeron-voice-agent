# Voxeron — Project Scope

## Canonical Context Rule

Reality is defined by:
- the Git repository state (branch + HEAD),
- committed documentation in this repository.

Chat history, verbal agreements, and local notes are **not authoritative**.

If it is not committed, it does not exist.

---

## Repositories and Responsibility Split

### voxeron-voice-agent
Implementation repository.

Owns:
- Runtime code (SessionController, parser, orchestration)
- Tests and golden regressions
- Tenant configuration
- Telemetry instrumentation
- Operational handover and day-to-day progress

### voxeron-architecture
Architecture governance repository.

Owns:
- Reference architecture
- Cross-repo architectural decisions (ADRs)
- Timeless principles and constraints
- Historical blueprints (snapshots only)

Project progress and sprint status **must not** live in the architecture repository.

---

## Goal

Build a deterministic, production-grade, multi-tenant voice platform.

Restaurant ordering is the initial proving ground.  
Other verticals are explicitly deferred until the core decision loop is hardened and observable.

We are building **infrastructure**, not a persona or demo bot.

---

## Non-Negotiable Principles

- Deterministic first, LLM last
- No tenant-specific logic in `SessionController`
- No menu or domain knowledge embedded in controller conditionals
- Domain intelligence lives in:
  - MenuSnapshot
  - Metadata
  - Tenant configuration
- Offline golden regression tests must run without:
  - database access
  - network access
- Every behavioral fix must include regression coverage
- One RC issue per branch
- Small, reviewable commits only

---

## In Scope (Current Phase)

- Decision loop hardening:
  - slot handling
  - latches
  - disambiguation
  - confirmation
  - refusal
- Deterministic golden regression suite
- Telemetry S1-4:
  - structured
  - privacy-safe
  - fire-and-forget
  - decision-point focused

---

## Explicitly Out of Scope (For Now)

- STT prompt style or persona tuning
- Conversational polish or UX copy refinement
- Analytics dashboards or reporting UI
- Personalization or marketing flows
- Non-deterministic ordering logic

We do not polish the system until it behaves correctly 1,000 out of 1,000 times.

---

## How Progress Is Tracked

- `docs/HANDOVER.md` is the **only** living progress document
- Architecture decisions go to `voxeron-architecture` ADRs
- Historical material is archived under `docs/archive/`

---

## Verification

```bash
pytest -q
pytest -q tests/regression/test_golden_transcripts.py
