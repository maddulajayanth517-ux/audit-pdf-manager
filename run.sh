#!/usr/bin/env sh
# Start the Audit PDF Volume & Annexure Manager on http://127.0.0.1:8000
cd "$(dirname "$0")"
if [ ! -x .venv/bin/python ]; then
  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt || exit 1
fi
exec .venv/bin/python -m uvicorn backend.app.main:app --host 127.0.0.1 --port 8000
