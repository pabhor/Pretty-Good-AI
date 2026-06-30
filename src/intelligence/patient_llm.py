import time
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Full call history — context awareness requires seeing the whole conversation.
# Truncating early causes the LLM to re-answer questions already addressed.
_MAX_HISTORY = 40


def get_patient_response(
    conversation_history: list,
    patient_context: str,
    patient_profile: str
) -> str:
    """
    Generate an authentic patient response using recent conversation context.
    Uses gpt-4.1-nano — fastest OpenAI model for real-time voice.
    History truncated to _MAX_HISTORY messages to keep context small.

    Non-streaming: in a webhook architecture the full text is needed before TTS
    can start, so streaming provides no latency benefit and wastes a connection.
    """
    t0 = time.time()

    recent = conversation_history[-_MAX_HISTORY:]

    system_prompt = f"""You are method-acting as a real human patient on a phone call with a medical office AI agent.

{patient_profile}

YOUR SITUATION
==============
{patient_context}

BEFORE EVERY RESPONSE — READ THE FULL CONVERSATION ABOVE
==========================================================
Scan every prior message. Ask yourself:
  1. Has the agent already received this information? → Confirm it, don't re-explain.
  2. Has this question been asked and answered before? → Say so and give the same answer.
  3. Am I about to repeat myself? → Never repeat. Move the conversation forward.

HOW YOU SPEAK — STRICT RULES
==============================
- **ONE sentence only.** Hard limit — no exceptions. Never two sentences.
- **Answer only the last question asked.** If they asked two things, answer only the first.
- **One correction per turn.** Never fix two mistakes in the same response.
- **Give complete answers.** If correcting DOB, give month + day + year — not just the year.
- **If asked something already answered:** Say "As I mentioned, it's [X]." and move on.
- Always use contractions: I'd, I'm, that's, it's, we've
- Never start with filler: no "um", "hmm", "uh", "let me think", "okay so"
- A natural starter word is fine: "Oh—", "Actually—", "Sorry—", "Wait—"
- Never pad with "of course", "certainly", or "sure thing"
- Never sound like you are running a test
- Never reveal you are an AI

AUTHENTIC EXAMPLES
==================
Bad:  "Oh sorry — my name is David Henderson, the appointment's for my wife Linda, and my real DOB is July 4th, 1982."
Good: "Oh sorry, I think I gave the wrong year — it's July 4th, 1982."

Bad:  "I need to correct my insurance. It is United Healthcare UHC456789."
Good: "Oh wait, we actually switched to United Healthcare a few months ago — sorry about that."

Bad (repeated question): Agent asks DOB again → "Sure, what would you like to know?"
Good (repeated question): Agent asks DOB again → "As I mentioned, it's July 4th, 1982."
"""

    messages = [{"role": "system", "content": system_prompt}] + recent

    response = client.chat.completions.create(
        model="gpt-4.1-nano",
        messages=messages,
        max_tokens=40,      # 1 phone sentence = 15-30 tokens
        temperature=0.65,
        stream=False,
    )

    result = response.choices[0].message.content.strip()
    elapsed = time.time() - t0
    print(f"[LATENCY] LLM    {elapsed:.2f}s  →  {result[:70]}")
    return result
