"""All settings come from environment variables (.env / .env.local / Settings).

Nothing in this module prints or returns raw secret material: keys are only ever
handed to the clients that need them, and ``describe()`` masks them for logs and
the public status page.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

DEFAULT_GEMINI_MODELS = [
    "gemini-3-flash-preview",   # fastest free endpoint in practice
    "gemini-3.6-flash",
    "gemini-3.1-flash-lite",
    "gemini-3.8-flash",         # free-tier quota is tiny, kept late in the chain
    "gemini-3.5-flash-lite",    # reliable but slow, last resort
]
DEFAULT_GROQ_MODELS = [
    "openai/gpt-oss-120b",
    "qwen/qwen3.8-27b",
    "openai/gpt-oss-20b",
    "allam-2-7b",
]
# OpenRouter free-tier models - Nemotron 3 Ultra leads (frontier reasoning at $0),
# then the smaller/faster NVIDIA free endpoints as belt-and-braces.
DEFAULT_OPENROUTER_MODELS = [
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "nvidia/nemotron-3-super-120b-a12b:free",
    "nvidia/nemotron-3.5-lightning:free",
]


def _s(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _i(name: str, default: int) -> int:
    raw = _s(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _f(name: str, default: float) -> float:
    raw = _s(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _b(name: str, default: bool) -> bool:
    raw = _s(name).lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _csv(name: str) -> list[str]:
    return [part.strip() for part in _s(name).split(",") if part.strip()]


@dataclass
class Config:
    # ---- Telegram -------------------------------------------------------
    telegram_token: str = ""
    admin_chat_ids: list[int] = field(default_factory=list)
    admin_code: str = ""           # empty -> generated once and stored in meta

    # ---- AI providers (Gemini primary -> OpenRouter/Nemotron -> Groq) ----
    gemini_key: str = ""
    gemini_models: list[str] = field(default_factory=lambda: list(DEFAULT_GEMINI_MODELS))
    openrouter_key: str = ""
    openrouter_models: list[str] = field(default_factory=lambda: list(DEFAULT_OPENROUTER_MODELS))
    groq_key: str = ""
    groq_models: list[str] = field(default_factory=lambda: list(DEFAULT_GROQ_MODELS))
    ai_temperature: float = 0.85
    ai_timeout: int = 25
    ai_min_interval: int = 30    # seconds between automatic analyst reads per node

    # ---- Data source ----------------------------------------------------
    base_url: str = "https://api.dmfirst9api.com/api/webapi/"
    origin: str = "https://dmfirst1.com"
    username: str = ""
    password: str = ""
    do_login: bool = False
    results_page_size: int = 60

    # ---- Storage / retention -------------------------------------------
    db_path: str = "data/wingo.db"
    history_dir: str = "data/history"
    snapshot_dir: str = "snapshots"
    snapshot_interval: int = 120        # seconds between automatic photos
    snapshot_retention_hours: int = 24  # every snapshot deletes itself after this
    snapshot_on_error: bool = True

    # ---- Engine ---------------------------------------------------------
    poll_fallback: float = 15.0
    max_records: int = 600              # must stay > 500
    backfill_target: int = 550
    analysis_window: int = 100
    notify_on_new_round: bool = False   # cards auto-update instead of pushing
    notify_on_result: bool = False

    # ---- Bot behaviour --------------------------------------------------
    auto_refresh_seconds: int = 5       # default card refresh cadence
    headless: bool = False              # engine/status only - never poll Telegram

    # ---- SaaS / activation --------------------------------------------
    upi_id: str = ""                    # where the 499 transfer goes
    price_inr: int = 499                # monthly subscription
    activation_days: int = 30           # one activation key = one month
    trial_hours: int = 6                # one free trial per account
    trial_predictions: int = 10         # hard cap of predictions inside that trial
    referral_bonus_days: int = 7        # per friend who activates

    # ---- Status server --------------------------------------------------
    status_host: str = "0.0.0.0"
    status_port: int = 8080

    # ---- Logging --------------------------------------------------------
    log_level: str = "info"
    log_file: str = "logs/analyzer.log"

    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls) -> "Config":
        port = _i("STATUS_PORT", 8080)
        if _s("PORT"):
            port = _i("PORT", port)

        gemini_models = _csv("GEMINI_MODEL") + [m for m in DEFAULT_GEMINI_MODELS]
        if _s("GEMINI_MODEL"):
            first = _s("GEMINI_MODEL")
            gemini_models = [first] + [m for m in DEFAULT_GEMINI_MODELS if m != first]

        groq_models = list(DEFAULT_GROQ_MODELS)
        if _s("GROQ_MODEL"):
            first = _s("GROQ_MODEL")
            groq_models = [first] + [m for m in DEFAULT_GROQ_MODELS if m != first]

        openrouter_models = list(DEFAULT_OPENROUTER_MODELS)
        if _s("OPENROUTER_MODEL"):
            first = _s("OPENROUTER_MODEL")
            openrouter_models = [first] + [m for m in DEFAULT_OPENROUTER_MODELS if m != first]

        admin_ids: list[int] = []
        for raw in _csv("TELEGRAM_ADMIN_CHAT_IDS"):
            if raw.lstrip("-").isdigit():
                admin_ids.append(int(raw))

        cfg = cls(
            telegram_token=_s("TELEGRAM_BOT_TOKEN"),
            admin_chat_ids=admin_ids,
            admin_code=_s("ADMIN_CODE"),
            gemini_key=_s("GEMINI_API_KEY"),
            gemini_models=gemini_models,
            openrouter_key=_s("OPENROUTER_API_KEY"),
            openrouter_models=openrouter_models,
            groq_key=_s("GROQ_API_KEY"),
            groq_models=groq_models,
            ai_temperature=_f("AI_TEMPERATURE", 0.85),
            ai_timeout=_i("AI_TIMEOUT", 25),
            ai_min_interval=max(_i("AI_MIN_INTERVAL_SECONDS", 30), 10),
            base_url=_s("DMFIRST_BASE_URL", cls.base_url).rstrip("/") + "/",
            origin=_s("DMFIRST_ORIGIN", cls.origin),
            username=_s("DMFIRST_USERNAME"),
            password=_s("DMFIRST_PASSWORD"),
            do_login=_b("DMFIRST_LOGIN", False),
            results_page_size=_i("DMFIRST_PAGE_SIZE", 60),
            db_path=_s("DB_PATH", "data/wingo.db"),
            history_dir=_s("HISTORY_DIR", "data/history"),
            snapshot_dir=_s("SNAPSHOT_DIR", "snapshots"),
            snapshot_interval=_i("SNAPSHOT_INTERVAL_SECONDS", 120),
            snapshot_retention_hours=_i("SNAPSHOT_RETENTION_HOURS", 24),
            snapshot_on_error=_b("SNAPSHOT_ON_ERROR", True),
            poll_fallback=_f("POLL_INTERVAL_SECONDS", 15.0),
            max_records=max(_i("MAX_RECORDS", 600), 501),
            backfill_target=min(_i("BACKFILL_TARGET", 550), _i("MAX_RECORDS", 600)),
            analysis_window=_i("ANALYSIS_WINDOW", 100),
            notify_on_new_round=_b("NOTIFY_ON_NEW_ROUND", False),
            notify_on_result=_b("NOTIFY_ON_RESULT", False),
            auto_refresh_seconds=max(_i("AUTO_REFRESH_SECONDS", 5), 2),
            headless=_b("WINGO_HEADLESS", False),  # explicit opt-in only; never inferred
            upi_id=_s("UPI_ID"),
            price_inr=max(_i("PRICE_INR", 499), 0),
            activation_days=max(_i("ACTIVATION_DAYS", 30), 1),
            trial_hours=max(_i("TRIAL_HOURS", 6), 1),
            trial_predictions=max(_i("TRIAL_PREDICTIONS", 10), 1),
            referral_bonus_days=max(_i("REFERRAL_BONUS_DAYS", 7), 0),
            status_host=_s("STATUS_HOST", "0.0.0.0") or "0.0.0.0",
            status_port=port,
            log_level=_s("LOG_LEVEL", "info").lower(),
            log_file=_s("LOG_FILE", "logs/analyzer.log"),
        )
        return cfg

    # ------------------------------------------------------------------ #
    @property
    def ai_providers(self) -> list[str]:
        ready = []
        if self.gemini_key:
            ready.append("gemini")
        if self.openrouter_key:
            ready.append("openrouter")
        if self.groq_key:
            ready.append("groq")
        return ready

    @property
    def bot_ready(self) -> bool:
        # A real Telegram token is all the bot gate looks at. Ghost Freebuff binds
        # (SERF_WINGO_BIND / WINGO_BIND coming from the host shell) are ignored on
        # purpose so an injected value can never turn the bot off and kill auto-update.
        # Headless mode is opt-in only via WINGO_HEADLESS=1, never inferred from env.
        if not self.telegram_token:
            return False
        bind = ""
        for name in ("WINGO_BIND", "SERF_WINGO_BIND", "BIND"):
            v = os.environ.get(name)
            if v:
                bind = v
                break
        return not self.headless and (not bind or bind != "headless")

    def describe(self) -> dict:
        """Status-page safe view: key *names* and shapes only, never values."""
        return {
            "telegram_token": "set" if self.telegram_token else "missing",
            "headless": self.headless,
            "gemini_key": "set" if self.gemini_key else "missing",
            "openrouter_key": "set" if self.openrouter_key else "missing",
            "groq_key": "set" if self.groq_key else "missing",
            "admin_chat_ids": len(self.admin_chat_ids),
            "admin_code": "configured" if self.admin_code else "generated-at-boot",
            "gemini_models": self.gemini_models,
            "openrouter_models": self.openrouter_models,
            "groq_models": self.groq_models,
            "db_path": self.db_path,
            "snapshot_retention_hours": self.snapshot_retention_hours,
            "status": f"{self.status_host}:{self.status_port}",
        }
