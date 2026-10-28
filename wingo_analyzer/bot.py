"""The Telegram bot.

Flow
----
/start -> "what's your name?" (stored) -> "are you over 18?" -> No = permanent
ban for that account (and any number it later shares) / Yes = the desk opens.

The desk is four nodes. Each user keeps **one** message per chat that flips
between the menu and the selected node's card, and a background loop *edits that
message in place* - last period + result, new period, predicted number/colour/
size/value, date, status, analyst (ROB/ROD), streak - so nothing is ever spammed
as separate texts.
"""
from __future__ import annotations

import asyncio
import io
import logging
import re
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import RetryAfter
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          ContextTypes, MessageHandler, filters)

from . import analyzer, render
from .config import Config
from .engine import Engine
from .modes import MODES, ORDER, parse_mode_arg, size_for
from .orchestrator import Orchestrator
from .personas import persona_for, system_prompt
from .snapshots import SnapshotManager, render_welcome
from .storage import DEFAULT_SETTINGS, User, normalize_phone, Storage

log = logging.getLogger("wingo.bot")

PHONE_RE = re.compile(r"^\+?[\d][\d\s\-()]{6,16}$")
VIEWS = ("menu", "card", "stats", "settings", "profile", "help", "admin", "health", "ask")


class BotCore:
    def __init__(self, cfg: Config, storage: Storage, engine: Engine,
                 orchestrator: Orchestrator, snapshots: SnapshotManager):
        self.cfg = cfg
        self.storage = storage
        self.engine = engine
        self.orch = orchestrator
        self.snapshots = snapshots
        self.app: Application | None = None
        self.awaiting: dict[int, str] = {}       # tid -> 'ask' | 'broadcast'
        self.last_node: dict[int, str] = {}      # tid -> last opened node
        self.trial_notified: set[int] = set()    # one trial-exhausted notice each
        self.username = ""
        self.running = False
        self.seen_settles: dict[str, str] = {}   # mode -> last settled period

    # ------------------------------------------------------------------ #
    def build(self) -> Application:
        self.app = Application.builder().token(self.cfg.telegram_token).concurrent_updates(True).build()
        app = self.app
        app.add_handler(CommandHandler(["start", "home"], self.cmd_start))
        app.add_handler(CommandHandler("help", self.cmd_help))
        app.add_handler(CommandHandler("stop", self.cmd_stop))
        app.add_handler(CommandHandler(["stats", "accuracy"], self.cmd_stats))
        app.add_handler(CommandHandler("last", self.cmd_last))
        app.add_handler(CommandHandler("predict", self.cmd_predict))
        app.add_handler(CommandHandler("status", self.cmd_status))
        app.add_handler(CommandHandler("ask", self.cmd_ask))
        app.add_handler(CommandHandler("admin", self.cmd_admin))
        app.add_handler(CommandHandler("snap", self.cmd_snap))
        app.add_handler(CommandHandler("health", self.cmd_health))
        app.add_handler(CommandHandler("broadcast", self.cmd_broadcast))
        app.add_handler(CommandHandler("unban", self.cmd_unban))
        app.add_handler(CommandHandler("users", self.cmd_users))
        app.add_handler(CommandHandler("find", self.cmd_find))
        app.add_handler(CommandHandler("remove", self.cmd_remove))
        app.add_handler(CommandHandler("ban", self.cmd_ban))
        app.add_handler(CommandHandler("grant", self.cmd_grant))
        app.add_handler(CommandHandler("revoke", self.cmd_revoke))
        app.add_handler(CommandHandler("resettrial", self.cmd_resettrial))
        app.add_handler(CommandHandler("cancel", self.cmd_cancel))
        app.add_handler(CommandHandler("activate", self.cmd_activate))
        app.add_handler(CommandHandler("report", self.cmd_report))
        app.add_handler(CommandHandler("keys", self.cmd_keys))
        app.add_handler(CommandHandler("genkey", self.cmd_genkey))
        app.add_handler(CallbackQueryHandler(self.on_callback))
        app.add_handler(MessageHandler(filters.CONTACT, self.on_contact))
        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.on_text))
        app.add_error_handler(self.on_error)
        return app

    async def start(self) -> None:
        if self.app is None:
            self.build()
        assert self.app is not None
        await self.app.initialize()
        await self.app.start()
        await self.app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES, drop_pending_updates=False
        )
        me = await self.app.bot.get_me()
        self.username = me.username or ""
        self.running = True
        log.info("telegram bot online as @%s", me.username)
        asyncio.create_task(self.view_loop())
        asyncio.create_task(self.snapshot_loop())
        # Auto-open a live card for every active member so the desk updates in place
        # even after a restart (or whenever a members card view row goes missing).
        asyncio.create_task(self._reopen_live_cards())
        # Watcher: if the warm-start ever silently misses a member, the card is
        # re-opened once a minute. This is the safety net behind the "it stayed
        # stuck" path - real auto-open, not a refresh button.
        asyncio.create_task(self._reopen_watcher())

    async def _reopen_watcher(self) -> None:
        """Keep every active members card alive after any restart.

        The immediate warm-start (_reopen_live_cards) is the fast path. This is
        the slow path: every minute, walk active members and confirm each has a
        card view the view_loop can edit. If the view row is gone (bot restarted
        with a closed chat, or the row got dropped), a fresh card is sent so the
        loop immediately has something to keep editing in place.
        """
        await asyncio.sleep(60)
        log.info("reopen_watcher: started")
        while self.running:
            try:
                await asyncio.sleep(60)
                if self.app is None:
                    continue
                card_chats: dict[int, int] = {}
                for row in self.storage.views():
                    if row["view"] == "card" and row["mode"]:
                        card_chats[row["telegram_id"]] = row["chat_id"]
                refreshed = 0
                for user in self.storage.users(active_only=True):
                    tid = user.telegram_id
                    if tid in card_chats:
                        continue
                    key = self.default_node(tid)
                    chat_id = card_chats.get(tid)
                    try:
                        if chat_id is None:
                            sent = await self.app.bot.send_message(
                                chat_id=tid, text="", parse_mode="HTML"
                            )
                            chat_id = sent.chat.id
                        await self._show(chat_id, user, "card", key, new=True)
                        refreshed += 1
                    except Exception:  # noqa: BLE001 - blocked/kicked, skip
                        continue
                if refreshed:
                    log.info("reopen_watcher: re-opened %d card(s)", refreshed)
            except Exception as exc:  # noqa: BLE001
                log.warning("reopen_watcher: %s", exc)

    async def stop(self) -> None:
        self.running = False
        if self.app is None:
            return
        try:
            await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
        except Exception as exc:  # noqa: BLE001
            log.warning("bot shutdown: %s", exc)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #
    def default_node(self, tid: int) -> str:
        return self.last_node.get(tid) or "1min"

    async def _charge_trial(self, user: User, key: str) -> bool:
        """Charge one trial prediction for the period currently on screen.

        Deduped per (account, period) inside storage, so the auto-updater
        re-rendering a card 100x only ever spends one credit. Returns False
        when the member ran out of trial budget (the welcome/upgrade flow
        takes over from the caller).
        """
        if user.is_admin:
            return True
        state = self.engine.state(key)
        period = state.issue
        if not period:
            return True
        consumed, trial = self.storage.spend_trial_prediction(user.telegram_id, period)
        if not trial.get("trial"):
            return True
        if trial.get("left", 0) <= 0:
            # budget gone (this credit was the last one, or it was already
            # spent): tell them once, then the locked/welcome flow takes over.
            await self._trial_exhausted(user)
            return False
        return True

    async def _trial_exhausted(self, user: User) -> None:
        """One-time heads-up when a trial runs out of predictions."""
        if user.telegram_id in self.trial_notified:
            return
        self.trial_notified.add(user.telegram_id)
        state = self.storage.trial_state(user.telegram_id)
        hours_left = max(0, state.get("window_left", 0)) // 3600
        mins_left = (max(0, state.get("window_left", 0)) % 3600) // 60
        text = (
            "🎟️ <b>Trial credits used up</b>\n"
            f"You used all <b>{state.get('quota', 10)} predictions</b> of your free "
            f"trial.\n"
            f"⏱ The time window still has {hours_left}h {mins_left}m left, but the "
            "prediction budget is what stops first.\n"
            "\n<i>Unlock unlimited predictions with full access 👇</i>"
        )
        try:
            if self.app is not None:
                await self.app.bot.send_message(
                    user.telegram_id, text, parse_mode="HTML",
                    reply_markup=render.welcome_keyboard(False, True),
                )
        except Exception as exc:  # noqa: BLE001
            log.debug("trial exhausted notice failed: %s", exc)


    def admin_code(self) -> str:
        return self.storage.get_meta("admin_code", self.cfg.admin_code)

    def _render(self, user: User, view: str, key: str | None) -> str:
        states = {k: self.engine.state(k) for k in MODES}
        trial = None if user.is_admin else self.storage.trial_state(user.telegram_id)
        if view == "card" and key:
            return render.card(user, self.engine.state(key))
        if view == "status":
            health = self.orch.health()
            extra = {"gate_rejects": getattr(self.engine, "gate_rejects", 0),
                     "queued": len(self.engine.ai_queue)}
            return render.status_view(states, health, self.storage.snapshot_stats(),
                                      extra, trial)
        if view == "stats" and key:
            state = self.engine.state(key)
            window = analyzer.window_stats(
                self.storage.rounds(key, self.cfg.analysis_window), self.cfg.analysis_window
            )
            return render.stats_view(user, key, state, window)
        if view == "settings":
            return render.settings_view(user)
        if view == "profile":
            return render.profile_view(user, states)
        if view == "help":
            return render.help_view()
        if view == "admin":
            return render.admin_view(self.storage.snapshot_stats(), self.storage.user_count(),
                                     self.orch.health(), self.admin_code(),
                                     self.storage.business_stats(),
                                     len(self.storage.pending_payments()))
        if view == "payments":
            return render.payments_view(self.storage.pending_payments())
        if view == "welcome":
            return render.welcome(user, self.storage.is_active(user.telegram_id),
                                  self.storage.active_until(user.telegram_id),
                                  self.cfg,
                                  self.storage.ensure_ref_code(user.telegram_id),
                                  self.storage.trial_used(user.telegram_id))
        if view == "buy":
            return render.buy_view(self.cfg)
        if view == "sub":
            return render.sub_view(user, self.storage.is_active(user.telegram_id),
                                   self.storage.active_until(user.telegram_id), self.cfg)
        if view == "how":
            return render.how_view()
        if view == "health":
            return render.health_view(self.orch.health())
        if view == "ask":
            mode = MODES.get(key or "1min", MODES["1min"])
            return render.ask_prompt(persona_for(mode))
        if view == "menu":
            return render.menu(user, states, trial)
        return render.menu(user, states, trial)

    def _keyboard(self, user: User, view: str, key: str | None) -> InlineKeyboardMarkup:
        if view == "card" and key:
            return render.card_keyboard(key, user)
        if view == "welcome":
            return render.welcome_keyboard(self.storage.is_active(user.telegram_id),
                                           self.storage.trial_used(user.telegram_id))
        if view == "buy":
            return render.buy_keyboard()
        if view == "payments":
            return render.payments_keyboard(self.storage.pending_payments())
        if view == "settings":
            return render.settings_keyboard(user)
        if view == "admin":
            return render.admin_keyboard(user)
        if view == "health":
            return render.health_keyboard()
        if view == "stats" and key:
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Re-analyse", callback_data=f"refresh:stats:{key}"),
                 InlineKeyboardButton("🎯 Open card", callback_data=f"node:{key}")],
                [InlineKeyboardButton("🔙 Menu", callback_data="menu")],
            ])
        if view == "ask" and key:
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("🎯 Back to card", callback_data=f"node:{key}")],
                [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
            ])
        if view in ("help", "profile"):
            return InlineKeyboardMarkup([[InlineKeyboardButton("🔙 Menu", callback_data="menu")]])
        if view == "status":
            return InlineKeyboardMarkup([
                [InlineKeyboardButton("🔄 Refresh", callback_data="view:status"),
                 InlineKeyboardButton("🎯 Open card", callback_data=f"node:{key or '1min'}")],
                [InlineKeyboardButton("🏠 Menu", callback_data="menu")],
            ])
        return render.menu_keyboard(user)

    async def _show(self, chat_id_or_tid: int, user: User, view: str, key: str | None,
                    *, message_id: int | None = None, new: bool = False) -> int | None:
        """Edit the user's existing desk message in place, or send a fresh one.

        When ``new=True`` with no ``message_id`` it sends a brand-new message and
        records its chat id so the auto-updater can keep editing it.
        """
        assert self.app is not None
        chat_id = chat_id_or_tid
        if view == "welcome" and new and message_id is None:
            # celebratory banner above the welcome text (fresh screens only,
            # never on edits - one image per /start, not per refresh)
            await self._send_banner(chat_id, user)
        if view == "card" and key:
            # opening a node starts the calculation + AI read - show typing while it runs
            await self._typing(chat_id)
            try:
                self.engine.ensure_prediction(key)
            except Exception:  # noqa: BLE001 - never block the card on this
                log.debug("ensure_prediction failed for %s", key, exc_info=True)
            # trial budget: one credit per period actually shown (idempotent)
            if not await self._charge_trial(user, key):
                view, key = "welcome", None
        text = self._render(user, view, key)
        reply_markup = self._keyboard(user, view, key)
        target = message_id
        if target is None and not new:
            with self.storage._lock:
                row = self.storage.conn.execute(
                    "SELECT message_id FROM views WHERE telegram_id=? AND chat_id=?",
                    (user.telegram_id, chat_id),
                ).fetchone()
            target = row["message_id"] if row else None

        sent = None
        if target:
            try:
                await self.app.bot.edit_message_text(
                    chat_id=chat_id, message_id=target, text=text,
                    parse_mode="HTML", reply_markup=reply_markup,
                    disable_web_page_preview=True,
                )
                self.storage.save_view(user.telegram_id, chat_id, target, view, key)
                self.storage.set_view_text(user.telegram_id, chat_id, text, time.time())
                return target
            except Exception as exc:  # noqa: BLE001 - fall through to a fresh message
                message = str(exc)
                if "message is not modified" in message:
                    self.storage.save_view(user.telegram_id, chat_id, target, view, key)
                    self.storage.set_view_text(user.telegram_id, chat_id, text, time.time())
                    return target
                if "message to edit not found" not in message and \
                        "message can't be edited" not in message and \
                        "message to delete not found" not in message:
                    log.debug("edit failed, sending fresh: %s", message)

        try:
            sent = await self.app.bot.send_message(
                chat_id=chat_id, text=text, parse_mode="HTML",
                reply_markup=reply_markup, disable_web_page_preview=True,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("send to %s failed: %s", chat_id, exc)
            return None
        self.storage.save_view(user.telegram_id, chat_id, sent.message_id, view, key)
        self.storage.set_view_text(user.telegram_id, chat_id, text, time.time())
        return sent.message_id

    def _blocked(self, update: Update, user: User) -> bool:
        if not user.banned and not self.storage.is_banned(user.telegram_id):
            return False
        return True

    async def _send_banner(self, chat_id: int, user: User) -> None:
        """Attractive welcome image above the /start text - best effort."""
        try:
            trial = {} if user.is_admin else self.storage.trial_state(user.telegram_id)
            trial_left = ""
            if trial.get("trial"):
                trial_left = (f"{int(trial.get('left', 0))} trial predictions left · "
                              f"{max(0, int(trial.get('window_left', 0))) // 3600}h"
                              f"{(max(0, int(trial.get('window_left', 0))) % 3600) // 60}m")
            data = render_welcome(
                user.name or user.username or "",
                self.storage.is_active(user.telegram_id) or user.is_admin,
                trial_left,
            )
            photo = io.BytesIO(data)
            photo.name = "welcome.png"
            await self.app.bot.send_photo(chat_id, photo)
        except Exception as exc:  # noqa: BLE001 - banner is decoration only
            log.debug("welcome banner failed: %s", exc)

    async def _refuse(self, update: Update, user: User | None) -> None:
        text = render.banned_notice(user.ban_reason if user else "")
        if update.callback_query:
            await self._answer_once(update.callback_query, text, True)
        elif update.effective_message:
            try:
                await update.effective_message.reply_text(text, parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------------ #
    # onboarding
    # ------------------------------------------------------------------ #
    async def cmd_start(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat = update.effective_chat
        telegram_user = update.effective_user
        if not chat or not telegram_user:
            return
        user = self.storage.touch_user(telegram_user.id, username=telegram_user.username or "")

        if user.banned or self.storage.is_banned(user.telegram_id):
            await self._refuse(update, user)
            return

        # referral deep link: /start REF-XXXXX (first referrer wins)
        payload = (ctx.args[0] if ctx.args else "").strip().upper()
        if payload.startswith("REF-"):
            referrer = self.storage.user_id_by_ref(payload)
            if referrer and self.storage.set_referred(user.telegram_id, referrer):
                log.info("referral recorded: %s invited %s", referrer, user.telegram_id)

        if user.stage == "new":
            self.storage.update_user(user.telegram_id, stage="name")
            await update.effective_message.reply_text(render.onboard_name(), parse_mode="HTML")
            return
        if user.stage == "name":
            await update.effective_message.reply_text(render.onboard_name(), parse_mode="HTML")
            return
        if user.stage == "age":
            name = user.name or "there"
            await update.effective_message.reply_text(
                render.onboard_age(name), parse_mode="HTML",
                reply_markup=render.age_keyboard(),
            )
            return

        # returning member: desk if subscribed, activation welcome otherwise
        # new=True -> /start always opens a FRESH message instead of editing
        # whatever stale message happened to be recorded last.
        view = "menu" if (user.is_admin or self.storage.is_active(user.telegram_id)) else "welcome"
        await self._show(chat.id, user, view, None, new=True)

    async def on_text(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        chat, telegram_user = update.effective_chat, update.effective_user
        message = update.effective_message
        if not chat or not telegram_user or not message:
            return
        text = (message.text or "").strip()
        user = self.storage.touch_user(telegram_user.id, username=telegram_user.username or "")

        if user.banned or self.storage.is_banned(user.telegram_id):
            if PHONE_RE.match(text):
                self.storage.record_banned_number(text, user.telegram_id,
                                                  user.name, user.ban_reason or "underage")
            await self._refuse(update, user)
            return

        intent = self.awaiting.pop(user.telegram_id, None)
        if intent == "broadcast":
            if user.is_admin:
                await self._broadcast(text)
                return
        if intent == "ask":
            await self._answer_question(update, user, text)
            return
        if intent == "upi_ref":
            ref = re.sub(r"\s+", "", text)[:48]
            pid = self.storage.create_payment(user.telegram_id,
                                              user.username or user.name or "",
                                              ref, self.cfg.price_inr)
            await message.reply_text(render.pay_submitted(pid), parse_mode="HTML")
            await self._notify_payment(pid)
            return
        if intent == "activate":
            await self._redeem(update, user, text)
            return
        if intent == "finduser":
            if not user.is_admin:
                return
            hits = self.storage.admin_search_users(text)
            if not hits:
                await message.reply_text(
                    f"🔍 No member matched <code>{render.esc(text[:40])}</code>",
                    parse_mode="HTML")
                return
            await message.reply_text(
                render.users_view(hits, 0, len(hits), per_page=len(hits) or 1),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton(f"🔎 {row['label'][:18]}",
                                          callback_data=f"u:view:{row['telegram_id']}")
                     for row in hits[:3]],
                    [InlineKeyboardButton("🔙 All members", callback_data="users:0")],
                ]),
            )
            return

        if user.stage == "name":
            if PHONE_RE.match(text) and self.storage.number_is_banned(text):
                await self._refuse(update, user)
                return
            name = re.sub(r"\s+", " ", text)[:32]
            self.storage.update_user(user.telegram_id, name=name, stage="age")
            await message.reply_text(render.onboard_age(name), parse_mode="HTML",
                                     reply_markup=render.age_keyboard())
            return

        if user.stage == "age":
            await message.reply_text(
                "🔞 Please use the buttons - <b>are you over 18?</b>",
                parse_mode="HTML", reply_markup=render.age_keyboard(),
            )
            return

        # active member: a typed message refreshes whatever is on screen
        if user.is_admin and await self._admin_value(update, user, text):
            return
        if PHONE_RE.match(text):
            self.storage.update_user(user.telegram_id, phone=normalize_phone(text))
            await message.reply_text("📇 Number saved to your profile.", parse_mode="HTML")
            return
        with self.storage._lock:
            row = self.storage.conn.execute(
                "SELECT message_id, view, mode FROM views WHERE telegram_id=? AND chat_id=?",
                (user.telegram_id, chat.id),
            ).fetchone()
        view = row["view"] if row else "menu"
        key = row["mode"] if row and row["view"] == "card" else None
        # an expired subscription falls back to the welcome screen
        if not user.is_admin and not self.storage.is_active(user.telegram_id):
            if view in ("card", "stats"):
                view, key = "welcome", None
        await self._show(chat.id, user, view, key,
                         message_id=(row["message_id"] if row else message.message_id))

    async def on_contact(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        message, chat = update.effective_message, update.effective_chat
        if not message or not chat:
            return
        contact = message.contact
        if not contact:
            return
        user = self.storage.touch_user(update.effective_user.id)
        phone = normalize_phone(contact.phone_number)
        if self.storage.number_is_banned(phone) or user.banned:
            self.storage.record_banned_number(phone, user.telegram_id, user.name, "underage")
            if not user.banned:
                self.storage.ban_user(user.telegram_id, reason="banned number reused",
                                      name=user.name, username=user.username, phone=phone)
            await message.reply_text(render.banned_notice("banned mobile number"), parse_mode="HTML")
            return
        self.storage.update_user(user.telegram_id, phone=phone)
        await message.reply_text("📇 Number saved to your profile.", parse_mode="HTML")

    # ------------------------------------------------------------------ #
    # callbacks
    # ------------------------------------------------------------------ #
    async def on_callback(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        chat, telegram_user = update.effective_chat, update.effective_user
        if not query or not chat or not telegram_user:
            return
        data = query.data or ""
        user = self.storage.touch_user(telegram_user.id, username=telegram_user.username or "")
        if user.banned or self.storage.is_banned(user.telegram_id):
            await self._refuse(update, user)
            return

        parts = data.split(":")
        action = parts[0]
        message = query.message

        # ---- age gate -------------------------------------------------- #
        if action == "age":
            await self._answer_once(query)
            if parts[1] == "yes":
                self.storage.update_user(user.telegram_id, stage="active")
                user = self.storage.get_user(user.telegram_id) or user
                first_view = "menu" if (user.is_admin or
                                        self.storage.is_active(user.telegram_id)) \
                    else "welcome"
                if message:
                    await self._show(chat.id, user, first_view, None,
                                     message_id=message.message_id, new=True)
                else:
                    await self._show(chat.id, user, first_view, None, new=True)
            else:
                self.storage.ban_user(
                    user.telegram_id, reason="underage (declined 18+ gate)",
                    name=user.name, username=user.username, phone=user.phone,
                )
                if message:
                    try:
                        await query.edit_message_text(render.age_declined(), parse_mode="HTML")
                    except Exception:  # noqa: BLE001
                        pass
                log.info("banned underage account %s (%s)", user.telegram_id, user.username or "-")
            return

        if user.stage != "active":
            await self._refuse(update, user)
            return

        active = user.is_admin or self.storage.is_active(user.telegram_id)
        wants = parts[1] if len(parts) > 1 else ""

        # ---- admin-only views (callbacks can be forged - never trust them) --
        if action == "view" and wants in ("admin", "payments", "health") and not user.is_admin:
            await self._answer_once(query, "👑 admins only", True)
            return

        # ---- activation lock: the desk needs a live subscription ---------- #
        if not active and (action in ("node", "refresh", "ask", "desk")
                           or (action == "view" and wants in ("stats", "ask", "status"))):
            if action == "desk":
                await self._answer_once(query)
                if message:
                    await self._show(chat.id, user, "buy", None,
                                     message_id=message.message_id)
                else:
                    await self._show(chat.id, user, "buy", None, new=True)
                return
            await self._answer_once(
                query, "🔒 Activate your subscription to open the desk", True)
            if message:
                await self._show(chat.id, user, "welcome", None,
                                 message_id=message.message_id)
            else:
                await self._show(chat.id, user, "welcome", None, new=True)
            return

        # ---- activation / payment flow ----------------------------------- #
        if action == "desk":
            if message:
                await self._show(chat.id, user, "menu", None, message_id=message.message_id)
            else:
                await self._show(chat.id, user, "menu", None, new=True)
            return

        if action == "activate":
            self.awaiting[user.telegram_id] = "activate"
            await self._answer_once(query)
            await self.app.bot.send_message(
                chat.id,
                "🔑 Send your activation key, like <code>/activate WINGO-XXXXX-XXXXX-XXXXX</code>\n"
                "<i>or just paste the key as a message.</i>",
                parse_mode="HTML",
            )
            return

        if action == "paid":
            self.awaiting[user.telegram_id] = "upi_ref"
            await self._answer_once(query)
            await self.app.bot.send_message(
                chat.id,
                "🧾 Send your <b>UPI reference number</b> (the UTR / transaction id "
                "from your payment app):",
                parse_mode="HTML",
            )
            return

        if action == "pay":
            if not user.is_admin:
                await self._answer_once(query, "👑 admins only", True)
                return
            await self._decide_payment(query, user, wants, parts[2] if len(parts) > 2 else "")
            return

        if action == "report":
            if not user.is_admin:
                await self._answer_once(query, "👑 admins only", True)
                return
            await self._answer_once(query)
            await self._typing(chat.id)
            await self.app.bot.send_message(chat.id, self._report_text(), parse_mode="HTML")
            return

        # ---- trial / referral / contact ---------------------------------- #
        if action == "trial":
            if active:
                await self._answer_once(query, "✅ already active", True)
                return
            if wants == "info" or self.storage.trial_used(user.telegram_id):
                await self._answer_once(query, "⏳ one free trial per account", True)
                if message:
                    await self._show(chat.id, user, "welcome", None,
                                     message_id=message.message_id)
                return
            ok, until = self.storage.use_trial(
                user.telegram_id, self.cfg.trial_hours, self.cfg.trial_predictions)
            if not ok:
                await self._answer_once(query, "⏳ one free trial per account", True)
                return
            await self._answer_once(
                query,
                f"⏳ trial live · {self.cfg.trial_predictions} predictions / "
                f"{self.cfg.trial_hours}h 🎉")
            await self.app.bot.send_message(
                chat.id,
                f"⏳ <b>Trial activated!</b>\n"
                f"🎟️ <b>{self.cfg.trial_predictions} predictions</b> over "
                f"<b>{self.cfg.trial_hours} hours</b> — until "
                f"<b>{datetime.fromtimestamp(until).strftime('%d %b · %H:%M')}</b>.\n"
                "\n<i>Each live round you open counts once. The desk is opening 👇</i>",
                parse_mode="HTML",
            )
            if message:
                await self._show(chat.id, user, "menu", None, message_id=message.message_id)
            else:
                await self._show(chat.id, user, "menu", None, new=True)
            return

        if action == "refer":
            await self._answer_once(query)
            code = self.storage.ensure_ref_code(user.telegram_id)
            count = self.storage.ref_count(user.telegram_id)
            await self.app.bot.send_message(
                chat.id,
                render.refer_view(code, self.username, count,
                                  self.cfg.referral_bonus_days),
                parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="view:welcome")],
                ]),
            )
            return

        if action == "contact":
            await self._answer_once(query)
            await self._typing(chat.id)
            await self.app.bot.send_message(
                chat.id, render.contact_view(), parse_mode="HTML",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("🔙 Back", callback_data="view:welcome")],
                ]),
            )
            who = f"@{render.esc(user.username)}" if user.username \
                else f"<code>{user.telegram_id}</code>"
            targets = set(self.cfg.admin_chat_ids)
            targets.update(u.telegram_id for u in self.storage.users() if u.is_admin)
            for admin_id in targets:
                try:
                    await self.app.bot.send_message(
                        admin_id,
                        f"💬 <b>{who}</b> (id <code>{user.telegram_id}</code>) "
                        "wants to contact you.",
                        parse_mode="HTML",
                    )
                except Exception:  # noqa: BLE001
                    pass
            return

        await self._answer_once(query)

        # ---- nodes ------------------------------------------------------ #
        if action == "node":
            key = parts[1] if len(parts) > 1 else "1min"
            if key not in MODES:
                key = "1min"
            self.last_node[user.telegram_id] = key
            if message:
                await self._show(chat.id, user, "card", key, message_id=message.message_id)
            else:
                await self._show(chat.id, user, "card", key, new=True)
            return

        if action == "refresh":
            arg = parts[1] if len(parts) > 1 else ""
            if arg == "stats":
                key = parts[2] if len(parts) > 2 else self.default_node(user.telegram_id)
                view = "stats"
            elif arg in MODES:
                key, view = arg, "card"
                self.last_node[user.telegram_id] = key
            else:
                key, view = self.default_node(user.telegram_id), "card"
            if message:
                await self._show(chat.id, user, view, key, message_id=message.message_id)
            else:
                await self._show(chat.id, user, view, key, new=True)
            return

        if action == "menu":
            if message:
                await self._show(chat.id, user, "menu", None, message_id=message.message_id)
            else:
                await self._show(chat.id, user, "menu", None, new=True)
            return

        if action == "view":
            name = parts[1] if len(parts) > 1 else "menu"
            key = self.default_node(user.telegram_id) if name in ("stats", "ask") else None
            if message:
                await self._show(chat.id, user, name, key, message_id=message.message_id)
            else:
                await self._show(chat.id, user, name, key, new=True)
            return

        if action == "back":
            name = parts[1] if len(parts) > 1 else "menu"
            key = parts[2] if len(parts) > 2 and parts[2] else None
            if name == "card" and not key:
                key = self.default_node(user.telegram_id)
            if message:
                await self._show(chat.id, user, name, key, message_id=message.message_id)
            else:
                await self._show(chat.id, user, name, key, new=True)
            return

        if action == "toggle":
            setting = parts[1] if len(parts) > 1 else ""
            await self._toggle(user, setting)
            # re-read the member so the button label reflects the NEW state -
            # rendering with the stale object is what made the snapshot stop
            # button look dead (label never flipped, admin tapped again).
            user = self.storage.get_user(user.telegram_id) or user
            if setting == "auto_snapshots":
                await self._answer_once(
                    query,
                    "📸 auto photos OFF - snapshots stopped"
                    if not user.settings.get("auto_snapshots")
                    else "📸 auto photos ON - every "
                         f"{self.cfg.snapshot_interval}s",
                )
            # the snapshot stop-button lives on the admin console - stay there
            name = "admin" if setting == "auto_snapshots" else "settings"
            key = None
            if message:
                await self._show(chat.id, user, name, key, message_id=message.message_id)
            else:
                await self._show(chat.id, user, name, key, new=True)
            return

        if action == "ask":
            mode_key = self.default_node(user.telegram_id)
            self.awaiting[user.telegram_id] = "ask"
            if message:
                await self._show(chat.id, user, "ask", mode_key, message_id=message.message_id)
            else:
                await self._show(chat.id, user, "ask", mode_key, new=True)
            return

        if action == "broadcast":
            if not user.is_admin:
                return
            self.awaiting[user.telegram_id] = "broadcast"
            await query.message.reply_text(
                render.broadcast_prompt(len(self.storage.users(active_only=True))),
                parse_mode="HTML",
            )
            return

        if action == "snap":
            if not user.is_admin:
                await self._answer_once(query, "admins only", True)
                return
            target = parts[1] if len(parts) > 1 else "all"
            await self._send_snapshots(chat.id, target)
            if message and user.stage == "active":
                await self._show(chat.id, user, "admin", None, message_id=message.message_id)
            return

        # ---- admin: member directory ------------------------------------ #
        if action == "users":
            if not user.is_admin:
                await self._answer_once(query, "👑 admins only", True)
                return
            try:
                page = max(0, int(parts[1]))
            except (IndexError, ValueError):
                page = 0
            rows, total = self.storage.browse_users(page)
            text = render.users_view(rows, page, total)
            markup = render.users_keyboard(page, total)
            if message:
                try:
                    await query.edit_message_text(
                        text, parse_mode="HTML", reply_markup=markup,
                        disable_web_page_preview=True)
                except Exception:  # noqa: BLE001
                    await self.app.bot.send_message(chat.id, text, parse_mode="HTML",
                                                    reply_markup=markup)
            else:
                await self.app.bot.send_message(chat.id, text, parse_mode="HTML",
                                                reply_markup=markup)
            return

        if action == "finduser":
            if not user.is_admin:
                await self._answer_once(query, "👑 admins only", True)
                return
            self.awaiting[user.telegram_id] = "finduser"
            await self._answer_once(query)
            await self.app.bot.send_message(
                chat.id,
                "🔍 <b>Find a member</b>\n\n"
                "Send a Telegram id, @username or name fragment.",
                parse_mode="HTML")
            return

        if action == "u":
            if not user.is_admin:
                await self._answer_once(query, "👑 admins only", True)
                return
            await self._admin_user_action(query, chat, parts)
            return

    # ------------------------------------------------------------------ #
    async def _toggle(self, user: User, setting: str) -> None:
        settings = dict(user.settings)
        if setting == "auto_refresh":
            cycle = {2: 5, 5: 10, 10: 2}
            settings["auto_refresh"] = cycle.get(int(settings.get("auto_refresh", 5)), 5)
        elif setting in DEFAULT_SETTINGS:
            settings[setting] = not bool(settings.get(setting))
        else:
            return
        self.storage.update_user(user.telegram_id, settings=settings)
        log.info("user %s settings -> %s", user.telegram_id, {k: settings[k] for k in DEFAULT_SETTINGS})

    async def _answer_question(self, update: Update, user: User, question: str) -> None:
        mode_key = self.default_node(user.telegram_id)
        mode = MODES.get(mode_key, MODES["1min"])
        persona = persona_for(mode)
        context = self.engine.ai_context(mode_key)
        prompt = (
            f"A member asks: {question}\n\n"
            f"Live {mode.label} period {context['issue']}, our pick {context['number']} "
            f"({', '.join(context['colours'])}, {context['size']}), conviction "
            f"{context['conviction']}%, colour hit-rate {context['colour_rate']}%, "
            f"streak {context['streak']}. Answer as {persona} in at most three short "
            "sentences, plain English, no emoji."
        )
        fallback = (
            f"{persona} here: our window read favours {context['number']} at "
            f"{context['conviction']}% conviction, but every round stands alone."
        )
        message = update.effective_message
        if message:
            try:
                await message.reply_chat_action("typing")
            except Exception:  # noqa: BLE001
                pass
        result = await asyncio.to_thread(
            self.orch.complete_or, system_prompt(persona), prompt, fallback,
            max_tokens=260, temperature=0.8,
        )
        badge = {"gemini": "Gemini", "openrouter": "Nemotron", "groq": "Groq",
                 "local": "local fallback"}.get(result.provider, result.provider)
        text = (
            f"🤖 <b>{persona}</b> · via <b>{render.esc(badge)}</b>\n"
            f"<i>{render.esc(result.model)}</i>\n"
            f"{RULE}\n"
            f"{render.esc(result.text)}\n"
            f"{RULE}\n"
            f"<i>{context['issue']} · pick {context['number']} · "
            f"conviction {context['conviction']}%</i>"
        )
        try:
            await message.reply_text(text, parse_mode="HTML")
        except Exception as exc:  # noqa: BLE001
            log.warning("ask reply failed: %s", exc)

    async def cmd_ask(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        question = " ".join(ctx.args).strip()
        user = self.storage.touch_user(update.effective_user.id)
        if user.banned or self.storage.is_banned(user.telegram_id):
            await self._refuse(update, user)
            return
        if await self._locked(update, user):
            return
        if not question:
            mode_key = self.default_node(user.telegram_id)
            self.awaiting[user.telegram_id] = "ask"
            await update.effective_message.reply_text(
                render.ask_prompt(persona_for(MODES.get(mode_key, MODES["1min"]))),
                parse_mode="HTML",
            )
            return
        await self._answer_question(update, user, question)

    # ------------------------------------------------------------------ #
    # commands
    # ------------------------------------------------------------------ #
    def _require_member(self, update: Update) -> User | None:
        user = self.storage.touch_user(update.effective_user.id,
                                       username=update.effective_user.username or "")
        if user.banned or self.storage.is_banned(user.telegram_id):
            return None
        return user

    async def cmd_help(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._require_member(update)
        if not user:
            await self._refuse(update, self.storage.get_user(update.effective_user.id))
            return
        await update.effective_message.reply_text(
            render.help_view(), parse_mode="HTML", disable_web_page_preview=True
        )

    async def cmd_stop(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._require_member(update)
        if not user:
            return
        settings = dict(user.settings)
        settings.update({"push": False, "alerts": False})
        self.storage.update_user(user.telegram_id, settings=settings)
        await update.effective_message.reply_text(
            "🔕 Pushes muted. Your card still updates when you open it - "
            "use /start to switch alerts back on.", parse_mode="HTML"
        )

    async def cmd_cancel(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        self.awaiting.pop(update.effective_user.id, None)
        await update.effective_message.reply_text("Cancelled.", parse_mode="HTML")

    # ------------------------------------------------------------------ #
    # activation / payments (SaaS)
    # ------------------------------------------------------------------ #
    async def _answer_once(self, query, text: str = "", alert: bool = False) -> None:
        """Telegram accepts exactly one answer per tap - guard every call."""
        if query is None or getattr(query, "_wingo_answered", False):
            return
        try:
            setattr(query, "_wingo_answered", True)
            await query.answer(text, show_alert=alert)
        except Exception:  # noqa: BLE001 - stale/expired query after a restart
            pass

    async def _locked(self, update: Update, user: User) -> bool:
        """True when the member has no live subscription (replies with welcome)."""
        if user.is_admin or self.storage.is_active(user.telegram_id):
            return False
        chat = update.effective_chat
        if chat and self.app is not None:
            await self.app.bot.send_message(
                chat.id,
                render.welcome(user, False, 0, self.cfg,
                               self.storage.ensure_ref_code(user.telegram_id),
                               self.storage.trial_used(user.telegram_id)),
                parse_mode="HTML",
                reply_markup=render.welcome_keyboard(False,
                                                     self.storage.trial_used(user.telegram_id)),
            )
        return True

    async def _typing(self, chat_id: int) -> None:
        try:
            await self.app.bot.send_chat_action(chat_id, "typing")
        except Exception:  # noqa: BLE001
            pass

    async def _referral_reward(self, telegram_id: int) -> None:
        """Pay the referrer the moment their invitee activates."""
        if not self.cfg.referral_bonus_days:
            return
        with self.storage._lock:
            row = self.storage.conn.execute(
                "SELECT referred_by FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
        referrer = int(row["referred_by"]) if row and row["referred_by"] else 0
        if not referrer:
            return
        new_until = self.storage.extend(referrer, self.cfg.referral_bonus_days)
        if not new_until:
            return
        stamp = datetime.fromtimestamp(new_until).strftime("%d %b %Y · %H:%M")
        log.info("referral bonus: invitee %s activated -> referrer %s +%dd",
                 telegram_id, referrer, self.cfg.referral_bonus_days)
        try:
            await self.app.bot.send_message(
                referrer,
                f"🔗 <b>Referral bonus!</b> A friend just activated — "
                f"<b>+{self.cfg.referral_bonus_days} days</b> added.\n"
                f"Now valid until <b>{stamp}</b> 🎉",
                parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("referral notify failed: %s", exc)

    async def _redeem(self, update: Update, user: User, raw: str) -> None:
        ok, msg, expires = self.storage.redeem_key(
            raw, user.telegram_id, self.cfg.activation_days)
        if not ok:
            await update.effective_message.reply_text(
                f"❌ <b>{render.esc(msg)}</b>\n\nCheck the key and try again, "
                "or /start to see your options.", parse_mode="HTML")
            return
        await update.effective_message.reply_text(
            render.activated_view(expires, self.cfg.activation_days), parse_mode="HTML")
        await self._referral_reward(user.telegram_id)
        chat = update.effective_chat
        if chat:
            await self._show(chat.id, user, "menu", None)
        log.info("user %s activated until %s", user.telegram_id, expires)

    async def cmd_activate(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id,
                                       username=update.effective_user.username or "")
        if user.banned or self.storage.is_banned(user.telegram_id):
            await self._refuse(update, user)
            return
        key = " ".join(ctx.args).strip()
        if not key:
            await update.effective_message.reply_text(
                "🔑 Send your key: <code>/activate WINGO-XXXXX-XXXXX-XXXXX</code>",
                parse_mode="HTML")
            return
        await self._redeem(update, user, key)

    async def _notify_payment(self, payment_id: int) -> None:
        row = self.storage.get_payment(payment_id)
        if not row:
            return
        who = f"@{render.esc(row['username'])}" if row["username"] \
            else f"<code>{row['telegram_id']}</code>"
        text = (
            f"🧾 <b>New payment #{row['id']}</b>\n"
            f"Amount <b>₹{row['amount']}</b> · {who}\n"
            f"ref <code>{render.esc(row['upi_ref'] or '—')}</code>\n\n"
            "Verify the UTR in your UPI app, then approve or reject:"
        )
        targets = set(self.cfg.admin_chat_ids)
        targets.update(u.telegram_id for u in self.storage.users() if u.is_admin)
        for chat_id in targets:
            try:
                await self.app.bot.send_message(
                    chat_id, text, parse_mode="HTML",
                    reply_markup=render.payments_keyboard([row]),
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("payment notify %s failed: %s", chat_id, exc)

    # ------------------------------------------------------------------ #
    # admin: member actions
    # ------------------------------------------------------------------ #
    async def _admin_user_action(self, query, chat, parts: list[str]) -> None:
        """u:<action>:<id>[:extra] - the member-management buttons."""
        action = parts[1] if len(parts) > 1 else "view"
        try:
            uid = int(parts[2]) if len(parts) > 2 else 0
        except ValueError:
            uid = 0
        if not uid:
            await self._answer_once(query, "bad member id", True)
            return

        def _detail() -> dict | None:
            rows, _ = self.storage.browse_users(0, 10000)
            for row in rows:
                if int(row["telegram_id"]) == uid:
                    with self.storage._lock:
                        raw = self.storage.conn.execute(
                            "SELECT phone, ban_reason, is_admin FROM users WHERE telegram_id=?",
                            (uid,)).fetchone()
                    if raw:
                        row["phone"] = raw["phone"] or ""
                        row["ban_reason"] = raw["ban_reason"] or ""
                        row["admin"] = bool(raw["is_admin"])
                    return row
            # banned members may be missing from users - show what we can
            for hit in self.storage.admin_search_users(str(uid)):
                if int(hit["telegram_id"]) == uid:
                    return hit
            return None

        def _render_detail() -> tuple[str, object] | None:
            row = _detail()
            if row is None:
                return None
            return (render.user_detail_view(row), render.user_detail_keyboard(row))

        async def _push(text: str, markup) -> None:
            if query.message:
                try:
                    await query.edit_message_text(
                        text, parse_mode="HTML", reply_markup=markup,
                        disable_web_page_preview=True)
                    return
                except Exception:  # noqa: BLE001 - fall through to a fresh message
                    pass
            await self.app.bot.send_message(chat.id, text, parse_mode="HTML",
                                            reply_markup=markup)

        if action == "view":
            detail = _render_detail()
            if detail is None:
                await self._answer_once(query, "member not found", True)
                return
            await self._answer_once(query)
            await _push(*detail)
            return

        if action == "ban":
            await self._answer_once(query)
            row = _detail()
            if row is None:
                return
            await _push(render.ban_confirm_view(row),
                        render.ban_confirm_keyboard(uid))
            return

        if action == "ban2":
            row = _detail() or {"telegram_id": uid, "label": str(uid), "username": ""}
            self.storage.ban_user(uid, reason="banned by admin",
                                  name=row.get("label", ""),
                                  username=row.get("username", ""),
                                  phone=row.get("phone", ""))
            with self.storage._lock:
                self.storage.conn.execute(
                    "UPDATE users SET active_until=? WHERE telegram_id=?",
                    (int(time.time()), uid))
                self.storage.conn.execute(
                    "DELETE FROM views WHERE telegram_id=?", (uid,))
                self.storage.conn.commit()
            self.trial_notified.discard(uid)
            await self._answer_once(query, "🚫 member banned for good", True)
            detail = _render_detail()
            await _push(f"🚫 <b>Member {uid} banned permanently.</b>\n"
                        "<i>Every linked number and re-attempt is refused now.</i>",
                        InlineKeyboardMarkup([[InlineKeyboardButton(
                            "🔙 Members", callback_data="users:0")]]))
            log.info("admin %s banned member %s", query.from_user.id if query.from_user else 0, uid)
            _ = detail
            return

        if action == "unban":
            ok = self.storage.unban(uid)
            await self._answer_once(query, "✅ unbanned" if ok else "no ban on record", True)
            detail = _render_detail()
            if detail:
                await _push(*detail)
            return

        if action == "del":
            # removal = the member can register fresh; confirm first
            row = _detail()
            if row is None:
                await self._answer_once(query, "member not found", True)
                return
            await self._answer_once(query)
            await _push(
                "🗑 <b>Remove this member?</b>\n" + render.RULE + "\n"
                f"<code>{uid}</code> · <b>{render.esc(row['label'])}</b>\n\n"
                "The whole profile, their views and trial history are deleted - "
                "they can /start again as a brand-new account.\n"
                "<i>Bans are kept: removal never un-bans anybody.</i>",
                InlineKeyboardMarkup([
                    [InlineKeyboardButton("🗑 Yes, remove", callback_data=f"u:del2:{uid}"),
                     InlineKeyboardButton("❌ Cancel", callback_data=f"u:view:{uid}")],
                ])
            )
            return

        if action == "del2":
            removed = self.storage.remove_user(uid)
            self.trial_notified.discard(uid)
            await self._answer_once(
                query, "🗑 removed - they may register fresh" if removed else "no such member", True)
            rows, total = self.storage.browse_users(0)
            await _push(render.users_view(rows, 0, total), render.users_keyboard(0, total))
            log.info("admin removed member %s (removed=%s)", uid, removed)
            return

        if action == "grant":
            try:
                days = int(parts[3]) if len(parts) > 3 else 7
            except ValueError:
                days = 7
            days = max(1, min(365, days))
            with self.storage._lock:
                exists = self.storage.conn.execute(
                    "SELECT 1 FROM users WHERE telegram_id=?", (uid,)).fetchone()
            if not exists:
                self.storage.update_user(uid, stage="active")
            new_until = self.storage.extend(uid, days)
            if new_until:
                with self.storage._lock:
                    self.storage.conn.execute(
                        "UPDATE users SET plan=CASE WHEN plan='trial' THEN plan ELSE 'paid' END,"
                        " stage='active' WHERE telegram_id=?", (uid,))
                    self.storage.conn.commit()
                stamp = datetime.fromtimestamp(new_until).strftime("%d %b %Y · %H:%M")
                note = f"➕ +{days}d granted · active until {stamp}"
                try:
                    await self.app.bot.send_message(
                        uid,
                        f"🎉 <b>Gifted access!</b> <b>+{days} days</b> added by the admin.\n"
                        f"Now valid until <b>{stamp}</b> 🎉",
                        parse_mode="HTML")
                except Exception:  # noqa: BLE001
                    pass
            else:
                note = "member not found"
            await self._answer_once(query, note, True)
            detail = _render_detail()
            if detail:
                await _push(*detail)
            return

        if action == "revoke":
            ok = self.storage.revoke_access(uid)
            await self._answer_once(
                query, "⏪ access revoked" if ok else "no such member", True)
            detail = _render_detail()
            if detail:
                await _push(*detail)
            return

        if action == "resettrial":
            ok = self.storage.reset_trial(uid)
            self.trial_notified.discard(uid)
            await self._answer_once(
                query, "🎟️ trial reset - they can claim a fresh one" if ok else "no such member",
                True)
            detail = _render_detail()
            if detail:
                await _push(*detail)
            return

        await self._answer_once(query, "unknown action", True)

    async def _decide_payment(self, query, user: User, decision: str, pid_raw: str) -> None:
        try:
            pid = int(pid_raw)
        except ValueError:
            await self._answer_once(query, "bad payment id", True)
            return
        approved = decision == "yes"
        row = self.storage.decide_payment(
            pid, "approved" if approved else "rejected", user.telegram_id)
        if row is None:
            await self._answer_once(query, "already decided", True)
            return
        if approved:
            key = self.storage.generate_key(
                user.telegram_id, telegram_id=row["telegram_id"],
                days=self.cfg.activation_days)
            try:
                await self.app.bot.send_message(
                    row["telegram_id"],
                    render.payment_approved(key, self.cfg.activation_days),
                    parse_mode="HTML",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("could not deliver key to %s: %s", row["telegram_id"], exc)
            note = f"✅ #{pid} approved · key issued"
        else:
            try:
                await self.app.bot.send_message(
                    row["telegram_id"], render.payment_rejected(pid), parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
            note = f"❌ #{pid} rejected"
        await self._answer_once(query, note)
        try:
            await query.message.edit_text(f"{note}\n<i>opened the queue again? use /report</i>",
                                          parse_mode="HTML")
        except Exception:  # noqa: BLE001
            pass
        log.info("payment %s %s by %s", pid, note, user.telegram_id)

    # ------------------------------------------------------------------ #
    # admin reports
    # ------------------------------------------------------------------ #
    def _log_tail(self, n: int = 6) -> list[str]:
        try:
            with open(self.cfg.log_file, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()
        except OSError:
            return []
        return [line.rstrip()[-115:] for line in lines[-n:]]

    def _report_text(self) -> str:
        rates = []
        for key in ORDER:
            score = self.storage.scorecard(key, 30)
            rates.append(
                f"• <b>{MODES[key].node_label()}</b> — size {score['size_rate']}% "
                f"({score['size_hits']}/{score['total']}) · streak "
                f"{score['streak_kind']}{score['streak_len']}"
            )
        return render.report_view(self.storage.business_stats(), self.orch.health(),
                                  rates, self._log_tail())

    async def cmd_report(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id,
                                       username=update.effective_user.username or "")
        if not user.is_admin:
            return
        await self._typing(update.effective_chat.id)
        await update.effective_message.reply_text(self._report_text(), parse_mode="HTML")

    async def cmd_keys(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id,
                                       username=update.effective_user.username or "")
        if not user.is_admin:
            return
        rows = self.storage.keys(12) if hasattr(self.storage, "keys") else []
        lines = ["🔑 <b>Recent activation keys</b>", render.RULE]
        if not rows:
            lines.append("<i>none yet — /genkey to mint one</i>")
        for row in rows:
            stamp = datetime.fromtimestamp(int(row["created_at"])).strftime("%d %b %H:%M")
            lines.append(
                f"<code>{render.esc(row['key'])}</code> · {render.esc(row['status'])} · {stamp}"
                + (f" → <code>{row['telegram_id']}</code>" if row["telegram_id"] else "")
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_genkey(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id,
                                       username=update.effective_user.username or "")
        if not user.is_admin:
            return
        key = self.storage.generate_key(user.telegram_id, days=self.cfg.activation_days)
        await update.effective_message.reply_text(
            f"🔑 New activation key ({self.cfg.activation_days} days):\n\n"
            f"<code>{key}</code>\n\n"
            "<i>Share it with a paying member; it works once.</i>",
            parse_mode="HTML")

    async def cmd_stats(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._require_member(update)
        if not user:
            await self._refuse(update, self.storage.get_user(update.effective_user.id))
            return
        if await self._locked(update, user):
            return
        mode = parse_mode_arg(" ".join(ctx.args)) or MODES[self.default_node(user.telegram_id)]
        state = self.engine.state(mode.key)
        await self._typing(update.effective_chat.id)
        window = analyzer.window_stats(
            self.storage.rounds(mode.key, self.cfg.analysis_window), self.cfg.analysis_window
        )
        await update.effective_message.reply_text(
            render.stats_view(user, mode.key, state, window), parse_mode="HTML"
        )

    async def cmd_last(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._require_member(update)
        if not user:
            return
        if await self._locked(update, user):
            return
        args = list(ctx.args)
        count = 10
        if args and args[0].isdigit():
            count = max(1, min(20, int(args.pop(0))))
        mode = parse_mode_arg(" ".join(args)) or MODES[self.default_node(user.telegram_id)]
        rounds = self.storage.rounds(mode.key, count)
        lines = [f"📜 <b>{mode.node_label()} · last {len(rounds)} rounds</b>", render.RULE]
        for rnd in rounds:
            pred = self.storage.get_prediction(mode.key, rnd.period)
            verdict = ""
            if pred and pred.evaluated_at:
                verdict = " · HIT ✅" if pred.size_hit else " · MISS ❌"
            lines.append(
                f"<code>{rnd.period}</code> → <b>{rnd.number}</b> "
                f"{size_for(rnd.number)} {render.colour_emoji(rnd.colour)} "
                f"{rnd.colour}{verdict}"
            )
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

    async def cmd_predict(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._require_member(update)
        if not user:
            return
        if await self._locked(update, user):
            return
        mode = parse_mode_arg(" ".join(ctx.args)) or MODES[self.default_node(user.telegram_id)]
        await self._show(update.effective_chat.id, user, "card", mode.key, new=True)

    async def cmd_status(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self._require_member(update)
        if not user:
            return
        health = self.orch.health()
        lines = ["📊 <b>Engine status</b>", render.RULE]
        for key in ORDER:
            state = self.engine.state(key)
            if not state.ready:
                lines.append(f"<b>{state.mode.node_label()}</b> — syncing…")
                continue
            pred = state.prediction
            lines.append(
                f"<b>{state.mode.node_label()}</b> <code>{state.issue}</code> "
                f"· {int(state.seconds_left())}s {state.status()} · "
                f"pick <b>{pred.number if pred else '—'}</b> · "
                f"{state.rounds_available} rounds · {state.scorecard.get('colour_rate', 0)}%"
            )
        lines += [
            render.RULE,
            f"🤖 chain primary <b>{health['primary']}</b> · calls {health['calls']} · "
            f"failovers {health['fallbacks']}" + (f" · missing {health['missing']}" if health['missing'] else ""),
            f"📸 snapshots {self.storage.snapshot_stats()['live']} live "
            f"({self.storage.snapshot_stats()['deleted']} auto-deleted)",
        ]
        await update.effective_message.reply_text("\n".join(lines), parse_mode="HTML")

    # ---------------- admin ------------------------------------------- #
    async def cmd_admin(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id)
        supplied = " ".join(ctx.args).strip()
        if user.is_admin or (supplied and supplied == self.admin_code()):
            if not user.is_admin:
                self.storage.update_user(user.telegram_id, is_admin=1)
                log.info("admin claimed by %s", user.telegram_id)
            chat = update.effective_chat
            await self._show(chat.id, self.storage.get_user(user.telegram_id),
                             "admin", None, new=True)
            return
        await update.effective_message.reply_text(
            "👑 Admin unlock: <code>/admin &lt;code&gt;</code>\n"
            "<i>The current code is printed in the service logs at boot "
            "(/logs in the preview panel).</i>",
            parse_mode="HTML",
        )

    async def cmd_health(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id)
        if not user.is_admin:
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        await update.effective_message.reply_text(
            render.health_view(self.orch.health()), parse_mode="HTML"
        )

    async def cmd_snap(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id)
        if not user.is_admin:
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        target = (ctx.args[0].lower() if ctx.args else "all")
        mode = parse_mode_arg(target)
        await self._send_snapshots(update.effective_chat.id, mode.key if mode else target)

    async def cmd_broadcast(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id)
        if not user.is_admin:
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        text = " ".join(ctx.args).strip()
        if not text:
            self.awaiting[user.telegram_id] = "broadcast"
            await update.effective_message.reply_text(
                render.broadcast_prompt(len(self.storage.users(active_only=True))),
                parse_mode="HTML",
            )
            return
        await self._broadcast(text)

    async def cmd_unban(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        user = self.storage.touch_user(update.effective_user.id)
        if not user.is_admin:
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        if not ctx.args or not ctx.args[0].lstrip("-").isdigit():
            await update.effective_message.reply_text("Usage: /unban &lt;telegram id&gt;",
                                                      parse_mode="HTML")
            return
        ok = self.storage.unban(int(ctx.args[0]))
        await update.effective_message.reply_text(
            "✅ unbanned" if ok else "no such account", parse_mode="HTML"
        )

    # ---------------- admin: member management commands ------------------ #
    def _admin_or_die(self, update: Update) -> User | None:
        user = self.storage.touch_user(update.effective_user.id,
                                       username=update.effective_user.username or "")
        if not user.is_admin:
            return None
        return user

    @staticmethod
    def _target_id(ctx: ContextTypes.DEFAULT_TYPE) -> int:
        return int(ctx.args[0]) if ctx.args and ctx.args[0].lstrip("-").isdigit() else 0

    async def cmd_users(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_or_die(update):
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        page = int(ctx.args[0]) if ctx.args and ctx.args[0].isdigit() else 0
        rows, total = self.storage.browse_users(page)
        await update.effective_message.reply_text(
            render.users_view(rows, page, total), parse_mode="HTML",
            reply_markup=render.users_keyboard(page, total))

    async def cmd_find(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_or_die(update):
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        term = " ".join(ctx.args).strip()
        if not term:
            self.awaiting[update.effective_user.id] = "finduser"
            await update.effective_message.reply_text(
                "🔍 Send a Telegram id, @username or name fragment.", parse_mode="HTML")
            return
        hits = self.storage.admin_search_users(term)
        if not hits:
            await update.effective_message.reply_text(
                f"🔍 No member matched <code>{render.esc(term[:40])}</code>",
                parse_mode="HTML")
            return
        await update.effective_message.reply_text(
            render.users_view(hits, 0, len(hits), per_page=len(hits) or 1),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton(f"🔎 {row['label'][:18]}",
                                      callback_data=f"u:view:{row['telegram_id']}")
                 for row in hits[:3]],
                [InlineKeyboardButton("🔙 All members", callback_data="users:0")],
            ]))

    async def cmd_remove(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_or_die(update):
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        uid = self._target_id(ctx)
        if not uid:
            await update.effective_message.reply_text("Usage: /remove &lt;telegram id&gt;",
                                                      parse_mode="HTML")
            return
        ok = self.storage.remove_user(uid)
        self.trial_notified.discard(uid)
        await update.effective_message.reply_text(
            "🗑 removed - they may /start fresh" if ok else "no such member",
            parse_mode="HTML")

    async def cmd_ban(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_or_die(update):
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        uid = self._target_id(ctx)
        if not uid:
            await update.effective_message.reply_text(
                "Usage: /ban &lt;telegram id&gt; [reason]", parse_mode="HTML")
            return
        reason = " ".join(ctx.args[1:]).strip() or "banned by admin"
        target = self.storage.get_user(uid)
        self.storage.ban_user(uid, reason=reason,
                              name=target.name if target else "",
                              username=target.username if target else "",
                              phone=target.phone if target else "")
        with self.storage._lock:
            self.storage.conn.execute(
                "UPDATE users SET active_until=? WHERE telegram_id=?",
                (int(time.time()), uid))
            self.storage.conn.execute("DELETE FROM views WHERE telegram_id=?", (uid,))
            self.storage.conn.commit()
        await update.effective_message.reply_text(
            f"🚫 <b>{uid}</b> banned forever · {render.esc(reason)}", parse_mode="HTML")

    async def cmd_grant(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_or_die(update):
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        uid = self._target_id(ctx)
        days = 7
        if len(ctx.args) > 1 and ctx.args[1].isdigit():
            days = max(1, min(365, int(ctx.args[1])))
        if not uid:
            await update.effective_message.reply_text(
                "Usage: /grant &lt;telegram id&gt; [days]", parse_mode="HTML")
            return
        with self.storage._lock:
            exists = self.storage.conn.execute(
                "SELECT 1 FROM users WHERE telegram_id=?", (uid,)).fetchone()
        if not exists:
            self.storage.update_user(uid, stage="active")
        new_until = self.storage.extend(uid, days)
        if new_until:
            with self.storage._lock:
                self.storage.conn.execute(
                    "UPDATE users SET plan=CASE WHEN plan='trial' THEN plan ELSE 'paid' END,"
                    " stage='active' WHERE telegram_id=?", (uid,))
                self.storage.conn.commit()
            stamp = datetime.fromtimestamp(new_until).strftime("%d %b %Y · %H:%M")
            await update.effective_message.reply_text(
                f"➕ <b>+{days}d</b> granted to <b>{uid}</b> · until {stamp}",
                parse_mode="HTML")
            try:
                if self.app is not None:
                    await self.app.bot.send_message(
                        uid,
                        f"🎉 <b>Gifted access!</b> <b>+{days} days</b> added by the admin.\n"
                        f"Now valid until <b>{stamp}</b> 🎉", parse_mode="HTML")
            except Exception:  # noqa: BLE001
                pass
        else:
            await update.effective_message.reply_text("no such member", parse_mode="HTML")

    async def cmd_revoke(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_or_die(update):
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        uid = self._target_id(ctx)
        if not uid:
            await update.effective_message.reply_text(
                "Usage: /revoke &lt;telegram id&gt;", parse_mode="HTML")
            return
        ok = self.storage.revoke_access(uid)
        await update.effective_message.reply_text(
            "⏪ access revoked" if ok else "no such member", parse_mode="HTML")

    async def cmd_resettrial(self, update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._admin_or_die(update):
            await update.effective_message.reply_text("👑 admins only", parse_mode="HTML")
            return
        uid = self._target_id(ctx)
        if not uid:
            await update.effective_message.reply_text(
                "Usage: /resettrial &lt;telegram id&gt;", parse_mode="HTML")
            return
        ok = self.storage.reset_trial(uid)
        self.trial_notified.discard(uid)
        await update.effective_message.reply_text(
            "🎟️ trial reset - they can claim a fresh one" if ok else "no such member",
            parse_mode="HTML")

    async def _broadcast(self, text: str) -> None:
        targets = self.storage.users(active_only=True)
        sent = failed = 0
        assert self.app is not None
        for target in targets:
            with self.storage._lock:
                row = self.storage.conn.execute(
                    "SELECT chat_id FROM views WHERE telegram_id=?",
                    (target.telegram_id,),
                ).fetchone()
            chat_id = row["chat_id"] if row else target.telegram_id
            try:
                await self.app.bot.send_message(chat_id, text)
                sent += 1
            except Exception:  # noqa: BLE001
                failed += 1
            await asyncio.sleep(0.05)
        log.info("broadcast sent=%s failed=%s", sent, failed)
        admin = [t for t in targets if t.is_admin]
        for target in admin:
            try:
                with self.storage._lock:
                    row = self.storage.conn.execute(
                        "SELECT chat_id FROM views WHERE telegram_id=?",
                        (target.telegram_id,),
                    ).fetchone()
                chat_id = row["chat_id"] if row else target.telegram_id
                await self.app.bot.send_message(
                    chat_id, f"📢 broadcast delivered to {sent} ({failed} failed)",
                    parse_mode="HTML",
                )
            except Exception:  # noqa: BLE001
                pass

    # ---------------- snapshots ---------------------------------------- #
    async def _admin_value(self, update: Update, user: User, text: str) -> bool:
        """Admin sends a value: node name / 'all' / a period -> snapshot images."""
        lowered = text.strip().lower()
        if lowered in ("all", "snapshot all"):
            await self._send_snapshots(update.effective_chat.id, "all")
            return True
        mode = parse_mode_arg(text)
        if mode:
            await self._send_snapshots(update.effective_chat.id, mode.key)
            return True
        if text.isdigit() and len(text) >= 12:
            found = None
            for key in ORDER:
                if self.storage.get_prediction(key, text) or any(
                        rnd.period == text for rnd in self.storage.rounds(key, 500)):
                    found = key
                    break
            if found:
                await self._send_snapshots(update.effective_chat.id, found)
            else:
                await update.effective_message.reply_text(
                    f"no stored round <code>{render.esc(text)}</code>", parse_mode="HTML"
                )
            return True
        if lowered in ("snapshot", "snap"):
            await self._send_snapshots(update.effective_chat.id, "all")
            return True
        return False

    async def _send_snapshots(self, chat_id: int, target: str) -> None:
        if self.app is None:
            return
        keys = list(MODES) if target == "all" else [target] if target in MODES else []
        if not keys:
            await self.app.bot.send_message(chat_id, "unknown node - try 30sec / 1min / 3min / 5min / all")
            return
        sent = 0
        for key in keys:
            state = self.engine.state(key)
            snapshot_id = await self.snapshots.send_signal(self.app.bot, chat_id, state)
            if snapshot_id:
                sent += 1
            await asyncio.sleep(0.3)
        await self.app.bot.send_message(
            chat_id,
            f"📸 {sent} snapshot image(s) sent · each deletes itself after "
            f"{self.cfg.snapshot_retention_hours}h",
            parse_mode="HTML",
        )

    # ------------------------------------------------------------------ #
    # background loops
    # ------------------------------------------------------------------ #
    async def view_loop(self) -> None:
        """Auto-updates every open desk message in place - cards, status and
        menu views all refresh themselves, no separate texts."""
        log.info("view_loop: started")
        await asyncio.sleep(2)
        while self.running:
            now = time.time()
            edits = 0
            try:
                rows = self.storage.views()
            except Exception:  # noqa: BLE001
                rows = []
            for row in rows:
                view = row["view"]
                if view not in ("card", "status", "menu"):
                    continue
                if view == "card" and not row["mode"]:
                    continue
                if edits >= 8:
                    break
                user = self.storage.get_user(row["telegram_id"])
                if not user or user.banned or user.stage != "active":
                    continue
                # locked/expired chats stop receiving card updates
                if not user.is_admin and not self.storage.is_active(user.telegram_id):
                    continue
                interval = max(2, int(user.settings.get("auto_refresh", 5)))
                if view == "menu":
                    interval = max(interval, 10)   # menus only tick every 10s
                if now - float(row["last_edit_at"] or 0) < interval:
                    continue
                key = row["mode"]
                if view == "card":
                    if key not in MODES:
                        continue
                    # trial members: the auto-updater spends one credit per new
                    # period it delivers (idempotent per period). Out of budget ->
                    # the card freezes and the upgrade notice goes out once.
                    if not await self._charge_trial(user, key):
                        continue
                else:
                    key = None
                text = self._render(user, view, key)
                if text == row["last_text"]:
                    # nothing new - stamp the time so we do not re-render every second
                    self.storage.set_view_text(user.telegram_id, row["chat_id"], text, now)
                    continue
                try:
                    await self.app.bot.edit_message_text(
                        chat_id=row["chat_id"], message_id=row["message_id"], text=text,
                        parse_mode="HTML", reply_markup=self._keyboard(user, view, key),
                        disable_web_page_preview=True,
                    )
                    self.storage.set_view_text(user.telegram_id, row["chat_id"], text, now)
                    edits += 1
                except RetryAfter as exc:
                    # Telegram told us the exact cool-down - honour it, then retry
                    await asyncio.sleep(min(int(exc.retry_after) + 1, 30))
                    continue
                except Exception as exc:  # noqa: BLE001
                    message = str(exc)
                    if "message is not modified" in message:
                        self.storage.set_view_text(user.telegram_id, row["chat_id"], text, now)
                    elif "message to edit not found" in message or \
                            "message can't be edited" in message or \
                            "message to delete not found" in message:
                        self.storage.drop_view(user.telegram_id, row["chat_id"])
                    elif "flood" in message.lower() or "retry after" in message.lower():
                        await asyncio.sleep(5)
                    else:
                        log.debug("card edit: %s", message)
                    continue
                await asyncio.sleep(0.5)
            await self._settle_notifications()
            await asyncio.sleep(1.0)

    async def _reopen_live_cards(self) -> None:
        """Warm-start the desk after boot: for every active member without an

        open card, send a fresh auto-updating card on their default node so the
        view loop has something to edit. This is what makes the desk update
        in place without the member having to tap a button."""
        await asyncio.sleep(4)
        opened = 0
        try:
            chats: dict[int, int] = {}
            for row in self.storage.views():
                if row["view"] == "card" and row["mode"]:
                    chats[row["telegram_id"]] = row["chat_id"]
            users = self.storage.users(active_only=True)
            for user in users:
                tid = user.telegram_id
                chat_id = chats.get(tid)
                key = self.default_node(tid)
                if chat_id is None:
                    # no card open yet -> send one, for the loop to keep editing
                    try:
                        sent = await self.app.bot.send_message(
                            chat_id=user.telegram_id, text="", parse_mode="HTML"
                        )
                        chat_id = sent.chat.id
                    except Exception:  # noqa: BLE001 - user blocked/bot kicked
                        continue
                await self._show(chat_id, user, "card", key, new=True)
                opened += 1
            if opened:
                log.info("reopen_live_cards: opened %d card(s)", opened)
        except Exception as exc:  # noqa: BLE001 - never take down the bot on warmup
            log.warning("reopen_live_cards: %s", exc)

    async def _settle_notifications(self) -> None:
        """Optional extra push (off by default) - the card already updates itself."""
        for key in ORDER:
            state = self.engine.state(key)
            if not state.ready or not state.last_settled:
                continue
            period = state.last_settled.period_label
            if self.seen_settles.get(key) == period:
                continue
            first = not self.seen_settles.get(key)
            self.seen_settles[key] = period
            if first or not self.cfg.notify_on_result:
                continue
            # BIG/SMALL is the desk's priority - the push judges the size call
            verdict = "HIT ✅" if state.last_settled.size_hit else "MISS ❌"
            text = (
                f"🔔 <b>{state.mode.node_label()}</b> <code>{period}</code> → "
                f"<b>{state.last_settled.actual_number}</b> "
                f"({size_for(state.last_settled.actual_number)}) · we called "
                f"<b>{size_for(state.last_settled.number)}</b> · <b>{verdict}</b>"
            )
            for user in self.storage.users(active_only=True):
                if not user.settings.get("push"):
                    continue
                with self.storage._lock:
                    row = self.storage.conn.execute(
                        "SELECT chat_id FROM views WHERE telegram_id=?",
                        (user.telegram_id,),
                    ).fetchone()
                if not row:
                    continue
                try:
                    await self.app.bot.send_message(row["chat_id"], text, parse_mode="HTML")
                except Exception:  # noqa: BLE001
                    pass
                await asyncio.sleep(0.1)

    async def snapshot_loop(self) -> None:
        """Sweeps expired snapshots (24h) + optional periodic admin photos."""
        log.info("snapshot_loop: started")
        await asyncio.sleep(5)
        last_auto = 0.0
        while self.running:
            try:
                await self.snapshots.sweep(self.app.bot)
            except Exception as exc:  # noqa: BLE001
                log.warning("snapshot sweep: %s", exc)

            admin_chats = [u for u in self.storage.users()
                           if u.is_admin and u.settings.get("auto_snapshots")]
            due = (time.time() - last_auto) >= self.cfg.snapshot_interval
            if admin_chats and due:
                last_auto = time.time()
                views = {row["telegram_id"]: row["chat_id"] for row in self.storage.views()}
                for admin in admin_chats:
                    chat_id = views.get(admin.telegram_id, admin.telegram_id)
                    for key in ORDER:
                        state = self.engine.state(key)
                        if not state.ready:
                            continue
                        try:
                            await self.snapshots.send_signal(self.app.bot, chat_id, state)
                        except Exception as exc:  # noqa: BLE001
                            log.debug("auto snapshot: %s", exc)
                        await asyncio.sleep(0.3)
            await asyncio.sleep(30)

    async def on_error(self, update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        log.error("handler error: %s", ctx.error, exc_info=ctx.error)


RULE = render.RULE
