# VOXERON – ACTIVE HANDOVER

Last updated: 2026-01-09  
Release target: v0.7.x  
Roadmap scope: RC1-1 → RC1-7

Purpose of this file:
This document is the **single narrative source of truth** for Voxeron’s
RC1 roadmap status.  
Git contains implementation details; this file records **intent, scope,
and completion state**.

If it is not reflected here, it is not considered roadmap progress.

---

## RC1-1 – Connectivity & Audio Transport  
Status: DONE

Goal:
Establish stable, low-latency audio transport between client and server.

Delivered:
- WebSocket audio streaming
- PCM framing
- Session lifecycle (connect / disconnect)
- Tenant bootstrap via `/tenant_config`

Notes:
- Considered stable enough for iterative development
- No remaining RC1-1 work

---

## RC1-2 – Core Voice Pipeline  
Status: DONE

Goal:
End-to-end voice loop: STT → reasoning → TTS.

Delivered:
- Streaming STT integration
- LLM reasoning path
- TTS playback
- Language propagation through the pipeline

Notes:
- Latency acceptable for conversational use
- Pipeline correctness validated manually

---

## RC1-3 – Deterministic Parsing MVP  
Status: DONE (CLOSED)

Goal:
Guarantee deterministic handling of core ordering flows
before any LLM fallback.

Delivered:
- Alias-based menu matching
- Quantity extraction (EN + NL)
- Deterministic cart mutation
- Safety guards against hallucinated items
- Canonical “parser-first” execution order

Acceptance:
- “two garlic naan” → deterministic add
- “make it one naan” → deterministic update
- Unknown items → LLM fallback only

Lock:
RC1-3 is **closed**.  
No further changes are permitted under this milestone.

---

## Post-RC1-3 Stabilization (RC3)  
Status: CLOSED (2026-01-09)

Context:
Real-world voice testing revealed session-level instability
outside the scope of deterministic parsing.

Scope constraints:
- SessionController logic only
- No parser changes
- No orchestrator refactors

Delivered:
- Fixed confirmation loops (“please say yes or no” dead-ends)
- Correct handling of split variant quantities  
  (e.g. “one plain naan and one garlic naan”)
- Variant-scoped increments  
  (“add one extra garlic naan”)
- Tolerant confirmation phrasing  
  (“That’s correct, yes.” / “Dat klopt”)
- Safe customer name addressing
- Language lane stability (EN/NL)
- RC3 closeout regression suite added and passing

Explicitly NOT solved:
- Short hesitation / stutter interruption  
  (classified as RC1-4 / server-side timing concern)

RC3 is complete and closed.

---

## RC1-4 – Session Robustness & Turn Control  
Status: PLANNED

Goal:
Make conversations resilient to real human speech patterns.

Planned scope:
- Grace windows for short hesitations
- Improved barge-in handling
- Turn finalization timing
- Reduced premature agent responses

Not started.

---

## RC1-5 – Session State Resilience  
Status: PLANNED

Goal:
Ensure session continuity under transient failures.

Planned scope:
- Session state recovery
- Safe reconnect behavior
- Optional persistence layer

Not started.

---

## RC1-6 – WebSocket Lifecycle Hardening  
Status: PLANNED

Goal:
Production-grade connection handling.

Planned scope:
- Cleanup on abnormal disconnects
- Resource leak prevention
- Long-running session stability

Not started.

---

## RC1-7 – Deployment Readiness  
Status: PLANNED

Goal:
Operational readiness for controlled rollout.

Planned scope:
- Telemetry
- Logging hygiene
- Basic observability
- Operational runbooks

Not started.

---

## Engineering principle (non-negotiable)

If it is not committed to Git, it does not exist.  
If it is not reflected here, it is not roadmap progress.
