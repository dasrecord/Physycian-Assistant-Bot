"""
main.py -- Physician Assistant Bot
Local Flask/SocketIO server.  Open http://localhost:5000 in any browser.
"""

import os
import sys
import json
import uuid
import threading
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit

from config import PORT, AUDIO_DIR, SESSION_DIR, OLLAMA_MODEL, OLLAMA_URL
from transcription.stt import WhisperTranscriber
from llm.soap_generator import SOAPGenerator
from emr.oscar import OscarEMR

app = Flask(__name__)
app.config["SECRET_KEY"] = os.urandom(32)
socketio = SocketIO(app, cors_allowed_origins="*", async_mode="threading",
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
        gb = psutil.virtual_memory().available / (1024 ** 3)
        if gb < 3.5:
            return "llama3.2:3b"
        elif gb < 10:
            return "llama3.1:8b"
        elif gb < 24:
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
    tmp_path = os.path.join(AUDIO_DIR, f"live_{session.get('id','x')}.webm")
    with open(tmp_path, "wb") as f:
        f.write(audio_data)

    def _live():
        try:
            transcript = get_stt().transcribe(tmp_path)
            session["transcript"] = transcript
            socketio.emit("partial_transcript", {"transcript": transcript})
        except Exception as exc:
            socketio.emit("status", {"msg": f"Live transcription: {exc}", "level": "warn"})

    threading.Thread(target=_live, daemon=True).start()
    return jsonify({"status": "transcribing"})


@app.route("/api/stop-recording", methods=["POST"])
def stop_recording():
    # Primary path: frontend sends the full audio blob in the request body
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
                transcript = get_stt().transcribe(audio_path)
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
    if not transcript.strip():
        return jsonify({"error": "No transcript available"}), 400

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
                transcript=transcript,
                patient_name=session.get("patient_name", ""),
                template_config=template_config,
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
    duration   = data.get("duration_minutes", 0)

    def _bill():
        socketio.emit("status", {"msg": "Submitting billing...", "level": "info"})
        try:
            from billing import submit_billing as do_billing
            result = do_billing(
                patient_name=pt_name, patient_dob=pt_dob, health_card=hc,
                icd9_codes=icd9, visit_type=visit_type, duration_minutes=duration,
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
        time.sleep(6)
        get_soap().warmup()
    threading.Thread(target=_warmup_delayed, daemon=True).start()

    socketio.run(app, host="0.0.0.0", port=PORT, debug=False)
