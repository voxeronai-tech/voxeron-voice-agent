# VOXERON – ACTIVE HANDOVER

Last updated: 2026-01-06
Release target: **v0.7.3**
Active milestone: **RC1-3 – Deterministic Parsing MVP**

---

## 1. Repository state (canonical source of truth)

- Repository: `voxeron-voice-agent`
- Primary development branch: `feature/sprint-1-orchestrator-parser`
- Last released stable tag: `v0.7.2-rc2-impl`
- RC3 stabilization decision: SessionController reverted to known-good v12 implementation
- RC1-3 start tag: v0.7.3-rc1-3-start

Purpose of this file:
> This document is the **single handover artifact** for continuing work in a new chat. Chat memory is not authoritative. Git + this file are.

---

## 2. Release roadmap – v0.7.3 (Sprint 1)

### RC1-1 – ParserResult typed contract ✅ **DONE**
**Goal**: Shared deterministic parser contract usable by orchestrator and session controller.

Delivered:
- Canonical typed contract: `src/api/parser/types.py`
- Backward-compatible re-export: `src/api/orchestrator/parser_types.py`
- Enums:
  - `ParserStatus` (MATCH / NO_MATCH / PARTIAL / AMBIGUOUS)
  - `ReasonCode`
- Fields include:
  - `status`
  - `reason_code`
  - `matched_entity`
  - `confidence`
  - `execution_time_ms`
- Unit tests:
  - `tests/unit/test_parser_result.py`

Acceptance:
- Matches blueprint v0.7.1
- `execution_time_ms` measured per invocation
- Unit tests cover: MATCH / NO_MATCH / PARTIAL / AMBIGUOUS

Implementation references:
- Branch: `fix/rc1-1-parserresult-contract`
- Merged into main: commit `aeeb9d4`

---

### RC1-2 – Deterministic-first flow in CognitiveOrchestrator ✅ DONE
**Goal**: Parser must always run before any LLM call.

Rules:
- Orchestrator invokes deterministic parser on every user turn
- If `MATCH`: execute deterministically, **skip LLM entirely (0 tokens)**
- If `NO_MATCH`: fall back to LLM within <50ms after parser completion
- Audio streaming must remain stable (no resets / pops)

Acceptance:
- Parser always invoked first
- MATCH → deterministic path (LLM skipped)
- NO_MATCH → LLM fallback only
- Canonical ParserResult enforced
- DeterministicParser aligned with confidence + timing
- Verified via local runtime checks

---

### RC1-3 – DeterministicParser MVP (alias + quantity) ✅ DONE
**Goal**: Minimal deterministic parser that handles core ordering reliably.

Capabilities:
- Alias lookup via `MenuSnapshot` / DB
- Quantity extraction (1–10, EN + NL)

Examples:
- "two garlic naan" → MATCH (item_id=garlic_naan, qty=2)
- "make it one naan" → MATCH (update qty=1)
- Unknown item → NO_MATCH (reason=NO_ALIAS)

Delivered (branch fix/rc1-3-deterministic-parser-mvp):

**Original scope (Issue #3)**  
Implement a minimal deterministic parser with:
- Alias lookup via `MenuSnapshot` / DB
- Quantity extraction (1–10, EN + NL)
- Canonical `ParserResult` output (MATCH / NO_MATCH / PARTIAL)

**Acceptance criteria – ALL MET**
- “two garlic naan” → MATCH (item_id + qty=2)
- “make it one naan” → MATCH (SET_QTY = 1)
- Unknown item → NO_MATCH (NO_ALIAS)
- `pytest -q` green

**Implementation (branch `fix/rc1-3-deterministic-parser-mvp`)**
- Quantity extractor: `src/api/parser/quantity.py`
- Deterministic alias + qty parser: `src/api/orchestrator/deterministic_parser.py`
- Deterministic SET_QTY wiring before LLM fallback
- Safety guards preventing payload dicts from entering cart
- Taj phonetics fix (`naam → naan`)

**Release anchors**
- Start tag: `v0.7.3-rc1-3-start`
- Completion tag: **`v0.7.3-rc1-3-complete`** ✅  
  (tag exists locally and on origin)

> 🔒 **RC1-3 is closed. No further changes are allowed on this scope.**

---

### Explicitly OUT OF SCOPE for RC1-3

These issues are **not** to be worked on until RC1-3 is complete:

- RC1-4 TelemetryEmitter (parser NO_MATCH telemetry)
- RC1-5 Redis SessionState shadow write
- RC1-6 WebSocket lifecycle cleanup
- RC1-7 End-to-end deterministic vs fallback harness

---

## 3. Post-RC1-3 work: RC3 Session Stability (ACTIVE)

RC3 is **explicitly out of RC1-3 scope** and exists to stabilize
real-world conversation behavior in the `SessionController`.

### Active RC3 branch
- Branch: `fix/rc3-closeout-session-stability`
- Status: **WIP**
- Type: behavioral stabilization only

### Problems being addressed
- Confirmation loops (“Please say yes or no” dead-ends)
- Mixed affirm/negate utterances (“No, I want …”)
- Language instability (accidental NL/DE token drift)
- Over-eager clarification questions (e.g. naan variants when unambiguous)
- Spurious LLM menu enumerations after NEGATE

### Hard constraints for RC3
- ❌ No parser changes
- ❌ No orchestrator refactors
- ❌ No new deterministic capabilities
- ✅ SessionController logic only
- ✅ Incremental, reviewable fixes only

### Baseline rule
- Known-good baseline: **SessionController v12**
- RC3 changes are *surgical deltas*, not rewrites

---

## 4. Branching & workflow rules (MANDATORY)

- One issue = one branch
- One concern = one commit
- No large file rewrites in chat
- No scope invention
- RC1-3 code must never be amended

**Active branches**
- `feature/sprint-1-orchestrator-parser` (integration)
- `fix/rc1-3-deterministic-parser-mvp` (closed)
- `fix/rc3-closeout-session-stability` (active)

**Commit message format**
RC3: <concise behavioral fix>

yaml
Copy code

---

## 5. Explicitly OUT OF SCOPE (until RC3 is closed)

- RC1-4 TelemetryEmitter
- RC1-5 Redis SessionState shadow writes
- RC1-6 WebSocket lifecycle cleanup
- RC1-7 Deterministic vs fallback E2E harness
- Any new language detection logic
- Any new intent parsing logic

---

## 6. How to continue in a NEW CHAT

Do **not** re-explain history manually.

### Step 1 – Local context
```bash
git clone https://github.com/voxeronai-tech/voxeron-voice-agent.git
cd voxeron-voice-agent
git checkout feature/sprint-1-orchestrator-parser
cat docs/HANDOVER.md
git tag --list | grep v0.7.3
---

## 7. Engineering principle (non-negotiable)

> If it is not committed to Git, it does not exist.

Chats are execution tools, not memory.

