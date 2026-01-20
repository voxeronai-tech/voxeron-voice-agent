from __future__ import annotations

import pytest

from src.api.telemetry.emitter import redact_pii_mvp


@pytest.mark.parametrize("raw", [
    "email me at test@example.com",
    "call me at +31 6 1234 5678",
    "my id is 1234567890",
])
def test_redact_pii_mvp_masks_sensitive(raw: str):
    red, changed, trunc = redact_pii_mvp(raw)
    assert "test@example.com" not in red
    assert "+31" not in red
    assert "1234567890" not in red
    assert changed is True
    assert len(red) <= 100
