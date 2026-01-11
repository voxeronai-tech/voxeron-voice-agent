# VOXERON – PROJECT_STATUS.md

Last updated: 2026-01-11 (Europe/Amsterdam)  
Repo: `voxeron-voice-agent`  
Canonical memory: `docs/HANDOVER.md` (Git is truth, chat is not)

---

## 1) Current Focus

**Active integration theme:** RC1-4 session stability hardening, shifting towards modular deterministic services:
- **S1-3 DeterministicParser** (planned modular extraction target)
- **S1-4 TelemetryEmitter** (planned observability target)

**Immediate priority:** Integrate **S1-4 TelemetryEmitter** on parser `NO_MATCH` without blocking runtime, and align normalization responsibilities with S1-3.

---

## 2) System Health

### 2.1 Summary

- **SessionController (`src/api/session_controller.py`) is currently a God Object**: it owns
  state transitions, slot latches, normalization/sanitization calls, deterministic routing, and parts of “parsing-like” behavior.
- **Tenant-scoped aliases.json is working** and is now the preferred place for tenant “noise”.
- **We already fixed major confirm UX regressions** with RC1-4 P0 work (separate branches) and did **Cleanup-1** (helper extraction) to reduce drift.

### 2.2 Known-good RC1-4 fixes shipped as isolated branches

**P0.1 Confirm latch fallthrough fixed**
- NL confirmation phrases recognized (e.g. “klopt”, “dat is correct”)
- `pending_confirm` no longer falls through on unclear input, it reprompts and hard-returns

**P0.2 “Thanks, None” fixed**
- name acknowledgements guarded so Optional name never speaks “None”

**P0.3 Completion idioms added**
- Dutch completion idioms (e.g. “nou dat zal wel”) normalized via `tenants/taj_mahal/aliases.json`

**Cleanup-1**
- duplicate confirm reprompt factored into a helper to prevent drift

> NOTE: These fixes are currently in separate feature branches. If not merged into the integration base branch yet, treat integration as pending work.

---

## 3) Reality Check Audit

### 3.1 Where the Controller is doing Parser/Normalization work (observed)

Based on current snippets and branch work we saw:

- **Normalization helpers inside controller**:
  - multiple `_is_*` intent detectors use `norm_simple(...)` and hardcoded token lists
  - confirm flow has multiple variations (`_is_yes_like`, `_is_affirm`, `_is_negative`, `_is_refusal_like`, `_is_done_intent`, `_is_order_complete_intent`)

- **Pre-processing / sanitization is not a single formal pipeline**:
  - `tm.apply_aliases(...)` is applied multiple times in SessionController (per earlier handover notes: main STT path and cart-check STT path)
  - this is effective but indicates preprocessing is scattered, not centralized

- **“Parsing-like” behavior in controller**:
  - slot/latch ownership logic (pending variant resolution, pending confirm, pending name, pending fulfillment)
  - deterministic routing based on transcript patterns and menu knowledge
  - these are orchestration responsibilities, but the boundary with “parser” is currently blurred because normalization and intent matching are co-located.

### 3.2 What we cannot claim without audit (UNKNOWN)

- Exact number of hardcoded regex/trigger lists across the entire SessionController beyond the snippets we’ve handled.
- Whether there is already a modular DeterministicParser module in repo (S1-3) with a stable contract and call sites.
- Whether there is a migrations folder in this repo or a separate migrations repo.

Action: run a repo-wide audit (see section 8).

---

## 4) Architectural Challenge Responses

### 4.1 Telemetry Paradox (partial matches vs scattered logic)

**Problem:** If match decisions are distributed across `if/else` blocks (slot latches, deterministic intents, fallback routing), telemetry can’t reliably capture:
- partial matches
- reasons for “why not matched”
- where normalization changed meaning

**Resolution direction:**
- Introduce a **single deterministic “ParseResult/Decision” contract** returned by S1-3 DeterministicParser (or a ParserOrchestrator wrapper) that always yields:
  - `status` (MATCH, PARTIAL_MATCH, NO_MATCH)
  - `reason` (enum/string)
  - `domain` and optionally `matched_intent` or `slot_target`
  - `normalization_applied` summary (aliases fired, redaction flags)
- Telemetry must be emitted at the **decision boundary**, not scattered in leaf branches.

### 4.2 Alias Leak (why apply_aliases twice)

**Current observed behavior:** aliases are applied in multiple STT paths (main + cart-check).  
This is correct functionally but indicates missing pipeline centralization.

**Resolution direction:**
- Define a **single “Normalization Pipeline”**:
  `raw_transcript -> normalized_transcript -> parser -> controller`
- Ownership:
  - **Tenant aliases** belong in a Normalizer step inside **S1-3 DeterministicParser** (or a dedicated `TranscriptNormalizer` called by it).
  - SessionController should receive a transcript that is already “normalized-for-parsing”.

### 4.3 PII constraint vs “tail loss”

Issue S1-4 requires `utterance_redacted <= 100 chars`. Truncation can destroy tail context.

**Resolution direction:**
- Use **head+tail truncation** instead of head-only truncation:
  - keep first 60 chars + last 40 chars (total 100), with a marker in the middle
  - this preserves command tails where errors occur (“… make it two”, “... no delivery”, “... remove garlic naan”)
- Store `truncation_strategy = "HEAD_TAIL"` (optional field), or encode marker `" … "`.

---

## 5) Component Maturity Matrix (best-effort)

> This is a planning estimate. Confirm with repo audit.

| Component | Maturity | Notes |
|---|---:|---|
| SessionController state machine | 70% | Stable latches, deterministic routing, but too much responsibility |
| Tenant alias sanitization | 70% | Works, but not centralized as a pre-parse pipeline |
| STT post-processing (heuristics) | 40% | Some per-slot fixes exist; still scattered |
| DeterministicParser (S1-3) | 10–30% (UNKNOWN) | Contract exists in concept; actual implementation/call sites require audit |
| TelemetryEmitter (S1-4) | 0% | Not implemented yet |
| Redaction MVP | 0% | Not implemented yet |
| Migrations/DB schema for telemetry | 0% (UNKNOWN) | Depends on migration approach in repo |

---

## 6) Drift Prevention Rules (non-negotiable)

### 6.1 “Where does logic go” contract

**SessionController MAY contain:**
- state transitions and latches
- orchestration calls to services (STT, Parser, DB, Telemetry)
- deterministic “next step” decisions based on ParserResult outputs
- generic user prompts

**SessionController MAY NOT contain (new additions):**
- tenant-specific regex fixes (Taj, etc.)
- transcript alias rules (these must go to tenant config)
- new normalization routines beyond generic wrappers
- telemetry formatting details (belongs to TelemetryEmitter)

**Tenant config (`tenants/<id>/aliases.json`) MUST contain:**
- idioms, language bleed fixes, acoustic mishears
- “completion phrases”
- small rewrite rules

**S1-3 DeterministicParser MUST contain:**
- the normalization pipeline orchestration (alias application, canonical tokens)
- deterministic parse decision contract (status, reason, partial match)

**S1-4 TelemetryEmitter MUST contain:**
- event shaping
- PII redaction
- truncation strategy
- async fire-and-forget delivery

### 6.2 Red/Green rule for adding regex
If a developer wants to add regex anywhere:
- ✅ If it’s tenant idiom/acoustic: add to `aliases.json`
- ✅ If it’s universal language normalization: add to Normalizer inside DeterministicParser
- ❌ Never add new regex directly into SessionController unless it is a temporary emergency patch with an explicit TODO and issue reference.

---

## 7) Branch Alignment Rules

- RC branches (RC1-4) are allowed to stabilize runtime behavior.
- S branches (S1-3, S1-4) introduce modular services.
- **No divergence allowed**: any RC fix that touches normalization must be followed by extraction into S1-3 within 1 week.

**Integration approach:**
- Merge P0 RC1-4 fixes into the integration base before starting service work that depends on them (or cherry-pick them into service branches).
- Keep “one issue per branch/commit”.

---

## 8) Immediate Next Steps (next 48 hours)

### Day 1 (0–24h): S1-4 TelemetryEmitter MVP
1) Add `src/api/telemetry/emitter.py`
   - `emit_parser_no_match()` uses `asyncio.create_task(...)`
   - DB insert wrapped in `wait_for(timeout=...)`, best-effort
2) Add `tests/unit/test_redaction.py`
   - email/phone/numbers redaction
   - truncation to <= 100
   - `pii_redacted` flag true when changed
3) Add DB schema stub
   - if migrations exist: new migration
   - else: `sql/telemetry_events.sql` with `CREATE TABLE telemetry_events (...)`

### Day 2 (24–48h): Hook Telemetry at a single decision boundary
4) Identify parser NO_MATCH handling location (grep audit)
5) Emit TelemetryEvent from NO_MATCH branch
   - include `session_id`, `tenant_id`, `domain`, `parser_status`, `parser_reason`
   - include `utterance_redacted`, `pii_redacted`
6) Confirm non-blocking behavior
   - local load test smoke (basic, not production grade) ensuring no latency spikes on NO_MATCH path

---

## 9) Telemetry Insights (placeholder)

Once TelemetryEmitter exists, we will track top NO_MATCH reasons:
1) UNKNOWN (telemetry not implemented)
2) UNKNOWN
3) UNKNOWN

---

## 10) Repo Audit Commands (run before broad refactors)

```bash
# Find normalization scattered in controller
grep -RIn "apply_aliases|aliases\.json|norm_simple|regex" src/api/session_controller.py

# Find parser decision boundary
grep -RIn "NO_MATCH|ParserResult|deterministic parser|parse_result" src/api | head -n 100

# Find migrations strategy
find . -maxdepth 3 -type d -iname "*migr*"
grep -RIn "alembic|migrate|migration" -n src | head -n 50
