# PhysAI — Physician Assistant Bot

A local, private AI scribe for physicians. Records patient consultations, transcribes speech with Whisper, and generates structured SOAP notes using a locally-running LLM (Ollama). **No data ever leaves your machine.**

---

## Features

- 🎙️ **Live transcription** via faster-whisper (runs on CPU, no GPU needed)
- 🧠 **SOAP note generation** via Ollama (llama3.2:3b — 2 GB model, fully offline)
- 🔒 **Anti-hallucination rules** — virtual/phone visits explicitly flagged, no invented physical exam findings
- 📋 **Note templates** — Standard, Mental Health, Neurology, Pediatric, Urgent Care
- 📱 **Mobile-friendly** — accessible from your phone on the same WiFi network
- 🏥 **OSCAR Pro integration** — post notes directly to EMR (optional)
- 💰 **Billing assist** — visit type and duration-based billing codes (Ontario)

---

## Quick Start

### macOS / Linux

```bash
git clone https://github.com/YOUR_USERNAME/physai.git
cd physai
bash setup.sh       # one-time setup (installs Ollama, pulls model, creates venv)
bash start.sh       # start the server
# Open http://localhost:5000
```

### Windows

```bat
git clone https://github.com/YOUR_USERNAME/physai.git
cd physai
setup.bat           :: one-time setup
start.bat           :: start the server
:: Open http://localhost:5000
```

---

## Requirements

| Dependency | Notes |
|---|---|
| Python 3.10+ | 3.14 supported |
| [Ollama](https://ollama.com/download) | Local LLM engine |
| ffmpeg | Audio processing (`brew install ffmpeg` / `winget install ffmpeg`) |
| ~3 GB disk | For model weights (auto-downloaded on first run) |
| ~1 GB RAM | llama3.2:3b minimum; 5 GB for llama3.1:8b |

> **Apple Silicon (M1/M2/M3/M4):** Ollama uses Metal GPU automatically → much faster generation than CPU-only Windows.

---

## Configuration

```bash
cp .env.example .env
# Edit .env with your OSCAR URL, credentials, and doctor billing number
```

All settings have working defaults — the app runs without a `.env` file.

Key options in `.env`:

```env
OLLAMA_MODEL=llama3.2:3b      # or llama3.1:8b if you have ≥5 GB RAM
WHISPER_MODEL=medium.en        # tiny.en → large-v3 (tradeoff: speed vs accuracy)
PORT=5000
```

---

## Speed Guide

| Hardware | Tokens/sec | Note generation time |
|---|---|---|
| CPU only (Windows/Mac Intel) | 5–15 t/s | ~60–90 seconds |
| Apple M1/M2/M3 (Metal) | 30–60 t/s | ~15–25 seconds |
| NVIDIA RTX 3060+ (CUDA) | 60–120 t/s | ~8–15 seconds |

---

## Do I need to copy the model when switching machines?

**No.** Ollama models are not tied to any machine. On each new machine:

1. Install Ollama from https://ollama.com/download
2. Run `ollama pull llama3.2:3b` — it downloads fresh (~2 GB)

Alternatively, if you want to avoid re-downloading, the model weights are stored at:
- **macOS/Linux:** `~/.ollama/models/`
- **Windows:** `%USERPROFILE%\.ollama\models\`

You can copy that folder between machines and Ollama will use the cached weights.

---

## Privacy

- All audio and transcriptions are processed **100% locally**
- `sessions/audio/` is in `.gitignore` — patient recordings are never committed
- OSCAR credentials are in `.env` — never committed
- No telemetry, no cloud API calls

---

## Project Structure

```
physai/
├── main.py               Flask server + all routes
├── config.py             Config loaded from .env
├── note_templates.json   Built-in + custom SOAP templates
├── llm/
│   ├── soap_generator.py Ollama API client + streaming + parser
│   └── prompts.py        System prompt + anti-hallucination rules
├── transcription/
│   └── stt.py            faster-whisper transcriber
├── emr/
│   └── oscar.py          OSCAR Pro browser automation
├── billing/
│   └── __init__.py       Billing code logic
├── static/
│   ├── css/style.css
│   └── js/app.js
├── templates/
│   └── index.html
├── setup.bat / setup.sh  First-time setup
└── start.bat / start.sh  Start the server
```
