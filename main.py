"""
main.py -- Physician Assistant Bot
Local Flask/SocketIO server.  Open http://localhost:5000 in any browser.
"""

import os
import re
import sys
import json
import uuid
import threading
from datetime import datetime
from pathlib import Path

import tempfile
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit

from config import PORT, AUDIO_DIR, SESSION_DIR, OLLAMA_MODEL, OLLAMA_URL
from transcription.stt import WhisperTranscriber
from llm.soap_generator import SOAPGenerator
from emr.oscar import OscarEMR

NOTES_DIR = os.path.join(SESSION_DIR, "notes")
os.makedirs(NOTES_DIR, exist_ok=True)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(32)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
                    ping_timeout=120, ping_interval=60,
                    max_http_buffer_size=50 * 1024 * 1024)

_stt = None
_soap = None
_oscar = None

session: dict = {}

TEMPLATES_FILE = Path(__file__).parent / "note_templates.json"


def get_stt():
    global _stt
    if _stt is None:
        socketio.emit("status", {"msg": "Loading Whisper model (first use)...", "level": "info"}, to="/", namespace="/")
        _stt = WhisperTranscriber()
        socketio.emit("status", {"msg": "Whisper model ready.", "level": "success"}, to="/", namespace="/")
    return _stt


def get_soap():
    global _soap
    if _soap is None:
        _soap = SOAPGenerator()
    return _soap


def get_oscar():
    global _oscar
    if _oscar is None:
        _oscar = OscarEMR()
    return _oscar


def _recommend_model():
    try:
        import psutil
        total_gb  = psutil.virtual_memory().total     / (1024 ** 3)
        avail_gb  = psutil.virtual_memory().available / (1024 ** 3)
        # Use total unified memory as the cap so we don't over-recommend
        # models that won't fit (e.g. 70b needs ~48 GB)
        cap = min(total_gb, avail_gb * 1.5)
        if cap < 4:
            return "llama3.2:3b"
        elif cap < 12:
            return "llama3.1:8b"
        elif cap < 48:
            return "llama3.2:11b"
        else:
            return "llama3.1:70b"
    except ImportError:
        return OLLAMA_MODEL


def _get_available_gb():
    try:
        import psutil
        return round(psutil.virtual_memory().available / (1024 ** 3), 1)
    except ImportError:
        return None


# ── Note history ─────────────────────────────────────────────────────────────

def _save_note(sess, note):
    """Persist a completed note to NOTES_DIR as JSON."""
    try:
        safe_name = sess.get("patient_name", "unknown").replace(" ", "_")[:20]
        ts = datetime.now().isoformat()
        fname = f"{ts[:10]}_{sess.get('id','x')}_{safe_name}.json"
        entry = {
            "session_id":    sess.get("id", ""),
            "patient_name":  sess.get("patient_name", ""),
            "patient_dob":   sess.get("patient_dob", ""),
            "health_card":   sess.get("health_card", ""),
            "visit_type":    sess.get("visit_type", "standard"),
            "timestamp":     ts,
            "soap_note":     note,
            "transcript":    sess.get("transcript", ""),
            "audio_filename": os.path.basename(sess.get("audio_path", "") or ""),
        }
        with open(os.path.join(NOTES_DIR, fname), "w", encoding="utf-8") as f:
            json.dump(entry, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        print(f"[history] auto-save failed: {exc}")


def _note_summary(soap_note: dict) -> str:
    """Extract a one-liner case summary from a soap_note dict."""
    if not isinstance(soap_note, dict):
        return ""
    for field in ("assessment", "subjective"):
        text = soap_note.get(field, "")
        if not text:
            continue
        for raw_line in text.splitlines():
            line = raw_line.strip().lstrip("-•*# ").strip()
            if not line or len(line) < 4:
                continue
            # Skip DDx / header lines
            if re.match(r'^(ddx|differential|assessment|plan|s:|o:|a:|p:)', line, re.I):
                continue
            # Clean trailing ICD code parenthetical for brevity if line is long
            clean = re.sub(r'\s*\(ICD-?9?:?\s*[\d.,\s]+\)', '', line).strip().rstrip(',:;')
            return (clean or line)[:72]
    return ""


@app.route("/api/history", methods=["GET"])
def get_history():
    """Return saved notes. ?date=YYYY-MM-DD (default today) or ?date=all"""
    date_filter = request.args.get("date", datetime.now().strftime("%Y-%m-%d"))
    entries = []
    try:
        for fname in sorted(os.listdir(NOTES_DIR), reverse=True):
            if not fname.endswith(".json"):
                continue
            if date_filter != "all" and not fname.startswith(date_filter):
                continue
            with open(os.path.join(NOTES_DIR, fname), encoding="utf-8") as f:
                data = json.load(f)
            soap = data.get("soap_note", {})
            entries.append({
                "session_id":   data.get("session_id", ""),
                "patient_name": data.get("patient_name") or "",
                "timestamp":    data.get("timestamp", ""),
                "icd9_codes":   soap.get("icd9_codes", []),
                "visit_type":   data.get("visit_type", ""),
                "summary":      _note_summary(soap),
                "filename":     fname,
            })
    except Exception as exc:
        print(f"[history] list failed: {exc}")
    return jsonify(entries)


@app.route("/api/history/<path:filename>", methods=["GET"])
def get_history_note(filename):
    """Return a specific saved note by filename."""
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    fpath = os.path.join(NOTES_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "Not found"}), 404
    with open(fpath, encoding="utf-8") as f:
        return jsonify(json.load(f))


@app.route("/api/history/<path:filename>", methods=["DELETE"])
def delete_history_note(filename):
    """Delete a saved note."""
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    fpath = os.path.join(NOTES_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "Not found"}), 404
    os.remove(fpath)
    return jsonify({"status": "deleted"})


@app.route("/api/history/<path:filename>", methods=["PATCH"])
def update_history_note(filename):
    """Update editable fields (patient_name, patient_dob, health_card, visit_type) in a saved note."""
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    fpath = os.path.join(NOTES_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "Not found"}), 404
    updates = request.get_json(force=True)
    with open(fpath, encoding="utf-8") as f:
        note = json.load(f)
    for field in ("patient_name", "patient_dob", "health_card", "visit_type"):
        if field in updates:
            note[field] = str(updates[field]).strip()
    with open(fpath, "w", encoding="utf-8") as f:
        json.dump(note, f, ensure_ascii=False, indent=2)
    return jsonify({"status": "updated"})


@app.route("/api/update-session", methods=["POST"])
def update_session_fields():
    """Update patient fields in the active session without clearing transcript/audio."""
    data = request.get_json(force=True)
    for field in ("patient_name", "patient_dob", "health_card", "visit_type"):
        if data.get(field) is not None:
            session[field] = str(data[field]).strip()
    return jsonify({"status": "ok"})


@app.route("/api/audio/latest", methods=["GET"])
def get_latest_audio():
    """Return the most recently saved session audio file (any date)."""
    try:
        files = [
            f for f in os.listdir(AUDIO_DIR)
            if f.startswith("session_") and f.endswith(".webm")
        ]
        if not files:
            return jsonify({"filename": None})
        files.sort(key=lambda f: os.path.getmtime(os.path.join(AUDIO_DIR, f)), reverse=True)
        return jsonify({"filename": files[0]})
    except Exception as exc:
        return jsonify({"filename": None, "error": str(exc)})


@app.route("/api/audio/<path:filename>", methods=["GET"])
def get_audio(filename):
    """Serve a saved audio file by name."""
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    fpath = os.path.join(AUDIO_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "Not found"}), 404
    return send_file(fpath, mimetype="audio/webm")


@app.route("/api/retranscribe", methods=["POST"])
def retranscribe():
    """Re-run transcription on a saved audio file (no blob upload needed)."""
    data = request.get_json(force=True)
    filename = data.get("filename", "")
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    fpath = os.path.join(AUDIO_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "Audio file not found"}), 404
    session["audio_path"] = fpath

    diarize = data.get("diarize", True)

    _TRANSCRIBE_TIMEOUT = 300  # 5 minutes max

    def _redo():
        socketio.emit("status", {"msg": "Re-transcribing audio...", "level": "info"})
        result = {}
        def _work():
            try:
                result["transcript"] = get_stt().transcribe(fpath, diarize=diarize)
            except Exception as exc:
                result["error"] = str(exc)
        t = threading.Thread(target=_work, daemon=True)
        t.start()
        t.join(timeout=_TRANSCRIBE_TIMEOUT)
        if t.is_alive():
            socketio.emit("status", {"msg": "Transcription timed out (audio may be corrupted).", "level": "error"})
            return
        if "error" in result:
            socketio.emit("status", {"msg": f"Transcription error: {result['error']}", "level": "error"})
            return
        session["transcript"] = result["transcript"]
        socketio.emit("transcript_ready", {"transcript": result["transcript"]})
        socketio.emit("status", {"msg": "Transcription complete.", "level": "success"})

    threading.Thread(target=_redo, daemon=True).start()
    return jsonify({"status": "transcribing"})


def _load_templates():
    if TEMPLATES_FILE.exists():
        with open(TEMPLATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def _save_templates(templates):
    with open(TEMPLATES_FILE, "w", encoding="utf-8") as f:
        json.dump(templates, f, indent=2, ensure_ascii=False)


# ── HTTP routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/start-session", methods=["POST"])
def start_session():
    data = request.get_json(force=True)
    session.clear()
    session.update({
        "id":            str(uuid.uuid4())[:8],
        "patient_name":  data.get("patient_name", "").strip(),
        "patient_dob":   data.get("patient_dob", "").strip(),
        "health_card":   data.get("health_card", "").strip(),
        "visit_type":    data.get("visit_type", "standard"),
        "start_time":    datetime.now().isoformat(),
        "transcript":    "",
        "soap_note":     None,
        "audio_path":    None,
        "chunks":        [],
    })
    return jsonify({"status": "ok", "session_id": session["id"]})


@app.route("/api/audio-chunk", methods=["POST"])
def audio_chunk():
    if not session:
        return jsonify({"error": "No active session"}), 400
    session["chunks"].append(request.get_data())
    return jsonify({"received": len(session["chunks"][-1]), "total_chunks": len(session["chunks"])})


@app.route("/api/transcribe-chunk", methods=["POST"])
def transcribe_chunk():
    if not session:
        return jsonify({"error": "No active session"}), 400
    audio_data = request.get_data()
    if not audio_data:
        return jsonify({"error": "No audio data"}), 400

    def _live():
        # Use a temp file so live chunks don't create permanent duplicates
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=".webm")
        try:
            with os.fdopen(tmp_fd, "wb") as f:
                f.write(audio_data)
            transcript = get_stt().transcribe(tmp_path)
            session["transcript"] = transcript
            socketio.emit("partial_transcript", {"transcript": transcript})
        except Exception as exc:
            socketio.emit("status", {"msg": f"Live transcription: {exc}", "level": "warn"})
        finally:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    threading.Thread(target=_live, daemon=True).start()
    return jsonify({"status": "transcribing"})


@app.route("/api/stop-recording", methods=["POST"])
def stop_recording():
    # Primary path: frontend sends the full audio blob in the request body
    no_diarize = request.args.get("no_diarize") == "1"
    body = request.get_data()
    if body and len(body) > 512:
        sid = session.get("id", "x")
        fname = f"session_{sid}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.webm"
        audio_path = os.path.join(AUDIO_DIR, fname)
        os.makedirs(AUDIO_DIR, exist_ok=True)
        with open(audio_path, "wb") as f:
            f.write(body)
        session["audio_path"] = audio_path
        session["chunks"] = []

        def _final():
            socketio.emit("status", {"msg": "Transcribing audio...", "level": "info"})
            try:
                transcript = get_stt().transcribe(audio_path, diarize=not no_diarize)
                session["transcript"] = transcript
                socketio.emit("transcript_ready", {"transcript": transcript})
                socketio.emit("status", {"msg": "Transcription complete.", "level": "success"})
            except Exception as exc:
                socketio.emit("status", {"msg": f"Transcription error: {exc}", "level": "error"})

        threading.Thread(target=_final, daemon=True).start()
        return jsonify({"status": "transcribing", "audio_path": fname})

    # Fallback: chunks accumulated via /api/audio-chunk
    if session.get("chunks"):
        fname = f"session_{session['id']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.webm"
        audio_path = os.path.join(AUDIO_DIR, fname)
        os.makedirs(AUDIO_DIR, exist_ok=True)
        with open(audio_path, "wb") as f:
            for chunk in session["chunks"]:
                f.write(chunk)
        session["audio_path"] = audio_path
        session["chunks"] = []

        def _final_chunks():
            socketio.emit("status", {"msg": "Transcribing audio...", "level": "info"})
            try:
                transcript = get_stt().transcribe(audio_path)
                session["transcript"] = transcript
                socketio.emit("transcript_ready", {"transcript": transcript})
                socketio.emit("status", {"msg": "Transcription complete.", "level": "success"})
            except Exception as exc:
                socketio.emit("status", {"msg": f"Transcription error: {exc}", "level": "error"})

        threading.Thread(target=_final_chunks, daemon=True).start()
        return jsonify({"status": "transcribing", "audio_path": fname})

    # If we already have a transcript from live transcription, use it
    if session.get("transcript"):
        socketio.emit("transcript_ready", {"transcript": session["transcript"]})
        socketio.emit("status", {"msg": "Transcription complete.", "level": "success"})
        return jsonify({"status": "done"})

    return jsonify({"error": "No audio recorded"}), 400


_generating = False


@app.route("/api/generate-note", methods=["POST"])
def generate_note():
    global _generating
    if _generating:
        return jsonify({"error": "Generation already in progress"}), 429

    data = request.get_json(force=True)
    transcript = data.get("transcript") or session.get("transcript", "")
    template_id = data.get("template_id", "standard")
    target_sid = data.get("socket_id") or None  # emit only to requesting client
    patient_supplied_info = data.get("patient_submitted_info", "")
    if not transcript.strip():
        return jsonify({"error": "No transcript available"}), 400

    # Prefer patient fields sent by the client (always current DOM values)
    for field in ("patient_name", "patient_dob", "health_card", "visit_type"):
        if data.get(field) is not None:
            session[field] = str(data[field]).strip()

    # --- Pre-process: extract and merge key fields from patient-supplied info into transcript ---
    def extract_field(text, field_names):
        for name in field_names:
            pattern = rf"{name}[:\s\-]*([^\n]*)"
            match = re.search(pattern, text, re.IGNORECASE)
            if match:
                return match.group(1).strip()
        return None

    merged_transcript = transcript
    if patient_supplied_info:
        # Allergies
        allergies = extract_field(patient_supplied_info, ["Allergies", "Medication Allergies"])
        if allergies and ("allerg" not in transcript.lower() or "not reported" in transcript.lower()):
            merged_transcript += f"\nPatient reports allergies: {allergies}"
        # Medications
        meds = extract_field(patient_supplied_info, ["Medications", "Current medications"])
        if meds and ("medication" not in transcript.lower() or "not reported" in transcript.lower()):
            merged_transcript += f"\nPatient reports medications: {meds}"
        # PMHx
        pmhx = extract_field(patient_supplied_info, ["Past Medical History", "PMHx"])
        if pmhx and ("pmhx" not in transcript.lower() or "not reported" in transcript.lower()):
            merged_transcript += f"\nPatient reports PMHx: {pmhx}"
        # SHx
        shx = extract_field(patient_supplied_info, ["Social History", "SHx"])
        if shx and ("shx" not in transcript.lower() or "not reported" in transcript.lower()):
            merged_transcript += f"\nPatient reports SHx: {shx}"

    _generating = True

    def _generate():
        global _generating
        socketio.emit("status", {"msg": "Generating SOAP note via local AI...", "level": "info"}, to=target_sid)
        try:
            template_config = None
            templates = _load_templates()
            if template_id:
                template_config = next((t for t in templates if t["id"] == template_id), None)
                if template_config and not template_config.get("output_format"):
                    template_config = None

            raw_tokens = []
            buf = []
            for token in get_soap().generate_streaming(
                transcript=merged_transcript,
                patient_name=session.get("patient_name", ""),
                template_config=template_config,
                patient_submitted_info=patient_supplied_info,
            ):
                raw_tokens.append(token)
                buf.append(token)
                if len(buf) >= 8:  # batch tokens to reduce socket overhead
                    socketio.emit("note_streaming", {"token": "".join(buf)}, to=target_sid)
                    buf.clear()
            if buf:
                socketio.emit("note_streaming", {"token": "".join(buf)}, to=target_sid)

            raw = "".join(raw_tokens)
            note = get_soap()._parse(raw)
            session["soap_note"] = note
            _save_note(session.copy(), note)
            socketio.emit("note_ready", note, to=target_sid)
            socketio.emit("status", {"msg": "Note generated. Review, edit, then post.", "level": "success"}, to=target_sid)
        except Exception as exc:
            socketio.emit("status", {"msg": f"Note generation error: {exc}", "level": "error"}, to=target_sid)
        finally:
            _generating = False

    threading.Thread(target=_generate, daemon=True).start()
    return jsonify({"status": "generating"})


@app.route("/api/post-to-oscar", methods=["POST"])
def post_to_oscar():
    data = request.get_json(force=True)
    note    = data.get("soap_note")    or session.get("soap_note")
    pt_name = data.get("patient_name") or session.get("patient_name", "")
    pt_dob  = data.get("patient_dob")  or session.get("patient_dob",  "")
    if not note:
        return jsonify({"error": "No note to post"}), 400

    def _post():
        socketio.emit("status", {"msg": "Opening OSCAR Pro...", "level": "info"})
        try:
            result = get_oscar().post_note(
                patient_name=pt_name, patient_dob=pt_dob, soap_note=note)
            socketio.emit("oscar_result", result)
            socketio.emit("status", {
                "msg": "Note posted to OSCAR Pro." if result["success"] else f"OSCAR error: {result.get('error')}",
                "level": "success" if result["success"] else "error",
            })
        except Exception as exc:
            socketio.emit("status", {"msg": f"OSCAR posting failed: {exc}", "level": "error"})

    threading.Thread(target=_post, daemon=True).start()
    return jsonify({"status": "posting"})


@app.route("/api/submit-billing", methods=["POST"])
def submit_billing():
    data       = request.get_json(force=True)
    note       = data.get("soap_note") or session.get("soap_note", {})
    icd9       = note.get("icd9_codes", []) if isinstance(note, dict) else []
    pt_name    = data.get("patient_name") or session.get("patient_name", "")
    pt_dob     = data.get("patient_dob")  or session.get("patient_dob",  "")
    hc         = data.get("health_card")  or session.get("health_card",  "")
    visit_type = data.get("visit_type")   or session.get("visit_type", "standard")

    # Derive timing from server session (more reliable than client timer)
    sess_start   = session.get("start_time", "")
    date_svc     = sess_start[:10] if len(sess_start) >= 10 else datetime.now().strftime("%Y-%m-%d")
    start_hm     = sess_start[11:16] if len(sess_start) >= 16 else ""   # "HH:MM"
    end_hm       = datetime.now().strftime("%H:%M")

    def _bill():
        socketio.emit("status", {"msg": "Submitting billing...", "level": "info"})
        try:
            from billing import submit_billing as do_billing
            result = do_billing(
                patient_name=pt_name,
                patient_dob=pt_dob,
                health_card=hc,
                icd9_codes=icd9,
                visit_type=visit_type,
                date_of_service=date_svc,
                start_time=start_hm,
                end_time=end_hm,
            )
            socketio.emit("billing_result", result)
            socketio.emit("status", {"msg": "Billing submitted.", "level": "success"})
        except Exception as exc:
            socketio.emit("status", {"msg": f"Billing error: {exc}", "level": "error"})

    threading.Thread(target=_bill, daemon=True).start()
    return jsonify({"status": "billing"})


@app.route("/api/health", methods=["GET"])
def health():
    import requests as _r
    ollama_ok, available_models = False, []
    try:
        r = _r.get(f"{OLLAMA_URL}/api/tags", timeout=3)
        if r.ok:
            ollama_ok = True
            available_models = [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    current_model = _soap.model if _soap else OLLAMA_MODEL
    return jsonify({
        "whisper":           "ready" if _stt else "not_loaded",
        "ollama":            "ok" if ollama_ok else "offline",
        "current_model":     current_model,
        "recommended_model": _recommend_model(),
        "available_models":  available_models,
        "available_gb":      _get_available_gb(),
    })


@app.route("/api/set-model", methods=["POST"])
def set_model():
    global _soap
    data = request.get_json(force=True)
    model_name = data.get("model", "").strip()
    if not model_name:
        return jsonify({"error": "No model name provided"}), 400
    _soap = SOAPGenerator(model=model_name)
    # Persist to .env
    env_path = Path(__file__).parent / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    new_lines, found = [], False
    for line in lines:
        if line.startswith("OLLAMA_MODEL="):
            new_lines.append(f"OLLAMA_MODEL={model_name}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"OLLAMA_MODEL={model_name}")
    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    # Start warmup in background
    threading.Thread(target=_soap.warmup, daemon=True).start()
    return jsonify({"status": "ok", "model": model_name})


@app.route("/api/templates", methods=["GET"])
def get_templates():
    return jsonify(_load_templates())


@app.route("/api/templates", methods=["POST"])
def save_template():
    data = request.get_json(force=True)
    required = ["id", "name"]
    if not all(data.get(k) for k in required):
        return jsonify({"error": "id and name are required"}), 400
    templates = _load_templates()
    # Prevent overwriting builtins
    existing = next((t for t in templates if t["id"] == data["id"]), None)
    if existing and existing.get("is_builtin"):
        return jsonify({"error": "Cannot overwrite a built-in template"}), 403
    template = {
        "id":                 data["id"],
        "name":               data["name"],
        "description":        data.get("description", ""),
        "icon":               data.get("icon", "📄"),
        "is_builtin":         False,
        "system_prompt_extra": data.get("system_prompt_extra", ""),
        "output_format":      data.get("output_format", ""),
    }
    templates = [t for t in templates if t["id"] != data["id"]]
    templates.append(template)
    _save_templates(templates)
    return jsonify({"status": "ok", "template": template})


@app.route("/api/templates/<template_id>", methods=["DELETE"])
def delete_template(template_id):
    templates = _load_templates()
    target = next((t for t in templates if t["id"] == template_id), None)
    if not target:
        return jsonify({"error": "Template not found"}), 404
    if target.get("is_builtin"):
        return jsonify({"error": "Cannot delete a built-in template"}), 403
    _save_templates([t for t in templates if t["id"] != template_id])
    return jsonify({"status": "ok"})


@socketio.on("connect")
def on_connect():
    emit("status", {"msg": "Connected to Physician Assistant Bot.", "level": "success"})


if __name__ == "__main__":
    import socket as _sock
    local_ip = _sock.gethostbyname(_sock.gethostname())
    print("=" * 60)
    print("  Physician Assistant Bot")
    print(f"  Local :  http://localhost:{PORT}")
    print(f"  Phone  :  http://{local_ip}:{PORT}")
    print("=" * 60)

    # Warmup Ollama in background so first generation is fast
    def _warmup_delayed():
        import time
        time.sleep(2)  # brief pause so Flask finishes starting
        get_soap().warmup()
    threading.Thread(target=_warmup_delayed, daemon=True).start()

    socketio.run(app, host="0.0.0.0", port=PORT, debug=True, use_reloader=True, reloader_type='stat')
