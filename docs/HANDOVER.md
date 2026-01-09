Perfect — thanks, that clarifies it.
Nothing weird to flag in Git *right now*; we’ll just **re-establish a clean, authoritative HANDOVER.md** and you can overwrite what VS Code shows.

Below is a **complete, self-contained HANDOVER.md** you can paste verbatim into `docs/HANDOVER.md`, commit, and treat as the renewed single source of truth.

I have:

* **Kept the original intent and structure** (roadmap RC1-1 → RC1-7)
* **Explicitly closed RC3** (with tag reference)
* **Separated RC3 from RC1 roadmap** so this confusion cannot reoccur
* **Documented the grace-window issue as RC1-4 (next)**, not RC3
* **Avoided referencing volatile branches** — this document stands on its own

---

# VOXERON – ACTIVE HANDOVER

Last updated: **2026-01-09**
Release target: **v0.7.3**
Primary roadmap: **RC1-1 → RC1-7**
Session stabilization track: **RC3 (CLOSED)**

---

## 0. Purpose of this document (non-negotiable)

This file is the **single authoritative handover artifact** for Voxeron engineering state.

* Chat history is **not** authoritative
* Branch names are **not** authoritative
* Tags and commits exist to support this document, not replace it

> If it is not reflected here, it is not “done”.

---

## 1. RC1 Release Roadmap Overview

RC1 represents the **deterministic core** of Voxeron.
It must be completed **sequentially**. No skipping, no mixing.

| RC    | Title                             | Status     |
| ----- | --------------------------------- | ---------- |
| RC1-1 | ParserResult contract             | ✅ DONE     |
| RC1-2 | Deterministic-first orchestration | ✅ DONE     |
| RC1-3 | DeterministicParser MVP           | ✅ DONE     |
| RC1-4 | Turn stability & grace windows    | ⏳ NEXT     |
| RC1-5 | Session state hardening           | ⏸️ PENDING |
| RC1-6 | Resilience & lifecycle recovery   | ⏸️ PENDING |
| RC1-7 | Production readiness              | ⏸️ PENDING |

---

## 2. RC1-1 — ParserResult Typed Contract ✅ DONE

**Goal**
Define a canonical, typed result contract shared across parser, orchestrator, and session logic.

**Delivered**

* `ParserResult` with:

  * `status` (MATCH / NO_MATCH / PARTIAL / AMBIGUOUS)
  * `reason_code`
  * `matched_entity`
  * `confidence`
  * `execution_time_ms`
* Enforced as the only allowed parser output
* Unit-tested

**Acceptance**
✔ Parser output is typed
✔ No dicts or ad-hoc payloads leak into flow
✔ Execution timing measured per invocation

---

## 3. RC1-2 — Deterministic-First Orchestration ✅ DONE

**Goal**
The deterministic parser must *always* run before any LLM involvement.

**Rules**

* Parser invoked on every user turn
* MATCH → deterministic execution, **LLM skipped**
* NO_MATCH → LLM fallback allowed
* Audio pipeline remains uninterrupted

**Acceptance**
✔ Zero-token paths for deterministic matches
✔ Deterministic → LLM boundary enforced
✔ Verified in live runtime

---

## 4. RC1-3 — DeterministicParser MVP ✅ DONE

**Goal**
Minimal but reliable deterministic ordering for real usage.

**Capabilities**

* Menu alias resolution
* Quantity extraction (EN + NL)
* SET / UPDATE quantity handling

**Examples**

* “two garlic naan” → MATCH (qty=2)
* “make it one naan” → MATCH (SET_QTY=1)
* Unknown item → NO_MATCH

**Acceptance**
✔ All MVP cases covered
✔ Parser emits canonical `ParserResult`
✔ No session logic leakage into parser

**Status**
🔒 **RC1-3 is closed. No further changes allowed.**

---

## 5. RC3 — Session Stability Track (OUTSIDE RC1) ✅ CLOSED

RC3 is **not part of RC1**.
It exists to stabilize real conversational behavior without expanding deterministic scope.

**Focus**

* Confirmation loops
* Mixed affirm/negate utterances
* Language drift
* Naan variant resolution
* Safe checkout phrasing

**Constraints**

* ❌ No parser changes
* ❌ No orchestrator refactors
* ✅ SessionController only
* ✅ Surgical, reversible changes

**Closure**

* Automated RC3 closeout suite added
* Happy-flow ordering validated
* Tagged as:

```
rc3-closed-2026-01-09
```

RC3 is **frozen**.

---

## 6. RC1-4 — Turn Stability & Grace Windows ⏳ NEXT

## RC1-4: Grace Window & Turn Boundary Stabilization

### Status
- **State**: OPEN
- **Branch**: feature/rc1-4-grace-window
- **Relation to RC3**: Post-RC3 only. RC3 is closed, tagged, and must not be modified.

---

### Problem Statement (Verified)

The agent prematurely ends a user turn when the user:
- starts speaking,
- pauses briefly (stutter / hesitation),
- then continues the sentence.

Example failing utterance:
> “I would like to add … (pause) … two butter chicken”

Observed behavior:
- Audio is flushed too early
- STT returns a syntactically valid but semantically incomplete fragment
- `SessionController` treats this fragment as a completed turn
- Agent interrupts, responds incorrectly, or frontend hangs in listening state

This is:
- **NOT** an STT bug  
- **NOT** a parser bug  
- **IS** a turn-boundary and semantic-grace problem

---

### Goal

Introduce **semantic grace** so that the system does **not finalize a user turn** when the transcript is *semantically incomplete*, even if it is syntactically valid.

The system must:
- wait for continuation when appropriate, or
- issue a short clarification,
- without interrupting, mutating state, or hanging.

---

### Scope (In-Scope)

1. **Semantic grace in `SessionController`**
   - Explicit rules to prevent turn finalization on known incomplete openers or continuations, such as:
     - “I would like to add”
     - “I want to”
     - “Can I”
     - “Add”
     - “One more”
   - This is a deterministic policy decision.

2. **Turn-boundary decision gate**
   - A single, explicit decision point classifying each transcript as:
     - **Actionable** – safe to process as a full turn
     - **Hold** – semantically incomplete, wait for continuation
     - **Clarify** – ask a short clarification instead of acting

3. **Non-interrupt guarantee**
   - While in semantic-grace hold:
     - no LLM response is emitted
     - no cart/order mutation occurs
     - no flow-state advance occurs
     - no mic shutdown or frontend state transition that could cause a hang

4. **Minimal observability**
   - Targeted logging/markers for:
     - semantic-grace triggered
     - continuation received and resolved
     - clarification triggered
     - timeout/fallback exit taken

---

### Acceptance Criteria

#### A. Core Behavior

1. **Hesitation continuation works**
   - Given: “I would like to add … (pause) … two butter chicken”
   - Then:
     - “I would like to add” is not treated as a completed turn
     - the system does not speak over the user
     - the completed intent is processed only after continuation

2. **No premature cart mutation**
   - During semantic-grace hold, order/cart state remains unchanged.

3. **No frontend hang**
   - After a grace-trigger event, the system remains in a recoverable listening state.
   - No stuck “listening forever” or dead-end states.

4. **No regression for valid short turns**
   - Single-token or short but complete replies continue to work:
     - “yes”, “no”, “garlic”, “pickup”, “delivery”, explicit quantities, explicit items

---

#### B. Boundary Correctness (Rules-Based)

5. **Deterministic rule set**
   - The incomplete-opener list is explicit and reviewable.
   - Matching uses existing transcript normalization.
   - No learned or probabilistic behavior.

6. **Context-aware behavior**
   - Completeness depends on conversation state.
   - Example: “Add” is incomplete in general and must not commit state by itself.
   - At least one state-sensitive example must be validated.

---

#### C. Fallback Behavior

7. **Clarification path**
   - If the user does not continue after an incomplete opener:
     - system issues a single short clarification
       (e.g., “Sure — what would you like to add?”)
   - No looping or repeated reprompts.

8. **Timeout as escape hatch only**
   - A timeout may exist to prevent deadlock.
   - Semantic rules, not timers, are the primary mechanism.

---

### Out of Scope (Explicitly NOT Included)

- No changes to the deterministic parser
- No orchestrator refactor
- No STT pipeline redesign
- No new product features or intent systems
- No broad refactors for elegance or cleanup

---

### Engineering Constraints

- Deterministic parser must remain untouched
- Orchestrator flow must not be refactored
- Fix must be minimal, localized, and reviewable
- `SessionController` is the primary target
- `server.py` changes only if strictly necessary

---

### Risks & Hidden Coupling

1. **Over-greedy grace**
   - Excessively broad rules may suppress valid short answers.

2. **Frontend/backend state mismatch**
   - Backend must not signal turn completion while frontend expects continued listening.

3. **Interaction with barge-in / TTS cancel**
   - Canceling TTS without a follow-up prompt or continuation path risks dead air.

4. **State explosion**
   - Avoid introducing a parallel state machine.
   - At most, a single “pending semantic turn” latch with clear ownership and exit rules.

---

### RC1-4 Checklist (Deliverables)

- [ ] Documented list of semantically incomplete openers
- [ ] Explicit turn-finalization gate in `SessionController`
- [ ] Guaranteed no-op on cart, LLM, and flow during hold
- [ ] Single clarification fallback
- [ ] Logging/metrics for grace paths
- [ ] Manual regression test matrix covering:
      - hesitation continuation
      - no-continuation fallback
      - valid single-token replies
      - quantity-only followups
      - context-sensitive “add” cases

---

## 7. Explicitly OUT OF SCOPE (until RC1-4 done)

* New intent types
* Phonetic AI / fuzzy matching
* Redis shadow state
* Telemetry emitters
* Production monitoring
* UI/UX tuning

---

## 8. How to continue in a NEW CHAT

1. Open `docs/HANDOVER.md`
2. Read **this file first**
3. State which RC you are working on
4. Do not re-explain history

---

## 9. Engineering principle

> Determinism first.
> Stability second.
> Intelligence last.

---


