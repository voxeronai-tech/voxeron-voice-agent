import json
import pathlib
import pytest

from src.api.tenant_manager import TenantManager

ROOT = pathlib.Path(__file__).resolve().parents[3]
TENANTS_DIR = ROOT / "tenants"
TAJ_DIR = TENANTS_DIR / "taj_mahal"
REGRESSION = ROOT / "tests" / "regression" / "transcripts.json"

@pytest.mark.skipif(not TAJ_DIR.exists(), reason="tenants/taj_mahal not present in this checkout")
def test_taj_mahal_regression_pack():
    tm = TenantManager(str(TENANTS_DIR))
    cfg = tm.load_tenant("taj_mahal")

    cases = json.loads(REGRESSION.read_text(encoding="utf-8"))
    assert isinstance(cases, list) and cases

    for c in cases:
        lang = c.get("lang", "en")
        text = c["input"]
        out = tm.normalize_text(cfg, lang, text)

        exp = c.get("expect", {})
        for token in exp.get("normalized_contains", []):
            assert token.lower() in out.lower(), f"{c['id']} expected '{token}' in '{out}'"
        for token in exp.get("normalized_not_contains", []):
            assert token.lower() not in out.lower(), f"{c['id']} expected '{token}' NOT in '{out}'"
