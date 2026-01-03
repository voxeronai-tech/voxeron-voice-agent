#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Voxeron: setting up tests under $ROOT"

mkdir -p tests/fixtures/tenant_min/schema
mkdir -p tests/regression
mkdir -p tests/tenants/taj_mahal

# -----------------------------
# Fixture tenant config (tenant-agnostic)
# -----------------------------
cat > tests/fixtures/tenant_min/tenant.json <<'JSON'
{
  "tenant_name": "Tenant Minimal Fixture",
  "base_language": "en",
  "supported_langs": ["en", "nl"],
  "tts": {
    "voices": { "en": "cedar", "nl": "marin" },
    "instructions": {
      "en": "Speak English naturally.",
      "nl": "Spreek vloeiend Nederlands."
    }
  },
  "stt": {
    "prompt_base": "Restaurant demo vocabulary.",
    "prompt_max_items": 8
  }
}
JSON

cat > tests/fixtures/tenant_min/phonetics.json <<'JSON'
{
  "gates": {
    "naam_to_naan": true
  },
  "patterns": {
    "*": [
      { "pattern": "\\\\bnederl[âa]ns\\\\b", "replace": "Nederlands", "flags": ["I"] }
    ],
    "nl": [
      { "pattern": "\\\\breis\\\\b", "replace": "rijst", "flags": ["I"] },
      { "pattern": "\\\\blamp\\\\b", "replace": "lamb", "flags": ["I"] }
    ]
  }
}
JSON

cat > tests/fixtures/tenant_min/rules.json <<'JSON'
{
  "upsell": {
    "enable": true,
    "never_suggest_if_in_cart": ["naan", "rijst"]
  },
  "category_aliases": {
    "lamb": ["lam", "lams", "lamsgerecht", "lamsgerechten", "lamb"]
  }
}
JSON

# -----------------------------
# Schemas (optional in tests)
# -----------------------------
cat > tests/fixtures/tenant_min/schema/tenant.schema.json <<'JSON'
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Voxeron Tenant Config",
  "type": "object",
  "required": ["tenant_name", "base_language", "supported_langs", "tts", "stt"],
  "properties": {
    "tenant_name": { "type": "string", "minLength": 1 },
    "base_language": { "type": "string", "enum": ["en", "nl"] },
    "supported_langs": {
      "type": "array",
      "items": { "type": "string", "enum": ["en", "nl"] },
      "minItems": 1,
      "uniqueItems": true
    },
    "tts": {
      "type": "object",
      "required": ["voices", "instructions"],
      "properties": {
        "voices": {
          "type": "object",
          "properties": {
            "en": { "type": "string", "minLength": 1 },
            "nl": { "type": "string", "minLength": 1 }
          },
          "additionalProperties": false
        },
        "instructions": {
          "type": "object",
          "properties": {
            "en": { "type": "string" },
            "nl": { "type": "string" }
          },
          "additionalProperties": false
        }
      },
      "additionalProperties": true
    },
    "stt": {
      "type": "object",
      "required": ["prompt_base", "prompt_max_items"],
      "properties": {
        "prompt_base": { "type": "string" },
        "prompt_max_items": { "type": "integer", "minimum": 0, "maximum": 200 }
      },
      "additionalProperties": true
    }
  },
  "additionalProperties": true
}
JSON

cat > tests/fixtures/tenant_min/schema/phonetics.schema.json <<'JSON'
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Voxeron Phonetics Rules",
  "type": "object",
  "required": ["patterns"],
  "properties": {
    "patterns": {
      "type": "object",
      "patternProperties": {
        "^(\\\\*|en|nl)$": {
          "type": "array",
          "items": {
            "type": "object",
            "required": ["pattern", "replace"],
            "properties": {
              "pattern": { "type": "string", "minLength": 1 },
              "replace": { "type": "string" },
              "flags": {
                "type": "array",
                "items": { "type": "string", "enum": ["I", "M"] },
                "uniqueItems": true
              }
            },
            "additionalProperties": false
          }
        }
      },
      "additionalProperties": false
    },
    "gates": {
      "type": "object",
      "properties": { "naam_to_naan": { "type": "boolean" } },
      "additionalProperties": true
    }
  },
  "additionalProperties": false
}
JSON

cat > tests/fixtures/tenant_min/schema/rules.schema.json <<'JSON'
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "Voxeron Tenant Rules",
  "type": "object",
  "properties": {
    "upsell": {
      "type": "object",
      "properties": {
        "enable": { "type": "boolean" },
        "never_suggest_if_in_cart": {
          "type": "array",
          "items": { "type": "string", "minLength": 1 },
          "uniqueItems": true
        }
      },
      "additionalProperties": true
    },
    "category_aliases": {
      "type": "object",
      "additionalProperties": {
        "type": "array",
        "items": { "type": "string", "minLength": 1 },
        "uniqueItems": true
      }
    }
  },
  "additionalProperties": true
}
JSON

# -----------------------------
# Core (tenant-agnostic) tests
# -----------------------------
cat > tests/test_tenant_manager_core.py <<'PY'
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
PY

# -----------------------------
# Taj Mahal "demo lock" regression tests (tenant-specific & optional)
# -----------------------------
cat > tests/tenants/taj_mahal/test_regression_taj_mahal.py <<'PY'
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
PY

# -----------------------------
# Taj Mahal regression transcripts (gold set)
# -----------------------------
cat > tests/regression/transcripts.json <<'JSON'
[
  {
    "id": "lang_pick_nl",
    "lang": "en",
    "input": "Nederlands.",
    "expect": { "normalized_contains": ["Nederlands"] }
  },
  {
    "id": "nl_order_butter_chicken_naan",
    "lang": "nl",
    "input": "Ik wil graag twee butter chicken en twee naan.",
    "expect": { "normalized_contains": ["twee", "butter", "naan"] }
  },
  {
    "id": "nl_rice_slip_reis_to_rijst",
    "lang": "nl",
    "input": "Ik wil reis erbij.",
    "expect": { "normalized_contains": ["rijst"] }
  },
  {
    "id": "nl_lamp_to_lamb",
    "lang": "nl",
    "input": "Biryani ook lamp Madras.",
    "expect": { "normalized_contains": ["lamb"] }
  },
  {
    "id": "nl_naam_gate_quantity",
    "lang": "nl",
    "input": "Twee naam graag.",
    "expect": { "normalized_contains": ["naan"] }
  },
  {
    "id": "nl_naam_gate_no_intent",
    "lang": "nl",
    "input": "Mijn naam is Marcel.",
    "expect": { "normalized_not_contains": ["naan"] }
  }
]
JSON

echo "✅ Done."
echo "Install deps if needed: pip install -U pytest jsonschema"
echo "Run core: pytest -q tests/test_tenant_manager_core.py"
echo "Run all : pytest -q"
