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
echo "  Open http://localhost:5000 in your browser"
echo ""

# Use /usr/bin/python3 explicitly — it has all packages installed
/usr/bin/python3 main.py
