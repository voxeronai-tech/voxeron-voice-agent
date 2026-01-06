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

### RC1-2 – Deterministic-first flow in CognitiveOrchestrator ⏳ **IN PROGRESS**
**Goal**: Parser must always run before any LLM call.

Rules:
- Orchestrator invokes deterministic parser on every user turn
- If `MATCH`: execute deterministically, **skip LLM entirely (0 tokens)**
- If `NO_MATCH`: fall back to LLM within <50ms after parser completion
- Audio streaming must remain stable (no resets / pops)

Acceptance:
- Parser invoked first on every turn
- MATCH path consumes zero LLM tokens
- Taj demo flow continues to work

---

### RC1-3 – DeterministicParser MVP (alias + quantity) ⏳ **PENDING**
**Goal**: Minimal deterministic parser that handles core ordering reliably.

Capabilities:
- Alias lookup via `MenuSnapshot` / DB
- Quantity extraction (1–10, EN + NL)

Examples:
- "two garlic naan" → MATCH (item_id=garlic_naan, qty=2)
- "make it one naan" → MATCH (update qty=1)
- Unknown item → NO_MATCH (reason=NO_ALIAS)

---

### Explicitly OUT OF SCOPE for RC1-3

These issues are **not** to be worked on until RC1-3 is complete:

- RC1-4 TelemetryEmitter (parser NO_MATCH telemetry)
- RC1-5 Redis SessionState shadow write
- RC1-6 WebSocket lifecycle cleanup
- RC1-7 End-to-end deterministic vs fallback harness

---

## 3. Current implementation status

### SessionController (CRITICAL STABILITY DECISION)

- `session_controller.py` was refactored aggressively during RC3
- Resulting v13 behavior caused regressions (confirmation loops, language drift)
- Decision made to **stabilize RC3 using known-good v12 implementation**

Authoritative baseline:
- `session_controller_rc3_ready_v12.py` (manually restored and committed)

Rule:
> No further large SessionController refactors until RC1-3 is fully delivered.

---

### Orchestrator / Parser state

- Orchestrator hook exists but must be aligned with RC1-1 contract
- Deterministic alias parsing exists but not yet formalized as `ParserResult`
- Folder structure may differ from issue suggestions; behavior > location

---

## 4. Branching and workflow rules (MANDATORY)

- One RC issue = one branch
- One concern = one commit
- No large file rewrites in chat
- Chat must never invent roadmap or scope

Branch naming:
- `fix/rc1-1-parserresult-contract`
- `fix/rc1-2-orchestrator-deterministic-first`
- `fix/rc1-3-deterministic-parser-mvp`

Commit messages MUST reference RC issue:
```
RC1-3: deterministic parser alias + qty MVP
```

---

## 5. Tags as memory anchors

Tags are used to anchor progress independently of chat context.

Examples:
- `v0.7.3-rc1-3-start`
- `rc3-session-v12-stable`
- `v0.7.3-rc1-3-complete`

---


## 6. How to resume work in a NEW CHAT

In a new chat, do NOT explain history manually.

Instead:

1. Run locally:
```bash
git clone https://github.com/voxeronai-tech/voxeron-voice-agent.git
cd voxeron-voice-agent
git checkout feature/sprint-1-orchestrator-parser
git log --oneline --decorate -5
cat docs/HANDOVER.md
```

2. Say to ChatGPT:
> "Continue from docs/HANDOVER.md at RC1-3. Git is canonical memory."

That is sufficient context.

---

## 7. Engineering principle (non-negotiable)

> If it is not committed to Git, it does not exist.

Chats are execution tools, not memory.

