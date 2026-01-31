-- Telemetry events table (S1-4)
-- Idempotent creation

CREATE TABLE IF NOT EXISTS telemetry_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),

    session_id TEXT NOT NULL,
    tenant_id TEXT NOT NULL,
    domain TEXT NOT NULL,

    parser_status TEXT NOT NULL,
    parser_reason TEXT NOT NULL,

    utterance_redacted TEXT NOT NULL,
    pii_redacted BOOLEAN NOT NULL DEFAULT true,

    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_telemetry_events_created_at
    ON telemetry_events (created_at DESC);

CREATE INDEX IF NOT EXISTS idx_telemetry_events_tenant
    ON telemetry_events (tenant_id);
