from src.api.telemetry.emitter import redact_pii_mvp, MAX_UTTERANCE


def test_truncation_head_tail():
    out, changed, reason = redact_pii_mvp("a" * 200)
    assert len(out) <= MAX_UTTERANCE
    assert changed is True
    assert reason  # non-empty


def test_redacts_email():
    out, changed, reason = redact_pii_mvp("contact me test@example.com ok")
    assert "[REDACTED_EMAIL]" in out
    assert changed is True
    assert reason  # non-empty


def test_redacts_numbers():
    out, changed, reason = redact_pii_mvp("order 12345 please")
    assert "[REDACTED_NUM]" in out
    assert changed is True
    assert reason  # non-empty
