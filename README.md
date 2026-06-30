# Pretty Good AI — Patient Voice Bot

Automated QA engine for healthcare voice AI. Calls Athena (the agent under test) as a synthetic patient, conducts a full multi-turn phone conversation, and delivers a structured bug report — no human needed.

---

## What It Does

1. **Dials the target number** via Twilio
2. **Streams audio in real time** — Deepgram Nova-2 transcribes Athena word-by-word
3. **Waits for a full sentence** — 500ms Deepgram endpointing + 2.5s debounce prevents cutting in mid-phrase
4. **Generates a patient reply** — GPT-4.1-nano acting as a realistic patient (1 sentence, full call context)
5. **Speaks it back** — OpenAI tts-1 → scipy polyphase resample → G.711 mulaw injected into the call
6. **Repeats until farewell** — detects goodbye phrases, plays closing line, saves transcript
7. **Writes a QA report** — GPT-4o scores the agent on understanding, accuracy, safety, and resolution

---

## Project Structure

```
main.py                    Entry point
src/
  audio/tts.py             TTS: OpenAI tts-1 + PCM->mulaw conversion
  call/handler.py          WebSocket pipeline: Twilio + Deepgram + inject
  call/state.py            Conversation state: history, transcript, turns
  intelligence/patient_llm.py  LLM patient brain: GPT-4.1-nano
  qa/analyzer.py           QA engine: GPT-4o bug analysis + reports
  runner/scenario_runner.py    Transcript save + recording download
  scenarios/catalog.py    10 patient scenario definitions
output/
  transcripts/             <scenario_id>.txt  — turn-by-turn call log
  recordings/              <scenario_id>.mp3  — dual-channel Twilio audio
  reports/                 <scenario_id>_report.txt  — QA analysis
```

---

## Voice Pipeline

```
Twilio (mulaw 8kHz) -> Deepgram STT -> debounce -> GPT-4.1-nano
                                                         |
Twilio <- 160B mulaw chunks <- scipy resample <- OpenAI tts-1
```

---

## Setup

```bash
# 1. Install
cd pretty-good-ai-bot && python -m venv venv
venv\Scripts\activate
pip install -r ../requirements.txt

# 2. Configure
copy .env.example .env   # fill in credentials

# 3. Run
python main.py           # scenario 1
python main.py 3         # scenario 3
python main.py all       # all 10
```

**Required credentials** — `.env`:

| Key | Service |
|-----|---------|
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` / `TWILIO_PHONE_NUMBER` | Twilio |
| `TARGET_PHONE` | Number to call |
| `OPENAI_API_KEY` | LLM + TTS |
| `DEEPGRAM_API_KEY` | Real-time STT |
| `NGROK_AUTHTOKEN` | Public tunnel |

---

## Scenarios

| # | ID | What It Tests |
|---|----|---------------|
| 01 | `mult_appt_01` | Multi-intent, mid-call corrections, proxy booking |
| 02 | `ins_loop_02` | Insurance update loop, data integrity |
| 03 | `urgt_symp_03` | Urgent symptom triage, ER escalation |
| 04 | `med_rfil_04` | Medication refill with partial information |
| 05 | `appt_time_05` | Appointment confusion, schedule accuracy |
| 06 | `new_pat_06` | New patient onboarding, incomplete insurance |
| 07 | `prxy_bkng_07` | Third-party booking, dual insurance |
| 08 | `bill_disp_08` | Billing dispute, frustrated patient |
| 09 | `aftr_safe_09` | After-hours safety escalation, allergy |
| 10 | `pcp_chng_10` | PCP change, address update, multi-location |

---

## QA Report Format

```
## CRITICAL BUGS     — patient safety or complete failure
## MAJOR BUGS        — primary request unresolved, wrong data used
## MINOR BUGS        — UX issues, awkward phrasing
## PASSED CRITERIA   — what worked and why
## QUALITY SCORE     — 1–10
## EXECUTIVE SUMMARY — 3 sentences for standup
## RECOMMENDED FIXES — prioritised list
```

---

## Engineering Notes

**Debounce over barge-in** — Athena's VAD fires `SpeechStarted` after ~30% of a sentence. Barge-in cut every response short. Instead: accumulate `speech_final` fragments, fire after 2.5s of silence. Covers EHR verification pauses (4-5s) without interrupting.

**scipy resample over audioop** — `audioop.ratecv` is linear interpolation with no anti-aliasing. Folded 4-12kHz content into the voice band as hiss. `scipy.signal.resample_poly` uses a Kaiser-windowed FIR — ~60dB stopband rejection.

**Pending utterance buffer** — Audio always forwarded to Deepgram during playback (no gate). If Athena speaks while we're injecting, her utterance is buffered and fired the moment the response lock releases. Zero dropped turns.
