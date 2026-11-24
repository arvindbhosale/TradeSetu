"""The morning Kite login reminder: a Telegram message at 08:30 IST on NSE trading days.

Kite logins end at 6 AM every day and Zerodha's terms rule out logging in for the user, so the desk reminds
instead. The message goes to the Telegram chat linked in Settings, only on a trading day (not a weekend, a
holiday in Settings or a holiday on NSE's own list) and only when the desk is not already logged in to Kite
that day. It carries a link that opens Zerodha's login page for this desk.

It runs on the desk's own clock (the algo tick), so it works wherever the desk runs: the server, and the
desktop app while it is open. Sent or skipped, it is decided once a day and remembered across restarts.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

from . import market
from .kite import KiteError
from .settings import home

logger = logging.getLogger(__name__)

WINDOW_MINUTES = 45            # a desk that starts late still reminds, up to this long after the set time
HOLIDAYS_MAX_AGE = 7 * 86400   # NSE's holiday list is fetched again after this long


def kite_signed_in(kite) -> bool | None:
    """True when today's Kite login is in place, False when it is not, None when Kite could not say."""
    if getattr(kite, "is_direct", False):
        return bool(kite.direct.signed_in)
    if not getattr(kite, "connected", False):
        return False
    try:
        kite.profile()
    except KiteError as exc:
        return False if exc.needs_login else None
    except Exception:  # noqa: BLE001 - an MCP server that is down cannot tell us
        return None
    return True


def parse_nse_holidays(answer) -> list[str]:
    """NSE ``holiday-master?type=trading`` → the cash-market holidays as YYYY-MM-DD."""
    rows = answer.get("CM") if isinstance(answer, dict) else None
    out = set()
    for row in rows or []:
        with contextlib.suppress(AttributeError, TypeError, ValueError):
            out.add(datetime.strptime(str(row.get("tradingDate", "")).strip(), "%d-%b-%Y").strftime("%Y-%m-%d"))
    return sorted(out)


class KiteReminder:
    def __init__(self, settings, kite, notifier, nse=None, now=market.now_ist, path: Path | None = None,
                 link=None, wall=None, background: bool = True):
        import time

        self.settings, self.kite, self.notifier, self.nse, self._now = settings, kite, notifier, nse, now
        self.path = path or home() / "kite-reminder.json"
        self.link = link or (lambda: "")          # () -> the address that starts a Kite login on this desk
        self._wall = wall or time.time
        self._background = background
        self._lock = threading.Lock()
        self._fetching: threading.Thread | None = None
        self._failures = 0
        self._state: dict = {"day": None, "outcome": None, "nse_holidays": [], "nse_fetched": 0, "nse_error": None,
                             "link_base": ""}
        with contextlib.suppress(OSError, ValueError, TypeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._state.update({k: stored[k] for k in self._state if k in stored})

    def _save(self) -> None:
        with contextlib.suppress(OSError):
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            os.replace(tmp, self.path)

    def remember_base(self, base: str) -> None:
        """The address the desk was last opened at, for the reminder's link when DESK_PUBLIC_URL is not set."""
        base = str(base or "").rstrip("/")
        if base.startswith(("http://", "https://")) and base != self._state.get("link_base"):
            with self._lock:
                self._state["link_base"] = base
                self._save()

    def link_base(self) -> str:
        return str(self._state.get("link_base") or "")

    # -- NSE holidays ------------------------------------------------------

    def _fetch_holidays(self) -> None:
        try:
            dates = parse_nse_holidays(self.nse.json("holiday-master", {"type": "trading"}))
            if not dates:
                raise ValueError("NSE's holiday list came back empty")
            with self._lock:
                self._state.update(nse_holidays=dates, nse_fetched=self._wall(), nse_error=None)
                self._save()
        except Exception as exc:  # noqa: BLE001 - Settings' own list still applies
            logger.info("NSE holiday list not fetched: %s", exc)
            with self._lock:
                # Try again in an hour, not every tick.
                self._state.update(nse_error=str(exc)[:200],
                                   nse_fetched=self._wall() - HOLIDAYS_MAX_AGE + 3600)
                self._save()

    def _holidays_ready(self) -> bool:
        """Start fetching NSE's list when it is old. False while a fetch is running (decide on a later tick)."""
        if self.nse is None:
            return True
        if self._fetching is not None and self._fetching.is_alive():
            return False
        if self._wall() - float(self._state.get("nse_fetched") or 0) < HOLIDAYS_MAX_AGE:
            return True
        if not self._background:
            self._fetch_holidays()
            return True
        self._fetching = threading.Thread(target=self._fetch_holidays, name="desk-nse-holidays", daemon=True)
        self._fetching.start()
        return False

    def is_trading_day(self, moment: datetime) -> bool:
        return market.is_trading_day(moment) and moment.strftime("%Y-%m-%d") not in set(self._state["nse_holidays"])

    # -- the reminder ------------------------------------------------------

    def _decide(self, day: str, outcome: str) -> None:
        with self._lock:
            self._state.update(day=day, outcome=outcome)
            self._save()

    def tick(self) -> None:
        if not self.settings.get("kite_reminder"):
            return
        now = self._now()
        day = now.strftime("%Y-%m-%d")
        if self._state.get("day") == day:
            return
        hour, minute = (int(x) for x in self.settings.get("kite_reminder_time").split(":"))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if now < start - timedelta(hours=2):
            return
        if not self._holidays_ready():          # fetched ahead of the reminder when the desk is already up
            return
        if now < start:
            return
        if now >= start + timedelta(minutes=WINDOW_MINUTES):
            self._decide(day, "missed: the desk was not running at the reminder time")
            return
        if not self.is_trading_day(now):
            self._decide(day, "not a trading day")
            return
        if self.settings.all().get("broker", "kite") != "kite":
            self._decide(day, "the desk uses another broker")
            return
        signed_in = kite_signed_in(self.kite)
        if signed_in is None:
            return                              # Kite could not say; ask again on the next tick
        if signed_in:
            self._decide(day, "already logged in to Kite")
            return
        target = self.notifier.telegram_target(require_alerts=False)
        if not target:
            self._decide(day, "no Telegram chat linked")
            return
        link = self.link()
        local = "//127.0.0.1" in link or "//localhost" in link
        text = ("🔑 Log in to Kite for today\n"
                "Your desk is not connected to Kite yet. One login lasts until 6 AM tomorrow."
                + (f"\nLog in: {link}" + (" (opens on the computer running the desk)" if local else "")
                   if link else "\nOpen the desk → Trade → Log in to Kite."))
        self.notifier.deliver(target, text)
        problem = self.notifier.last_error
        if problem:
            self._failures += 1
            if self._failures < 3:
                return                          # Telegram may be briefly down; try again on the next tick
            self.notifier.record("Kite login reminder not sent", f"Telegram did not take it: {problem}", "warn")
            self._decide(day, f"not delivered: {problem}")
        else:
            self.notifier.record("Kite login reminder sent", f"Sent to Telegram at {now:%H:%M}: not logged in to "
                                 "Kite yet today.", "info")
            self._decide(day, "sent")
        self._failures = 0

    def status(self) -> dict:
        return {"enabled": bool(self.settings.get("kite_reminder")), "time": self.settings.get("kite_reminder_time"),
                "last_day": self._state.get("day"), "last_outcome": self._state.get("outcome"),
                "nse_holidays": len(self._state.get("nse_holidays") or []), "nse_error": self._state.get("nse_error")}
