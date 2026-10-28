"""Smoke test: render every new view (status, users, detail, banner, bidding).

Run: python3 scripts/check_views.py
"""
from __future__ import annotations

import sys

sys.path.insert(0, ".")

from wingo_analyzer import render, snapshots  # noqa: E402
from wingo_analyzer.engine import ModeState  # noqa: E402
from wingo_analyzer.modes import MODES, colours_for, parity_for, size_for  # noqa: E402
from wingo_analyzer.storage import Prediction, Round, User  # noqa: E402

FAIL = 0


def check(label: str, cond: bool) -> None:
    global FAIL
    print(f"[{'PASS' if cond else 'FAIL'}] {label}")
    if not cond:
        FAIL += 1


def main() -> int:
    mode = MODES["1min"]
    pred = Prediction(
        id=1, mode="1min", period_label="20261028100010472", number=7,
        colours=colours_for(7), size=size_for(7), parity=parity_for(7),
        conviction=58, factors={"window": 40, "size": {"p_big": 0.56, "confidence": 60},
                                "bar": "🟨🟨🟨🟨🟨🟨🟨⬛⬛⬛⬛⬛"},
        ai_comment="", ai_provider="", ai_model="", created_at=0,
    )
    state = ModeState(mode=mode, issue="20261028100010472", remaining=30.0,
                      synced_at=__import__("time").monotonic(), prediction=pred,
                      rounds_available=600, ready=True, latency_ms=4)

    # 1. bidding state: no answer until AI replies
    text = render.card(User(telegram_id=1, name="T", stage="active"), state)
    check("bidding hides APEX CALL", "APEX CALL" not in text)
    check("bidding shows decoding line", "AI decoding the window" in text)
    check("bidding says updates itself", "updates itself" in text)

    # 2. answered state: answer shows again
    pred.ai_comment = "Momentum holds. CALL: BIG"
    text2 = render.card(User(telegram_id=1, name="T", stage="active"), state)
    check("answered shows APEX CALL", "APEX CALL" in text2)
    check("answered hides decoding line", "AI decoding the window" not in text2)

    # 3. status view
    health = {"ready": True, "primary": "gemini", "configured": ["gemini", "groq"],
              "missing": [], "calls": 12, "fallbacks": 1, "uptime_s": 3671,
              "providers": [{"name": "gemini", "configured": True, "model": "gemini-3-flash",
                             "calls": 10, "errors": 0, "cooldown_s": 0, "disabled_s": 0},
                            {"name": "groq", "configured": True, "model": "llama",
                             "calls": 2, "errors": 0, "cooldown_s": 0, "disabled_s": 0}]}
    st_view = render.status_view({"1min": state}, health, {"live": 3, "deleted": 9},
                                 {"gate_rejects": 2, "queued": 1},
                                 {"trial": True, "used": 3, "quota": 10, "left": 7,
                                  "window_left": 5400})
    check("status has nodes", "NODE 02" in st_view)
    check("status has gate counter", "gate rejected" in st_view)
    check("status has trial line", "3/10" in st_view)
    check("status has uptime", "01:01:11" in st_view)

    # 4. users list + keyboard
    rows = [{"telegram_id": 111, "label": "Ali", "username": "ali", "stage": "active",
             "banned": False, "plan": "trial", "active": True, "active_until": 0,
             "trial": "3/10", "admin": False, "joined": 0},
            {"telegram_id": 222, "label": "Sam", "username": "", "stage": "active",
             "banned": True, "plan": "paid", "active": False, "active_until": 0,
             "trial": "", "admin": True, "joined": 0}]
    uv = render.users_view(rows, 0, 20)
    check("users view lists members", "Ali" in uv and "Sam" in uv)
    check("users view shows paging", "page 1/3" in uv)
    uk = render.users_keyboard(0, 20)
    check("users keyboard has next", any("Next" in b.text for r in uk.inline_keyboard for b in r))
    uk_last = render.users_keyboard(2, 20)
    check("last page has no next", not any("Next" in b.text for r in uk_last.inline_keyboard for b in r))

    # 5. member detail + keyboard
    detail = {"telegram_id": 111, "label": "Ali", "username": "ali", "stage": "active",
              "banned": False, "plan": "trial", "active": True, "active_until": 1790000000,
              "trial": "3/10", "joined": 1700000000, "admin": False,
              "ban_reason": "", "phone": ""}
    dv = render.user_detail_view(detail)
    check("detail shows id", "111" in dv)
    check("detail shows trial", "3/10" in dv)
    dk = render.user_detail_keyboard(detail)
    flat = [b.callback_data for r in dk.inline_keyboard for b in r]
    check("detail has ban", "u:ban:111" in flat)
    check("detail has remove", "u:del:111" in flat)
    check("detail has grant", "u:grant:111:7" in flat)
    check("detail has revoke", "u:revoke:111" in flat)
    check("detail has reset trial", "u:resettrial:111" in flat)

    banned = dict(detail, banned=True)
    dk2 = render.user_detail_keyboard(banned)
    flat2 = [b.callback_data for r in dk2.inline_keyboard for b in r]
    check("banned detail offers unban", "u:unban:111" in flat2)

    # 6. ban confirm
    bv = render.ban_confirm_view(detail)
    check("ban confirm warns", "permanent" in bv.lower())
    bk = render.ban_confirm_keyboard(111)
    check("ban confirm has yes", any("u:ban2:111" == b.callback_data
                                     for r in bk.inline_keyboard for b in r))

    # 7. welcome banner renders as PNG
    data = render_welcome_check()
    check("banner is a PNG", data[:8] == b"\x89PNG\r\n\x1a\n")
    check("banner has bytes", len(data) > 3000)

    # 8. welcome copy shows the 10/6h trial
    w = render.welcome(User(telegram_id=1, name="Ali", stage="active"), False, 0,
                       type("C", (), {"trial_hours": 6, "trial_predictions": 10,
                                      "price_inr": 499, "referral_bonus_days": 7})(),
                       ref_code="REF-TEST1")
    check("welcome says 10 predictions", "10 predictions" in w)
    check("welcome says 6h", "6h" in w)

    # 9. menu trial line
    m = render.menu(User(telegram_id=1, name="Ali", stage="active"), {"1min": state},
                    {"trial": True, "used": 3, "quota": 10, "left": 7, "window_left": 5400})
    check("menu shows trial budget", "3/10" in m)

    # 10. keyboards have status buttons
    mk = render.menu_keyboard(User(telegram_id=1))
    check("menu has Status button",
          any("view:status" == b.callback_data for r in mk.inline_keyboard for b in r))
    ck = render.card_keyboard("1min", User(telegram_id=1))
    check("card has Status button",
          any("view:status" == b.callback_data for r in ck.inline_keyboard for b in r))
    ak = render.admin_keyboard(User(telegram_id=1, is_admin=True))
    check("admin console has Members",
          any("users:0" == b.callback_data for r in ak.inline_keyboard for b in r))

    print()
    if FAIL:
        print(f"{FAIL} FAILED")
        return 1
    print("ALL VIEW CHECKS PASSED")
    return 0


def render_welcome_check() -> bytes:
    return snapshots.render_welcome("Ali", False, "7 trial predictions left · 5h30m")


if __name__ == "__main__":
    raise SystemExit(main())
