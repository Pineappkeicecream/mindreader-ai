#!/usr/bin/env bash
set -euo pipefail

cd "$(dirname "$0")"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! "$PYTHON_BIN" - <<'PY' >/dev/null 2>&1
import fastapi, openai, uvicorn
PY
then
  if [ ! -d ".venv" ]; then
    "$PYTHON_BIN" -m venv .venv
  fi
  # shellcheck disable=SC1091
  source .venv/bin/activate
  python -m pip install -r requirements.txt
else
  if [ -d ".venv" ]; then
    # shellcheck disable=SC1091
    source .venv/bin/activate
  fi
fi

echo "MindReader AI running at http://127.0.0.1:7777"
python -m uvicorn server:app --host 127.0.0.1 --port 7777
