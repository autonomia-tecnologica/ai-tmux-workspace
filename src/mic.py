#!/usr/bin/env python3
"""Microphone dictation (desktop): records the default mic until you stop talking and types the transcript into
the window's active Claude tab, without Enter. Recording ends after SILENCE_S of silence following speech;
"silence" is adaptive (see SilenceDetector), not a fixed volume.

Screen: 1 s on a red background with an animated sine wave (get ready), then green showing the
live waveform of the captured audio, then a short "transcribing" screen while faster-whisper runs
offline (src/stt.py, in the venv at ~/.cache/at-whisper/venv, override with AT_WHISPER_VENV).
Usage: mic.py <window id>
"""
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import wave

import i18n
import lib

READY_S = 1.0
SILENCE_S = 2.0             # silence after speech that ends the recording
NO_SPEECH_S = 8.0           # nobody spoke at all: give up, nothing is typed
MAX_S = 180.0               # safety cap
MIN_RMS = 150.0             # absolute floor of the speech threshold (s16 units)
BOX_W, BOX_H = 56, 9       # the wave box, centered in the popup
RATE = 16000
CHUNK = RATE // 20          # 50 ms of samples per waveform column
BG_RED, BG_GREEN, BG_BLUE = "\x1b[48;5;124m", "\x1b[48;5;28m", "\x1b[48;5;24m"
FG = "\x1b[38;5;255m"
VENV = os.environ.get("AT_WHISPER_VENV", os.path.expanduser("~/.cache/at-whisper/venv"))
DOCKER_IMAGE = os.environ.get("AT_WHISPER_IMAGE", "at-whisper-rocm")
GGML_MODEL = os.environ.get("AT_WHISPER_GGML", "/mnt/Data/LLM/at-whisper/ggml/ggml-small.bin")
LANGUAGE = os.environ.get("AT_WHISPER_LANGUAGE", "pt")
STT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "stt.py")


def frame(bg, cols, rows, amps, title):
    """Compact centered box (BOX_W x BOX_H): `title` on top, a bar waveform below (one amp 0..1 per column).
    The rest of the screen is cleared and left plain."""
    w, h = min(BOX_W, cols), min(BOX_H, rows)
    x0, y0 = (cols - w) // 2, (rows - h) // 2
    amps = amps[-w:] if len(amps) >= w else [0.0] * (w - len(amps)) + list(amps)
    mid, half = 1 + (h - 1) // 2, max(1, (h - 1) // 2 - 0)
    out = ["\x1b[H\x1b[2J"]
    for r in range(h):
        if r == 0:
            line = f" {title}"[:w].ljust(w)
        else:
            line = "".join("█" if abs(r - mid) <= round(a * (half - 1)) else " " for a in amps)
        out.append(f"\x1b[{y0 + r + 1};{x0 + 1}H{bg}{FG}{line}\x1b[0m")
    return "".join(out)


def sine_amps(cols, t):
    return [0.15 + 0.85 * abs(math.sin(x / 6 - t * 8)) * (0.5 + 0.5 * math.sin(x / 17 + t * 2)) for x in range(cols)]


def pcm_peak(buf):
    """Peak 0..1 of a raw s16le chunk (soft-boosted so normal speech fills the screen)."""
    import array
    a = array.array("h", buf[: len(buf) // 2 * 2])
    return min(1.0, (max(map(abs, a)) / 32768.0) * 3) if a else 0.0


class SilenceDetector:
    """Decides when the speaker finished, from the RMS of each 50 ms chunk, relative to what came before.

    threshold = max(MIN_RMS, 2.5 x noise floor, 30% of the average speech level so far)
    - noise floor: moving average of the chunks below the threshold (starts at the quietest of the first 10);
    - speech level: moving average of the chunks above it, so a loud speaker needs a deeper drop to count
      as silence, and a quiet one is still heard.
    Stops after SILENCE_S of silence once speech started; cancels after NO_SPEECH_S without any speech.
    """

    def __init__(self):
        self.noise, self.speech = None, None
        self.first = []
        self.spoke_s = self.silent_s = self.total_s = 0.0
        self.step = CHUNK / RATE

    def threshold(self):
        t = MIN_RMS
        if self.noise is not None:
            t = max(t, self.noise * 2.5)
        if self.speech is not None:
            t = max(t, self.speech * 0.3)
        return t

    def feed(self, rms):
        """-> None to keep recording, "stop" (transcribe) or "cancel"."""
        self.total_s += self.step
        if self.noise is None and len(self.first) < 10:
            self.first.append(rms)
            if len(self.first) == 10:
                self.noise = min(self.first)
        loud = rms > self.threshold()
        if loud:
            self.speech = rms if self.speech is None else self.speech * 0.95 + rms * 0.05
            self.spoke_s += self.step
            self.silent_s = 0.0
        else:
            self.noise = rms if self.noise is None else self.noise * 0.95 + rms * 0.05
            self.silent_s += self.step
        if self.total_s >= MAX_S:
            return "stop"
        if self.spoke_s >= 0.3 and self.silent_s >= SILENCE_S:
            return "stop"
        if self.spoke_s < 0.3 and self.total_s >= NO_SPEECH_S:
            return "cancel"
        return None


def pcm_rms(buf):
    import array
    a = array.array("h", buf[: len(buf) // 2 * 2])
    return math.sqrt(sum(x * x for x in a) / len(a)) if a else 0.0


def record(raw, peaks, det, outcome, done):
    """Records the default device (arecord) into `raw` and `peaks` until `det` says stop/cancel (-> `outcome`)."""
    proc = subprocess.Popen(["arecord", "-q", "-f", "S16_LE", "-r", str(RATE), "-c", "1", "-t", "raw",
                             ], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    while True:
        buf = proc.stdout.read(CHUNK * 2)
        if not buf:
            break
        raw.extend(buf)
        peaks.append(pcm_peak(buf))
        outcome[0] = det.feed(pcm_rms(buf))
        if outcome[0]:
            break
    proc.kill()
    proc.wait()
    done.set()


def draw(text):
    sys.stdout.write(text)
    sys.stdout.flush()


def docker_ready():
    """True when the ROCm image is built (docker/whisper-rocm/Dockerfile) and docker answers."""
    try:
        return subprocess.run(["docker", "image", "inspect", DOCKER_IMAGE], stdin=subprocess.DEVNULL,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5).returncode == 0
    except Exception:
        return False


def start_stt():
    """Picks the STT backend and starts what can start early. -> (backend, proc, error).
    "docker": whisper.cpp on the GPU (ROCm) in a container, no network; runs after recording.
    "venv": faster-whisper on the CPU; starts now so the model loads while the user records."""
    if os.environ.get("AT_WHISPER_BACKEND", "auto") in ("auto", "docker") and docker_ready() \
            and os.path.exists(GGML_MODEL):
        return "docker", None, None
    py = os.path.join(VENV, "bin", "python")
    if not os.path.exists(py):
        return None, None, i18n.t("mic.no_venv", venv=VENV)
    return "venv", subprocess.Popen([py, STT], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, text=False), None


def transcribe_docker(raw):
    with tempfile.TemporaryDirectory(prefix="at-mic-") as d:
        with wave.open(os.path.join(d, "a.wav"), "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(RATE)
            w.writeframes(bytes(raw))
        os.chmod(d, 0o755)
        r = subprocess.run(
            ["docker", "run", "--rm", "--network", "none", "--device", "/dev/kfd", "--device", "/dev/dri",
             "--group-add", "video", "--group-add", "render", "-v", f"{d}:/audio:ro",
             "-v", f"{os.path.dirname(GGML_MODEL)}:/models:ro", DOCKER_IMAGE,
             "-m", "/models/" + os.path.basename(GGML_MODEL), "-f", "/audio/a.wav", "-l", LANGUAGE,
             "-nt", "-np", "-bs", "1", "-fa"],
            capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if r.returncode:
        return None, (r.stderr.strip().splitlines() or ["?"])[-1]
    return " ".join(r.stdout.split()), None


def transcribe(handle, raw):
    backend, proc = handle
    if backend == "docker":
        return transcribe_docker(raw)
    out, err = proc.communicate(bytes(raw))
    if proc.returncode:
        return None, (err.decode(errors="replace").strip().splitlines() or ["?"])[-1]
    return out.decode().strip(), None


def run(window):
    if not shutil.which("arecord"):
        print(i18n.t("mic.no_arecord"))
        time.sleep(2)
        return
    raw, peaks, done, outcome, det = bytearray(), [], threading.Event(), [None], SilenceDetector()
    draw("\x1b[?1049h\x1b[?25l")
    try:
        cols, rows = shutil.get_terminal_size((80, 24))
        backend, stt, stt_err = start_stt()
        t0 = time.time()
        while time.time() - t0 < READY_S:                      # red: get ready
            draw(frame(BG_RED, cols, rows, sine_amps(BOX_W, time.time() - t0), i18n.t("mic.ready")))
            time.sleep(0.04)
        threading.Thread(target=record, args=(raw, peaks, det, outcome, done), daemon=True).start()
        t0 = time.time()
        while not done.is_set():                               # green: live waveform
            cols, rows = shutil.get_terminal_size((80, 24))
            amps = peaks[-BOX_W:]
            title = i18n.t("mic.recording", s=f"{time.time() - t0:.0f}") if det.spoke_s < 0.3 else \
                i18n.t("mic.recording_silence", s=f"{max(0.0, SILENCE_S - det.silent_s):.1f}")
            draw(frame(BG_GREEN, cols, rows, amps, title))
            time.sleep(0.04)
        result = {}
        if outcome[0] == "cancel":
            stt and stt.kill()
            result["err"] = i18n.t("mic.no_speech")
            worker = threading.Thread(target=lambda: None)
        elif backend is None:
            result["err"] = stt_err
            worker = threading.Thread(target=lambda: None)
        else:
            worker = threading.Thread(target=lambda: result.update(zip(("text", "err"), transcribe((backend, stt), raw))),
                                      daemon=True)
        worker.start()
        t0 = time.time()
        while worker.is_alive():                               # blue: transcribing
            cols, rows = shutil.get_terminal_size((80, 24))
            draw(frame(BG_BLUE, cols, rows, sine_amps(BOX_W, time.time() - t0), i18n.t("mic.transcribing")))
            time.sleep(0.08)
    finally:
        draw("\x1b[?25h\x1b[?1049l")
    if result.get("err"):
        print(f"\n {i18n.t('mic.error', e=result['err'])}")
        time.sleep(3)
    elif result.get("text"):
        subprocess.run([lib.WS, "type", window, result["text"]], check=False,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    if len(sys.argv) > 1:
        run(sys.argv[1])
