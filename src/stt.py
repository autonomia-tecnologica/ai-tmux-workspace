#!/usr/bin/env python3
"""Offline speech-to-text helper, run with the faster-whisper venv's python (not the system one).

Linux only: it is the transcription half of src/mic.py, whose recorder is ALSA's arecord. On other
platforms `main()` returns the "not available" message before importing numpy/faster-whisper.

Usage: stt.py -> loads the model, then reads raw s16le 16 kHz mono audio from stdin until EOF and prints the
transcript on stdout. The caller starts it before recording so the model loads while the user speaks.
The model is read from the local cache only (HF_HUB_OFFLINE): the workspace never touches the network.
"""
import os
import sys

import i18n


def available(platform=None):
    """True on the only platform the recorder supports: Linux/ALSA (arecord)."""
    return (platform or sys.platform).startswith("linux")


def default_models(platform=None):
    """Default model cache: the legacy Linux install path, or the per-user cache elsewhere."""
    if (platform or sys.platform).startswith("linux"):
        return "/mnt/Data/LLM/at-whisper/models"
    return os.path.expanduser("~/.cache/at-whisper/models")


MODELS = os.environ.get("AT_WHISPER_MODELS", default_models())
MODEL = os.environ.get("AT_WHISPER_MODEL", "small")
LANGUAGE = os.environ.get("AT_WHISPER_LANGUAGE", "pt")


def main():
    """Loads the model and transcribes stdin to stdout. Returns 0 on success, 1 off Linux."""
    if not available():
        print(i18n.t("mic.no_arecord"), file=sys.stderr)
        return 1
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

    import numpy as np
    from faster_whisper import WhisperModel

    model = WhisperModel(MODEL, device="cpu", compute_type="int8", download_root=MODELS,
                         cpu_threads=os.cpu_count() or 4)
    audio = np.frombuffer(sys.stdin.buffer.read(), dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio, language=LANGUAGE, vad_filter=True, beam_size=1,
                                   condition_on_previous_text=False)
    print(" ".join(s.text.strip() for s in segments).strip())
    return 0


if __name__ == "__main__":
    sys.exit(main())
