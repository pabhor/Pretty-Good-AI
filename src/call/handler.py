"""
StreamHandler — Twilio Media Streams + Deepgram real-time STT pipeline.

t=0        WebSocket "start" fires; prefetch begins; greeting timer armed.
t=0-12 s   Athena's greeting. speech_finals discarded (_opening_played=False).
t=12 s     Timer fires _timer_open() -> plays pre-fetched opening line.
t=12 s+    Normal turns: speech_final -> LLM -> TTS -> inject.

Audio forwarding:
  Audio is ALWAYS forwarded to Deepgram — no gate during our playback.
  Athena's utterances during playback are buffered in _pending_utterance
  and fired the instant the response lock releases.

_call_done flag:
  Set the moment _run_farewell starts. Blocks all further responses so
  the farewell never plays more than once.
"""

import os
import json
import base64
import time
import threading

from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

from src.intelligence.patient_llm import get_patient_response
from src.audio.tts import text_to_speech_mulaw
from src.runner.scenario_runner import save_transcript
from src.qa.analyzer import analyze_transcript, save_scenario_report

_GREETING_S      = 12.0
_RESP_DEBOUNCE_S = 1.5    # 500ms Deepgram endpointing + 1500ms debounce = ~2s total silence


class StreamHandler:
    _CHUNK = 160   # 20 ms of mulaw @ 8 kHz

    def __init__(self, ws, state, all_analyses: list):
        self.ws           = ws
        self.state        = state
        self.all_analyses = all_analyses
        self.stream_sid   = None

        self._call_start_time: float          = None
        self._greeting_timer: threading.Timer = None

        self._opening_audio: bytes = None
        self._opening_ready        = threading.Event()
        self._opening_played: bool = False

        self._pending_utterance: str = ""
        self._call_done: bool        = False

        self._resp_lock      = threading.Lock()
        self._transcript_buf: list[str] = []
        self._dg_conn        = None

        self._accumulated: list[str]       = []
        self._resp_timer: threading.Timer  = None
        self._resp_timer_lock              = threading.Lock()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self):
        self._connect_deepgram()
        print(f"\n{'='*60}")
        print(f"[STREAM] {self.state.scenario['name']}")
        print(f"[TIMER]  Opening at t={_GREETING_S:.0f}s")
        print(f"{'='*60}")
        try:
            while True:
                raw = self.ws.receive()
                if raw is None:
                    break
                self._on_twilio(json.loads(raw))
        except Exception as e:
            print(f"[STREAM] Loop ended: {e}")
        finally:
            if self._greeting_timer:
                self._greeting_timer.cancel()
            if self._dg_conn:
                self._dg_conn.finish()
            print("[STREAM] Closed")

    # ------------------------------------------------------------------
    # Deepgram connection
    # ------------------------------------------------------------------

    def _connect_deepgram(self):
        dg = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))
        self._dg_conn = dg.listen.live.v("1")

        def on_transcript(_, result, **kw):
            self._on_transcript(result)

        def on_utterance_end(_, utterance_end, **kw):
            self._on_utterance_end()

        def on_error(_, error, **kw):
            print(f"[DEEPGRAM] Error: {error}")

        self._dg_conn.on(LiveTranscriptionEvents.Transcript,   on_transcript)
        self._dg_conn.on(LiveTranscriptionEvents.UtteranceEnd, on_utterance_end)
        self._dg_conn.on(LiveTranscriptionEvents.Error,        on_error)

        self._dg_conn.start(LiveOptions(
            model="nova-2",
            encoding="mulaw",
            sample_rate=8000,
            language="en-US",
            punctuate=True,
            smart_format=True,
            interim_results=True,
            endpointing=500,
            utterance_end_ms="1500",
            vad_events=True,
        ))

    # ------------------------------------------------------------------
    # Opening-line prefetch
    # ------------------------------------------------------------------

    def _prefetch_opening(self):
        text  = self.state.scenario.get("opening_line", "")
        voice = self.state.scenario["voice"]
        if not text:
            self._opening_ready.set()
            return
        print(f"[PREFETCH] Generating '{voice}' TTS...")
        t0 = time.time()
        audio = text_to_speech_mulaw(text, voice)
        elapsed = time.time() - t0
        if audio:
            self._opening_audio = audio
            print(f"[PREFETCH] Ready {elapsed:.2f}s — {len(audio):,} B "
                  f"({_GREETING_S - elapsed:.1f}s before timer)")
        else:
            print(f"[PREFETCH] TTS failed — will retry at timer")
        self._opening_ready.set()

    # ------------------------------------------------------------------
    # Twilio WebSocket events
    # ------------------------------------------------------------------

    def _on_twilio(self, msg: dict):
        event = msg.get("event")

        if event == "start":
            self.stream_sid       = msg["start"]["streamSid"]
            self._call_start_time = time.time()
            incoming = msg["start"]["callSid"]
            if self.state.call_sid and self.state.call_sid != incoming:
                raise ValueError(f"CallSid mismatch: {incoming}")
            print(f"[STREAM] t=0  sid={self.stream_sid}")
            threading.Thread(target=self._prefetch_opening, daemon=True).start()
            self._greeting_timer = threading.Timer(_GREETING_S, self._timer_open)
            self._greeting_timer.daemon = True
            self._greeting_timer.start()

        elif event == "media":
            self._dg_conn.send(base64.b64decode(msg["media"]["payload"]))

        elif event == "stop":
            raise StopIteration("Twilio stop")

    # ------------------------------------------------------------------
    # Greeting timer
    # ------------------------------------------------------------------

    def _timer_open(self):
        elapsed = time.time() - (self._call_start_time or 0)
        print(f"\n[TIMER] t={elapsed:.2f}s — playing opening line")

        if not self._resp_lock.acquire(blocking=False):
            print(f"[TIMER] Lock busy — skipping")
            return

        self._opening_played = True
        self.state.turn += 1
        self.state.add_agent("How can I help you today?")
        threading.Thread(target=self._run_opening, daemon=True).start()

    # ------------------------------------------------------------------
    # Deepgram STT callbacks
    # ------------------------------------------------------------------

    def _on_transcript(self, result):
        if not result.is_final:
            return
        text = result.channel.alternatives[0].transcript.strip()
        if text:
            self._transcript_buf.append(text)
        if result.speech_final:
            self._handle_speech_final()

    def _on_utterance_end(self):
        self._handle_speech_final()

    def _handle_speech_final(self):
        if self._call_done:
            self._transcript_buf.clear()
            return

        elapsed   = time.time() - (self._call_start_time or 0)
        utterance = " ".join(self._transcript_buf).strip()
        self._transcript_buf.clear()

        if not self._opening_played:
            if utterance:
                print(f"[GATE] t={elapsed:.1f}s discard: \"{utterance[:60]}\"")
            return

        if not utterance:
            return

        with self._resp_timer_lock:
            self._accumulated.append(utterance)
            print(f"[ACCUM] t={elapsed:.1f}s +\"{utterance[:60]}\"")
            if self._resp_timer:
                self._resp_timer.cancel()
            self._resp_timer = threading.Timer(_RESP_DEBOUNCE_S, self._debounce_fire)
            self._resp_timer.daemon = True
            self._resp_timer.start()

    def _debounce_fire(self):
        with self._resp_timer_lock:
            combined = " ".join(self._accumulated).strip()
            self._accumulated.clear()
            self._resp_timer = None

        if not combined or self._call_done:
            return

        elapsed = time.time() - (self._call_start_time or 0)
        print(f"[DEBOUNCE] t={elapsed:.1f}s firing: \"{combined[:80]}\"")

        if not self._resp_lock.acquire(blocking=False):
            self._pending_utterance = (
                self._pending_utterance + " " + combined
                if self._pending_utterance else combined
            )
            print(f"[QUEUE] t={elapsed:.1f}s pending: \"{combined[:60]}\"")
            return

        self._run_turn(combined)

    # ------------------------------------------------------------------
    # Turn router  (called with _resp_lock held)
    # ------------------------------------------------------------------

    def _run_turn(self, agent_text: str):
        self.state.turn += 1
        self.state.add_agent(agent_text)
        elapsed = time.time() - (self._call_start_time or 0)
        print(f"\n[TURN {self.state.turn:02d}] t={elapsed:.1f}s <- \"{agent_text[:80]}\"")

        if self.state.should_end(agent_text):
            threading.Thread(target=self._run_farewell, daemon=True).start()
            return

        history = list(self.state.history)
        threading.Thread(target=self._run_response, args=(history,), daemon=True).start()

    # ------------------------------------------------------------------
    # Response workers  (each holds _resp_lock, releases on exit)
    # ------------------------------------------------------------------

    def _run_opening(self):
        text  = self.state.scenario.get("opening_line", "")
        voice = self.state.scenario["voice"]
        if not text:
            self._resp_lock.release()
            return
        try:
            self.state.add_bot(text)
            print(f"[TURN {self.state.turn:02d}] -> (opening) \"{text[:80]}\"")
            self._opening_ready.wait(timeout=2.0)
            audio = self._opening_audio or text_to_speech_mulaw(text, voice)
            if audio:
                self._play(audio)
            else:
                print("[STREAM] Opening TTS failed")
        except Exception as e:
            print(f"[STREAM] Opening error: {e}")
        finally:
            self._resp_lock.release()
            self._fire_pending()

    def _run_response(self, history: list):
        t0 = time.time()
        try:
            text = get_patient_response(
                conversation_history=history,
                patient_context=self.state.scenario["goal"],
                patient_profile=self.state.scenario["patient_profile"],
            )
            t_llm = time.time()
            self.state.add_bot(text)
            print(f"[TURN {self.state.turn:02d}] -> \"{text[:80]}\"")
            print(f"[TIMING] LLM={t_llm-t0:.2f}s")
            self._speak(text)
            print(f"[TIMING] total={time.time()-t0:.2f}s")
        except Exception as e:
            print(f"[STREAM] Response error: {e}")
        finally:
            self._resp_lock.release()
            self._fire_pending()

    def _run_farewell(self):
        self._call_done = True
        farewell = "Okay, thanks so much for your help. Have a great one, bye!"
        self.state.add_bot(farewell)
        try:
            self._speak(farewell)
        except Exception as e:
            print(f"[STREAM] Farewell error: {e}")
        finally:
            self._resp_lock.release()

        self.state.transcript_saved = True
        save_transcript(self.state, self.state.scenario["id"])
        analysis = analyze_transcript(self.state.transcript_lines, self.state.scenario)
        self.all_analyses.append(analysis)
        save_scenario_report(analysis)
        try:
            self.ws.close()
        except Exception:
            pass

    def _fire_pending(self):
        if self._call_done:
            return
        pending = self._pending_utterance
        if not pending:
            return
        self._pending_utterance = ""

        if not self._resp_lock.acquire(blocking=False):
            self._pending_utterance = pending
            return

        self._run_turn(pending)

    # ------------------------------------------------------------------
    # Audio injection
    # ------------------------------------------------------------------

    def _speak(self, text: str):
        t0 = time.time()
        audio = text_to_speech_mulaw(text, self.state.scenario["voice"])
        if not audio:
            print("[STREAM] TTS None — skip")
            return
        print(f"[TIMING] TTS={time.time()-t0:.2f}s  {len(audio):,} B")
        self._play(audio)

    def _play(self, mulaw_audio: bytes):
        if not mulaw_audio:
            return

        t0    = time.time()
        sent  = 0
        total = len(mulaw_audio) // self._CHUNK
        try:
            for i in range(0, len(mulaw_audio), self._CHUNK):
                self.ws.send(json.dumps({
                    "event":     "media",
                    "streamSid": self.stream_sid,
                    "media":     {"payload": base64.b64encode(
                                     mulaw_audio[i:i+self._CHUNK]).decode()},
                }))
                sent += 1
                time.sleep(0.02)
        except Exception as e:
            print(f"[STREAM] Inject error: {e}")
        finally:
            try:
                self.ws.send(json.dumps({
                    "event":     "mark",
                    "streamSid": self.stream_sid,
                    "mark":      {"name": f"t{self.state.turn}"},
                }))
            except Exception:
                pass
            print(f"[TIMING] play={time.time()-t0:.2f}s  {sent}/{total} chunks")
