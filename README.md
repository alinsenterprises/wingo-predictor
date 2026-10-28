# Wingo Prediction Analyzer — dmfirst1.com

> **Public repository note:** Live credentials, private SSH keys, runtime user/payment data, logs, snapshots, Git history, virtual environments, browser binaries, and nested backup archives are excluded from this source copy. Set credentials locally from `env.example`; never commit live values.
>
> WinGo results are random or pseudorandom. The analysis and confidence scores do not guarantee future outcomes; do not use them for real-money gambling decisions.


A Python service that runs **all four Wingo games at once** — **30 sec, 1 min,
3 min and 5 min** — each with **its own Chromium browser head** and its own
login session, scrapes their results, keeps 500+ previous rounds per game in
SQLite, analyses them, and pushes every new-round prediction and result to
**Telegram**.

Everything it produces lands in **two folders**:

| Folder | Contents |
|---|---|
| `data/` | the rolling SQLite dataset **plus** `data/history/<game>.jsonl` — every prediction, every result and every score, append-only and never pruned |
| `snapshots/` | photos: one per game **every 2 minutes** and one **for every error**, as `snapshots/<game>/<date>/<HH-MM-SS>_<reason>.jpg`, with automatic retention |

- **Language:** Python 3.10+
- **Scraping:** one headless Chromium per game (Playwright), with a plain `requests` fallback; JSON endpoint first, HTML table fallback
- **Storage:** SQLite with automatic pruning to a configurable cap + JSONL archives
- **Notifications:** `python-telegram-bot` (long polling)
- **Optional AI commentary:** Google Gemini (`GEMINI_API_KEY`)
- **Self-healing:** a stale session re-logs itself in, a broken browser head is rebuilt, a hung cycle is killed by a watchdog — and systemd/Docker `Restart=always` covers the process itself
- **Ops:** typed errors, retries with exponential backoff, rotating log file + stdout, systemd/Docker on AWS Lightsail

## 0. What runs today (v2.0)

The service has been reorganised around four things:

| Area | What it does | Where |
|---|---|---|
| **AI orchestration** | Gemini is the primary free provider, **Groq Cloud is the automatic fallback**, and the two are walked in a continuous loop: a 429/503/timeout rotates to the next *model* within the same provider, a dead provider goes into an exponential cooldown (5 s → 60 s) and the call falls through to the other one, a lapsed cooldown is retried automatically and a success resets it. Each provider rotates its own model list so a rate-limited model is skipped before the provider cools down. Every answer is tagged with the provider and model that served it; the status page, `/status` and `/health` expose the live loop state. If both providers are down the card still renders from the local heuristic. | `wingo_analyzer/orchestrator.py`, `personas.py` |
| **Onboarding + age gate** | `/start` asks for a **name** (stored), then **“are you over 18?”**. *No* permanently bans that account — and any mobile number it later shares is recorded in `banned_numbers`, so the same number cannot come back. *Yes* opens the desk. | `wingo_analyzer/bot.py` |
| **Four nodes, one auto-updating card** | The menu offers the four games. Each user keeps **one message per chat** that flips between menu and the selected node, and a background loop **edits it in place**: last period + result, new period, predicted number / colour / size / value, date, status + countdown, the analyst (ROB or ROD), streak and hit-rates. No spam of separate texts; cadence, advanced mode, alerts and pushes are per-user settings. | `wingo_analyzer/bot.py`, `render.py` |
| **AI commentary via Gemini → Groq** | Each open card gets a short analyst read (ROB or ROD) through Gemini first, with Groq as the automatic failover; both can be cooling down or disabled and the call still resolves. If the chain is exhausted the card keeps the local heuristic line. | `wingo_analyzer/orchestrator.py`, `personas.py` |
| **Snapshots that delete themselves** | Admin sends a value — a node (`1min`, `node 3`), `all`, or a period number — and gets PNG cards back: a signal card and a result board. Every snapshot (message **and** file) is removed **24 h** after it was created by the retention sweeper. | `wingo_analyzer/snapshots.py` |

The status page at `/` (JSON at `/status`, probe at `/healthz`) shows the live
nodes, the provider loop and the snapshot counters.

Admin unlock: `/admin <code>` — the code is generated at first boot and printed
in the logs (`freebuff-preview logs`), or set `ADMIN_CODE`. The bot and the
status page both expose the AI chain state (Gemini → Groq), so a missing or
cooling-down provider is visible rather than silent.

The optional Chromium transport (`heads.py`, `transports.py`) is still present
for the four browser heads, but the default HTTP path below signs the JSON
endpoint itself (`api.py`) and does not import a browser — so `check` and the
status server work without Playwright installed.

## 1. How it meets the brief

| # | Requirement | Where |
|---|-------------|-------|
| 1 | Log in securely with your account + password | `wingo_analyzer/scraper.py` → `login()` (JSON or form POST, bearer token **or** session cookie capture) |
| 2 | Scrape the latest Wingo results | `scraper.py` → `fetch_page()` — JSON history API, BeautifulSoup table fallback, `auto` mode tries both |
| 2 | Keep **> 500** previous predictions, delete older ones automatically | `storage.py` → `prune()` — `MAX_RECORDS=600` (must be > 500, enforced at boot), pruned after every poll cycle |
| 3 | Run continuously on Amazon Lightsail | `deploy/install-lightsail.sh` (systemd, `Restart=always`) and `Dockerfile` (`--restart unless-stopped`) |
| 4 | Telegram bot: new predictions start, last round results, updates | `bot.py` (commands) + `service.py` (push) + `notifier.py` (delivery) |
| 4 | Gemini key integration | `gemini_client.py` — optional, degrades gracefully |
| 5 | Python + suitable libraries | `requirements.txt` |
| 6 | Robust error handling + logging | `errors.py`, tenacity retries, `logging_setup.py` |
| 7 | Documentation | this file (and the in-app status page at `GET /`) |
| 8 | **Four games at once (30 sec / 1 min / 3 min / 5 min), one browser head each** | `modes.py` (type ids verified against the live API: 30/1/2/3) + `heads.py` (a Chromium process + cookie jar per game) |
| 9 | **Two folders — history and photos** | `storage.append_archive()` → `data/history/<game>.jsonl`; `snapshots.py` → `snapshots/<game>/…` every 2 minutes and on every error |
| 10 | **Error self-recovery + 24/7 uptime** | `service.py` supervisor keeps the engine alive; the per-cycle watchdog and the provider loop self-heal without restarting. The optional Chromium head path (`heads.py`) photographs failures and rebuilds a broken head. systemd/Docker `Restart=always` is the last line of defence |

## 2. Architecture

```
run.py / python -m wingo_analyzer
└── cli.py                     argument parsing, sub-commands
    ├── config.py              all settings come from environment variables
    ├── modes.py               the four games: 30sec=30, 1min=1, 3min=2, 5min=3
    ├── logging_setup.py       console + rotating file (logs/analyzer.log)
    ├── snapshots.py           the snapshots/ photo folder + retention
    ├── bot.py                 Telegram Application + /commands
    └── service.py             supervisor: starts + self-heals four heads
        └── worker.py          one game: backfill → poll → store → predict → notify
            ├── heads.py       BrowserHead - one Chromium per game, screenshots, restart
            │   └── transports.py  BrowserTransport (context.request) | HttpTransport
            ├── scraper.py     login + history fetch + JSON/HTML parsing (async, retries)
            ├── storage.py     SQLite: rounds/predictions per mode + JSONL archives
            ├── analyzer.py    frequency / recency / due-ness model + report
            ├── gemini_client.py  optional 2-sentence commentary
            └── notifier.py    broadcast to subscribers
```

Each mode runs the same poll cycle (`POLL_INTERVAL_SECONDS`, default 20 s) in
its own supervised asyncio task:

1. take the periodic photo when `SNAPSHOT_INTERVAL_SECONDS` (2 min) elapsed,
2. fetch page 1 of that game's history **through its own browser context**,
3. insert records that are new (unique on `(mode, period)`),
4. **prune** that game to `MAX_RECORDS` — the oldest rounds are deleted,
5. score the still-open prediction against every new result (feeds `/accuracy`),
6. archive the result/score to `data/history/<game>.jsonl`,
7. when no prediction is pending, generate one and broadcast it,
8. when a result lands, broadcast it with the running scorecard.

Every step is wrapped in `try/except`. When something does fail, the
supervisor (a) photographs the failure into `snapshots/<game>/`, (b)
re-logs-in automatically if the session expired, (c) rebuilds the whole
Chromium head after 3 consecutive failures or any browser-level error, and
(d) backs off 5 s → 60 s before trying again. A cycle that hangs longer than
`CYCLE_TIMEOUT_SECONDS` is cancelled and treated as a browser failure.

## 3. Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/playwright install chromium        # the four browser heads
.venv/bin/playwright install-deps chromium   # system libraries (needs sudo once)

cp env.example env.local      # or export the variables any way you like
#   DMFIRST_USERNAME, DMFIRST_PASSWORD, TELEGRAM_BOT_TOKEN, GEMINI_API_KEY
```

In this Freebuff workspace the variables are already injected into every command
from **Settings → Environment**, so no `export` is needed — just add the keys
there: `DMFIRST_USERNAME`, `DMFIRST_PASSWORD`, `TELEGRAM_BOT_TOKEN`,
`GEMINI_API_KEY`.

```bash
.venv/bin/python -m wingo_analyzer check                    # all four games
.venv/bin/python -m wingo_analyzer check --mode 30sec       # just one
.venv/bin/python -m wingo_analyzer backfill                 # seed all games (> 500 rounds each)
.venv/bin/python -m wingo_analyzer run                      # 4 heads + bot + poller
```

## 4. Configuration

Everything is environment-driven; `env.example` is the commented template.
The important knobs:

| Variable | Default | Purpose |
|---|---|---|
| `DMFIRST_BASE_URL` | `https://api.dmfirst9api.com` | API host (the marketing site is a shell) |
| `DMFIRST_ORIGIN` | `https://dmfirst1.com` | Sent as the `Ar-Origin` header |
| `DMFIRST_USERNAME` / `DMFIRST_PASSWORD` | – | Account used to log in |
| `DMFIRST_TOKEN` | – | Optional pre-authenticated token (skips the login call) |
| `DMFIRST_LOGIN` | `true` | `false` = never call the login endpoint (history is public) — use while credentials are wrong so restarts do not burn failed-password slots |
| `DMFIRST_LOGIN_PATH` | `/api/webapi/Login` | Login endpoint |
| `DMFIRST_LOGIN_MODE` | `json` | `json` or `form` body |
| `DMFIRST_LOGIN_TEMPLATE` | `{"username":"$username","pwd":"$password","logintype":"$login_type"}` | Login body — the site sends `phonetype`/`deviceId` too; `logintype` is `email` for addresses, `mobile` otherwise |
| `DMFIRST_SIGN` / `DMFIRST_LANGUAGE` | `true` / `0` | Request signing (md5) + site language index on every request |
| **`WINGO_MODES`** | *(all four)* | Games to run, `name=typeId`: `30sec=30,1min=1,3min=2,5min=3` — ids verified against the live API |
| **`DEFAULT_MODE`** | `1min` | Game used when a command does not name one |
| **`NOTIFY_MODES`** | `all` | Which games push to Telegram (`1min,5min` to narrow; all four ≈ 5 000 msgs/day) |
| `DMFIRST_LOGIN_TOKEN_PATH` | *(auto)* | Dot path of the token in the login response (`data.token`, …) |
| `DMFIRST_RESULTS_MODE` | `auto` | `json` \| `html` \| `auto` |
| `DMFIRST_RESULTS_PATH` | `/api/webapi/GetNoaverageEmerdList` | History endpoint (absolute URL also allowed); **public — no session required** |
| `DMFIRST_RESULTS_METHOD` | `POST` | `GET` (query params) or `POST` (JSON body) |
| `DMFIRST_RESULTS_TEMPLATE` | `{"typeId":$type_id,"pageNo":$page,"pageSize":$page_size}` | `$page`, `$page_size`, `$type_id`, `$username`, `$password` are substituted |
| `DMFIRST_RESULTS_LIST_PATH` | `data.list` | Dot path of the record array — if it does not resolve, common keys (`list`, `records`, `rows`, …) are searched automatically |
| `DMFIRST_FIELD_PERIOD` / `_NUMBER` / `_PREDICTED` / `_TIME` | `issueNumber`,`number`,*(blank)* | Field names inside one record; blank = try the common aliases (`issue`, `openNo`, `expect`, `digit`, …). `_PREDICTED` is blank because dmfirst's `premium` repeats the drawn number — it is not a pick |
| `DMFIRST_HTML_ROW_SELECTOR` / `_CELL_SELECTOR` / `_MAP` | `table tbody tr`, `td`, `period=1,number=2,time=3` | HTML fallback parsing |
| `DMFIRST_PAGE_SIZE` | `60` | Records per request |
| `DMFIRST_TLS_VERIFY` | `true` | Set `false` only for broken certificates |
| `DB_PATH` | `data/wingo.db` | SQLite file (all four games; migrated automatically on first start) |
| **`HISTORY_DIR`** | `data/history` | Append-only JSONL archive per game: every prediction, result and score |
| **`TRANSPORT`** | `auto` | `browser` = scrape through the Chromium heads, `http` = plain requests, `auto` = browser with automatic http fallback |
| **`PAGE_URL`** | *(origin)* | The page the heads sit on when taking photos |
| **`SNAPSHOT_DIR`** | `snapshots` | The photo folder |
| **`SNAPSHOT_INTERVAL_SECONDS`** | `120` | One photo per game every 2 minutes |
| **`SNAPSHOT_ON_ERROR`** | `true` | Plus a photo whenever a cycle fails (taken *before* recovery) |
| **`SNAPSHOT_FORMAT` / `_QUALITY`** | `jpeg` / `72` | Photo format — jpeg keeps 24/7 capture cheap on disk |
| **`SNAPSHOT_RETENTION_HOURS` / `_MAX_FILES`** | `24` / `720` | **Every snapshot deletes itself after 24 h** — Telegram message *and* file — enforced by the sweeper (auto-capture stays `SNAPSHOT_INTERVAL_SECONDS=120`) |
| **`CYCLE_TIMEOUT_SECONDS`** | `120` | Watchdog: a hung cycle is cancelled and that game's head rebuilt |
| `MAX_RECORDS` | `600` | Rolling dataset size — **must be > 500** |
| `BACKFILL_TARGET` | `550` | Rounds fetched on the first run |
| `ANALYSIS_WINDOW` | `100` | Rounds used by the model |
| `POLL_INTERVAL_SECONDS` | `20` | Poll cadence |
| `TELEGRAM_BOT_TOKEN` | – | Enables the bot and notifications |
| `TELEGRAM_ADMIN_CHAT_IDS` | – | Chat ids that always receive updates |
| `NOTIFY_ON_NEW_ROUND` / `NOTIFY_ON_RESULT` | `true` | Toggle the two push messages |
| `GEMINI_API_KEY` / `GEMINI_MODEL` | – / `gemini-3.8-flash` | Optional AI commentary |
| `STATUS_HOST` / `STATUS_PORT` | `0.0.0.0` / `8080` (the platform's `PORT` wins) | `GET /` health + status JSON — the same snapshot as `/status` |
| `LOG_LEVEL` / `LOG_FILE` | `info` / `logs/analyzer.log` | Logging |

## 5. The endpoints (already discovered)

`dmfirst1.com` is a JavaScript app: every call is a signed JSON POST to
`https://api.dmfirst9api.com/api/webapi/*`. The login payload, the md5 request
signature (`language` + `random` + `timestamp`, `signature`/`track`/
`xosoBettingData` excluded) and the `Ar-Origin` header were read straight out of
the site's own bundle (`/assets/js/index-*.js`), so the defaults above match
what the browser sends. The history list (`GetNoaverageEmerdList`) answers
without a session; only the login is account-bound.

If the API ever changes, one five-minute session in your browser gives you
everything this tool needs:

1. Log in to dmfirst1.com, open **DevTools → Network**, then open the Wingo
   history/results screen.
2. Find the request that returns the round list (filter by `XHR/Fetch`, look for
   `history`, `wingo`, `record`, `GetNoaverageEmerdList`, …).
3. Copy from it: **URL path**, **method**, **body/query template**, and the JSON
   path of the array (e.g. `data.list`) plus the keys of one record
   (`period`, `number`/`openNo`, `time`).
4. Do the same for the **login** request (right-click → *Copy as cURL*, then
   translate the body into `DMFIRST_LOGIN_TEMPLATE`).
5. Put them into the environment, then run `check`:

```bash
python -m wingo_analyzer check
```

`check` prints the parser mode it used, how many records it parsed and a sample
row — that is the fastest way to confirm a configuration.

## 6. Telegram bot

1. Create the bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy the token.
2. Add `TELEGRAM_BOT_TOKEN`, `GEMINI_API_KEY`, `GROQ_API_KEY` (and optionally
   `TELEGRAM_ADMIN_CHAT_IDS` / `ADMIN_CODE`) in **Settings → Environment**.
3. Message the bot `/start`.

**Flow:** `/start` → *“what's your name?”* (stored on the profile) → *“are you
over 18?”* → **Yes** opens the desk, **No** bans the account (and any number it
shares afterwards) for good.

Once inside, four inline buttons open the four nodes. Each node keeps a single
message that updates itself — press **Switch node**, **Stats** or **Settings**
and the *same message* changes; a background loop refreshes it every `2/5/10 s`
while a round is running.

| Command | What it does |
|---|---|
| `/start` | Onboarding, or the menu for returning members |
| `/stats [node]` | Window analysis: hot/cold/due digits, colour & size split, streaks |
| `/accuracy [node]` | Same view of the real hit-rate of our last calls |
| `/last [1-20] [node]` | Latest round results with our verdict per round |
| `/predict [node]` | Opens the live signal card |
| `/status` | Engine + provider-loop health |
| `/ask <question>` | Ask ROB or ROD (routed Gemini → Groq) |
| `/stop` | Mute pushes and auto-alerts |
| `/help` | Usage |

Admin commands (after `/admin <code>`): `/snap [node|all]`, `/health`,
`/broadcast <text>`, `/unban <id>` — or just **send a value** (`1min`, `all`,
`20261026100010812`) to get snapshot images back.

Cards are pushed by editing one message per chat. Optional per-user pushes
(`NOTIFY_ON_RESULT=1` + the **Push** toggle) remain available off by default.

## 7. The model

Over the last `ANALYSIS_WINDOW` rounds each digit 0–9 gets three normalised
scores — **frequency** (45 %), **recency** (35 %, exponential decay with a
half-life of half the window) and **due-ness** (20 %, absence relative to the
expected `window / 10` gap). The winner's share of the total becomes a
*conviction index* where **50 = no signal above uniform** and 95 = the strongest
signal the window can produce. Colour and size are derived from the digit
(0 → red+violet, 5 → green+violet, 1/3/7/9 green, 2/4/6/8 red; 0–4 small, 5–9 big).

Every prediction is stored and later scored against the drawn number, so
`/accuracy` reports what actually happened rather than what the model claims.
The site's own published predictions are scored the same way in `/stats` — but
dmfirst's `premium` field just repeats the drawn number on every settled round
(verified across the full 600-round dataset), so it is **not** treated as a pick
and `DMFIRST_FIELD_PREDICTED` stays blank rather than reporting a fake 100%.

## 8. CLI reference

```bash
python -m wingo_analyzer run            # bot + poller (default command)
python -m wingo_analyzer check          # validate login/endpoint config
python -m wingo_analyzer backfill --target 600
python -m wingo_analyzer stats          # analysis of the stored history
python -m wingo_analyzer predict        # next-round pick
python -m wingo_analyzer last -n 10     # latest stored rounds
python -m wingo_analyzer --db /path/to.db --log-level debug stats
```

`run` without `TELEGRAM_BOT_TOKEN` still scrapes and analyses (headless mode),
which is useful while you tune the endpoint configuration.

## 9. Data model, pruning & the two folders

```
data/                       <- folder 1: "everything we predicted and saw"
  wingo.db                  rolling SQLite dataset (all four games)
  history/30sec.jsonl       append-only: {"kind":"prediction|result|score", ...}
  history/1min.jsonl        one JSON object per line, never pruned
  history/3min.jsonl
  history/5min.jsonl
snapshots/                  <- folder 2: photos
  30sec/2026-10-26/12-00-00_interval.jpg
  30sec/2026-10-26/12-03-45_error_session.jpg
  1min/ ... 3min/ ... 5min/
```

```
rounds       id, mode, period, UNIQUE(mode, period), number, predicted,
             drawn_at, raw, recorded_at
predictions  id, mode, period_label, number, colour, size, confidence, factors,
             created_at, evaluated_at, actual_number
chats        chat_id, username, subscribed_at
meta         key/value (1min.last_sync, 1min.last_period, ...)
```

After every cycle `prune(mode)` keeps only the newest `MAX_RECORDS` rounds
**of that game** (`ORDER BY period DESC`), so the 1-minute game adds one row
and removes one row — each game always holds its own 500+ previous rounds and
the file stays small. `BACKFILL_TARGET` (550) is capped at `MAX_RECORDS`, and
`MAX_RECORDS` cannot be set below 501. Databases created before the four-mode
build are migrated automatically on first start (existing rows become the
`1min` game).

## 10. Logging & error handling

- Root logger → stdout **and** `logs/analyzer.log` (rotating, 5 MB × 5),
  format `2026-10-26 12:00:01 INFO [wingo_analyzer.scraper] …`.
- Typed exceptions in `errors.py`: `ConfigError`, `LoginError`, `ScraperError`,
  `ParseError`, `StorageError`, `NotificationError`, `GeminiError`.
- Network calls retry **4× with exponential backoff** (1 → 15 s). Retried:
  timeouts, connection failures, HTTP 5xx, expired sessions (401/403 → re-login
  on the next attempt). Not retried: bad credentials, wrong endpoint, unparsable
  payloads — those fail fast with a message that points at the config.
- **Automatic error recovery** (per game, in `service.py`):
  1. photograph the failure → `snapshots/<game>/<date>/<HH-MM-SS>_error_<kind>.jpg`,
  2. expired/dropped session → `relogin()` + reload the page,
  3. browser-level error or 3 failures in a row → rebuild the Chromium head,
  4. back off 5 s → 60 s; a cycle over `CYCLE_TIMEOUT_SECONDS` is cancelled
     by the watchdog and treated as a head failure.
  Rejected *credentials* are never hammered — the site locks the account after
  `passwordErrorMaxNum` tries — so that case keeps scraping the public history
  and logs the reason instead.
- One unreachable Telegram chat is logged and skipped; it never blocks the others.
-Gemini/Groq failures (timeout, bad key, unknown model, rate-limit) are absorbed by the orchestrator loop (Gemini → Groq → Gemini, with per-model and per-provider cooldowns) and the card still renders from the local heuristic when both providers are down.
- The whole poll cycle is guarded, so no single failure can exit the process;
  systemd/Docker `Restart=always` is the last line of defence.

## 11. Deploying to AWS Lightsail

### Path A — Lightsail VM (recommended, ~$3.50/mo)

```bash
# on the instance (Ubuntu): copy the project, then
sh ./deploy/install-lightsail.sh
```

The script creates `.venv`, installs `requirements.txt`, installs **Chromium for
the four browser heads** (`playwright install chromium` + its system
libraries), writes `/etc/systemd/system/wingo-analyzer.service`, enables and
starts it. Then:

```bash
sudo nano /opt/wingo-analyzer/.env       # credentials, bot token, Gemini key
sudo systemctl restart wingo-analyzer
journalctl -u wingo-analyzer -f          # follow the logs
```

`Restart=always` + `RestartSec=5` bring it back after a crash, an OOM kill or a
reboot, so it runs continuously without interruptions.

### Path B — container

```bash
docker build -t wingo-analyzer .
docker run -d --name wingo-analyzer --restart unless-stopped \
  -e DMFIRST_USERNAME=[set-in-env] -e DMFIRST_PASSWORD=[set-in-env] \
  -e TELEGRAM_BOT_TOKEN=[set-in-env] -e GEMINI_API_KEY=[set-in-env] \
  -v wingo-data:/app/data -v wingo-logs:/app/logs \
  -v wingo-snapshots:/app/snapshots \
  wingo-analyzer
```

Persistent volumes keep the dataset **and the photo archive** across container
updates (the image already contains Chromium for the four heads). For a
Lightsail *container service*, push the image to ECR and point the service at it
with a health check on `python -m wingo_analyzer stats`.

### Housekeeping

- Keep secrets out of the image: pass them as environment variables (or use
  `systemd`'s `EnvironmentFile`, which `install-lightsail.sh` already sets up).
- `data/wingo.db` is the only state; back it up with `sqlite3 wingo.db ".backup …"`
  while the service is running.
- Watch the log for `backfill page N` on first start — it stops at
  `BACKFILL_TARGET` or when the site stops serving older pages.

## 12. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `login failed: Wrong account or password` | Credentials rejected — or the site logs in with `country code + number` | Fix `DMFIRST_USERNAME`/`DMFIRST_PASSWORD` (3 wrong tries of 10 are already counted; the account locks at `passwordErrorMaxNum`), or paste a browser session token into `DMFIRST_TOKEN`. The analyser keeps scraping the public history and does **not** re-try the password until you restart |
| `login rejected with HTTP 401` | Wrong credentials or wrong login body | Compare with the real login request (§5), adjust `DMFIRST_LOGIN_TEMPLATE` / `DMFIRST_LOGIN_PATH` |
| `DMFIRST_LOGIN_TOKEN_PATH was not found` | Token sits elsewhere in the response | Leave it blank (auto-detect) or set the exact dot path |
| `no parsable Wingo records` | Wrong history endpoint or field names | Run `check` with `--log-level debug`, inspect the printed snippet, set `DMFIRST_RESULTS_*` |
| `browser head could not start: … libglib-2.0.so.0` | Chromium's system libraries are missing | `.venv/bin/playwright install-deps chromium` (sudo), or run with `TRANSPORT=http` (data without photos) |
| `TRANSPORT=browser` + `browser head failed to start` | Chromium cannot launch on this host | Check memory (`free -m`), or set `TRANSPORT=auto` so the service falls back to requests and keeps running |
| Photos stopped appearing | Head is down or snapshots disabled | `GET /status` → `browser: down` / `photos`; the error photo explains it |
| `no parsable records … mode=html` | The endpoint is an SPA shell | Set `DMFIRST_HTML_ROW_SELECTOR` to the real result markup, or find the JSON call (§5) |
| `list path 'data' did not resolve to an array` | Different nesting | Set `DMFIRST_RESULTS_LIST_PATH` (e.g. `data.list`) |
| Bot silent | No token, or nobody subscribed | Set `TELEGRAM_BOT_TOKEN`, send `/start`, check `/status` |
| `Gemini commentary disabled` / provider cooling down / failover lines in `/status` | Missing/invalid key, wrong model, rate-limited Gemini | Set `GEMINI_API_KEY` and/or `GROQ_API_KEY`, try another `GEMINI_MODEL`/`GROQ_MODEL`; the chain keeps working through Groq if Gemini is down |
| Dataset stuck below 500 | Site caps public history | Check the `backfill page N` log lines; raise `DMFIRST_PAGE_SIZE` |

## 13. Notes

- Predictions are statistical heuristics built from past draws; every Wingo
  round is independent and **no outcome can be guaranteed**. `/accuracy` exists
  to keep that honest.
- Only scrape with your own account, at a polite rate (`POLL_INTERVAL_SECONDS`),
  and respect the site's terms of service and applicable law in your
  jurisdiction.
# wingo-predictor


## Standalone reference script

`wingo_predictor_v2.py` is a separate reference implementation copied from [ronimahabub2021/wingo-predictor](https://github.com/ronimahabub2021/wingo-predictor). Its MIT notice is scoped in `LICENSES/wingo_predictor_v2_MIT.txt`; it is separate from the `wingo_analyzer` service.
