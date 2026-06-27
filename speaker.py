import time
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def text_to_speech(text: str, filename: str) -> str:
    """
    Convert text to mp3 audio using OpenAI TTS.
    - tts-1: fastest model (tts-1-hd is higher quality but ~2x slower)
    - onyx: deep, natural-sounding male voice
    - speed 0.95: slightly slower than natural for phone clarity
    Returns the filepath of the saved audio file.
    """
    os.makedirs("recordings", exist_ok=True)
    filepath = f"recordings/{filename}.mp3"

    t0 = time.time()
    response = client.audio.speech.create(
        model="tts-1",
        voice="onyx",
        input=text,
        speed=0.95,
        response_format="mp3",
    )
    response.stream_to_file(filepath)
    elapsed = time.time() - t0

    print(f"[LATENCY] TTS    {elapsed:.2f}s  →  {filepath}")
    return filepath
