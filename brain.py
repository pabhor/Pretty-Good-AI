import time
from openai import OpenAI
import os
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Keep only the last N messages to reduce context size and LLM latency.
# At 10 messages (~5 turns), we have enough context without bloating the call.
_MAX_HISTORY = 10


def get_patient_response(
    conversation_history: list,
    patient_context: str,
    patient_profile: str
) -> str:
    """
    Generate an authentic patient response using recent conversation context.
    Streams from GPT-4o-mini for lowest time-to-first-token.
    History is truncated to the last _MAX_HISTORY messages to keep
    context small and LLM response fast.
    """
    t0 = time.time()

    # Truncate history — reduces tokens, reduces LLM latency
    recent = conversation_history[-_MAX_HISTORY:]

    system_prompt = f"""You are method-acting as a real human patient on a phone call with a medical office AI agent.

{patient_profile}

YOUR SITUATION
==============
{patient_context}

HOW YOU SPEAK (STRICT RULES)
=============================
- Maximum 2 SHORT sentences per response — this is a phone call
- Always use contractions: I'd, I'm, that's, it's, we've
- React to what the agent JUST said before pushing your own agenda
- Occasional natural fillers: "um", "oh", "actually", "wait — "
- Sound naturally embarrassed when you correct your own mistakes
- One thought at a time — let the conversation breathe
- Never sound like you are running a test
- Never reveal you are an AI

AUTHENTIC EXAMPLES
==================
Bad: "I need to correct my insurance. It is United Healthcare UHC456789."
Good: "Oh wait — actually, we switched insurance a few months back. It's United Healthcare now, sorry about that."

Bad: "I would like to change the appointment to my wife Linda."
Good: "Hmm — oh gosh, these appointments are actually for my wife Linda, not me. Can we switch that? Sorry."
"""

    messages = [{"role": "system", "content": system_prompt}] + recent

    full_response = ""
    stream = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=80,      # 2 short sentences ≈ 40-60 tokens — 80 is safe ceiling
        temperature=0.9,
        stream=True,
    )

    for chunk in stream:
        delta = chunk.choices[0].delta.content
        if delta:
            full_response += delta

    result = full_response.strip()
    elapsed = time.time() - t0
    print(f"[LATENCY] LLM    {elapsed:.2f}s  →  {result[:70]}")
    return result
