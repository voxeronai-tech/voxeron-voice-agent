# Sprint 1 — Orchestrator + Deterministic Parser MVP

## Goal
Replace “fat SessionController” path with:
- CognitiveOrchestrator calling DeterministicParser before any LLM call
- Typed ParserResult contract + reason codes
- Telemetry event emitted on NO_MATCH
- Minimal Redis SessionState wrapper (Phase 1: shadow-write)

## Definition of Done
- ParserResult dataclass exists + unit tested
- Parser called before LLM in the runtime flow
- NO_MATCH triggers telemetry event async (non-blocking)
- No regression in Taj demo call flow (order add/update works)
- WebSocket close cancels tasks (no orphan LLM/TTS)

## Branch
feature/sprint-1-orchestrator-parser

## Issues
- S1-1 ParserResult contract
- S1-2 Deterministic-first flow
- S1-3 Telemetry emitter + events table
- S1-4 Redis SessionState (shadow write)
- S1-5 WS cleanup guarantees (cancel tasks)
- S1-6 Tests + load sanity
