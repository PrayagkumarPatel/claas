#!/usr/bin/env bash
# ============================================================================
# CLaaS — one-command launcher (macOS / Linux)
#
# Solves the Homebrew Python "externally-managed-environment" error by using
# a project-local virtual environment (.venv), created automatically on the
# first run. Dependencies are (re)installed automatically whenever
# requirements.txt changes.
#
# Usage:
#   ./run.sh              # start the marketplace on http://localhost:8000
#   PORT=9000 ./run.sh    # ... or on a custom port
#
# First run:  creates .venv + installs dependencies (takes ~a minute)
# Later runs: skips straight to starting the server
# ============================================================================
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8000}"
VENV=".venv"

# 1) Create the virtual environment on first run (this is the step that
#    avoids Homebrew's PEP 668 "externally managed" error).
if [ ! -d "$VENV" ]; then
  echo "▶ Creating virtual environment in $VENV ..."
  python3 -m venv "$VENV"
fi

# 2) Activate it (all python/pip calls below are now isolated to .venv).
# shellcheck disable=SC1091
source "$VENV/bin/activate"

# 3) Install dependencies if missing or if requirements.txt changed.
if [ ! -f "$VENV/.deps-installed" ] || [ requirements.txt -nt "$VENV/.deps-installed" ]; then
  echo "▶ Installing dependencies (first run or requirements changed) ..."
  python -m pip install --quiet --upgrade pip
  python -m pip install --quiet -r requirements.txt
  touch "$VENV/.deps-installed"
fi

echo "▶ CLaaS marketplace starting on http://localhost:${PORT}"
echo "  (web UI on /   ·   API docs on /docs   ·   Ctrl+C to stop)"
exec python -m uvicorn api:app --host 0.0.0.0 --port "$PORT"
