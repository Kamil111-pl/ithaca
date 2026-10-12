"""Local speech-to-text (Whisper on MLX), wake-phrase parsing and the voice token.

Audio never leaves the machine. The voice listener (`./ithaca voice`) talks to
the API with a per-install token stored in data/voice.token (mode 600), so it
works without a browser session.
"""

from __future__ import annotations

import asyncio
import difflib
import io
import os
import re
import secrets
import threading
import wave
from pathlib import Path

import numpy as np

from . import config

WHISPER_DIR = Path(os.environ.get("ITHACA_WHISPER_MODEL", config.ROOT / "model" / "weights" / "whisper"))
SAMPLE_RATE = 16_000
_lock = threading.Lock()

# Priming that helps Whisper spell the name without making it skip the words.
WAKE_PROMPT = "The assistant is called Ithaca."
WAKE_WORDS = ("ithaca", "ithica", "itaca", "ithika", "ithacca", "ithaka", "athaca", "it haka")


def available() -> bool:
    return (WHISPER_DIR / "config.json").exists() or (WHISPER_DIR / "weights.npz").exists() or any(WHISPER_DIR.glob("*.safetensors"))


def transcribe_array(audio: np.ndarray, prompt: str | None = None) -> str:
    """Transcribe mono float32 audio at 16 kHz."""
    import mlx_whisper

    if audio.size < SAMPLE_RATE // 4:
        return ""
    with _lock:  # one transcription at a time on the GPU
        out = mlx_whisper.transcribe(
            audio.astype(np.float32), path_or_hf_repo=str(WHISPER_DIR), language="en",
            initial_prompt=prompt, condition_on_previous_text=False, fp16=True,
        )
    return " ".join(str(out.get("text", "")).split())


async def transcribe_wav(data: bytes) -> str:
    audio = decode_wav(data)
    return await asyncio.to_thread(transcribe_array, audio)


def decode_wav(data: bytes) -> np.ndarray:
    with wave.open(io.BytesIO(data)) as w:
        rate, width, channels = w.getframerate(), w.getsampwidth(), w.getnchannels()
        frames = w.readframes(w.getnframes())
    if width != 2:
        raise ValueError("Expected 16-bit PCM WAV.")
    audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    if channels > 1:
        audio = audio.reshape(-1, channels).mean(axis=1)
    if rate != SAMPLE_RATE:  # linear resample is fine for speech
        n = int(len(audio) * SAMPLE_RATE / rate)
        audio = np.interp(np.linspace(0, len(audio) - 1, n), np.arange(len(audio)), audio).astype(np.float32)
    return audio


GREETINGS = {"hey", "hi", "hello", "okay", "ok", "yo", "oh"}


def parse_wake(text: str) -> tuple[bool, str]:
    """('Hey Ithaca, open Notes.') → (True, 'open Notes.'). Tolerates Whisper's spellings."""
    clean = text.strip()
    tokens = [(m.group().lower(), m.end()) for m in re.finditer(r"[A-Za-z']+", clean)]
    for i in range(min(3, len(tokens))):
        if any(tok not in GREETINGS for tok, _ in tokens[:i]):
            break
        word = tokens[i][0]
        end = None
        fused = re.sub(r"^(?:hey|hay|hei|hi|a)", "", word) if i == 0 else word   # "Hayethika", "Heyithaca"
        if (word in WAKE_WORDS or difflib.get_close_matches(word, WAKE_WORDS, n=1, cutoff=0.8)
                or (fused != word and difflib.get_close_matches(fused, WAKE_WORDS, n=1, cutoff=0.7))):
            end = tokens[i][1]
        elif i + 1 < len(tokens) and f"{word} {tokens[i + 1][0]}" in WAKE_WORDS:
            end = tokens[i + 1][1]
        if end is not None:
            return True, clean[end:].lstrip(" ,.!?-:;").strip()
    return False, ""


def token_path() -> Path:
    return config.DATA_DIR / "voice.token"


def voice_token() -> str:
    p = token_path()
    if p.exists():
        return p.read_text().strip()
    config.ensure_dirs()
    tok = secrets.token_urlsafe(32)
    p.write_text(tok)
    os.chmod(p, 0o600)
    return tok


def speakable(markdown: str, limit: int = 600) -> str:
    """Plain text suitable for text-to-speech."""
    t = re.sub(r"```.*?```", " (code omitted) ", markdown, flags=re.S)
    t = re.sub(r"`([^`]*)`", r"\1", t)
    t = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", t)
    t = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", t)
    t = re.sub(r"^[#>\-*\s]+", "", t, flags=re.M)
    t = re.sub(r"[*_~|]", "", t)
    t = " ".join(t.split())
    if len(t) > limit:
        cut = t[:limit]
        t = cut[: cut.rfind(". ") + 1] if ". " in cut else cut + "…"
    return t
