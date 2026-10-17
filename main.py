"""
main.py -- Physician Assistant Bot
Local Flask/SocketIO server.  Open http://localhost:5001 in any browser.
"""

import os
import re
import sys
import json
import uuid
import threading
import stat
from datetime import datetime
from pathlib import Path
from zipfile import BadZipFile

import tempfile
from flask import Flask, render_template, request, jsonify, send_file
from flask_socketio import SocketIO, emit
from openpyxl import load_workbook
from openpyxl.utils.exceptions import InvalidFileException

from config import (
    PORT, AUDIO_DIR, SESSION_DIR, OLLAMA_MODEL, OLLAMA_URL,
    DOCTOR_LAST_NAME, DOCTOR_FIRST_NAME,
)
from transcription.stt import WhisperTranscriber, set_correction_callback
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
BILLING_EXPORT_FILE = (
    Path(__file__).resolve().parent.parent
    / "Medical-Billing-Bot"
    / "20261016_billing_export.xlsx"
)
BILLING_ARCHIVE_DIRS = tuple(
    BILLING_EXPORT_FILE.parent / folder
    for folder in ("archive", "archives")
)


def _ssl_context_from_env():
    cert_file = (os.environ.get("PAB_SSL_CERT_FILE") or "").strip()
    key_file = (os.environ.get("PAB_SSL_KEY_FILE") or "").strip()
    if not cert_file or not key_file:
        return None
    if not os.path.isfile(cert_file) or not os.path.isfile(key_file):
        return None
    return (cert_file, key_file)


def get_stt():
    global _stt
    if _stt is None:
        socketio.emit("status", {"msg": "Loading Whisper model (first use)...", "level": "info"}, to="/", namespace="/")
        _stt = WhisperTranscriber()
        # Wire vocab-correction callback so the UI gets a badge per transcribe.
        set_correction_callback(lambda changes: socketio.emit(
            "transcript_corrections",
            {"count": len(changes), "changes": changes[:50]},
        ))
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


def _resolve_billing_workbook(file_id):
    """Resolve a workbook identifier strictly within the current export/archive folders."""
    parts = Path(str(file_id or "")).parts
    if len(parts) != 2 or parts[0] not in {"current", "archive", "archives"}:
        return None
    filename = parts[1]
    if Path(filename).name != filename or Path(filename).suffix.lower() != ".xlsx":
        return None

    if parts[0] == "current":
        path = BILLING_EXPORT_FILE
        if filename != path.name:
            return None
    else:
        archive_dir = next(
            (directory for directory in BILLING_ARCHIVE_DIRS if directory.name == parts[0]),
            None,
        )
        if archive_dir is None:
            return None
        path = archive_dir / filename

    if path.is_symlink() or not path.is_file():
        return None
    if not path.resolve().is_relative_to(BILLING_EXPORT_FILE.parent.resolve()):
        return None
    return path


def _workbook_cell_value(value):
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


# ── Note history ─────────────────────────────────────────────────────────────

def _save_note(sess, note):
    """Persist a completed note to NOTES_DIR as JSON."""
    try:
        safe_name = sess.get("patient_name", "unknown").replace(" ", "_")[:20]
        ts = datetime.now().isoformat()
        started_at = sess.get("start_time", "")
        fname = f"{ts[:10]}_{sess.get('id','x')}_{safe_name}.json"
        entry = {
            "session_id":    sess.get("id", ""),
            "patient_name":  sess.get("patient_name", ""),
            "patient_dob":   sess.get("patient_dob", ""),
            "health_card":   sess.get("health_card", ""),
            "visit_type":    sess.get("visit_type", "standard"),
            "timestamp":     ts,
            "service_date":  sess.get("service_date") or started_at[:10] or ts[:10],
            "service_time":  sess.get("service_time") or (started_at[11:16] if len(started_at) >= 16 else ts[11:16]),
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


def _billing_service_times(data, now=None):
    """Build rounded local billing times from the service-time field and elapsed timer."""
    from billing import add_elapsed_time, round_billing_time

    now = now or datetime.now()
    session_start = session.get("start_time", "")
    service_date = str(
        data.get("service_date")
        or session.get("service_date")
        or (session_start[:10] if len(session_start) >= 10 else now.strftime("%Y-%m-%d"))
    ).strip()
    start_time = str(
        data.get("service_time")
        or session.get("service_time")
        or (session_start[11:16] if len(session_start) >= 16 else now.strftime("%H:%M"))
    ).strip()
    elapsed_seconds = data.get("duration_seconds", 0)
    if (
        not isinstance(elapsed_seconds, int)
        or isinstance(elapsed_seconds, bool)
        or not 0 <= elapsed_seconds <= 24 * 60 * 60
    ):
        raise ValueError("Session duration must be a whole number of seconds from 0 to 24 hours.")
    prolonged_counselling = data.get("prolonged_counselling", False)
    if not isinstance(prolonged_counselling, bool):
        raise ValueError("Prolonged counselling confirmation must be checked or unchecked.")
    if prolonged_counselling and data.get("visit_type") != "counselling":
        raise ValueError("Select Counselling before confirming a prolonged counselling visit.")
    if prolonged_counselling or (
        data.get("visit_type") == "counselling" and elapsed_seconds >= 20 * 60
    ):
        elapsed_seconds = max(elapsed_seconds, 20 * 60)

    rounded_start = round_billing_time(start_time)
    if elapsed_seconds:
        rounded_end = add_elapsed_time(start_time, elapsed_seconds)
    else:
        end_time = str(data.get("service_end_time") or now.strftime("%H:%M")).strip()
        rounded_end = round_billing_time(end_time)
    return service_date, rounded_start, rounded_end, elapsed_seconds


def _is_prolonged_counselling(data, visit_type, elapsed_seconds):
    manually_confirmed = data.get("prolonged_counselling", False)
    if not isinstance(manually_confirmed, bool):
        raise ValueError("Prolonged counselling confirmation must be checked or unchecked.")
    if manually_confirmed and visit_type != "counselling":
        raise ValueError("Select Counselling before confirming a prolonged counselling visit.")
    return visit_type == "counselling" and (
        elapsed_seconds >= 20 * 60 or manually_confirmed
    )


@app.route("/api/update-session", methods=["POST"])
def update_session_fields():
    """Update patient fields in the active session without clearing transcript/audio."""
    data = request.get_json(force=True)
    for field in ("patient_name", "patient_dob", "health_card", "visit_type", "service_date", "service_time"):
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


def _audio_note_index():
    """Map of audio_filename -> note metadata (for orphan detection / linkage)."""
    index = {}
    try:
        for fname in os.listdir(NOTES_DIR):
            if not fname.endswith(".json"):
                continue
            try:
                with open(os.path.join(NOTES_DIR, fname), encoding="utf-8") as f:
                    data = json.load(f)
            except Exception:
                continue
            af = data.get("audio_filename") or ""
            if af:
                index[af] = {
                    "note_filename": fname,
                    "patient_name":  data.get("patient_name", ""),
                    "session_id":    data.get("session_id", ""),
                    "timestamp":     data.get("timestamp", ""),
                }
    except Exception as exc:
        print(f"[audio] note index failed: {exc}")
    return index


@app.route("/api/audio", methods=["GET"])
def list_audio():
    """List all session audio files with size, mtime, and any linked note metadata."""
    entries = []
    try:
        note_idx = _audio_note_index()
        for fname in os.listdir(AUDIO_DIR):
            if not fname.endswith(".webm"):
                continue
            fpath = os.path.join(AUDIO_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            st = os.stat(fpath)
            link = note_idx.get(fname)
            entries.append({
                "filename":      fname,
                "size_bytes":    st.st_size,
                "mtime":         datetime.fromtimestamp(st.st_mtime).isoformat(),
                "linked":        bool(link),
                "patient_name":  (link or {}).get("patient_name", ""),
                "session_id":    (link or {}).get("session_id", ""),
                "note_filename": (link or {}).get("note_filename", ""),
            })
        entries.sort(key=lambda e: e["mtime"], reverse=True)
    except Exception as exc:
        print(f"[audio] list failed: {exc}")
    total_bytes = sum(e["size_bytes"] for e in entries)
    return jsonify({
        "entries":      entries,
        "count":        len(entries),
        "total_bytes":  total_bytes,
    })


@app.route("/api/audio/<path:filename>", methods=["GET"])
def get_audio(filename):
    """Serve a saved audio file by name."""
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    fpath = os.path.join(AUDIO_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "Not found"}), 404
    return send_file(fpath, mimetype="audio/webm")


@app.route("/api/audio/<path:filename>", methods=["DELETE"])
def delete_audio(filename):
    """Delete a single audio file. Optionally clears the linked note's audio_filename field."""
    if ".." in filename or "/" in filename or "\\" in filename:
        return jsonify({"error": "Invalid filename"}), 400
    fpath = os.path.join(AUDIO_DIR, filename)
    if not os.path.isfile(fpath):
        return jsonify({"error": "Not found"}), 404
    try:
        os.remove(fpath)
    except OSError as exc:
        return jsonify({"error": str(exc)}), 500

    # Unlink from any note that referenced this audio file
    cleared_notes = 0
    try:
        for nf in os.listdir(NOTES_DIR):
            if not nf.endswith(".json"):
                continue
            npath = os.path.join(NOTES_DIR, nf)
            try:
                with open(npath, encoding="utf-8") as f:
                    data = json.load(f)
                if data.get("audio_filename") == filename:
                    data["audio_filename"] = ""
                    with open(npath, "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                    cleared_notes += 1
            except Exception:
                continue
    except Exception:
        pass
    return jsonify({"status": "deleted", "notes_unlinked": cleared_notes})


@app.route("/api/audio/purge", methods=["POST"])
def purge_audio():
    """Bulk-delete audio files.
    Body: {"mode": "all" | "orphans" | "older_than", "days": int (only for older_than)}
    """
    data = request.get_json(silent=True) or {}
    mode = data.get("mode", "orphans")
    if mode not in ("all", "orphans", "older_than"):
        return jsonify({"error": "Invalid mode"}), 400

    note_idx = _audio_note_index() if mode in ("orphans", "all") else {}
    now = datetime.now().timestamp()
    days = 0
    if mode == "older_than":
        try:
            days = int(data.get("days", 30))
        except (TypeError, ValueError):
            return jsonify({"error": "Invalid days"}), 400
        if days < 0:
            return jsonify({"error": "days must be >= 0"}), 400

    deleted, errors, freed = [], [], 0
    try:
        for fname in list(os.listdir(AUDIO_DIR)):
            if not fname.endswith(".webm"):
                continue
            fpath = os.path.join(AUDIO_DIR, fname)
            if not os.path.isfile(fpath):
                continue
            if mode == "orphans" and fname in note_idx:
                continue
            if mode == "older_than":
                age_days = (now - os.path.getmtime(fpath)) / 86400.0
                if age_days < days:
                    continue
            size = os.path.getsize(fpath)
            try:
                os.remove(fpath)
                deleted.append(fname)
                freed += size
            except OSError as exc:
                errors.append({"filename": fname, "error": str(exc)})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500

    # For "all" mode: delete notes too. For other modes: unlink audio_filename only.
    notes_deleted = 0
    notes_unlinked = 0
    if mode == "all":
        try:
            for nf in os.listdir(NOTES_DIR):
                if not nf.endswith(".json"):
                    continue
                npath = os.path.join(NOTES_DIR, nf)
                try:
                    os.remove(npath)
                    notes_deleted += 1
                except OSError:
                    continue
        except Exception:
            pass
    elif deleted:
        deleted_set = set(deleted)
        try:
            for nf in os.listdir(NOTES_DIR):
                if not nf.endswith(".json"):
                    continue
                npath = os.path.join(NOTES_DIR, nf)
                try:
                    with open(npath, encoding="utf-8") as f:
                        ndata = json.load(f)
                    if ndata.get("audio_filename") in deleted_set:
                        ndata["audio_filename"] = ""
                        with open(npath, "w", encoding="utf-8") as f:
                            json.dump(ndata, f, ensure_ascii=False, indent=2)
                        notes_unlinked += 1
                except Exception:
                    continue
        except Exception:
            pass

    return jsonify({
        "status":         "ok",
        "mode":           mode,
        "deleted_count":  len(deleted),
        "freed_bytes":    freed,
        "notes_deleted":  notes_deleted,
        "notes_unlinked": notes_unlinked,
        "errors":         errors,
    })


# ── ICD-9 lookup ─────────────────────────────────────────────────────────────

_ICD9_INDEX: list[dict] | None = None
_ICD9_LOWER: list[tuple[str, str, dict]] | None = None  # (code_lower, desc_lower, record)

def _load_icd9():
    global _ICD9_INDEX, _ICD9_LOWER
    if _ICD9_INDEX is not None:
        return
    data_path = Path(__file__).parent / "vocab" / "data" / "icd9.json"
    with open(data_path, encoding="utf-8") as f:
        _ICD9_INDEX = json.load(f)
    _ICD9_LOWER = [(r["code"].lower(), r["desc"].lower(), r) for r in _ICD9_INDEX]


@app.route("/api/icd9/search", methods=["GET"])
def icd9_search():
    """Search ICD-9-CM codes by code prefix or description keywords.
    Query params:
      q     — search string (required)
      limit — max results (default 20, max 100)
    Returns list of {code, desc, short}.
    """
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify([])
    try:
        limit = min(int(request.args.get("limit", 20)), 100)
    except (TypeError, ValueError):
        limit = 20

    _load_icd9()
    q_lower = q.lower().replace(".", "")

    # Phase 1: code prefix matches (ordered by code length asc so specific codes first)
    code_hits = [r for (cl, _, r) in _ICD9_LOWER if cl.replace(".", "").startswith(q_lower)]
    code_hits.sort(key=lambda r: len(r["code"]))

    # Phase 2: description word matches (all words in q must appear in desc)
    words = q_lower.split()
    desc_hits = [r for (_, dl, r) in _ICD9_LOWER if all(w in dl for w in words)]

    # Merge: code matches first, then desc-only matches, dedup by code
    seen = {r["code"] for r in code_hits}
    merged = code_hits + [r for r in desc_hits if r["code"] not in seen]
    return jsonify(merged[:limit])


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
                result["transcript"] = get_stt().transcribe(fpath, diarize=diarize, visit_type=session.get("visit_type"))
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
        "service_date":  data.get("service_date", "").strip(),
        "service_time":  data.get("service_time", "").strip(),
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
                transcript = get_stt().transcribe(audio_path, diarize=not no_diarize, visit_type=session.get("visit_type"))
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
                transcript = get_stt().transcribe(audio_path, visit_type=session.get("visit_type"))
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
    for field in ("patient_name", "patient_dob", "health_card", "visit_type", "service_date", "service_time"):
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
            note = get_soap()._parse(raw, template_config=template_config)
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

    try:
        date_svc, start_hm, end_hm, duration_seconds = _billing_service_times(data)
        prolonged_counselling = _is_prolonged_counselling(
            data, visit_type, duration_seconds
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400

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
                prolonged_counselling=prolonged_counselling,
            )
            socketio.emit("billing_result", result)
            socketio.emit("status", {"msg": "Billing submitted.", "level": "success"})
        except Exception as exc:
            socketio.emit("status", {"msg": f"Billing error: {exc}", "level": "error"})

    threading.Thread(target=_bill, daemon=True).start()
    return jsonify({"status": "billing"})


@app.route("/api/export-billing", methods=["POST"])
def export_billing():
    """Append the current encounter to the supplied Medical-Billing-Bot workbook."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid billing export request"}), 400

    note = data.get("soap_note") or session.get("soap_note")
    if not isinstance(note, dict) or note.get("note_format") == "freeform":
        return jsonify({"error": "No clinical note available to export"}), 400

    icd9_codes = note.get("icd9_codes") or []
    if isinstance(icd9_codes, str):
        icd9_codes = [icd9_codes]
    if not icd9_codes:
        return jsonify({"error": "The note has no ICD-9 diagnosis codes to export"}), 400

    patient_name = str(data.get("patient_name") or session.get("patient_name") or "").strip()
    if not patient_name:
        return jsonify({"error": "Patient name is required for billing export"}), 400

    patient_dob = str(data.get("patient_dob") or session.get("patient_dob") or "").strip()
    health_card = str(data.get("health_card") or session.get("health_card") or "").strip()
    visit_type = str(data.get("visit_type") or session.get("visit_type") or "standard")
    now = datetime.now()
    try:
        date_of_service, start_time, end_time, duration_seconds = _billing_service_times(data, now)
        prolonged_counselling = _is_prolonged_counselling(
            data, visit_type, duration_seconds
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    requested_times = (date_of_service, start_time, end_time)

    from billing import append_billing_export_row, build_billing_export_row
    from zipfile import BadZipFile
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        row = build_billing_export_row(
            patient_name=patient_name,
            patient_dob=patient_dob,
            health_card=health_card,
            icd9_codes=icd9_codes,
            visit_type=visit_type,
            date_of_service=date_of_service,
            start_time=start_time,
            end_time=end_time,
            doc_last_name=DOCTOR_LAST_NAME or "Das",
            doc_first_name=DOCTOR_FIRST_NAME or "Prasenjit",
            prolonged_counselling=prolonged_counselling,
        )
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        row_number = append_billing_export_row(row, str(BILLING_EXPORT_FILE))
    except ValueError as exc:
        app.logger.warning("Billing workbook export blocked: %s", exc)
        return jsonify({"error": str(exc)}), 409
    except (OSError, BadZipFile, InvalidFileException) as exc:
        app.logger.exception("Billing workbook export failed")
        return jsonify({"error": f"Billing workbook export failed: {exc}"}), 500
    return jsonify({
        "status": "exported",
        "row": row_number,
        "billing_item": row["billing_item"],
        "date_of_service": row["date_of_service"],
        "start_time": row["start_time"],
        "end_time": row["end_time"],
        "time_adjusted": requested_times != (
            row["date_of_service"], row["start_time"], row["end_time"]
        ),
    })


@app.route("/api/xlsx/workbooks", methods=["GET"])
def list_billing_workbooks():
    """List the current export and archived billing workbooks available to edit."""
    workbooks = []
    candidates = []
    if BILLING_EXPORT_FILE.is_file():
        candidates.append(("current", BILLING_EXPORT_FILE))
    for archive_dir in BILLING_ARCHIVE_DIRS:
        if archive_dir.is_dir():
            candidates.extend(
                (archive_dir.name, path)
                for path in archive_dir.glob("*.xlsx")
                if path.is_file() and not path.is_symlink()
            )

    for folder, path in sorted(candidates, key=lambda item: (item[0] != "current", item[1].name)):
        if not path.resolve().is_relative_to(BILLING_EXPORT_FILE.parent.resolve()):
            continue
        try:
            workbook = load_workbook(path, read_only=True, data_only=True)
        except (OSError, ValueError, InvalidFileException, BadZipFile):
            app.logger.exception("Could not inspect billing workbook %s", path)
            continue
        try:
            sheets = [
                {"name": sheet.title, "rows": sheet.max_row, "columns": sheet.max_column}
                for sheet in workbook.worksheets
            ]
            workbooks.append({
                "id": f"{folder}/{path.name}",
                "name": path.name,
                "sheets": sheets,
            })
        finally:
            workbook.close()
    return jsonify(workbooks)


@app.route("/api/xlsx/workbook", methods=["GET"])
def read_billing_workbook():
    file_id = request.args.get("file", "")
    path = _resolve_billing_workbook(file_id)
    if path is None:
        return jsonify({"error": "Workbook not found"}), 404
    sheet_name = request.args.get("sheet", "")
    try:
        workbook = load_workbook(path, read_only=True, data_only=False)
        if sheet_name and sheet_name not in workbook.sheetnames:
            workbook.close()
            return jsonify({"error": "Worksheet not found"}), 404
        sheet = workbook[sheet_name] if sheet_name else workbook.active
        rows = [
            [_workbook_cell_value(value) for value in row]
            for row in sheet.iter_rows(values_only=True)
        ]
        result = {
            "file": file_id,
            "name": path.name,
            "sheet": sheet.title,
            "sheets": workbook.sheetnames,
            "rows": rows,
            "modified_ns": str(path.stat().st_mtime_ns),
        }
        workbook.close()
        return jsonify(result)
    except (OSError, ValueError, InvalidFileException, BadZipFile) as exc:
        app.logger.exception("Could not read billing workbook %s", path)
        return jsonify({"error": f"Could not read workbook: {exc}"}), 500


@app.route("/api/xlsx/workbook", methods=["POST"])
def save_billing_workbook():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"error": "Invalid workbook edit request"}), 400

    file_id = data.get("file", "")
    path = _resolve_billing_workbook(file_id)
    if path is None:
        return jsonify({"error": "Workbook not found"}), 404
    sheet_name = data.get("sheet")
    changes = data.get("changes")
    add_rows = data.get("add_rows", 0)
    new_rows = data.get("new_rows", [])
    delete_rows = data.get("delete_rows", [])
    expected_mtime = data.get("modified_ns")
    if (
        not isinstance(sheet_name, str)
        or not isinstance(changes, list)
        or not isinstance(new_rows, list)
        or not isinstance(delete_rows, list)
        or not isinstance(add_rows, int)
        or isinstance(add_rows, bool)
        or add_rows < 0
        or add_rows > 100
        or len(new_rows) + add_rows > 100
        or (not changes and not new_rows and not delete_rows and add_rows == 0)
    ):
        return jsonify({"error": "Worksheet, valid cell edits, or added rows are required"}), 400
    if len(changes) > 1000:
        return jsonify({"error": "Save at most 1,000 edited cells at a time"}), 400
    if not isinstance(expected_mtime, str) or not expected_mtime.isdecimal():
        return jsonify({"error": "Reload the workbook before saving"}), 400
    if str(path.stat().st_mtime_ns) != expected_mtime:
        return jsonify({"error": "This workbook changed since it was opened. Reload before saving."}), 409

    try:
        workbook = load_workbook(path, data_only=False)
        if sheet_name not in workbook.sheetnames:
            workbook.close()
            return jsonify({"error": "Worksheet not found"}), 404
        sheet = workbook[sheet_name]
        original_max_row = sheet.max_row
        if not all(
            isinstance(row, int) and not isinstance(row, bool)
            and 2 <= row <= original_max_row
            for row in delete_rows
        ) or len(delete_rows) != len(set(delete_rows)):
            workbook.close()
            return jsonify({"error": "Only existing data rows can be deleted"}), 400
        deleted_set = set(delete_rows)
        if len(changes) > 1000:
            workbook.close()
            return jsonify({"error": "Save at most 1,000 edited cells at a time"}), 400

        from billing import normalise_diagnosis_value

        header_columns = {
            str(sheet.cell(row=1, column=column).value or "")
            .strip()
            .lower()
            .replace(" ", "_"): column
            for column in range(1, sheet.max_column + 1)
        }

        def normalise_cell(column, value):
            header = next(
                (name for name, index in header_columns.items() if index == column),
                "",
            )
            if header in {"last_name", "first_name"}:
                return value.upper()
            if header == "diagnosis":
                return normalise_diagnosis_value(value)
            return value

        seen_cells = set()
        for change in changes:
            if not isinstance(change, dict):
                workbook.close()
                return jsonify({"error": "Invalid cell edit"}), 400
            row = change.get("row")
            column = change.get("column")
            value = change.get("value")
            if (
                not isinstance(row, int) or isinstance(row, bool) or row < 1
                or not isinstance(column, int) or isinstance(column, bool) or column < 1
                or row > original_max_row
                or column > sheet.max_column
                or not isinstance(value, str) or len(value) > 5000
                or row in deleted_set
            ):
                workbook.close()
                return jsonify({"error": "Cell edit is outside the worksheet, targets a deleted row, or has an invalid value"}), 400
            coordinate = (row, column)
            if coordinate in seen_cells:
                workbook.close()
                return jsonify({"error": "Duplicate cell edits are not allowed"}), 400
            seen_cells.add(coordinate)
            normalized_value = normalise_cell(column, value)
            sheet.cell(row=row, column=column).value = normalized_value if normalized_value else None

        for row in sorted(deleted_set, reverse=True):
            sheet.delete_rows(row, 1)

        for values in new_rows:
            if (
                not isinstance(values, list)
                or len(values) > sheet.max_column
                or any(not isinstance(value, str) or len(value) > 5000 for value in values)
            ):
                workbook.close()
                return jsonify({"error": "New worksheet rows must contain valid text cells"}), 400
            row_values = [
                normalise_cell(column, values[column - 1] if column <= len(values) else "")
                for column in range(1, sheet.max_column + 1)
            ]
            sheet.append(row_values)
        for _ in range(add_rows):
            sheet.append([""] + [None] * max(sheet.max_column - 1, 0))

        file_mode = stat.S_IMODE(path.stat().st_mode)
        with tempfile.NamedTemporaryFile(
            dir=path.parent, suffix=".xlsx", prefix=".billing-edit-", delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
        try:
            workbook.save(temp_path)
            workbook.close()
            os.chmod(temp_path, file_mode)
            if str(path.stat().st_mtime_ns) != expected_mtime:
                return jsonify({"error": "This workbook changed while saving. Reload before trying again."}), 409
            os.replace(temp_path, path)
        finally:
            if temp_path.exists():
                temp_path.unlink()
        return jsonify({"status": "saved", "modified_ns": str(path.stat().st_mtime_ns)})
    except (OSError, ValueError, InvalidFileException, BadZipFile) as exc:
        app.logger.exception("Could not save billing workbook %s", path)
        return jsonify({"error": f"Could not save workbook: {exc}"}), 500


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
    ssl_context = _ssl_context_from_env()
    public_host = (os.environ.get("PAB_PUBLIC_HOSTNAME") or "").strip()
    print("=" * 60)
    print("  Physician Assistant Bot")
    if ssl_context and public_host:
        print(f"  Secure:  https://{public_host}:{PORT}")
        print("  Note  :  use the tailnet hostname so the certificate matches")
    else:
        print(f"  Local :  http://localhost:{PORT}")
        print(f"  Phone  :  http://{local_ip}:{PORT}")
    print("=" * 60)

    # Warmup Ollama in background so first generation is fast
    def _warmup_delayed():
        import time
        time.sleep(2)  # brief pause so Flask finishes starting
        get_soap().warmup()
    threading.Thread(target=_warmup_delayed, daemon=True).start()

    # Debug + reloader are opt-in via env var. The reloader spawns a child
    # process and can lose the venv (causing the wrong Python to be used),
    # so default to off for unattended/launchd runs.
    _debug = os.environ.get("PAB_DEBUG", "0") == "1"
    socketio.run(
        app,
        host="0.0.0.0",
        port=PORT,
        debug=_debug,
        use_reloader=_debug,
        reloader_type='stat',
        allow_unsafe_werkzeug=True,
        ssl_context=ssl_context,
    )
