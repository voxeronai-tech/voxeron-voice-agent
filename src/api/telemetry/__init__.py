from __future__ import annotations

from .emitter import TelemetryEmitter
from .insert_asyncpg import insert_telemetry_event

_emitter: TelemetryEmitter | None = None


def get_telemetry_emitter() -> TelemetryEmitter:
    global _emitter
    if _emitter is None:
        _emitter = TelemetryEmitter(insert_telemetry_event)
    return _emitter
