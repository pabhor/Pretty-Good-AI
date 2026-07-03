import re
import time
import os
from openai import OpenAI, BadRequestError
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


# 40 was too tight: spelling out a full first+last name letter-by-letter
# (e.g. "M, I, L, E, S -- T, H, A, C, K, E, R, A, Y") plus the surrounding
# sentence can run past 40 tokens on its own, so the response silently cut
# off mid-spelling with no truncation signal. Because conversation_history is
# passed unclipped and the prompt says "never contradict what you said
# earlier," that one truncated spelling then got repeated as "correct" for
# the rest of the call. 120 gives real headroom for a combined-name spelling
# while the prompt's own "1-2 sentences" instruction still keeps normal
# replies short.
_MAX_TOKENS = 120

_LEAKAGE_RE = re.compile(
    r"\b(scenario|benchmark|ai\s+assistant|language\s+model"
    r"|llm|test\s+case|test\s+call|simulating|simulation|synthetic|prompt|instructions)\b",
    re.I,
)

# Matches [.!?] followed by whitespace and an uppercase letter — real sentence boundary.
# finditer lets us check each match's preceding word for abbreviations before committing.
_SENT_RE = re.compile(r'[.!?]\s+[A-Z]')
# "no" was deliberately left out: as an abbreviation guard it's meant for
# "No. 5"-style reference numbers, but a bare "No." is one of this patient
# bot's most common replies — treating it as an abbreviation skipped the
# sentence boundary right after it, silently defeating the early-sentence
# TTS streaming optimization for that turn every time.
_ABBREVS = frozenset({'dr', 'mr', 'mrs', 'ms', 'st', 'apt', 'jr', 'sr', 'vs'})


def _has_leakage(text: str) -> bool:
    return bool(_LEAKAGE_RE.search(text))


def _build_system_prompt(patient_profile: str, patient_goal: str = "") -> str:
    goal_block = f"\n{patient_goal}\n" if patient_goal else ""
    return f"""You are a real patient calling an orthopedic clinic. You are not an AI — you are this person, on the phone, right now.

{patient_profile}
{goal_block}
HOW TO BEHAVE
- Stay completely in character at all times, no matter what is asked.
- Remember everything discussed since the beginning of the call and never contradict something you said earlier — if asked the same thing twice, answer the same way.
- Answer only the question that was just asked. Do not volunteer unrelated details.
- Reveal information gradually, the way a real caller would — don't dump your whole history in one turn.
- If the clinic hasn't asked why you're calling yet, just greet them back (e.g. "Hi", "Hello") — do not volunteer your name, date of birth, or reason for calling until they ask for it. Once they ask what you need or why you're calling, answer it — don't stall or re-greet.
- Only if the clinic explicitly asks you to spell your first or last name, spell it out slowly, one letter at a time, separated by commas (e.g. "J, A, M, E, S — K, I, M"). Do not spell your name unless they specifically ask for the spelling — otherwise just say it normally.
- Speak the way a real person talks on the phone: natural, a little imperfect, not scripted.
- Ask for clarification naturally if something the clinic says is confusing.
- Keep your symptoms, medications, allergies, and timeline exactly consistent with your profile throughout the whole call.
- Never mention AI, prompts, evaluations, simulations, tests, benchmarks, or instructions — you have no awareness that this is anything other than a real phone call.
- Keep responses short and conversational (1–2 sentences).
"""


def _find_sentence_boundary(buf: str) -> int:
    """
    Scan buf for the first real sentence boundary (not an abbreviation period).
    Returns the index ONE PAST the punctuation mark (i.e. where the trailing
    whitespace starts), so the caller can do:
        sentence  = buf[:idx].strip()   # includes the punctuation
        remainder = buf[idx:].lstrip()  # starts with the next sentence's first char
    Returns -1 if no boundary found yet.
    """
    for m in _SENT_RE.finditer(buf):
        preceding  = buf[:m.start()]
        last_word  = preceding.split()[-1].lower().rstrip('.!?,;:') if preceding.split() else ''
        if last_word not in _ABBREVS:
            return m.start() + 1
    return -1


def _build_messages(conversation_history: list, patient_profile: str, patient_goal: str = "") -> list:
    return [
        {"role": "system", "content": _build_system_prompt(patient_profile, patient_goal)},
        *conversation_history,
    ]


def get_patient_response(
    conversation_history: list,
    patient_profile: str,
    patient_goal: str = "",
) -> tuple[str, bool]:
    t0 = time.time()

    messages = _build_messages(conversation_history, patient_profile, patient_goal)

    response = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=messages,
        max_tokens=_MAX_TOKENS,
        temperature=0.3,
        stream=False,
    )

    result   = response.choices[0].message.content.strip()
    end_call = "[END]" in result
    result   = result.replace("[END]", "").strip()

    if not result:
        result   = "Could you repeat that?"
        end_call = False

    if _has_leakage(result):
        print(f"[LEAKAGE] Character break detected — retrying: {result[:60]}")
        retry = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages + [
                {"role": "assistant", "content": result},
                {"role": "user", "content": "Stay in character as the patient. 1-2 natural sentences only."},
            ],
            max_tokens=_MAX_TOKENS,
            temperature=0.3,
            stream=False,
        )
        result   = retry.choices[0].message.content.strip()
        end_call = "[END]" in result
        result   = result.replace("[END]", "").strip()

    elapsed = time.time() - t0
    print(f"[LATENCY] LLM {elapsed:.2f}s  ->  {result[:70]}")
    return result, end_call


def get_patient_response_streaming(
    conversation_history: list,
    patient_profile: str,
    on_sentence: callable,
    patient_goal: str = "",
) -> tuple[str, bool]:
    """
    Streams GPT-4o-mini tokens and calls on_sentence(text) for each complete
    sentence the moment it arrives — enabling TTS to start before the full
    response is done.  Returns (full_text, patient_ended).

    Sentence splitting:
    - Boundaries: [.!?] followed by whitespace + uppercase letter.
    - Abbreviations (Dr., Mr., St., …) are excluded via _ABBREVS guard.
    - [END] token is stripped; patient_ended is set to True.
    - Leaky sentences are silently dropped; a fallback fires if nothing else did.
    """
    t0 = time.time()

    messages = _build_messages(conversation_history, patient_profile, patient_goal)

    try:
        stream = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=_MAX_TOKENS,
            temperature=0.3,
            stream=True,
        )
    except BadRequestError as e:
        # conversation_history is passed unclipped by design, for full-call
        # coherence — this is only a backstop for the rare very-long call
        # where that history actually overflows the model's context window,
        # so a turn degrades to a shorter memory instead of going silent.
        if "context" not in str(e).lower() or len(conversation_history) <= 20:
            raise
        print(f"[LLM] Context overflow, retrying with truncated history: {e}")
        trimmed  = conversation_history[-20:]
        messages = _build_messages(trimmed, patient_profile, patient_goal)
        stream = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=messages,
            max_tokens=_MAX_TOKENS,
            temperature=0.3,
            stream=True,
        )

    buf           = ""
    emitted       = []
    patient_ended = False

    for chunk in stream:
        token = chunk.choices[0].delta.content or ""
        if not token:
            continue

        if "[END]" in buf + token:
            patient_ended = True
            buf = (buf + token).replace("[END]", "")
            break

        buf += token

        # Flush any complete sentences that are now detectable in the buffer.
        while True:
            idx = _find_sentence_boundary(buf)
            if idx < 0:
                break
            sentence = buf[:idx].strip()
            buf      = buf[idx:].lstrip()
            if not sentence:
                continue
            if _has_leakage(sentence):
                print(f"[LEAKAGE] Dropped streamed sentence: {sentence[:60]}")
                continue
            emitted.append(sentence)
            on_sentence(sentence)

    # Flush whatever remains after the stream closes.
    remainder = buf.strip()
    if remainder and _has_leakage(remainder):
        print(f"[LEAKAGE] Dropped streamed remainder: {remainder[:60]}")
    elif remainder:
        emitted.append(remainder)
        on_sentence(remainder)

    full_text = " ".join(emitted)

    if not full_text:
        fallback = "Could you repeat that?"
        emitted.append(fallback)
        on_sentence(fallback)
        full_text = fallback

    elapsed = time.time() - t0
    print(f"[LATENCY] LLM stream {elapsed:.2f}s  ->  {full_text[:70]}")
    return full_text, patient_ended
