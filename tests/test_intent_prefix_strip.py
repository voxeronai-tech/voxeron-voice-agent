from dataclasses import dataclass
from typing import Any, Dict

from src.api.tenant_manager import TenantManager


@dataclass
class FakeCfg:
    base_language: str
    intents: Dict[str, Any]


def test_strip_affirmation_prefix_nl_doe_maar_with_remainder():
    tm = TenantManager(base_dir="tenants")
    cfg = FakeCfg(
        base_language="nl",
        intents={"nl": {"affirmation_triggers": ["doe maar", "ja doe maar", "graag"]}},
    )

    out, stripped, trig = tm.strip_affirmation_prefix(cfg, "doe maar de chicken", "nl")
    assert stripped is True
    assert trig in ("doe maar", "ja doe maar")
    assert out == "chicken"


def test_do_not_strip_when_only_prefix():
    tm = TenantManager(base_dir="tenants")
    cfg = FakeCfg(
        base_language="nl",
        intents={"nl": {"affirmation_triggers": ["doe maar"]}},
    )

    out, stripped, _ = tm.strip_affirmation_prefix(cfg, "doe maar", "nl")
    assert stripped is False
    assert out == "doe maar"

def test_strip_affirmation_prefix_does_not_match_inside_word():
    from src.api.tenant_manager import TenantManager

    tm = TenantManager(base_dir="tenants")

    class Cfg:
        base_language = "en"
        intents = {"en": {"affirmation_triggers": ["ok", "okay", "yes"]}}

    cfg = Cfg()

    out, stripped, trig = tm.strip_affirmation_prefix(cfg, "oké", "en")
    assert stripped is False
    assert out == "oké"
