import base64
import json
import os
import threading
import time
from typing import Optional

from deepgram import DeepgramClient, LiveOptions, LiveTranscriptionEvents

from src.audio.tts import text_to_speech_mulaw
from src.intelligence.patient_llm import get_patient_response_streaming
from src.qa.analyzer import analyze_transcript, save_scenario_report
from src.runner.scenario_runner import save_transcript


_OPENING_DELAY_S              = 11.0
_DEFAULT_MIN_CALL_DURATION_S  = 195.0
_DEFAULT_MIN_TURNS_BEFORE_END = 10
_BARGE_CLEAR_COOLDOWN_S       = 0.5
_MIN_BARGE_TEXT_CHARS         = 15
_HOLD_GRACE_S                 = 1.5
_SHORT_UTTERANCE_CHARS        = 12


class StreamHandler:
    _CHUNK = 160

    def __init__(self, ws, state, all_analyses: list):
        self.ws             = ws
        self.state          = state
        self.all_analyses   = all_analyses
        self.stream_sid: Optional[str] = None

        self._call_start_time: Optional[float]  = None
        self._opening_timer: Optional[threading.Timer] = None

        self._opening_audio: Optional[bytes] = None
        self._opening_ready  = threading.Event()
        self._opening_played = False

        self._call_done        = False
        self._farewell_started = False

        self._resp_lock  = threading.Lock()
        self._state_lock = threading.Lock()

        self._transcript_buf: list[str] = []
        self._dg_conn = None

        self._pending_utterance = ""
        self._pending_lock      = threading.Lock()

        self._fragment_buf        = ""
        self._fragment_timer: Optional[threading.Timer] = None
        self._fragment_lock       = threading.Lock()
        self._fragment_generation = 0

        self._is_playing        = False
        self._play_protected    = False
        self._play_start_time   = 0.0
        self._barge_in          = threading.Event()
        self._last_clear_at     = 0.0
        self._ws_lock           = threading.Lock()

        self._speech_final_time: Optional[float] = None

    def run(self):
        self._connect_deepgram()
        print(f"\n{'=' * 60}")
        print(f"[STREAM] {self.state.scenario['name']}")
        print(f"[TIMER] Opening at t={_OPENING_DELAY_S:.0f}s (fixed)")
        print(f"{'=' * 60}")

        try:
            while True:
                raw = self.ws.receive()
                if raw is None:
                    break
                self._on_twilio(json.loads(raw))
        except StopIteration as e:
            print(f"[STREAM] Stop: {e}")
        except Exception as e:
            print(f"[STREAM] Loop ended: {e}")
        finally:
            if self._opening_timer:
                self._opening_timer.cancel()
            if self._fragment_timer:
                self._fragment_timer.cancel()
            if self._dg_conn:
                self._dg_conn.finish()

            # If the call ended via a raw Twilio hangup rather than the bot's
            # own farewell flow, _call_done was never set — any in-flight
            # thread would keep trying to use this now-dead connection, and
            # nothing would save the transcript/report from this side (only
            # main.py's own status-poll fallback would, and only if the call
            # reaches "completed" before its own save runs). transcript_saved
            # guards against double-saving over the normal farewell path.
            if not self._call_done:
                self._call_done = True
                if not self.state.transcript_saved:
                    self._save_outputs_and_close()

            print("[STREAM] Closed")

    def _connect_deepgram(self):
        dg = DeepgramClient(os.getenv("DEEPGRAM_API_KEY"))
        self._dg_conn = dg.listen.live.v("1")

        self._dg_conn.on(
            LiveTranscriptionEvents.Transcript,
            lambda _, result, **kw: self._on_transcript(result),
        )
        self._dg_conn.on(
            LiveTranscriptionEvents.UtteranceEnd,
            lambda _, utterance_end, **kw: self._on_utterance_end(),
        )
        self._dg_conn.on(
            LiveTranscriptionEvents.Error,
            lambda _, error, **kw: print(f"[DEEPGRAM] Error: {error}"),
        )

        self._dg_conn.start(
            LiveOptions(
                model="nova-3",
                encoding="mulaw",
                sample_rate=8000,
                language="en-US",
                punctuate=True,
                smart_format=True,
                interim_results=True,
                # 80ms was too aggressive: a normal breath/comma pause inside one
                # continuous clinic sentence exceeds that, so speech_final fired
                # mid-sentence and the bot replied to sentence fragments instead
                # of waiting for the clinic to finish talking. 300ms was raised
                # as a first pass but real call audio (2026-07-02) still showed
                # it firing mid-sentence — this target speaks with a slow, human
                # cadence where clause pauses regularly exceed 300ms. 800ms
                # tolerates that cadence while staying below the 1000ms
                # utterance_end_ms floor, so genuine turn-ends still resolve
                # faster via endpointing than waiting on the utterance_end
                # backstop.
                endpointing=800,
                # Deepgram's documented minimum for utterance_end_ms is 1000ms —
                # anything below that gets the WebSocket handshake rejected with
                # HTTP 400, so the whole STT connection never comes up. Values
                # under 1000ms give no benefit anyway (interim results only
                # arrive ~once/sec), so 1000 is both the floor and the fastest
                # valid setting.
                utterance_end_ms="1000",
            )
        )

    def _on_twilio(self, msg: dict):
        event = msg.get("event")

        if event == "start":
            self.stream_sid      = msg["start"]["streamSid"]
            self._call_start_time = time.time()
            incoming             = msg["start"].get("callSid")

            if self.state.call_sid and self.state.call_sid != incoming:
                raise ValueError(f"CallSid mismatch: {incoming}")

            print(f"[STREAM] t=0 sid={self.stream_sid}")

            threading.Thread(target=self._prefetch_opening, daemon=True).start()

            # Fixed, unconditional delay: Athena's intro has been consistently
            # ~11-12s across every scenario observed. Earlier versions tried to
            # detect "the clinic just went quiet" from STT signals (speech_final
            # timing, VAD activity) to respond the instant she stopped talking —
            # but Deepgram's raw voice-activity events fire on line noise/echo
            # with no real words required, so on an open phone line "quiet
            # enough" could never be reliably detected and the bot ended up
            # waiting out the clinic's entire no-input gap instead. A plain
            # fixed timer sidesteps that: no listening, no detection, no
            # dependence on STT signal quality — just speak at t=_OPENING_DELAY_S.
            self._opening_timer = threading.Timer(_OPENING_DELAY_S, self._timer_open)
            self._opening_timer.daemon = True
            self._opening_timer.start()

        elif event == "media":
            payload = msg.get("media", {}).get("payload")
            if payload and self._dg_conn:
                self._dg_conn.send(base64.b64decode(payload))

        elif event == "stop":
            raise StopIteration("Twilio stop")

    def _prefetch_opening(self):
        text  = self.state.scenario.get("opening_line", "")
        voice = self.state.scenario["voice"]

        if not text:
            self._opening_ready.set()
            return

        print(f"[PREFETCH] Opening TTS voice={voice}")
        audio = text_to_speech_mulaw(text, voice)

        if audio:
            self._opening_audio = audio
            print(f"[PREFETCH] Opening ready {len(audio):,} B")
        else:
            print("[PREFETCH] Opening TTS failed")

        self._opening_ready.set()

    def _timer_open(self):
        print(f"\n[TIMER] t={self._elapsed():.2f}s — playing opening line")

        if self._call_done:
            return

        if not self._resp_lock.acquire(blocking=False):
            print("[TIMER] Lock busy — skipping opening")
            return

        self._opening_played = True
        self.state.turn += 1
        self.state.add_agent("How can I help you today?")

        threading.Thread(target=self._run_opening, daemon=True).start()

    def _on_transcript(self, result):
        text = result.channel.alternatives[0].transcript.strip()

        if self._should_barge_in(text):
            self._trigger_barge_in(text)

        if not result.is_final:
            return

        if text:
            self._transcript_buf.append(text)

        if result.speech_final:
            self._handle_speech_final()

    def _on_utterance_end(self):
        self._handle_speech_final()

    def _should_barge_in(self, text: str) -> bool:
        if self._call_done or not self._opening_played:
            return False
        if self._play_protected:
            return False
        if not self._is_playing:
            return False
        if not text or len(text.strip()) < _MIN_BARGE_TEXT_CHARS:
            return False
        return True

    def _trigger_barge_in(self, text: str = ""):
        now = time.time()
        if now - self._last_clear_at < _BARGE_CLEAR_COOLDOWN_S:
            return

        self._last_clear_at = now
        self._barge_in.set()
        self._clear_twilio_playback()

        print(
            f"[BARGE-IN] t={self._elapsed():.1f}s cleared playback "
            f"because caller said: \"{text[:70]}\""
        )

    def _clear_twilio_playback(self):
        if not self.stream_sid:
            return

        try:
            with self._ws_lock:
                self.ws.send(
                    json.dumps({"event": "clear", "streamSid": self.stream_sid})
                )
        except Exception as e:
            print(f"[BARGE-IN] clear failed: {e}")

    def _handle_speech_final(self):
        self._speech_final_time = time.time()

        if self._call_done:
            self._transcript_buf.clear()
            return

        utterance = " ".join(self._transcript_buf).strip()
        self._transcript_buf.clear()

        if not utterance:
            return

        if not self._opening_played:
            # Before the fixed opening timer fires, the clinic's intro is
            # just heard out and not acted on — the bot always speaks first
            # at exactly _OPENING_DELAY_S, regardless of what's been said.
            return

        self._accumulate_or_dispatch(utterance)

    def _accumulate_or_dispatch(self, utterance: str):
        """
        Deepgram's endpointing (800ms, raised from an original 300ms — real
        call audio showed even 300ms firing mid-sentence against this
        target's slow, human cadence) occasionally still fires speech_final
        on a mid-sentence pause, finalizing a fragment (e.g. "For most
        routine blunt") instead of the full sentence. Dispatching that
        fragment as its own turn makes the bot react to nonsense and then
        react again when the real sentence lands right after — audibly a
        broken, repeating exchange. Anything that doesn't look like a
        finished sentence is held for a brief grace window so a same-
        utterance continuation gets merged into one turn instead of two.

        A short one-word reply (e.g. "Okay.") is the same problem in
        disguise: Deepgram punctuates it as a finished sentence, but it's
        often just a verbal placeholder before the clinic continues talking
        after a normal conversational pause. Dispatching it immediately means
        the bot starts speaking its reply right as the clinic resumes, which
        trips barge-in and cuts the bot off mid-sentence. Short utterances get
        the same grace window before being treated as genuinely finished.
        """
        with self._fragment_lock:
            combined = (
                f"{self._fragment_buf} {utterance}".strip()
                if self._fragment_buf
                else utterance
            )

            if self._looks_incomplete(combined) or len(combined) < _SHORT_UTTERANCE_CHARS:
                grace = _HOLD_GRACE_S
            else:
                grace = None

            if grace is not None:
                self._fragment_buf = combined
                if self._fragment_timer:
                    self._fragment_timer.cancel()
                # threading.Timer.cancel() can't stop a callback that has
                # already started running (only queued/waiting ones) — if a
                # stale _fire_fragment call is already blocked on
                # _fragment_lock right here, cancel() above is a no-op for
                # it. The generation counter catches that case: a stale
                # _fire_fragment checks its captured generation against the
                # current one before touching the buffer, so it no-ops
                # instead of dispatching the freshly-merged text early and
                # defeating the grace window this mechanism exists to give.
                self._fragment_generation += 1
                my_generation = self._fragment_generation
                self._fragment_timer = threading.Timer(
                    grace, self._fire_fragment, args=(my_generation,)
                )
                self._fragment_timer.daemon = True
                self._fragment_timer.start()
                print(f"[FRAGMENT] t={self._elapsed():.1f}s holding ({grace}s): \"{combined[:70]}\"")
                return

            self._fragment_buf = ""
            self._fragment_generation += 1
            if self._fragment_timer:
                self._fragment_timer.cancel()
                self._fragment_timer = None

        self._dispatch_turn(combined)

    def _fire_fragment(self, generation: int):
        with self._fragment_lock:
            if generation != self._fragment_generation:
                return
            combined = self._fragment_buf.strip()
            self._fragment_buf = ""
            self._fragment_timer = None

        if combined and not self._call_done:
            self._dispatch_turn(combined)

    @staticmethod
    def _looks_incomplete(text: str) -> bool:
        return bool(text) and text[-1] not in ".!?"

    def _dispatch_turn(self, utterance: str):
        """
        Responds to a finalized clinic utterance immediately — no artificial
        batching delay. The LLM still gets the full conversation history on every
        call (see _run_turn -> _run_response), so responding fast doesn't cost
        coherence — each reply is grounded in everything said so far, not just
        the isolated utterance that triggered it.
        """
        if self._call_done:
            return

        print(f"[DISPATCH] t={self._elapsed():.1f}s firing: \"{utterance[:90]}\"")

        if not self._resp_lock.acquire(blocking=False):
            if self._is_playing:
                self._trigger_barge_in(utterance)
            self._queue_pending(utterance)
            return

        self._run_turn(utterance)

    def _run_turn(self, agent_text: str):
        self.state.turn += 1
        self.state.add_agent(agent_text)

        if self._speech_final_time is not None:
            stt_latency = time.time() - self._speech_final_time
            print(f"[LATENCY] STT {stt_latency:.2f}s (speech end -> turn dispatch)")

        print(
            f"\n[TURN {self.state.turn:02d}] "
            f"t={self._elapsed():.1f}s <- \"{agent_text[:90]}\""
        )

        if self.state.should_end(agent_text):
            if self._end_call_allowed("state.should_end"):
                self._start_farewell_locked()
                return

        history = list(self.state.history)
        threading.Thread(
            target=self._run_response,
            args=(history,),
            daemon=True,
        ).start()

    def _run_opening(self):
        text  = self.state.scenario.get("opening_line", "")
        voice = self.state.scenario["voice"]

        if not text:
            self._safe_release_response_lock()
            return

        try:
            self.state.add_bot(text)
            print(f"[TURN {self.state.turn:02d}] -> opening: \"{text[:90]}\"")

            self._opening_ready.wait(timeout=2.0)
            audio = self._opening_audio or text_to_speech_mulaw(text, voice)

            if audio:
                self._barge_in.clear()
                self._play(audio, protected=True)
            else:
                print("[STREAM] Opening TTS failed")
        except Exception as e:
            print(f"[STREAM] Opening error: {e}")
        finally:
            self._safe_release_response_lock()
            self._fire_pending()

    def _run_response(self, history: list):
        """
        Streams LLM output sentence by sentence.  Each sentence dispatches its own
        TTS call immediately (parallel threads), then the play loop waits for each
        in order.  Time-to-first-audio drops from ~1.3s to ~0.6s for typical turns.
        """
        t0    = time.time()
        should_start_farewell = False

        # Per-sentence TTS results, indexed in arrival order.
        tts_events: list[threading.Event] = []
        tts_audios: list[list]            = []

        def on_sentence(text: str):
            event  = threading.Event()
            holder = [None]
            tts_events.append(event)
            tts_audios.append(holder)

            def _tts():
                try:
                    holder[0] = text_to_speech_mulaw(text, voice)
                except Exception as e:
                    print(f"[TTS] sentence synth failed: {e}")
                finally:
                    event.set()

            threading.Thread(target=_tts, daemon=True).start()

        try:
            # Read inside the try so a missing/renamed scenario key is caught
            # by the except below and releases _resp_lock via finally, instead
            # of raising before the lock-release path is reached and leaving
            # the bot silent (locked out) for the rest of the call.
            voice = self.state.scenario["voice"]

            full_text, patient_ended = get_patient_response_streaming(
                conversation_history=history,
                patient_profile=self.state.patient_memory,
                on_sentence=on_sentence,
                patient_goal=self.state.scenario.get("goal", ""),
            )
            t_llm = time.time()

            if not full_text:
                return

            if self._barge_in.is_set() and self._has_pending():
                print("[STALE] Skipping — barge-in during LLM reasoning")
                return

            self.state.add_bot(full_text)

            print(f"[TURN {self.state.turn:02d}] -> \"{full_text[:90]}\"")
            print(f"[LATENCY] LLM {t_llm - t0:.2f}s  sentences={len(tts_events)}")

            # Play sentences in arrival order; each TTS ran in parallel so most
            # will already be done by the time we reach them.
            first_audio_logged = False
            for i, (event, holder) in enumerate(zip(tts_events, tts_audios)):
                event.wait(timeout=8.0)

                if self._call_done and not self._farewell_started:
                    break
                if self._barge_in.is_set() and self._has_pending():
                    print(f"[STALE] Barge-in — skipping sentence {i + 1}/{len(tts_events)}")
                    break

                if holder[0]:
                    if not first_audio_logged:
                        print(f"[LATENCY] TTS first-audio {time.time() - t_llm:.2f}s")
                        first_audio_logged = True
                    # A caller interruption only stops the sentence that was
                    # playing when it happened — _play() clears _barge_in in
                    # its own finally as part of normal turn-end cleanup, so
                    # by the time this loop re-checks the flag for the next
                    # sentence it's already been reset. Using _play()'s own
                    # return value instead means an interruption still stops
                    # every remaining sentence in this turn, not just the one
                    # that was playing at that instant.
                    if self._play(holder[0]):
                        break

            print(f"[LATENCY] end-to-end {time.time() - t0:.2f}s")

            if patient_ended:
                should_start_farewell = self._end_call_allowed("patient_ended")

        except Exception as e:
            print(f"[STREAM] Response error: {e}")
        finally:
            if should_start_farewell:
                # Hand _resp_lock straight to _start_farewell_locked() while
                # still held, instead of releasing it and trying to reacquire
                # (the old _start_farewell_when_lock_free() path) — that
                # release-then-reacquire gap let another dispatched turn steal
                # the lock first, silently dropping the patient's own [END]
                # end-of-call signal with no retry.
                self._start_farewell_locked()
            else:
                self._safe_release_response_lock()
                self._fire_pending()

    def _start_farewell_locked(self):
        with self._state_lock:
            if self._farewell_started or self._call_done:
                self._safe_release_response_lock()
                return

            self._farewell_started = True
            self._call_done        = True

        self._clear_pending_audio_state()

        threading.Thread(target=self._run_farewell_locked, daemon=True).start()

    def _run_farewell_locked(self):
        farewell = "Thanks so much for your help. Have a great day. Bye."
        self.state.add_bot(farewell)

        try:
            # protected=True: _clear_pending_audio_state() just set _barge_in to
            # interrupt whatever was playing before the farewell; without this,
            # _play() would see that same flag still set on its very first chunk
            # and abort immediately, so every call ended in silence instead of
            # an audible goodbye.
            self._speak(farewell, protected=True)
        except Exception as e:
            print(f"[STREAM] Farewell error: {e}")
        finally:
            self._safe_release_response_lock()

        self._save_outputs_and_close()

    def _save_outputs_and_close(self):
        try:
            self.state.transcript_saved = True
            save_transcript(self.state, self.state.scenario["id"])

            analysis = analyze_transcript(
                self.state.transcript_lines,
                self.state.scenario,
            )
            self.all_analyses.append(analysis)
            save_scenario_report(analysis)

        except Exception as e:
            print(f"[SAVE] Error while saving outputs: {e}")

        try:
            self.ws.close()
        except Exception:
            pass

    def _end_call_allowed(self, reason: str) -> bool:
        """
        Single gate for 'is the call allowed to end right now.' Both ways a
        call can end — the clinic saying goodbye (state.should_end, checked
        from _run_turn) and the patient LLM's own [END] token (checked from
        _run_response) — funnel through the same guard and the same log
        line, instead of two call sites independently checking
        _can_end_call_now and drifting to different messages over time.

        This does NOT start the farewell itself: both call sites hand off to
        _start_farewell_locked() while still holding _resp_lock (never
        released and reacquired first) — releasing and reacquiring around
        that handoff previously let another dispatched turn steal the lock
        first, silently dropping the end-of-call signal.
        """
        if self._can_end_call_now(reason):
            return True
        print(f"[END-GUARD] continuing instead of ending early — reason={reason}")
        return False

    def _can_end_call_now(self, reason: str = "") -> bool:
        scenario = self.state.scenario

        min_duration = float(scenario.get("min_call_duration_s", _DEFAULT_MIN_CALL_DURATION_S))
        min_turns    = int(scenario.get("min_turns_before_end", _DEFAULT_MIN_TURNS_BEFORE_END))

        elapsed = self._elapsed()

        if elapsed < min_duration:
            print(
                f"[END-GUARD] blocked early end at {elapsed:.1f}s "
                f"< {min_duration:.1f}s reason={reason}"
            )
            return False

        if self.state.turn < min_turns:
            print(
                f"[END-GUARD] blocked early end at turn={self.state.turn} "
                f"< {min_turns} reason={reason}"
            )
            return False

        return True

    def _has_pending(self) -> bool:
        with self._pending_lock:
            return bool(self._pending_utterance)

    def _queue_pending(self, utterance: str):
        with self._pending_lock:
            self._pending_utterance = (
                f"{self._pending_utterance} {utterance}".strip()
                if self._pending_utterance
                else utterance
            )
        print(f"[QUEUE] t={self._elapsed():.1f}s pending: \"{utterance[:70]}\"")

    def _fire_pending(self):
        if self._call_done:
            return

        with self._pending_lock:
            pending = self._pending_utterance.strip()
            if not pending:
                return
            self._pending_utterance = ""

        self._barge_in.clear()

        if not self._resp_lock.acquire(blocking=False):
            with self._pending_lock:
                self._pending_utterance = (
                    f"{pending} {self._pending_utterance}".strip()
                    if self._pending_utterance
                    else pending
                )
            return

        self._run_turn(pending)

    def _clear_pending_audio_state(self):
        with self._pending_lock:
            self._pending_utterance = ""
        with self._fragment_lock:
            self._fragment_buf = ""
            self._fragment_generation += 1
            if self._fragment_timer:
                self._fragment_timer.cancel()
                self._fragment_timer = None
        self._barge_in.set()
        self._clear_twilio_playback()

    def _speak(self, text: str, protected: bool = False):
        t0    = time.time()
        audio = text_to_speech_mulaw(text, self.state.scenario["voice"])

        if not audio:
            print("[STREAM] TTS None — skip")
            return

        print(f"[TIMING] TTS={time.time() - t0:.2f}s {len(audio):,} B")
        self._play(audio, protected=protected)

    def _play(self, mulaw_audio: bytes, protected: bool = False) -> bool:
        """Returns True if playback was cut short by a barge-in."""
        if not mulaw_audio:
            return False

        t0          = time.time()
        sent        = 0
        total       = max(1, len(mulaw_audio) // self._CHUNK)
        interrupted = False

        self._play_protected  = protected
        self._play_start_time = time.time()
        self._set_playing(True)

        try:
            for i in range(0, len(mulaw_audio), self._CHUNK):
                if not protected and self._barge_in.is_set():
                    interrupted = True
                    self._clear_twilio_playback()
                    break

                if self._call_done and not self._farewell_started:
                    interrupted = True
                    self._clear_twilio_playback()
                    break

                self._send_media_chunk(mulaw_audio[i : i + self._CHUNK])
                sent += 1
                time.sleep(0.02)

        except Exception as e:
            print(f"[STREAM] Inject error: {e}")
        finally:
            self._play_protected = False
            self._set_playing(False)
            self._barge_in.clear()
            self._send_mark()

            tag = " [interrupted]" if interrupted else ""
            print(f"[TIMING] play={time.time() - t0:.2f}s {sent}/{total} chunks{tag}")

        return interrupted

    def _send_media_chunk(self, chunk: bytes):
        if not self.stream_sid:
            return

        with self._ws_lock:
            self.ws.send(
                json.dumps({
                    "event":     "media",
                    "streamSid": self.stream_sid,
                    "media":     {"payload": base64.b64encode(chunk).decode()},
                })
            )

    def _send_mark(self):
        if not self.stream_sid:
            return

        try:
            with self._ws_lock:
                self.ws.send(
                    json.dumps({
                        "event":     "mark",
                        "streamSid": self.stream_sid,
                        "mark":      {"name": f"t{self.state.turn}"},
                    })
                )
        except Exception:
            pass

    def _elapsed(self) -> float:
        if not self._call_start_time:
            return 0.0
        return time.time() - self._call_start_time

    def _set_playing(self, value: bool):
        with self._state_lock:
            self._is_playing = value

    def _safe_release_response_lock(self):
        try:
            self._resp_lock.release()
        except RuntimeError:
            print("[LOCK] response lock already released")
