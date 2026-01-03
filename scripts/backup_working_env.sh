#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="/home/marcelino/projects/voxeron-voice-agent"
BACKUP_ROOT="${PROJECT_DIR}/_backups"
TS="$(date +%Y%m%d_%H%M%S)"
BACKUP_DIR="${BACKUP_ROOT}/working_env_${TS}"

FILES=(
  "${PROJECT_DIR}/voice_widget_ptt.html"
  "${PROJECT_DIR}/src/api/server.py"
  "${PROJECT_DIR}/src/agent/voice_agent.py"
  "${PROJECT_DIR}/.env"
)

mkdir -p "${BACKUP_DIR}"

echo "==> Backing up working env to: ${BACKUP_DIR}"
for f in "${FILES[@]}"; do
  if [[ -f "${f}" ]]; then
    mkdir -p "${BACKUP_DIR}/$(dirname "${f#"${PROJECT_DIR}/"}")"
    cp -a "${f}" "${BACKUP_DIR}/$(dirname "${f#"${PROJECT_DIR}/"}")/"
    echo "  - saved: ${f}"
  else
    echo "  - WARNING missing: ${f}"
  fi
done

echo "==> Done. Backup: ${BACKUP_DIR}"
