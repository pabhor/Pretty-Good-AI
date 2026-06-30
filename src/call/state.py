import time
import threading


class ConversationState:
    """
    Single source of truth for one call session.

    Tracks history for LLM context, transcript for QA analysis,
    and async prefetch state for the LLM+TTS background thread.
    """

    def __init__(self, scenario: dict):
        self.scenario          = scenario
        self.history           = []      # OpenAI chat message format
        self.transcript_lines  = []      # human-readable call log
        self.turn              = 0
        self.start_time        = time.time()
        self.call_sid          = None
        self.re_listen_count   = 0
        self.transcript_saved  = False

        # Async prefetch — set by _prefetch_response background thread in main.py
        self.pending_audio_key = None    # basename of mp3 in audio_cache, or "__tts_failed__"
        self.pending_text      = None    # LLM output text
        self.prefetch_done     = threading.Event()

    def add_bot(self, text: str):
        self.history.append({"role": "assistant", "content": text})
        self.transcript_lines.append(f"BOT:   {text}")
        print(f"\n[BOT]   {text}")

    def add_agent(self, text: str):
        self.history.append({"role": "user", "content": text})
        self.transcript_lines.append(f"AGENT: {text}")
        print(f"\n[AGENT] {text}")

    def reset_prefetch(self):
        """Clear async state before firing a new background LLM+TTS job."""
        self.pending_audio_key = None
        self.pending_text      = None
        self.prefetch_done.clear()

    def should_end(self, agent_said: str) -> bool:
        """
        Detect genuine call-closing phrases only.

        Turn guard (< 3) prevents false triggers during early greetings.
        "take care" removed — fires on clinical phrases like
        "take care of your symptoms", causing premature hangup mid-call.
        """
        if self.turn < 3:
            return False

        lower = agent_said.lower()

        farewell_signals = [
            "goodbye",
            " bye",             # space prefix avoids "nearby", "library"
            "have a great day",
            "have a great one",
            "have a good day",
            "have a good one",
            "we'll see you",
            # NOTE: "thank you for calling" / "thanks for calling" intentionally
            # excluded — Athena opens EVERY call with this phrase; keeping it
            # caused premature farewell at turn 3.
        ]

        if self.turn >= 20:
            return True

        return any(signal in lower for signal in farewell_signals)

    def duration(self) -> float:
        return round(time.time() - self.start_time, 1)

    def to_text(self) -> str:
        return "\n".join(self.transcript_lines)
