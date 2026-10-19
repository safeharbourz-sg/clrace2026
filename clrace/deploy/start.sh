#!/usr/bin/env bash
# Start CL Race 2026 backend (maxmini-user, port 8013, bind 127.0.0.1)
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x backend/.venv/bin/python ]; then
  uv venv backend/.venv --python 3.11
  uv pip install --python backend/.venv/bin/python -r backend/requirements.txt
fi
export PYTHONPATH=
exec backend/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8013
