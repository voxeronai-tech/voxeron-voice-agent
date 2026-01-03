#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

mkdir -p scripts tools

cat > scripts/start_backend.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

# Load .env into THIS shell so uvicorn inherits it
set -a
if [[ -f .env ]]; then
  # shellcheck disable=SC1091
  source .env
fi
set +a

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8000}"

# -------------------------
# Tenant / Phase flags (safe defaults)
# -------------------------
# Where tenant folders live
TENANTS_DIR="${TENANTS_DIR:-tenants}"

# Phase A: enable tenant-driven STT prompt + TTS instructions (safe, no logic change)
TENANT_STT_PROMPT_ENABLED="${TENANT_STT_PROMPT_ENABLED:-1}"
TENANT_TTS_INSTRUCTIONS_ENABLED="${TENANT_TTS_INSTRUCTIONS_ENABLED:-1}"

# Phase B/C: enable tenant rules/normalization (start OFF, parallel-run diff logging happens when OFF)
TENANT_RULES_ENABLED="${TENANT_RULES_ENABLED:-0}"

# Export so FastAPI process gets them
export TENANTS_DIR
export TENANT_STT_PROMPT_ENABLED
export TENANT_TTS_INSTRUCTIONS_ENABLED
export TENANT_RULES_ENABLED

# Hard guard only when tenant features are in use
if [[ "$TENANT_STT_PROMPT_ENABLED" == "1" || "$TENANT_TTS_INSTRUCTIONS_ENABLED" == "1" || "$TENANT_RULES_ENABLED" == "1" ]]; then
  if [[ ! -d "$TENANTS_DIR" ]]; then
    echo "❌ TENANTS_DIR '$TENANTS_DIR' not found."
    echo "   Create it (e.g. ./tools/drop_taj_tenant.sh) or set TENANTS_DIR to the correct path."
    exit 1
  fi
fi

echo "==[start_backend]=="
echo "HOST=${HOST} PORT=${PORT}"
echo "TENANTS_DIR=${TENANTS_DIR}"
echo "TENANT_STT_PROMPT_ENABLED=${TENANT_STT_PROMPT_ENABLED}"
echo "TENANT_TTS_INSTRUCTIONS_ENABLED=${TENANT_TTS_INSTRUCTIONS_ENABLED}"
echo "TENANT_RULES_ENABLED=${TENANT_RULES_ENABLED}"
echo "OPENAI_STT_MODEL=${OPENAI_STT_MODEL:-}"
echo "OPENAI_CHAT_MODEL=${OPENAI_CHAT_MODEL:-}"
echo "OPENAI_TTS_MODEL=${OPENAI_TTS_MODEL:-}"
echo "OPENAI_AUDIO_FILENAME_HINT=${OPENAI_AUDIO_FILENAME_HINT:-}"
echo "DATABASE_URL set: $([[ -n "${DATABASE_URL:-}" ]] && echo yes || echo no)"
echo "Press CTRL+C to stop"

exec uvicorn src.api.server:app --reload --host "${HOST}" --port "${PORT}"
SH

chmod +x scripts/start_backend.sh

cat > scripts/start_frontend.sh <<'SH'
#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")/.."

HOST="127.0.0.1"
BASE_PORT="${1:-8080}"
TENANT="${TENANT:-default}"

pick_port() {
  local p="$1"
  while true; do
    if python - <<PY >/dev/null 2>&1
import socket
s=socket.socket()
try:
    s.bind(("${HOST}", ${p}))
    print("free")
finally:
    s.close()
PY
    then
      echo "${p}"
      return 0
    fi
    p=$((p+1))
    if [ "$p" -gt $((BASE_PORT+20)) ]; then
      echo "No free port found in range ${BASE_PORT}..$((BASE_PORT+20))" >&2
      return 1
    fi
  done
}

PORT="$(pick_port "${BASE_PORT}")"

echo "==[start_frontend]=="
echo "Serving repo root over HTTP"
echo "URL: http://${HOST}:${PORT}/voice_widget.html?tenant=${TENANT}"
echo "Press CTRL+C to stop"

exec python -m http.server "${PORT}" --bind "${HOST}"
SH

chmod +x scripts/start_frontend.sh

echo "✅ Updated:"
echo "  - scripts/start_backend.sh (tenant flags + guards)"
echo "  - scripts/start_frontend.sh (prints tenant URL param)"
