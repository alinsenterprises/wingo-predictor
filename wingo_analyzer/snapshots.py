"""Snapshot images: rendered signal cards and result boards, delivered to the
admin chat and deleted - message *and* file - after ``SNAPSHOT_RETENTION_HOURS``
(default 24h) by the sweeper.
"""
from __future__ import annotations

import glob
import io
import logging
import os
import random
import time
from datetime import datetime
from urllib.parse import quote

import qrcode as qrc
from PIL import Image, ImageDraw, ImageFont

from .config import Config
from .engine import ModeState
from .modes import COLOUR_HEX, MODES, colours_for, size_for
from .storage import Storage

log = logging.getLogger("wingo.snapshots")

W, H = 900, 560
BG_TOP = (9, 13, 28)
BG_BOTTOM = (19, 26, 51)
PANEL = (16, 23, 48)
PANEL_EDGE = (40, 54, 96)
TEXT = (233, 240, 255)
MUTED = (138, 152, 184)
GREEN = (53, 208, 165)
RED = (255, 92, 108)
VIOLET = (160, 107, 255)

_FONT_DIRS = ["/usr/share/fonts/truetype/liberation", "/usr/share/fonts/truetype/dejavu",
              "/usr/share/fonts/truetype/freefont", "/usr/share/fonts/truetype/noto"]


def _pick(*names: str) -> str | None:
    for directory in _FONT_DIRS:
        for name in names:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                return path
    for name in names:
        hits = glob.glob(f"/usr/share/fonts/**/{name}", recursive=True)
        if hits:
            return hits[0]
    return None


_REGULAR = _pick("LiberationSans-Regular.ttf", "DejaVuSans.ttf", "FreeSans.ttf")
_BOLD = _pick("LiberationSans-Bold.ttf", "DejaVuSans-Bold.ttf", "FreeSansBold.ttf")


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    path = _BOLD if bold else _REGULAR
    try:
        if path:
            return ImageFont.truetype(path, size)
        return ImageFont.load_default()
    except OSError:
        return ImageFont.load_default()


def hex_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    try:
        return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]
    except (ValueError, IndexError):
        return (110, 168, 255)


def gradient(width: int, height: int) -> Image.Image:
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for y in range(height):
        t = y / max(1, height - 1)
        colour = tuple(int(BG_TOP[i] + (BG_BOTTOM[i] - BG_TOP[i]) * t) for i in range(3))
        draw.line([(0, y), (width, y)], fill=colour)
    return image


def wrap(draw: ImageDraw.ImageDraw, text: str, fnt, max_width: int, limit: int = 2) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        trial = f"{current} {word}".strip()
        if draw.textlength(trial, font=fnt) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= limit:
                break
    if current and len(lines) < limit:
        lines.append(current)
    if len(lines) == limit and len(words) > 1:
        tail = lines[-1]
        while draw.textlength(tail + "…", font=fnt) > max_width and len(tail) > 4:
            tail = tail[:-1]
        lines[-1] = tail.rstrip() + "…"
    return lines


def _pill(draw, box, label, colour, fnt, fill=(24, 33, 66)):
    draw.rounded_rectangle(box, radius=(box[3] - box[1]) // 2, fill=fill)
    cx = (box[0] + box[2]) / 2
    cy = (box[1] + box[3]) / 2
    draw.text((cx, cy), label, font=fnt, fill=colour, anchor="mm")


def _header(draw, title: str, subtitle: str, accent, status: str):
    draw.rounded_rectangle((20, 18, W - 20, 96), radius=16, fill=PANEL, outline=PANEL_EDGE)
    draw.text((40, 34), title, font=font(25, True), fill=TEXT)
    draw.text((40, 66), subtitle, font=font(16), fill=accent)
    pill_w = int(draw.textlength(status, font=font(15, True))) + 34
    _pill(draw, (W - 40 - pill_w, 34, W - 40, 68), status, TEXT, font(15, True))


def _label(draw, xy, text, colour=MUTED, size=13):
    draw.text(xy, text.upper(), font=font(size, True), fill=colour)


# --------------------------------------------------------------------------- #
def render_signal(state: ModeState, analyst_note: str = "", provider: str = "") -> bytes:
    """One card: prediction + periods + analyst + streak."""
    mode = state.mode
    accent = hex_rgb(mode.accent)
    image = gradient(W, H)
    draw = ImageDraw.Draw(image)

    status = state.status()
    status_colour = {"LIVE": GREEN, "SETTLING": (255, 176, 32), "SETTLED": (110, 168, 255),
                     "SYNCING": MUTED}.get(status, TEXT)
    _header(draw, "WINGO NEXUS AI",
            f"{mode.node_label()} · WINGO {mode.label} · analyst {mode.ai}",
            accent, status)

    # ---- left panel: prediction ---------------------------------------- #
    draw.rounded_rectangle((20, 112, 442, 330), radius=16, fill=PANEL, outline=PANEL_EDGE)
    _label(draw, (40, 130), "prediction")
    pred = state.prediction
    if pred:
        colour_hex = COLOUR_HEX.get(pred.colours[0], mode.accent)
        circle = (56, 168, 196, 308)
        draw.ellipse(circle, fill=hex_rgb(colour_hex))
        draw.ellipse((66, 178, 186, 298), outline=(255, 255, 255, 60), width=2)
        draw.text((126, 238), str(pred.number), font=font(76, True), fill=(10, 14, 30), anchor="mm")

        rows = [
            ("colour", " / ".join(c.upper() for c in pred.colours)),
            ("size", pred.size),
            ("value", pred.parity),
            ("conviction", f"{pred.conviction}%"),
        ]
        y = 176
        for label, value in rows:
            _label(draw, (222, y), label)
            draw.text((222, y + 18), value, font=font(21, True), fill=TEXT)
            y += 40
        bar = (222, 316, 422, 322)
        draw.rounded_rectangle(bar, radius=3, fill=(34, 44, 82))
        filled = int(200 * (pred.conviction / 100.0))
        if filled > 6:
            draw.rounded_rectangle((222, 316, 222 + filled, 322), radius=3, fill=accent)
    else:
        draw.text((126, 238), "?", font=font(70, True), fill=MUTED, anchor="mm")
        draw.text((222, 210), "model warming up", font=font(20), fill=MUTED)

    # ---- right panel: periods ------------------------------------------ #
    draw.rounded_rectangle((462, 112, W - 20, 330), radius=16, fill=PANEL, outline=PANEL_EDGE)
    _label(draw, (482, 130), "last period")
    if state.last_round:
        rnd = state.last_round
        draw.text((482, 150), rnd.period, font=font(24, True), fill=TEXT)
        verdict = ""
        if state.last_settled and state.last_settled.period_label == rnd.period:
            # BIG/SMALL decides the verdict - the desk's priority
            verdict = "WIN" if state.last_settled.size_hit else "LOSS"
        result_colour = hex_rgb(COLOUR_HEX.get(rnd.colour.split(",")[0], "#6ea8ff"))
        draw.ellipse((482, 186, 532, 236), fill=result_colour)
        draw.text((507, 211), str(rnd.number), font=font(26, True), fill=(10, 14, 30), anchor="mm")
        draw.text((544, 190), " / ".join(c.upper() for c in colours_for(rnd.number)),
                  font=font(17, True), fill=TEXT)
        draw.text((544, 212), size_for(rnd.number), font=font(16), fill=MUTED)
        if verdict:
            _pill(draw, (760, 190, W - 40, 226), verdict,
                  GREEN if verdict == "WIN" else RED, font(15, True))
    else:
        draw.text((482, 170), "first sync in progress…", font=font(18), fill=MUTED)

    draw.line((482, 252, W - 40, 252), fill=PANEL_EDGE, width=1)
    _label(draw, (482, 264), "new period")
    if state.issue:
        draw.text((482, 284), state.issue, font=font(24, True), fill=TEXT)
        left = int(state.seconds_left())
        draw.text((W - 40, 296), f"{left // 60:02d}:{left % 60:02d} left",
                  font=font(22, True), fill=status_colour, anchor="ra")

    # ---- analyst strip --------------------------------------------------- #
    draw.rounded_rectangle((20, 346, W - 20, 470), radius=16, fill=PANEL, outline=PANEL_EDGE)
    _label(draw, (40, 362), "analyst")
    score = state.scorecard or {}
    head = f"{mode.ai} · via {provider or 'the chain'}"
    draw.text((40, 382), head, font=font(20, True), fill=accent)
    streak = f"{score.get('streak_kind', '')}{score.get('streak_len', 0)}"
    stats = (f"size streak {streak} · size hit {score.get('size_rate', 0)}% "
             f"· colour {score.get('colour_rate', 0)}% "
             f"· exact {score.get('exact_rate', 0)}% · {score.get('total', 0)} calls")
    draw.text((W - 40, 392), stats, font=font(16), fill=MUTED, anchor="ra")
    if analyst_note:
        for index, line in enumerate(wrap(draw, analyst_note, font(17), W - 110, limit=2)):
            draw.text((40, 418 + index * 24), line, font=font(17), fill=TEXT)

    # ---- footer ---------------------------------------------------------- #
    stamp = datetime.now().strftime("%d %b %Y · %H:%M:%S")
    draw.text((20, 500), f"snapshot {stamp}", font=font(15), fill=MUTED)
    draw.text((W - 20, 500), "auto-deletes after 24h", font=font(15, True), fill=MUTED, anchor="ra")
    draw.text((20, 524),
              f"{state.rounds_available} rounds stored · statistical model, "
              f"every round is independent · 18+ only · made by alins",
              font=font(14), fill=(96, 110, 142))
    draw.line((20, 490, W - 20, 490), fill=PANEL_EDGE, width=1)

    return _to_png(image)


def render_board(key: str, rows: list[dict], title: str = "") -> bytes:
    """Result board: the last rounds with our verdict on each."""
    mode = MODES[key]
    accent = hex_rgb(mode.accent)
    image = gradient(W, H)
    draw = ImageDraw.Draw(image)
    _header(draw, "RESULT BOARD",
            f"{mode.node_label()} · WINGO {mode.label} · last {len(rows)} rounds",
            accent, title or "LIVE")

    columns, cell_w, cell_h, gap = 5, 164, 96, 12
    x0, y0 = 24, 118
    for index, row in enumerate(rows[:15]):
        col, line = index % columns, index // columns
        x = x0 + col * (cell_w + gap)
        y = y0 + line * (cell_h + gap)
        draw.rounded_rectangle((x, y, x + cell_w, y + cell_h), radius=12,
                               fill=PANEL, outline=PANEL_EDGE)
        period = str(row.get("period", ""))
        draw.text((x + 12, y + 10), "…" + period[-5:], font=font(13), fill=MUTED)
        colour_name = (row.get("colour") or "").split(",")[0]
        circle = (x + 12, y + 34, x + 66, y + 88)
        draw.ellipse(circle, fill=hex_rgb(COLOUR_HEX.get(colour_name, "#6ea8ff")))
        draw.text((x + 39, y + 61), str(row.get("number", "?")),
                  font=font(26, True), fill=(10, 14, 30), anchor="mm")
        draw.text((x + 76, y + 40), size_for(int(row.get("number", 0))),
                  font=font(15, True), fill=TEXT)
        outcome = row.get("outcome")
        if outcome is True:
            draw.text((x + 76, y + 62), "HIT ✚", font=font(15, True), fill=GREEN)
        elif outcome is False:
            draw.text((x + 76, y + 62), "MISS ✖", font=font(15, True), fill=RED)
        else:
            draw.text((x + 76, y + 62), "—", font=font(15), fill=MUTED)

    draw.line((20, 500, W - 20, 500), fill=PANEL_EDGE, width=1)
    stamp = datetime.now().strftime("%d %b %Y · %H:%M:%S")
    draw.text((20, 516), f"snapshot {stamp} · made by alins", font=font(15), fill=MUTED)
    draw.text((W - 20, 516), "auto-deletes after 24h", font=font(15, True), fill=MUTED, anchor="ra")
    return _to_png(image)


def _to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# chat images: hero banner (welcome) + UPI QR (payment)
# --------------------------------------------------------------------------- #
HERO_W, HERO_H = 900, 440
_CONFETTI = [(53, 208, 165), (255, 92, 108), (160, 107, 255), (255, 205, 86),
             (110, 168, 255), (255, 158, 66)]


def hero_banner(title: str = "WINGO NEXUS AI",
                tagline: str = "four nodes · two analysts · one signal desk") -> bytes:
    """Celebratory welcome banner: gradient, confetti, glow - sent on /start."""
    image = gradient(HERO_W, HERO_H)
    draw = ImageDraw.Draw(image)
    rng = random.Random()

    # confetti burst - decorative only, never touches predictions
    for _ in range(140):
        x = rng.randint(0, HERO_W - 10)
        y = rng.randint(0, HERO_H - 10)
        size = rng.randint(4, 11)
        colour = rng.choice(_CONFETTI)
        if rng.random() < 0.5:
            draw.ellipse((x, y, x + size, y + size), fill=colour)
        else:
            draw.rectangle((x, y, x + size, y + size // 2), fill=colour)

    # centre panel so the words stay readable over the confetti
    pad = 60
    draw.rounded_rectangle((pad, 96, HERO_W - pad, HERO_H - 96), radius=26,
                           fill=(11, 16, 36), outline=(64, 84, 150), width=2)
    cx = HERO_W // 2
    draw.text((cx, 150), "WELCOME TO", font=font(24, True), fill=MUTED, anchor="mm")
    draw.text((cx, 205), title, font=font(56, True), fill=TEXT, anchor="mm")
    # accent underline
    draw.rounded_rectangle((cx - 150, 240, cx + 150, 248), radius=4, fill=GREEN)
    draw.text((cx, 285), tagline, font=font(21), fill=GREEN, anchor="mm")
    draw.text((cx, 330), "🎯 predictions · 🤖 AI reads · 🚦 HOLD system · 🔥 streaks",
              font=font(18), fill=TEXT, anchor="mm")
    draw.text((cx, 372), "18+ only · statistics, not guarantees",
              font=font(15), fill=MUTED, anchor="mm")
    draw.text((pad + 16, HERO_H - 34), "made by alins", font=font(15, True), fill=VIOLET)
    draw.text((HERO_W - pad - 16, HERO_H - 34), "t.me/@dmfirstauto_bot",
              font=font(15), fill=MUTED, anchor="ra")
    return _to_png(image)


QR_W, QR_H = 900, 980


def upi_qr(upi_id: str, amount: int, payee: str = "WINGO NEXUS AI") -> bytes:
    """A real, scannable UPI QR for the activation payment, styled to match."""
    uri = (f"upi://pay?pa={quote(upi_id)}&pn={quote(payee)}"
           f"&am={amount}.00&cu=INR&tn={quote('WINGO activation')}")
    builder = qrc.QRCode(error_correction=qrc.constants.ERROR_CORRECT_M,
                         box_size=12, border=2)
    builder.add_data(uri)
    builder.make(fit=True)
    qr_image = builder.make_image(fill_color=(9, 13, 28),
                                  back_color="white").convert("RGB")

    image = gradient(QR_W, QR_H)
    draw = ImageDraw.Draw(image)
    cx = QR_W // 2

    draw.rounded_rectangle((50, 34, QR_W - 50, 150), radius=22,
                           fill=PANEL, outline=PANEL_EDGE)
    draw.text((cx, 74), "💳  PAY  ₹%d" % amount, font=font(44, True),
              fill=GREEN, anchor="mm")
    draw.text((cx, 124), "scan with GPay · PhonePe · Paytm · any UPI app",
              font=font(19), fill=MUTED, anchor="mm")

    # white plate + QR
    qw, qh = qr_image.size
    scale = min(640 / qw, 640 / qh, 1.0)
    qr_image = qr_image.resize((int(qw * scale), int(qh * scale)), Image.LANCZOS)
    qw, qh = qr_image.size
    px, py = cx - qw // 2, 200
    draw.rounded_rectangle((px - 26, py - 26, px + qw + 26, py + qh + 26),
                           radius=24, fill=(255, 255, 255))
    image.paste(qr_image, (px, py))

    bottom = py + qh + 76
    draw.text((cx, bottom), f"{upi_id}", font=font(26, True), fill=TEXT, anchor="mm")
    draw.text((cx, bottom + 44),
              "after paying, tap  📨 I've paid  below and send your",
              font=font(19), fill=MUTED, anchor="mm")
    draw.text((cx, bottom + 76),
              "UPI reference or payment screenshot in this chat",
              font=font(19), fill=MUTED, anchor="mm")
    draw.text((cx, QR_H - 40), "made by alins · secure manual verification",
              font=font(16), fill=VIOLET, anchor="mm")
    return _to_png(image)


def render_welcome(name: str = "", active: bool = False, trial_left: str = "") -> bytes:
    """The /start banner: one celebratory image, no data, no files kept."""
    image = gradient(W, 420)
    draw = ImageDraw.Draw(image)

    # confetti: deterministic spread so the banner never flickers between sends
    palette = [(53, 208, 165), (255, 92, 108), (160, 107, 255),
               (255, 205, 82), (110, 168, 255)]
    seed = sum(ord(c) for c in name) or 7
    for i in range(60):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        x = 24 + seed % (W - 48)
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        y = 16 + seed % 388
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        r = 3 + seed % 5
        colour = palette[i % len(palette)]
        if i % 3 == 0:
            draw.ellipse((x, y, x + r * 2, y + r * 2), fill=colour)
        elif i % 3 == 1:
            draw.rectangle((x, y, x + r + 3, y + r), fill=colour)
        else:
            draw.polygon([(x, y), (x + r + 2, y + r), (x - r, y + r + 2)], fill=colour)

    # centre plate
    draw.rounded_rectangle((50, 84, W - 50, 336), radius=24,
                           fill=(13, 18, 38), outline=(46, 62, 112), width=2)
    draw.text((W / 2, 130), "WINGO NEXUS AI", font=font(42, True),
              fill=(233, 240, 255), anchor="mm")
    draw.text((W / 2, 178), "⚡ four live nodes · dual analyst desk ⚡",
              font=font(19), fill=(53, 208, 165), anchor="mm")
    if name:
        draw.text((W / 2, 224), f"Hey {name[:22]}, the desk is open 🚀",
                  font=font(24, True), fill=(255, 205, 82), anchor="mm")
    sub = "ROB reads the window · ROD reads the risk"
    draw.text((W / 2, 268), sub, font=font(17), fill=(138, 152, 184), anchor="mm")
    state = "✅ full access unlocked" if active else (
        f"🎟️ {trial_left}" if trial_left else "🔒 pick a way in below")
    badge_font = font(16, True)
    bw = int(draw.textlength(state, font=badge_font)) + 44
    _pill(draw, ((W - bw) / 2, 296, (W + bw) / 2, 330), state,
          (10, 14, 30) if active else (233, 240, 255), badge_font,
          fill=(53, 208, 165) if active else (30, 40, 78))
    draw.text((W / 2, 384), "tap the buttons below · cards update themselves",
              font=font(15), fill=(138, 152, 184), anchor="mm")
    return _to_png(image)


# --------------------------------------------------------------------------- #
class SnapshotManager:
    def __init__(self, cfg: Config, storage: Storage):
        self.cfg = cfg
        self.storage = storage

    # ---------------- files --------------------------------------------- #
    def save(self, mode_key: str, kind: str, data: bytes) -> str:
        day = datetime.now().strftime("%Y-%m-%d")
        stamp = datetime.now().strftime("%H-%M-%S")
        directory = os.path.join(self.cfg.snapshot_dir, mode_key or "global", day)
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, f"{stamp}_{kind}.png")
        counter = 1
        while os.path.exists(path):
            path = os.path.join(directory, f"{stamp}_{kind}_{counter}.png")
            counter += 1
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def render(self, state: ModeState) -> tuple[bytes, str]:
        return (render_signal(state, (state.prediction.ai_comment if state.prediction else ""),
                              state.prediction.ai_provider if state.prediction else ""), "signal")

    # ---------------- delivery ------------------------------------------- #
    async def send_signal(self, bot, chat_id: int, state: ModeState) -> str | None:
        data, kind = self.render(state)
        return await self._deliver(bot, chat_id, state.mode.key, kind, data,
                                   self._signal_caption(state))

    async def send_board(self, bot, chat_id: int, key: str, rows: list[dict]) -> str | None:
        data = render_board(key, rows)
        caption = (
            f"🗂 <b>{MODES[key].node_label()} result board</b>\n"
            f"last {len(rows)} rounds with our verdicts · "
            f"image deletes itself in {self.cfg.snapshot_retention_hours}h"
        )
        return await self._deliver(bot, chat_id, key, "board", data, caption)

    async def _deliver(self, bot, chat_id: int, mode_key: str, kind: str,
                       data: bytes, caption: str) -> str | None:
        path = self.save(mode_key, kind, data)
        snapshot_id = self.storage.add_snapshot(
            mode=mode_key, kind=kind, path=path, chat_id=None, message_id=None,
            caption=caption, retention_hours=self.cfg.snapshot_retention_hours,
        )
        try:
            from telegram import InputMediaPhoto  # noqa: F401  (import check only)
            photo = io.BytesIO(data)
            photo.name = os.path.basename(path)
            message = await bot.send_photo(
                chat_id=chat_id, photo=photo, caption=caption, parse_mode="HTML",
            )
        except Exception as exc:  # noqa: BLE001 - keep the file, report the miss
            log.warning("snapshot delivery failed: %s", exc)
            return None
        with self.storage._lock:  # attach the delivery target to the row
            self.storage.conn.execute(
                "UPDATE snapshots SET chat_id=?, message_id=? WHERE id=?",
                (chat_id, message.message_id, snapshot_id),
            )
            self.storage.conn.commit()
        return snapshot_id

    @staticmethod
    def _signal_caption(state: ModeState) -> str:
        pred = state.prediction
        pick = f"{pred.number} ({' / '.join(pred.colours)}, {pred.size})" if pred else "—"
        streak = state.scorecard or {}
        return (
            f"📸 <b>{state.mode.node_label()} · WINGO {state.mode.label}</b>\n"
            f"period <code>{state.issue or '—'}</code> · pick <b>{pick}</b> · "
            f"streak {streak.get('streak_kind', '')}{streak.get('streak_len', 0)}\n"
            f"⏳ deletes itself in {state.mode and ''}{24}h with the message"
        ).replace(f"{state.mode and ''}{24}h", "24h")

    # ---------------- retention ------------------------------------------ #
    async def sweep(self, bot) -> int:
        """Delete every snapshot that has outlived its 24 hours: message + file."""
        removed = 0
        for row in self.storage.expired_snapshots():
            if row["chat_id"] and row["message_id"]:
                try:
                    await bot.delete_message(chat_id=row["chat_id"], message_id=row["message_id"])
                except Exception as exc:  # noqa: BLE001 - already gone is fine
                    log.debug("snapshot message delete: %s", exc)
            try:
                if row["path"] and os.path.exists(row["path"]):
                    os.remove(row["path"])
            except OSError as exc:
                log.warning("cannot remove %s: %s", row["path"], exc)
            self.storage.mark_snapshot_deleted(row["id"])
            removed += 1
        if removed:
            log.info("retention: deleted %s snapshot(s) past %sh",
                     removed, self.cfg.snapshot_retention_hours)
        return removed

    def purge_boot(self, bot=None) -> None:
        """Anything that expired while the process was down goes now."""
        now = int(time.time())
        with self.storage._lock:
            rows = self.storage.conn.execute(
                "SELECT * FROM snapshots WHERE deleted_at IS NULL AND expires_at<=?", (now,)
            ).fetchall()
        for row in rows:
            try:
                if row["path"] and os.path.exists(row["path"]):
                    os.remove(row["path"])
            except OSError:
                pass
            self.storage.mark_snapshot_deleted(row["id"])
