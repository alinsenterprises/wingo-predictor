"""The two analysts that speak on this desk."""
from __future__ import annotations

from .modes import Mode

PERSONAS = {
    "ROB": (
        "You are ROB, senior round analyst of the WINGO NEXUS signal desk. "
        "You are sharp, calm and numeric: you quote the window stats, name the hot and due "
        "digits and never over-promise. You speak in at most two short sentences, "
        "plain English, no markdown, no lists."
    ),
    "ROD": (
        "You are ROD, the risk analyst of the WINGO NEXUS signal desk. "
        "You are candid about variance: you state the conviction, the streak and the risk, "
        "and you remind that every round is independent. You speak in at most two short "
        "sentences, plain English, no markdown, no lists."
    ),
}

OTHER_PERSONA = {"ROB": "ROD", "ROD": "ROB"}


def persona_for(mode: Mode) -> str:
    return mode.ai if mode.ai in PERSONAS else "ROB"


def system_prompt(persona: str) -> str:
    return PERSONAS.get(persona, PERSONAS["ROB"])


def build_commentary_prompt(persona: str, mode: Mode, context: dict) -> str:
    """Context dict comes from engine.ai_context(mode).

    The BIG/SMALL call is the desk's priority: the analyst must read the
    size pattern first and end with an explicit ``CALL: BIG`` / ``CALL: SMALL``
    that the card renders as the pattern answer.
    """
    return (
        f"Game: WINGO {mode.label} (node {mode.node:02d}).\n"
        f"Live period: {context['issue']}\n"
        f"Our pick for it: number {context['number']} "
        f"({', '.join(context['colours'])}, {context['size']}, {context['parity']}), "
        f"conviction {context['conviction']}%.\n"
        f"Last settled: {context['last_issue']} -> {context['last_number']} "
        f"({context['last_colour']}).\n"
        f"PRIORITY - BIG/SMALL. Last 14 results oldest to newest: {context['size_pattern']} "
        f"(B=BIG, S=SMALL); current run: {context['size_run']}; "
        f"window split {context.get('big_small_window', {})}. "
        f"Our size hit-rate {context['size_rate']}%, size streak {context['size_streak']}.\n"
        f"Window of {context['window']} rounds - hot digits: {context['hot']}; "
        f"due digits: {context['due']}; colour hit-rate {context['colour_rate']}%, "
        f"colour streak {context['streak']}.\n"
        f"Chart rhythm: {context.get('rhythm', {}).get('label', '-')} "
        f"(flip rate {context.get('rhythm', {}).get('flip_rate', 0.5):.0%}, "
        f"z {context.get('rhythm', {}).get('z', 0)}) - STREAKY means runs hold, "
        "ALTERNATING means sides keep flipping, CHOPPY means no rhythm to follow.\n"
        f"Pattern decoder: {context.get('decoder', {}).get('label', '-')} - with the last "
        f"{len(context.get('decoder', {}).get('context', '-'))} result(s) "
        f"({context.get('decoder', {}).get('context', '-')}, newest first) the next side was "
        f"BIG {context.get('decoder', {}).get('p', 0.5):.0%} historically "
        f"({context.get('decoder', {}).get('hits', 0)}/{context.get('decoder', {}).get('total', 0)} "
        f"matches, lag-{context.get('decoder', {}).get('lag', 1)} autocorrelation "
        f"{context.get('decoder', {}).get('corr', 0)}, mean run "
        f"{context.get('decoder', {}).get('runs', 0)} vs fair 2.0; hottest context "
        f"'{context.get('decoder', {}).get('hot_context', '-')} -> "
        f"{context.get('decoder', {}).get('hot_p', 0.5):.0%}'. Treat it as evidence, not "
        "certainty: follow a context only when its sample is decent and the skew is real.\n"
        "First check the BIG/SMALL pattern: alternation vs streaks, the balance of the "
        "window, and whether the current run looks due to break. Then give your read of "
        "this round in two sentences or fewer as the persona you are, no preamble, and "
        "END your reply with exactly one final line of the form: CALL: BIG or CALL: SMALL "
        "- your big/small answer for this round."
    )
