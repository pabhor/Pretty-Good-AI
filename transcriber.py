from deepgram import DeepgramClient, PrerecordedOptions
import os
from dotenv import load_dotenv

load_dotenv()


def transcribe_audio(audio_url: str) -> str:
    """
    Transcribe full call recording from URL.
    Uses Deepgram Nova-2 — most accurate model.
    Diarization enabled — identifies separate speakers.
    Returns full transcript text.
    """
    try:
        deepgram = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))

        options = PrerecordedOptions(
            model="nova-2",
            smart_format=True,
            language="en-US",
            punctuate=True,
            diarize=True,           # identifies different speakers
            utterances=True         # breaks into natural utterances
        )

        source   = {"url": audio_url}
        response = deepgram.listen.prerecorded.v("1").transcribe_url(
            source, options
        )

        transcript = (
            response.results
            .channels[0]
            .alternatives[0]
            .transcript
        )

        print(f"[TRANSCRIBER] Complete: {transcript[:80]}...")
        return transcript

    except Exception as e:
        print(f"[TRANSCRIBER] Error: {e}")
        return ""