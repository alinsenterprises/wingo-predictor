"""Trial quota + AI publish-gate checks.

Run: python3 scripts/check_trial.py
Covers: 10 predictions / 6h window, per-period dedup, paid lift, ban/remove,
        admin helpers, and the AI answer gate (bad answers never publish).
"""
from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, ".")

DB = "data/_trial_check.db"
for suffix in ("", "-wal", "-shm"):
    try:
        os.remove(DB + suffix)
    except OSError:
        pass

from wingo_analyzer.storage import Storage  # noqa: E402
from wingo_analyzer import analyzer  # noqa: E402

PASS = 0
FAIL = 0


def check(label: str, got, want) -> None:
    global PASS, FAIL
    ok = got == want
    if ok:
        PASS += 1
    else:
        FAIL += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: got={got!r} want={want!r}")


def main() -> int:
    st = Storage(DB)
    TID = 1001

    # ---- claim --------------------------------------------------------- #
    ok, until = st.use_trial(TID, hours=6, quota=10)
    check("trial claim fires", ok, True)
    check("window is 6h", until - int(time.time()) <= 6 * 3600, True)
    check("active after claim", st.is_active(TID), True)
    state = st.trial_state(TID)
    check("quota is 10", state["quota"], 10)
    check("10 credits left", state["left"], 10)

    # ---- double claim is refused --------------------------------------- #
    ok2, _ = st.use_trial(TID, hours=6, quota=10)
    check("second claim refused", ok2, False)

    # ---- 10 distinct periods = exactly 10 credits ----------------------- #
    spent = 0
    for i in range(10):
        consumed, s = st.spend_trial_prediction(TID, f"PERIOD{i:02d}")
        if consumed:
            spent += 1
    check("10 periods charged", spent, 10)
    check("budget now zero", st.trial_state(TID)["left"], 0)
    check("trial member locked after budget", st.is_active(TID), False)

    # ---- 11th period refused ------------------------------------------- #
    consumed, s = st.spend_trial_prediction(TID, "PERIOD99")
    check("11th period refused", consumed, False)

    # ---- dedup: re-showing the same period never charges --------------- #
    st2_tid = 2002
    st.use_trial(st2_tid, hours=6, quota=10)
    first, _ = st.spend_trial_prediction(st2_tid, "SAME")
    again, s2 = st.spend_trial_prediction(st2_tid, "SAME")
    again2, s3 = st.spend_trial_prediction(st2_tid, "SAME")
    check("first show charges", first, True)
    check("re-show does not charge", again, False)
    check("re-show #3 does not charge", again2, False)
    check("only one credit spent", st.trial_state(st2_tid)["used"], 1)

    # ---- window expiry locks even with credits left --------------------- #
    TID3 = 3003
    st.use_trial(TID3, hours=6, quota=10)
    st.spend_trial_prediction(TID3, "P1")
    with st._lock:
        st.conn.execute(
            "UPDATE users SET active_until=? WHERE telegram_id=?",
            (int(time.time()) - 10, TID3))
        st.conn.commit()
    check("expired window locks the trial", st.is_active(TID3), False)

    # ---- paid key lifts the trial cap ----------------------------------- #
    TID4 = 4004
    st.use_trial(TID4, hours=6, quota=10)
    for i in range(10):
        st.spend_trial_prediction(TID4, f"X{i}")
    check("TID4 locked on trial", st.is_active(TID4), False)
    key = st.generate_key(1, telegram_id=TID4, days=30)
    ok, msg, exp = st.redeem_key(key, TID4, days=30)
    check("key redeems", ok, True)
    check("paid plan lifts trial lock", st.is_active(TID4), True)
    check("paid user never charged", st.spend_trial_prediction(TID4, "Y1")[0], False)

    # ---- admin helpers --------------------------------------------------- #
    TID5 = 5005
    st.touch_user(TID5, username="tester")
    rows, total = st.browse_users(0, 8)
    check("browse returns members", any(r["telegram_id"] == TID5 for r in rows), True)
    hits = st.admin_search_users("tester")
    check("search by username finds", bool(hits) and hits[0]["telegram_id"] == TID5, True)
    hits2 = st.admin_search_users(str(TID5))
    check("search by id finds", bool(hits2) and hits2[0]["telegram_id"] == TID5, True)

    st.ban_user(TID5, reason="test ban")
    check("ban recorded", st.is_banned(TID5), True)
    check("unban clears", st.unban(TID5), True)
    check("ban gone after unban", st.is_banned(TID5), False)

    removed = st.remove_user(TID5)
    check("member removed", removed, True)
    check("gone from db", st.get_user(TID5) is None, True)

    # remove must NOT unban
    TID6 = 6006
    st.touch_user(TID6)
    st.ban_user(TID6, reason="bad")
    st.remove_user(TID6)
    check("removal keeps the ban", st.is_banned(TID6), True)

    # grant / revoke / reset trial
    TID7 = 7007
    st.touch_user(TID7)
    st.update_user(TID7, stage="active")
    check("grant extends", st.extend(TID7, 7) > int(time.time()), True)
    check("revoke cuts access", st.revoke_access(TID7), True)
    check("revoked is inactive", st.is_active(TID7), False)
    st.use_trial(TID7, hours=6, quota=10)
    check("trial claim after revoke", st.trial_state(TID7)["trial"], True)
    check("reset_trial unlocks a fresh claim", st.reset_trial(TID7), True)
    check("trial flag cleared", st.trial_used(TID7), False)
    ok3, _ = st.use_trial(TID7, hours=6, quota=10)
    check("fresh trial claim allowed", ok3, True)

    st.close()

    # ---- AI publish gate ------------------------------------------------- #
    good = "Window favours BIG, momentum holds. CALL: BIG"
    check("valid answer publishes", analyzer.is_publishable_ai(good), True)
    check("gate keeps valid text", analyzer.validate_ai_answer(good), good)
    check("no-CALL text rejected",
          analyzer.is_publishable_ai("Well, it could go either way honestly."), False)
    check("refusal rejected",
          analyzer.is_publishable_ai("Sorry, I cannot predict this. CALL: BIG"), False)
    check("empty rejected", analyzer.is_publishable_ai(""), False)
    check("too-short rejected", analyzer.is_publishable_ai("CALL: BIG"), False)
    check("gibberish rejected",
          analyzer.is_publishable_ai("xq9#z!$%&*()[]{}<>/\\|`~^_+=" * 6), False)
    check("garbage returns None", analyzer.validate_ai_answer("????"), None)
    fallback = analyzer.local_call_text(
        type("P", (), {"size": "BIG", "conviction": 61, "number": 7})())
    check("desk fallback always publishable",
          analyzer.is_publishable_ai(fallback), True)
    check("desk fallback carries CALL", "CALL: BIG" in fallback, True)

    # ---- new quant decoders --------------------------------------------- #
    from wingo_analyzer.quant import chi_square, runs_test, transition_matrix
    from wingo_analyzer.storage import Round

    # perfectly alternating sides -> runs z strongly positive (FLIPPING)
    alt = [Round(mode="1min", period=str(i), number=(i % 2) * 5 + 1,
                 colour="green", recorded_at=0) for i in range(60)]
    r = runs_test(alt, 60)
    check("alternating run counted high", r["runs"] > 50, True)
    check("alternating flagged FLIPPING", r["label"], "FLIPPING")
    check("runs expected near 31", abs(r["expected"] - 31.0) < 1.0, True)

    # uniform-ish digits -> chi2 not significant
    uni = [Round(mode="1min", period=str(i), number=i % 10,
                 colour="red", recorded_at=0) for i in range(100)]
    c = chi_square(uni, 100)
    check("uniform digits -> UNIFORM", c["label"], "UNIFORM")
    check("chi2 df is 9", c["df"], 9)
    check("chi2 p in (0,1]", 0 < c["p_value"] <= 1, True)

    # all one digit -> chi2 significant
    skew = [Round(mode="1min", period=str(i), number=7,
                  colour="green", recorded_at=0) for i in range(60)]
    c2 = chi_square(skew, 60)
    check("degenerate digits -> SKEWED", c2["label"], "SKEWED")
    check("degenerate p tiny", c2["p_value"] < 0.01, True)

    # markov: alternating input -> after B context predicts S (low p_big)
    m = transition_matrix(alt, 60, order=2)
    check("markov has context", len(m["context"]) == 2, True)
    check("markov counts samples", m["total"] > 0, True)

    print()
    if FAIL:
        print(f"{FAIL} FAILED, {PASS} passed")
        return 1
    print(f"ALL {PASS} TRIAL/GATE/QUANT CHECKS PASSED")
    return 0


if __name__ == "__main__":
    code = main()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(DB + suffix)
        except OSError:
            pass
    raise SystemExit(code)
