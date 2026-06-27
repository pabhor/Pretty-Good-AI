import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()


def save_transcript(state, scenario_id: str) -> str:
    """
    Write the full conversation transcript to transcripts/<scenario_id>.txt.
    Includes metadata header: scenario name, date, duration, turn count, call SID.
    """
    os.makedirs("transcripts", exist_ok=True)
    filepath = f"transcripts/{scenario_id}.txt"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"SCENARIO: {state.scenario['name']}\n")
        f.write(f"DATE:     {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"DURATION: {state.duration()}s\n")
        f.write(f"TURNS:    {state.turn}\n")
        f.write(f"CALL SID: {state.call_sid}\n")
        f.write("=" * 60 + "\n\n")
        for line in state.transcript_lines:
            f.write(line + "\n")

    print(f"[TRANSCRIPT] Saved → {filepath}")
    return filepath


def download_recording(
    twilio_client,
    call_sid: str,
    scenario_id: str,
    max_retries: int = 10,
) -> str:
    """
    Download the dual-channel call recording from Twilio.

    Twilio processes recordings asynchronously — we retry every 10 seconds
    for up to 100 seconds before giving up. The recording URL is logged
    via /recording/done callback in main.py; this function downloads the binary.
    """
    os.makedirs("recordings", exist_ok=True)
    filepath = f"recordings/{scenario_id}.mp3"

    print(f"\n[RECORDING] Waiting for Twilio to process audio...")

    for attempt in range(1, max_retries + 1):
        time.sleep(10)

        try:
            recordings = twilio_client.recordings.list(call_sid=call_sid)

            if not recordings:
                print(f"[RECORDING] Not ready — attempt {attempt}/{max_retries}")
                continue

            recording = recordings[0]
            url = (
                "https://api.twilio.com"
                + recording.uri.replace(".json", ".mp3")
            )

            resp = requests.get(
                url,
                auth=(
                    os.getenv("TWILIO_ACCOUNT_SID"),
                    os.getenv("TWILIO_AUTH_TOKEN"),
                ),
                stream=True,
                timeout=30,
            )

            if resp.status_code == 200:
                with open(filepath, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        f.write(chunk)

                size = os.path.getsize(filepath)
                print(f"[RECORDING] Saved → {filepath}  ({size:,} bytes)")
                return filepath

            print(f"[RECORDING] HTTP {resp.status_code} — retrying...")

        except Exception as e:
            print(f"[RECORDING] Error on attempt {attempt}: {e}")

    print("[RECORDING] Could not download — check Twilio dashboard manually")
    return None
