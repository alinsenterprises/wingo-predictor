"""The live engine: one supervised poll loop per game.

Each cycle (per README cadence):
  1. ask the API which issue is live now,
  2. pull page 1 of that game's history, insert only the rounds we do not have,
  3. prune to MAX_RECORDS and append the new rounds to data/history/<game>.jsonl,
  4. score every still-open prediction against the rounds that just landed,
  5. make sure the live issue has a prediction (creates one and queues an AI read),
  6. publish an immutable ModeState that the bot and the status page render.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime

from . import analyzer
from .api import DmFirstClient
from .config import Config
from .errors import ApiError
from .modes import MODES, Mode, colours_for, parity_for, size_for
from .orchestrator import AIResult, Orchestrator
from .personas import build_commentary_prompt, persona_for, system_prompt
from .storage import Prediction, Round, Storage

log = logging.getLogger("wingo.engine")

TS_FMT = "%Y-%m-%d %H:%M:%S"


@dataclass
class ModeState:
    mode: Mode
    issue: str = ""
    start: str = ""
    end: str = ""
    server_now: str = ""
    interval_m: float = 0.0
    remaining: float = 0.0          # seconds left, measured at synced_at
    synced_at: float = 0.0          # monotonic stamp for the countdown
    synced_wall: float = 0.0        # wall-clock stamp for "synced Ns ago"
    last_round: Round | None = None
    prediction: Prediction | None = None
    last_settled: Prediction | None = None
    scorecard: dict = field(default_factory=dict)
    rounds_available: int = 0
    error: str = ""
    ready: bool = False
    latency_ms: int = 0
    last_target: str = ""
    last_target_colour: str = ""
    streak: int = 0
    streak_side: str = ""
    ledger: list[dict] = field(default_factory=list)
    session: dict = field(default_factory=dict)
    uptime_s: float = 0.0

    def seconds_left(self) -> float:
        if not self.ready:
            return 0.0
        return max(0.0, self.remaining - (time.monotonic() - self.synced_at))

    def status(self) -> str:
        if not self.ready:
            return "SYNCING"
        if self.prediction and self.prediction.evaluated_at:
            return "SETTLED"
        if self.seconds_left() <= 0:
            return "SETTLING"
        return "LIVE"


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value or "", TS_FMT)
    except ValueError:
        return None


class Engine:
    def __init__(self, cfg: Config, storage: Storage, api: DmFirstClient,
                 orchestrator: Orchestrator):
        self.cfg = cfg
        self.storage = storage
        self.api = api
        self.orch = orchestrator
        self.states: dict[str, ModeState] = {
            key: ModeState(mode=mode) for key, mode in MODES.items()
        }
        self.ai_queue: list[tuple[str, str]] = []
        self.last_ai_at: dict[str, float] = {}
        self.no_data_logged: set[str] = set()
        self.gate_rejects = 0
        self.running = False
        self.boot_at = time.time()
        self.last_cycle: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    def state(self, key: str) -> ModeState:
        return self.states.get(key) or ModeState(mode=MODES[key])

    def seconds_left(self, key: str) -> float:
        return self.state(key).seconds_left()

    # ---------------- one cycle (runs in a worker thread) --------------- #
    def sync(self, key: str) -> None:
        t0 = time.monotonic()
        mode = MODES[key]
        issue = self.api.game_issue(mode.type_id)
        rows = self.api.history(mode.type_id, 1, self.cfg.results_page_size)
        if not rows:
            raise ApiError(f"{key}: empty history payload", retryable=True)

        existing = {rnd.period for rnd in self.storage.rounds(key, len(rows) + 10)}
        fresh = [row for row in rows if str(row.get("issueNumber")) not in existing]
        if fresh:
            if self.storage.insert_rounds(key, fresh):
                self.storage.prune(key, self.cfg.max_records)
                self._archive(key, fresh)
                log.info("%s: %s new rounds (%s total)", key, len(fresh),
                         self.storage.round_count(key))

        # 1. settle anything the new rounds decide ------------------------
        for row in rows:
            period = str(row.get("issueNumber"))
            try:
                actual = int(str(row.get("number")))
            except ValueError:
                continue
            pred = self.storage.get_prediction(key, period)
            if pred is None or pred.evaluated_at is not None:
                continue
            colour_hit, size_hit, exact_hit = analyzer.evaluate(pred.number, actual)
            self.storage.settle_prediction(key, period, actual, colour_hit, size_hit, exact_hit)
            # BIG/SMALL is the desk's priority - judge the settle on the size call
            verdict = "HIT" if size_hit else "MISS"
            log.info("%s %s -> %s %s (size call %s): %s", key, period, actual,
                     size_for(actual), size_for(pred.number), verdict)

        # 2. make sure the live issue has a prediction --------------------
        #    calculated LAZILY: only for nodes a chat has actually opened;
        #    idle games just keep storing their latest rounds (no AI tokens).
        current = str(issue.get("issueNumber") or "")
        pred = self.storage.get_prediction(key, current) if current else None
        if current and pred is None and key in self.storage.active_modes():
            rounds = self.storage.rounds(key, self.cfg.analysis_window)
            made = analyzer.predict(mode, rounds, self.cfg.analysis_window, current)
            if made is None:
                # no chart data yet: defer the answer instead of guessing
                if current not in self.no_data_logged:
                    if len(self.no_data_logged) > 500:
                        self.no_data_logged.clear()
                    self.no_data_logged.add(current)
                    log.warning("%s: no chart data for %s - prediction deferred",
                                key, current)
            else:
                pred = made
                self.storage.upsert_prediction(pred)
                self.ai_queue.append((key, current))
                log.info("%s: new period %s -> pick %s (%s%%)", key, current,
                         pred.number, pred.conviction)

        # 3. publish the state ------------------------------------------- #
        rounds = self.storage.rounds(key, max(self.cfg.analysis_window, 10))
        last_round = rounds[0] if rounds else None
        settled = self.storage.settled_predictions(key, 1)
        remaining = 0.0
        end, start = _parse_ts(issue.get("endTime")), _parse_ts(issue.get("startTime"))
        now_srv = _parse_ts(issue.get("serviceTime"))
        if end and now_srv:
            remaining = max(0.0, (end - now_srv).total_seconds())

        self._last_latency_ms = int((time.monotonic() - t0) * 1000)
        self.states[key] = ModeState(
            mode=mode,
            issue=current,
            start=issue.get("startTime") or "",
            end=issue.get("endTime") or "",
            server_now=issue.get("serviceTime") or "",
            interval_m=float(issue.get("intervalM") or 0),
            remaining=remaining,
            synced_at=time.monotonic(),
            synced_wall=time.time(),
            last_round=last_round,
            prediction=pred,
            last_settled=settled[0] if settled else None,
            scorecard=self.storage.scorecard(key, 30),
            rounds_available=self.storage.round_count(key),
            error="",
            ready=True,
            latency_ms=int(self._last_latency_ms),
            last_target=size_for(int(last_round.number)) if last_round else "",
            last_target_colour=last_round.colour if last_round else "",
            streak=self._streak(key),
            streak_side=self._streak_side(key),
            ledger=self.storage.ledger(key, 6),
            session=self.storage.session_stats(key),
            uptime_s=max(0.0, time.time() - self.boot_at),
        )
        self.last_cycle[key] = time.time()

    _last_latency_ms: int = 0

    def _streak(self, key: str) -> int:
        """Consecutive same-size results at the head of history."""
        rows = self.storage.rounds(key, 40)
        if not rows:
            return 0
        try:
            side = size_for(int(rows[0].number))
        except (TypeError, ValueError):
            return 0
        n = 0
        for rnd in rows:
            try:
                if size_for(int(rnd.number)) != side:
                    break
            except (TypeError, ValueError):
                break
            n += 1
        return n

    def _streak_side(self, key: str) -> str:
        rows = self.storage.rounds(key, 1)
        if not rows:
            return ""
        try:
            return size_for(int(rows[0].number))
        except (TypeError, ValueError):
            return ""

    def _archive(self, key: str, rows: list[dict]) -> None:
        """Append-only JSONL history, never pruned (README folder 1)."""
        try:
            os.makedirs(self.cfg.history_dir, exist_ok=True)
            path = os.path.join(self.cfg.history_dir, f"{key}.jsonl")
            with open(path, "a", encoding="utf-8") as handle:
                for row in rows:
                    handle.write(json.dumps({
                        "kind": "result",
                        "mode": key,
                        "period": row.get("issueNumber"),
                        "number": row.get("number"),
                        "colour": row.get("colour"),
                        "premium": row.get("premium"),
                        "recorded_at": int(time.time()),
                    }) + "\n")
        except OSError as exc:
            log.warning("cannot archive %s: %s", key, exc)

    # ---------------- backfill ------------------------------------------ #
    def backfill(self, key: str) -> int:
        mode = MODES[key]
        target = self.cfg.backfill_target
        page = 1
        while self.storage.round_count(key) < target and page <= 14:
            rows = self.api.history(mode.type_id, page, 60)
            if not rows:
                break
            self.storage.insert_rounds(key, rows)
            page += 1
        self.storage.prune(key, self.cfg.max_records)
        count = self.storage.round_count(key)
        log.info("%s: backfilled to %s rounds", key, count)
        return count

    # ---------------- AI read ------------------------------------------- #
    def _gated_read(self, persona: str, prompt: str, fallback: str) -> AIResult:
        """One chain pass behind the publish gate.

        Returns the provider's answer untouched when it passes
        ``analyzer.is_publishable_ai``. When a provider answered with
        unusable text (refusal, no explicit CALL, gibberish) the returned
        result carries the local desk read with ``model='desk'`` - the
        caller's signal to retry once with a stricter contract. A dead chain
        (``complete_or`` local fallback, model='heuristic') is returned as-is:
        its text is the deterministic desk read, which always passes.
        """
        result = self.orch.complete_or(
            system_prompt(persona), prompt, fallback,
            max_tokens=140, temperature=0.7,
        )
        if result.model != "heuristic" and analyzer.is_publishable_ai(result.text):
            return result
        if result.model == "heuristic":
            return result                      # chain down, desk read is publishable
        reason = "gate: no CALL / refusal / unusable text"
        return AIResult(text=fallback, provider="local", model="desk",
                        latency_ms=0, fallback=True, note=reason)

    def ai_context(self, key: str) -> dict:
        state = self.state(key)
        rounds = self.storage.rounds(key, self.cfg.analysis_window)
        stats = analyzer.window_stats(rounds, self.cfg.analysis_window)
        score = state.scorecard or self.storage.scorecard(key, 30)
        last = state.last_round
        pred = state.prediction
        # BIG/SMALL pattern of the raw results - the desk's primary read
        recent = rounds[:14]
        sizes = [size_for(r.number) for r in recent]
        pattern = " ".join("B" if s == "BIG" else "S" for s in reversed(sizes)) or "-"
        run_kind, run_len = "-", 0
        for size in sizes:
            if run_len == 0:
                run_kind, run_len = size, 1
            elif size == run_kind:
                run_len += 1
            else:
                break
        return {
            "issue": state.issue or "n/a",
            "number": pred.number if pred else "-",
            "colours": pred.colours if pred else [],
            "size": pred.size if pred else "-",
            "parity": pred.parity if pred else "-",
            "conviction": pred.conviction if pred else 0,
            "last_issue": last.period if last else "n/a",
            "last_number": last.number if last else "-",
            "last_colour": last.colour if last else "-",
            "window": stats["window"],
            "hot": ", ".join(str(d) for d in stats["hot"]) or "-",
            "due": ", ".join(str(d) for d in stats["due"]) or "-",
            "colour_rate": score.get("colour_rate", 0),
            "streak": f"{score.get('streak_kind', '')}{score.get('streak_len', 0)}",
            "size_rate": score.get("size_rate", 0),
            "size_streak": f"{score.get('size_streak_kind', '')}{score.get('size_streak_len', 0)}",
            "size_pattern": pattern,
            "size_run": f"{run_len}x {run_kind}" if run_len else "-",
            "rhythm": analyzer.pattern_label(rounds, self.cfg.analysis_window),
            "decoder": analyzer.pattern_profile(rounds, self.cfg.analysis_window),
        }

    # ---------------- loops --------------------------------------------- #
    async def run(self) -> None:
        self.running = True
        tasks = [asyncio.create_task(self._mode_loop(key)) for key in MODES]
        tasks.append(asyncio.create_task(self._ai_loop()))
        try:
            await asyncio.gather(*tasks)
        except Exception:  # noqa: BLE001 - never die silently
            log.exception("engine loop stopped")
            raise

    async def _mode_loop(self, key: str) -> None:
        mode = MODES[key]
        delay = mode.poll_seconds
        # small stagger so the four games do not hit the API at the same instant
        try:
            await asyncio.sleep(list(MODES).index(key) * 0.4)
        except Exception:  # noqa: BLE001
            pass
        while self.running:
            started = time.time()
            try:
                await asyncio.to_thread(self.sync, key)
            except ApiError as exc:
                state = self.states.get(key)
                if state:
                    state.error = str(exc)[:160]
                log.warning("%s sync failed: %s", key, exc)
            except Exception:  # noqa: BLE001 - a cycle must never die
                log.exception("%s sync crashed", key)
            elapsed = time.time() - started
            # poll faster while a round is about to settle so the card flips
            # from SETTLING to the result within a couple of seconds
            left = self.state(key).seconds_left()
            sleep_for = 2.0 if 0 < left < delay else max(1.0, delay - elapsed)
            await asyncio.sleep(sleep_for)

    async def _ai_loop(self) -> None:
        """Drains queued reads through the Gemini -> Groq loop."""
        while self.running:
            if not self.ai_queue:
                await asyncio.sleep(0.8)
                continue
            key, issue = self.ai_queue.pop(0)
            # a node whose card was closed stops here - no tokens spent
            if key not in self.storage.active_modes():
                continue
            mode = MODES[key]
            state = self.state(key)
            pred = self.storage.get_prediction(key, issue)
            if pred is None or pred.evaluated_at is not None:
                continue
            persona = persona_for(mode)
            # local fallback still carries an explicit CALL so a published
            # answer always has a verdict attached (analyzer gate below).
            fallback = analyzer.local_call_text(pred)
            # free-tier quotas are small: one automatic read per node per
            # AI_MIN_INTERVAL_SECONDS, and never inside the last 10s of a round.
            # A throttled read is kept pending so the Gemini -> Groq chain
            # answers it as soon as it is allowed - the local heuristic is a
            # last resort, not a stand-in for a provider that is available.
            now = time.time()
            last = self.last_ai_at.get(key, 0.0)
            # First read for a period is prioritised (short cool-down) so the
            # analyst answers *inside* the bidding window; re-reads keep the
            # full per-node interval. Only a round about to settle is skipped.
            gap = self.cfg.ai_min_interval if pred.ai_comment else min(10, self.cfg.ai_min_interval)
            throttled = (now - last) < gap or state.seconds_left() < 3
            if throttled:
                if (key, issue) not in self.ai_queue:
                    self.ai_queue.append((key, issue))
                await asyncio.sleep(2.0)
                continue
            self.last_ai_at[key] = now
            prompt = build_commentary_prompt(persona, mode, self.ai_context(key))
            result = None
            try:
                # pass 1: the normal read, gated - an unpublishable answer
                # (refusal, no CALL, gibberish) is never shown to members.
                # model == "desk" is the marker for "text failed the gate".
                result = await asyncio.to_thread(
                    self._gated_read, persona, prompt, fallback,
                )
                if result.model == "desk":
                    # pass 2: a provider answered but the text failed the gate -
                    # ask once more with the contract spelled out.
                    strict = (
                        prompt
                        + "\n\nREQUIRED FORMAT: two sentences max, ending with"
                        " 'CALL: BIG' or 'CALL: SMALL'. No refusals, no caveats."
                    )
                    retry = await asyncio.to_thread(
                        self._gated_read, persona, strict, fallback,
                    )
                    if retry.model != "desk":
                        result = retry
            except Exception as exc:  # noqa: BLE001
                log.warning("ai read failed for %s/%s: %s", key, issue, exc)
                continue
            if result.model == "desk":
                # both passes failed the gate: publish the deterministic desk
                # read (always carries CALL) and remember the miss.
                self.gate_rejects += 1
                log.warning("%s %s: AI answer rejected by gate (%s) - desk read used",
                            key, issue, (result.note or "no CALL")[:80])
            if pred.evaluated_at is None:
                self.storage.set_ai_comment(key, issue, result.text, result.provider, result.model)
                # the analyst's explicit pattern call feeds the live pick:
                # if CALL: BIG/SMALL disagrees with the model side, re-pick the
                # strongest digit inside the analyst's side so value + answer agree
                call = analyzer.ai_size_call(result.text)
                if call and call != pred.size:
                    rounds = self.storage.rounds(key, self.cfg.analysis_window)
                    new_number = analyzer.best_digit_for_size(
                        rounds, self.cfg.analysis_window, call)
                    if new_number is not None and new_number != pred.number:
                        pred.number = new_number
                        pred.colours = colours_for(new_number)
                        pred.parity = parity_for(new_number)
                        pred.size = call
                        if not isinstance(pred.factors, dict):
                            pred.factors = {}
                        pred.factors["ai_override"] = {
                            "call": call,
                            "provider": result.provider,
                            "model": result.model,
                        }
                        self.storage.upsert_prediction(pred)
                        log.info("%s %s: analyst call %s overrides model -> value %s",
                                 key, issue, call, new_number)
                pred.ai_comment = result.text
                pred.ai_provider = result.provider
                pred.ai_model = result.model
                state = self.state(key)
                if state.prediction and state.prediction.period_label == issue:
                    state.prediction = pred
                log.info("%s %s read via %s/%s: %s", key, issue, result.provider,
                         result.model, result.text[:90])

    # ---------------- views for renderers -------------------------------- #
    def ensure_prediction(self, key: str) -> None:
        """Called when a chat opens a node's card: build the pick for the live
        period right away so the card never has to wait for the next poll."""
        state = self.state(key)
        current = state.issue
        if not current or key not in MODES:
            return
        if self.storage.get_prediction(key, current) is not None:
            return
        rounds = self.storage.rounds(key, self.cfg.analysis_window)
        made = analyzer.predict(MODES[key], rounds, self.cfg.analysis_window, current)
        if made is None:
            return
        self.storage.upsert_prediction(made)
        if (key, current) not in self.ai_queue:
            self.ai_queue.append((key, current))
        log.info("%s: node opened -> period %s pick %s (%s%%)", key, current,
                 made.number, made.conviction)

    def board(self, key: str, limit: int = 15) -> list[dict]:
        """Last rounds joined with our prediction outcome (snapshot boards)."""
        rounds = self.storage.rounds(key, limit)
        out = []
        for rnd in rounds:
            pred = self.storage.get_prediction(key, rnd.period)
            outcome = None
            if pred and pred.evaluated_at:
                # boards judge the BIG/SMALL call - the desk's priority
                outcome = bool(pred.size_hit)
            out.append({
                "period": rnd.period,
                "number": rnd.number,
                "colour": rnd.colour,
                "outcome": outcome,
            })
        return out

    def status(self) -> dict:
        games = {}
        for key, state in self.states.items():
            games[key] = {
                "node": state.mode.node,
                "label": state.mode.label,
                "issue": state.issue,
                "seconds_left": int(state.seconds_left()),
                "status": state.status(),
                "prediction": None if not state.prediction else {
                    "number": state.prediction.number,
                    "colours": state.prediction.colours,
                    "size": state.prediction.size,
                    "parity": state.prediction.parity,
                    "conviction": state.prediction.conviction,
                    "ai": persona_for(state.mode),
                    "provider": state.prediction.ai_provider,
                },
                "last_round": None if not state.last_round else {
                    "period": state.last_round.period,
                    "number": state.last_round.number,
                    "colour": state.last_round.colour,
                },
                "scorecard": state.scorecard,
                "rounds": state.rounds_available,
                "synced_ago": int(time.time() - state.synced_wall) if state.ready else None,
                "error": state.error,
            }
        return games


