#!/usr/bin/env bash
# One-time local setup: Python venv + dependencies + .env from template.
# Usage (macOS): from project root —  bash scripts/bootstrap.sh
# With Homebrew Python:  PYTHON=python3.12 bash scripts/bootstrap.sh

set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

PY="${PYTHON:-python3}"
if ! command -v "$PY" &>/dev/null; then
  echo "Need python3 on PATH (or set PYTHON=/path/to/python3)."
  exit 1
fi

if [[ ! -d .venv ]]; then
  echo "Creating .venv ..."
  "$PY" -m venv .venv
fi

# shellcheck disable=SC1091
source .venv/bin/activate
python -m pip install -U pip -q
pip install -r requirements.txt

if [[ ! -f .env ]]; then
  if [[ -f .env.example ]]; then
    cp .env.example .env
    echo "Created .env from .env.example — edit it with your tokens before running."
  else
    echo "Warning: no .env.example found; create .env manually."
  fi
else
  echo ".env already exists; left unchanged."
fi

echo ""
echo "Next steps:"
echo "  1. Edit .env with Slack, FireHydrant, Jira, and channel IDs."
echo "  2. Install Ollama and: ollama pull \${OLLAMA_MODEL:-llama3}"
echo "  3. Optional RCCA: ollama pull nomic-embed-text && set OLLAMA_EMBED_MODEL in .env"
echo "  4. Run:  source .venv/bin/activate && uvicorn app:app --host 0.0.0.0 --port 8000"
echo "  5. Add NGROK_AUTHTOKEN to .env, then: bash scripts/ngrok-start.sh — paste the https URL into Slack Event/Interactivity URLs."
echo ""
