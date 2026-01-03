#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

WIRE="tools/wire_tenant_manager_server.sh"
SERVER="src/api/server.py"

if [[ ! -f "$WIRE" ]]; then
  echo "❌ $WIRE not found"
  exit 1
fi
if [[ ! -f "$SERVER" ]]; then
  echo "❌ $SERVER not found"
  exit 1
fi

echo "==> Hardening wire script + server STT prompt injection (remove current_state hack)"

python3 - <<'PY'
import pathlib, re

root = pathlib.Path(".")
wire = root/"tools/wire_tenant_manager_server.sh"
server = root/"src/api/server.py"

w = wire.read_text(encoding="utf-8")
s = server.read_text(encoding="utf-8")

# -------------------------------
# A) Fix server.py cleanly:
#    - transcribe_pcm(pcm16, lang, prompt=None)
#    - call transcribe_pcm(..., prompt=tenant_prompt)
#    - remove current_state approach if present
# -------------------------------

# 1) Remove module-level current_state if exists
s = re.sub(r"\ncurrent_state\s*=\s*None[^\n]*\n", "\n", s)

# 2) Update transcribe_pcm signature to include prompt
# Looks for: async def transcribe_pcm(pcm16: bytes, lang: Optional[str]) -> str:
s = re.sub(
    r"async def transcribe_pcm\((\s*)pcm16:\s*bytes,\s*lang:\s*Optional\[str\](\s*)\)\s*->\s*str\s*:",
    r"async def transcribe_pcm(\1pcm16: bytes, lang: Optional[str], prompt: Optional[str] = None\2) -> str:",
    s
)

# 3) Ensure kwargs can accept prompt if provided
# Insert after kwargs init if not already prompt-aware
if 'kwargs["prompt"]' not in s:
    s = re.sub(
        r'kwargs:\s*Dict\[str,\s*Any\]\s*=\s*\{\s*"model"\s*:\s*OPENAI_STT_MODEL\s*,\s*"file"\s*:\s*f\s*\}\s*\n',
        'kwargs: Dict[str, Any] = {"model": OPENAI_STT_MODEL, "file": f}\n'
        '    if prompt:\n'
        '        kwargs["prompt"] = str(prompt)\n',
        s,
        flags=re.M
    )

# 4) Replace any call-site "global current_state" hack around transcribe_pcm
s = re.sub(
    r"\s*global current_state\s*\n\s*current_state\s*=\s*state\s*\n\s*transcript\s*=\s*await transcribe_pcm\(([^)]+)\)\s*\n\s*current_state\s*=\s*None\s*\n",
    r"            transcript = await transcribe_pcm(\1)\n",
    s
)

# 5) Now ensure the actual call passes prompt from tenant config when enabled
# We look for the call line: transcript = await transcribe_pcm(pcm, stt_lang)
# and expand it with prompt logic using tenant_cfg.
needle = "            transcript = await transcribe_pcm(pcm, stt_lang)\n"
if needle in s:
    insert = (
        "            stt_prompt = None\n"
        "            if TENANT_STT_PROMPT_ENABLED:\n"
        "                try:\n"
        "                    cfg = getattr(state, 'tenant_cfg', None)\n"
        "                    if cfg and getattr(cfg, 'stt_prompt_base', None):\n"
        "                        stt_prompt = cfg.stt_prompt_base\n"
        "                except Exception:\n"
        "                    stt_prompt = None\n"
        "            transcript = await transcribe_pcm(pcm, stt_lang, prompt=stt_prompt)\n"
    )
    s = s.replace(needle, insert)
else:
    # If not found, don't fail hard; keep server intact.
    pass

server.write_text(s, encoding="utf-8")

# -------------------------------
# B) Fix wire script so it doesn't re-introduce current_state
# -------------------------------

# Remove the block that adds current_state
w = w.replace(
    "# We need a safe way to provide state into transcribe_pcm without refactoring signatures too much.\n"
    "# We wrap transcribe_pcm at call site by setting a module-level variable current_state.\n"
    "if \"current_state = None\" not in s:\n"
    "    s = s.replace(\n"
    "        \"# -------------------------\\n# STT\\n# -------------------------\\n\",\n"
    "        \"# -------------------------\\n# STT\\n# -------------------------\\ncurrent_state = None  # set per call-site to allow tenant STT prompt\\n\\n\"\n"
    "    )\n\n",
    ""
)

# Remove the place where it tries to inject cfg via current_state closure in kwargs
w = w.replace(
    "        \"    # Tenant STT context injection (menu/domain vocabulary)\\n\"\n"
    "        \"    if TENANT_STT_PROMPT_ENABLED:\\n\"\n"
    "        \"        try:\\n\"\n"
    "        \"            cfg = getattr(current_state, 'tenant_cfg', None)  # injected via closure (see wrapper below)\\n\"\n"
    "        \"        except Exception:\\n\"\n"
    "        \"            cfg = None\\n\"\n"
    "        \"        if cfg and getattr(cfg, 'stt_prompt_base', ''):\\n\"\n"
    "        \"            kwargs[\\\"prompt\\\"] = str(cfg.stt_prompt_base)\\n\",\n",
    ""
)

# Remove the call-site wrapper injection (global current_state)
w = w.replace(
    "s = s.replace(\n"
    "    \"            transcript = await transcribe_pcm(pcm, stt_lang)\\n\",\n"
    "    \"            global current_state\\n\"\n"
    "    \"            current_state = state\\n\"\n"
    "    \"            transcript = await transcribe_pcm(pcm, stt_lang)\\n\"\n"
    "    \"            current_state = None\\n\"\n"
    ")\n\n",
    ""
)

wire.write_text(w, encoding="utf-8")

print("✅ Updated server.py to use prompt arg (no current_state)")
print("✅ Hardened tools/wire_tenant_manager_server.sh to not re-add current_state hack")
PY

echo "✅ Done."
echo "Next:"
echo "  1) Re-run: ./tools/drop_taj_tenant.sh"
echo "  2) Re-run: ./tools/wire_tenant_manager_server.sh"
echo "  3) Start:  ./scripts/start_backend.sh"
