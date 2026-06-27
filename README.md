# Pretty Good AI — Patient Voice Bot

Automated QA tester for healthcare voice AI agents. A realistic synthetic patient
calls the Pretty Good AI test line, has a natural conversation, and produces a
structured bug report for every call.

---

## Architecture

```
main.py  (Flask + ngrok webhook server)
  ├── brain.py       GPT-4o-mini generates authentic patient replies (streaming)
  ├── speaker.py     OpenAI TTS converts replies to MP3 (tts-1, onyx voice)
  ├── conversation.py  Tracks history, transcript, turn state
  ├── scenarios.py   10 patient scenarios (multi-intent, billing, urgent, etc.)
  ├── bug_detector.py  GPT-4o analyzes transcripts and writes QA report
  └── scenario_runner.py  Saves transcripts + downloads Twilio recordings
```

**Latency pipeline per turn:**
1. Agent speaks → Twilio STT (built-in, ~300ms after 2s silence)
2. POST to `/call/respond` → GPT-4o-mini streaming (~0.5–1.5s)
3. OpenAI TTS-1 generates MP3 (~0.4–1.0s)
4. Flask returns TwiML → Twilio fetches and plays audio

Typical total response latency: **1.2–3s** (down from 3–6s with the original `speech_timeout="auto"`)

---

## Setup

### 1. Install dependencies

```bash
cd "pretty-good-ai-bot"
python -m venv venv
venv\Scripts\activate        # Windows
pip install -r ../requirements.txt
```

### 2. Configure environment

```bash
copy .env.example .env
# Edit .env with your real credentials
```

Required credentials:
- **Twilio** — account SID, auth token, phone number (must be capable of outbound calls)
- **OpenAI** — API key (used for GPT-4o-mini LLM + TTS)
- **Deepgram** — API key (post-call transcription, optional for live runs)
- **ngrok** — auth token (free tier works, but the tunnel resets between runs)

### 3. Run the bot

```bash
# From the project root (where main.py lives)
cd "c:\Users\prabh\OneDrive\Desktop\Code\Create Gen AI Projects\Pretty AI"

# Activate venv
pretty-good-ai-bot\venv\Scripts\activate

# Run one scenario (scenario 1 is the default)
python main.py

# Run a specific scenario (1-indexed)
python main.py 3

# Run all 10 scenarios sequentially
python main.py all
```

---

## Scenarios

| # | Name | Key Test |
|---|------|----------|
| 01 | Multi-Intent Stress Test | Multi-intent, patient switch, data correction |
| 02 | Insurance Correction Loop | Multiple insurance corrections, multi-intent |
| 03 | Urgent Symptom Triage | Clinical escalation, ER referral |
| 04 | Medication Refill — Partial Info | Partial drug info, pharmacy update |
| 05 | Appointment Time Confusion | Appointment lookup, schedule accuracy |
| 06 | New Patient Registration | Onboarding, incomplete insurance info |
| 07 | Proxy Booking — Elderly Parent | Third-party auth, dual insurance |
| 08 | Billing Dispute | Billing routing, frustrated patient |
| 09 | After-Hours Medication Safety | Safety escalation, penicillin allergy |
| 10 | PCP Change + Address Update | Provider change, multi-location confusion |

---

## Output Files

| Path | Contents |
|------|----------|
| `transcripts/scenario_NN.txt` | Full turn-by-turn call transcript |
| `recordings/scenario_NN.mp3` | Dual-channel call recording (both sides) |
| `reports/bug_report.md` | Structured QA analysis for all completed scenarios |

---

## Latency Logging

Every turn prints a breakdown:

```
[LATENCY] LLM    0.82s  →  Oh wait — actually, we switched insurance...
[LATENCY] TTS    0.61s  →  recordings/turn_03.mp3
[LATENCY] Turn 03 | LLM 0.82s | TTS 0.61s | Total 1.44s
```

This makes it easy to identify whether LLM or TTS is the bottleneck on any given turn.

---

## Key Design Decisions

**`speech_timeout="2"` instead of `"auto"`**
Twilio's "auto" mode can wait 3–5 seconds after the agent finishes speaking before
sending to the webhook. Fixing it at 2 seconds cuts ~1–3s of dead air per turn.

**History truncated to last 10 messages**
Sending the full conversation history grows context quadratically. Capping at 10
messages (5 turns) gives the LLM enough context without slowing it down.

**max_tokens=80 instead of 120**
Two natural phone sentences fit comfortably in 40–60 tokens. 80 is a safe ceiling
that speeds up generation without cutting responses off.

**Pre-generated opening audio**
The opening line is synthesized before the call is dialed. The patient speaks the
instant the agent picks up — zero first-response latency.

**Bug report saved after every call**
`save_bug_report` is called after each scenario completes. If the runner crashes
mid-way through 10 scenarios, all completed analyses are preserved.

---

## Troubleshooting

**"ngrok tunnel not working"**
Free ngrok tunnels expire. Re-run `python main.py` to get a fresh tunnel.

**"No recording downloaded"**
Twilio takes 30–60s to process recordings. The downloader retries 10 times.
If it still fails, find the recording at console.twilio.com → Recordings.

**"Bot says goodbye too early"**
Check `conversation.py` `should_end()`. The agent must say an explicit farewell
phrase (e.g. "goodbye", "have a great day") or reach turn 15.

**"IDE shows import warnings"**
The packages live in `pretty-good-ai-bot/venv/`. Point your IDE's Python
interpreter to `pretty-good-ai-bot\venv\Scripts\python.exe`.
