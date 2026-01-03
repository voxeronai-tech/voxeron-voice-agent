#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

echo "==> Fixing pytest imports (make src importable)"

# 1) Make sure src is treated as a package
mkdir -p src/api tests

# Create __init__.py only if missing (don't overwrite)
[[ -f src/__init__.py ]] || echo "# package" > src/__init__.py
[[ -f src/api/__init__.py ]] || echo "# package" > src/api/__init__.py

# 2) Add pytest.ini to put repo root on python path
cat > pytest.ini <<'INI'
[pytest]
pythonpath = .
testpaths = tests
addopts = -ra
INI

# 3) Extra safety: conftest inserts repo root into sys.path (harmless even if pythonpath works)
cat > tests/conftest.py <<'PY'
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
PY

echo "✅ Done."
echo "Now run: pytest -q tests/test_tenant_manager_core.py"
