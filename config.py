"""config.py — Central configuration (loaded from .env)"""

import os
from dotenv import load_dotenv

load_dotenv(override=True)

# ── OSCAR Pro ────────────────────────────────────────────────
OSCAR_URL       = os.getenv("OSCAR_URL", "")
OSCAR_USERNAME  = os.getenv("OSCAR_USERNAME", "")
OSCAR_PASSWORD  = os.getenv("OSCAR_PASSWORD", "")

# ── Doctor ───────────────────────────────────────────────────
DOCTOR_LAST_NAME    = os.getenv("DOCTOR_LAST_NAME", "")
DOCTOR_FIRST_NAME   = os.getenv("DOCTOR_FIRST_NAME", "")
DOCTOR_BILLING_NUM  = os.getenv("DOCTOR_BILLING_NUMBER", "")

# ── Local AI ─────────────────────────────────────────────────
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
OLLAMA_URL   = os.getenv("OLLAMA_URL", "http://localhost:11434")

WHISPER_MODEL = os.getenv("WHISPER_MODEL", "medium.en")

# ── Vocabulary / correction ──────────────────────────────────
ENABLE_TRANSCRIPT_CORRECTION = os.getenv("ENABLE_TRANSCRIPT_CORRECTION", "1") not in ("0", "false", "False", "")
ENABLE_LLM_GLOSSARY          = os.getenv("ENABLE_LLM_GLOSSARY",          "1") not in ("0", "false", "False", "")

# ── Server ───────────────────────────────────────────────────
PORT = int(os.getenv("PORT", 5001))

# ── Paths ────────────────────────────────────────────────────
BASE_DIR     = os.path.dirname(__file__)
AUDIO_DIR    = os.path.join(BASE_DIR, "sessions", "audio")
SESSION_DIR  = os.path.join(BASE_DIR, "sessions")

os.makedirs(AUDIO_DIR, exist_ok=True)
os.makedirs(SESSION_DIR, exist_ok=True)
