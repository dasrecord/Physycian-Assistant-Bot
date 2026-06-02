#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  Physician Assistant Bot — Start server  (macOS / Linux)
#  Run:  bash start.sh
# ══════════════════════════════════════════════════════════════════
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Activate venv if present
if [ -d ".venv" ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

# Start Ollama in the background if not already running
if command -v ollama &>/dev/null; then
  if ! pgrep -x "ollama" > /dev/null 2>&1; then
    echo "Starting Ollama in background..."
    ollama serve &>/dev/null &
    sleep 2
  fi
fi

echo ""
echo "  Starting PhysAI..."
echo "  Open http://localhost:5001 in your browser"
echo ""

# Use venv python (project requires Python 3.12; system /usr/bin/python3 is 3.9)
if [ -x ".venv/bin/python" ]; then
  .venv/bin/python main.py
else
  python3 main.py
fi
