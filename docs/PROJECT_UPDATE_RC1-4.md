Below is an **updated, final handover** reflecting everything that actually landed in Git, including the successful RC1-4 disambiguation merge, cleanup decisions, and the correct continuation point.

This version **replaces** the previous handover.
You can copy-paste it verbatim into a new chat.

---

# VOXERON VOICE AGENT — HANDOVER PROMPT (RC1-4)

## 🔒 Canonical Context Rule (MANDATORY)

This handover is the **single source of truth** for resuming work.
Chat memory is **not authoritative**.

**Reality = Git repository + this handover.**

On resume:

1. Ask for `git status -sb`
2. Verify branch and commits
3. Continue **only** from here

---

## 1. Project & Architectural Direction (LOCKED)

**Project:** Voxeron Voice Agent
**Goal:** Deterministic, production-grade voice ordering (restaurant first, horizontal platform later)

### Architectural principles (NON-NEGOTIABLE)

* ❌ No item-specific logic in `SessionController`
* ❌ No menu knowledge encoded in controller conditionals
* ❌ No conversational phrases in STT prompts
* ✅ Deterministic parser first, LLM last
* ✅ Generic slot latches driven by menu metadata
* ✅ Typed state objects for multi-turn flows
* ✅ Controller orchestrates state, never interprets language

This direction is **approved, implemented, and frozen**.

---

## 2. Current Git State (FACTUAL)

### Active branch

```
feature/S1-4-telemetry-emitter
```

### HEAD commit

```
5242abc RC1-4: category-head disambiguation with typed DisambiguationContext
```

### Recent commits (chronological)

1. `75cf048` – Telemetry emitter groundwork
2. `2d09395` – Architecture freeze: Category Head abstraction
3. `200882e` – MenuSnapshot ambiguity metadata (Category Heads)
4. `5242abc` – Typed DisambiguationContext + generic disambiguation latch

Branch is **clean and pushed**.
Local == origin.

---

## 3. RC Status

### RC3

* ✅ **CLOSED**
* Last stable reference: `rc3-closed-2026-01-09`
* Behavior baseline for regressions

### RC1-4 (CURRENT)

**Theme:** Deterministic orchestration, telemetry, generic disambiguation

#### Delivered and WORKING

* `MenuSnapshot.ambiguity_options` (category heads, e.g. biryani, curry, tikka)
* `DisambiguationContext` dataclass (moved to `src/api/models/state.py`)
* Generic category-head disambiguation (naan, biryani, paneer, curry, etc.)
* Controller latch: `pending_disambiguation`
* Cleaned logging (debug spam removed)
* Telemetry emitter wired (non-blocking)

Example now works correctly:

> “two butter chicken and two biryani”
> → system asks **which biryani**, preserves qty = 2
> → “lamb” resolves to **2× Lamb Biryani**

---

## 4. What Was Intentionally REMOVED

These were **deliberate cleanups**, not regressions:

* ❌ Item-specific naan logic (being phased out)
* ❌ Regex-based “biryani / chicken / naan” checks in controller
* ❌ Conversational bias in STT prompt
* ❌ Debug logs:

  * `DISAMB_PAYLOAD`
  * `DISAMB_SET`
  * verbose `ORCH_MATCH transcript=…`

Only **one canonical ORCH_MATCH log** remains (route-level only).

---

## 5. Current Known Issues (OPEN)

### 5.1 Affirmation + item regression (IMPORTANT)

Phrase patterns like:

* “doe maar de chicken”
* “ja, de lamb”

Previously worked in RC3.
Now partially broken.

**Confirmed facts:**

* ❌ NOT an STT issue (prompt intentionally de-biased)
* ❌ NOT a menu metadata issue
* ✅ Regression introduced during disambiguation refactor
* Likely cause:

  * affirmation intent handling lost precedence
  * interaction between confirmation/affirmation and add-item intent

⚠️ **Do NOT fix by adding controller hacks or STT phrases.**

---

## 6. Architectural Guardrails (DO NOT VIOLATE)

### STT

* Bias ONLY with menu terms and phonetics
* No affirmations, no intents, no conversation glue

### Intents

* “ja”, “doe maar”, “klopt”, “yes”, “correct”
* Must live in `tenants/*/intents.yaml`
* Resolved by intent engine / parser

### Controller

* No language interpretation
* No menu knowledge
* Only reacts to:

  * parser results
  * typed state (`pending_*`)

---

## 7. Required First Commands in New Chat

Before **any reasoning**, run and paste:

```bash
git status -sb
```

Then establish regression baseline:

```bash
git diff rc3-closed-2026-01-09..HEAD -- src/api/session_controller.py
```

Optional but useful:

```bash
git diff rc3-closed-2026-01-09..HEAD -- src/api/orchestrator
```

---

## 8. Immediate Next Steps (ORDERED)

1. **Do not add features**
2. Diff RC3 vs current to isolate affirmation flow
3. Identify where affirmation intent used to short-circuit add-item
4. Restore behavior via:

   * intent resolution
   * parser routing
   * WITHOUT controller conditionals
5. Fix failing deterministic tests (if any)
6. Only then resume live voice testing

---

## 9. Testing Rules

* `pytest` must be green before live audio
* Live testing is validation, not debugging
* Logs to inspect:

  * `STT_RESULT`
  * parser route
  * state transitions (`pending_*`)

---

## 🔁 Resume Prompt for New Chat

Use **exactly** this:

> “We are continuing Voxeron Voice Agent RC1-4.
> RC3 is closed and stable.
> Category-head disambiguation is implemented and working.
> There is a regression in affirmation + item (‘doe maar de chicken’).
> First step: ask me for `git status -sb`, then diff against `rc3-closed-2026-01-09`.
> Do not re-explain architecture or add features.”

---

If you want, in the **next chat** I can:

* do a surgical RC3→RC1-4 diff
* reconstruct the lost affirmation intent path
* or help you fully delete remaining naan-specific legacy logic

Just start the new chat with the resume prompt above.
