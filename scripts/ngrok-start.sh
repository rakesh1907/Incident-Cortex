#!/usr/bin/env bash
# Start ngrok using NGROK_AUTHTOKEN and NGROK_PORT from project .env (same file as the app).
# Requires: ngrok CLI (brew install ngrok/ngrok/ngrok) and python-dotenv (project .venv).
# Usage:  cd project && bash scripts/ngrok-start.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

pick_python() {
  if [[ -x "$ROOT/.venv/bin/python" ]]; then
    echo "$ROOT/.venv/bin/python"
  elif command -v python3 &>/dev/null; then
    command -v python3
  else
    command -v python
  fi
}

PY="$(pick_python)"
if [[ -z "$PY" || ! -x "$PY" ]]; then
  echo "Python 3 not found. Create .venv: bash scripts/bootstrap.sh"
  exit 1
fi

if ! "$PY" -c "import dotenv" 2>/dev/null; then
  echo "Missing python-dotenv. Run: pip install -r requirements.txt (or bash scripts/bootstrap.sh)"
  exit 1
fi

export _NGROK_ENV_ROOT="$ROOT"
TOKEN=$("$PY" -c "
import os
from pathlib import Path
from dotenv import dotenv_values
v = dotenv_values(Path(os.environ['_NGROK_ENV_ROOT']) / '.env')
print((v.get('NGROK_AUTHTOKEN') or '').strip())
")
PORT=$("$PY" -c "
import os
from pathlib import Path
from dotenv import dotenv_values
v = dotenv_values(Path(os.environ['_NGROK_ENV_ROOT']) / '.env')
print((v.get('NGROK_PORT') or '8000').strip())
")
unset _NGROK_ENV_ROOT

if [[ -z "$TOKEN" ]]; then
  echo "NGROK_AUTHTOKEN is empty in .env"
  echo "Add it from https://dashboard.ngrok.com/get-started/your-authtoken"
  exit 1
fi

if ! command -v ngrok &>/dev/null; then
  echo "ngrok not found. macOS: brew install ngrok/ngrok/ngrok"
  exit 1
fi

echo "Forwarding http://127.0.0.1:${PORT} → use the https URL below for Slack Event & Interactivity URLs."
exec ngrok http "$PORT" --authtoken="$TOKEN"
