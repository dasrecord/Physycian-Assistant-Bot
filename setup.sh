#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════
#  Physician Assistant Bot — First-time setup  (macOS / Linux)
#  Run:  bash setup.sh
# ══════════════════════════════════════════════════════════════════
set -e

echo ""
echo "  ================================================="
echo "   PhysAI — Physician Assistant Bot  (Mac/Linux)"
echo "  ================================================="
echo ""

# ── 1. Python check ──────────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
  echo "[ERROR] python3 not found."
  echo "  macOS:  brew install python  (or download from python.org)"
  echo "  Ubuntu: sudo apt install python3 python3-pip python3-venv"
  exit 1
fi
echo "[OK] $(python3 --version)"

# ── 2. Virtual environment ───────────────────────────────────────
if [ ! -d ".venv" ]; then
  echo "[1/5] Creating virtual environment..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
echo "[OK] venv activated"

# ── 3. Python dependencies ───────────────────────────────────────
echo "[2/5] Installing Python packages..."
pip install --upgrade pip -q
pip install -r requirements.txt

# ── 4. ffmpeg check ──────────────────────────────────────────────
echo ""
echo "[3/5] Checking ffmpeg..."
if command -v ffmpeg &>/dev/null; then
  echo "[OK] ffmpeg found: $(ffmpeg -version 2>&1 | head -1)"
else
  echo "  ffmpeg not found — attempting install..."
  if command -v brew &>/dev/null; then
    brew install ffmpeg
  elif command -v apt-get &>/dev/null; then
    sudo apt-get install -y ffmpeg
  else
    echo "  [WARN] Could not auto-install ffmpeg."
    echo "  macOS:  brew install ffmpeg"
    echo "  Ubuntu: sudo apt install ffmpeg"
  fi
fi

# ── 5. Ollama install + model pull ───────────────────────────────
echo ""
echo "[4/5] Checking Ollama..."
if ! command -v ollama &>/dev/null; then
  echo "  Ollama not found — installing..."
  curl -fsSL https://ollama.com/install.sh | sh
fi
echo "[OK] $(ollama --version)"

OLLAMA_MODEL="${OLLAMA_MODEL:-llama3.2:3b}"
echo ""
echo "[5/5] Pulling model: $OLLAMA_MODEL  (skip if already downloaded)"
ollama pull "$OLLAMA_MODEL"

# ── 6. .env setup ────────────────────────────────────────────────
echo ""
if [ ! -f ".env" ]; then
  cp .env.example .env
  echo "[OK] Created .env from .env.example — edit it to add your OSCAR credentials."
else
  echo "[OK] .env already exists."
fi

echo ""
echo "  ================================================="
echo "   Setup complete!"
echo "   Start the server:  bash start.sh"
echo "   Then open:         http://localhost:5000"
echo "  ================================================="
echo ""
