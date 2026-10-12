"""`./ithaca voice`: always-on, fully local "Hey Ithaca" listener.

    Hey Ithaca                          → chime, then listens for the request
    Hey Ithaca, open Notes and write …  → runs it in Ithaca and speaks the answer
    Hey Ithaca, open the app            → just brings Ithaca up
    Hey Ithaca, stop                    → stops the current task

A simple energy-based voice-activity detector cuts speech into utterances;
only those are transcribed (Whisper small.en on the GPU), so it's idle-cheap.
The microphone permission prompt appears the first time (for Terminal).
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import sys
import threading
import time
import urllib.request
from typing import Any

import numpy as np

from . import config, voice

BASE = f"http://127.0.0.1:{config.PORT}"
BLOCK = 1600                         # 100 ms at 16 kHz
SOUNDS = "/System/Library/Sounds"


def _api(method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
    req = urllib.request.Request(
        BASE + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "X-Ithaca": "1", "X-Ithaca-Voice-Token": voice.voice_token()},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _sound(name: str) -> None:
    subprocess.Popen(["afplay", f"{SOUNDS}/{name}.aiff"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class Listener:
    def __init__(self) -> None:
        self.q: queue.Queue[np.ndarray] = queue.Queue()
        self.speaking = threading.Event()
        self.awaiting_command_until = 0.0
        self.current_run: str | None = None

    # ------------------------------------------------------------ audio
    def _callback(self, indata, frames, t, status) -> None:  # sounddevice thread
        if not self.speaking.is_set():
            self.q.put(indata[:, 0].copy())

    def utterances(self):
        """Yield speech segments using an adaptive energy threshold."""
        floor = 0.004
        pre = collections.deque(maxlen=4)
        speech: list[np.ndarray] = []
        loud = quiet = 0
        while True:
            block = self.q.get()
            rms = float(np.sqrt(np.mean(block ** 2)))
            threshold = max(floor * 3.0, 0.012)
            if not speech:
                if rms > threshold:
                    loud += 1
                    pre.append(block)
                    if loud >= 2:
                        speech = list(pre)
                        quiet = 0
                else:
                    loud = 0
                    pre.append(block)
                    floor = 0.95 * floor + 0.05 * rms
                continue
            speech.append(block)
            quiet = quiet + 1 if rms < threshold else 0
            if quiet >= 8 or len(speech) >= 150:      # 0.8 s of silence, or 15 s max
                yield np.concatenate(speech)
                speech, loud, quiet = [], 0, 0
                pre.clear()

    # ------------------------------------------------------------ actions
    def say(self, text: str) -> None:
        if not text:
            return
        self.speaking.set()
        try:
            subprocess.run(["say", "-r", "195", text], check=False)
        finally:
            time.sleep(0.3)
            with self.q.mutex:
                self.q.queue.clear()
            self.speaking.clear()

    def open_ui(self, fragment: str = "chat") -> None:
        subprocess.run(["open", f"{BASE}/#{fragment}"], check=False)

    def handle(self, command: str) -> None:
        low = command.lower().strip(" .!?")
        if not low:
            return
        if low in ("stop", "cancel", "never mind", "nevermind"):
            if self.current_run:
                _api("POST", f"/api/runs/{self.current_run}/cancel", {})
            self.say("Stopped.")
            return
        if low in ("open", "open the app", "open ithaca", "open yourself", "show yourself", "wake up", "open the application"):
            self.open_ui()
            return
        try:
            started = _api("POST", "/api/voice/command", {"text": command})
        except Exception as exc:  # noqa: BLE001
            print(f"  ! could not reach Ithaca at {BASE}: {exc}")
            self.say("I can't reach Ithaca. Is it running?")
            return
        cid, run_id = started["conversation_id"], started["run_id"]
        self.current_run = run_id
        self.open_ui(f"chat/{cid}")
        deadline = time.time() + 900
        while time.time() < deadline:
            time.sleep(1.5)
            res = _api("GET", f"/api/voice/result/{cid}")
            if res.get("pending_approval"):
                self.say("I need your approval in Ithaca to continue.")
                while _api("GET", f"/api/voice/result/{cid}").get("pending_approval"):
                    time.sleep(1.5)
            if res.get("done"):
                self.current_run = None
                self.say(res.get("speech") or "Done.")
                return
        self.current_run = None

    # ------------------------------------------------------------ main loop
    def run(self) -> None:
        import sounddevice as sd

        if not voice.available():
            sys.exit("Whisper model not found. Run: ./ithaca model whisper")
        print("Loading speech model…", flush=True)
        voice.transcribe_array(np.zeros(voice.SAMPLE_RATE, dtype=np.float32))  # warm up
        print('Listening. Say "Hey Ithaca, …"  (Ctrl+C to quit)', flush=True)
        with sd.InputStream(samplerate=voice.SAMPLE_RATE, channels=1, dtype="float32", blocksize=BLOCK,
                            callback=self._callback):
            for segment in self.utterances():
                text = voice.transcribe_array(segment, prompt=voice.WAKE_PROMPT)
                if not text:
                    continue
                if time.time() < self.awaiting_command_until:
                    self.awaiting_command_until = 0
                    print(f"  → {text}", flush=True)
                    _sound("Pop")
                    threading.Thread(target=self.handle, args=(text,), daemon=True).start()
                    continue
                woke, command = voice.parse_wake(text)
                if not woke:
                    continue
                print(f"  ✓ heard: {text}", flush=True)
                if command:
                    _sound("Pop")
                    threading.Thread(target=self.handle, args=(command,), daemon=True).start()
                else:
                    _sound("Tink")
                    self.awaiting_command_until = time.time() + 8


def main() -> None:
    try:
        Listener().run()
    except KeyboardInterrupt:
        print("\nBye.")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    main()
