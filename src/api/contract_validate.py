from __future__ import annotations

import json
from pathlib import Path
from jsonschema import Draft202012Validator

ARCH_DIR = Path(__file__).resolve().parents[2] / "architecture"
SCHEMAS_DIR = ARCH_DIR / "schemas"

def load_schema(rel_path: str) -> dict:
    p = SCHEMAS_DIR / rel_path
    if not p.exists():
        raise FileNotFoundError(f"Schema not found: {p}")
    return json.loads(p.read_text(encoding="utf-8"))

def validate_payload(schema_rel_path: str, payload: dict) -> None:
    schema = load_schema(schema_rel_path)
    Draft202012Validator(schema).validate(payload)
