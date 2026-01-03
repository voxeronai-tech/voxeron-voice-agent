import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def show(schema_path: str):
    p = ROOT / schema_path
    s = json.loads(p.read_text(encoding="utf-8"))

    print(f"\n== {schema_path} ==")
    print("title:", s.get("title"))
    print("type:", s.get("type"))
    print("required:", s.get("required"))
    print("properties:", list(s.get("properties", {}).keys()))
    if "$defs" in s:
        print("$defs:", list(s["$defs"].keys()))

show("architecture/schemas/domain/order_draft.v0.6.json")
show("architecture/schemas/domain/order_result.v0.6.json")
show("architecture/schemas/tools/create_pos_order.v0.6.json")
