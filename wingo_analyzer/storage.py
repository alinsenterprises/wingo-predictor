"""SQLite persistence: rounds, predictions, users, bans, views, snapshots.

One file (``data/wingo.db``), WAL mode, a single shared connection guarded by a
lock so the poller threads and the asyncio bot can share it safely.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass, field

log = logging.getLogger("wingo.storage")


def colors_for_legacy(number: int) -> list[str]:
    from .modes import colours_for
    return colours_for(number)


def parse_epoch(value) -> int:
    from datetime import datetime
    text = str(value or "")
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return int(datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return 0

DEFAULT_SETTINGS = {
    "auto_refresh": 5,   # seconds between card edits (2 / 5 / 10)
    "advanced": True,    # advanced telegram mode: insights, provider badges
    "alerts": True,      # settle alerts appear on the card automatically
    "push": False,       # extra push message when a round settles
    "auto_snapshots": False,  # periodic snapshot photos to admin chats (stop button)
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS rounds (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mode        TEXT NOT NULL,
    period      TEXT NOT NULL,
    number      INTEGER NOT NULL,
    colour      TEXT NOT NULL,
    premium     TEXT,
    recorded_at INTEGER NOT NULL,
    UNIQUE(mode, period)
);
CREATE INDEX IF NOT EXISTS idx_rounds_mode_period ON rounds(mode, period DESC);

CREATE TABLE IF NOT EXISTS predictions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    mode          TEXT NOT NULL,
    period_label  TEXT NOT NULL,
    number        INTEGER NOT NULL,
    colours       TEXT NOT NULL,
    size          TEXT NOT NULL,
    parity        TEXT NOT NULL,
    conviction    INTEGER NOT NULL,
    factors       TEXT,
    ai_comment    TEXT,
    ai_provider   TEXT,
    ai_model      TEXT,
    created_at    INTEGER NOT NULL,
    evaluated_at  INTEGER,
    actual_number INTEGER,
    colour_hit    INTEGER,
    size_hit      INTEGER,
    exact_hit     INTEGER,
    UNIQUE(mode, period_label)
);

CREATE TABLE IF NOT EXISTS users (
    telegram_id INTEGER PRIMARY KEY,
    name        TEXT,
    username    TEXT,
    phone       TEXT,
    stage       TEXT NOT NULL DEFAULT 'new',
    banned      INTEGER NOT NULL DEFAULT 0,
    ban_reason  TEXT,
    joined_at   INTEGER NOT NULL,
    last_seen   INTEGER NOT NULL,
    is_admin    INTEGER NOT NULL DEFAULT 0,
    settings    TEXT NOT NULL DEFAULT '{}',
    active_until INTEGER,
    trial_used   INTEGER NOT NULL DEFAULT 0,
    plan         TEXT NOT NULL DEFAULT '',
    trial_started_at INTEGER,
    trial_preds  INTEGER NOT NULL DEFAULT 0,
    trial_quota  INTEGER NOT NULL DEFAULT 0,
    ref_code     TEXT,
    referred_by  INTEGER
);

CREATE TABLE IF NOT EXISTS trial_events (
    telegram_id INTEGER NOT NULL,
    period      TEXT NOT NULL,
    created_at  INTEGER NOT NULL,
    PRIMARY KEY (telegram_id, period)
);

CREATE TABLE IF NOT EXISTS bans (
    telegram_id INTEGER PRIMARY KEY,
    name        TEXT,
    username    TEXT,
    phone       TEXT,
    reason      TEXT NOT NULL,
    banned_at   INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS banned_numbers (
    phone      TEXT PRIMARY KEY,
    telegram_id INTEGER,
    name       TEXT,
    reason     TEXT,
    banned_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS views (
    telegram_id  INTEGER NOT NULL,
    chat_id      INTEGER NOT NULL,
    message_id   INTEGER NOT NULL,
    view         TEXT NOT NULL DEFAULT 'menu',
    mode         TEXT,
    last_text    TEXT,
    last_edit_at REAL NOT NULL DEFAULT 0,
    PRIMARY KEY (telegram_id, chat_id)
);

CREATE TABLE IF NOT EXISTS snapshots (
    id          TEXT PRIMARY KEY,
    mode        TEXT,
    kind        TEXT NOT NULL,
    path        TEXT NOT NULL,
    chat_id     INTEGER,
    message_id  INTEGER,
    caption     TEXT,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    deleted_at  INTEGER
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_id INTEGER NOT NULL,
    username    TEXT,
    amount      INTEGER NOT NULL DEFAULT 499,
    upi_ref     TEXT,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  INTEGER NOT NULL,
    decided_at  INTEGER,
    decided_by  INTEGER
);

CREATE TABLE IF NOT EXISTS activation_keys (
    key         TEXT PRIMARY KEY,
    telegram_id INTEGER,
    created_by  INTEGER,
    created_at  INTEGER NOT NULL,
    expires_at  INTEGER NOT NULL,
    redeemed_at INTEGER,
    status      TEXT NOT NULL DEFAULT 'active'
);
"""


@dataclass
class Round:
    mode: str
    period: str
    number: int
    colour: str
    premium: str = ""
    recorded_at: int = 0


@dataclass
class Prediction:
    id: int
    mode: str
    period_label: str
    number: int
    colours: list[str]
    size: str
    parity: str
    conviction: int
    factors: dict
    ai_comment: str = ""
    ai_provider: str = ""
    ai_model: str = ""
    created_at: int = 0
    evaluated_at: int | None = None
    actual_number: int | None = None
    colour_hit: int | None = None
    size_hit: int | None = None
    exact_hit: int | None = None


@dataclass
class User:
    telegram_id: int
    name: str = ""
    username: str = ""
    phone: str = ""
    stage: str = "new"
    banned: bool = False
    ban_reason: str = ""
    joined_at: int = 0
    last_seen: int = 0
    is_admin: bool = False
    settings: dict = field(default_factory=lambda: dict(DEFAULT_SETTINGS))


class Storage:
    def __init__(self, path: str = "data/wingo.db"):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
            self._migrate_legacy()

    # ---------------- schema migration --------------------------------- #
    def _migrate_legacy(self) -> None:
        """Bring a database created by the first-generation build up to date.

        The original build stored rounds without colour/premium and predictions
        with a real ``confidence`` score; both are upgraded in place so the
        500+ rounds of history are kept.
        """
        tables = {row[0] for row in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}

        if "rounds" in tables:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(rounds)")}
            if "colour" not in columns:
                self.conn.executescript(
                    "ALTER TABLE rounds ADD COLUMN colour TEXT NOT NULL DEFAULT '';"
                    "ALTER TABLE rounds ADD COLUMN premium TEXT;"
                )
                rows = self.conn.execute("SELECT id, number, raw FROM rounds").fetchall()
                for row in rows:
                    colour = ""
                    try:
                        colour = (json.loads(row["raw"] or "{}") or {}).get("colour", "")
                    except (TypeError, ValueError):
                        colour = ""
                    if not colour:
                        try:
                            colour = ",".join(colors_for_legacy(int(row["number"])))
                        except (TypeError, ValueError):
                            colour = ""
                    self.conn.execute("UPDATE rounds SET colour=? WHERE id=?", (colour, row["id"]))
                log.info("migrated rounds: added colour/premium to %s rows", len(rows))

            for row in self.conn.execute(
                    "SELECT id, recorded_at FROM rounds WHERE typeof(recorded_at)!='integer'"):
                epoch = parse_epoch(row["recorded_at"])
                if epoch:
                    self.conn.execute("UPDATE rounds SET recorded_at=? WHERE id=?",
                                      (epoch, row["id"]))

        if "users" in tables:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(users)")}
            if "active_until" not in columns:
                self.conn.execute("ALTER TABLE users ADD COLUMN active_until INTEGER")
                log.info("migrated users: added active_until (SaaS activation)")
            for column, ddl in (
                ("trial_used", "ALTER TABLE users ADD COLUMN trial_used INTEGER NOT NULL DEFAULT 0"),
                ("ref_code", "ALTER TABLE users ADD COLUMN ref_code TEXT"),
                ("referred_by", "ALTER TABLE users ADD COLUMN referred_by INTEGER"),
                ("plan", "ALTER TABLE users ADD COLUMN plan TEXT NOT NULL DEFAULT ''"),
                ("trial_started_at", "ALTER TABLE users ADD COLUMN trial_started_at INTEGER"),
                ("trial_preds", "ALTER TABLE users ADD COLUMN trial_preds INTEGER NOT NULL DEFAULT 0"),
                ("trial_quota", "ALTER TABLE users ADD COLUMN trial_quota INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in columns:
                    self.conn.execute(ddl)
                    log.info("migrated users: added %s", column)
            # trial accounts created before the quota existed: mark them as
            # trials and give them the standard budget (10 / 6h) starting now,
            # so the cap applies from here on instead of an endless window.
            self.conn.execute(
                "UPDATE users SET plan='trial' WHERE plan='' AND trial_used=1"
                " AND active_until IS NOT NULL AND trial_started_at IS NULL"
            )
            self.conn.execute(
                "UPDATE users SET trial_started_at=strftime('%s','now'),"
                " trial_preds=0, trial_quota=10,"
                " active_until=MIN(active_until, strftime('%s','now') + 21600)"
                " WHERE plan='trial' AND (trial_quota IS NULL OR trial_quota=0)"
            )

        if "predictions" in tables:
            columns = {row[1] for row in self.conn.execute("PRAGMA table_info(predictions)")}
            if "conviction" not in columns:
                self.conn.execute("ALTER TABLE predictions RENAME TO predictions_legacy")
                self.conn.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS predictions (
                        id            INTEGER PRIMARY KEY AUTOINCREMENT,
                        mode          TEXT NOT NULL,
                        period_label  TEXT NOT NULL,
                        number        INTEGER NOT NULL,
                        colours       TEXT NOT NULL,
                        size          TEXT NOT NULL,
                        parity        TEXT NOT NULL,
                        conviction    INTEGER NOT NULL,
                        factors       TEXT,
                        ai_comment    TEXT,
                        ai_provider   TEXT,
                        ai_model      TEXT,
                        created_at    INTEGER NOT NULL,
                        evaluated_at  INTEGER,
                        actual_number INTEGER,
                        colour_hit    INTEGER,
                        size_hit      INTEGER,
                        exact_hit     INTEGER,
                        UNIQUE(mode, period_label)
                    );
                    """
                )
                log.info("migrated predictions: legacy table kept as predictions_legacy")
        self.conn.commit()

    def close(self) -> None:
        with self._lock:
            self.conn.close()

    # ---------------- rounds ------------------------------------------- #
    def insert_rounds(self, mode: str, rows: list[dict]) -> int:
        now = int(time.time())
        inserted = 0
        payload = []
        for row in rows:
            try:
                number = int(str(row.get("number", "")).strip())
            except ValueError:
                continue
            if not 0 <= number <= 9:
                continue
            payload.append((
                mode,
                str(row.get("issueNumber", "")).strip(),
                number,
                str(row.get("colour") or "").strip(),
                str(row.get("premium") or "").strip(),
                now,
            ))
        if not payload:
            return 0
        with self._lock:
            for item in payload:
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO rounds(mode, period, number, colour, premium, recorded_at)"
                    " VALUES(?,?,?,?,?,?)",
                    item,
                )
                inserted += cur.rowcount
            self.conn.commit()
        return inserted

    def rounds(self, mode: str, limit: int = 100) -> list[Round]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT mode, period, number, colour, premium, recorded_at FROM rounds"
                " WHERE mode=? ORDER BY period DESC LIMIT ?",
                (mode, limit),
            ).fetchall()
        return [
            Round(r["mode"], r["period"], r["number"], r["colour"], r["premium"] or "", r["recorded_at"])
            for r in rows
        ]

    def round_count(self, mode: str) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM rounds WHERE mode=?", (mode,)
            ).fetchone()
        return int(row["n"])

    def prune(self, mode: str, keep: int) -> int:
        with self._lock:
            cur = self.conn.execute(
                "DELETE FROM rounds WHERE mode=? AND id NOT IN ("
                " SELECT id FROM rounds WHERE mode=? ORDER BY period DESC LIMIT ?)",
                (mode, mode, keep),
            )
            self.conn.commit()
        return cur.rowcount

    def total_rounds(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM rounds").fetchone()
        return int(row["n"])

    # ---------------- predictions --------------------------------------- #
    def upsert_prediction(self, pred: Prediction) -> int:
        with self._lock:
            self.conn.execute(
                "INSERT INTO predictions(mode, period_label, number, colours, size, parity,"
                " conviction, factors, ai_comment, ai_provider, ai_model, created_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(mode, period_label) DO UPDATE SET number=excluded.number,"
                " colours=excluded.colours, size=excluded.size, parity=excluded.parity,"
                " conviction=excluded.conviction, factors=excluded.factors",
                (
                    pred.mode, pred.period_label, pred.number, ",".join(pred.colours),
                    pred.size, pred.parity, pred.conviction,
                    json.dumps(pred.factors), pred.ai_comment, pred.ai_provider,
                    pred.ai_model, pred.created_at or int(time.time()),
                ),
            )
            row = self.conn.execute(
                "SELECT id FROM predictions WHERE mode=? AND period_label=?",
                (pred.mode, pred.period_label),
            ).fetchone()
            self.conn.commit()
        pred.id = int(row["id"])
        return pred.id

    @staticmethod
    def _row_to_prediction(row: sqlite3.Row) -> Prediction:
        try:
            factors = json.loads(row["factors"] or "{}")
        except (TypeError, ValueError):
            factors = {}
        if not isinstance(factors, dict):  # legacy builds stored a list of notes
            factors = {"window": 0, "notes": factors}
        colours = row["colours"] if "colours" in row.keys() else row["colour"]
        return Prediction(
            id=row["id"], mode=row["mode"] if "mode" in row.keys() else "1min",
            period_label=row["period_label"], number=row["number"],
            colours=[c for c in (colours or "").split(",") if c],
            size=row["size"], parity=row["parity"], conviction=row["conviction"],
            factors=factors, ai_comment=row["ai_comment"] or "",
            ai_provider=row["ai_provider"] or "", ai_model=row["ai_model"] or "",
            created_at=row["created_at"], evaluated_at=row["evaluated_at"],
            actual_number=row["actual_number"], colour_hit=row["colour_hit"],
            size_hit=row["size_hit"], exact_hit=row["exact_hit"],
        )

    def get_prediction(self, mode: str, period: str) -> Prediction | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM predictions WHERE mode=? AND period_label=?",
                (mode, period),
            ).fetchone()
        return self._row_to_prediction(row) if row else None

    def latest_prediction(self, mode: str) -> Prediction | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM predictions WHERE mode=? ORDER BY period_label DESC LIMIT 1",
                (mode,),
            ).fetchone()
        return self._row_to_prediction(row) if row else None

    def open_prediction(self, mode: str) -> Prediction | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM predictions WHERE mode=? AND evaluated_at IS NULL"
                " ORDER BY period_label DESC LIMIT 1",
                (mode,),
            ).fetchone()
        return self._row_to_prediction(row) if row else None

    def settle_prediction(self, mode: str, period: str, actual: int,
                          colour_hit: int, size_hit: int, exact_hit: int) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE predictions SET evaluated_at=?, actual_number=?, colour_hit=?,"
                " size_hit=?, exact_hit=? WHERE mode=? AND period_label=?",
                (int(time.time()), actual, colour_hit, size_hit, exact_hit, mode, period),
            )
            self.conn.commit()

    def set_ai_comment(self, mode: str, period: str, comment: str,
                       provider: str, model: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE predictions SET ai_comment=?, ai_provider=?, ai_model=?"
                " WHERE mode=? AND period_label=?",
                (comment, provider, model, mode, period),
            )
            self.conn.commit()

    def settled_predictions(self, mode: str, limit: int = 30) -> list[Prediction]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM predictions WHERE mode=? AND evaluated_at IS NOT NULL"
                " ORDER BY period_label DESC LIMIT ?",
                (mode, limit),
            ).fetchall()
        return [self._row_to_prediction(r) for r in rows]

    def scorecard(self, mode: str, limit: int = 30) -> dict:
        """Hit rates + streaks from the last settled calls.

        The desk's priority is BIG/SMALL: the headline ``streak_*`` keys are
        size-streaks (a run of size hits or misses) and ``size_rate`` leads.
        The colour streak is kept alongside it for reference.
        """
        settled = self.settled_predictions(mode, limit)
        total = len(settled)
        colour_hits = sum(1 for p in settled if p.colour_hit)
        size_hits = sum(1 for p in settled if p.size_hit)
        exact_hits = sum(1 for p in settled if p.exact_hit)

        def _streak(attr: str) -> tuple[int, str]:
            if not settled:
                return 0, "-"
            first = bool(getattr(settled[0], attr))
            kind = "W" if first else "L"
            length = 0
            for pred in settled:
                if bool(getattr(pred, attr)) == first:
                    length += 1
                else:
                    break
            return length, kind

        size_len, size_kind = _streak("size_hit")
        colour_len, colour_kind = _streak("colour_hit")

        return {
            "total": total,
            "colour_hits": colour_hits,
            "size_hits": size_hits,
            "exact_hits": exact_hits,
            "colour_rate": round(100 * colour_hits / total) if total else 0,
            "size_rate": round(100 * size_hits / total) if total else 0,
            "exact_rate": round(100 * exact_hits / total) if total else 0,
            # headline streak = BIG/SMALL run (the desk's priority)
            "streak_len": size_len,
            "streak_kind": size_kind,
            "size_streak_len": size_len,
            "size_streak_kind": size_kind,
            "colour_streak_len": colour_len,
            "colour_streak_kind": colour_kind,
        }

    def ledger(self, mode: str, limit: int = 6) -> list[dict]:
        """Recent settled calls for the combat ledger on the live card.

        Rows arrive newest-first, in the card's own shape:
        {period, call, actual, hit, pnl}.
        """
        from .modes import size_for
        rows = self.settled_predictions(mode, limit)
        out: list[dict] = []
        for p in rows:
            out.append({
                "period": str(p.period_label)[-4:],
                "call": "B" if p.size == "BIG" else "S",
                "actual": "B" if size_for(p.actual_number or 0) == "BIG" else "S",
                "hit": bool(p.size_hit),
                "pnl": round(p.conviction * 9.5 / 100, 1) if p.size_hit else -500.0,
            })
        return out

    def session_stats(self, mode: str) -> dict:
        """Whole-history win rate, record and net P/L for one node."""
        settled = self.settled_predictions(mode, 10000)
        total = len(settled)
        wins = sum(1 for p in settled if p.size_hit)
        losses = total - wins
        pnl = 0.0
        for p in settled:
            pnl += round(p.conviction * 9.5 / 100, 1) if p.size_hit else -500.0
        return {
            "total": total,
            "wins": wins,
            "losses": losses,
            "rate": round(100 * wins / total, 1) if total else 0.0,
            "pnl": round(pnl, 2),
        }

    def prediction_count(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM predictions").fetchone()
        return int(row["n"])

    # ---------------- users --------------------------------------------- #
    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> User:
        try:
            settings = json.loads(row["settings"] or "{}")
        except (TypeError, ValueError):
            settings = {}
        merged = dict(DEFAULT_SETTINGS)
        merged.update({k: v for k, v in settings.items() if k in DEFAULT_SETTINGS})
        return User(
            telegram_id=row["telegram_id"], name=row["name"] or "",
            username=row["username"] or "", phone=row["phone"] or "",
            stage=row["stage"], banned=bool(row["banned"]),
            ban_reason=row["ban_reason"] or "", joined_at=row["joined_at"],
            last_seen=row["last_seen"], is_admin=bool(row["is_admin"]), settings=merged,
        )

    def get_user(self, telegram_id: int) -> User | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        return self._row_to_user(row) if row else None

    def touch_user(self, telegram_id: int, *, username: str = "") -> User:
        now = int(time.time())
        with self._lock:
            self.conn.execute(
                "INSERT INTO users(telegram_id, username, joined_at, last_seen)"
                " VALUES(?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET last_seen=excluded.last_seen"
                + (", username=excluded.username" if username else ""),
                (telegram_id, username, now, now) if username else (telegram_id, "", now, now),
            )
            self.conn.commit()
            row = self.conn.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        return self._row_to_user(row)

    def update_user(self, telegram_id: int, **fields) -> None:
        if not fields:
            return
        columns = []
        values = []
        for key, value in fields.items():
            if key == "settings":
                value = json.dumps(value)
            columns.append(f"{key}=?")
            values.append(value)
        values.append(telegram_id)
        with self._lock:
            self.conn.execute(f"UPDATE users SET {', '.join(columns)} WHERE telegram_id=?", values)
            self.conn.commit()

    def users(self, *, active_only: bool = False) -> list[User]:
        query = "SELECT * FROM users"
        if active_only:
            query += " WHERE banned=0 AND stage='active'"
        with self._lock:
            rows = self.conn.execute(query + " ORDER BY joined_at").fetchall()
        return [self._row_to_user(r) for r in rows]

    def user_count(self) -> dict:
        with self._lock:
            total = self.conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()["n"]
            active = self.conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE banned=0 AND stage='active'"
            ).fetchone()["n"]
            banned = self.conn.execute("SELECT COUNT(*) AS n FROM bans").fetchone()["n"]
        return {"total": int(total), "active": int(active), "banned": int(banned)}

    # ---------------- admin: user management ---------------------------- #
    def browse_users(self, page: int = 0, per_page: int = 8) -> tuple[list[dict], int]:
        """Paginated member directory for the admin console."""
        with self._lock:
            total = int(self.conn.execute(
                "SELECT COUNT(*) AS n FROM users").fetchone()["n"])
            rows = self.conn.execute(
                "SELECT telegram_id, name, username, stage, banned, plan, active_until,"
                " trial_preds, trial_quota, joined_at, is_admin"
                " FROM users ORDER BY last_seen DESC LIMIT ? OFFSET ?",
                (per_page, max(0, page) * per_page),
            ).fetchall()
        now = int(time.time())
        out = []
        for row in rows:
            out.append({
                "telegram_id": row["telegram_id"],
                "label": row["name"] or row["username"] or str(row["telegram_id"]),
                "username": row["username"] or "",
                "stage": row["stage"],
                "banned": bool(row["banned"]),
                "plan": row["plan"] or "",
                "active": bool(row["active_until"] and int(row["active_until"]) > now),
                "active_until": int(row["active_until"] or 0),
                "trial": (f"{int(row['trial_preds'] or 0)}/{int(row['trial_quota'] or 0)}"
                          if (row["plan"] == "trial") else ""),
                "admin": bool(row["is_admin"]),
                "joined": int(row["joined_at"] or 0),
            })
        return out, total

    def admin_search_users(self, term: str, limit: int = 8) -> list[dict]:
        """Find members by id, username or name (admin lookup)."""
        term = (term or "").strip().lstrip("@")
        if not term:
            return []
        like = f"%{term}%"
        with self._lock:
            if term.lstrip("-").isdigit():
                rows = self.conn.execute(
                    "SELECT telegram_id, name, username, stage, banned, plan, active_until,"
                    " trial_preds, trial_quota, joined_at FROM users WHERE telegram_id=?"
                    " OR username LIKE ? OR name LIKE ? ORDER BY last_seen DESC LIMIT ?",
                    (int(term), like, like, limit),
                ).fetchall()
            else:
                rows = self.conn.execute(
                    "SELECT telegram_id, name, username, stage, banned, plan, active_until,"
                    " trial_preds, trial_quota, joined_at FROM users WHERE username LIKE ?"
                    " OR name LIKE ? ORDER BY last_seen DESC LIMIT ?",
                    (like, like, limit),
                ).fetchall()
        now = int(time.time())
        return [{
            "telegram_id": r["telegram_id"],
            "label": r["name"] or r["username"] or str(r["telegram_id"]),
            "username": r["username"] or "",
            "stage": r["stage"], "banned": bool(r["banned"]),
            "plan": r["plan"] or "",
            "active": bool(r["active_until"] and int(r["active_until"]) > now),
            "active_until": int(r["active_until"] or 0),
            "trial": (f"{int(r['trial_preds'] or 0)}/{int(r['trial_quota'] or 0)}"
                      if (r["plan"] == "trial") else ""),
            "joined": int(r["joined_at"] or 0),
        } for r in rows]

    def remove_user(self, telegram_id: int) -> bool:
        """Hard-delete a member so they can register fresh (admin action).

        Removes the profile, their desk views and trial history. The ban row
        is kept if one exists - removing must never unban anybody.
        """
        with self._lock:
            cur = self.conn.execute("DELETE FROM users WHERE telegram_id=?", (telegram_id,))
            self.conn.execute("DELETE FROM views WHERE telegram_id=?", (telegram_id,))
            self.conn.execute("DELETE FROM trial_events WHERE telegram_id=?", (telegram_id,))
            self.conn.commit()
        return cur.rowcount > 0

    def reset_trial(self, telegram_id: int) -> bool:
        """Give an account a clean slate: they may claim a fresh trial."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE users SET trial_used=0, plan='', trial_started_at=NULL,"
                " trial_preds=0, trial_quota=0 WHERE telegram_id=?", (telegram_id,))
            self.conn.execute("DELETE FROM trial_events WHERE telegram_id=?",
                              (telegram_id,))
            self.conn.commit()
        return cur.rowcount > 0

    def revoke_access(self, telegram_id: int) -> bool:
        """Cut a subscription short (paid or trial) without banning."""
        with self._lock:
            cur = self.conn.execute(
                "UPDATE users SET active_until=? WHERE telegram_id=?",
                (int(time.time()), telegram_id))
            self.conn.commit()
        return cur.rowcount > 0

    def set_plan(self, telegram_id: int, plan: str) -> bool:
        with self._lock:
            cur = self.conn.execute(
                "UPDATE users SET plan=? WHERE telegram_id=?", (plan, telegram_id))
            self.conn.commit()
        return cur.rowcount > 0

    def top_referrers(self, limit: int = 5) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT telegram_id, name, username, ref_code, referred_by FROM users"
                " WHERE ref_code IS NOT NULL"
            ).fetchall()
            counts = dict(self.conn.execute(
                "SELECT referred_by, COUNT(*) AS n FROM users WHERE referred_by IS NOT NULL"
                " GROUP BY referred_by").fetchall())
        ranked = sorted(
            ({**{k: r[k] for k in ("telegram_id", "name", "username", "ref_code")},
              "count": int(counts.get(r["telegram_id"], 0))} for r in rows),
            key=lambda d: d["count"], reverse=True)
        return [r for r in ranked if r["count"]][:limit]

    # ---------------- bans ---------------------------------------------- #
    def ban_user(self, telegram_id: int, *, reason: str, name: str = "",
                 username: str = "", phone: str = "") -> None:
        now = int(time.time())
        with self._lock:
            self.conn.execute(
                "INSERT INTO users(telegram_id, name, username, phone, stage, banned,"
                " ban_reason, joined_at, last_seen, is_admin, settings)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(telegram_id) DO UPDATE SET banned=1, ban_reason=excluded.ban_reason",
                (telegram_id, name, username, phone, "banned", 1, reason, now, now, 0, "{}"),
            )
            self.conn.execute(
                "INSERT INTO bans(telegram_id, name, username, phone, reason, banned_at)"
                " VALUES(?,?,?,?,?,?) ON CONFLICT(telegram_id) DO UPDATE SET reason=excluded.reason,"
                " banned_at=excluded.banned_at",
                (telegram_id, name, username, phone, reason, now),
            )
            if phone:
                self.conn.execute(
                    "INSERT OR REPLACE INTO banned_numbers(phone, telegram_id, name, reason, banned_at)"
                    " VALUES(?,?,?,?,?)",
                    (normalize_phone(phone), telegram_id, name, reason, now),
                )
            self.conn.commit()

    def record_banned_number(self, phone: str, telegram_id: int | None = None,
                             name: str = "", reason: str = "underage") -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR REPLACE INTO banned_numbers(phone, telegram_id, name, reason, banned_at)"
                " VALUES(?,?,?,?,?)",
                (normalize_phone(phone), telegram_id, name, reason, int(time.time())),
            )
            if telegram_id:
                self.conn.execute(
                    "UPDATE bans SET phone=? WHERE telegram_id=?",
                    (normalize_phone(phone), telegram_id),
                )
            self.conn.commit()

    def is_banned(self, telegram_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM bans WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        return bool(row)

    def number_is_banned(self, phone: str) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM banned_numbers WHERE phone=?", (normalize_phone(phone),)
            ).fetchone()
        return bool(row)

    def ban_count(self) -> int:
        with self._lock:
            row = self.conn.execute("SELECT COUNT(*) AS n FROM banned_numbers").fetchone()
        return int(row["n"])

    def unban(self, telegram_id: int) -> bool:
        with self._lock:
            cur = self.conn.execute("DELETE FROM bans WHERE telegram_id=?", (telegram_id,))
            self.conn.execute(
                "UPDATE users SET banned=0, ban_reason=NULL, stage='active'"
                " WHERE telegram_id=?",
                (telegram_id,),
            )
            self.conn.commit()
        return cur.rowcount > 0

    # ---------------- subscriptions (SaaS) ------------------------------ #
    def create_payment(self, telegram_id: int, username: str, upi_ref: str,
                       amount: int) -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO payments(telegram_id, username, amount, upi_ref, status, created_at)"
                " VALUES(?,?,?,?, 'pending', ?)",
                (telegram_id, username or "", amount, upi_ref, int(time.time())),
            )
            self.conn.commit()
        return int(cur.lastrowid)

    def payments(self, limit: int = 30) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM payments ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()

    def pending_payments(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM payments WHERE status='pending' ORDER BY id"
            ).fetchall()

    def get_payment(self, payment_id: int) -> sqlite3.Row | None:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM payments WHERE id=?", (payment_id,)
            ).fetchone()

    def decide_payment(self, payment_id: int, status: str, decided_by: int) -> sqlite3.Row | None:
        """Approve/reject a submitted payment (status: approved | rejected)."""
        with self._lock:
            self.conn.execute(
                "UPDATE payments SET status=?, decided_at=?, decided_by=?"
                " WHERE id=? AND status='pending'",
                (status, int(time.time()), decided_by, payment_id),
            )
            self.conn.commit()
        return self.get_payment(payment_id)

    @staticmethod
    def _make_key() -> str:
        # WINGO-XXXXX-XXXXX-XXXXX, unambiguous alphabet, crypto-random
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        block = lambda: "".join(secrets.choice(alphabet) for _ in range(5))
        return f"WINGO-{block()}-{block()}-{block()}"

    def generate_key(self, created_by: int, telegram_id: int | None = None,
                     days: int = 30) -> str:
        with self._lock:
            for _ in range(12):
                key = self._make_key()
                try:
                    self.conn.execute(
                        "INSERT INTO activation_keys(key, telegram_id, created_by,"
                        " created_at, expires_at, status) VALUES(?,?,?,?,?,'active')",
                        (key, telegram_id, created_by, int(time.time()),
                         int(time.time()) + days * 86400),
                    )
                    self.conn.commit()
                    return key
                except sqlite3.IntegrityError:
                    continue
        raise RuntimeError("could not allocate an activation key")

    def keys(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM activation_keys ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def redeem_key(self, key: str, telegram_id: int, days: int = 30) -> tuple[bool, str, int]:
        """Redeem an activation key -> 30 days of desk access.
        Returns (ok, message, expires_at)."""
        key = (key or "").strip().upper()
        now = int(time.time())
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM activation_keys WHERE key=?", (key,)
            ).fetchone()
            if row is None:
                return False, "key not found - check it and try again", 0
            if row["status"] == "revoked":
                return False, "this key was revoked", 0
            if row["status"] == "redeemed":
                return False, "this key has already been used", 0
            if now > int(row["expires_at"]):
                self.conn.execute(
                    "UPDATE activation_keys SET status='expired' WHERE key=?", (key,))
                self.conn.commit()
                return False, "this key has expired", 0
            if row["telegram_id"] and int(row["telegram_id"]) != telegram_id:
                return False, "this key was issued to another account", 0
            expires = now + days * 86400
            self.conn.execute(
                "UPDATE activation_keys SET status='redeemed', telegram_id=?, redeemed_at=?"
                " WHERE key=?",
                (telegram_id, now, key),
            )
            cur = self.conn.execute(
                "UPDATE users SET active_until=?, plan='paid' WHERE telegram_id=?",
                (expires, telegram_id))
            if cur.rowcount == 0:
                # key redeemed before the profile row exists - create it
                self.conn.execute(
                    "INSERT INTO users(telegram_id, stage, joined_at, last_seen,"
                    " active_until, plan) VALUES(?,?,?,?,?,'paid')",
                    (telegram_id, "active", now, now, expires),
                )
            self.conn.commit()
        return True, "activated", expires

    def is_active(self, telegram_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT active_until, plan, trial_started_at, trial_preds, trial_quota"
                " FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        if not row or not row["active_until"] or int(row["active_until"]) <= time.time():
            return False
        # trial plans are capped on two axes: prediction budget first, then the
        # wall-clock window. Either exhausted = the trial is over, whatever
        # active_until still says.
        if row["plan"] == "trial":
            if int(row["trial_preds"] or 0) >= int(row["trial_quota"] or 0):
                return False
        return True

    def active_until(self, telegram_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT active_until FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        return int(row["active_until"]) if row and row["active_until"] else 0

    # ---------------- trial & referral ---------------------------------- #
    def ensure_ref_code(self, telegram_id: int) -> str:
        """Members get one shareable referral code, generated on first ask."""
        with self._lock:
            row = self.conn.execute(
                "SELECT ref_code FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if row and row["ref_code"]:
                return str(row["ref_code"])
            for _ in range(12):
                code = "REF-" + "".join(secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789")
                                        for _ in range(5))
                taken = self.conn.execute(
                    "SELECT 1 FROM users WHERE ref_code=?", (code,)).fetchone()
                if not taken:
                    self.conn.execute(
                        "UPDATE users SET ref_code=? WHERE telegram_id=?",
                        (code, telegram_id))
                    self.conn.commit()
                    return code
        raise RuntimeError("could not allocate a referral code")

    def set_referred(self, telegram_id: int, referrer_id: int) -> bool:
        """Record the referral once (first referrer wins)."""
        if telegram_id == referrer_id:
            return False
        with self._lock:
            cur = self.conn.execute(
                "UPDATE users SET referred_by=? WHERE telegram_id=?"
                " AND referred_by IS NULL AND telegram_id IN (SELECT telegram_id FROM users)",
                (referrer_id, telegram_id),
            )
            self.conn.commit()
        return cur.rowcount > 0

    def ref_count(self, telegram_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM users WHERE referred_by=?", (telegram_id,)
            ).fetchone()
        return int(row["n"])

    def user_id_by_ref(self, code: str) -> int | None:
        with self._lock:
            row = self.conn.execute(
                "SELECT telegram_id FROM users WHERE ref_code=?",
                ((code or "").strip().upper(),),
            ).fetchone()
        return int(row["telegram_id"]) if row else None

    def use_trial(self, telegram_id: int, hours: int, quota: int = 0) -> tuple[bool, int]:
        """One free trial per account -> (ok, expires_at). Atomic: the flag and
        the window are claimed in a single UPDATE so it can never double-fire.

        The trial is bound to a hard budget: ``quota`` predictions inside
        ``hours`` hours (default 10 per 6h). Both the window and the counter
        start at claim time.
        """
        now = int(time.time())
        until = now + hours * 3600
        with self._lock:
            # the row may not exist yet (claim straight after the deep link) -
            # create a minimal active profile first so the claim always lands
            self.conn.execute(
                "INSERT OR IGNORE INTO users(telegram_id, stage, joined_at, last_seen)"
                " VALUES(?,?,?,?)", (telegram_id, "active", now, now),
            )
            cur = self.conn.execute(
                "UPDATE users SET trial_used=1, plan='trial',"
                " trial_started_at=?, trial_preds=0, trial_quota=?"
                " WHERE telegram_id=? AND (trial_used IS NULL OR trial_used=0)",
                (now, max(0, int(quota)), telegram_id),
            )
            if cur.rowcount == 0:
                self.conn.commit()
                return False, 0
            self.conn.execute(
                "UPDATE users SET active_until=?"
                " WHERE telegram_id=? AND (active_until IS NULL OR active_until<?)",
                (until, telegram_id, until),
            )
            self.conn.commit()
        return True, until

    def trial_used(self, telegram_id: int) -> bool:
        with self._lock:
            row = self.conn.execute(
                "SELECT trial_used FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        return bool(row and row["trial_used"])

    def trial_state(self, telegram_id: int) -> dict:
        """Live trial budget: {trial, used, quota, left, expires, window_left}."""
        now = int(time.time())
        with self._lock:
            row = self.conn.execute(
                "SELECT plan, active_until, trial_preds, trial_quota"
                " FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        if not row:
            return {"trial": False, "used": 0, "quota": 0, "left": 0,
                    "expires": 0, "window_left": 0}
        used = int(row["trial_preds"] or 0)
        quota = int(row["trial_quota"] or 0)
        expires = int(row["active_until"] or 0)
        return {
            "trial": row["plan"] == "trial",
            "used": used,
            "quota": quota,
            "left": max(0, quota - used),
            "expires": expires,
            "window_left": max(0, expires - now),
        }

    def spend_trial_prediction(self, telegram_id: int, period: str) -> tuple[bool, dict]:
        """Atomically consume one trial prediction for ``period``.

        Returns (consumed, state). A period is charged at most once per account
        (deduped on the trial_events primary key), so the auto-updating card
        re-rendering 100x per round can never burn the budget. Paid plans and
        admins return (False, state) with trial=False and are never charged.
        """
        now = int(time.time())
        with self._lock:
            row = self.conn.execute(
                "SELECT plan, active_until, trial_preds, trial_quota"
                " FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if not row or row["plan"] != "trial":
                state = {"trial": False, "used": 0, "quota": 0, "left": 0,
                         "expires": 0, "window_left": 0}
                return False, state
            used, quota = int(row["trial_preds"] or 0), int(row["trial_quota"] or 0)
            state = {
                "trial": True, "used": used, "quota": quota,
                "left": max(0, quota - used),
                "expires": int(row["active_until"] or 0),
                "window_left": max(0, int(row["active_until"] or 0) - now),
            }
            if used >= quota:
                return False, state
            try:
                self.conn.execute(
                    "INSERT INTO trial_events(telegram_id, period, created_at)"
                    " VALUES(?,?,?)", (telegram_id, str(period), now),
                )
            except sqlite3.IntegrityError:
                self.conn.commit()
                return False, state          # this period was already charged
            self.conn.execute(
                "UPDATE users SET trial_preds=trial_preds+1 WHERE telegram_id=?",
                (telegram_id,),
            )
            self.conn.commit()
            used += 1
        state["used"] = used
        state["left"] = max(0, quota - used)
        return True, state

    def extend(self, telegram_id: int, days: int) -> int:
        """Add days on top of whatever the account already has (referral reward)."""
        now = int(time.time())
        with self._lock:
            row = self.conn.execute(
                "SELECT active_until FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            if row is None:
                return 0
            base = max(now, int(row["active_until"] or 0))
            new_until = base + days * 86400
            self.conn.execute(
                "UPDATE users SET active_until=? WHERE telegram_id=?",
                (new_until, telegram_id))
            self.conn.commit()
        return new_until

    def business_stats(self) -> dict:
        """Live counters for the admin console."""
        day_ago = int(time.time()) - 86400
        with self._lock:
            one = lambda sql, *a: int(self.conn.execute(sql, a).fetchone()[0])
            users = one("SELECT COUNT(*) FROM users")
            new_day = one("SELECT COUNT(*) FROM users WHERE joined_at>=?", day_ago)
            active = one("SELECT COUNT(*) FROM users WHERE active_until>?", int(time.time()))
            pending = one("SELECT COUNT(*) FROM payments WHERE status='pending'")
            approved = one("SELECT COUNT(*) FROM payments WHERE status='approved'")
            rejected = one("SELECT COUNT(*) FROM payments WHERE status='rejected'")
            revenue = int(self.conn.execute(
                "SELECT COALESCE(SUM(amount),0) FROM payments WHERE status='approved'"
            ).fetchone()[0])
            keys_active = one("SELECT COUNT(*) FROM activation_keys WHERE status='active'")
            keys_used = one("SELECT COUNT(*) FROM activation_keys WHERE status='redeemed'")
            keys_total = one("SELECT COUNT(*) FROM activation_keys")
            rounds = one("SELECT COUNT(*) FROM rounds")
            preds = one("SELECT COUNT(*) FROM predictions WHERE evaluated_at IS NOT NULL")
        return {
            "users": users,
            "new_today": new_day,
            "active_subs": active,
            "pending": pending,
            "approved": approved,
            "rejected": rejected,
            "revenue": revenue,
            "keys_active": keys_active,
            "keys_used": keys_used,
            "keys_total": keys_total,
            "rounds": rounds,
            "settled_calls": preds,
        }

    # ---------------- views (auto-updating cards) ------------------------ #
    def save_view(self, telegram_id: int, chat_id: int, message_id: int,
                  view: str, mode: str | None) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO views(telegram_id, chat_id, message_id, view, mode, last_text, last_edit_at)"
                " VALUES(?,?,?,?,?,NULL,0)"
                " ON CONFLICT(telegram_id, chat_id) DO UPDATE SET message_id=excluded.message_id,"
                " view=excluded.view, mode=excluded.mode",
                (telegram_id, chat_id, message_id, view, mode),
            )
            self.conn.commit()

    def set_view_text(self, telegram_id: int, chat_id: int, text: str, when: float) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE views SET last_text=?, last_edit_at=? WHERE telegram_id=? AND chat_id=?",
                (text, when, telegram_id, chat_id),
            )
            self.conn.commit()

    def set_view(self, telegram_id: int, chat_id: int, view: str, mode: str | None) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE views SET view=?, mode=? WHERE telegram_id=? AND chat_id=?",
                (view, mode, telegram_id, chat_id),
            )
            self.conn.commit()

    def views(self) -> list[sqlite3.Row]:
        with self._lock:
            return self.conn.execute("SELECT * FROM views").fetchall()

    def active_modes(self) -> set[str]:
        """Nodes someone has actually opened (card/stats view with a mode).

        The desk calculates lazily: a node only gets predictions and AI reads
        while at least one chat is looking at it; the other games just keep
        storing their latest results.
        """
        with self._lock:
            rows = self.conn.execute(
                "SELECT DISTINCT mode FROM views WHERE mode IS NOT NULL AND mode != ''"
            ).fetchall()
        return {row["mode"] for row in rows if row["mode"]}

    def drop_view(self, telegram_id: int, chat_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM views WHERE telegram_id=? AND chat_id=?", (telegram_id, chat_id)
            )
            self.conn.commit()

    # ---------------- snapshots ----------------------------------------- #
    def add_snapshot(self, *, mode: str, kind: str, path: str, chat_id: int | None,
                     message_id: int | None, caption: str, retention_hours: int) -> str:
        snapshot_id = uuid.uuid4().hex[:16]
        now = int(time.time())
        with self._lock:
            self.conn.execute(
                "INSERT INTO snapshots(id, mode, kind, path, chat_id, message_id, caption,"
                " created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (snapshot_id, mode, kind, path, chat_id, message_id, caption, now,
                 now + retention_hours * 3600),
            )
            self.conn.commit()
        return snapshot_id

    def expired_snapshots(self) -> list[sqlite3.Row]:
        now = int(time.time())
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM snapshots WHERE deleted_at IS NULL AND expires_at<=?",
                (now,),
            ).fetchall()

    def pending_snapshots(self) -> list[sqlite3.Row]:
        now = int(time.time())
        with self._lock:
            return self.conn.execute(
                "SELECT * FROM snapshots WHERE deleted_at IS NULL AND expires_at>?",
                (now,),
            ).fetchall()

    def mark_snapshot_deleted(self, snapshot_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE snapshots SET deleted_at=? WHERE id=?",
                (int(time.time()), snapshot_id),
            )
            self.conn.commit()

    def snapshot_stats(self) -> dict:
        now = int(time.time())
        with self._lock:
            pending = self.conn.execute(
                "SELECT COUNT(*) AS n, MIN(expires_at) AS next FROM snapshots"
                " WHERE deleted_at IS NULL AND expires_at>?",
                (now,),
            ).fetchone()
            deleted = self.conn.execute(
                "SELECT COUNT(*) AS n FROM snapshots WHERE deleted_at IS NOT NULL"
            ).fetchone()
        return {
            "live": int(pending["n"] or 0),
            "deleted": int(deleted["n"] or 0),
            "next_expiry": int(pending["next"] or 0),
        }

    # ---------------- meta ---------------------------------------------- #
    def get_meta(self, key: str, default: str = "") -> str:
        with self._lock:
            row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT INTO meta(key, value) VALUES(?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
            self.conn.commit()


def normalize_phone(phone: str) -> str:
    digits = "".join(ch for ch in (phone or "") if ch.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits
