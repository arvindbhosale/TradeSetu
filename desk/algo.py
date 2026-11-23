"""Algo trading: a strategy that trades one stock by itself.

Two trade types. Day candles are always positional; intraday candles can be either.

- **Positional on day candles (delivery, CNC).** After the close the algo reads the day's candle. If
  the strategy's position changed it queues one order and sends it when the
  trade window opens next session (09:30 by default). With the window switched
  off it sends at once, live as an AMO. A target and stoploss, when set, are
  placed as a GTT after the buy fills.
- **Intraday (MIS), intraday candles.** During the trade window the algo reads
  each finished candle and enters or exits on the strategy's signal. It watches
  the price for the target and stoploss, takes no new entry after the window
  ends, and sells whatever it holds at the square-off time. Nothing is carried
  overnight.
- **Positional (delivery, CNC), intraday candles.** Reads each finished candle
  during market hours like an intraday algo, but there is no square-off: the
  position is carried overnight until the strategy, the stoploss, the target or
  the holding limit closes it. After the buy fills, the target and stoploss are
  also placed as a GTT, so the broker still guards the position overnight,
  across a desk restart and before the morning login; a trailing stoploss moves
  the GTT up once a candle. The trade window in Settings limits its buys only
  when Settings enforces it. The daily loss limit does not sell it (as with
  delivery holdings).

A holding limit (``max_hold_days``) sells a positional position once it has been
held that many trading days: a day-candle algo queues the sell for the open of
that day, an intraday-candle one sells at its first check that day.

Every order passes the desk's risk checks. Things to know before going live:

- Kite signs you out every morning. Until someone signs in, a live algo can
  neither read prices nor place orders; it reports that and waits.
- Yahoo's intraday candles are delayed. A live intraday algo uses Kite's candles
  when the API key has historical data, and refuses to act on stale candles.
- A live order counts as a position only once Kite reports it filled.
- A new daily algo waits for a fresh entry signal rather than joining a trend.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import uuid
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.symbols import kite_instrument, normalize_symbol, safe_ticker_component

from . import data, market, risk
from .broker import Desk, OrderRejected
from .kite import KiteError
from .settings import home

logger = logging.getLogger(__name__)

MAX_ALGOS = 20
RUN_AFTER = clock_time(15, 50)     # the day's candle is final and Yahoo has usually published it
RUN_UNTIL = clock_time(23, 30)
RETRY = timedelta(minutes=10)
FRESH_SECONDS = 300
UNFILLED_AFTER = timedelta(minutes=3)      # an intraday limit order still open after this is cancelled
TICK_SECONDS = 20
JOB_NAMES = {                               # the engines the server drives from this clock, in plain words
    "Scanner.tick": "Scanner",
    "OpenPeaks.tick": "Open P&L high/low recorder",
    "Derivatives.tick": "F&O contract refresh",
    "FnoAlgoEngine.tick": "F&O algos",
    "Desk.close_paper_mcx": "Paper MCX square-off",
    "create_app.<locals>.settle_paper_options": "Paper option expiry settlement",
    "Recorder.tick": "Option chain recorder",
    "ChartOrders.tick": "Chart orders (stoploss and target)",
}


class AlgoError(ValueError):
    pass


def arm_phrase(name: str) -> str:
    return f"ARM LIVE {' '.join(name.upper().split())}"


class AlgoEngine:
    def __init__(self, desk: Desk, path: Path | None = None, fetch=None, now=market.now_ist, library=None):
        from .custom import CustomStrategies

        self.desk, self.fetch, self._now = desk, fetch, now
        self.library = library or CustomStrategies()
        self.path = path or home() / "algos.json"
        self._lock = threading.RLock()
        self._algos: dict[str, dict] = {}
        self._retry_at: dict[str, datetime] = {}
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.extra_ticks: list = []          # other engines driven by the same clock (the scanner)
        self.extra_live: list = []           # callables naming other engines' live algos (F&O), for the login alert
        self._kite_checked = datetime(2000, 1, 1, tzinfo=market.IST)
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, list):
                for algo in stored:
                    if isinstance(algo, dict) and "id" in algo:
                        algo = {**_BLANK, **algo}
                        if not algo.get("trade_type"):        # saved before the choice: the product says which
                            algo["trade_type"] = "intraday" if algo.get("product") == "MIS" else "positional"
                        self._algos[algo["id"]] = algo

    # -- storage ------------------------------------------------------------

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._algos.values()), indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        with self._lock:
            out = json.loads(json.dumps(sorted(self._algos.values(), key=lambda a: a["created"])))
        for algo in out:
            text = self.rules(algo).text()
            if algo.get("trail_pct"):
                text += f", trailing stoploss {algo['trail_pct']:g}% below the high"
            algo["exits_text"] = text
        return out

    def get(self, algo_id: str) -> dict:
        with self._lock:
            if algo_id not in self._algos:
                raise AlgoError("no such algo")
            return self._algos[algo_id]

    def _log(self, algo: dict, message: str) -> None:
        if algo["log"] and algo["log"][-1]["message"] == message:
            return                      # a state that persists is logged once, not every tick
        algo["log"].append({"time_ist": self._now().strftime("%Y-%m-%d %H:%M"), "message": message})
        del algo["log"][:-80]
        logger.info("algo %s: %s", algo["name"], message)

    # -- create, change, remove ---------------------------------------------

    def _config(self, raw: dict) -> dict:
        """The settings ``raw`` describes, checked; raises AlgoError. Nothing is stored."""
        name = " ".join(str(raw.get("name", "")).split())
        if not 1 <= len(name) <= 40 or not all(c.isalnum() or c in " -_&." for c in name):
            raise AlgoError("Give the algo a name of up to 40 letters, digits or spaces.")
        try:
            symbol = safe_ticker_component(normalize_symbol(str(raw.get("symbol", ""))))
        except ValueError:
            raise AlgoError("That is not a valid symbol.") from None
        if not kite_instrument(symbol):
            raise AlgoError(f"{symbol} is not an NSE/BSE equity. An algo trades a stock, not an index.")
        try:
            strategy = self.library.strategy(str(raw.get("strategy", "")))
        except ValueError:
            raise AlgoError("Unknown strategy.") from None
        if strategy.key == "buy_hold":
            raise AlgoError("Buy and hold never changes position; there is nothing to automate.")
        params = strategy.resolve(raw.get("strategy_params"))
        interval = str(raw.get("interval", "1d"))
        if interval not in data.INTERVALS:
            raise AlgoError(f"Candle size must be one of {', '.join(data.INTERVALS)}.")
        if strategy.intraday_only and interval == "1d":
            raise AlgoError(f"{strategy.name} needs an intraday candle size.")
        kind = str(raw.get("trade_type") or ("positional" if interval == "1d" else "intraday")).lower()
        if kind not in ("intraday", "positional"):
            raise AlgoError("Trade type must be intraday or positional.")
        if kind == "intraday" and interval == "1d":
            raise AlgoError("An intraday algo needs minute or hourly candles. Day candles are positional.")
        hold = raw.get("max_hold_days") or 0
        if isinstance(hold, bool) or not isinstance(hold, (int, float)) or hold != int(hold) or not 0 <= hold <= 2000:
            raise AlgoError("Sell after (days held) must be a whole number of days from 0 (no limit) to 2000.")
        if hold and kind == "intraday":
            raise AlgoError("A holding limit in days needs a positional algo: intraday algos sell the same day.")
        mode = raw.get("mode", "paper")
        if mode not in ("paper", "live"):
            raise AlgoError("Mode must be paper or live.")
        budget = raw.get("budget")
        if isinstance(budget, bool) or not isinstance(budget, (int, float)) or not 1000 <= budget <= 1e9:
            raise AlgoError("Budget must be an amount in rupees of at least 1,000.")
        levels = {}
        for key, label in (("target_pct", "Target"), ("stop_loss_pct", "Stoploss"), ("trail_pct", "Trailing stoploss")):
            value = raw.get(key, 0) or 0
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 50:
                raise AlgoError(f"{label} must be between 0 and 50 percent (0 for none).")
            levels[key] = float(value)
        try:
            exits = risk.from_payload(raw.get("exits"), "Stock",
                                      legacy={"stop_loss": levels["stop_loss_pct"], "target": levels["target_pct"]})
        except ValueError as exc:
            raise AlgoError(f"{str(exc)[:1].upper()}{str(exc)[1:]}.") from None
        levels.update(target_pct=0.0, stop_loss_pct=0.0)       # kept for algos saved before exit rules
        return {"name": name, "symbol": symbol, "strategy": strategy.key, "strategy_name": strategy.name,
                "params": params, "interval": interval, "trade_type": kind, "max_hold_days": int(hold),
                "product": "CNC" if kind == "positional" else "MIS",
                "mode": mode, "budget": float(budget), **levels, "exits": exits.as_dict()}

    @staticmethod
    def _summary(algo: dict) -> str:
        kind = ("daily, delivery" if algo["interval"] == "1d" else
                f"{algo['interval']} candles, {'positional (delivery)' if positional(algo) else 'intraday'}")
        if positional(algo) and algo.get("max_hold_days"):
            kind += f", sold after {algo['max_hold_days']} day{'s' if algo['max_hold_days'] > 1 else ''} held"
        exits = AlgoEngine.rules(algo).text()
        return (f"{algo['strategy_name']} on {algo['symbol']}, ₹{algo['budget']:,.0f}, {algo['mode']}, {kind}; "
                f"{exits}")

    def create(self, raw: dict) -> dict:
        config = self._config(raw)
        with self._lock:
            if len(self._algos) >= MAX_ALGOS:
                raise AlgoError(f"The desk runs at most {MAX_ALGOS} algos.")
            if any(a["name"].lower() == config["name"].lower() for a in self._algos.values()):
                raise AlgoError("An algo with that name already exists.")
            algo = {**json.loads(json.dumps(_BLANK)), **config, "id": uuid.uuid4().hex[:10],
                    "created": self._now().strftime("%Y-%m-%d %H:%M:%S")}
            self._log(algo, f"Created: {self._summary(algo)}.")
            self._algos[algo["id"]] = algo
            self._save()
            return json.loads(json.dumps(algo))

    @staticmethod
    def holding(algo: dict) -> bool:
        """Whether the algo has shares, an order out, an order waiting for the window, or a GTT."""
        return bool(algo["position"] or algo["pending"] or algo["queued"] or algo["protect"])

    def update(self, algo_id: str, raw: dict) -> dict:
        """Change an algo that is not trading live.

        The name is fixed: the P&L ledger files every fill under it. While the algo holds shares or has an order
        or GTT out, only the budget and the exit rules can change (and not the exit rules of a live GTT, which
        sits at the broker); the stock, strategy, candle and mode are tied to that position. A change of mode
        stops the algo, so a paper algo becomes live only through Start and its typed phrase."""
        with self._lock:
            algo = self.get(algo_id)
            if algo["enabled"] and algo["mode"] == "live":
                raise AlgoError("This algo is trading live. Stop it before editing.")
            config = self._config({**raw, "name": algo["name"]})
            changed = [k for k in config if config[k] != algo.get(k)]
            if algo.get("exits") is None and "exits" in changed and config["exits"] == self.rules(algo).as_dict():
                changed.remove("exits")                      # an algo saved before exit rules, rules unchanged
            for key in ("strategy_name", "product", "target_pct", "stop_loss_pct"):
                if key in changed:
                    changed.remove(key)                       # follow from the fields below
            if not changed:
                return json.loads(json.dumps(algo))
            core = [k for k in changed if k in ("symbol", "strategy", "params", "interval", "trade_type", "mode")]
            if self.holding(algo):
                if core:
                    raise AlgoError(f"This algo {self._holding_text(algo)}. While it does, only the budget, the "
                                    f"exit rules and the holding limit can change. Close or clear the position to "
                                    f"change the rest.")
                if "exits" in changed and algo["protect"] and algo["mode"] == "live":
                    raise AlgoError(f"The target and stoploss of this algo sit at your broker as GTT {algo['protect']}. "
                                    f"Change them there, or close the position first.")
            was_enabled = algo["enabled"]
            algo.update(config)
            if core:
                # A different stock, strategy or candle is a new algo in all but name: wait for a fresh signal.
                algo.update(last_bar=None, last_signal=None, seen_flat=False, blocked=False, rearm=None,
                            peak=None, trail_stop=None, stop_kind=None, stop_level=None)
                self._retry_at.pop(algo_id, None)
            if "mode" in changed and was_enabled:
                algo.update(enabled=False, status="Stopped")
            if "exits" in changed and algo["position"]:
                algo.update(trail_stop=None, stop_level=None, stop_kind=None, reentries_left=self.rules(algo).reentry)
                if algo["protect"]:                         # paper GTT: replace it with the new levels
                    with contextlib.suppress(KiteError):
                        self.desk.cancel_gtt(algo["mode"], algo["protect"])
                    algo["protect"] = None
                    self._protect(algo)
            self._log(algo, f"Edited ({', '.join(changed)}): {self._summary(algo)}."
                      + (" Stopped: the mode changed." if "mode" in changed and was_enabled else ""))
            self._save()
            return json.loads(json.dumps(algo))

    @staticmethod
    def _holding_text(algo: dict) -> str:
        if algo["position"]:
            return f"holds {algo['position']} shares"
        if algo["pending"]:
            return f"has an order out ({algo['pending']['side']} {algo['pending']['quantity']})"
        if algo["queued"]:
            return f"has a {algo['queued']['side']} waiting for the trade window"
        return f"has a GTT ({algo['protect']}) out"

    def set_enabled(self, algo_id: str, enabled: bool, phrase: str = "") -> dict:
        with self._lock:
            algo = self.get(algo_id)
            if enabled and algo["mode"] == "live":
                if " ".join(str(phrase).upper().split()) != arm_phrase(algo["name"]):
                    raise AlgoError(f'Type "{arm_phrase(algo["name"])}" to let this algo place live orders.')
                if not self.desk.settings.get("live_enabled"):
                    raise AlgoError("Live orders are switched off in Settings.")
            if enabled:
                self._strategy(algo)             # a deleted custom strategy stops it here, not mid-session
            algo["enabled"] = bool(enabled)
            algo["exit_requested"] = False
            algo["status"] = "Running" if enabled else "Stopped"
            self._log(algo, "Started." if enabled else "Stopped.")
            self._retry_at.pop(algo_id, None)
            self._save()
            return json.loads(json.dumps(algo))

    def delete(self, algo_id: str) -> None:
        with self._lock:
            algo = self.get(algo_id)
            if algo["enabled"]:
                raise AlgoError("Stop the algo before deleting it.")
            if algo["position"] or algo["pending"]:
                raise AlgoError(f"This algo still holds {algo['position']} shares or has an order pending. "
                                f"It is not sold when deleted; close it yourself, then delete.")
            del self._algos[algo_id]
            self._save()

    def forget_position(self, algo_id: str) -> dict:
        """Mark a stopped algo flat, after its shares were dealt with by hand."""
        with self._lock:
            algo = self.get(algo_id)
            if algo["enabled"]:
                raise AlgoError("Stop the algo first.")
            self._log(algo, f"Position of {algo['position']} cleared by hand; no order was sent.")
            algo.update(position=0, average_price=0.0, pending=None, queued=None, protect=None, seen_flat=False,
                        entry_day=None, entry_at=None)
            self._save()
            return json.loads(json.dumps(algo))

    def request_exit(self, algo_id: str, reason: str = "Exit pressed") -> dict:
        """Sell what the algo holds at the next chance inside market hours, then stop it. Works with the kill
        switch on, and on a stopped algo: it only ever sells (its target/stoploss GTT is cancelled first)."""
        with self._lock:
            algo = self.get(algo_id)
            if not algo["position"] and not algo["pending"]:
                if algo["enabled"]:
                    algo.update(enabled=False, exit_requested=False, queued=None, status="Stopped")
                    self._log(algo, f"{reason} with nothing held: stopped.")
                    self._save()
                    return json.loads(json.dumps(algo))
                raise AlgoError("This algo is stopped and holds nothing, so there is nothing to exit.")
            algo["queued"] = None if (algo["queued"] or {}).get("side") == "BUY" else algo["queued"]
            algo["exit_requested"] = True
            self._log(algo, f"{reason}: selling what it holds, then stopping.")
            self._retry_at.pop(algo_id, None)
            self._save()
        return self.evaluate(algo_id)["algo"]

    def hold_entries(self, algo_id: str, until: str, reason: str) -> dict:
        """Take no new buy up to and including the day ``until`` (YYYY-MM-DD). Exits keep working."""
        with self._lock:
            algo = self.get(algo_id)
            algo["hold_until"] = max(until, algo.get("hold_until") or "")
            self._log(algo, f"New buys held until the end of {algo['hold_until']}: {reason}.")
            self._save()
            return json.loads(json.dumps(algo))

    def release_hold(self, algo_id: str) -> dict:
        with self._lock:
            algo = self.get(algo_id)
            if algo.get("hold_until"):
                algo["hold_until"] = None
                self._log(algo, "New buys allowed again.")
                self._save()
            return json.loads(json.dumps(algo))

    def _held(self, algo: dict) -> bool:
        return bool(algo.get("hold_until")) and self._now().strftime("%Y-%m-%d") <= algo["hold_until"]

    def _exit(self, algo: dict) -> dict:
        """One step of an exit request: sell inside market hours, stop once flat."""
        if not algo["position"]:
            algo.update(enabled=False, exit_requested=False, queued=None)
            return self._result(algo, "Stopped", True, "Flat after the exit request: stopped.")
        if not self.desk.window_status()["market"]:
            return self._result(algo, "Running: exit waits for the market to open", True)
        outcome = self._send(algo, "SELL", "exit requested", fallback_price=algo["average_price"] or None)
        if not algo["position"] and not algo["pending"]:
            algo.update(enabled=False, exit_requested=False, queued=None)
            self._log(algo, "Flat after the exit request: stopped.")
            return {"status": "Stopped", "done": True}
        return outcome

    def _strategy(self, algo: dict):
        try:
            return self.library.strategy(algo["strategy"])
        except ValueError:
            raise AlgoError(f"The strategy “{algo['strategy_name']}” no longer exists.") from None

    # -- one step -----------------------------------------------------------

    def evaluate(self, algo_id: str, force: bool = False) -> dict:
        """Run one algo now. Returns what it did."""
        with self._lock:
            algo = self.get(algo_id)
            tail = algo["log"][-1] if algo["log"] else None
            try:
                outcome = self._step(algo, force)
            except AlgoError as exc:
                outcome = self._result(algo, f"Blocked: {exc}", False, str(exc))
            before = algo["status"]
            algo["status"] = outcome["status"]
            if outcome["status"].startswith("Blocked") and outcome["status"] != before:
                # The status, and the reason logged by this run when it adds to it.
                last = algo["log"][-1]["message"] if algo["log"] and algo["log"][-1] is not tail else ""
                body = outcome["status"] if not last or last in outcome["status"] else f"{outcome['status']}. {last}"
                self.desk.notifier.notify(f"Algo “{algo['name']}” is blocked", body, "bad",
                                          key=f"blocked:{algo['id']}:{outcome['status']}")
            self._save()
            return {**outcome, "algo": json.loads(json.dumps(algo))}

    def _result(self, algo: dict, status: str, done: bool, message: str | None = None) -> dict:
        if message:
            self._log(algo, message)
        return {"status": status, "done": done}

    def _step(self, algo: dict, force: bool) -> dict:
        if algo.get("exit_requested") and not algo["position"] and not algo["pending"]:
            return self._exit(algo)
        if self.desk.settings.get("kill_switch") and not algo["position"] and not algo["pending"]:
            # Nothing to close: with the kill switch on there is nothing for it to do.
            return self._result(algo, "Blocked: kill switch is on", False, "Skipped: the kill switch is on.")
        if algo["mode"] == "live" and not self.desk.kite.connected:
            return self._result(algo, "Blocked: Kite not connected", False, "Skipped: Kite is not connected.")
        if algo["pending"]:
            try:
                settled = self._reconcile(algo)
            except KiteError as exc:
                why = "Kite needs a login" if exc.needs_login else str(exc)
                return self._result(algo, f"Blocked: {why}", False, f"Could not check the pending order: {why}.")
            if not settled:
                return self._result(algo, "Waiting for a pending order", False)
        if algo.get("exit_requested"):
            return self._exit(algo)
        return self._daily(algo, force) if algo["interval"] == "1d" else self._intraday(algo, force)

    def _days_held(self, algo: dict) -> int:
        """Trading days since the buy, not counting the buy's own day: 0 on the day of the buy, 1 the next session.
        A position bought before the entry day was recorded counts from today."""
        start = algo.get("entry_day")
        today = self._now().date()
        if not start:
            algo["entry_day"] = today.isoformat()
            return 0
        day, held = datetime.strptime(start, "%Y-%m-%d").date(), 0
        while day < today and held < 5000:
            day += timedelta(days=1)
            held += market.is_trading_day(datetime.combine(day, clock_time(12), tzinfo=market.IST))
        return held

    def _holding(self, algo: dict) -> str:
        return f"holding {algo['position']}" if algo["position"] else "flat"

    @staticmethod
    def rules(algo: dict) -> risk.ExitRules:
        """The algo's exit rules. An algo saved before exit rules has percent stoploss and target fields."""
        if algo.get("exits"):
            return risk.ExitRules(**algo["exits"])
        return risk.ExitRules(unit="pct", stop_loss=algo.get("stop_loss_pct") or 0.0, target=algo.get("target_pct") or 0.0)

    def _levels(self, algo: dict) -> tuple[float | None, str, float | None]:
        """Stoploss (with its kind) and target for the open position, from the best price seen since the buy."""
        entry = algo["average_price"]
        peak = max(algo.get("peak") or entry, entry)
        stop, kind, goal = self.rules(algo).levels(entry, peak, 1)
        if algo["trail_pct"]:                  # the older trailing stop: a set % below the high
            trail = peak * (1 - algo["trail_pct"] / 100)
            if stop is None or trail > stop:
                stop, kind = trail, "trailing stoploss"
        return stop, kind, goal

    def _stopped_out(self, algo: dict, kind: str) -> None:
        """After a stoploss exit: block the plain re-entry and, when allowed, arm a re-entry."""
        algo["blocked"] = True
        left = algo.get("reentries_left") or 0
        algo["rearm"] = ({"price": algo["average_price"], "left": left}
                         if kind == "stoploss" and left > 0 else None)

    def _reentry_due(self, algo: dict, price: float | None) -> bool:
        rearm = algo.get("rearm")
        if not rearm or algo["position"] or not price:
            return False
        return self.rules(algo).reentry_mode == "at_once" or price >= rearm["price"]

    def _mark_entry(self, algo: dict, reentry: bool) -> None:
        if reentry:
            algo["reentries_left"] = max((algo["rearm"] or {}).get("left", 1) - 1, 0)
        else:
            algo["reentries_left"] = self.rules(algo).reentry
        algo["rearm"] = None

    # -- daily ---------------------------------------------------------------

    def _daily(self, algo: dict, force: bool) -> dict:
        cfg, window, now = self.desk.settings.all(), self.desk.window_status(), self._now()
        today = now.strftime("%Y-%m-%d")

        if algo["protect"]:
            self._check_protection(algo)

        # 1. Send an order queued by an earlier evaluation, once the window allows it.
        if algo["queued"]:
            side = algo["queued"]["side"]
            allowed = (window["entries"] if side == "BUY" else window["market"]) if cfg["enforce_trade_window"] else True
            if not allowed:
                return self._result(algo, f"Running: {side} queued for {cfg['trade_start']}", True)
            if side == "BUY" and cfg["kill_switch"]:
                return self._result(algo, "Blocked: kill switch is on", False, "BUY held back: the kill switch is on.")
            if side == "BUY" and self._held(algo):
                return self._result(algo, "Running: new buys held", True,
                                    f"BUY held back: new buys are held until the end of {algo['hold_until']}.")
            return self._send(algo, side, algo["queued"]["reason"])

        # 2. Read the latest finished daily candle.
        due = force or (market.is_trading_day(now) and RUN_AFTER <= now.time() <= RUN_UNTIL and algo["last_bar"] != today)
        if not due:
            return self._result(algo, algo["status"] if algo["status"].startswith("Running") else
                                f"Running: {self._holding(algo)}", True)
        try:
            bars = data.history(algo["symbol"], fetch=self.fetch, max_age=FRESH_SECONDS)
        except data.NoData as exc:
            return self._result(algo, "Blocked: no price data", False, f"No price data: {exc}")
        if bars.index[-1].strftime("%Y-%m-%d") == today and now.time() < market.CLOSE:
            bars = bars.iloc[:-1]      # today's candle is still forming; a signal needs a close
            if len(bars) < 2:
                return self._result(algo, "Blocked: no price data", False, "Not enough price history.")
        last_bar = bars.index[-1].strftime("%Y-%m-%d")
        if not force and (algo["last_bar"] == last_bar or last_bar != today):
            return self._result(algo, "Waiting for today's bar", False)
        try:
            target = int(self._strategy(algo).positions(bars, algo["params"]).iloc[-1])
        except ValueError as exc:
            return self._result(algo, "Blocked: strategy error", False, f"Strategy error: {exc}")
        close = float(bars["Close"].iloc[-1])
        algo["last_signal"] = {"bar": last_bar, "target": target, "close": round(close, 2)}
        algo["last_bar"] = last_bar
        if target == 0:
            algo["seen_flat"], algo["blocked"], algo["rearm"] = True, False, None

        side = None
        reason = f"{last_bar} signal"
        limit = algo.get("max_hold_days") or 0
        if algo["position"] and limit and not algo["pending"] and self._days_held(algo) + 1 >= limit:
            # Sold at the next open: that session is the limit-th trading day after the buy.
            side, reason = "SELL", f"held {limit} day{'s' if limit > 1 else ''}"
            algo["blocked"], algo["rearm"] = True, None
        elif target == 1 and algo["position"] == 0:
            if not algo["seen_flat"]:
                return self._result(algo, "Running: waiting for a fresh entry", True,
                                    f"{last_bar}: strategy is already long. Waiting for it to go flat and signal "
                                    f"a new entry rather than joining mid-trend.")
            if algo["blocked"] and self._reentry_due(algo, float(bars["High"].iloc[-1])):
                self._mark_entry(algo, reentry=True)
                reason = f"{last_bar} re-entry after stoploss"
            elif algo["blocked"]:
                waiting = (f"; re-entry when the price is back at ₹{algo['rearm']['price']:,.2f}"
                           if algo.get("rearm") else "; no re-entry until the strategy goes flat")
                status = "Running: waiting to re-enter" if algo.get("rearm") else "Running: waiting for the signal to reset"
                return self._result(algo, status, True,
                                    f"{last_bar}: still a buy after the target or stoploss exit{waiting}.")
            else:
                self._mark_entry(algo, reentry=False)
            side = "BUY"
        elif target == 0 and algo["position"] > 0:
            side = "SELL"
        if side is None:
            if algo["position"]:
                self._trail_daily(algo, close)
            return self._result(algo, f"Running: {self._holding(algo)}", True,
                                f"{last_bar}: no change, {self._holding(algo)} (close ₹{close:,.2f}).")
        if side == "BUY" and cfg["kill_switch"]:
            algo["queued"] = {"side": side, "reason": reason}
            return self._result(algo, "Blocked: kill switch is on", False,
                                f"{last_bar}: BUY signal held back: the kill switch is on.")
        if side == "BUY" and self._held(algo):
            algo["queued"] = {"side": side, "reason": reason}
            return self._result(algo, "Running: new buys held", True,
                                f"{last_bar}: BUY signal held back: new buys are held until the end of "
                                f"{algo['hold_until']}.")
        if cfg["enforce_trade_window"]:
            allowed = window["entries"] if side == "BUY" else window["market"]
            if not allowed:
                algo["queued"] = {"side": side, "reason": reason}
                return self._result(algo, f"Running: {side} queued for {cfg['trade_start']}", True,
                                    f"{last_bar}: {side} signal (close ₹{close:,.2f}). Queued for the next "
                                    f"trade window at {cfg['trade_start']} IST.")
        return self._send(algo, side, reason, fallback_price=close)

    # -- intraday ------------------------------------------------------------

    def _intraday(self, algo: dict, force: bool) -> dict:
        window, now = self.desk.window_status(), self._now()
        cfg = self.desk.settings.all()
        carry = positional(algo)
        if not window["market"] and carry:
            # Held overnight: nothing to do until the market opens. The GTT (if any) guards it at the broker.
            return self._result(algo, f"Running: market closed, {self._holding(algo)}", True)
        if not window["market"]:
            if algo["position"] and algo["mode"] == "paper":
                # Missed the square-off (the desk was down): nothing is carried overnight.
                return self._result(algo, "Blocked: position left open", False,
                                    f"Holding {algo['position']} after the close. Paper positions are not "
                                    f"carried overnight; stop the algo and clear the position.")
            algo["blocked"], algo["rearm"] = False, None
            return self._result(algo, "Running: market closed", True)

        instrument = "{}:{}".format(*kite_instrument(algo["symbol"]))
        price, _ = self.desk.last_price(instrument)
        # A positional algo's buys: inside market hours, and inside the trade window only when Settings enforces it.
        entries_open = (window["entries"] if cfg["enforce_trade_window"] else window["market"]) if carry else window["entries"]

        if carry and algo["protect"]:
            self._check_protection(algo)         # the GTT may have sold it at the open, before this check

        # 1a. Positional: the holding limit.
        limit = algo.get("max_hold_days") or 0
        if carry and algo["position"] and limit and self._days_held(algo) >= limit:
            algo["blocked"], algo["rearm"] = True, None
            return self._send(algo, "SELL", f"held {limit} day{'s' if limit > 1 else ''}", fallback_price=price)

        # 1. Intraday: square-off time, sell and stop for the day.
        if not carry and window["square_off"]:
            if algo["position"]:
                return self._send(algo, "SELL", f"square-off at {cfg['square_off']}", fallback_price=price)
            return self._result(algo, "Running: done for the day", True)

        # 2. Intraday: the daily loss limit was hit, close out and stay out. Positional positions are held, like
        # delivery holdings; the kill switch stops new buys.
        if not carry and algo["position"] and self.desk.flatten_day == now.strftime("%Y-%m-%d"):
            algo["blocked"] = True
            return self._send(algo, "SELL", "daily loss limit", fallback_price=price)

        # 3. Stoploss, target, trailing stoploss and stoploss to cost, on the live price.
        if algo["position"] and price:
            entry = algo["average_price"]
            algo["peak"] = max(algo.get("peak") or entry, price)
            stop, kind, goal = self._levels(algo)
            if stop is not None and price <= stop:
                self._stopped_out(algo, kind)
                return self._send(algo, "SELL", f"{kind} ₹{stop:,.2f} (bought at ₹{entry:,.2f}), price ₹{price:,.2f}")
            if goal is not None and price >= goal:
                algo["blocked"], algo["rearm"] = True, None
                return self._send(algo, "SELL", f"target ₹{goal:,.2f} (bought at ₹{entry:,.2f}), price ₹{price:,.2f}")

        # 3b. A re-entry after a stoploss, while the strategy still says buy.
        if (algo.get("rearm") and (algo["last_signal"] or {}).get("target") == 1 and entries_open
                and not self.desk.settings.get("kill_switch") and not self._held(algo)
                and self._reentry_due(algo, price)):
            self._mark_entry(algo, reentry=True)
            return self._send(algo, "BUY", f"re-entry after stoploss at ₹{price:,.2f}", fallback_price=price)

        # 3. The latest finished candle, read once per candle.
        minutes = data.INTERVALS[algo["interval"]]["minutes"]
        naive_now = now.replace(tzinfo=None)
        retry = self._retry_at.get(algo["id"])
        if not force and retry and retry > now:
            return self._result(algo, algo["status"], True)
        try:
            bars = self._candles(algo, instrument, naive_now)
        except (data.NoData, KiteError) as exc:
            self._retry_at[algo["id"]] = now + timedelta(minutes=1)
            return self._result(algo, "Blocked: no candle data", False, f"No candles: {exc}")
        bars = bars[bars.index + pd.Timedelta(minutes=minutes) <= naive_now]       # drop the forming candle
        bars_today = bars[bars.index.normalize() == pd.Timestamp(naive_now.date())]
        if bars_today.empty:
            self._retry_at[algo["id"]] = now + timedelta(minutes=1)
            return self._result(algo, "Running: waiting for today's first candle", True)
        stamp = bars.index[-1]
        last_bar = stamp.strftime("%Y-%m-%d %H:%M")
        age = (naive_now - (stamp + pd.Timedelta(minutes=minutes))).total_seconds() / 60
        if age > max(2 * minutes, 10):
            self._retry_at[algo["id"]] = now + timedelta(minutes=1)
            return self._result(algo, "Blocked: candles are delayed", False,
                                f"The newest finished candle is {age:.0f} minutes old. Not trading on stale "
                                f"data. Kite's historical-data add-on gives live candles.")
        if algo["last_bar"] == last_bar and not force:
            self._retry_at[algo["id"]] = now + timedelta(seconds=45)
            return self._result(algo, f"Running: {self._holding(algo)}", True)
        try:
            target = int(self._strategy(algo).positions(bars, algo["params"]).iloc[-1])
        except ValueError as exc:
            return self._result(algo, "Blocked: strategy error", False, f"Strategy error: {exc}")
        close = float(bars["Close"].iloc[-1])
        algo["last_bar"] = last_bar
        algo["last_signal"] = {"bar": last_bar, "target": target, "close": round(close, 2)}
        if carry and algo["position"] and target == 1:
            self._trail_daily(algo, close)         # a trailing stoploss moves the GTT up, once a candle
        if target == 0:
            algo["blocked"], algo["rearm"] = False, None
        if target == 0 and algo["position"]:
            return self._send(algo, "SELL", f"{last_bar} signal", fallback_price=price or close)
        if target == 1 and not algo["position"] and not algo["blocked"]:
            if self.desk.settings.get("kill_switch"):
                return self._result(algo, "Blocked: kill switch is on", False,
                                    f"{last_bar}: BUY signal not taken: the kill switch is on.")
            if self._held(algo):
                return self._result(algo, "Running: new buys held", True,
                                    f"{last_bar}: BUY signal not taken: new buys are held until the end of "
                                    f"{algo['hold_until']}.")
            if not entries_open:
                where = f"{cfg['trade_start']}-{cfg['trade_end']}" if not carry or cfg["enforce_trade_window"] else "market hours"
                return self._result(algo, "Running: outside the entry window", True,
                                    f"{last_bar}: BUY signal outside {where}; not taken.")
            self._mark_entry(algo, reentry=False)
            return self._send(algo, "BUY", f"{last_bar} signal", fallback_price=price or close)
        return self._result(algo, f"Running: {self._holding(algo)}", True)

    def _candles(self, algo: dict, instrument: str, naive_now) -> pd.DataFrame:
        kite = self.desk.kite
        if self.fetch is None and kite.connected and "get_historical_data" in kite.tools:
            # Five days covers indicators and daily pivots; weekly or monthly pivots need the whole period before.
            need = 0
            with contextlib.suppress(AlgoError):
                need = self._strategy(algo).history_days
            days = min(max(5, need), data.KITE_CHUNK[algo["interval"]])
            with contextlib.suppress(KiteError, KeyError, ValueError, TypeError):
                rows = kite.candles(instrument, algo["interval"], naive_now - timedelta(days=days), naive_now)
                frame = data.kite_frame(rows)
                if len(frame) >= 30:
                    return frame
        return data.candles(algo["symbol"], algo["interval"], fetch=self.fetch, max_age=20)

    # -- orders --------------------------------------------------------------

    def _send(self, algo: dict, side: str, reason: str, fallback_price: float | None = None) -> dict:
        cfg = self.desk.settings.all()
        live = algo["mode"] == "live"
        exchange, tradingsymbol = kite_instrument(algo["symbol"])
        reference, _ = self.desk.last_price(f"{exchange}:{tradingsymbol}")
        reference = reference or fallback_price
        if not reference:
            return self._result(algo, "Blocked: no price", False, f"{side} ({reason}): no price available.")
        buffer = cfg["limit_buffer_pct"] / 100
        price = market.round_to_tick(reference * (1 + buffer if side == "BUY" else 1 - buffer), side)
        quantity = int(algo["budget"] // price) if side == "BUY" else algo["position"]
        if quantity < 1:
            algo["queued"] = None
            return self._result(algo, "Running: budget too small", True,
                                f"{side} ({reason}): ₹{algo['budget']:,.0f} does not buy one share at ₹{price:,.2f}.")
        if side == "SELL" and algo["protect"]:
            # The GTT must go before the sell: left active, it could fire later and sell shares that are no longer
            # this algo's (another algo's, or your own, in the same stock).
            problem = self._drop_protection(algo)
            if problem == "triggered":
                self._check_protection(algo)                # the GTT has sold the position already
                algo["queued"] = None
                return self._result(algo, f"Running: {self._holding(algo)}", True)
            if problem:
                self._retry_at[algo["id"]] = self._now() + (RETRY if algo["interval"] == "1d" else timedelta(minutes=1))
                if algo["interval"] == "1d":
                    algo["queued"] = {"side": side, "reason": reason}
                return self._result(algo, "Blocked: target/stoploss GTT not cancelled", False,
                                    f"SELL ({reason}) held back: could not cancel GTT {algo['protect']} first "
                                    f"({problem}). Trying again.")
        session_open = market.session_status(self._now())["open"]
        order = {"exchange": exchange, "tradingsymbol": tradingsymbol, "side": side, "quantity": quantity,
                 "product": algo["product"], "order_type": "LIMIT", "price": price,
                 "variety": "regular" if not live or session_open else "amo"}
        try:
            placed = self.desk.auto_place(order, algo["mode"], f"algo:{algo['name']}")
        except OrderRejected as exc:
            needs_login = "log in" in str(exc).lower() or "login" in str(exc).lower()
            self._retry_at[algo["id"]] = self._now() + (RETRY if algo["interval"] == "1d" else timedelta(minutes=1))
            if algo["interval"] == "1d":
                algo["queued"] = {"side": side, "reason": reason}      # a daily signal is not lost: it retries
            return self._result(algo, "Blocked: Kite needs a login" if needs_login else "Blocked: order refused",
                                False, f"{side} {quantity} ({reason}) refused: {exc}")
        algo["queued"] = None
        if live:
            algo["pending"] = {"order_id": placed.get("order_id"), "side": side, "quantity": quantity,
                               "price": price, "variety": order["variety"],
                               "placed": self._now().strftime("%Y-%m-%d %H:%M")}
            return self._result(algo, "Running: order sent", True,
                                f"Sent {side} {quantity} at limit ₹{price:,.2f} ({order['variety']}, {reason}), "
                                f"order {placed.get('order_id')}. Not yet filled.")
        self._apply_fill(algo, side, quantity, placed["fill_price"])
        self._log(algo, f"Paper {side} {quantity} filled at ₹{placed['fill_price']:,.2f} ({reason}).")
        if side == "SELL" and not reason.endswith("signal"):
            self.desk.notifier.notify(f"Algo “{algo['name']}” closed its position", f"{reason} · paper", "warn")
        if side == "BUY":
            self._protect(algo)
        return self._result(algo, f"Running: {self._holding(algo)}", True)

    def _protect(self, algo: dict) -> None:
        """After a delivery buy, place the target and stoploss as a GTT."""
        if not positional(algo) or not algo["position"]:
            return
        stop, kind, goal = self._levels(algo)
        if algo.get("trail_stop") and (stop is None or algo["trail_stop"] > stop):
            stop = algo["trail_stop"]
        if stop is None and goal is None:
            return
        exchange, tradingsymbol = kite_instrument(algo["symbol"])
        target = market.round_to_tick(goal) if goal else 0
        stop = market.round_to_tick(stop) if stop else 0
        algo["stop_kind"], algo["stop_level"] = kind, stop or None
        gtt = {"exchange": exchange, "tradingsymbol": tradingsymbol, "side": "SELL", "quantity": algo["position"]}
        gtt.update({"kind": "oco", "target": target, "stop": stop} if target and stop
                   else {"kind": "single", "trigger": target or stop})
        try:
            placed = self.desk.auto_gtt(gtt, algo["mode"], f"algo:{algo['name']}")
        except OrderRejected as exc:
            self._log(algo, f"Could not place the target/stoploss GTT: {exc}. The position is unprotected.")
            return
        algo["protect"] = placed["order_id"]
        levels = " and ".join(part for part in (f"target ₹{target:,.2f}" if target else "",
                                                f"{kind} ₹{stop:,.2f}" if stop else "") if part)
        self._log(algo, f"GTT {placed['order_id']} placed: {levels}.")

    def _trail_daily(self, algo: dict, close: float) -> None:
        """Once a day, move a delivery position's stoploss up (trailing stoploss, stoploss to cost) behind the
        highest close. A stoploss only ever moves up."""
        rules = self.rules(algo)
        if not (rules.trail_every or rules.cost_after or algo["trail_pct"]):
            return
        algo["peak"] = max(algo.get("peak") or algo["average_price"], close)
        stop, kind, _ = self._levels(algo)
        if stop is None:
            return
        wanted = market.round_to_tick(stop)
        current = algo.get("trail_stop") or algo.get("stop_level") or 0
        if wanted <= current + market.TICK:
            return
        if algo["protect"]:
            problem = self._drop_protection(algo)
            if problem:
                # A second GTT next to the old one could sell the shares twice: keep the old one for now.
                self._log(algo, f"Stoploss not raised to ₹{wanted:,.2f}: GTT {algo['protect']} "
                                f"{'has triggered' if problem == 'triggered' else f'could not be cancelled ({problem})'}. "
                                f"Tried again with the next close.")
                return
        algo["trail_stop"] = wanted
        self._log(algo, f"Stoploss raised to ₹{wanted:,.2f} ({kind}; highest close ₹{algo['peak']:,.2f}).")
        self._protect(algo)

    def _drop_protection(self, algo: dict) -> str:
        """Cancel the algo's target/stoploss GTT. Returns "" once it is gone (cancelled now, or no longer listed or
        active), "triggered" when it has fired, else why it could not be cancelled; ``protect`` is cleared only
        when it is gone."""
        try:
            self.desk.cancel_gtt(algo["mode"], algo["protect"])
        except KiteError as exc:
            try:
                state = (self.desk.gtt_status(algo["mode"], algo["protect"]) or {}).get("status", "gone")
            except KiteError:
                return str(exc)
            if state == "triggered":
                return "triggered"
            if state == "active":
                return str(exc)
        algo["protect"] = None
        return ""

    def _check_protection(self, algo: dict) -> None:
        try:
            status = self.desk.gtt_status(algo["mode"], algo["protect"])
        except KiteError:
            return
        state = (status or {}).get("status", "gone")
        if state == "active":
            return
        if state == "triggered":
            leg = status.get("leg")
            fill = status.get("fill_price")
            stop = algo.get("stop_level")
            # Which side fired: the paper book says; for Kite, a fill (or price) at or under the stop means the stop.
            if leg not in ("stop", "target"):
                price = fill or self.desk.last_price("{}:{}".format(*kite_instrument(algo["symbol"])))[0]
                leg = "stop" if stop and (price is None or price <= stop * 1.01 or price < algo["average_price"]) else "target"
            where = f" at ₹{fill:,.2f}" if fill else ""
            note = "" if algo["mode"] == "paper" else " Check in Kite that its sell order filled."
            kind = algo.get("stop_kind") or "stoploss"
            self._log(algo, f"GTT {algo['protect']} triggered ({kind if leg == 'stop' else 'target'}){where}: "
                            f"position closed.{note}")
            if leg == "stop":
                self._stopped_out(algo, kind)
            else:
                algo["blocked"], algo["rearm"] = True, None
            algo.update(position=0, average_price=0.0, entry_day=None, entry_at=None)
        else:
            self._log(algo, f"GTT {algo['protect']} is {state}. The position is no longer protected by it.")
        algo["protect"] = None

    def _apply_fill(self, algo: dict, side: str, quantity: int, price: float) -> None:
        if side == "BUY":
            if not algo["position"]:
                algo["entry_day"] = self._now().strftime("%Y-%m-%d")
                algo["entry_at"] = self._now().strftime("%Y-%m-%d %H:%M:%S")
            total = algo["position"] + quantity
            algo["average_price"] = round((algo["position"] * algo["average_price"] + quantity * price) / total, 2)
            algo["position"] = total
            algo["peak"], algo["trail_stop"], algo["stop_level"] = price, None, None
        else:
            algo["position"] = max(algo["position"] - quantity, 0)
            if not algo["position"]:
                algo["average_price"], algo["entry_day"], algo["entry_at"] = 0.0, None, None

    def _reconcile(self, algo: dict) -> bool:
        """Settle a live order sent earlier. True once it is no longer pending."""
        pending = algo["pending"]
        row = next((o for o in self.desk.kite.orders()
                    if str(o.get("order_id")) == str(pending["order_id"])), None)
        status = str((row or {}).get("status", "")).upper()
        filled = int((row or {}).get("filled_quantity") or 0)
        placed = datetime.strptime(pending["placed"], "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)
        if row is None:
            # Kite lists only the current day's orders. An order from an earlier
            # day that is gone was not carried over; treat it as not filled.
            if pending["placed"][:10] == self._now().strftime("%Y-%m-%d"):
                return False
            self._log(algo, f"Order {pending['order_id']} is no longer listed by Kite. Assuming it did not "
                            f"fill. Check your holdings and correct the algo's position if it did.")
            algo["pending"] = None
            return True
        if status == "COMPLETE" or (status in ("CANCELLED", "REJECTED") and filled):
            quantity = filled or pending["quantity"]
            price = float(row.get("average_price") or pending["price"])
            self._apply_fill(algo, pending["side"], quantity, price)
            self._log(algo, f"Order {pending['order_id']}: {pending['side']} {quantity} filled at ₹{price:,.2f}.")
            algo["pending"] = None
            if pending["side"] == "BUY":
                self._protect(algo)
            return True
        if status in ("CANCELLED", "REJECTED"):
            reason = row.get("status_message") or "no reason given"
            self._log(algo, f"Order {pending['order_id']} {status.lower()} unfilled ({reason}). Position unchanged.")
            if pending["side"] == "BUY":
                algo["seen_flat"] = True
            algo["pending"] = None
            return True
        if algo["interval"] != "1d" and self._now() - placed > UNFILLED_AFTER:
            with contextlib.suppress(KiteError):
                self.desk.kite.cancel_order(pending["order_id"], pending.get("variety", "regular"))
                self._log(algo, f"Order {pending['order_id']} was still unfilled after "
                                f"{UNFILLED_AFTER.seconds // 60} minutes; cancel sent.")
        return False      # still open, or queued as AMO

    # -- scheduler ----------------------------------------------------------

    def due(self) -> list[str]:
        """The enabled algos with something to do now."""
        now = self._now()
        if not market.is_trading_day(now):
            return []
        today, clock = now.strftime("%Y-%m-%d"), now.time()
        out = []
        with self._lock:
            for algo in self._algos.values():
                if not (algo["enabled"] or algo.get("exit_requested")) or self._retry_at.get(algo["id"], now) > now:
                    continue
                if algo.get("exit_requested"):
                    if market.OPEN <= clock < market.CLOSE or not algo["position"]:
                        out.append(algo["id"])
                    continue
                if algo["interval"] != "1d":
                    # An intraday position left open after the close is reported; a positional one simply waits,
                    # once an order sent before the close is settled (the broker lists only the day's orders).
                    if (market.OPEN <= clock < market.CLOSE or (algo["position"] and not positional(algo))
                            or algo["pending"]):
                        out.append(algo["id"])
                elif (algo["queued"] or algo["pending"] or algo["protect"]) and (
                        market.OPEN <= clock < market.CLOSE or RUN_AFTER <= clock <= RUN_UNTIL) or RUN_AFTER <= clock <= RUN_UNTIL and algo["last_bar"] != today:
                    out.append(algo["id"])
        return out

    def _job(self, name: str, job) -> None:
        """Run one background job. A failure must not stop the others, and must not pass unseen: it is logged and
        the user is told, at most once an hour for the same job and error."""
        try:
            job()
        except Exception as exc:  # noqa: BLE001 - one job's failure must not stop the others
            logger.exception("desk job %s failed", name)
            self.desk.notifier.notify(f"{name} failed", f"{type(exc).__name__}: {exc}. It is tried again on the next "
                                      f"tick; until it works, it is not being done.", "bad",
                                      key=f"job-failed:{name}:{self._now():%Y-%m-%d %H}")

    def tick(self) -> None:
        self._job("Paper GTT check", self.desk.check_paper_gtts)
        self._job("Live fill sync", self.desk.sync_live_fills)
        self._job("Daily loss limit check", self.desk.check_loss_limit)
        self._job("Broker login check", self._watch_kite)
        for algo_id in self.due():
            try:
                outcome = self.evaluate(algo_id)
            except Exception as exc:  # noqa: BLE001 - one algo's failure must not stop the others
                logger.exception("algo %s failed", algo_id)
                name = self._algos.get(algo_id, {}).get("name", algo_id)
                self.desk.notifier.notify(f"Algo “{name}” hit an error", f"{type(exc).__name__}: {exc}. It is tried "
                                          f"again; check the algo and its position.", "bad",
                                          key=f"algo-error:{algo_id}:{self._now():%Y-%m-%d %H}")
                outcome = {"done": False}
            if not outcome["done"] and algo_id not in self._retry_at:
                daily = self._algos.get(algo_id, {}).get("interval") == "1d"
                self._retry_at[algo_id] = self._now() + (RETRY if daily else timedelta(minutes=1))
            elif outcome["done"] and self._retry_at.get(algo_id, self._now()) <= self._now():
                self._retry_at.pop(algo_id, None)
        for extra in self.extra_ticks:
            self._job(JOB_NAMES.get(getattr(extra, "__qualname__", ""), "A background job"), extra)

    def _watch_kite(self) -> None:
        """Tell the user, once a morning, that Kite has signed them out while live work is waiting on it."""
        now = self._now()
        if not market.is_trading_day(now) or not clock_time(8, 45) <= now.time() < market.CLOSE:
            return
        with self._lock:
            live = [a["name"] for a in self._algos.values() if a["enabled"] and a["mode"] == "live"]
        for names in self.extra_live:
            with contextlib.suppress(Exception):
                live += names()
        if not live or now - self._kite_checked < timedelta(minutes=10):
            return
        self._kite_checked = now
        kite, problem = self.desk.kite, None
        if not kite.connected:
            problem = "Kite is not connected."
        else:
            try:
                kite.profile()
            except KiteError as exc:
                problem = "Kite has signed you out." if exc.needs_login else None
        if problem:
            self.desk.notifier.notify("Log in to Kite", f"{problem} Live algos waiting: {', '.join(live)}. Open the desk → "
                                      f"Trade → Log in to Kite.", "bad", key=f"kite-login:{now:%Y-%m-%d}")

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()

        def loop():
            while not self._stop.wait(TICK_SECONDS):
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 - the clock must keep running
                    logger.exception("desk tick failed")

        self._thread = threading.Thread(target=loop, name="desk-algos", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()


_BLANK = {
    "interval": "1d", "product": "CNC", "target_pct": 0.0, "stop_loss_pct": 0.0, "trail_pct": 0.0,
    "peak": None, "trail_stop": None, "enabled": False, "exits": None, "rearm": None, "reentries_left": 0,
    "stop_kind": None, "stop_level": None,
    "position": 0, "average_price": 0.0, "seen_flat": False, "blocked": False,
    "pending": None, "queued": None, "protect": None,
    "last_bar": None, "last_signal": None, "status": "Stopped", "log": [],
    "exit_requested": False, "hold_until": None, "trade_type": None, "max_hold_days": 0, "entry_day": None,
    "entry_at": None,
}


def positional(algo: dict) -> bool:
    """Whether the algo holds across days (CNC). Day-candle algos always do."""
    return algo["interval"] == "1d" or algo.get("trade_type") == "positional"
