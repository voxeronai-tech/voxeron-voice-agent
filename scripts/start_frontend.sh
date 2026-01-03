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
