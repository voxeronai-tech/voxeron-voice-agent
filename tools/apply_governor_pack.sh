#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."

python3 - <<'PY'
from pathlib import Path
import re

p = Path("src/api/server.py")
s = p.read_text(encoding="utf-8")

# -----------------------------
# 1) Replace _policy_guard_append with a robust version
#    - No literal newlines in strings
#    - Maps OrderState dict -> policy OrderItem list
#    - Works with or without menu snapshot
# -----------------------------
new_guard = r'''
def _policy_guard_append(state, system_text: str) -> str:
    """Append deterministic 'Governor' instructions to the LLM system prompt."""
    try:
        ps = SessionPolicyState(lang=getattr(state, "lang", "en"))

        # --- Map server OrderState (dict item_id->qty) into policy Order(items=[OrderItem...]) ---
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

                    # Resolve display name if menu exists, otherwise fall back to item_id
                    if menu is not None:
                        try:
                            name = menu.display_name(item_id)
                        except Exception:
                            name = str(item_id)
                    else:
                        name = str(item_id)

                    # policy.py Order.add(name, qty)
                    ps.order.add(name, q)
        except Exception:
            pass

        # --- Carry stickiness across ---
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

# Replace entire function block
# from "def _policy_guard_append" up to the next blank line before "def build_llm_messages"
pattern = r"def _policy_guard_append\(state, system_text: str\) -> str:\n(?:.*?\n)\n(?=def build_llm_messages)"
s2, n = re.subn(pattern, new_guard + "\n\n", s, flags=re.S)
if n != 1:
    raise SystemExit(f"❌ Could not replace _policy_guard_append cleanly (matches={n}).")
s = s2

# -----------------------------
# 2) Remove the WRONG set_last_category(state, ...) call
#    - policy.set_last_category expects SessionPolicyState, not SessionState
#    - you already store stickiness in state.last_category/_items, that’s sufficient
# -----------------------------
# Remove only this block:
# try:
#     set_last_category(state, cat, cat_items)
# except Exception:
#     pass
s = re.sub(
    r"\n\s*try:\n\s*set_last_category\(state,\s*cat,\s*cat_items\)\n\s*except Exception:\n\s*pass\n",
    "\n",
    s,
    flags=re.S
)

p.write_text(s, encoding="utf-8")
print("✅ server.py patched: governor cart-mapping fixed + removed wrong set_last_category call.")
PY

python -m py_compile src/api/server.py
pytest
echo "✅ All done: server.py compiles and tests are green."
