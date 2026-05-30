"""
transcription/stt.py
Local speech-to-text using faster-whisper (OpenAI Whisper, CPU-optimised).
Runs completely offline — zero cost.

Requirements:
  pip install faster-whisper
  ffmpeg must be installed and in PATH  (https://ffmpeg.org/download.html)
  Windows: winget install ffmpeg   or   choco install ffmpeg
"""

import os
from config import WHISPER_MODEL

# faster-whisper is imported lazily so the server starts instantly
# and loads the model only on first transcription request


class WhisperTranscriber:
    """Wraps faster-whisper.WhisperModel with medical-dictation defaults."""

    def __init__(self, model_size: str = WHISPER_MODEL):
        from faster_whisper import WhisperModel  # lazy import

        print(f"[Whisper] Loading model '{model_size}' on CPU (int8)…")
        # int8 quantisation — fast on CPU, minimal quality loss
        self._model = WhisperModel(model_size, device="cpu", compute_type="int8")
        print("[Whisper] Model ready.")

    def transcribe(self, audio_path: str) -> str:
        """
        Transcribe an audio file (WebM, MP3, WAV, MP4 …) to text.
        Returns the full transcript as a single string.
        """
        if not audio_path or not os.path.exists(audio_path):
            raise FileNotFoundError(f"Audio file not found: {audio_path}")

        segments, _info = self._model.transcribe(
            audio_path,
            beam_size=5,
            language="en",
            # VAD filter suppresses non-speech silence — critical for long consultations
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 500,
                "speech_pad_ms": 200,
            },
            # Initial prompt primes the model on medical vocabulary
            initial_prompt=(
                "Family medicine consultation. "
                "Medical terms, diagnoses, medications, and dosages follow."
            ),
        )

        # Materialise generator
        text_parts = []
        for seg in segments:
            text_parts.append(seg.text.strip())

        return " ".join(text_parts).strip()
