"""
transcription/stt.py
Local speech-to-text — uses mlx-whisper on Apple Silicon (GPU, 3-5x faster)
and falls back to faster-whisper on CPU automatically.

Speaker diarization: pause-based heuristic labels turns as Dr: / Pt:
so the LLM can accurately attribute speech to the correct SOAP sections.
"""

import os
import re
import shutil
import subprocess
import platform
import tempfile
from config import WHISPER_MODEL

# ── Ensure ffmpeg is on PATH (static-ffmpeg provides a pre-built binary) ─────
try:
    import static_ffmpeg
    static_ffmpeg.add_paths()
except ImportError:
    pass  # fall back to system ffmpeg if static-ffmpeg not installed

# ── Speaker-diarization settings ─────────────────────────────────────────────
_SPEAKER_GAP_S      = 0.6   # pause ≥ this → candidate for speaker change
_MIN_SPEAKER_HOLD_S = 3.0   # stay on same speaker for at least this long before flipping
_START_SPEAKER      = "Dr"  # doctor typically opens the consultation

# ── Medical vocabulary priming prompt ─────────────────────────────────────────
_MEDICAL_PROMPT = (
    "Family medicine consultation between doctor and patient. "
    "Chief complaint, history, physical exam, diagnoses, prescriptions. "
    "Abbreviations: PMHx, FHx, SHx, ROS, HPI, c/o, h/o, r/o, s/p, SOB, N/V/D, "
    "HTN, DM2, GERD, URI, UTI, CAD, CHF, COPD, CKD, T2DM, OA, RA, "
    "BP, HR, RR, SpO2, BMI, HbA1c, eGFR, TSH, INR, CBC, BMP, ECG, CXR. "
    "Medications: metformin, lisinopril, ramipril, atorvastatin, rosuvastatin, "
    "amlodipine, hydrochlorothiazide, bisoprolol, metoprolol, pantoprazole, "
    "amoxicillin, azithromycin, ciprofloxacin, trimethoprim, nitrofurantoin, "
    "salbutamol, fluticasone, tiotropium, montelukast, cetirizine, "
    "sertraline, escitalopram, venlafaxine, quetiapine, clonazepam, lorazepam, "
    "levothyroxine, prednisone, naproxen, ibuprofen, acetaminophen, "
    "warfarin, apixaban, rivaroxaban, clopidogrel, aspirin, nitroglycerin."
)


def _ffmpeg() -> str:
    return shutil.which("ffmpeg") or "ffmpeg"


def _audio_channels(audio_path: str) -> int:
    """Return number of audio channels in file using ffmpeg stderr output."""
    try:
        r = subprocess.run([_ffmpeg(), "-i", audio_path],
                           capture_output=True, text=True,
                           stdin=subprocess.DEVNULL)
        for line in r.stderr.splitlines():
            if "Audio:" in line:
                if "stereo" in line:
                    return 2
                if "mono" in line:
                    return 1
                m = re.search(r"(\d+) channels?", line)
                if m and int(m.group(1)) >= 2:
                    return 2
    except Exception:
        pass
    return 1


def _extract_channel(audio_path: str, channel: str) -> str:
    """Extract FL (left=Dr) or FR (right=Pt) as a temp mono WAV. Caller must delete."""
    fd, tmp = tempfile.mkstemp(suffix=".wav")
    os.close(fd)
    subprocess.run(
        [_ffmpeg(), "-y", "-i", audio_path, "-af", f"pan=mono|c0={channel}", tmp],
        check=True, capture_output=True, stdin=subprocess.DEVNULL,
    )
    return tmp


def _diarize(segments, get_start, get_end, get_text):
    """
    Apply pause-based speaker heuristic to a list of segments.
    Switches Dr:/Pt: when a pause >= _SPEAKER_GAP_S occurs AND the current
    speaker has been talking for at least _MIN_SPEAKER_HOLD_S (prevents
    flipping mid-sentence on a natural breath pause).
    """
    lines = []
    speaker = _START_SPEAKER
    prev_end = None
    last_switch_at = 0.0
    for seg in segments:
        text = get_text(seg).strip()
        if not text:
            continue
        start = get_start(seg)
        if prev_end is not None:
            gap = start - prev_end
            held_for = start - last_switch_at
            if gap >= _SPEAKER_GAP_S and held_for >= _MIN_SPEAKER_HOLD_S:
                speaker = "Pt" if speaker == "Dr" else "Dr"
                last_switch_at = start
        lines.append(f"{speaker}: {text}")
        prev_end = get_end(seg)
    return "\n".join(lines)


class WhisperTranscriber:
    """
    Wraps mlx-whisper (Apple Silicon Metal GPU) with automatic fallback
    to faster-whisper (CPU int8) on non-Apple or missing mlx-whisper.
    """

    def __init__(self, model_size: str = WHISPER_MODEL):
        self._use_mlx = False
        if platform.machine() == "arm64" and platform.system() == "Darwin":
            try:
                import mlx_whisper  # noqa — test import only
                self._use_mlx  = True
                self._mlx_repo = f"mlx-community/whisper-{model_size}-mlx"
                print(f"[Whisper] Apple Silicon — MLX model '{model_size}' (GPU).")
                return
            except ImportError:
                print("[Whisper] mlx-whisper not installed — falling back to faster-whisper CPU.")
                print("[Whisper] Run:  pip install mlx-whisper  for 3-5x speedup on this Mac.")

        from faster_whisper import WhisperModel
        print(f"[Whisper] Loading '{model_size}' on CPU (int8)…")
        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        print("[Whisper] Ready.")

    def transcribe(self, audio_path: str, diarize: bool = True) -> str:
        if not audio_path or not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")
        # Phone call mode: stereo = L channel is doctor's mic, R is patient's audio
        if _audio_channels(audio_path) >= 2:
            print("[Whisper] Stereo detected — phone call mode: channel-based diarization.")
            return self._transcribe_stereo(audio_path)
        if not diarize:
            print("[Whisper] Diarization disabled — returning raw transcript.")
            return self._transcribe_raw(audio_path)
        return self._transcribe_mlx(audio_path) if self._use_mlx else self._transcribe_faster(audio_path)

    def _get_segments(self, audio_path: str) -> list:
        """Return [(start, end, text), …] for a mono audio file."""
        if self._use_mlx:
            import mlx_whisper
            result = mlx_whisper.transcribe(
                audio_path,
                path_or_hf_repo=self._mlx_repo,
                language="en",
                initial_prompt=_MEDICAL_PROMPT,
                word_timestamps=False,
            )
            return [
                (s.get("start", 0), s.get("end", 0), s.get("text", "").strip())
                for s in result.get("segments", [])
            ]
        else:
            segs, _ = self._model.transcribe(
                audio_path,
                beam_size=5,
                language="en",
                vad_filter=True,
                word_timestamps=False,
                condition_on_previous_text=False,
                vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
                initial_prompt=_MEDICAL_PROMPT,
            )
            return [(s.start, s.end, s.text.strip()) for s in segs]

    def _transcribe_stereo(self, audio_path: str) -> str:
        """Split stereo into L (Dr mic) and R (patient audio), transcribe each,
        then merge segments sorted by start time for a perfectly labelled transcript."""
        dr_path = pt_path = None
        try:
            dr_path = _extract_channel(audio_path, "FL")   # left  = doctor
            pt_path = _extract_channel(audio_path, "FR")   # right = patient
            print("[Whisper] Transcribing doctor channel…")
            dr_segs = self._get_segments(dr_path)
            print("[Whisper] Transcribing patient channel…")
            pt_segs = self._get_segments(pt_path)
            all_segs = (
                [(s, e, t, "Dr") for s, e, t in dr_segs if t] +
                [(s, e, t, "Pt") for s, e, t in pt_segs if t]
            )
            all_segs.sort(key=lambda x: x[0])
            return "\n".join(f"{spk}: {txt}" for _, _, txt, spk in all_segs)
        finally:
            for p in (dr_path, pt_path):
                if p:
                    try:
                        os.unlink(p)
                    except OSError:
                        pass

    def _transcribe_raw(self, audio_path: str) -> str:
        """Transcribe without any speaker labels (used when phone call capture failed)."""
        if self._use_mlx:
            import mlx_whisper
            result = mlx_whisper.transcribe(
                audio_path,
                path_or_hf_repo=self._mlx_repo,
                language="en",
                initial_prompt=_MEDICAL_PROMPT,
                word_timestamps=False,
            )
            segs = result.get("segments", [])
            return " ".join(s.get("text", "").strip() for s in segs if s.get("text", "").strip())
        else:
            segs, _ = self._model.transcribe(
                audio_path,
                beam_size=5,
                language="en",
                vad_filter=True,
                word_timestamps=False,
                condition_on_previous_text=False,
                vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
                initial_prompt=_MEDICAL_PROMPT,
            )
            return " ".join(s.text.strip() for s in segs if s.text.strip())

    def _transcribe_mlx(self, audio_path: str) -> str:
        import mlx_whisper
        result = mlx_whisper.transcribe(
            audio_path,
            path_or_hf_repo=self._mlx_repo,
            language="en",
            initial_prompt=_MEDICAL_PROMPT,
            word_timestamps=False,
        )
        segs = result.get("segments", [])
        if not segs:
            return result.get("text", "").strip()
        return _diarize(segs,
                        get_start=lambda s: s.get("start", 0),
                        get_end  =lambda s: s.get("end",   0),
                        get_text =lambda s: s.get("text",  ""))

    def _transcribe_faster(self, audio_path: str) -> str:
        segs, _ = self._model.transcribe(
            audio_path,
            beam_size=5,
            language="en",
            vad_filter=True,
            word_timestamps=False,
            condition_on_previous_text=False,
            vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 200},
            initial_prompt=_MEDICAL_PROMPT,
        )
        return _diarize(list(segs),
                        get_start=lambda s: s.start,
                        get_end  =lambda s: s.end,
                        get_text =lambda s: s.text)
