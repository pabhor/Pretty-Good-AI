import time
import threading


class ConversationState:

    def __init__(self, scenario: dict):
        self.scenario         = scenario
        self.history          = []
        self.transcript_lines = []
        self.turn             = 0
        self.start_time       = time.time()
        self.call_sid         = None
        self.transcript_saved = False

        # Structured patient facts, kept separate from the turn-by-turn history/
        # transcript so they stay a stable source of truth the LLM is grounded on.
        self.patient_memory = scenario.get("patient_profile", "")

        self.pending_audio_key = None
        self.pending_text      = None
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
        self.pending_audio_key = None
        self.pending_text      = None
        self.prefetch_done.clear()

    def should_end(self, agent_said: str) -> bool:
        if self.turn < 3:
            return False

        lower = agent_said.lower()

        farewell_signals = [
            "goodbye",
            " bye",
            "have a great day",
            "have a great one",
            "have a good day",
            "have a good one",
            "we'll see you",
        ]

        # No turn-count cutoff — a fixed turn limit was ending calls mid-topic
        # regardless of what was actually being discussed. Let the conversation
        # end when the clinic actually says goodbye, not on an arbitrary count.
        # Duration cap is a pure runaway-call safety net (cost/operational),
        # generous enough that it should never fire during a normal call.
        if self.duration() > 600:
            return True

        return any(signal in lower for signal in farewell_signals)

    def duration(self) -> float:
        return round(time.time() - self.start_time, 1)

    def to_text(self) -> str:
        return "\n".join(self.transcript_lines)
