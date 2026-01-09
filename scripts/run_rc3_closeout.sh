#!/usr/bin/env bash
set -euo pipefail

echo "== RC3 closeout checks =="

python -m py_compile src/api/session_controller.py
python -m py_compile src/api/server.py

pytest -q tests/rc3/test_rc3_closeout.py
echo "RC3 closeout suite passed ✅"

