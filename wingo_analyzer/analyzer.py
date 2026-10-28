"""The prediction model.

Over the last ``ANALYSIS_WINDOW`` rounds every digit 0-9 gets three normalised
scores - frequency (45 %), recency (35 %, exponential decay with a half-life of
half the window) and due-ness (20 %, absence relative to the expected window/10
gap). The winner's share becomes a conviction index where 50 = no signal above
uniform and 95 = the strongest signal the window can produce.

Colour and size are derived from the digit (0 -> red+violet, 5 -> green+violet,
1/3/7/9 -> green, 2/4/6/8 -> red; 0-4 small, 5-9 big) - the same mapping the
result API reports, so scoring is a straight comparison.
"""
from __future__ import annotations

import re
import time

from .modes import Mode, colours_for, parity_for, size_for
from . import quant
from .storage import Prediction, Round

FREQ_W, RECENCY_W, DUE_W = 0.45, 0.35, 0.20

AI_CALL_RE = re.compile(r"CALL\s*:\s*(BIG|SMALL)", re.IGNORECASE)


def ai_size_call(text: str | None) -> str:
    """The analyst's explicit BIG/SMALL answer from a commentary reply."""
    match = AI_CALL_RE.search(text or "")
    return match.group(1).upper() if match else ""


def digit_scores(rounds: list[Round], window: int) -> dict:
    """Return per-digit scores + the factor breakdown for a newest-first window."""
    windowed = rounds[:window]
    n = len(windowed)
    if n == 0:
        return {"scores": {d: 0.1 for d in range(10)}, "window": 0,
                "hot": [], "cold": [], "due": [], "shares": {}}

    counts = [0] * 10
    last_seen = [None] * 10
    recency = [0.0] * 10
    half_life = max(1.0, n / 2.0)

    for index, rnd in enumerate(windowed):
        digit = rnd.number
        counts[digit] += 1
        if last_seen[digit] is None:
            last_seen[digit] = index
        recency[digit] += 0.5 ** (index / half_life)

    freq_total = float(sum(counts)) or 1.0
    rec_total = float(sum(recency)) or 1.0
    expected_gap = n / 10.0

    freq = [c / freq_total for c in counts]
    rec = [r / rec_total for r in recency]
    due_raw = []
    for digit in range(10):
        gap = (n - last_seen[digit]) if last_seen[digit] is not None else n
        due_raw.append(min(gap / (2.0 * expected_gap), 1.0) if expected_gap else 0.0)
    due_total = float(sum(due_raw)) or 1.0
    due = [d / due_total for d in due_raw]

    scores = {
        digit: FREQ_W * freq[digit] + RECENCY_W * rec[digit] + DUE_W * due[digit]
        for digit in range(10)
    }

    order = sorted(range(10), key=lambda d: scores[d], reverse=True)
    return {
        "scores": scores,
        "window": n,
        "hot": [d for d in order[:3]],
        "cold": [d for d in order[-3:][::-1]],
        "due": sorted(range(10), key=lambda d: (last_seen[d] is None, last_seen[d] if last_seen[d] is not None else n), reverse=True)[:3],
        "last_seen": last_seen,
        "counts": counts,
        "shares": {d: round(scores[d], 4) for d in order},
    }


def conviction_for(share: float) -> int:
    """50 = no signal above uniform (0.1), 95 = strongest the window allows."""
    excess = max(0.0, min(1.0, (share - 0.1) / 0.25))
    return int(round(50 + 45 * excess))


def size_model(rounds: list[Round], window: int, scores: dict | None = None) -> dict:
    """A dedicated BIG/SMALL engine - the desk's primary call.

    Five independent reads are blended into P(BIG):
      * balance     (40 %) - recency-decayed share of BIG vs SMALL results,
      * mass        (25 %) - where the digit scores sit (5-9 vs 0-4),
      * momentum    (15 %) - split of the last 12 rounds (recent form),
      * alternation (10 %) - flip rate of recent rounds projected one step ahead,
      * reversion   (10 %) - pressure against the side that is currently running.
    pattern_profile() runs alongside as a decoder: backtesting showed folding
    its Markov read into the probability did not improve hit-rate, so it feeds
    the card/AI as evidence instead of moving P(BIG).
    Lagging reads are shrunk toward 0.5 so no single stale signal can pin the
    call to one side; the fresher reads then decide the flip. Weights were
    chosen by backtesting each component against stored rounds.
    Returns side, P(BIG), a 0-100 confidence and the component parts.
    """
    if scores is None:
        scores = digit_scores(rounds, window)["scores"]
    windowed = rounds[:window]
    n = len(windowed)
    if n == 0:
        return {"side": "SMALL", "p_big": 0.5, "confidence": 0,
                "balance": 0.5, "momentum": 0.5, "mass": 0.5,
                "alternation": 0.5, "reversion": 0.5, "decoder": None,
                "run": 0}

    def _shrink(value: float, k: float) -> float:
        return 0.5 + k * (value - 0.5)

    # 1. recency-decayed balance of the raw results (long horizon, shrunk)
    half_life = max(2.0, n / 3.0)
    big_w = small_w = 0.0
    for index, rnd in enumerate(windowed):  # newest first
        weight = 0.5 ** (index / half_life)
        if size_for(rnd.number) == "BIG":
            big_w += weight
        else:
            small_w += weight
    balance = big_w / (big_w + small_w) if (big_w + small_w) else 0.5

    # 2. momentum - split of the last 12 rounds (fresh form)
    recent = windowed[:12]
    recent_big = sum(1 for rnd in recent if size_for(rnd.number) == "BIG")
    momentum = recent_big / len(recent) if recent else 0.5

    # 3. digit-score mass: where the model's conviction sits (5-9 vs 0-4)
    mass_big = sum(scores.get(d, 0.0) for d in range(5, 10))
    mass_small = sum(scores.get(d, 0.0) for d in range(0, 5))
    mass = mass_big / (mass_big + mass_small) if (mass_big + mass_small) else 0.5

    # current run measured strictly from the newest round
    run_side, run_len = None, 0
    for rnd in windowed:
        side = size_for(rnd.number)
        if run_side is None:
            run_side, run_len = side, 1
        elif side == run_side:
            run_len += 1
        else:
            break
    run_side = run_side or "SMALL"

    # 4. alternation: how often recent rounds flipped, projected one step ahead
    transitions = min(10, max(0, n - 1))
    flips = 0
    for index in range(transitions):
        if size_for(windowed[index].number) != size_for(windowed[index + 1].number):
            flips += 1
    alt_rate = flips / transitions if transitions else 0.5
    # next round is expected to flip away from the last result at the flip rate
    alternation = (0.5 + 0.25 * alt_rate) if run_side == "SMALL" \
        else (0.5 - 0.25 * alt_rate)

    # 5. mean-reversion pressure against a long current run
    pressure = min(0.15, 0.05 * max(0, run_len - 1))
    reversion = 0.5 + (pressure if run_side == "SMALL" else -pressure)
    reversion = max(0.0, min(1.0, reversion))

    # 6. pattern decoder: Markov context read of the side sequence
    decoder = pattern_profile(rounds, window)

    p_big = (
        0.40 * _shrink(balance, 0.6)
        + 0.25 * _shrink(mass, 0.6)
        + 0.15 * _shrink(momentum, 0.7)
        + 0.10 * alternation
        + 0.10 * reversion
    )
    p_big = max(0.0, min(1.0, p_big))
    return {
        "side": "BIG" if p_big >= 0.5 else "SMALL",
        "p_big": round(p_big, 4),
        "confidence": int(round(abs(p_big - 0.5) * 200)),
        "balance": round(balance, 4),
        "momentum": round(momentum, 4),
        "mass": round(mass, 4),
        "alternation": round(alternation, 4),
        "reversion": round(reversion, 4),
        "decoder": decoder,
        "run": run_len,
        "run_side": run_side,
        "flip_rate": round(alt_rate, 4),
    }


def pattern_label(rounds: list[Round], window: int, cap: int = 60) -> dict:
    """Classify the chart's rhythm: ALTERNATING / STREAKY / CHOPPY.

    Counts side-flips over the newest ``cap`` rounds and z-scores them against
    the 50/50 expectation (binomial, std = 0.5*sqrt(n)):
      z <= -2  -> runs hold   -> STREAKY
      z >= +2  -> sides flip  -> ALTERNATING
      else     -> no rhythm   -> CHOPPY
    The AI and the card both read this - it is the 'is there a pattern at all'
    gate before any pattern-following advice.
    """
    n = min(window, cap, len(rounds))
    if n < 8:
        return {"label": "NEW", "flip_rate": 0.5, "z": 0.0, "sample": n,
                "run": 0, "run_side": "-"}
    windowed = rounds[:n]
    flips = 0
    for index in range(n - 1):
        if size_for(windowed[index].number) != size_for(windowed[index + 1].number):
            flips += 1
    transitions = n - 1
    flip_rate = flips / transitions
    z = (flips - 0.5 * transitions) / (0.5 * (transitions ** 0.5))
    if z <= -2:
        label = "STREAKY"
    elif z >= 2:
        label = "ALTERNATING"
    else:
        label = "CHOPPY"
    run_side, run_len = None, 0
    for rnd in windowed:
        side = size_for(rnd.number)
        if run_side is None:
            run_side, run_len = side, 1
        elif side == run_side:
            run_len += 1
        else:
            break
    return {
        "label": label,
        "flip_rate": round(flip_rate, 4),
        "z": round(z, 2),
        "sample": n,
        "run": run_len,
        "run_side": run_side or "-",
    }


def pattern_profile(rounds: list[Round], window: int, cap: int = 200) -> dict:
    """The pattern decoder - a chart-driven read of the side sequence.

    Three independent looks at the newest-first side series (B/S):
      * Markov context - count, for every 1-3 side context, how often the round
        that followed was BIG; the read uses the context actually on screen
        right now (longest with >= 8 samples), Laplace-smoothed then shrunk
        toward 0.5 by sample size (small samples cannot dominate), plus the
        historically hottest context as extra evidence,
      * autocorrelation - lag 1..4 correlation of the side series; a strong
        negative lag-1 means alternating, a strong positive means streaks,
      * runs - mean run length vs the 2.0 expected under a fair coin, plus the
        live run.

    Everything is computed from stored rounds only. With no usable context the
    read is a neutral 0.5 and the label says NONE.
    """
    n = min(window, cap, len(rounds))
    if n < 12:
        return {"label": "NEW", "context": "-", "p": 0.5, "hits": 0,
                "total": 0, "hot_context": "-", "hot_p": 0.5,
                "lag": 1, "corr": 0.0, "runs": 0.0,
                "run": 0, "run_side": "-"}
    sides = ["B" if size_for(rnd.number) == "BIG" else "S" for rnd in rounds[:n]]

    # --- 1. Markov context: predict sides[i] from sides[i+1 .. i+k] --------
    # (sides is newest-first, so sides[i+1:] are the rounds *before* sides[i])
    counts: dict[str, list[int]] = {}
    hot = None  # historically most skewed context
    for k in (1, 2, 3):
        for i in range(0, n - k):
            target = sides[i]
            ctx = "".join(sides[i + 1:i + 1 + k])
            hits, total = counts.setdefault(ctx, [0, 0])
            counts[ctx] = [hits + (1 if target == "B" else 0), total + 1]
    for ctx, (hits, total) in counts.items():
        if total < 8:
            continue
        skew = abs((hits + 1) / (total + 2) - 0.5)
        if hot is None or skew > abs((hot[1] + 1) / (hot[2] + 2) - 0.5):
            hot = (ctx, hits, total)

    # the live read: the context actually on screen right now, k = 1..3,
    # preferring the longest context that still has enough samples
    live, live_hits, live_total = "-", 0, 0
    for k in (3, 2, 1):
        ctx = "".join(sides[0:k])
        hits, total = counts.get(ctx, [0, 0])
        if total >= 8:
            live, live_hits, live_total = ctx, hits, total
            break
    if live_total < 8 and counts:
        ctx = "".join(sides[0:1])
        live, live_hits, live_total = (ctx, *counts.get(ctx, [0, 0]))
    raw = (live_hits + 1) / (live_total + 2) if live_total else 0.5
    # shrink toward 0.5 by sample size so 8/12 cannot shout over 60/110
    weight = live_total / (live_total + 12)
    p_big = 0.5 + (raw - 0.5) * weight
    context = live

    # --- 2. autocorrelation at lags 1..4 ----------------------------------
    x = [1.0 if s == "B" else -1.0 for s in sides]
    best_lag, best_corr = 1, 0.0
    for lag in range(1, 5):
        pairs = n - lag
        if pairs < 8:
            continue
        corr = sum(x[i] * x[i + lag] for i in range(pairs)) / pairs
        if abs(corr) > abs(best_corr):
            best_lag, best_corr = lag, corr

    # --- 3. runs -----------------------------------------------------------
    runs: list[tuple[str, int]] = []
    cur_side, cur_len = sides[0], 1
    for s in sides[1:]:
        if s == cur_side:
            cur_len += 1
        else:
            runs.append((cur_side, cur_len))
            cur_side, cur_len = s, 1
    runs.append((cur_side, cur_len))
    # sides is newest-first, so runs[0] is the live run on screen
    lead_side, lead_len = runs[0]
    mean_run = sum(length for _, length in runs) / len(runs)

    hot_ctx = hot[0] if hot else "-"
    hot_p = ((hot[1] + 1) / (hot[2] + 2)) if hot else 0.5
    if live_total < 8:
        label = "NEW"
    elif best_corr <= -0.20 and best_lag == 1:
        label = "FLIP"
    elif best_corr >= 0.20:
        label = "HOLD"
    elif abs(raw - 0.5) * weight >= 0.04:
        label = "BIAS"
    else:
        label = "NONE"
    return {
        "label": label,
        "context": context,
        "p": round(p_big, 4),
        "hits": live_hits,
        "total": live_total,
        "hot_context": hot_ctx,
        "hot_p": round(hot_p, 4),
        "lag": best_lag,
        "corr": round(best_corr, 3),
        "runs": round(mean_run, 2),
        "run": lead_len,
        "run_side": "BIG" if lead_side == "B" else "SMALL",
    }


def best_digit_for_size(rounds: list[Round], window: int, side: str) -> int | None:
    """The strongest digit *inside* the given side - keeps value and answer in sync."""
    analysis = digit_scores(rounds, window)
    if analysis["window"] == 0:
        return None
    digits = range(5, 10) if side == "BIG" else range(0, 5)
    return max(digits, key=lambda d: analysis["scores"][d])


def predict(mode: Mode, rounds: list[Round], window: int, period: str) -> Prediction | None:
    """Next-round pick: the BIG/SMALL side is decided first (size_model), then
    the best digit *within* that side becomes the predicted value - so the
    value and the answer can never contradict each other.

    Every value comes from the stored chart (window stats + size model).
    With no chart data at all this returns ``None`` - the desk never emits a
    random or clock-derived guess.
    """
    analysis = digit_scores(rounds, window)
    scores = analysis["scores"]
    if analysis["window"] == 0:
        return None
    size_info = size_model(rounds, window, scores)
    deep = quant.deep_analytics(rounds, window)
    side = size_info["side"]
    number = best_digit_for_size(rounds, window, side) or 0
    conviction = conviction_for(scores[number])
    # a model side backed by a clear digit mass deserves a small bump
    if size_for(number) == side:
        conviction = min(95, conviction + max(0, size_info["confidence"] // 10))
    # deep analytics can move conviction only in their own band: a chaotic
    # window (low entropy read + flat z) trims it, a decisive one adds a point.
    if deep["lyapunov"]["label"] == "CHAOTIC" and abs(deep["zscore"]["z"]) < 1.0:
        conviction = max(50, conviction - 3)
    elif abs(deep["zscore"]["z"]) >= 1.96:
        conviction = min(95, conviction + 2)
    # runs test: a window the Wald-Wolfowitz test calls FLIPPING backs an
    # alternation read, STREAKING backs the model side - each worth a point.
    runs = deep.get("runs") or {}
    if runs.get("label") == "FLIPPING" and abs(runs.get("z", 0)) >= 1.96:
        conviction = min(95, conviction + 1)
    elif runs.get("label") == "STREAKING" and abs(runs.get("z", 0)) >= 1.96:
        conviction = min(95, conviction + 1)
    # order-2 Markov agreement: when the transition table's next-side call
    # matches the model side with enough samples, the read is corroborated.
    markov = deep.get("markov") or {}
    if markov.get("total", 0) >= 8:
        markov_side = "BIG" if float(markov.get("p_big", 0.5)) >= 0.5 else "SMALL"
        if markov_side == side:
            conviction = min(95, conviction + 1)
        else:
            conviction = max(50, conviction - 1)
    # chi-square significance: a window that genuinely departs from uniform
    # digit spread is worth trusting more than pure noise.
    chi = deep.get("chi2") or {}
    if chi.get("significant"):
        conviction = min(95, conviction + 1)
    factors = {
        "window": analysis["window"],
        "hot": analysis["hot"],
        "cold": analysis["cold"],
        "due": analysis["due"],
        "share": round(scores[number], 4),
        "top3": {str(d): round(scores[d], 4) for d in
                 sorted(range(10), key=lambda d: scores[d], reverse=True)[:3]},
        "size": size_info,
        "pattern": pattern_label(rounds, window),
        "deep": deep,
        "risk": quant.risk_matrix(deep, size_info["p_big"], size_info["confidence"]),
        "bar": quant.confidence_bar(conviction),
    }

    return Prediction(
        id=0,
        mode=mode.key,
        period_label=period,
        number=number,
        colours=colours_for(number),
        size=size_for(number),
        parity=parity_for(number),
        conviction=conviction,
        factors=factors,
        created_at=int(time.time()),
    )


# --------------------------------------------------------------------------- #
# AI answer gate
# --------------------------------------------------------------------------- #
_REFUSAL_RE = re.compile(
    r"\b(cannot|can't|unable to|sorry|as an ai|i'm not able|i am not able|"
    r"no financial advice|do not predict|can not predict)\b", re.IGNORECASE)


def local_call_text(pred: Prediction) -> str:
    """The deterministic desk read used when no provider can answer.

    Always carries an explicit CALL so the card never publishes an answer
    without a verdict attached.
    """
    side = pred.size
    return (
        f"Local model read: window favours {side} at {pred.conviction}% conviction "
        f"on digit {pred.number}. CALL: {side}"
    )


def is_publishable_ai(text: str, expected: str = "") -> bool:
    """True only for a *usable* analyst answer.

    Publishable means: non-empty, carries an explicit BIG/SMALL CALL, is not a
    refusal/deflection, and - when we already know the side - does not wander
    off into gibberish. Garbage answers are dropped, never shown to members.
    """
    body = (text or "").strip()
    if len(body) < 12 or len(body) > 1200:
        return False
    call = AI_CALL_RE.search(body)
    if not call:
        return False
    # a refusal *before* the CALL is still a refusal - reluctant answers that
    # open with "sorry / cannot / as an AI" never get published, CALL or not
    if _REFUSAL_RE.search(body[:call.start()]):
        return False
    if expected and expected not in ("BIG", "SMALL"):
        return True
    letters = sum(1 for c in body if c.isalpha())
    if letters and letters / len(body) < 0.4:
        return False
    return True


def validate_ai_answer(text: str, expected: str = "") -> str | None:
    """Return a cleaned, publishable answer or ``None`` (never publish bad AI)."""
    body = (text or "").strip()
    if not is_publishable_ai(body, expected):
        return None
    return body


def evaluate(predicted: int, actual: int) -> tuple[int, int, int]:
    """(colour hit, size hit, exact hit) - violet only hits violet."""
    colour_hit = int(bool(set(colours_for(predicted)) & set(colours_for(actual))))
    size_hit = int(size_for(predicted) == size_for(actual))
    exact_hit = int(predicted == actual)
    return colour_hit, size_hit, exact_hit


def window_stats(rounds: list[Round], window: int) -> dict:
    analysis = digit_scores(rounds, window)
    windowed = rounds[:window]
    colours = {}
    sizes = {}
    for rnd in windowed:
        colours[rnd.colour] = colours.get(rnd.colour, 0) + 1
        size = size_for(rnd.number)
        sizes[size] = sizes.get(size, 0) + 1

    colour_run, run_value = 0, None
    for rnd in windowed:
        lead = rnd.colour.split(",")[0]
        if run_value is None:
            run_value = lead
        if lead == run_value:
            colour_run += 1
        else:
            break

    return {
        "window": analysis["window"],
        "hot": analysis["hot"],
        "cold": analysis["cold"],
        "due": analysis["due"],
        "colours": colours,
        "sizes": sizes,
        "colour_run": colour_run,
        "colour_run_value": run_value,
        "shares": analysis["shares"],
    }
