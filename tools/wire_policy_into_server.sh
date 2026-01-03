#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

echo "🔧 Repairing known-bad governor injection artefacts (idempotent)..."

python3 - <<'PY'
from pathlib import Path
import re

p = Path("src/api/server.py")
s = p.read_text(encoding="utf-8")

# 1) Fix any accidental literal-newline string joins in _policy_guard_append
#    (this catches the exact failure mode you hit)
s = re.sub(
    r'return\s+\(system_text\s+or\s+""\)\.rstrip\(\)\s*\+\s*"\s*\n\s*"\s*\+\s*guard\.strip\(\)',
    r'return (system_text or "").rstrip() + "\\n\\n" + guard.strip()',
    s,
    flags=re.M
)

# 2) Ensure _policy_guard_append exists and has robust cart mapping
guard_impl = r'''
def _policy_guard_append(state, system_text: str) -> str:
    """Append deterministic 'Governor' instructions to the LLM system prompt."""
    try:
        ps = SessionPolicyState(lang=getattr(state, "lang", "en"))

        # Map server OrderState (dict item_id->qty) into policy Order(items=[OrderItem...])
        try:
            menu = getattr(state, "menu", None)
            order_obj = getattr(state, "order", None)
            items_dict = getattr(order_obj, "items", None)

            if isinstance(items_dict, dict):
                for item_id, qty in items_dict.items():
                    try:
                        q = int(qty)
                    except Exception:
                        q = 1
                    if q <= 0:
                        continue

                    if menu is not None:
                        try:
                            name = menu.display_name(item_id)
                        except Exception:
                            name = str(item_id)
                    else:
                        name = str(item_id)

                    ps.order.add(name, q)
        except Exception:
            pass

        # Carry stickiness across
        try:
            ps.last_category = getattr(state, "last_category", None)
            ps.last_category_items = list(getattr(state, "last_category_items", []) or [])
        except Exception:
            pass

        guard = system_guard_for_llm(ps)
        return (system_text or "").rstrip() + "\\n\\n" + guard.strip()
    except Exception:
        return system_text
'''.strip()

# Replace existing _policy_guard_append body (if present)
if "def _policy_guard_append" in s:
    s2, n = re.subn(
        r"def _policy_guard_append\(state, system_text: str\) -> str:\n(?:.*?\n)\n(?=def build_llm_messages)",
        guard_impl + "\n\n",
        s,
        flags=re.S,
        count=1,
    )
    if n == 1:
        s = s2

# 3) Remove wrong policy.set_last_category(state, ...) calls if any exist
s = re.sub(
    r"\n\s*try:\n\s*set_last_category\(state,\s*cat,\s*cat_items\)\n\s*except Exception:\n\s*pass\n",
    "\n",
    s,
    flags=re.S
)

p.write_text(s, encoding="utf-8")
print("✅ Repair complete.")
PY

echo "Now run:"
echo "  python -m py_compile src/api/server.py"
echo "  pytest"
