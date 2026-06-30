import os
import time
from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))


def analyze_transcript(transcript_lines: list, scenario: dict) -> dict:
    """
    Senior QA analysis of one call transcript.

    Uses the scenario's own goal text as the evaluation baseline —
    no hardcoded per-scenario checks, works for any scenario.
    GPT-4o at low temperature for consistent structured output.
    """
    transcript_text = "\n".join(transcript_lines)

    prompt = f"""You are a senior QA engineer reviewing a healthcare voice AI agent.
A synthetic patient bot called the agent and behaved according to the patient goal below.
Analyze ONLY the agent's behavior — not the bot's.

SCENARIO
========
Name:     {scenario['name']}
Severity: {scenario.get('severity', 'High')}
Type:     {scenario.get('test_type', 'Functional')}

WHAT THE PATIENT WAS TRYING TO DO
===================================
{scenario['goal']}

TRANSCRIPT (BOT = synthetic patient, AGENT = the AI being tested)
==================================================================
{transcript_text}

EVALUATION CRITERIA
===================
For each agent response, check:

1. UNDERSTANDING — Did the agent correctly interpret the patient's request?
2. DATA ACCURACY — Did the agent use correct/updated information after corrections?
3. CONTEXT RETENTION — Did the agent maintain context across the conversation?
4. BUSINESS LOGIC — Did the agent follow correct policies (escalation, scheduling rules, etc.)?
5. SAFETY — Did the agent escalate appropriately for urgent/clinical situations?
6. MULTI-INTENT — Did the agent track and address all patient requests?
7. PROFESSIONAL TONE — Was the agent appropriate and empathetic throughout?
8. RESOLUTION — Did the agent fully resolve or appropriately defer each request?

SEVERITY DEFINITIONS
====================
Critical: Patient safety risk, data corruption, or complete feature failure
High:     Major functionality failure, unresolved primary request, wrong data used
Low:      Minor UX issue, slightly awkward phrasing, trivial omission

OUTPUT (use exactly this structure)
====================================

## CRITICAL BUGS
- [Description] | Turn: X | Expected: [what should happen] | Actual: [what happened]
(write "None" if no critical bugs)

## MAJOR BUGS
- [Description] | Turn: X | Expected: ... | Actual: ...
(write "None" if no major bugs)

## MINOR BUGS
- [Description] | Turn: X | Expected: ... | Actual: ...
(write "None" if no minor bugs)

## PASSED CRITERIA
- [Criterion that worked well, with brief evidence from transcript]

## QUALITY SCORE: [1-10]
(1 = catastrophic, 5 = partial functionality, 10 = flawless)

## EXECUTIVE SUMMARY
[3-4 sentences suitable for an engineering team standup]

## RECOMMENDED FIXES
[Prioritized numbered list — most critical first]
"""

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=900,
        temperature=0.2,
    )

    analysis = response.choices[0].message.content.strip()
    print(f"[QA] Analysis complete — {scenario['name']}")

    return {
        "scenario_id":   scenario["id"],
        "scenario_name": scenario["name"],
        "severity":      scenario.get("severity", "High"),
        "test_type":     scenario.get("test_type", "Functional"),
        "analysis":      analysis,
        "timestamp":     time.strftime("%Y-%m-%d %H:%M:%S"),
    }


def save_scenario_report(analysis: dict) -> str:
    """
    Save one scenario's QA analysis to its own file.
    Filename: reports/{scenario_id}_report.txt
    e.g. reports/mult_appt_01_report.txt
    """
    os.makedirs("output/reports", exist_ok=True)
    filepath = f"output/reports/{analysis['scenario_id']}_report.txt"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"QA Report — {analysis['scenario_name']}\n")
        f.write("=" * 60 + "\n")
        f.write(f"Scenario ID : {analysis['scenario_id']}\n")
        f.write(f"Type        : {analysis['test_type']}\n")
        f.write(f"Severity    : {analysis['severity']}\n")
        f.write(f"Tested      : {analysis['timestamp']}\n")
        f.write("=" * 60 + "\n\n")
        f.write(analysis["analysis"])
        f.write("\n")

    print(f"[BUG REPORT] Saved → {filepath}")
    return filepath


def save_bug_report(analyses: list) -> str:
    """
    Write a combined summary of all analyses to reports/bug_report.md.
    Optional — only used when running all scenarios at the end.
    """
    os.makedirs("output/reports", exist_ok=True)
    filepath = "output/reports/bug_report.md"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write("# QA Bug Report — Pretty Good AI Voice Agent\n\n")
        f.write(f"**Generated:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**Scenarios tested:** {len(analyses)}\n\n")
        f.write("---\n\n")

        for i, item in enumerate(analyses, 1):
            f.write(f"## Test {i} — {item['scenario_name']}\n\n")
            f.write(f"**Type:** {item['test_type']}  \n")
            f.write(f"**Severity:** {item['severity']}  \n")
            f.write(f"**Tested:** {item['timestamp']}\n\n")
            f.write(f"{item['analysis']}\n\n")
            f.write("---\n\n")

    print(f"[BUG REPORT] Combined report → {filepath}")
    return filepath
