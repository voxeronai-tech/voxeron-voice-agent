import json
import pathlib
import pytest

try:
    import jsonschema
except Exception:
    jsonschema = None

from src.api.tenant_manager import TenantManager

ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "tenant_min"

def _load(p: pathlib.Path):
    return json.loads(p.read_text(encoding="utf-8"))

def test_load_fixture_tenant():
    tm = TenantManager(str(FIXTURE.parent))  # points to tests/fixtures
    cfg = tm.load_tenant("tenant_min")
    assert cfg.tenant_name
    assert "en" in cfg.supported_langs
    assert "nl" in cfg.supported_langs

@pytest.mark.skipif(jsonschema is None, reason="jsonschema not installed")
def test_fixture_schema_validation():
    tenant = _load(FIXTURE / "tenant.json")
    phonetics = _load(FIXTURE / "phonetics.json")
    rules = _load(FIXTURE / "rules.json")

    tenant_schema = _load(FIXTURE / "schema" / "tenant.schema.json")
    phonetics_schema = _load(FIXTURE / "schema" / "phonetics.schema.json")
    rules_schema = _load(FIXTURE / "schema" / "rules.schema.json")

    jsonschema.validate(tenant, tenant_schema)
    jsonschema.validate(phonetics, phonetics_schema)
    jsonschema.validate(rules, rules_schema)

def test_normalize_applies_language_specific_rules():
    tm = TenantManager(str(FIXTURE.parent))
    cfg = tm.load_tenant("tenant_min")

    out = tm.normalize_text(cfg, "nl", "Ik wil reis en lamp madras.")
    assert "rijst" in out.lower()
    assert "lamb" in out.lower()

def test_naam_to_naan_gate_quantity_or_intent_only():
    tm = TenantManager(str(FIXTURE.parent))
    cfg = tm.load_tenant("tenant_min")

    out1 = tm.normalize_text(cfg, "nl", "Twee naam graag.")
    assert "naan" in out1.lower()

    out2 = tm.normalize_text(cfg, "nl", "Mijn naam is Marcel.")
    assert "naan" not in out2.lower()
