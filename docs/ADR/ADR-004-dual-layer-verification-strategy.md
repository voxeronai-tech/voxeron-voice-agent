# ADR-004: Dual-Layer Verification Strategy (A1/A2/B1/B2)

**Status:** Proposed / Active  
**Owner:** Architecture  
**Date:** 2026-01-20  
**Scope:** Voxeron Voice Agent (RC1-4 → S1-4A and beyond)

## Context

Voxeron is a deterministic-first voice commerce platform. End-to-end “black box” testing is insufficient because it conflates:
- linguistic normalization and tenant-specific text handling, with
- multi-turn state-machine orchestration, and
- non-deterministic external components (STT/LLM/TTS).

We must enforce deterministic reliability while remaining honest about what each test type guarantees.

## Decision

We adopt a four-layer verification hierarchy:

| Layer | Type | Responsibility | Performance Target | Gate |
|------:|------|----------------|--------------------|------|
| **A1** | Micro Invariant | Pure normalization (lowercasing, punctuation, basic typos). | < 1ms | Pre-commit |
| **A2** | Tenant Invariant | Alias expansion, prefix stripping, tenant-scoped logic. | < 10ms | Pre-commit |
| **B1** | Controller Golden | Headless state-machine transitions (the “Brain” without the “Ears”). | < 100ms | Pre-commit |
| **B2** | Integration Smoke | Full-stack (STT/LLM/TTS enabled) validation. | Seconds | Nightly/CD |

### Guarantees

**Guaranteed**
- A specific text string produces a specific deterministic transformation (A1/A2), and/or
- a specific sequence of turns produces the expected state-machine mutation for a specific tenant (B1).

**Not Guaranteed**
- A human voice is transcribed correctly (STT variability) — this is B2.
- The LLM never hallucinates during fallback — this is monitored via S1-4 telemetry and validated by B2.

## Consequences

1. **Fail-fast ordering**
   - A1/A2 failures must be resolved before diagnosing B1/B2 failures.
2. **Developer velocity**
   - Pre-commit gates never depend on external AI services.
3. **Operational ergonomics**
   - Avoid mega-JSON files. Prefer directory-as-suite to reduce merge conflicts and improve reviewability.
4. **Honest coverage**
   - Passing B1 does not imply STT/LLM behavior correctness; it implies deterministic orchestration correctness.

## Directory Convention

Recommended layout:

- `tests/regression/A1/` (universal normalization invariants)
- `tests/regression/A2/<tenant_ref>/` (tenant-aware invariants)
- `tests/regression/B1/<tenant_ref>/` (golden state-machine flows)
- `tests/regression/B2/<tenant_ref>/` (nightly integration smoke scripts/config)

## Engineer Checklist

Use these questions to classify a new test:

1. **Does the text handling change by tenant?**
   - YES → A2
   - NO  → A1
2. **Are you validating word-to-meaning mapping only?**
   - YES → A1/A2
3. **Are you validating multi-turn meaning-to-action transitions?**
   - YES → B1
4. **Does the test require real STT/LLM/TTS?**
   - YES → B2 (nightly/CD only)

## Notes

- The pre-commit hook should run A1/A2/B1 (and telemetry redaction tests), but must exclude B2.
- Telemetry (S1-4A) provides the evidence base for whether an S2 behavioral change is justified.

