"""Everything the bot says and every button it shows.

Cards are rendered as a single HTML message that the bot *edits in place*, so a
node never floods a chat with separate texts - the same message keeps updating.
"""
from __future__ import annotations

import html
import re
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

from .engine import ModeState
from .modes import COLOUR_EMOJI, MODES, ORDER, Mode, colours_for, size_for
from .storage import User

RULE = "━" * 18
FOOTER = "<i>✨ made by alins</i>"
PROVIDER_LABEL = {
    "gemini": "Gemini",
    "openrouter": "Nemotron",
    "groq": "Groq",
    "local": "local fallback",
}
AI_CALL_RE = re.compile(r"CALL\s*:\s*(BIG|SMALL)", re.IGNORECASE)


def esc(value) -> str:
    return html.escape(str(value if value is not None else ""))


def ai_size_call(comment: str | None) -> str:
    """The analyst's explicit BIG/SMALL answer, if the reply carried one."""
    match = AI_CALL_RE.search(comment or "")
    return match.group(1).upper() if match else ""


def mmss(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


def pretty_date(value: str) -> str:
    try:
        stamp = datetime.strptime(value or "", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "—"
    return stamp.strftime("%d %b %Y · %H:%M")


def streak_label(scorecard: dict) -> str:
    length = int(scorecard.get("streak_len") or 0)
    kind = scorecard.get("streak_kind") or "-"
    if not length:
        return "—"
    if kind == "W":
        return f"W{length}" + (" 🔥" if length >= 2 else " ✅")
    return f"L{length} 🧊"


def signal_for(pred) -> tuple[str, str, str]:
    """HOLD system: grade the pick from conviction + size-model confidence.

    Returns (code, emoji, label):
      hold   - no edge, the model advises sitting the round out,
      watch  - weak edge, paper only,
      play   - solid edge,
      strong - the strongest signal the window can produce.
    """
    if pred is None:
        return "warming", "⏳", "WARMING"
    factors = pred.factors if isinstance(pred.factors, dict) else {}
    size_info = factors.get("size") or {}
    try:
        conf = int(size_info.get("confidence", 0) or 0)
    except (TypeError, ValueError):
        conf = 0
    try:
        conv = int(pred.conviction or 50)
    except (TypeError, ValueError):
        conv = 50
    score = (conv - 50) + 0.4 * conf
    if conv <= 54 or conf <= 3:
        return "hold", "⏸", "HOLD"
    if score >= 32:
        return "strong", "💎", "STRONG"
    if score >= 16:
        return "play", "🟢", "PLAY"
    return "watch", "🟡", "WATCH"


def meter(value: int, span: int = 100, cells: int = 8) -> str:
    """Confidence / probability bar for the card."""
    filled = max(0, min(cells, round((value / span) * cells))) if span else 0
    return "▰" * filled + "▱" * (cells - filled)


def colour_emoji(colour: str) -> str:
    # the API reports colours uppercase ("RED"), the emoji map is lowercase
    return COLOUR_EMOJI.get((colour or "").split(",")[0].strip().lower(), "⚪")


def sides_phrase(context: str) -> str:
    """Decoder context is a compact side string ("BSB") -> 'B → S → B'.

    Accepts the older comma form too ("BIG,SMALL,BIG").
    """
    ctx = (context or "").strip()
    if not ctx or ctx == "-":
        return "—"
    if "," in ctx:
        return " → ".join(p.strip().upper() for p in reversed(ctx.split(",")) if p.strip())
    # compact: newest-first, so reverse to chronological order
    return " → ".join(c.upper() for c in reversed(ctx))


def colours_label(colours: list[str]) -> str:
    return " ".join(COLOUR_EMOJI.get(c, "⚪") for c in colours) or "⚪"


# --------------------------------------------------------------------------- #
# onboarding
# --------------------------------------------------------------------------- #
def onboard_name() -> str:
    return (
        "👋 <b>Welcome to WINGO NEXUS AI</b>\n\n"
        "The desk runs four live nodes at once, watched by two analysts - "
        "<b>ROB</b> and <b>ROD</b>.\n\n"
        "First things first: <b>what's your name?</b>\n"
        "<i>Send it as a message - it is stored with your profile.</i>"
    )


def onboard_age(name: str) -> str:
    return (
        f"Nice to meet you, <b>{esc(name)}</b> 🤝\n\n"
        "One gate before the desk opens:\n\n"
        "🔞 <b>Are you over 18?</b>\n\n"
        "This service carries adult-only prediction content. Answer honestly - "
        "a <b>No</b> permanently revokes access for this account and any number "
        "linked to it."
    )


def age_declined() -> str:
    return (
        "🚫 <b>Access denied</b>\n\n"
        "You confirmed you are under 18, so this account is permanently banned "
        "from WINGO NEXUS AI. Re-trying with this account or with a number linked "
        "to it will not restore access.\n\n"
        "<i>Removed by request - come back when you're older.</i>"
    )


def banned_notice(reason: str = "") -> str:
    tail = f"\n<i>Reason: {esc(reason)}</i>" if reason else ""
    return "⛔ <b>Your access has been revoked.</b>" + tail


# --------------------------------------------------------------------------- #
# menu + cards
# --------------------------------------------------------------------------- #
def menu(user: User, states: dict[str, ModeState], trial: dict | None = None) -> str:
    name = esc(user.name or user.username or "there")
    lines = [
        f"👋 <b>Welcome back, {name}</b>",
        "🤖 <b>WINGO NEXUS AI</b> · 4 live nodes · analysts <b>ROB</b> &amp; <b>ROD</b>",
        "<i>━ tap a node to wake it ━</i>",
        RULE,
    ]
    for key in ORDER:
        state = states.get(key)
        if not state or not state.ready:
            lines.append(f"<b>{state.mode.node_label() if state else 'NODE'}</b> · {esc(key)} — syncing… ⏳")
            continue
        if not state.prediction:
            lines.append(
                f"<b>{state.mode.node_label()}</b> · {esc(state.mode.label)} — "
                f"💤 idle · results still tracked · <code>{esc(state.issue[-6:]) if state.issue else '—'}</code>"
            )
            continue
        pick = state.prediction.number
        emoji = colours_label(state.prediction.colours)
        lines.append(
            f"<b>{state.mode.node_label()}</b> · {esc(state.mode.label)} — "
            f"🔮 <b>{pick}</b> {emoji} · <b>{esc(state.prediction.size)}</b> · "
            f"<code>{esc(state.issue[-6:])}</code> · {mmss(state.seconds_left())}"
        )
    if trial and trial.get("trial"):
        hours = max(0, trial.get("window_left", 0)) // 3600
        mins = (max(0, trial.get("window_left", 0)) % 3600) // 60
        lines += [
            RULE,
            f"🎟️ <b>TRIAL</b> — {int(trial.get('used', 0))}/{int(trial.get('quota', 0))} "
            f"predictions used · {hours}h {mins}m left",
            f"   <i>{_cells(int(trial.get('left', 0)), max(1, int(trial.get('quota', 10))))} "
            f"credits remaining</i>",
        ]
    lines += [
        RULE,
        "Pick a node to open its <b>auto-updating signal card</b> - the card "
        "refreshes itself, no spam, no separate texts.",
        FOOTER,
    ]
    return "\n".join(lines)


def _hms(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def _cells(filled: int, total: int = 10, on: str = "🟨", off: str = "⬛") -> str:
    filled = max(0, min(total, int(filled)))
    return on * filled + off * (total - filled)


def card(user: User, state: ModeState) -> str:
    """The box-drawing combat card: uplink, matrix, prediction, deep
    quant analytics, risk matrix, combat ledger, session stats - one HUD."""
    mode = state.mode
    advanced = bool(user.settings.get("advanced", True))
    pred = state.prediction
    factors = pred.factors if pred and isinstance(pred.factors, dict) else {}
    size_info = factors.get("size") or {}
    deep = factors.get("deep") or {}
    risk = factors.get("risk") or {}

    top = "┏" + "━" * 30 + "┓"
    mid = "┣" + "━" * 30 + "┫"
    end = "┗" + "━" * 30 + "┛"
    status = state.status()
    badge = {"LIVE": "🟢", "SETTLING": "🟠", "SETTLED": "✅", "SYNCING": "⏳"}.get(status, "")
    epoch = state.issue or "—"
    target_epoch = "—"
    if state.issue and str(state.issue).isdigit():
        target_epoch = str(int(state.issue) + 1)

    lines = [top]

    # --- 1. uplink ----------------------------------------------------- #
    lines += [
        f"┃ 📡 <b>UPLINK NODE</b> : HEAD_{esc(mode.label.replace(' ', ''))}",
        f"┃ ⚡ <b>LATENCY</b> : {int(state.latency_ms)}ms",
        f"┃ ⏱ <b>LIVE EPOCH</b> : <code>{esc(epoch)}</code>",
        f"┃ ⏳ <b>REMAINING</b> : {mmss(state.seconds_left())} {badge} <b>{esc(status)}</b>",
        mid,
    ]

    # --- 2. matrix snapshot -------------------------------------------- #
    lines.append("┃ 👁️‍🗨️ <b>MATRIX SNAPSHOT</b>")
    if state.last_round:
        rnd = state.last_round
        target = state.last_target or size_for(rnd.number)
        colour = state.last_target_colour or rnd.colour
        streak = int(state.streak or 0)
        streak_side = state.streak_side or size_for(rnd.number)
        if streak:
            streak_txt = f"{streak} {streak_side}{'s' if streak != 1 else ''}"
        else:
            streak_txt = "none"
        lines += [
            f"┃ ├ 🎯 <b>LAST TARGET</b> : [ {esc(target)} ]",
            f"┃ ├ 🎨 <b>LAST COLOR</b> : [ {colour_emoji(colour)} {esc(colour.upper())} ]",
            f"┃ └ 🔥 <b>ACTIVE STRK</b> : {esc(streak_txt)}",
        ]
    else:
        lines.append("┃ └ ⏳ first sync in progress…")
    lines.append(mid)

    # --- 3. prediction -------------------------------------------------- #
    lines.append("┃ 🧠 <b>QUANTUM AI PREDICTION</b>")
    if pred:
        p_big = float(size_info.get("p_big", 0.5))
        conf = int(size_info.get("confidence", 0) or 0)
        bar = str(factors.get("bar") or _cells(round(conf / 10), 12))
        # Bidding-period rule: while the round is open and the analyst has
        # not answered yet, the direct answer stays hidden - the AI reply
        # lands and the card flips itself in real time.
        bidding = status in ("LIVE", "SETTLING") and not (pred.ai_comment or "").strip()
        if bidding:
            lines += [
                f"┃ ↳ ⏳ <b>TARGET EPOCH</b>: <code>{esc(target_epoch)}</code>",
                f"┃ ↳ 🧬 <b>STATUS</b> : AI decoding the window…",
                f"┃ ↳ 📊 <b>PROBABILITY</b> : [{bar}]",
                "┃ ↳ 🔒 <b>ANSWER</b> : withheld until "
                f"<b>{esc(mode.ai)}</b> replies — this card updates itself",
                f"┃ ↳ 🤖 <i>{esc(mode.ai)} is reading {int(factors.get('window', 0) or 0)}"
                " rounds right now…</i>",
            ]
        else:
            code, sig_emoji, sig_text = signal_for(pred)
            override = factors.get("ai_override") or {}
            confirmed = " 🧠" if override.get("call") else ""
            lines += [
                f"┃ ↳ ⏳ <b>TARGET EPOCH</b>: <code>{esc(target_epoch)}</code>",
                f"┃ ↳ 🚀 <b>APEX CALL</b>: {sig_emoji} [ <b>{esc(pred.size)}</b> ] {sig_emoji}{confirmed}",
                f"┃ ↳ 🎯 <b>CONFIDENCE</b> : {int(pred.conviction)}%",
                f"┃ ↳ 📊 <b>PROBABILITY</b> : [{bar}]",
                f"┃ ↳ 🔢 <b>VALUE</b> : <b>{pred.number}</b> {colours_label(pred.colours)} · "
                f"P(BIG) <b>{p_big:.0%}</b> / P(SMALL) <b>{1 - p_big:.0%}</b>",
                f"┃ ↳ 🚦 <b>SIGNAL</b> : {sig_emoji} <b>{esc(sig_text)}</b> · window "
                f"{int(factors.get('window', 0) or 0)} rounds",
            ]
            if code == "hold":
                lines.append("┃ ↳ ⏸ <i>no edge — sit this round out</i>")
        if advanced and not bidding:
            pattern = factors.get("pattern") or {}
            decoder = size_info.get("decoder") or {}
            if decoder and decoder.get("label"):
                chrono = sides_phrase(str(decoder.get("context") or ""))
                lines.append(
                    f"┃ ↳ 🧩 <b>PATTERN</b> {esc(decoder.get('label', '-'))} · after "
                    f"<b>{esc(chrono)}</b> → BIG <b>{float(decoder.get('p', 0.5)):.0%}</b> "
                    f"({int(decoder.get('hits', 0))}/{int(decoder.get('total', 0))})"
                )
            elif pattern.get("label"):
                lines.append(f"┃ ↳ 🧩 <b>PATTERN</b> {esc(pattern.get('label', '-'))}")
            provider = PROVIDER_LABEL.get(pred.ai_provider, pred.ai_provider or "")
            model = f" <code>{esc(pred.ai_model)}</code>" if pred.ai_model and pred.ai_provider != "local" else ""
            call = ai_size_call(pred.ai_comment)
            head = f"┃ ↳ 🤖 <b>{mode.ai}</b>"
            if provider:
                head += f" via <b>{esc(provider)}</b>"
            head += model
            lines.append(head)
            if call:
                lines.append(f"┃ ↳ 🧩 <b>Pattern read → {esc(call)}</b>")
            if pred.ai_comment:
                quote = AI_CALL_RE.sub("", pred.ai_comment).strip()
                if len(quote) > 200:
                    quote = quote[:197] + "…"
                lines.append(f"┃ ↳ 💬 <i>{esc(quote)}</i>")
            else:
                lines.append("┃ ↳ 💬 <i>analyst is reading the window…</i>")
    else:
        lines.append("┃ ↳ ⏳ model warming up…")
    lines.append(mid)

    # --- 4. deep quantum analytics -------------------------------------- #
    if advanced and deep:
        ent = deep.get("entropy") or {}
        zs = deep.get("zscore") or {}
        rsi = deep.get("rsi") or {}
        macd = deep.get("macd") or {}
        ly = deep.get("lyapunov") or {}
        chi = deep.get("chi2") or {}
        runs = deep.get("runs") or {}
        markov = deep.get("markov") or {}
        z = float(zs.get("z", 0) or 0)
        z_sign = "+" if z >= 0 else ""
        lines += [
            "┃ 🔬 <b>DEEP QUANTUM ANALYTICS</b>",
            f"┃ ├ 🧬 <b>ENTROPY</b> : {float(ent.get('pct', 0) or 0):.2f}% "
            f"({esc(ent.get('label', '-'))})",
            f"┃ ├ 📉 <b>Z-SCORE</b> : {z_sign}{z:.3f}σ ({esc(zs.get('label', '-'))}) · "
            f"p={float(zs.get('p_value', 1) or 1):.3f}",
            f"┃ ├ 📈 <b>RSI ({int(rsi.get('period', 14) or 14)})</b> : "
            f"{float(rsi.get('rsi', 50) or 50):.1f} ({esc(rsi.get('label', '-'))})",
            f"┃ ├ 🌊 <b>MACD</b> : {float(macd.get('macd', 0) or 0):+.4f} "
            f"({esc(macd.get('label', '-'))})",
            f"┃ ├ 🌀 <b>LYAPUNOV</b> : {float(ly.get('lambda', 0) or 0):+.4f} "
            f"({esc(ly.get('label', '-'))})",
            f"┃ ├ 📐 <b>CHI²</b> : {float(chi.get('x2', 0) or 0):.2f}/{int(chi.get('df', 9))} "
            f"({esc(chi.get('label', '-'))} p={float(chi.get('p_value', 1) or 1):.3f})",
            f"┃ ├ 🔢 <b>RUNS Z</b> : {float(runs.get('z', 0) or 0):+.2f} "
            f"({esc(runs.get('label', '-'))} {int(runs.get('runs', 0))}/"
            f"{float(runs.get('expected', 0) or 0):.1f})",
        ]
        if markov.get("total", 0) >= 4:
            lines.append(
                f"┃ ├ 🧮 <b>MARKOV[{esc(str(markov.get('context', '-')))}]</b> : "
                f"→ BIG <b>{float(markov.get('p_big', 0.5)):.0%}</b> "
                f"({int(markov.get('hits', 0))}/{int(markov.get('total', 0))}) "
                f"{esc(markov.get('label', '-'))}"
            )
        lines += [
            f"┃ └ 📊 <b>TREND</b> : {esc(deep.get('trend', macd.get('trend', '-')))}",
        ]
        lines.append(mid)

    # --- 5. aegis risk & threat matrix ---------------------------------- #
    if risk:
        threat = float(risk.get("threat", 0) or 0)
        level = int(risk.get("level", 0) or 0)
        lines += [
            "┃ 🛡️ <b>AEGIS RISK &amp; THREAT MATRIX</b>",
            f"┃ ├ ⚠️ <b>THREAT LVL</b> : [{_cells(level, 10, '🟥', '⬛')}] {threat:.1f}%",
            f"┃ ├ 🛡️ <b>RISK MODE</b> : [ {esc(risk.get('mode', '-'))} ]",
            f"┃ └ 💰 <b>ALLOCATION</b> : {int(risk.get('units', 0) or 0)} Units",
            mid,
        ]

    # --- 6. deep combat ledger ------------------------------------------ #
    ledger = state.ledger or []
    lines.append("┃ 📜 <b>DEEP COMBAT LEDGER</b>")
    if ledger:
        for row in ledger:
            mark = "✅ WIN" if row.get("hit") else "❌ LOSS"
            pnl = float(row.get("pnl", 0) or 0)
            sign = "+" if pnl >= 0 else ""
            call = "B" if str(row.get("call", "")).upper().startswith("B") else "S"
            actual = "B" if str(row.get("actual", "")).upper().startswith("B") else "S"
            lines.append(
                f"┣ {esc(str(row.get('period', '—')))} | AI:{call} » Act:{actual} | "
                f"{mark} ({sign}{pnl:.1f}U)"
            )
    else:
        lines.append("┃ └ no settled calls yet")
    lines.append(mid)

    # --- 7. session analytics -------------------------------------------- #
    session = state.session or {}
    total = int(session.get("total", 0) or 0)
    wins = int(session.get("wins", 0) or 0)
    losses = int(session.get("losses", 0) or 0)
    rate = float(session.get("rate", 0) or 0)
    pnl = float(session.get("pnl", 0) or 0)
    lines += [
        "┃ 🏆 <b>SESSION ANALYTICS</b>",
        f"┃ ├ 🏅 <b>WIN RATE</b> : {rate:.1f}%",
        f"┃ ├ ⚔️ <b>RECORD</b> : {wins}W - {losses}L",
        f"┃ └ 📈 <b>NET P/L</b> : {'+' if pnl >= 0 else ''}{pnl:.2f} Units",
        end,
    ]

    # --- status line ----------------------------------------------------- #
    lines.append(
        f"<i>⠼ {esc(mode.label)} engine · {state.rounds_available} verified nodes · "
        f"uptime {_hms(state.uptime_s)}</i>"
    )
    if advanced:
        lines.append(
            f"<i>⚡ {esc(mode.ai)} + quant model · every round is independent</i>"
        )
    lines.append(FOOTER)
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# secondary views
# --------------------------------------------------------------------------- #
def stats_view(user: User, key: str, state: ModeState, window: dict) -> str:
    mode = MODES[key]
    score = state.scorecard or {}
    lines = [
        f"📊 <b>{mode.node_label()} · WINGO {mode.label}</b> — stats",
        RULE,
        f"🎯 Colour hit <b>{score.get('colour_rate', 0)}%</b> "
        f"({score.get('colour_hits', 0)}/{score.get('total', 0)})"
        f" · exact <b>{score.get('exact_rate', 0)}%</b>",
        f"📏 Big/Small hit <b>{score.get('size_rate', 0)}%</b> "
        f"({score.get('size_hits', 0)}/{score.get('total', 0)})"
        f" · streak <b>{streak_label(score)}</b>",
        f"🎨 Colour streak <b>{score.get('colour_streak_kind', '')}"
        f"{score.get('colour_streak_len', 0)}</b>",
        f"📈 Window {window.get('window', 0)} rounds · run "
        f"{window.get('colour_run', 0)}× {esc(window.get('colour_run_value') or '')}",
        "",
        "🟩 green {} · 🟥 red {} · 🟪 violet {}".format(
            window.get("colours", {}).get("green", 0),
            window.get("colours", {}).get("red", 0),
            window.get("colours", {}).get("violet", 0),
        ),
        "📏 BIG {} · SMALL {}".format(
            window.get("sizes", {}).get("BIG", 0),
            window.get("sizes", {}).get("SMALL", 0),
        ),
        "",
        f"🔥 Hot digits <b>{', '.join(str(d) for d in window.get('hot', [])) or '—'}</b>",
        f"🧊 Cold digits <b>{', '.join(str(d) for d in window.get('cold', [])) or '—'}</b>",
        f"⏳ Due digits <b>{', '.join(str(d) for d in window.get('due', [])) or '—'}</b>",
        RULE,
        "<i>Scores: frequency 45% · recency 35% · due-ness 20%</i>",
        FOOTER,
    ]
    return "\n".join(lines)


def status_view(states: dict[str, ModeState], health: dict, snapshot_stats: dict,
                engine_extra: dict | None = None, trial: dict | None = None) -> str:
    """📊 Status button view: one live screen for the whole desk."""
    uptime = int(health.get("uptime_s", 0))
    lines = [
        "📊 <b>DESK STATUS</b>",
        "<i>━ everything live, one tap ━</i>",
        RULE,
        f"🟢 Engine uptime <b>{_hms(uptime)}</b> · "
        f"calls {int(health.get('calls', 0))} · failovers {int(health.get('fallbacks', 0))}",
        f"🤖 AI chain: <b>{esc(health.get('primary', '-'))}</b>"
        + (f" · missing <b>{esc(', '.join(health.get('missing', [])))}</b>"
           if health.get("missing") else ""),
    ]
    for provider in health.get("providers", []):
        if provider.get("configured"):
            state = "🟢" if not (provider.get("cooldown_s") or provider.get("disabled_s")) else "🟡"
            lines.append(f"   {state} {esc(provider['name'])} · <code>{esc(provider.get('model') or '-')}</code>")
    lines.append(RULE)
    for key in ORDER:
        st = states.get(key)
        if not st or not st.ready:
            lines.append(f"<b>{MODES[key].node_label()}</b> — syncing… ⏳")
            continue
        badge = {"LIVE": "🟢", "SETTLING": "🟠", "SETTLED": "✅",
                 "SYNCING": "⏳"}.get(st.status(), "")
        pick = st.prediction.number if st.prediction else "—"
        side = st.prediction.size if st.prediction else "—"
        lines.append(
            f"<b>{st.mode.node_label()}</b> {badge} <b>{esc(st.status())}</b> · "
            f"{mmss(st.seconds_left())} · pick <b>{pick}</b> {esc(side)} · "
            f"<code>{esc(st.issue[-6:]) if st.issue else '—'}</code> · "
            f"{st.rounds_available} rounds"
        )
    lines.append(RULE)
    lines.append(
        f"📸 Snapshots {int(snapshot_stats.get('live', 0))} live "
        f"({int(snapshot_stats.get('deleted', 0))} auto-deleted)"
    )
    if engine_extra:
        if engine_extra.get("gate_rejects") is not None:
            lines.append(
                f"🚦 AI gate rejected <b>{int(engine_extra['gate_rejects'])}</b> bad "
                "answers (never published)"
            )
        if engine_extra.get("queued") is not None:
            lines.append(f"📥 AI reads queued: <b>{int(engine_extra['queued'])}</b>")
    if trial and trial.get("trial"):
        lines.append(
            f"🎟️ Your trial: <b>{int(trial.get('used', 0))}/{int(trial.get('quota', 0))}</b> "
            f"predictions · {max(0, int(trial.get('window_left', 0))) // 3600}h"
            f"{(max(0, int(trial.get('window_left', 0))) % 3600) // 60}m left"
        )
    lines += [RULE, "<i>⬆️ this screen edits itself - keep it open</i>", FOOTER]
    return "\n".join(lines)


def profile_view(user: User, states: dict[str, ModeState]) -> str:
    joined = datetime.fromtimestamp(user.joined_at).strftime("%d %b %Y") if user.joined_at else "—"
    lines = [
        f"👤 <b>{esc(user.name or 'Unnamed')}</b>",
        f"   @{esc(user.username) if user.username else 'no username'} · id <code>{user.telegram_id}</code>",
        RULE,
        f"🗓 Joined {joined} · stage <b>{esc(user.stage)}</b>",
        f"👑 {'admin' if user.is_admin else 'member'} · age gate "
        + ("<b>verified 18+</b>" if user.stage == "active" else f"<b>{esc(user.stage)}</b>"),
        "",
    ]
    for key in ORDER:
        state = states.get(key)
        if state and state.ready:
            score = state.scorecard or {}
            lines.append(
                f"<b>{state.mode.node_label()}</b> size {score.get('size_rate', 0)}% · "
                f"{streak_label(score)} · {state.rounds_available} rounds"
            )
    lines += [RULE, "<i>Settings, streaks and snapshots live in the buttons below.</i>"]
    return "\n".join(lines)


def settings_view(user: User) -> str:
    s = user.settings
    interval = int(s.get("auto_refresh", 5))
    lines = [
        "⚙️ <b>Settings</b>",
        RULE,
        f"🔄 Auto-refresh — <b>every {interval}s</b> "
        f"({'off' if interval <= 0 else 'card edits itself'})",
        f"🧠 Advanced mode — <b>{'ON' if s.get('advanced') else 'OFF'}</b> "
        "(analyst quotes, provider badges, window detail)",
        f"🔔 Settle alerts on card — <b>{'ON' if s.get('alerts') else 'OFF'}</b>",
        f"📨 Extra push on result — <b>{'ON' if s.get('push') else 'OFF'}</b> "
        "(cards already update themselves)",
        RULE,
        "<i>Changes apply immediately to your open card.</i>",
    ]
    return "\n".join(lines)


def help_view() -> str:
    return (
        "🆘 <b>How WINGO NEXUS AI works</b>\n"
        + RULE + "\n"
        + "<b>Nodes</b> — 30 sec / 1 min / 3 min / 5 min. Each node keeps one card "
        "message that updates itself: <b>NEXT VALUE</b>, the <b>BIG/SMALL answer</b>, "
        "last period + verdict, status, analyst, streak. Results are hidden behind "
        "a tap-to-reveal spoiler; the analyst quote collapses.\n\n"
        + "<b>Lazy desk</b> — opening a node is what starts calculating it: the node "
        "you open gets the model + AI read, the others just keep storing their "
        "last period numbers. Tap 💤 idle nodes in the menu to wake them.\n\n"
        + "<b>The call</b> — the BIG/SMALL side is decided first (balance · digit mass "
        "· run reversion), then the best digit inside that side becomes the NEXT "
        "VALUE, so value and answer always agree. ROB/ROD then read the pattern live "
        "and their explicit CALL can re-lock the answer before the round settles.\n\n"
        + "<b>Analysts</b> — ROB and ROD take turns reading each window. Their calls "
        "route through Gemini, then Nemotron, then Groq - every round gets a model "
        "+ AI answer, and answers that fail the quality gate are never published.\n\n"
        + "<b>Commands</b>\n"
        "• /stats [node] — window analysis\n"
        "• /accuracy [node] — real hit-rates of our last calls\n"
        "• /last [1-20] [node] — recent results\n"
        "• /predict [node] — the current pick\n"
        "• /ask &lt;question&gt; — ask ROB/ROD\n"
        "• /status — engine + provider health (or the 📡 Status button)\n"
        "• /stop — mute pushes · /help — this text\n\n"
        + "<b>Live screens</b> — cards, menu and the status screen all edit "
        "themselves in place. Keep one open and it never goes stale.\n\n"
        "<i>Predictions are statistical, not guarantees. 18+ only.</i>\n"
        + FOOTER
    )


def ask_prompt(persona: str) -> str:
    return (
        f"🤖 <b>{persona}</b> is listening.\n\n"
        "Send your question as one message - the desk routes it through Gemini, "
        "then Nemotron, then Groq, so someone always answers."
    )


# --------------------------------------------------------------------------- #
# SaaS: welcome, activation, subscription
# --------------------------------------------------------------------------- #
def welcome(user: User, active: bool, until: int, cfg,
             ref_code: str = "", trial_used: bool = False) -> str:
    name = esc(user.name or user.username or "there")
    status = (
        f"✅ <b>ACTIVE</b> until <code>{datetime.fromtimestamp(until).strftime('%d %b %Y')}</code>"
        if active else
        "🔒 <b>LOCKED</b> — pick a way in below"
    )
    trial = (
        "<s>⏳ free trial — already used</s>" if trial_used else
        f"🎟️ <b>Free trial</b> — <b>{cfg.trial_predictions} predictions</b> over "
        f"<b>{cfg.trial_hours}h</b>, once per account"
    )
    return (
        "🌟 <b>WINGO NEXUS AI</b> 🌟\n"
        "<i>━━━━ the killer prediction desk ━━━━</i>\n"
        "\n"
        f"👋 Hey <b>{name}</b> — you made it! 🎉 Pull up a chair, the desk is live.\n"
        "\n"
        "🎯 <b>FOUR LIVE NODES</b>\n"
        "30s · 1m · 3m · 5m streaming in parallel — 600+ rounds per game "
        "stored, one self-editing card per node.\n"
        "\n"
        "🧠 <b>DUAL-ENGINE ANALYSIS</b>\n"
        "• BIG/SMALL model: balance · digit mass · momentum · alternation · "
        "reversion\n"
        "• Chart rhythm classifier: <b>STREAKY / ALTERNATING / CHOPPY</b>\n"
        "• Entropy · z-score · RSI · MACD · Lyapunov · chi² · runs · Markov\n"
        "• The value is always picked <i>inside</i> the called side\n"
        "\n"
        "🤖 <b>THREE AI ANALYSTS, BACK TO BACK</b>\n"
        "<b>ROB</b> reads the window · <b>ROD</b> reads the risk — Gemini → "
        "Nemotron → Groq, every round ending in an explicit CALL.\n"
        "Anything unusable gets rejected before you ever see it. 🛡️\n"
        "\n"
        "🚦 <b>HOLD SYSTEM</b> — ⏸ HOLD (no edge) · 🟡 WATCH · 🟢 PLAY · "
        "💎 STRONG\n"
        "\n"
        "📈 <b>HONEST TRACK RECORD</b> — every call scored, live streaks, "
        "<code>/accuracy</code> &amp; <code>/report</code>.\n"
        "\n"
        "🔐 <b>WAYS IN</b>\n"
        f"💳 Full access <b>₹{cfg.price_inr}</b> / month via UPI\n"
        f"{trial}\n"
        f"🔗 Refer a friend — <b>+{cfg.referral_bonus_days} days</b> each "
        "who activates\n"
        "🔑 Have a key? redeem it instantly\n"
        "\n"
        f"{status}\n"
        "\n"
        "<i>━━━━ pick a button below ⬇️ ━━━━</i>\n"
        + FOOTER
    )


def refer_view(ref_code: str, bot_username: str, count: int, bonus_days: int) -> str:
    link = f"https://t.me/{bot_username}?start={ref_code}" if bot_username else "—"
    return (
        "🔗 <b>REFER &amp; EARN</b>\n"
        "<i>━ invite friends, stack days ━</i>\n"
        "\n"
        f"Your code: <code>{esc(ref_code)}</code>\n"
        f"Your link: <code>{esc(link)}</code>\n"
        "\n"
        "<b>How it works</b>\n"
        f"1️⃣ Share the link (friends tap it and /start opens with your code)\n"
        f"2️⃣ The moment they <b>activate</b> (key or payment)\n"
        f"3️⃣ You get <b>+{bonus_days} days</b> added automatically\n"
        "\n"
        f"👥 Friends referred: <b>{count}</b>\n"
        "\n"
        "<tg-spoiler>There is no cap — refer 10 friends, get 10 bonuses. "
        "Alt accounts that never activate earn nothing.</tg-spoiler>\n"
        + FOOTER
    )


def contact_view() -> str:
    return (
        "🤝 <b>CONTACT THE ADMIN</b>\n"
        "\n"
        "Tap below and the admin gets your profile instantly — for payment "
        "issues, renewals, refunds or business queries.\n"
        "\n"
        "<i>Replies usually land within minutes.</i>\n"
        + FOOTER
    )


def buy_view(cfg) -> str:
    upi = esc(cfg.upi_id or "(set UPI_ID in Settings → Environment)")
    price = cfg.price_inr
    return (
        "💳 <b>ACTIVATION — ₹%d / month</b>\n"
        "<i>━ three simple steps ━</i>\n"
        "\n"
        "1️⃣ Send <b>₹%d</b> via UPI to\n"
        "   <code>%s</code>\n"
        "\n"
        "2️⃣ Tap <b>📨 I've paid</b> below and paste your "
        "<b>UPI reference number</b>\n"
        "\n"
        "3️⃣ Admin approves → you receive an "
        "<b>activation key</b> → send it with "
        "<code>/activate KEY</code>\n"
        "\n"
        "⏳ Approval is manual (scam-proof): the desk unlocks the moment "
        "your key is redeemed.\n"
        "\n"
        "<tg-spoiler>One key = one month of full access on one Telegram "
        "account. Renew by paying again before it expires.</tg-spoiler>"
    ) % (price, price, upi)


def pay_submitted(payment_id: int) -> str:
    return (
        f"📨 <b>Payment submitted</b> <code>#{payment_id}</code>\n"
        "\n"
        "Your reference is queued for admin approval. You'll get a message "
        "with your activation key the moment it's approved.\n"
        "\n"
        "<i>You can keep this chat open — nothing else to do.</i>"
    )


def payment_approved(key: str, days: int) -> str:
    return (
        "✅ <b>Payment approved!</b>\n"
        "\n"
        "Your activation key (valid %d days):\n"
        "\n"
        "<tg-spoiler><code>%s</code></tg-spoiler>\n"
        "\n"
        "Redeem it now: <code>/activate %s</code>\n"
        "\n"
        "<i>Keep it private — one key works once.</i>"
    ) % (days, key, key)


def payment_rejected(payment_id: int) -> str:
    return (
        f"❌ <b>Payment #{payment_id} rejected</b>\n"
        "\n"
        "The reference could not be verified. If you did pay, contact the "
        "admin with your UPI screenshot, or resubmit from the Buy screen."
    )


def activated_view(until: int, days: int) -> str:
    stamp = datetime.fromtimestamp(until).strftime("%d %b %Y · %H:%M")
    return (
        "🔓 <b>ACTIVATED!</b> 🎉\n"
        "\n"
        f"Full desk access for <b>{days} days</b> — until <b>{stamp}</b>.\n"
        "\n"
        "<i>Opening your desk…</i>"
    )


def sub_view(user: User, active: bool, until: int, cfg) -> str:
    lines = ["🧾 <b>My subscription</b>", RULE]
    if active:
        stamp = datetime.fromtimestamp(until).strftime("%d %b %Y · %H:%M")
        left = max(0, int((until - time.time()) // 86400))
        lines += [
            "Status <b>✅ ACTIVE</b>",
            f"Valid until <b>{stamp}</b> · <b>{left}</b> day(s) left",
        ]
    else:
        lines += [
            "Status <b>🔒 LOCKED</b>",
            f"Unlock the desk for ₹{cfg.price_inr}/month — see the Buy screen.",
        ]
    lines += [
        RULE,
        f"Account <code>{user.telegram_id}</code>"
        + (f" · @{esc(user.username)}" if user.username else ""),
        FOOTER,
    ]
    return "\n".join(lines)


def how_view() -> str:
    return (
        "📖 <b>How it works</b>\n"
        + RULE + "\n"
        "<b>1.</b> Four live nodes (30s/1m/3m/5m) stream results into the "
        "chart — 600+ rounds per game, always stored.\n"
        "<b>2.</b> The BIG/SMALL model scores balance · digit mass · momentum "
        "· alternation · reversion, then picks the value inside the called side.\n"
        "<b>3.</b> ROB/ROD read the live pattern through Gemini → Groq and can "
        "re-lock the answer before the round settles.\n"
        "<b>4.</b> The card grades everything ⏸ HOLD / 🟡 WATCH / 🟢 PLAY / "
        "💎 STRONG — no edge, no call.\n"
        "\n"
        "🔒 <b>Activation</b> — the desk opens with a monthly key. Nodes only "
        "start calculating once you open them, so AI is never wasted on nodes "
        "you are not watching.\n"
        "\n"
        "<i>Predictions are statistical, not guarantees. 18+ only.</i>\n"
        + FOOTER
    )


def welcome_keyboard(active: bool, trial_used: bool = False) -> InlineKeyboardMarkup:
    rows = []
    if not active:
        rows.append([InlineKeyboardButton("💳 Buy full access",
                                          callback_data="view:buy")])
        if trial_used:
            rows.append([InlineKeyboardButton("⏳ Free trial — used",
                                              callback_data="trial:info")])
        else:
            rows.append([InlineKeyboardButton("⏳ Claim free trial",
                                              callback_data="trial")])
        rows.append([InlineKeyboardButton("🔑 I have an activation key",
                                          callback_data="activate")])
    rows.append([InlineKeyboardButton("🔗 Refer & earn", callback_data="refer")])
    rows.append([InlineKeyboardButton("📊 My subscription", callback_data="view:sub")])
    rows.append([InlineKeyboardButton("📖 How it works", callback_data="view:how")])
    rows.append([InlineKeyboardButton("🤝 Contact admin", callback_data="contact")])
    rows.append([InlineKeyboardButton("🎯 Open the desk", callback_data="desk")])
    return InlineKeyboardMarkup(rows)


def buy_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📨 I've paid — submit reference",
                              callback_data="paid")],
        [InlineKeyboardButton("🔙 Back", callback_data="view:welcome")],
    ])


def payments_keyboard(payments) -> InlineKeyboardMarkup:
    rows = []
    for row in list(payments)[:8]:
        who = row["username"] or str(row["telegram_id"])
        rows.append([
            InlineKeyboardButton(f"✅ #{row['id']} {who}",
                                 callback_data=f"pay:yes:{row['id']}"),
            InlineKeyboardButton(f"❌ #{row['id']}",
                                 callback_data=f"pay:no:{row['id']}"),
        ])
    rows.append([InlineKeyboardButton("🔄 Refresh queue", callback_data="view:admin")])
    return InlineKeyboardMarkup(rows)


# --------------------------------------------------------------------------- #
# admin
# --------------------------------------------------------------------------- #
def admin_view(snapshot_stats: dict, users: dict, health: dict, admin_code: str,
               stats: dict | None = None, pending: int = 0) -> str:
    next_in = ""
    if snapshot_stats.get("next_expiry"):
        left = max(0, snapshot_stats["next_expiry"] - int(time.time()))
        next_in = f" · next deletion in {left // 60}m"
    missing = ", ".join(health.get("missing", [])) or "none"
    s = stats or {}
    uptime = int(health.get("uptime_s", 0))
    days, hours = divmod(uptime, 86400)
    lines = [
        "👑 <b>ADMIN CONSOLE</b>",
        "<i>━ live counters ━</i>",
        RULE,
        f"👥 Users <b>{users.get('active', 0)}</b> seen / <b>{users.get('total', 0)}</b> total "
        f"· {s.get('new_today', 0)} joined today · 🚫 banned {users.get('banned', 0)}",
        f"💎 Subscriptions <b>{s.get('active_subs', 0)}</b> active",
        f"💰 Revenue <b>₹{s.get('revenue', 0)}</b> · approved {s.get('approved', 0)} "
        f"· rejected {s.get('rejected', 0)} · 🧾 <b>pending {pending}</b>",
        f"🔑 Keys: <b>{s.get('keys_active', 0)}</b> unused · {s.get('keys_used', 0)} redeemed "
        f"· {s.get('keys_total', 0)} issued",
        f"📈 Settled calls {s.get('settled_calls', 0)} · rounds stored {s.get('rounds', 0)}",
        f"🤖 AI primary <b>{esc(health.get('primary', '-'))}</b> · calls {health.get('calls', 0)} "
        f"· failovers {health.get('fallbacks', 0)} · missing: {esc(missing)}",
        f"⏱ Uptime {days}d {hours // 3600}h",
        f"📸 Snapshots {snapshot_stats.get('live', 0)} live "
        f"({snapshot_stats.get('deleted', 0)} auto-deleted{next_in})",
        RULE,
        "<b>Members</b>",
        "• 👥 Members — browse, search, act (buttons)\n"
        "• /users [page] · /find &lt;id|@name&gt;\n"
        "• /remove &lt;id&gt; — delete so they can register fresh\n"
        "• /ban &lt;id&gt; [reason] · /unban &lt;id&gt; — permanent\n"
        "• /grant &lt;id&gt; [days] · /revoke &lt;id&gt;\n"
        "• /resettrial &lt;id&gt; — let them claim again\n",
        RULE,
        "<b>Reports &amp; tools</b>",
        "• /report — daily business + hit-rate report\n"
        "• /keys — recent activation keys · /genkey — mint one\n"
        "• /snap · /health · /broadcast · /status\n"
        "• send a value for snapshots: <code>1min</code>, <code>all</code>, a period",
        FOOTER,
    ]
    return "\n".join(lines)


def users_view(page_users: list[dict], page: int, total: int, per_page: int = 8) -> str:
    """Paginated member directory for the admin console."""
    pages = max(1, (total + per_page - 1) // per_page)
    lines = [
        f"👥 <b>MEMBERS</b> — {total} total",
        f"<i>━ page {page + 1}/{pages} · tap a member for actions ━</i>",
        RULE,
    ]
    if not page_users:
        lines.append("<i>No members yet.</i>")
    for row in page_users:
        label = esc(row["label"][:24])
        uname = f"@{esc(row['username'])}" if row.get("username") else "no @"
        flags = []
        if row.get("admin"):
            flags.append("👑")
        if row.get("banned"):
            flags.append("🚫")
        if row.get("active"):
            flags.append("✅")
        elif row.get("plan") == "trial":
            flags.append("🎟️")
        plan = row.get("plan") or "free"
        trial = f" · trial {esc(row['trial'])}" if row.get("trial") else ""
        lines.append(
            f"<code>{row['telegram_id']}</code> · <b>{label}</b> ({uname}) · "
            f"{esc(plan)}{trial} {' '.join(flags)}"
        )
    lines += [RULE, FOOTER]
    return "\n".join(lines)


def users_keyboard(page: int, total: int, per_page: int = 8) -> InlineKeyboardMarkup:
    pages = max(1, (total + per_page - 1) // per_page)
    rows: list[list[InlineKeyboardButton]] = []
    nav: list[InlineKeyboardButton] = []
    if page > 0:
        nav.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"users:{page - 1}"))
    if page + 1 < pages:
        nav.append(InlineKeyboardButton("Next ➡️", callback_data=f"users:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([InlineKeyboardButton("🔍 Find member", callback_data="finduser"),
                 InlineKeyboardButton("🔄 Refresh", callback_data=f"users:{page}")])
    rows.append([InlineKeyboardButton("🔙 Admin console", callback_data="view:admin")])
    return InlineKeyboardMarkup(rows)


def user_detail_view(row: dict) -> str:
    """One member, everything an admin needs, with the action buttons below."""
    status = "✅ active" if row.get("active") else ("🚫 banned" if row.get("banned") else "🔒 locked")
    lines = [
        f"🔎 <b>MEMBER</b> <code>{row['telegram_id']}</code>",
        RULE,
        f"📛 {esc(row['label'])}" + (f" · @{esc(row['username'])}" if row.get("username") else ""),
        f"📦 Plan <b>{esc(row.get('plan') or 'free')}</b> · {status}",
    ]
    if row.get("trial"):
        lines.append(f"🎟️ Trial used <b>{esc(row['trial'])}</b> predictions")
    if row.get("active_until"):
        stamp = datetime.fromtimestamp(int(row["active_until"])).strftime("%d %b %Y · %H:%M")
        lines.append(f"⏳ Until <b>{stamp}</b>")
    if row.get("joined"):
        joined = datetime.fromtimestamp(int(row["joined"])).strftime("%d %b %Y")
        lines.append(f"🗓 Joined <b>{joined}</b>")
    if row.get("banned"):
        lines.append(f"⛔ Reason: <b>{esc(row.get('ban_reason') or '—')}</b>")
    if row.get("phone"):
        lines.append(f"📇 Phone <code>{esc(row['phone'])}</code>")
    lines += [RULE, "<i>Pick an action below 👇</i>", FOOTER]
    return "\n".join(lines)


def user_detail_keyboard(row: dict) -> InlineKeyboardMarkup:
    uid = int(row["telegram_id"])
    rows: list[list[InlineKeyboardButton]] = []
    if row.get("banned"):
        rows.append([InlineKeyboardButton("✅ Unban", callback_data=f"u:unban:{uid}")])
    else:
        rows.append([
            InlineKeyboardButton("🚫 Ban permanently", callback_data=f"u:ban:{uid}"),
            InlineKeyboardButton("🗑 Remove", callback_data=f"u:del:{uid}"),
        ])
    rows.append([
        InlineKeyboardButton("➕ Grant +7d", callback_data=f"u:grant:{uid}:7"),
        InlineKeyboardButton("➕ Grant +30d", callback_data=f"u:grant:{uid}:30"),
    ])
    rows.append([
        InlineKeyboardButton("⏪ Revoke access", callback_data=f"u:revoke:{uid}"),
        InlineKeyboardButton("🎟️ Reset trial", callback_data=f"u:resettrial:{uid}"),
    ])
    rows.append([InlineKeyboardButton("🔙 Back to members", callback_data="users:0")])
    return InlineKeyboardMarkup(rows)


def ban_confirm_view(row: dict) -> str:
    return (
        f"🚫 <b>Permanent ban</b> <code>{row['telegram_id']}</code>\n"
        + RULE + "\n"
        f"<b>{esc(row['label'])}</b> will be locked out for good: the 18+ gate, "
        "any linked numbers and every re-attempt are refused.\n\n"
        "<i>This cannot be undone by the member - only by you.</i>"
    )


def ban_confirm_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, ban forever", callback_data=f"u:ban2:{uid}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"u:view:{uid}")],
    ])


def payments_view(payments) -> str:
    if not payments:
        return (
            "🧾 <b>Payment queue</b>\n" + RULE + "\n"
            "No pending payments. 🎉\n\n"
            "New submissions appear here with approve/reject buttons.\n" + FOOTER
        )
    lines = ["🧾 <b>Payment queue</b>", "<i>━ tap to decide ━</i>", RULE]
    for row in payments:
        who = f"@{esc(row['username'])}" if row["username"] else esc(str(row["telegram_id"]))
        ref = esc(row["upi_ref"] or "—")
        stamp = datetime.fromtimestamp(int(row["created_at"])).strftime("%d %b %H:%M")
        lines.append(
            f"#{row['id']} · <b>₹{row['amount']}</b> · {who}\n"
            f"   ref <code>{ref}</code> · {stamp}"
        )
    lines += [RULE, "<i>✅ = approve + issue key · ❌ = reject</i>", FOOTER]
    return "\n".join(lines)


def report_view(biz: dict, health: dict, rates: list[str], log_lines: list[str]) -> str:
    lines = [
        "📊 <b>DAILY REPORT</b>",
        "<i>━ business + performance ━</i>",
        RULE,
        f"👥 New today <b>{biz.get('new_today', 0)}</b> · total {biz.get('users', 0)}",
        f"💎 Active subs <b>{biz.get('active_subs', 0)}</b> · pending payments {biz.get('pending', 0)}",
        f"💰 Revenue <b>₹{biz.get('revenue', 0)}</b> (approved {biz.get('approved', 0)})",
        f"🔑 Keys unused <b>{biz.get('keys_active', 0)}</b> / issued {biz.get('keys_total', 0)}",
        f"📈 Settled calls {biz.get('settled_calls', 0)} · rounds {biz.get('rounds', 0)}",
        f"🤖 AI calls {health.get('calls', 0)} · failovers {health.get('fallbacks', 0)}",
        RULE,
        "<b>Hit-rates (last 30 calls per node)</b>",
    ]
    lines.extend(rates or ["—"])
    if log_lines:
        lines += [RULE, "<b>Last log lines</b>"]
        for line in log_lines:
            lines.append(f"<code>{esc(line[:110])}</code>")
    lines.append(FOOTER)
    return "\n".join(lines)


def health_view(health: dict) -> str:
    lines = ["🩺 <b>AI chain health</b>", RULE]
    lines.append(
        f"Primary <b>{esc(health.get('primary', '-'))}</b> · "
        f"calls {health.get('calls', 0)} · failovers {health.get('fallbacks', 0)}"
    )
    for provider in health.get("providers", []):
        state = "🟢 ready"
        if provider.get("disabled_s"):
            state = f"⛔ disabled {provider['disabled_s']}s"
        elif provider.get("cooldown_s"):
            state = f"🟡 cooldown {provider['cooldown_s']}s"
        elif not provider.get("configured"):
            state = "⚪ key missing"
        lines.append(
            f"\n<b>{esc(provider['name'])}</b> {state}\n"
            f"  model <code>{esc(provider.get('model') or '-')}</code>\n"
            f"  calls {provider.get('calls', 0)} · errors {provider.get('errors', 0)}"
            + (f"\n  last: {esc(provider['last_error'])}" if provider.get("last_error") else "")
        )
    lines += [RULE, "<i>The loop walks Gemini → Nemotron → Groq until someone answers.</i>"]
    return "\n".join(lines)


def broadcast_prompt(count: int) -> str:
    return (
        f"📢 <b>Broadcast</b> — {count} active chats.\n\n"
        "Send the exact text to send. /cancel to abort."
    )


# --------------------------------------------------------------------------- #
# keyboards
# --------------------------------------------------------------------------- #
def age_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Yes, I'm 18+", callback_data="age:yes"),
         InlineKeyboardButton("🚫 No", callback_data="age:no")],
    ])


def node_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"⚡ NODE 01 · 30 SEC", callback_data="node:30sec"),
         InlineKeyboardButton(f"⚡ NODE 02 · 1 MIN", callback_data="node:1min")],
        [InlineKeyboardButton(f"⚡ NODE 03 · 3 MIN", callback_data="node:3min"),
         InlineKeyboardButton(f"⚡ NODE 04 · 5 MIN", callback_data="node:5min")],
    ])


def menu_keyboard(user: User) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⚡ NODE 01 · 30 SEC", callback_data="node:30sec"),
         InlineKeyboardButton("⚡ NODE 02 · 1 MIN", callback_data="node:1min")],
        [InlineKeyboardButton("⚡ NODE 03 · 3 MIN", callback_data="node:3min"),
         InlineKeyboardButton("⚡ NODE 04 · 5 MIN", callback_data="node:5min")],
        [InlineKeyboardButton("📊 Stats", callback_data="view:stats"),
         InlineKeyboardButton("📡 Status", callback_data="view:status"),
         InlineKeyboardButton("⚙️ Settings", callback_data="view:settings")],
        [InlineKeyboardButton("👤 Profile", callback_data="view:profile"),
         InlineKeyboardButton("🤖 Ask AI", callback_data="ask"),
         InlineKeyboardButton("🆘 Help", callback_data="view:help")],
    ]
    if user.is_admin:
        rows.append([InlineKeyboardButton("👑 Admin", callback_data="view:admin")])
    return InlineKeyboardMarkup(rows)


def card_keyboard(key: str, user: User) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton("⚡ 30s", callback_data="node:30sec"),
         InlineKeyboardButton("⚡ 1m", callback_data="node:1min"),
         InlineKeyboardButton("⚡ 3m", callback_data="node:3min"),
         InlineKeyboardButton("⚡ 5m", callback_data="node:5min")],
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{key}"),
         InlineKeyboardButton("📊 Stats", callback_data="view:stats"),
         InlineKeyboardButton("📡 Status", callback_data="view:status")],
        [InlineKeyboardButton("⚙️ Settings", callback_data="view:settings"),
         InlineKeyboardButton("🤖 Ask AI", callback_data="ask"),
         InlineKeyboardButton("🆘 Help", callback_data="view:help")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
    ]
    if user.is_admin:
        rows.append([InlineKeyboardButton("👑 Admin", callback_data="view:admin")])
    return InlineKeyboardMarkup(rows)


def back_keyboard(view: str = "menu", node: str = "") -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton("🔙 Back", callback_data=f"back:{view}:{node}")]]
    if node:
        rows[0].insert(0, InlineKeyboardButton("🎯 Open card", callback_data=f"node:{node}"))
    return InlineKeyboardMarkup(rows)


def settings_keyboard(user: User) -> InlineKeyboardMarkup:
    s = user.settings
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"🔄 Auto-refresh: {s.get('auto_refresh', 5)}s",
                              callback_data="toggle:auto_refresh"),
         InlineKeyboardButton(f"🧠 Advanced: {'ON' if s.get('advanced') else 'OFF'}",
                              callback_data="toggle:advanced")],
        [InlineKeyboardButton(f"🔔 Alerts: {'ON' if s.get('alerts') else 'OFF'}",
                              callback_data="toggle:alerts"),
         InlineKeyboardButton(f"📨 Push: {'ON' if s.get('push') else 'OFF'}",
                              callback_data="toggle:push")],
        [InlineKeyboardButton("🔙 Back", callback_data="back:menu:")],
    ])


def admin_keyboard(user: User | None = None) -> InlineKeyboardMarkup:
    auto = bool(user is not None and user.settings.get("auto_snapshots"))
    rows = [
        [InlineKeyboardButton("🧾 Payment queue", callback_data="view:payments"),
         InlineKeyboardButton("📊 Report", callback_data="report")],
        [InlineKeyboardButton("📸 Snap 30s", callback_data="snap:30sec"),
         InlineKeyboardButton("📸 Snap 1m", callback_data="snap:1min")],
        [InlineKeyboardButton("📸 Snap 3m", callback_data="snap:3min"),
         InlineKeyboardButton("📸 Snap 5m", callback_data="snap:5min")],
        [InlineKeyboardButton("📸 All nodes", callback_data="snap:all"),
         InlineKeyboardButton(
             f"📸 Auto photos: {'⏹ STOP' if auto else '▶️ START'}",
             callback_data="toggle:auto_snapshots")],
        [InlineKeyboardButton("👥 Members", callback_data="users:0"),
         InlineKeyboardButton("🩺 AI health", callback_data="view:health")],
        [InlineKeyboardButton("📢 Broadcast", callback_data="broadcast"),
         InlineKeyboardButton("📈 Status", callback_data="view:status")],
        [InlineKeyboardButton("🔙 Back", callback_data="back:menu:")],
    ]
    return InlineKeyboardMarkup(rows)


def health_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👑 Admin console", callback_data="view:admin"),
         InlineKeyboardButton("🏠 Menu", callback_data="menu")],
    ])


def snap_keyboard(node: str = "") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📸 Another snapshot", callback_data="view:admin")],
        [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
    ])
