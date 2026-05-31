@echo off
REM ══════════════════════════════════════════════════════════════════
REM  MedScribe — First-time setup  (run once as Administrator)
REM ══════════════════════════════════════════════════════════════════

echo.
echo  ===================================================
echo   MedScribe — Physician Assistant Bot Setup
echo  ===================================================
echo.

REM ── 1. Python check ───────────────────────────────────────────────
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found. Install from https://python.org
    pause & exit /b 1
)

REM ── 2. Install Python dependencies ───────────────────────────────
echo [1/4] Installing Python packages...
pip install -r requirements.txt
if errorlevel 1 ( echo [ERROR] pip install failed. & pause & exit /b 1 )

REM ── 3. ffmpeg check ───────────────────────────────────────────────
echo.
echo [2/4] Checking ffmpeg (needed for audio conversion)...
ffmpeg -version >nul 2>&1
if errorlevel 1 (
    echo  ffmpeg not found. Attempting install via winget...
    winget install ffmpeg --silent
    if errorlevel 1 (
        echo  [WARN] winget install failed.
        echo  Please install ffmpeg manually:
        echo    1. Download from https://ffmpeg.org/download.html
        echo    2. Extract and add the bin folder to your PATH
    ) else (
        echo  [OK] ffmpeg installed.
    )
) else (
    echo  [OK] ffmpeg found.
)

REM ── 4. Ollama install + model pull ───────────────────────────────
echo.
echo [3/4] Checking Ollama (local LLM engine)...

REM Ollama may be installed but not yet on PATH in this shell session
set OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe
where ollama >nul 2>&1
if not errorlevel 1 ( set OLLAMA_EXE=ollama )

"%OLLAMA_EXE%" --version >nul 2>&1
if errorlevel 1 (
    echo  Ollama not found. Installing automatically...
    powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://ollama.com/install.ps1 | iex"
    if errorlevel 1 (
        echo  [ERROR] Ollama auto-install failed.
        echo  Please install manually from: https://ollama.com/download/windows
        echo  Then re-run this setup script.
        pause & exit /b 1
    )
    echo  [OK] Ollama installed.
    REM Refresh path after install
    set OLLAMA_EXE=%LOCALAPPDATA%\Programs\Ollama\ollama.exe
) else (
    echo  [OK] Ollama already installed.
)

echo  Pulling llama3.2:3b model (downloads ~2 GB — skipped if already present)...
"%OLLAMA_EXE%" pull llama3.2:3b
if errorlevel 1 (
    echo  [WARN] Model pull failed. Run manually:  ollama pull llama3.1:8b
) else (
    echo  [OK] Model ready.
)

REM ── 5. .env setup ────────────────────────────────────────────────
echo.
echo [4/4] Checking .env configuration...
if not exist .env (
    copy .env.example .env
    echo  [OK] Created .env from template.
    echo  *** IMPORTANT: Edit .env and fill in your OSCAR Pro URL and credentials ***
) else (
    echo  [OK] .env already exists.
)

echo.
echo  ===================================================
echo   Setup complete!
echo.
echo   Next steps:
echo     1. Edit .env with your OSCAR Pro URL + credentials
echo     2. Run:  python main.py
echo     3. Open: http://localhost:5001
echo  ===================================================
echo.
pause
