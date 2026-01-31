from src.api.telemetry.emitter import MAX_UTTERANCE, redact_pii_mvp


def test_truncation_head_tail():
    out, pii_redacted, truncated = redact_pii_mvp("a" * 200)
    assert len(out) <= MAX_UTTERANCE
    assert pii_redacted is False
    assert truncated is True


def test_redacts_email():
    out, pii_redacted, truncated = redact_pii_mvp("contact me test@example.com ok")
    assert "[REDACTED_EMAIL]" in out
    assert pii_redacted is True
    assert truncated is False


def test_redacts_numbers():
    out, pii_redacted, truncated = redact_pii_mvp("order 12345 please")
    assert "[REDACTED_NUM]" in out
    assert pii_redacted is True
    assert truncated is False
