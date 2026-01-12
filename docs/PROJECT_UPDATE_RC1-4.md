Below is a **clean, copy-pasteable project document** you can commit as
`docs/PROJECT_UPDATE_RC1-4.md`.

It is written to **freeze the architectural decision**, explain *why* we stopped, *what changes*, and *how to resume in the next chat without drift*.

No guessing, no extra scope.

---

````md
# PROJECT UPDATE — RC1-4
## Voxeron Voice Agent

**Date:** 2026-01-12  
**Status:** Architectural correction approved  
**Scope:** Deterministic ordering, disambiguation, scalability

---

## 1. Context & Reason for This Update

During RC1-4 stabilization, a structural issue was identified:

- “Naan” works due to **hardcoded, item-specific logic**
- “Biryani”, “Paneer”, “Vegetarian”, etc. fail or behave inconsistently
- Fixing each item individually would introduce **unbounded technical debt**

This document records a **formal architectural intervention** to stop vertical, item-specific logic and replace it with a **horizontal, metadata-driven model**.

This decision is **locked** for RC1-4 and beyond.

---

## 2. Architectural Decision (LOCKED)

### ❌ What We Will NOT Do

- No new methods or branches containing food names  
  (`naan`, `biryani`, `paneer`, etc.)
- No duplication of `_handle_pending_nan_variant` for other items
- No fixes by expanding `aliases.json` to encode taxonomy
- No controller logic that “knows” menu structure

---

### ✅ What We WILL Do

We introduce a **Category Head** abstraction.

A **Category Head** is:
- a **non-orderable menu node**
- representing a group of variants (leaf items)
- resolved deterministically before cart mutation

Examples:
- Biryani → {Chicken, Lamb, Vegetable}
- Naan → {Plain, Garlic, Cheese}
- Paneer → {Butter Paneer, Palak Paneer}

---

## 3. Core Principle: Horizontal Logic

The system must reason in **states**, not **products**.

### Before (Rejected)

```text
if "naan" → ask variant
if "biryani" → ??? (broken)
````

### After (Approved)

```text
if category mentioned without leaf → DISAMBIGUATE(category, options)
```

This applies to **all menu categories**, automatically.

---

## 4. Required Architectural Shifts

### Shift A — Metadata-Driven Parsing (S1-3)

* Menu items gain structural metadata
* Parser detects **category vs leaf**, not strings

Parser responsibility:

* Detect mention of a category without a variant
* Emit `DISAMBIGUATE` with options

Parser must **not** contain food names.

---

### Shift B — Single Generic Disambiguation Slot

* `_handle_pending_nan_variant` is deprecated
* `_handle_pending_disambiguation` becomes the **only** variant latch

Controller rule:

> If `pending_disambiguation` is set, **no other ordering logic runs**

---

### Shift C — Dynamic Prompting

Prompts are constructed from parser data only:

```text
“We have {options}. Which {category} would you like?”
```

No hardcoded prompts.

---

## 5. MenuSnapshot Change (Required)

Menu ingestion must flag category nodes.

### Example (Conceptual)

```json
{
  "item_id": "CAT-BIRYANI",
  "display_name": "Biryani",
  "is_category": true,
  "children": ["LAM-051", "KIP-044", "VEG-012"]
}
```

Leaf items reference parent:

```json
{
  "item_id": "LAM-051",
  "display_name": "Lamb Biryani",
  "parent_id": "CAT-BIRYANI"
}
```

This is the **single source of truth** for ambiguity.

---

## 6. RC1-4 Scope Clarification

RC1-4 goal is **stability + correctness**, not full refactor.

Therefore:

### In Scope

* Introduce Category Head metadata
* Route both Naan and Biryani through generic disambiguation
* Remove reliance on item-specific controller logic
* Preserve deterministic behavior and telemetry

### Out of Scope (Post-RC1-4)

* New parser statuses (`PARTIAL_MATCH`)
* Full menu graph engine
* UI changes

---

## 7. Current Git State (at time of update)

> **IMPORTANT:** This must be verified again when resuming.

Expected state when this document was written:

```bash
git status -sb
```

```
## feature/S1-4-telemetry-emitter
 M src/api/session_controller.py
 M src/api/orchestrator/deterministic_parser.py
 M tenants/taj_mahal/tenant.json
```

No RC3 regression commits have been made.

---

## 8. Plan Forward (Order Matters)

1. **Stop further fixes to biryani / naan**
2. Add `is_category` + `parent_id` to MenuSnapshot
3. Update parser to detect category nodes structurally
4. Route naan through `_handle_pending_disambiguation`
5. Delete / disable `_handle_pending_nan_variant`
6. Verify:

   * “2 biryani” → asks variant
   * “2 garlic naan” → skips disambiguation
7. Run pytest
8. Only then resume live audio testing

---

## 9. Guardrails for All Future PRs

* ❌ No food names in controller logic
* ❌ No new slot handlers per item
* ✅ Menu metadata defines structure
* ✅ Parser emits intent, controller orchestrates state
* ✅ Deterministic first, LLM last

Violations block merge.

---

## 10. Resume Instructions (Next Chat)

Start the next chat with:

> “We are continuing Voxeron Voice Agent RC1-4.
> Category Heads are approved.
> Naan-only logic is deprecated.
> MenuSnapshot must define category vs leaf.
> First step: verify git status and implement category metadata.”

This document is **canonical** for RC1-4 decisions.

````

---

### What I recommend you do now

1. Save this as `docs/PROJECT_UPDATE_RC1-4.md`
2. Commit it **alone** (documentation-only commit)
3. In the next chat, paste:
   ```bash
   cat docs/PROJECT_UPDATE_RC1-4.md
````


