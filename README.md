# Pretty Good AI — Patient Voice Bot

Automated QA engine for healthcare voice AI. Calls Athena (the agent under test) as a synthetic patient, conducts a full multi-turn phone conversation, and delivers a structured bug report — no human needed.

---

## What It Does

1. **Dials the target number** via Twilio
2. **Streams audio in real time** — Deepgram Nova-3 transcribes Athena word-by-word with 300ms endpointing
3. **Waits for a full utterance** — utterances lacking terminal punctuation are held for a 700ms grace window so a mid-sentence pause doesn't get dispatched as two turns; barge-in only fires on ≥15 chars of real transcribed text, with a cooldown to avoid clipping
4. **Generates a patient reply** — GPT-4o-mini acting as a realistic patient with full call context, persona leakage filter, and identity front-loading (name + DOB given on the opening line so the first 2-3 turns aren't wasted on verification)
5. **Speaks it back** — OpenAI tts-1 → scipy polyphase FIR resample → G.711 mulaw injected into the call
6. **Repeats until resolution** — 600s runaway safety net, 195s/10-turn minimum guard before the call is allowed to end, farewell detection on top
7. **Writes a QA report** — GPT-4o scores the agent through a 4-layer pipeline: deterministic regex → eligibility gate → LLM judge → fallback

---

## Project Structure

```
main.py                         Entry point
src/
  audio/tts.py                  TTS: OpenAI tts-1 + polyphase PCM→mulaw conversion
  call/handler.py               WebSocket pipeline: Twilio + Deepgram + inject + barge-in
  call/state.py                 Conversation state: history, transcript, turn guards
  intelligence/patient_llm.py   LLM patient brain: GPT-4o-mini + persona leakage filter
  qa/analyzer.py                QA engine: 4-layer analysis + GPT-4o bug report
  runner/scenario_runner.py     Transcript save + recording download
  scenarios/catalog.py          18 patient scenario definitions
output/
  transcripts/                  <scenario_id>.txt  — turn-by-turn call log
  recordings/                   <scenario_id>.mp3  — dual-channel Twilio audio
  reports/                      <scenario_id>_report.txt  — QA analysis
```

---

## Voice Pipeline

```
Twilio (mulaw 8kHz) ──► Deepgram Nova-3 STT ──► fragment grace-hold ──► GPT-4o-mini
                                                                          │
Twilio ◄── 160B mulaw chunks ◄── scipy polyphase FIR ◄── OpenAI tts-1 PCM
```

---

## Setup

```bash
# 1. Create and activate a virtual environment
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # Mac/Linux

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
copy .env.example .env       # then fill in your keys

# 4. Run
python main.py               # scenario 1
python main.py 3             # scenario 3  (1-indexed)
python main.py all           # all 18 scenarios sequentially
```

**Required credentials** — fill into `.env`:

| Key | Service | Where to get it |
|-----|---------|-----------------|
| `TWILIO_ACCOUNT_SID` | Twilio | console.twilio.com |
| `TWILIO_AUTH_TOKEN` | Twilio | console.twilio.com |
| `TWILIO_PHONE_NUMBER` | Twilio outbound number | Buy a number in Twilio console |
| `TARGET_PHONE` | Number to call | Set to `+18054398008` for this assessment |
| `OPENAI_API_KEY` | LLM (gpt-4o-mini + gpt-4o) + TTS (tts-1) | platform.openai.com |
| `DEEPGRAM_API_KEY` | Real-time STT (Nova-3) | console.deepgram.com |
| `NGROK_AUTHTOKEN` | Public tunnel for Twilio webhooks | dashboard.ngrok.com |

---

## Scenarios

18 patient scenarios covering the full range of healthcare voice agent interactions:

| # | ID | Scenario | What It Tests |
|---|----|----------|---------------|
| 01 | `mult_appt_01` | Multi-Intent Stress Test | Multi-request call: scheduling + refill in one conversation |
| 02 | `ins_loop_02` | Insurance Correction Loop | Insurance data update, verification loop detection |
| 03 | `urgt_symp_03` | Urgent Symptom Triage | Chest tightness same-day request, ER escalation path |
| 04 | `med_rfil_04` | Medication Refill — Stale Records | Refill with outdated pharmacy on file |
| 05 | `appt_time_05` | Appointment Time Confusion | Conflicting appointment records, schedule accuracy |
| 06 | `new_pat_06` | New Patient Registration | Onboarding with incomplete insurance information |
| 07 | `prxy_bkng_07` | Proxy Booking — Elderly Parent | Third-party booking, dual insurance, POA |
| 08 | `bill_disp_08` | Billing Dispute | Frustrated patient contesting a charge |
| 09 | `aftr_safe_09` | After-Hours Medication Safety | Penicillin allergy + amoxicillin prescription — escalation |
| 10 | `pcp_chng_10` | PCP Change + Address Update | Provider change with simultaneous address correction |
| 11 | `prior_auth_11` | Prior Authorization Hold | PA delay blocking a scheduled procedure |
| 12 | `lab_rslt_12` | Lab Results Anxiety | Patient interpreting their own abnormal lab values |
| 13 | `post_surg_13` | Post-Surgical Wound Concern | Post-op wound question with potential infection signs |
| 14 | `spec_ref_14` | Specialist Referral Maze | Referral chain confusion across multiple providers |
| 15 | `sun_trap_15` | Sunday Appointment Trap | Agent books a Sunday slot when office is closed |
| 16 | `phi_priv_16` | Family PHI Privacy Test | Caller asking for another patient's information |
| 17 | `rfil_urg_17` | Urgent Last Pill Refill | Controlled substance refill, last dose tonight |
| 18 | `vague_req_18` | Vague Request — Clarification Test | Under-specified symptom, agent must clarify |

---

## QA Report Format

Each call produces a structured report:

```
DETERMINISTIC CHECKS    — regex-based rule violations (6 rules, always run)
ELIGIBILITY GATE        — did the call cover enough ground to evaluate?
LLM ANALYSIS            — GPT-4o judges 5 dimensions with evidence quotes
  • Understanding        • Accuracy     • Safety
  • Resolution           • Communication
CRITICAL BUGS           — patient safety or complete failure
HIGH / MEDIUM BUGS      — primary request unresolved, wrong data given
PASSED CRITERIA         — what worked and why
QUALITY SCORE           — 1–10
EXECUTIVE SUMMARY       — 3 sentences for standup
```

The 6 deterministic rules catch: unsafe diagnosis (CRITICAL), missing urgent escalation (CRITICAL), Sunday scheduling (HIGH), refill overpromise (HIGH), insurance overconfidence (HIGH), and repeated identity loops (MEDIUM/HIGH).

---

## Engineering Notes

**Identity front-loading** — Every scenario opens with the patient giving their full name and date of birth in the first sentence. The previous approach wasted 2-3 turns on identity verification before the patient could state their reason for calling. The LLM is instructed not to re-introduce itself if the agent asks again.

**Debounce over instant barge-in** — Athena's VAD fires `SpeechStarted` after ~30% of a sentence. Responding immediately cut every agent turn short. Barge-in only fires once ≥15 chars of real transcribed text land (`_MIN_BARGE_TEXT_CHARS`), not on raw voice-activity. Separately, Deepgram's 300ms endpointing can still fire `speech_final` on a mid-sentence breath/comma pause, finalizing a fragment instead of the full sentence — anything that doesn't end in terminal punctuation is held for a 700ms grace window (`_FRAGMENT_GRACE_S`) so a same-utterance continuation merges into one turn instead of being dispatched (and replied to) twice.

**Fixed opening delay, not a "wait for quiet" detector** — The bot always speaks first at exactly `_OPENING_DELAY_S` (11s) from call start, matching how long Athena's recorded intro consistently runs. An earlier version tried to detect "the clinic just went quiet" from STT signals to respond the instant she stopped talking, but Deepgram's raw voice-activity events fire on line noise/echo with no real words required — on an open phone line, "quiet enough" could never be reliably detected, so the bot ended up waiting out the clinic's entire no-input gap before her own reprompt produced real transcribed speech. A plain fixed timer has no such failure mode: no listening, no detection, nothing for line noise to interfere with.

**scipy polyphase FIR over audioop** — `audioop.ratecv` uses linear interpolation with no anti-aliasing. This folds 4–12 kHz content into the voice band as broadband hiss audible through G.711 compression. `scipy.signal.resample_poly` uses a Kaiser-windowed FIR (~60 dB stopband rejection) that properly filters before decimation, eliminating aliasing noise.

**End-call guards** — Two independent floors prevent premature hangup: minimum 10 turns and minimum 195s elapsed (`handler.py`, `_can_end_call_now`), both required before either the clinic's own farewell phrase or the patient LLM's `[END]` token is allowed to actually end the call. A 600s duration cap (`state.py`, `should_end`) is a separate runaway-call safety net, not a target end time. The patient LLM is also instructed never to say farewell words unless it also appends `[END]` in the same response, preventing goodbye loops.

**4-layer QA pipeline** — Deterministic regex rules run first (zero LLM cost, deterministic), then an eligibility gate rejects calls too short to evaluate, then GPT-4o judges with mandatory evidence quotes, with a graceful fallback if the LLM call fails. Combined reports across all scenarios rank bugs by severity.
