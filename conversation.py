import time


class ConversationState:
    """
    Single source of truth for one call session.

    Tracks history for LLM context, transcript for QA analysis,
    and timing/metadata for reporting.
    """

    def __init__(self, scenario: dict):
        self.scenario         = scenario
        self.history          = []      # OpenAI chat message format
        self.transcript_lines = []      # human-readable call log
        self.turn             = 0
        self.start_time       = time.time()
        self.call_sid         = None

    def add_bot(self, text: str):
        self.history.append({"role": "assistant", "content": text})
        self.transcript_lines.append(f"BOT:   {text}")
        print(f"\n[BOT]   {text}")

    def add_agent(self, text: str):
        self.history.append({"role": "user", "content": text})
        self.transcript_lines.append(f"AGENT: {text}")
        print(f"\n[AGENT] {text}")

    def should_end(self, agent_said: str) -> bool:
        """
        Detect genuine call-closing phrases only.

        Removed from original:
          - "is there anything else" → agent is inviting more requests, NOT closing
          - "you're all set"         → often said mid-flow after confirming one item

        Keep only unambiguous goodbyes and a hard turn-limit safety valve.
        """
        lower = agent_said.lower()

        farewell_signals = [
            "goodbye",
            " bye",             # space prefix avoids matching "nearby", "library"
            "have a great day",
            "have a great one",
            "have a good day",
            "have a good one",
            "thank you for calling",
            "thanks for calling",
            "we'll see you",
            "take care",
        ]

        # Hard limit — prevents infinite loops on non-closing agents
        if self.turn >= 15:
            return True

        return any(signal in lower for signal in farewell_signals)

    def duration(self) -> float:
        return round(time.time() - self.start_time, 1)

    def to_text(self) -> str:
        return "\n".join(self.transcript_lines)
