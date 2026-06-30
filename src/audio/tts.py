import re
import audioop
import time
import os
import threading
from math import gcd
import numpy as np
from scipy.signal import resample_poly
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

_openai = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# ── Filler-word patterns ──────────────────────────────────────────────────────
_FILLER_PATTERNS = [
    (re.compile(r'\b[Uu]mm+\b,?\s*'),                           ''),
    (re.compile(r'\b[Hh]mm+\b,?\s*'),                           ''),
    (re.compile(r'\b[Uu]hh?\b,?\s*'),                           ''),
    (re.compile(r'\bokay\.{1,3}\s*', re.I),                     ''),
    (re.compile(r'\bLet me (see|think|check)\.{0,3},?\s*', re.I), ''),
    (re.compile(r'^\s*—\s*'),                                    ''),
    (re.compile(r'  +'),                                         ' '),
]


def clean_text_for_voice(text: str) -> str:
    """Strip hesitant filler sounds before TTS."""
    for pattern, replacement in _FILLER_PATTERNS:
        text = pattern.sub(replacement, text)
    return text.strip()


# ── PCM post-processing ───────────────────────────────────────────────────────

def _trim_pcm_silence(data: bytes, rate: int = 8000,
                       threshold: int = 200, margin_ms: int = 10) -> bytes:
    """
    Trim leading/trailing near-silence from 16-bit signed little-endian PCM.
    OpenAI TTS prepends 50-100ms of silence before the first phoneme — this
    removes it so the bot's voice starts immediately on playback.
    """
    n = len(data) // 2
    if n < rate // 20:
        return data
    margin = (rate * margin_ms) // 1000

    start = 0
    for i in range(n):
        if abs(int.from_bytes(data[i*2:i*2+2], 'little', signed=True)) >= threshold:
            start = max(0, i - margin)
            break

    end = n - 1
    for i in range(n - 1, -1, -1):
        if abs(int.from_bytes(data[i*2:i*2+2], 'little', signed=True)) >= threshold:
            end = min(n - 1, i + margin)
            break

    return data[start*2 : (end+1)*2] if end >= start else data


def _pcm_to_mulaw8k(pcm: bytes, source_rate: int = 24000) -> bytes:
    """
    Convert any-rate 16-bit mono PCM to trimmed 8 kHz G.711 mulaw.

    Uses scipy polyphase resampler (Kaiser-windowed FIR) instead of
    audioop.ratecv (linear interpolation). The polyphase FIR applies a
    proper anti-aliasing filter before decimation, eliminating the aliasing
    noise that audioop introduced — audible as broadband hiss on mulaw output.
    """
    if not pcm:
        return b''

    if source_rate != 8000:
        arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
        g = gcd(source_rate, 8000)
        resampled = resample_poly(arr, 8000 // g, source_rate // g)
        pcm = np.clip(resampled, -32768, 32767).astype(np.int16).tobytes()

    pcm = _trim_pcm_silence(pcm, rate=8000)
    if not pcm:
        return b''

    # Boost only if genuinely quiet (< 25% of full scale) — cap at 1.5×
    peak = audioop.max(pcm, 2)
    if 0 < peak < 8000:
        pcm = audioop.mul(pcm, 2, min(1.5, 12000 / peak))

    return audioop.lin2ulaw(pcm, 2)


# ── Cache (MP3 fallback path only) ───────────────────────────────────────────

_cache_lock = threading.Lock()
audio_cache: dict = {}   # filename → (bytes, timestamp)


def _evict_old_entries():
    while True:
        time.sleep(15)
        now = time.time()
        with _cache_lock:
            expired = [k for k, (_, ts) in audio_cache.items() if now - ts > 60]
            for k in expired:
                del audio_cache[k]


threading.Thread(target=_evict_old_entries, daemon=True).start()


def get_audio(filename: str):
    """Return cached bytes; None if absent. Non-destructive for Twilio retries."""
    with _cache_lock:
        entry = audio_cache.get(filename)
    return entry[0] if entry else None


# ── TTS public API ────────────────────────────────────────────────────────────

def text_to_speech_mulaw(text: str, voice: str = "nova") -> bytes | None:
    """
    Generate speech via OpenAI tts-1 (24 kHz raw PCM) and convert to
    8 kHz G.711 mulaw ready for Twilio Media Streams injection.

    Why OpenAI tts-1 over Deepgram Aura:
      • Significantly more natural prosody — better breath rhythm, intonation,
        sentence stress — all audible even through G.711 8 kHz compression
      • Consistent volume levelling across short and long utterances
      • tts-1 (not tts-1-hd) — comparable latency to Deepgram (~600-900 ms)
        while sounding noticeably less robotic on a phone call

    Voice choices (set per-scenario in scenarios.py):
      nova    — warm conversational female  (default)
      shimmer — mature/professional female
      onyx    — deep authoritative male
      alloy   — neutral mid-range male
      echo    — younger male
    """
    text = clean_text_for_voice(text)
    if not text:
        return None

    t0 = time.time()
    try:
        response = _openai.audio.speech.create(
            model="tts-1",
            voice=voice,
            input=text,
            response_format="pcm",   # raw 16-bit 24 kHz mono — no container overhead
        )
        pcm_24k = response.content
    except Exception as e:
        print(f"[TTS ERROR] OpenAI: {e}")
        return None

    mulaw = _pcm_to_mulaw8k(pcm_24k, source_rate=24000)
    elapsed = time.time() - t0
    print(f"[TTS] OpenAI tts-1 '{voice}'  {elapsed:.2f}s  "
          f"{len(pcm_24k):,} B pcm24k -> {len(mulaw):,} B mulaw8k")
    return mulaw or None


def text_to_speech(text: str, filename: str, voice: str = "nova") -> str | None:
    """
    Generate MP3 via OpenAI tts-1 and store in cache.
    Used only for the <Play> / /audio/<filename> fallback path.
    """
    text = clean_text_for_voice(text)
    if not text:
        return None

    t0 = time.time()
    try:
        response = _openai.audio.speech.create(
            model="tts-1",
            voice=voice,
            input=text,
            response_format="mp3",
        )
        mp3_bytes = response.content
    except Exception as e:
        print(f"[TTS ERROR] OpenAI mp3: {e}")
        return None

    key = f"{filename}.mp3"
    with _cache_lock:
        audio_cache[key] = (mp3_bytes, time.time())

    elapsed = time.time() - t0
    print(f"[LATENCY] TTS mp3  {elapsed:.2f}s  {key}  ({len(mp3_bytes):,} B)")
    return f"recordings/{key}"
