import pandas as pd
import time
import requests
import csv
import os
import sys
import math
from collections import Counter, defaultdict
from statistics import mean, stdev

# ============================================================
#  CONFIGURATION
# ============================================================
URL            = "https://draw.ar-lottery01.com/WinGo/WinGo_1M/GetHistoryIssuePage.json"
CSV_FILE       = "wingo_data.csv"
HEADERS        = {
    'accept'    : 'application/json',
    'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                  'AppleWebKit/537.36 (KHTML, like Gecko) '
                  'Chrome/120.0.0.0 Safari/537.36'
}
MIN_DATA            = 50
FETCH_INTERVAL      = 5
MAX_RETRIES         = 3
RETRY_DELAY         = 3
MIN_CONSENSUS       = 3          # algos must agree before emitting signal
BACKTEST_WINDOW     = 80         # rolling walk-forward test size
BACKTEST_TRAIN_MIN  = 30         # minimum train rows before predicting

# ── terminal colour codes ──────────────────────────────────
GREEN   = "\033[92m"
RED     = "\033[91m"
YELLOW  = "\033[93m"
CYAN    = "\033[96m"
BLUE    = "\033[94m"
MAGENTA = "\033[95m"
BOLD    = "\033[1m"
DIM     = "\033[2m"
RESET   = "\033[0m"

def c(text, *codes): return "".join(codes) + str(text) + RESET


# ============================================================
#  MATH HELPERS
# ============================================================
def sigmoid_conf(raw, k=0.08, midpoint=60):
    """Map raw [0–100] to calibrated [50–97] via sigmoid."""
    val = 50 + 47 / (1 + math.exp(-k * (raw - midpoint)))
    return round(min(val, 97), 1)


def entropy(seq):
    """Shannon entropy of a sequence (bits)."""
    if not seq:
        return 0.0
    total = len(seq)
    counts = Counter(seq)
    return -sum((v/total) * math.log2(v/total) for v in counts.values() if v)


# ============================================================
#  CSV HELPERS
# ============================================================
def save_to_csv(issue, num, size):
    existing = set()
    if os.path.isfile(CSV_FILE):
        try:
            df = pd.read_csv(CSV_FILE, dtype={'Issue': str})
            existing = set(df['Issue'].tolist())
        except Exception:
            pass
    if str(issue) in existing:
        return False
    file_exists = os.path.isfile(CSV_FILE)
    with open(CSV_FILE, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if not file_exists:
            w.writerow(['Issue', 'Number', 'Size', 'Time'])
        w.writerow([issue, num, size, time.strftime('%Y-%m-%d %H:%M:%S')])
    return True


def load_data():
    if not os.path.isfile(CSV_FILE):
        return pd.DataFrame(columns=['Issue', 'Number', 'Size', 'Time'])
    try:
        df = pd.read_csv(CSV_FILE, dtype={'Issue': str})
        df['Number'] = pd.to_numeric(df['Number'], errors='coerce')
        df.dropna(subset=['Number'], inplace=True)
        df.reset_index(drop=True, inplace=True)
        return df
    except Exception as e:
        print(c(f"⚠️  CSV error: {e}", YELLOW))
        return pd.DataFrame(columns=['Issue', 'Number', 'Size', 'Time'])


# ============================================================
#  SESSION STATE  (Bayesian prior + performance tracking)
# ============================================================
class SessionState:
    def __init__(self):
        self.prior_big   = 0.50     # Bayesian prior P(Big)
        self.total_pred  = 0
        self.correct     = 0
        self.history     = []       # list of (pred, actual)
        self.alpha       = 1        # Beta distribution α (Big successes + 1)
        self.beta_param  = 1        # Beta distribution β (Small successes + 1)

    def update(self, pred, actual):
        self.total_pred += 1
        hit = (pred == actual)
        if hit:
            self.correct += 1
        self.history.append((pred, actual))
        # Update Bayesian prior based on actual outcome
        if actual == "Big":
            self.alpha += 1
        else:
            self.beta_param += 1
        self.prior_big = self.alpha / (self.alpha + self.beta_param)

    @property
    def accuracy(self):
        return round(self.correct / self.total_pred * 100, 1) if self.total_pred else 0.0

    @property
    def recent_accuracy(self):
        recent = self.history[-20:]
        if not recent:
            return 0.0
        return round(sum(1 for p, a in recent if p == a) / len(recent) * 100, 1)

SESSION = SessionState()


# ============================================================
#  ALGORITHM 1 — Dragon / Streak Trend  (enhanced)
# ============================================================
def algo_dragon(sizes):
    if len(sizes) < 5:
        return None, 0, ""
    streak, current = 1, sizes[-1]
    for i in range(len(sizes) - 1, 0, -1):
        if sizes[i] == sizes[i - 1]:
            streak += 1
        else:
            break

    # Look at historical average streak length to calibrate break point
    avg_streak = _avg_streak_len(sizes[:-streak])

    if streak >= max(6, avg_streak + 2):
        pred   = "Big" if current == "Small" else "Small"
        conf   = sigmoid_conf(60 + streak * 2)
        reason = f"Long streak break ({streak}× {current}, avg={avg_streak:.1f})"
    elif streak >= 3:
        pred   = current
        conf   = sigmoid_conf(55 + streak * 3)
        reason = f"Dragon continue ({streak}× {current})"
    else:
        pred   = "Big" if current == "Small" else "Small"
        conf   = sigmoid_conf(52)
        reason = "Short streak — reversal lean"
    return pred, conf, reason


def _avg_streak_len(sizes):
    if len(sizes) < 4:
        return 3.0
    streaks, run = [], 1
    for i in range(1, len(sizes)):
        if sizes[i] == sizes[i - 1]:
            run += 1
        else:
            streaks.append(run)
            run = 1
    streaks.append(run)
    return mean(streaks) if streaks else 3.0


# ============================================================
#  ALGORITHM 2 — Statistical Balance (Overload Reversal)
# ============================================================
def algo_balance(df):
    if len(df) < 30:
        return None, 0, ""
    windows = [(df.tail(100), 0.40), (df.tail(50), 0.35), (df.tail(20), 0.25)]
    big_score = small_score = 0.0
    for win, weight in windows:
        b = win['Size'].value_counts().get('Big',  0)
        s = win['Size'].value_counts().get('Small', 0)
        t = b + s
        if not t:
            continue
        bp, sp = b / t * 100, s / t * 100
        if bp > 55:
            small_score += (bp - 50) * weight
        elif sp > 55:
            big_score   += (sp - 50) * weight
    if big_score > small_score and big_score > 3:
        return "Big",   sigmoid_conf(50 + big_score * 2),   f"Small overload reversal (score {big_score:.1f})"
    if small_score > big_score and small_score > 3:
        return "Small", sigmoid_conf(50 + small_score * 2), f"Big overload reversal (score {small_score:.1f})"
    return None, 0, ""


# ============================================================
#  ALGORITHM 3 — Momentum Score (Weighted Recent Trend)
# ============================================================
def algo_momentum(df):
    if len(df) < 20:
        return None, 0, ""

    def sw(sizes, w):
        b = sizes.count('Big')
        t = len(sizes)
        return ((b - (t - b)) / t) * w if t else 0

    l5  = df.tail(5)['Size'].tolist()
    l10 = df.tail(10)['Size'].tolist()
    l20 = df.tail(20)['Size'].tolist()
    m   = sw(l5, .5) + sw(l10, .3) + sw(l20, .2)
    if  m >  0.15:
        return "Big",   sigmoid_conf(50 + abs(m) * 80), f"Big momentum ({m:.2f})"
    if  m < -0.15:
        return "Small", sigmoid_conf(50 + abs(m) * 80), f"Small momentum ({m:.2f})"
    return None, 0, ""


# ============================================================
#  ALGORITHM 4 — Pattern Sequence Matching  (order-3 + order-4)
# ============================================================
def algo_pattern(df):
    if len(df) < 20:
        return None, 0, ""
    sizes = df['Size'].tolist()
    best_pred, best_conf, best_reason = None, 0, ""

    for order in (4, 3):
        if len(sizes) < order + 1:
            continue
        last_n = sizes[-order:]
        nxt = [sizes[i + order]
               for i in range(len(sizes) - order - 1)
               if sizes[i:i + order] == last_n]
        if len(nxt) < 3:
            continue
        b, t = nxt.count('Big'), len(nxt)
        if b / t >= 0.60:
            conf = sigmoid_conf(50 + (b / t - .5) * 120)
            if conf > best_conf:
                best_pred, best_conf, best_reason = "Big",   conf, f"Seq-{order} match: Big {b}/{t}"
        elif (t - b) / t >= 0.60:
            conf = sigmoid_conf(50 + ((t - b) / t - .5) * 120)
            if conf > best_conf:
                best_pred, best_conf, best_reason = "Small", conf, f"Seq-{order} match: Small {t-b}/{t}"

    return best_pred, best_conf, best_reason


# ============================================================
#  ALGORITHM 5 — Number Cluster & Hot/Cold Analysis
# ============================================================
def algo_cluster(df):
    if len(df) < 25:
        return None, 0, ""
    counter  = Counter(df.tail(20)['Number'].tolist())
    num, freq = counter.most_common(1)[0]
    if freq < 3:
        return None, 0, ""
    hot_side = "Big" if num >= 5 else "Small"
    if freq >= 5:
        return ("Big" if hot_side == "Small" else "Small",
                sigmoid_conf(52 + freq * 3), f"Hot#{int(num)} ({freq}×) cooldown")
    return hot_side, sigmoid_conf(53 + freq * 2), f"Hot#{int(num)} ({freq}×) continuing"


# ============================================================
#  ALGORITHM 6 — Standard Deviation Volatility
# ============================================================
def algo_volatility(df):
    if len(df) < 15:
        return None, 0, ""
    nums = df.tail(10)['Number'].tolist()
    avg  = mean(nums)
    try:
        sd = stdev(nums)
    except Exception:
        return None, 0, ""
    if sd < 1.5:
        return ("Big" if avg > 4.5 else "Small"), sigmoid_conf(60), f"Low volatility (SD {sd:.1f}, avg {avg:.1f})"
    if sd > 3.0:
        return ("Big" if avg < 4.5 else "Small"), sigmoid_conf(58), f"High volatility reversal (SD {sd:.1f})"
    return None, 0, ""


# ============================================================
#  ALGORITHM 7 — Parity / Odd-Even Cycle
# ============================================================
def algo_parity(df):
    if len(df) < 20:
        return None, 0, ""
    last10 = df.tail(10)['Number'].tolist()
    odds   = sum(1 for n in last10 if int(n) % 2 == 1)
    evens  = 10 - odds
    avg    = mean(last10)
    if odds >= 8:
        pred = "Big" if avg < 4.5 else "Small"
        return pred, sigmoid_conf(58), f"Odd surge ({odds}/10) correction"
    if evens >= 8:
        pred = "Small" if avg >= 4.5 else "Big"
        return pred, sigmoid_conf(58), f"Even surge ({evens}/10) correction"
    return None, 0, ""


# ============================================================
#  ALGORITHM 8 — Neighbour Zone Bias
# ============================================================
def algo_zone(df):
    if len(df) < 20:
        return None, 0, ""
    nums  = df.tail(15)['Number'].tolist()
    lo    = sum(1 for n in nums if n <= 2)
    hi    = sum(1 for n in nums if n >= 7)
    total = len(nums)
    if lo / total >= 0.45:
        return "Big",   sigmoid_conf(57), f"Low-zone overload ({lo}/15)"
    if hi / total >= 0.45:
        return "Small", sigmoid_conf(57), f"High-zone overload ({hi}/15)"
    return None, 0, ""


# ============================================================
#  ALGORITHM 9 — Markov Chain (order-2 transition matrix)  ★NEW
# ============================================================
def algo_markov(df):
    """
    Build a 2nd-order Markov transition table over the full history.
    State = (prev2, prev1), next = Big/Small.
    Emit prediction only when empirical probability ≥ 0.62.
    """
    if len(df) < 40:
        return None, 0, ""
    sizes = df['Size'].tolist()
    trans: dict = defaultdict(Counter)
    for i in range(2, len(sizes)):
        state = (sizes[i - 2], sizes[i - 1])
        trans[state][sizes[i]] += 1

    state_now = (sizes[-2], sizes[-1])
    counts    = trans.get(state_now)
    if not counts:
        return None, 0, ""
    total = sum(counts.values())
    if total < 5:
        return None, 0, ""

    best_pred = max(counts, key=counts.get)
    prob      = counts[best_pred] / total
    if prob < 0.58:
        return None, 0, ""
    conf = sigmoid_conf(50 + (prob - 0.5) * 120)
    return best_pred, conf, f"Markov-2 {state_now}→{best_pred} ({prob:.0%} of {total})"


# ============================================================
#  ALGORITHM 10 — Entropy Collapse Detector  ★NEW
# ============================================================
def algo_entropy(df):
    """
    When local entropy drops (sequence becomes repetitive/predictable),
    continuation is more reliable. Low entropy → follow current trend.
    High entropy → no signal.
    """
    if len(df) < 30:
        return None, 0, ""
    sizes  = df.tail(20)['Size'].tolist()
    H      = entropy(sizes)        # max = 1.0 for perfectly alternating
    recent = df.tail(5)['Size'].tolist()
    majority = Counter(recent).most_common(1)[0]
    pred_side, freq5 = majority

    if H < 0.75:                   # low entropy — sequence is predictable
        conf = sigmoid_conf(55 + (0.75 - H) * 60)
        return pred_side, conf, f"Low entropy ({H:.2f}) — trend stable"
    return None, 0, ""


# ============================================================
#  ALGORITHM 11 — Number Gap (Overdue) Analysis  ★NEW
# ============================================================
def algo_gap(df):
    """
    Track how many rounds since each number (0-9) last appeared.
    If Big-side numbers (5-9) are collectively overdue → predict Big,
    and vice versa.
    """
    if len(df) < 30:
        return None, 0, ""
    nums   = df['Number'].tolist()
    last   = {}
    total  = len(nums)
    for i, n in enumerate(nums):
        last[int(n)] = i
    overdue_big   = sum(total - last.get(n, 0) for n in range(5, 10))
    overdue_small = sum(total - last.get(n, 0) for n in range(0, 5))
    diff = overdue_big - overdue_small
    if diff > total * 0.5:
        return "Big",   sigmoid_conf(54 + diff / total * 10), f"Big numbers overdue (gap diff {diff})"
    if diff < -total * 0.5:
        return "Small", sigmoid_conf(54 + abs(diff) / total * 10), f"Small numbers overdue (gap diff {-diff})"
    return None, 0, ""


# ============================================================
#  ALGORITHM 12 — Session Bias Tracker  ★NEW
# ============================================================
def algo_session_bias(df):
    """
    If this session has a measurable Big/Small skew in recent 30 rounds
    AND that skew agrees with Bayesian prior → amplify.
    """
    if len(df) < 30:
        return None, 0, ""
    tail30 = df.tail(30)
    b30    = tail30['Size'].value_counts().get('Big',  0)
    s30    = tail30['Size'].value_counts().get('Small', 0)
    total  = b30 + s30
    if not total:
        return None, 0, ""
    bias   = SESSION.prior_big           # Bayesian-updated prior
    local  = b30 / total
    # Only fire when both local count and prior agree strongly
    if local >= 0.60 and bias >= 0.55:
        return "Big",   sigmoid_conf(52 + (local - 0.5) * 80), f"Session Big-biased (local {local:.0%}, prior {bias:.0%})"
    if local <= 0.40 and bias <= 0.45:
        return "Small", sigmoid_conf(52 + (0.5 - local) * 80), f"Session Small-biased (local {local:.0%}, prior {bias:.0%})"
    return None, 0, ""


# ============================================================
#  MULTI-LAYER VOTING ENGINE  (12 algorithms)
# ============================================================
ALGO_WEIGHTS = {
    "Dragon"      : 1.4,
    "Balance"     : 1.3,
    "Momentum"    : 1.2,
    "Pattern"     : 1.6,   # boosted — sequence matching is highly reliable
    "Cluster"     : 1.0,
    "Volatility"  : 0.9,
    "Parity"      : 1.0,
    "Zone"        : 0.9,
    "Markov"      : 1.7,   # highest weight — mathematically grounded
    "Entropy"     : 1.1,
    "Gap"         : 0.8,
    "SessionBias" : 1.2,
}
TOTAL_ALGOS = len(ALGO_WEIGHTS)


def multi_layer_prediction(df):
    total = len(df)
    if total < MIN_DATA:
        return {"pred": "WAIT", "conf": 0, "final_confidence": 0,
                "logic": f"Collecting… {total}/{MIN_DATA}",
                "stats": "-", "votes": {}, "total_data": total,
                "win_rate": 0.0, "needed": MIN_DATA - total}

    sizes = df['Size'].tolist()
    algos = {
        "Dragon"     : algo_dragon(sizes),
        "Balance"    : algo_balance(df),
        "Momentum"   : algo_momentum(df),
        "Pattern"    : algo_pattern(df),
        "Cluster"    : algo_cluster(df),
        "Volatility" : algo_volatility(df),
        "Parity"     : algo_parity(df),
        "Zone"       : algo_zone(df),
        "Markov"     : algo_markov(df),
        "Entropy"    : algo_entropy(df),
        "Gap"        : algo_gap(df),
        "SessionBias": algo_session_bias(df),
    }

    big_score = small_score = 0.0
    vote_log  = {}
    active    = 0

    for name, res in algos.items():
        if not res or len(res) < 2:
            continue
        pred, conf, *reason = res
        if not pred or conf == 0:
            continue
        reason_str = reason[0] if reason else ""
        active += 1
        wc = conf * ALGO_WEIGHTS[name]
        vote_log[name] = {"pred": pred, "conf": conf, "reason": reason_str}
        if pred == "Big":
            big_score += wc
        else:
            small_score += wc

    if big_score == 0 and small_score == 0:
        return {"pred": "WAIT", "conf": 50, "final_confidence": 50,
                "logic": "No strong signal", "stats": "-",
                "votes": vote_log, "total_data": total, "win_rate": 0.0}

    tot_score  = big_score + small_score
    final_pred = "Big" if big_score > small_score else "Small"
    raw_conf   = (max(big_score, small_score) / tot_score) * 100

    bv = sum(1 for v in vote_log.values() if v['pred'] == "Big")
    sv = len(vote_log) - bv
    tv = len(vote_log)

    # ── consensus gate: suppress weak signals ──────────────
    votes_for_winner = bv if final_pred == "Big" else sv
    if votes_for_winner < MIN_CONSENSUS:
        return {"pred": "WAIT", "conf": raw_conf, "final_confidence": raw_conf,
                "logic": f"Consensus too low ({votes_for_winner}/{tv} algos agree) — skipping",
                "stats": "-", "votes": vote_log, "total_data": total,
                "win_rate": 0.0, "agreement": f"{votes_for_winner}/{tv}",
                "needed": None, "bayesian_prior": round(SESSION.prior_big * 100, 1)}

    # ── agreement bonus ────────────────────────────────────
    ab = ((max(bv, sv) / tv) - 0.5) * 20 if tv else 0

    # ── Bayesian prior boost ───────────────────────────────
    prior_factor = (SESSION.prior_big - 0.5) * 6   # ±3 points max
    bayesian_adj = prior_factor if final_pred == "Big" else -prior_factor

    final_conf = sigmoid_conf(raw_conf + ab + bayesian_adj)

    dominant = max(
        (k for k, v in vote_log.items() if v['pred'] == final_pred),
        key=lambda k: vote_log[k]['conf'], default="Combined"
    )
    main_logic = vote_log.get(dominant, {}).get('reason', 'Multi-algo consensus')

    b100 = df.tail(100)['Size'].value_counts().get('Big',  0)
    s100 = df.tail(100)['Size'].value_counts().get('Small', 0)
    b20  = df.tail(20)['Size'].value_counts().get('Big',   0)
    s20  = df.tail(20)['Size'].value_counts().get('Small', 0)
    H    = round(entropy(df.tail(20)['Size'].tolist()), 3)
    stats_str = (f"100G: B{b100}/S{s100} | 20G: B{b20}/S{s20} "
                 f"| Algos: {active}/{TOTAL_ALGOS} | H={H}")

    return {
        "pred"            : final_pred,
        "conf"            : round(raw_conf, 1),
        "final_confidence": final_conf,
        "logic"           : f"[{dominant}] {main_logic}",
        "stats"           : stats_str,
        "votes"           : vote_log,
        "total_data"      : total,
        "win_rate"        : 0.0,
        "big_score"       : round(big_score, 1),
        "small_score"     : round(small_score, 1),
        "agreement"       : f"{votes_for_winner}/{tv}",
        "entropy"         : H,
        "bayesian_prior"  : round(SESSION.prior_big * 100, 1),
    }


# ============================================================
#  WIN-RATE BACK-TEST  (rolling walk-forward validation)
# ============================================================
def calculate_win_rate(df):
    """
    Rolling walk-forward: train on first N rows, predict row N+1.
    Uses only the last BACKTEST_WINDOW rows for efficiency.
    """
    if len(df) < BACKTEST_TRAIN_MIN + 5:
        return 0.0
    test_df    = df.tail(BACKTEST_WINDOW).reset_index(drop=True)
    total = correct = 0
    start = max(BACKTEST_TRAIN_MIN, 30)

    for i in range(start, len(test_df)):
        train  = test_df.iloc[:i].copy().reset_index(drop=True)
        actual = "Big" if test_df.iloc[i]['Number'] >= 5 else "Small"
        try:
            res  = multi_layer_prediction(train)
        except Exception:
            continue
        pred = res.get('pred', 'WAIT')
        if pred not in ('WAIT', 'ERROR', None):
            total   += 1
            correct += (pred == actual)

    return round(correct / total * 100, 1) if total else 0.0


# ============================================================
#  FULL SIGNAL
# ============================================================
def generate_signal(df):
    result   = multi_layer_prediction(df)
    win_rate = calculate_win_rate(df)
    result['win_rate'] = win_rate
    if result['pred'] not in ('WAIT', 'ERROR') and win_rate > 0:
        # Blend: algo confidence 60%, backtest win rate 40%
        blended = (result['final_confidence'] * 0.60) + (win_rate * 0.40)
        result['final_confidence'] = round(min(blended, 97), 1)
    return result


# ============================================================
#  NETWORK FETCH
# ============================================================
def fetch_latest():
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.get(
                URL, headers=HEADERS,
                params={'ts': int(time.time() * 1000)},
                timeout=10
            )
            if resp.status_code == 200:
                data_list = resp.json().get('data', {}).get('list', [])
                if data_list:
                    return data_list[0]
        except requests.exceptions.Timeout:
            print(c(f"  ⏱  Timeout (attempt {attempt}/{MAX_RETRIES})", YELLOW))
        except Exception as e:
            print(c(f"  ⚠  {e} (attempt {attempt}/{MAX_RETRIES})", YELLOW))
        time.sleep(RETRY_DELAY * attempt)
    return None


# ============================================================
#  DISPLAY HELPERS
# ============================================================
def sig_label(conf):
    if conf >= 85: return c("◆ SUPER STRONG", GREEN, BOLD)
    if conf >= 75: return c("▲ STRONG",        GREEN)
    if conf >= 65: return c("● MODERATE",       YELLOW)
    return             c("○ WEAK",             DIM)


def pred_colored(pred):
    if pred == "Big":   return c("BIG  ", GREEN, BOLD)
    if pred == "Small": return c("SMALL", RED,   BOLD)
    return c("WAIT ", DIM)


def progress_bar(pct, width=28):
    filled = int(width * pct / 100)
    bar    = "█" * filled + "░" * (width - filled)
    col    = GREEN if pct >= 70 else YELLOW if pct >= 50 else DIM
    return c(bar, col)


def print_banner():
    w = 66
    print()
    print(c("═" * w, CYAN))
    print(c(f"  {'WINGO ADVANCED PREDICTOR  v2.0':^{w-4}}", CYAN, BOLD))
    print(c(f"  {'12-Algorithm · Markov Chain · Bayesian · Entropy Engine':^{w-4}}", CYAN))
    print(c("═" * w, CYAN))
    lines = [
        "Dragon · Balance · Momentum · Pattern · Cluster · Volatility",
        "Parity · Zone · Markov-2 · Entropy · Gap-Analysis · SessionBias",
        f"Min data: {MIN_DATA}  |  Consensus gate: {MIN_CONSENSUS}/{TOTAL_ALGOS}  |  Fetch: {FETCH_INTERVAL}s",
    ]
    for l in lines:
        print(c(f"  {l}", DIM))
    print(c("═" * w, CYAN))
    print()


def print_signal(result, period, num, size, count):
    pred = result.get('pred', 'WAIT')
    conf = result.get('final_confidence', 0)
    win  = result.get('win_rate', 0.0)
    w    = 66

    size_col = c(f"{num} ({size})", GREEN if size == "Big" else RED)
    print(f"\n{c('─' * w, DIM)}")
    print(f"  {c('Period', DIM)}: {c(period, CYAN)}  "
          f"{c('Result', DIM)}: {size_col}  "
          f"{c('Data', DIM)}: {count}  "
          f"{c('Prior', DIM)}: {c('B=' + str(result.get('bayesian_prior', 50)) + '%', MAGENTA)}")
    print(c("─" * w, DIM))

    if pred == "WAIT":
        needed = result.get('needed', None)
        reason = result.get('logic', '')
        pct    = int(count / MIN_DATA * 100) if count < MIN_DATA else 80
        print(f"  {c('STATUS', DIM)}  : {c('⏳ ' + reason, YELLOW)}")
        print(f"  {progress_bar(pct)} {pct}%")
        if needed:
            print(f"  {c(f'Need {needed} more periods', DIM)}")
        # Still show votes if any algos fired
        votes = result.get('votes', {})
        if votes:
            print(f"\n  {c('  Algo          Vote    Conf   Reason', DIM)}")
            print(f"  {c('  ' + '─' * 56, DIM)}")
            for algo, v in votes.items():
                vp     = c(v['pred'], GREEN if v['pred'] == "Big" else RED)
                reason_txt = v.get('reason', '')[:36]
                wt     = ALGO_WEIGHTS.get(algo, 1.0)
                print(f"  {c('·', DIM)} {algo:<13} {vp:<14} {v['conf']:>4}%  "
                      f"{c('w='+str(wt), DIM)} {c(reason_txt, DIM)}")
    else:
        print(f"  {c('SIGNAL', DIM)}  : {pred_colored(pred)}  {sig_label(conf)}")
        print(f"  {c('Confidence', DIM)}: {progress_bar(conf)} {conf}%")
        print(f"  {c('Score', DIM)}   : "
              f"{c('Big', GREEN)} {result.get('big_score', 0):>6}  "
              f"{c('Small', RED)} {result.get('small_score', 0):>6}  "
              f"{c('Agree', DIM)} {result.get('agreement', '?')}/{TOTAL_ALGOS}")
        print(f"  {c('Backtest', DIM)}: {c(f'{win:.1f}%', CYAN)}  "
              f"{c('Session', DIM)}: {c(f'{SESSION.accuracy:.1f}%', BOLD)} "
              f"({SESSION.correct}/{SESSION.total_pred})  "
              f"{c('Recent20', DIM)}: {c(f'{SESSION.recent_accuracy:.1f}%', BOLD)}")
        print(f"  {c('Entropy', DIM)} : {c(result.get('entropy', '?'), BLUE)}  "
              f"{c('(lower = more predictable)', DIM)}")
        print(f"  {c('Logic', DIM)}   : {result.get('logic', '—')}")
        print(f"  {c('Stats', DIM)}   : {c(result.get('stats', '—'), DIM)}")

        votes = result.get('votes', {})
        if votes:
            print(f"\n  {c('  Algo          Vote    Conf   Reason', DIM)}")
            print(f"  {c('  ' + '─' * 56, DIM)}")
            for algo, v in votes.items():
                match  = v['pred'] == pred
                marker = c("✓", GREEN) if match else c("✗", RED)
                vp     = c(v['pred'], GREEN if v['pred'] == "Big" else RED)
                reason = v.get('reason', '')[:36]
                wt     = ALGO_WEIGHTS.get(algo, 1.0)
                print(f"  {marker} {algo:<13} {vp:<14} {v['conf']:>4}%  "
                      f"{c('w='+str(wt), DIM)} {c(reason, DIM)}")

    print(c("─" * w, DIM))


def print_result_check(predicted, actual_size, actual_num):
    if predicted in ('WAIT', 'ERROR', '', None):
        return
    if predicted == actual_size:
        print(c(f"\n  ✅  CORRECT  — Predicted {predicted:<5} | Got {actual_num} ({actual_size})", GREEN, BOLD))
    else:
        print(c(f"\n  ❌  MISSED   — Predicted {predicted:<5} | Got {actual_num} ({actual_size})", RED))


def print_performance(total_pred, correct_pred):
    if not total_pred:
        return
    acc   = round(correct_pred / total_pred * 100, 1)
    wrong = total_pred - correct_pred
    col   = GREEN if acc >= 60 else YELLOW if acc >= 50 else RED
    print(f"\n  {c('─' * 62, CYAN)}")
    print(f"  {c('PERFORMANCE SUMMARY', CYAN, BOLD)}  ({total_pred} predictions)")
    print(f"  {c('Correct', GREEN)}: {correct_pred:<4}  "
          f"{c('Wrong', RED)}: {wrong:<4}  "
          f"{c('Accuracy', BOLD)}: {c(f'{acc}%', col, BOLD)}  "
          f"{c('Recent-20', DIM)}: {c(f'{SESSION.recent_accuracy}%', BOLD)}")
    print(f"  {c('Bayesian Prior Big', DIM)}: {c(f'{SESSION.prior_big*100:.1f}%', MAGENTA)}")
    print(f"  {c('─' * 62, CYAN)}")


# ============================================================
#  MAIN LOOP
# ============================================================
def main():
    print_banner()

    last_issue         = None
    pending_prediction = None

    while True:
        latest = fetch_latest()

        if latest is None:
            print(c(f"  ⚠  Fetch failed — retrying in {FETCH_INTERVAL}s…", YELLOW))
            time.sleep(FETCH_INTERVAL)
            continue

        issue = str(latest.get('issueNumber', ''))
        num   = int(latest.get('number', 0))
        size  = "Big" if num >= 5 else "Small"

        if issue == last_issue:
            time.sleep(FETCH_INTERVAL)
            continue

        # ── evaluate previous prediction ───────────────────
        if pending_prediction:
            prev = pending_prediction.get('pred', '')
            if prev not in ('WAIT', 'ERROR', ''):
                SESSION.update(prev, size)
                print_result_check(prev, size, num)

        # ── save & reload ──────────────────────────────────
        written = save_to_csv(issue, num, size)
        if not written:
            last_issue = issue
            time.sleep(FETCH_INTERVAL)
            continue

        df     = load_data()
        result = generate_signal(df)

        print_signal(result, issue, num, size, len(df))

        if SESSION.total_pred > 0 and SESSION.total_pred % 10 == 0:
            print_performance(SESSION.total_pred, SESSION.correct)

        pending_prediction = {'issue': issue, 'pred': result.get('pred', 'WAIT')}
        last_issue         = issue
        time.sleep(FETCH_INTERVAL)


# ============================================================
#  ENTRY POINT
# ============================================================
if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(c("\n\n  Script stopped by user.", CYAN))
        if SESSION.total_pred:
            print_performance(SESSION.total_pred, SESSION.correct)
        print(c("  Good luck! 🍀\n", CYAN))
        sys.exit(0)
