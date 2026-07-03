import os
import sys
import time
import threading
import requests
from twilio.rest import Client
from flask import Flask, request, make_response
from flask_sock import Sock
from pyngrok import ngrok
from dotenv import load_dotenv

from src.audio.tts import get_audio
from src.call.state import ConversationState
from src.call.handler import StreamHandler
from src.runner.scenario_runner import save_transcript, download_recording
from src.qa.analyzer import analyze_transcript, save_scenario_report, save_bug_report
from src.scenarios.catalog import SCENARIOS

load_dotenv()

app  = Flask(__name__)
sock = Sock(app)

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN"),
)

TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER")
TARGET_PHONE = os.getenv("TARGET_PHONE")

current_state: ConversationState = None
public_url: str                  = None
all_analyses: list               = []


@sock.route("/stream")
def stream(ws):
    global current_state, all_analyses
    handler = StreamHandler(ws, current_state, all_analyses)
    handler.run()


@app.route("/call/start", methods=["POST"])
def call_start():
    ws_url = public_url.replace("https://", "wss://") + "/stream"
    twiml  = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        f'<Connect><Stream url="{ws_url}"/></Connect>'
        "<Hangup/>"
        "</Response>"
    )
    return twiml, 200, {"Content-Type": "text/xml"}


@app.route("/audio/<filename>")
def serve_audio(filename):
    data = get_audio(filename)
    if not data:
        return ("Audio not found", 404)
    resp = make_response(data)
    resp.headers["Content-Type"]  = "audio/mpeg"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@app.route("/health")
def health():
    return {
        "status":   "ok",
        "scenario": current_state.scenario["name"] if current_state else "idle",
        "turn":     current_state.turn if current_state else 0,
    }, 200


@app.route("/recording/done", methods=["POST"])
def recording_done():
    url    = request.form.get("RecordingUrl", "")
    status = request.form.get("RecordingStatus", "")
    print(f"\n[RECORDING] {status} — {url}")
    return ("", 200)


def make_call(scenario: dict) -> str:
    global current_state

    current_state = ConversationState(scenario)

    print(f"\n{'='*60}")
    print(f"[SCENARIO]  {scenario['name']}")
    print(f"[SEVERITY]  {scenario.get('severity', 'High')}")
    print(f"[DIALING]   {TARGET_PHONE}")
    print(f"{'='*60}")

    call = twilio_client.calls.create(
        to=TARGET_PHONE,
        from_=TWILIO_PHONE,
        url=f"{public_url}/call/start",
        method="POST",
        record=True,
        recording_channels="dual",
        recording_status_callback=f"{public_url}/recording/done",
        recording_status_callback_method="POST",
    )

    current_state.call_sid = call.sid
    print(f"[CALL] SID: {call.sid}")
    return call.sid


def wait_for_completion(call_sid: str, timeout: int = 660) -> str:
    # Must exceed state.py's should_end() 600s runaway-call safety cap — it
    # was 420s, which is *shorter* than that cap, so a genuinely long call
    # could get abandoned here (falling through to "timeout", which skips
    # the abnormal-end save below) before the call's own safety net even
    # had a chance to end it gracefully. 660s leaves a 60s buffer for the
    # farewell to actually play out and save after the cap fires.
    print("\n[WAITING] Call in progress...")
    start = time.time()
    while time.time() - start < timeout:
        status = twilio_client.calls(call_sid).fetch().status
        print(f"[STATUS]  {status}")
        if status in ("completed", "failed", "busy", "no-answer", "canceled"):
            if (
                status == "completed"
                and current_state
                and current_state.transcript_lines
                and not current_state.transcript_saved
            ):
                print("[TRANSCRIPT] Abnormal end — saving transcript + bug report")
                save_transcript(current_state, current_state.scenario["id"])
                current_state.transcript_saved = True
                analysis = analyze_transcript(
                    current_state.transcript_lines, current_state.scenario
                )
                all_analyses.append(analysis)
                save_scenario_report(analysis)
            return status
        time.sleep(8)
    return "timeout"


if __name__ == "__main__":

    arg = sys.argv[1] if len(sys.argv) > 1 else "1"

    if arg.lower() == "all":
        scenarios_to_run = SCENARIOS
    else:
        try:
            idx = int(arg) - 1
            scenarios_to_run = [SCENARIOS[idx]]
        except (ValueError, IndexError):
            print(f"[ERROR] Invalid scenario '{arg}'. Use 1-{len(SCENARIOS)} or 'all'.")
            sys.exit(1)

    print("\n[SETUP] Starting ngrok tunnel...")
    ngrok.set_auth_token(os.getenv("NGROK_AUTHTOKEN"))
    public_url = ngrok.connect(5000).public_url
    print(f"[SETUP] Public URL: {public_url}")
    print(f"[SETUP] WebSocket:  {public_url.replace('https://', 'wss:/')}/stream")

    flask_thread = threading.Thread(
        target=lambda: app.run(host="0.0.0.0", port=5000, threaded=True),
        daemon=True,
    )
    flask_thread.start()
    time.sleep(2)

    try:
        requests.get(f"{public_url}/health", timeout=5)
        print("[SETUP] Server healthy\n")
    except Exception as e:
        print(f"[SETUP] Warning: {e} — proceeding anyway")
        time.sleep(2)

    for i, scenario in enumerate(scenarios_to_run, 1):
        print(f"\n{'='*60}")
        print(f"[RUN {i}/{len(scenarios_to_run)}]  {scenario['name']}")
        print(f"{'='*60}")

        call_sid = make_call(scenario)
        status   = wait_for_completion(call_sid)
        print(f"\n[CALL ENDED] {status}")

        download_recording(twilio_client, call_sid, scenario["id"])

        if i < len(scenarios_to_run):
            print(f"\n[PAUSE] 20 s before next scenario...")
            time.sleep(20)

    if len(scenarios_to_run) > 1:
        save_bug_report(all_analyses)

    print(f"\n{'='*60}")
    print(f"DONE — {len(scenarios_to_run)} scenario(s) complete")
    print(f"{'='*60}")
    for s in scenarios_to_run:
        print(f"  output/transcripts/{s['id']}.txt")
        print(f"  output/recordings/{s['id']}.mp3")
        print(f"  output/reports/{s['id']}_report.txt")
    print(f"{'='*60}\n")
