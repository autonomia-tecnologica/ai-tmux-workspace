#!/usr/bin/env python3
"""Offline speech-to-text helper, run with the faster-whisper venv's python (not the system one).

Usage: stt.py -> loads the model, then reads raw s16le 16 kHz mono audio from stdin until EOF and prints the
transcript on stdout. The caller starts it before recording so the model loads while the user speaks.
The model is read from the local cache only (HF_HUB_OFFLINE): the workspace never touches the network.
"""
import os
import sys

os.environ.setdefault("HF_HUB_OFFLINE", "1")

import numpy as np  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

MODELS = os.environ.get("AT_WHISPER_MODELS", "/mnt/Data/LLM/at-whisper/models")
MODEL = os.environ.get("AT_WHISPER_MODEL", "small")
LANGUAGE = os.environ.get("AT_WHISPER_LANGUAGE", "pt")

if __name__ == "__main__":
    model = WhisperModel(MODEL, device="cpu", compute_type="int8", download_root=MODELS,
                         cpu_threads=os.cpu_count() or 4)
    audio = np.frombuffer(sys.stdin.buffer.read(), dtype=np.int16).astype(np.float32) / 32768.0
    segments, _ = model.transcribe(audio, language=LANGUAGE, vad_filter=True, beam_size=1,
                                   condition_on_previous_text=False)
    print(" ".join(s.text.strip() for s in segments).strip())
