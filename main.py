"""
Pretty Good AI — Patient Voice Bot
===================================
Calls +1-805-439-8008, behaves as a realistic patient, records the call,
and generates a QA bug report for every scenario.

Usage:
    python main.py              # runs scenario_01 (first in SCENARIOS list)
    python main.py 3            # runs scenario_03 (1-indexed)
    python main.py all          # runs every scenario sequentially
"""

import os
import sys
import time
import threading
import requests
from twilio.rest import Client
from twilio.twiml.voice_response import VoiceResponse, Gather
from flask import Flask, request, send_from_directory, make_response
from pyngrok import ngrok
from dotenv import load_dotenv

from brain import get_patient_response
from speaker import text_to_speech
from conversation import ConversationState
from scenario_runner import save_transcript, download_recording
from bug_detector import analyze_transcript, save_scenario_report
from scenarios import SCENARIOS

load_dotenv()

# ================================================================
# Setup
# ================================================================

app = Flask(__name__)

twilio_client = Client(
    os.getenv("TWILIO_ACCOUNT_SID"),
    os.getenv("TWILIO_AUTH_TOKEN"),
)

TWILIO_PHONE = os.getenv("TWILIO_PHONE_NUMBER")
TARGET_PHONE = os.getenv("TARGET_PHONE")

# Global state — safe because calls are run sequentially, never concurrently
current_state: ConversationState = None
public_url: str = None
all_analyses: list = []


# ================================================================
# Webhook Routes
# ================================================================

@app.route("/call/start", methods=["POST"])
def call_start():
    """
    Fires the instant the call connects.

    The agent plays a bilingual greeting (~8-9 seconds):
      "How can I help you today? / ¿Cómo puedo ayudarle hoy?"

    We listen silently — no audio from our side yet.
    When 1 second of silence is detected the Gather fires and sends
    the captured greeting text to /call/greeting, which calls the LLM
    and generates a real, contextual patient response.

    Timeline:
      t=0    call connects → Gather starts listening
      t=0-8  agent plays bilingual greeting
      t=9    1s silence → POST to /call/greeting
      t=9+   LLM + TTS → bot speaks its first real response
    """
    response = VoiceResponse()

    # Listen for the agent's greeting; do NOT play anything from our side
    gather = Gather(
        input="speech",
        timeout=15,
        speech_timeout="auto",
        action="/call/greeting",
        method="POST",
    )
    response.append(gather)

    # Fallback if the Gather timeout fires with no speech detected
    response.redirect("/call/greeting", method="POST")
    return str(response)


@app.route("/call/greeting", methods=["POST"])
def call_greeting():
    """
    Receives the agent's opening greeting, reasons about it with the LLM,
    and speaks a natural patient response — no pre-scripted audio.

    This is turn 0. From here the call enters the normal /call/respond loop.
    """
    global current_state

    agent_greeting = request.form.get("SpeechResult", "").strip()
    call_sid       = request.form.get("CallSid", "")

    # Stale-call guard
    if current_state and current_state.call_sid and call_sid != current_state.call_sid:
        resp = VoiceResponse()
        resp.hangup()
        return str(resp)

    if agent_greeting:
        current_state.add_agent(agent_greeting)
        print(f"[GREETING] {agent_greeting}")

    # Generate a real patient response via LLM — same path as all other turns
    t0 = time.time()
    bot_reply = get_patient_response(
        conversation_history=current_state.history,
        patient_context=current_state.scenario["goal"],
        patient_profile=current_state.scenario["patient_profile"],
    )
    t1 = time.time()

    current_state.add_bot(bot_reply)

    audio_file = text_to_speech(bot_reply, "turn_greet")
    t2 = time.time()

    print(f"[LATENCY] Greeting | LLM {t1 - t0:.2f}s | TTS {t2 - t1:.2f}s")

    filename = os.path.basename(audio_file)

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        timeout=8,
        speech_timeout="auto",
        action="/call/respond",
        method="POST",
    )
    gather.play(f"{public_url}/audio/{filename}")
    response.append(gather)

    response.redirect("/call/respond", method="POST")
    return str(response)


@app.route("/call/respond", methods=["POST"])
def call_respond():
    """
    Fires every time the agent finishes speaking.
    Latency breakdown logged at each stage: STT done → LLM → TTS → response sent.
    """
    global current_state, all_analyses

    t_request = time.time()

    # Twilio provides STT result from its built-in recognizer
    agent_said  = request.form.get("SpeechResult", "").strip()
    confidence  = request.form.get("Confidence", "?")
    call_sid    = request.form.get("CallSid", "")

    # Ignore stale webhook calls from a previous scenario's call
    if current_state and current_state.call_sid and call_sid != current_state.call_sid:
        print(f"[WARN] Stale request from {call_sid}, hanging up")
        resp = VoiceResponse()
        resp.hangup()
        return str(resp)

    if not agent_said:
        print("[WARN] Empty STT result — re-listening")
        response = VoiceResponse()
        gather = Gather(
            input="speech",
            timeout=8,
            speech_timeout="auto",
            action="/call/respond",
            method="POST",
        )
        response.append(gather)
        return str(response)

    print(f"[STT]   confidence={confidence}")
    current_state.turn += 1
    current_state.add_agent(agent_said)

    # ── Natural call-end detection ─────────────────────────────
    if current_state.should_end(agent_said):
        farewell = "Okay, thanks so much for your help. Have a great one, bye!"
        current_state.add_bot(farewell)

        # Persist transcript and run QA before hanging up
        save_transcript(current_state, current_state.scenario["id"])
        analysis = analyze_transcript(
            current_state.transcript_lines,
            current_state.scenario,
        )
        all_analyses.append(analysis)
        save_scenario_report(analysis)  # one file per scenario

        audio_file = text_to_speech(farewell, "turn_farewell")
        filename   = os.path.basename(audio_file)

        response = VoiceResponse()
        gather = Gather(
            input="speech",
            timeout=4,
            speech_timeout="auto",
            action="/call/respond",
            method="POST",
        )
        gather.play(f"{public_url}/audio/{filename}")
        response.append(gather)
        response.hangup()
        return str(response)

    # ── Generate patient reply ─────────────────────────────────
    t_llm_start = time.time()
    bot_reply = get_patient_response(
        conversation_history=current_state.history,
        patient_context=current_state.scenario["goal"],
        patient_profile=current_state.scenario["patient_profile"],
    )
    t_llm_end = time.time()

    current_state.add_bot(bot_reply)

    t_tts_start = time.time()
    audio_file = text_to_speech(bot_reply, f"turn_{current_state.turn:02d}")
    t_tts_end  = time.time()

    filename = os.path.basename(audio_file)

    # Log full latency breakdown for every turn
    print(
        f"[LATENCY] Turn {current_state.turn:02d} | "
        f"LLM {t_llm_end - t_llm_start:.2f}s | "
        f"TTS {t_tts_end - t_tts_start:.2f}s | "
        f"Total {t_tts_end - t_request:.2f}s"
    )

    response = VoiceResponse()
    gather = Gather(
        input="speech",
        timeout=8,
        speech_timeout="auto",
        action="/call/respond",
        method="POST",
    )
    gather.play(f"{public_url}/audio/{filename}")
    response.append(gather)

    return str(response)


@app.route("/audio/<filename>")
def serve_audio(filename):
    """
    Serve mp3 audio to Twilio with correct MIME type.
    All turn_ files are deleted 5s after serving to avoid stale audio
    accumulating between scenarios.
    """
    filepath = os.path.join("recordings", filename)

    def _delete_later():
        time.sleep(5)
        if os.path.exists(filepath) and "turn_" in filename:
            try:
                os.remove(filepath)
            except OSError:
                pass

    threading.Thread(target=_delete_later, daemon=True).start()

    resp = make_response(send_from_directory("recordings", filename))
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


# ================================================================
# Call Control
# ================================================================

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
        recording_channels="dual",       # captures both sides separately
        recording_status_callback=f"{public_url}/recording/done",
        recording_status_callback_method="POST",
    )

    current_state.call_sid = call.sid
    print(f"[CALL] SID: {call.sid}")
    return call.sid


def wait_for_completion(call_sid: str, timeout: int = 420) -> str:
    """Poll Twilio every 8 seconds until the call reaches a terminal state."""
    print("\n[WAITING] Call in progress...")
    start = time.time()
    while time.time() - start < timeout:
        status = twilio_client.calls(call_sid).fetch().status
        print(f"[STATUS]  {status}")
        if status in ("completed", "failed", "busy", "no-answer", "canceled"):
            return status
        time.sleep(8)
    return "timeout"


# ================================================================
# Entry Point
# ================================================================

if __name__ == "__main__":

    # ── Parse CLI argument ─────────────────────────────────────
    # python main.py          → run scenario 1
    # python main.py 3        → run scenario 3
    # python main.py all      → run all scenarios
    arg = sys.argv[1] if len(sys.argv) > 1 else "1"

    if arg.lower() == "all":
        scenarios_to_run = SCENARIOS
    else:
        try:
            idx = int(arg) - 1
            scenarios_to_run = [SCENARIOS[idx]]
        except (ValueError, IndexError):
            print(f"[ERROR] Invalid scenario: '{arg}'. Use a number 1-{len(SCENARIOS)} or 'all'.")
            sys.exit(1)

    # ── Start ngrok tunnel ─────────────────────────────────────
    print("\n[SETUP] Starting ngrok tunnel...")
    ngrok.set_auth_token(os.getenv("NGROK_AUTHTOKEN"))
    public_url = ngrok.connect(5000).public_url
    print(f"[SETUP] Public URL: {public_url}")

    # ── Start Flask server ─────────────────────────────────────
    flask_thread = threading.Thread(
        target=lambda: app.run(
            host="0.0.0.0",
            port=5000,
            debug=False,
            use_reloader=False,
        )
    )
    flask_thread.daemon = True
    flask_thread.start()
    time.sleep(2)

    try:
        requests.get(f"{public_url}/health", timeout=5)
        print("[SETUP] Server healthy ✓\n")
    except Exception as e:
        print(f"[SETUP] Warning: {e} — proceeding anyway")
        time.sleep(2)

    # ── Run scenarios ──────────────────────────────────────────
    for i, scenario in enumerate(scenarios_to_run, 1):
        print(f"\n{'='*60}")
        print(f"[RUN {i}/{len(scenarios_to_run)}]  {scenario['name']}")
        print(f"{'='*60}")

        call_sid = make_call(scenario)
        status   = wait_for_completion(call_sid)
        print(f"\n[CALL ENDED] {status}")

        download_recording(twilio_client, call_sid, scenario["id"])

        # Pause between scenarios — lets Twilio finish callbacks cleanly
        if i < len(scenarios_to_run):
            print(f"\n[PAUSE] 20s before next scenario...")
            time.sleep(20)

    # ── Final summary ──────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"✅  ALL DONE — {len(scenarios_to_run)} scenario(s) complete")
    print(f"{'='*60}")
    for s in scenarios_to_run:
        print(f"  📄  transcripts/{s['id']}.txt")
        print(f"  🎵  recordings/{s['id']}.mp3")
    print(f"  🐛  reports/bug_report.md")
    print(f"{'='*60}\n")
