#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Fixing tenant_min fixture phonetics + schema escaping"

mkdir -p tests/fixtures/tenant_min/schema

# ---------------------------------------------------------
# Fix phonetics.json (remove over-escaping so \b works)
# ---------------------------------------------------------
cat > tests/fixtures/tenant_min/phonetics.json <<'JSON'
{
  "gates": {
    "naam_to_naan": true
  },
  "patterns": {
    "*": [
      { "pattern": "\\bnederl[âa]ns\\b", "replace": "Nederlands", "flags": ["I"] }
    ],
    "nl": [
      { "pattern": "\\breis\\b", "replace": "rijst", "flags": ["I"] },
      { "pattern": "\\blamp\\b", "replace": "lamb", "flags": ["I"] }
    ]
  }
}
JSON

# ---------------------------------------------------------
# Fix phonetics.schema.json to allow "*" (not "\*")
# ---------------------------------------------------------
cat > tests/fixtures/tenant_min/schema/phonetics.schema.json <<'JSON'
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "type": "object",
  "additionalProperties": false,
  "required": ["patterns"],
  "properties": {
    "gates": {
      "type": "object",
      "additionalProperties": false,
      "properties": {
        "naam_to_naan": { "type": "boolean" }
      }
    },
    "patterns": {
      "type": "object",
      "patternProperties": {
        "^(\\*|en|nl)$": {
          "type": "array",
          "items": {
            "type": "object",
            "additionalProperties": false,
            "required": ["pattern", "replace"],
            "properties": {
              "pattern": { "type": "string", "minLength": 1 },
              "replace": { "type": "string" },
              "flags": {
                "type": "array",
                "items": { "type": "string", "enum": ["I", "M"] },
                "uniqueItems": true
              }
            }
          }
        }
      },
      "additionalProperties": false
    }
  }
}
JSON

echo "✅ Fixed:"
echo " - tests/fixtures/tenant_min/phonetics.json"
echo " - tests/fixtures/tenant_min/schema/phonetics.schema.json"
echo
echo "Now run:"
echo "  pytest -q tests/test_tenant_manager_core.py"
