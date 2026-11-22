"""Price alerts: tell me when a share, a future or a coin reaches a price.

An alert fires once, when the price reaches its level, and then stops. It goes
to the in-app alert log and, when set up in Settings, to Telegram.

Indian prices come from the broker only (never a delayed free feed, which
would fire late or on yesterday's close), and only while that market is open:
NSE/BSE 09:15-15:30, MCX in its own hours. Crypto trades all day, so crypto
alerts are checked all the time.

An alert set on the wrong side (say "above 2,500" while the price is already
2,600) would fire at once, so it is refused when the current price is known.

Checking runs on a background thread that exists only while an alert waits.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from pathlib import Path

from . import market, mcx

logger = logging.getLogger(__name__)

MARKETS = {"india": "Indian shares, F&O and MCX", "crypto": "Crypto"}
CONDITIONS = {"above": "rises to or above", "below": "falls to or below"}
MAX_ALERTS = 100
KEEP_DONE = 50
CHECK_SECONDS = 15


class PriceAlerts:
    def __init__(self, path: Path, notify, india_prices, crypto_prices, now=market.now_ist,
                 every: float = CHECK_SECONDS):
        """``india_prices(instruments) -> {instrument: price}`` (raises when the broker is not available);
        ``crypto_prices(symbols) -> {symbol: {"price"} | {"error"}}``; ``notify(title, body, level, key)``."""
        self.path, self._notify = path, notify
        self.india_prices, self.crypto_prices, self._now, self._every = india_prices, crypto_prices, now, every
        self._lock = threading.Lock()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()
        self.status: dict = {"checked_ist": None, "india": None, "crypto": None}
        self._alerts: list[dict] = self._load()

    # -- storage -----------------------------------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            rows = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        return [r for r in rows if isinstance(r, dict) and r.get("id")] if isinstance(rows, list) else []

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        with contextlib.suppress(OSError):
            tmp.write_text(json.dumps(self._alerts, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    # -- what the screen uses ----------------------------------------------------------------------
    def list(self) -> dict:
        with self._lock:
            rows = sorted(self._alerts, key=lambda a: (a["status"] != "waiting", -a["created"]))
            return {"alerts": [dict(r) for r in rows], "status": dict(self.status), "every_seconds": self._every}

    def add(self, raw: dict, instrument: str | None = None, current: float | None = None) -> dict:
        """A new alert. ``instrument`` is the broker name (``NSE:RELIANCE``) for Indian alerts, or the crypto
        pair; ``current`` is the price now when known, used to refuse an alert that would fire at once."""
        kind = str(raw.get("market", ""))
        if kind not in MARKETS:
            raise ValueError("Choose Indian market or Crypto")
        condition = str(raw.get("condition", ""))
        if condition not in CONDITIONS:
            raise ValueError("Choose whether the price should rise to or fall to the level")
        price = raw.get("price")
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not price > 0 or price > 1e9:
            raise ValueError("Enter the alert price, more than 0")
        price = float(price)
        if not instrument:
            raise ValueError("Enter a symbol")
        if current is not None:
            if condition == "above" and current >= price:
                raise ValueError(f"The price is already {current:,.2f}, at or above {price:,.2f}. Choose a higher level, "
                                 f"or “falls to” instead.")
            if condition == "below" and current <= price:
                raise ValueError(f"The price is already {current:,.2f}, at or below {price:,.2f}. Choose a lower level, "
                                 f"or “rises to” instead.")
        note = str(raw.get("note") or "").strip()[:120]
        alert = {"id": uuid.uuid4().hex[:10], "market": kind, "instrument": instrument,
                 "label": str(raw.get("label") or instrument)[:60], "condition": condition, "price": price,
                 "note": note, "price_when_set": current, "created": time.time(),
                 "created_ist": self._now().strftime("%Y-%m-%d %H:%M"), "status": "waiting",
                 "triggered_ist": None, "triggered_price": None}
        with self._lock:
            if sum(1 for a in self._alerts if a["status"] == "waiting") >= MAX_ALERTS:
                raise ValueError(f"You have {MAX_ALERTS} waiting alerts, the most allowed. Delete some first.")
            self._alerts.append(alert)
            self._prune()
            self._save()
        self.start()
        return alert

    def remove(self, alert_id: str) -> bool:
        with self._lock:
            before = len(self._alerts)
            self._alerts = [a for a in self._alerts if a["id"] != alert_id]
            changed = len(self._alerts) != before
            if changed:
                self._save()
        return changed

    def clear_done(self) -> int:
        with self._lock:
            before = len(self._alerts)
            self._alerts = [a for a in self._alerts if a["status"] == "waiting"]
            self._save()
            return before - len(self._alerts)

    def _prune(self) -> None:
        done = sorted((a for a in self._alerts if a["status"] != "waiting"), key=lambda a: a["created"])
        drop = {a["id"] for a in done[:-KEEP_DONE]} if len(done) > KEEP_DONE else set()
        if drop:
            self._alerts = [a for a in self._alerts if a["id"] not in drop]

    # -- checking ----------------------------------------------------------------------------------
    def _open(self, instrument: str, moment) -> bool:
        if instrument.startswith(f"{mcx.EXCHANGE}:"):
            return mcx.session_status(moment)["open"]
        return market.session_status(moment)["open"]

    def check(self) -> list[dict]:
        """One pass over the waiting alerts. Returns the alerts that fired."""
        moment = self._now()
        with self._lock:
            waiting = [dict(a) for a in self._alerts if a["status"] == "waiting"]
        india = sorted({a["instrument"] for a in waiting if a["market"] == "india" and self._open(a["instrument"], moment)})
        coins = sorted({a["instrument"] for a in waiting if a["market"] == "crypto"})
        prices: dict[tuple, float] = {}
        india_note = None if india else ("Market closed: Indian alerts wait for the session." if any(
            a["market"] == "india" for a in waiting) else None)
        if india:
            try:
                for name, value in (self.india_prices(india) or {}).items():
                    if value:
                        prices[("india", name)] = float(value)
                india_note = "Live prices from your broker."
            except Exception as exc:  # noqa: BLE001 - shown on the screen; the next pass tries again
                india_note = f"No live price from your broker: {str(exc)[:160]}"
        crypto_note = None
        if coins:
            try:
                got = self.crypto_prices(coins) or {}
                for name, value in got.items():
                    if isinstance(value, dict) and value.get("price"):
                        prices[("crypto", name)] = float(value["price"])
                missing = [c for c in coins if ("crypto", c) not in prices]
                crypto_note = "Live crypto prices." if not missing else f"No price for {', '.join(missing)} this time."
            except Exception as exc:  # noqa: BLE001
                crypto_note = f"No crypto price: {str(exc)[:160]}"
        fired = []
        stamp = moment.strftime("%Y-%m-%d %H:%M:%S")
        with self._lock:
            self.status = {"checked_ist": stamp, "india": india_note, "crypto": crypto_note}
            for alert in self._alerts:
                if alert["status"] != "waiting":
                    continue
                price = prices.get((alert["market"], alert["instrument"]))
                if price is None:
                    continue
                alert["last_price"] = price
                hit = price >= alert["price"] if alert["condition"] == "above" else price <= alert["price"]
                if hit:
                    alert.update(status="triggered", triggered_ist=stamp, triggered_price=price)
                    fired.append(dict(alert))
            if fired:
                self._save()
        for alert in fired:
            verb = "rose to" if alert["condition"] == "above" else "fell to"
            body = f"{alert['label']} {verb} {alert['triggered_price']:,.2f} (your level {alert['price']:,.2f}) at {stamp} IST."
            if alert["note"]:
                body += f"\nNote: {alert['note']}"
            try:
                self._notify(f"Price alert: {alert['label']}", body, "info", f"price-alert-{alert['id']}")
            except Exception:  # noqa: BLE001 - the alert is already marked; the log shows it
                logger.exception("price alert notification failed")
        return fired

    def waiting(self) -> int:
        with self._lock:
            return sum(1 for a in self._alerts if a["status"] == "waiting")

    def start(self) -> None:
        """Run the checker while an alert waits; it stops by itself when none is left."""
        if self._worker and self._worker.is_alive():
            return
        if not self.waiting():
            return
        self._stop.clear()

        def run():
            while not self._stop.wait(self._every) and self.waiting():
                try:
                    self.check()
                except Exception:  # noqa: BLE001 - keep checking
                    logger.exception("price alert check failed")

        self._worker = threading.Thread(target=run, name="desk-price-alerts", daemon=True)
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()
