"""Alerts: what the desk did or could not do, kept in a log and sent to Telegram.

Telegram is used because it reaches a phone without an app store, a paid
service or a browser left open. The bot token and chat id are set in Settings.
An alert is never the only record: everything also goes to the in-app log.
"""

from __future__ import annotations

import contextlib
import json
import logging
import queue
import threading
import time
from pathlib import Path

from . import market
from .settings import home

logger = logging.getLogger(__name__)

KEEP = 300
REPEAT_SECONDS = 600          # the same alert is not sent again within this time
LEVELS = ("info", "good", "warn", "bad")
ICON = {"info": "ℹ️", "good": "✅", "warn": "⚠️", "bad": "🛑"}


class Notifier:
    def __init__(self, settings=None, path: Path | None = None, send=None, now=market.now_ist):
        self.settings, self._now = settings, now
        self.path = path or home() / "alerts.jsonl"
        self._send = send                      # (token, chat_id, text) -> None; replaces the Telegram call
        self._lock = threading.Lock()
        self._recent: dict[tuple, float] = {}
        self._queue: queue.Queue = queue.Queue(maxsize=200)
        self._worker: threading.Thread | None = None
        self.last_error: str | None = None

    def _telegram(self) -> tuple[str, str] | None:
        return self.telegram_target()

    def telegram_target(self, require_alerts: bool = True) -> tuple[str, str] | None:
        """(bot token, chat id) to send to. ``require_alerts`` False: also when "Send alerts" is off (reminders)."""
        if self.settings is None:
            return None
        cfg = self.settings.all()
        token, chat = cfg["api_keys"].get("TELEGRAM_BOT_TOKEN", ""), cfg["telegram_chat_id"]
        return (token, chat) if (cfg["alerts_enabled"] or not require_alerts) and token and chat else None

    def record(self, title: str, body: str = "", level: str = "info") -> None:
        """Add to the in-app alert log only."""
        entry = {"time_ist": self._now().strftime("%Y-%m-%d %H:%M:%S"), "level": level if level in LEVELS else "info",
                 "title": title, "body": body}
        with self._lock, contextlib.suppress(OSError), open(self.path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")

    def deliver(self, target: tuple[str, str], text: str) -> None:
        """Send one Telegram message now (sets ``last_error`` when it fails)."""
        self._deliver(target, text)

    def notify(self, title: str, body: str = "", level: str = "info", key: str | None = None) -> bool:
        """Record an alert and queue it for Telegram. Returns False when it repeats a recent one."""
        level = level if level in LEVELS else "info"
        mark = (key or title, body)
        with self._lock:
            now = time.time()
            self._recent = {k: t for k, t in self._recent.items() if now - t < REPEAT_SECONDS}
            if mark in self._recent:
                return False
            self._recent[mark] = now
            entry = {"time_ist": self._now().strftime("%Y-%m-%d %H:%M:%S"), "level": level, "title": title, "body": body}
            with contextlib.suppress(OSError), open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
        target = self._telegram()
        if target:
            text = f"{ICON[level]} {title}" + (f"\n{body}" if body else "")
            if self._send is not None:
                self._deliver(target, text)
            else:
                with contextlib.suppress(queue.Full):
                    self._queue.put_nowait((target, text))
                self._ensure_worker()
        return True

    def _ensure_worker(self) -> None:
        if self._worker and self._worker.is_alive():
            return

        def run():
            while True:
                try:
                    target, text = self._queue.get(timeout=60)
                except queue.Empty:
                    return
                self._deliver(target, text)
                time.sleep(1.1)                 # Telegram allows about one message a second to a chat

        self._worker = threading.Thread(target=run, name="desk-alerts", daemon=True)
        self._worker.start()

    def _deliver(self, target: tuple[str, str], text: str) -> None:
        token, chat = target
        try:
            if self._send is not None:
                self._send(token, chat, text)
            else:
                import httpx

                response = httpx.post(f"https://api.telegram.org/bot{token}/sendMessage",
                                      json={"chat_id": chat, "text": text[:3900], "disable_web_page_preview": True},
                                      timeout=15)
                if response.status_code != 200:
                    detail = response.json().get("description", response.text[:120]) if response.content else ""
                    raise RuntimeError(f"Telegram answered {response.status_code}: {detail}")
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 - an alert that cannot be sent must not break trading
            # The token is part of the URL; never let it reach a log or the screen.
            self.last_error = str(exc).replace(token, "<token>")[:200]
            logger.warning("alert not delivered: %s", self.last_error)

    def test(self) -> dict:
        """Send a test message now and say what happened."""
        target = self._telegram()
        if not target:
            return {"ok": False, "error": "Enter the bot token and chat id, tick “Send alerts to Telegram”, and save."}
        self._deliver(target, f"{ICON['good']} TradeSetu test alert\nAlerts from your desk will arrive here.")
        return {"ok": self.last_error is None, "error": self.last_error}

    def recent(self, limit: int = 100) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        if len(lines) > 2 * KEEP:
            with contextlib.suppress(OSError):
                self.path.write_text("\n".join(lines[-KEEP:]) + "\n", encoding="utf-8")
        out = []
        for line in reversed(lines[-limit:]):
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
        return out


class TelegramLink:
    """Link a Telegram chat to the desk without looking up a chat id: open the bot, press Start, done.

    ``begin`` makes a one-time code and a t.me link that opens the bot with it; when the user presses Start,
    Telegram sends the bot "/start <code>", and ``check`` finds that message among the bot's updates and
    takes its chat id. The code lasts 10 minutes. ``call(token, method, params)`` replaces the Telegram API
    (tests).
    """

    LIFETIME = 600

    def __init__(self, call=None, wall=time.time):
        self._call, self._wall = call, wall
        self._pending: dict | None = None

    def _api(self, token: str, method: str, params: dict | None = None):
        if self._call is not None:
            return self._call(token, method, params or {})
        import httpx

        try:
            response = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=params or {}, timeout=15)
            answer = response.json() if response.content else {}
        except Exception as exc:  # noqa: BLE001 - reported to the user, without the token
            raise ValueError("Telegram is not answering: " + str(exc).replace(token, "<token>")[:160]) from None
        if not answer.get("ok"):
            code, detail = response.status_code, str(answer.get("description") or "")[:160]
            if code == 401:
                raise ValueError("Telegram does not accept this bot token. Copy it again from @BotFather and Save.")
            if code == 409:
                raise ValueError("This bot sends its messages to a webhook, so the desk cannot read them. Enter "
                                 "the chat id by hand instead.")
            raise ValueError(f"Telegram answered {code}: {detail}")
        return answer.get("result")

    def begin(self, token: str) -> dict:
        if not token:
            raise ValueError("Save the bot token from @BotFather first, then press Link.")
        bot = self._api(token, "getMe") or {}
        username = bot.get("username")
        if not username:
            raise ValueError("Telegram did not say what this bot is called. Check the bot token.")
        import secrets

        code = "desk" + secrets.token_hex(8)
        self._pending = {"code": code, "until": self._wall() + self.LIFETIME}
        return {"url": f"https://t.me/{username}?start={code}", "bot": "@" + username, "code": code}

    def check(self, token: str) -> dict:
        """{"linked": True, "chat_id", "name"} once Start was pressed; {"linked": False} until then."""
        pending = self._pending
        if not pending or self._wall() > pending["until"]:
            self._pending = None
            raise ValueError("The link has expired. Press “Link my Telegram” again.")
        updates = self._api(token, "getUpdates", {"allowed_updates": ["message"], "limit": 100}) or []
        for update in reversed(updates):
            message = update.get("message") or {}
            if str(message.get("text") or "").strip() == f"/start {pending['code']}":
                chat = message.get("chat") or {}
                if chat.get("id") is None:
                    continue
                self._pending = None
                # Mark these updates read, so the code is not found again.
                with contextlib.suppress(Exception):
                    self._api(token, "getUpdates", {"offset": int(update["update_id"]) + 1, "limit": 1})
                name = " ".join(x for x in (chat.get("first_name"), chat.get("last_name")) if x) \
                    or chat.get("title") or chat.get("username") or ""
                return {"linked": True, "chat_id": str(chat["id"]), "name": name}
        return {"linked": False}
