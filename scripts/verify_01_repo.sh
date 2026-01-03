#!/usr/bin/env bash
set -euo pipefail

echo "==[verify_01_repo]=="
echo "PWD: $(pwd)"
echo

echo "-- Git status (voice-agent) --"
git status -sb || true
echo

echo "-- Submodule status --"
git submodule status || true
echo

echo "-- Architecture submodule refs --"
if [ -d "architecture/.git" ] || [ -f "architecture/.git" ]; then
  (cd architecture && echo "architecture HEAD: $(git rev-parse --short HEAD)" && git status -sb) || true
else
  echo "architecture submodule not found at ./architecture"
fi
echo

echo "-- Python --"
python -V || true
echo

echo "-- Key env vars present? (not printing secrets) --"
for k in DATABASE_URL OPENAI_API_KEY ELEVENLABS_API_KEY; do
  if [ -n "${!k-}" ]; then echo "OK: $k is set"; else echo "MISSING: $k"; fi
done
echo
