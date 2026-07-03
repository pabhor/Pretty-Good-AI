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

_FILLER_PATTERNS = [
    (re.compile(r'\b[Uu]mm+\b,?\s*'),                                       ''),
    (re.compile(r'\b[Hh]mm+\b,?\s*'),                                       ''),
    (re.compile(r'\b[Uu]hh?\b,?\s*'),                                       ''),
    (re.compile(r'\bokay\.{1,3}\s*', re.I),                                 ''),
    (re.compile(r'\bLet me (see|think|check)\.{0,3},?\s*', re.I),          ''),
    (re.compile(r'\bGive me (just )?a moment[^.]*\.\s*', re.I),            ''),
    (re.compile(r'\b(Please hold|Just a moment|One moment)[^.]*\.\s*', re.I), ''),
    (re.compile(r'^\s*—\s*'),                                               ''),
    (re.compile(r'  +'),                                                     ' '),
]


def clean_text_for_voice(text: str) -> str:
    original = text.strip()
    cleaned  = original
    for pattern, replacement in _FILLER_PATTERNS:
        cleaned = pattern.sub(replacement, cleaned)
    cleaned = cleaned.strip()
    # Filler patterns (e.g. a bare "Okay.") can strip a short reply down to
    # nothing even though it was the caller's entire, intentional response —
    # fall back to the uncleaned text rather than producing silent dead air.
    return cleaned or original


def _trim_pcm_silence(data: bytes, rate: int = 8000,
                       threshold: int = 200, margin_ms: int = 10) -> bytes:
    arr = np.frombuffer(data, dtype=np.int16)
    n   = len(arr)
    if n < rate // 20:
        return data

    margin  = (rate * margin_ms) // 1000
    indices = np.where(np.abs(arr) >= threshold)[0]
    if len(indices) == 0:
        return data

    start = int(max(0,     indices[0]  - margin))
    end   = int(min(n - 1, indices[-1] + margin))
    return arr[start : end + 1].tobytes()


def _pcm_to_mulaw8k(pcm: bytes, source_rate: int = 24000) -> bytes:
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

    peak = audioop.max(pcm, 2)
    if 0 < peak < 8000:
        pcm = audioop.mul(pcm, 2, min(1.5, 12000 / peak))

    return audioop.lin2ulaw(pcm, 2)


_cache_lock = threading.Lock()
audio_cache: dict = {}


def _evict_old_entries():
    while True:
        time.sleep(15)
        now = time.time()
        with _cache_lock:
            expired = [k for k, (_, ts) in audio_cache.items() if now - ts > 60]
            for k in expired:
                del audio_cache[k]

            # _common_mulaw_cache is meant to survive a whole call (up to the
            # 600s duration safety net) so repeat "yes"/"okay"/etc. don't
            # re-hit OpenAI mid-call — a 60s TTL like audio_cache above would
            # defeat that. It's naturally small (COMMON_PHRASES x voices), so
            # a long TTL is just a backstop against unbounded growth over a
            # long-running process, not a real memory concern today.
            expired_common = [
                k for k, (_, ts) in _common_mulaw_cache.items() if now - ts > 3600
            ]
            for k in expired_common:
                del _common_mulaw_cache[k]


threading.Thread(target=_evict_old_entries, daemon=True).start()


def get_audio(filename: str):
    with _cache_lock:
        entry = audio_cache.get(filename)
    return entry[0] if entry else None


# Short, high-frequency patient responses are worth caching per-voice so we
# skip the OpenAI TTS round trip entirely on repeat use within a call.
_COMMON_PHRASES = {"yes", "no", "okay", "thank you", "i'm not sure"}
_common_mulaw_cache: dict[tuple[str, str], tuple[bytes, float]] = {}


def text_to_speech_mulaw(text: str, voice: str = "nova") -> bytes | None:
    text = clean_text_for_voice(text)
    if not text:
        return None

    normalized = text.strip().lower().rstrip(".!?")
    is_common  = normalized in _COMMON_PHRASES
    cache_key  = (voice, normalized)

    if is_common:
        with _cache_lock:
            entry = _common_mulaw_cache.get(cache_key)
        if entry is not None:
            print(f"[TTS] cache hit '{voice}' '{text}'")
            return entry[0]

    t0 = time.time()
    try:
        response = _openai.audio.speech.create(
            model="tts-1",
            voice=voice,
            input=text,
            response_format="pcm",
        )
        pcm_24k = response.content
    except Exception as e:
        print(f"[TTS ERROR] OpenAI: {e}")
        return None

    mulaw   = _pcm_to_mulaw8k(pcm_24k, source_rate=24000)
    elapsed = time.time() - t0
    print(f"[TTS] tts-1 '{voice}'  {elapsed:.2f}s  "
          f"{len(pcm_24k):,} B pcm24k -> {len(mulaw):,} B mulaw8k")

    if is_common and mulaw:
        with _cache_lock:
            _common_mulaw_cache[cache_key] = (mulaw, time.time())

    return mulaw or None


def text_to_speech(text: str, filename: str, voice: str = "nova") -> str | None:
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
    print(f"[TTS] mp3  {elapsed:.2f}s  {key}  ({len(mp3_bytes):,} B)")
    return f"recordings/{key}"
