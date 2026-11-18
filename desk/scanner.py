"""Breakout scanner: find price-volume breakouts in a list of stocks and trade them with GTTs.

The setup, on daily candles:

1. **Breakout candle.** Volume at least ``volume_multiple`` times the average of
   the ``lookback`` days before it, and a close above the highest high of
   those days.
2. **Holding the breakout.** Every close since stays within ``tolerance_pct``
   of the breakout candle's high: the stock is resting there, not running
   away and not falling apart.
3. **Entry.** A buy GTT at the breakout candle's low, waiting for a pullback.
4. **Risk.** Once that buy triggers, a stoploss GTT ``stop_loss_pct`` below
   the entry (and a target, when one is set).

The scanner remembers every setup it has acted on, keyed by stock and breakout
date, so running it again tomorrow does not place the same order twice.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
from pathlib import Path

import pandas as pd

from tradingagents.dataflows.symbols import kite_instrument, normalize_symbol, safe_ticker_component

from . import data, market
from .broker import Desk, OrderRejected
from .kite import KiteError
from .settings import home

logger = logging.getLogger(__name__)

# Large caps from the Nifty 100 (NSE symbols) that the price feed serves. Index membership changes twice a year;
# edit the list on the Scanner tab. A symbol the price feed does not know is
# reported and skipped.
NIFTY_100 = ["ADANIENT", "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO", "BAJFINANCE", "BAJAJFINSV", "BEL", "BHARTIARTL", "CIPLA", "COALINDIA", "DRREDDY", "EICHERMOT", "ETERNAL", "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO", "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY", "ITC", "JIOFIN", "JSWSTEEL", "KOTAKBANK", "LT", "M&M", "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID", "RELIANCE", "SBILIFE", "SBIN", "SHRIRAMFIN", "SUNPHARMA", "TATACONSUM", "TATASTEEL", "TCS", "TECHM", "TITAN", "TRENT", "ULTRACEMCO", "WIPRO", "ABB", "ADANIENSOL", "ADANIGREEN", "ADANIPOWER", "AMBUJACEM", "BAJAJHLDNG", "BANKBARODA", "BPCL", "BRITANNIA", "BOSCHLTD", "CANBK", "CGPOWER", "CHOLAFIN", "DABUR", "DIVISLAB", "DLF", "DMART", "GAIL", "GODREJCP", "HAL", "HAVELLS", "HYUNDAI", "ICICIGI", "INDHOTEL", "INDIGO", "IOC", "IRFC", "JINDALSTEL", "JSWENERGY", "LICI", "LODHA", "MOTHERSON", "NAUKRI", "PFC", "PIDILITIND", "PNB", "RECLTD", "SHREECEM", "SIEMENS", "TATAPOWER", "TORNTPHARM", "TVSMOTOR", "UNITDSPR", "VBL", "VEDL", "ZYDUSLIFE"]

DEFAULTS = {"lookback": 20, "volume_multiple": 3.0, "tolerance_pct": 1.0, "max_age": 20,
            "stop_loss_pct": 5.0, "target_pct": 0.0}
LIMITS = {"lookback": (5, 100), "volume_multiple": (1.0, 20.0), "tolerance_pct": (0.1, 10.0),
          "max_age": (1, 60), "stop_loss_pct": (0.0, 50.0), "target_pct": (0.0, 200.0)}
MAX_UNIVERSE = 250
OPEN_STATES = ("buy_active", "holding")


def params_of(raw: dict | None) -> dict:
    out = {}
    for key, default in DEFAULTS.items():
        value = (raw or {}).get(key, default)
        lo, hi = LIMITS[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not lo <= value <= hi:
            raise ValueError(f"{key.replace('_', ' ')} must be between {lo:g} and {hi:g}")
        out[key] = int(value) if isinstance(default, int) else float(value)
    return out


def universe_of(raw) -> list[str]:
    """A clean list of canonical symbols from text or a list."""
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list) or not raw:
        raise ValueError("Give at least one symbol to scan")
    if len(raw) > MAX_UNIVERSE:
        raise ValueError(f"At most {MAX_UNIVERSE} symbols in one scan")
    out = []
    for item in raw:
        try:
            symbol = safe_ticker_component(normalize_symbol(str(item)))
        except ValueError:
            raise ValueError(f"“{str(item)[:30]}” is not a valid symbol") from None
        if not kite_instrument(symbol):
            raise ValueError(f"{symbol} is not an NSE/BSE stock")
        if symbol not in out:
            out.append(symbol)
    return out


def find_setup(bars: pd.DataFrame, params: dict) -> dict | None:
    """The most recent breakout in ``bars`` that is still holding, or None."""
    lookback = params["lookback"]
    if len(bars) < lookback + 2:
        return None
    volume = bars["Volume"].astype(float)
    average = volume.rolling(lookback).mean().shift(1)
    prior_high = bars["High"].rolling(lookback).max().shift(1)
    breakout = (volume >= params["volume_multiple"] * average) & (bars["Close"] > prior_high)
    recent = breakout.iloc[-params["max_age"] - 1:]
    hits = recent[recent].index
    if len(hits) == 0:
        return None
    day = hits[-1]
    candle = bars.loc[day]
    after = bars.loc[bars.index > day, "Close"]
    band = params["tolerance_pct"] / 100
    high = float(candle["High"])
    if len(after) and not ((after >= high * (1 - band)) & (after <= high * (1 + band))).all():
        return None
    low = float(candle["Low"])
    return {
        "breakout_date": day.strftime("%Y-%m-%d"), "age": int(len(after)),
        "breakout_high": round(high, 2), "breakout_low": round(low, 2),
        "breakout_close": round(float(candle["Close"]), 2),
        "volume_multiple": round(float(volume.loc[day] / average.loc[day]), 1),
        "last_close": round(float(bars["Close"].iloc[-1]), 2),
        "last_date": bars.index[-1].strftime("%Y-%m-%d"),
        "buy_trigger": market.round_to_tick(low),
        "stop_loss": market.round_to_tick(low * (1 - params["stop_loss_pct"] / 100)) if params["stop_loss_pct"] else None,
        "target": market.round_to_tick(low * (1 + params["target_pct"] / 100)) if params["target_pct"] else None,
    }


class Scanner:
    def __init__(self, desk: Desk, path: Path | None = None, fetch=None, now=market.now_ist):
        self.desk, self.fetch, self._now = desk, fetch, now
        self.path = path or home() / "scanner.json"
        self._lock = threading.RLock()
        self._state = {"universe": [normalize_symbol(f"NSE:{s}") for s in NIFTY_100], "params": dict(DEFAULTS),
                       "last_scan": None, "live": False, "trades": {}}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._state.update({k: stored[k] for k in self._state if k in stored})

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def view(self) -> dict:
        with self._lock:
            state = json.loads(json.dumps(self._state))
        state["trades"] = sorted(state["trades"].values(), key=lambda t: t["placed"], reverse=True)
        state["defaults"] = dict(DEFAULTS)
        return state

    def configure(self, universe=None, params=None) -> dict:
        with self._lock:
            if universe is not None:
                self._state["universe"] = universe_of(universe)
            if params is not None:
                self._state["params"] = params_of(params)
            self._save()
        return self.view()

    def reset_universe(self) -> dict:
        with self._lock:
            self._state["universe"] = [normalize_symbol(f"NSE:{s}") for s in NIFTY_100]
            self._save()
        return self.view()

    # -- scan ----------------------------------------------------------------

    def scan(self, progress=None) -> dict:
        """Scan the whole universe. Slow the first time (one download per stock)."""
        with self._lock:
            universe, params = list(self._state["universe"]), dict(self._state["params"])
        signals, failed = [], []
        today = self._now().strftime("%Y-%m-%d")
        for number, symbol in enumerate(universe, 1):
            if progress:
                progress(number, len(universe), symbol)
            try:
                bars = data.history(symbol, fetch=self.fetch)
            except data.NoData as exc:
                failed.append({"symbol": symbol, "reason": str(exc)[:140]})
                continue
            if bars.index[-1].strftime("%Y-%m-%d") == today and self._now().time() < market.CLOSE:
                bars = bars.iloc[:-1]            # today's candle is not finished
            setup = find_setup(bars, params)
            if setup:
                signals.append({"symbol": symbol, **setup})
        signals.sort(key=lambda s: (s["age"], s["symbol"]))
        with self._lock:
            placed = self._state["trades"]
            for signal in signals:
                trade = placed.get(f"{signal['symbol']}|{signal['breakout_date']}")
                signal["state"] = trade["state"] if trade else "new"
            self._state["last_scan"] = {"time_ist": self._now().strftime("%Y-%m-%d %H:%M"), "params": params,
                                        "scanned": len(universe), "signals": signals, "failed": failed}
            self._save()
            return json.loads(json.dumps(self._state["last_scan"]))

    # -- orders --------------------------------------------------------------

    def _quantity(self, trigger: float, mode: str) -> int:
        cfg = self.desk.settings.all()
        capital = self.desk._live_cash() if mode == "live" else self.desk.paper.snapshot()["capital"]
        if not capital:
            return 0
        return int(min(cfg["position_pct"] / 100 * capital, cfg["max_order_value"]) // trigger)

    def place(self, mode: str, keys: list[str] | None = None) -> dict:
        """Place a buy GTT for every signal of the last scan not already acted on."""
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        with self._lock:
            scan = self._state["last_scan"]
            if not scan:
                raise ValueError("Run a scan first.")
            results = []
            for signal in scan["signals"]:
                key = f"{signal['symbol']}|{signal['breakout_date']}"
                if keys is not None and key not in keys:
                    continue
                existing = self._state["trades"].get(key)
                if existing:
                    results.append({"key": key, "symbol": signal["symbol"], "outcome": "already placed",
                                    "state": existing["state"]})
                    continue
                exchange, tradingsymbol = kite_instrument(signal["symbol"])
                quantity = self._quantity(signal["buy_trigger"], mode)
                if quantity < 1:
                    results.append({"key": key, "symbol": signal["symbol"], "outcome": "skipped",
                                    "reason": "your position size and order limits do not buy one share"})
                    continue
                try:
                    placed = self.desk.auto_gtt({"exchange": exchange, "tradingsymbol": tradingsymbol, "side": "BUY",
                                                 "quantity": quantity, "kind": "single",
                                                 "trigger": signal["buy_trigger"], "limit": signal["buy_trigger"]},
                                                mode, "scanner")
                except OrderRejected as exc:
                    results.append({"key": key, "symbol": signal["symbol"], "outcome": "refused", "reason": str(exc)})
                    continue
                self._state["trades"][key] = {
                    "key": key, "symbol": signal["symbol"], "breakout_date": signal["breakout_date"], "mode": mode,
                    "quantity": quantity, "buy_trigger": signal["buy_trigger"], "stop_loss": signal["stop_loss"],
                    "target": signal["target"], "buy_gtt": placed["order_id"], "exit_gtt": None,
                    "state": "buy_active", "placed": self._now().strftime("%Y-%m-%d %H:%M"),
                    "note": "Buy GTT active; stoploss follows once it triggers."}
                signal["state"] = "buy_active"
                results.append({"key": key, "symbol": signal["symbol"], "outcome": "placed",
                                "gtt": placed["order_id"], "quantity": quantity})
            self._save()
            return {"results": results, "mode": mode}

    def set_live(self, live: bool) -> dict:
        with self._lock:
            self._state["live"] = bool(live)
            self._save()
        return self.view()

    def cancel(self, key: str) -> dict:
        """Cancel a setup's open GTTs. Shares already bought are not sold."""
        with self._lock:
            trade = self._state["trades"].get(key)
            if not trade:
                raise ValueError("no such scanner trade")
            for field in ("buy_gtt", "exit_gtt"):
                if trade[field] and trade["state"] in OPEN_STATES:
                    with contextlib.suppress(KiteError):
                        self.desk.cancel_gtt(trade["mode"], trade[field])
            held = trade["state"] == "holding"
            trade.update(state="cancelled", note="Cancelled by hand." + (
                " The shares bought are still held and no longer have a stoploss." if held else ""))
            self._save()
        return self.view()

    def forget(self, key: str) -> dict:
        with self._lock:
            trade = self._state["trades"].get(key)
            if not trade:
                raise ValueError("no such scanner trade")
            if trade["state"] in OPEN_STATES:
                raise ValueError("Cancel it first: it still has a GTT open.")
            del self._state["trades"][key]
            self._save()
        return self.view()

    # -- following the orders ------------------------------------------------

    def tick(self) -> None:
        """Follow every open setup: place the stoploss once the buy triggers, note exits."""
        with self._lock:
            if not self._state["live"]:
                return
            changed = False
            for trade in self._state["trades"].values():
                if trade["state"] not in OPEN_STATES:
                    continue
                if trade["mode"] == "live" and not self.desk.kite.connected:
                    continue
                try:
                    changed |= self._follow(trade)
                except KiteError as exc:
                    note = "Kite needs a login." if exc.needs_login else f"Kite error: {exc}"
                    if trade["note"] != note:
                        trade["note"], changed = note, True
            if changed:
                self._save()

    def _follow(self, trade: dict) -> bool:
        mode = trade["mode"]
        if trade["state"] == "buy_active":
            status = self.desk.gtt_status(mode, trade["buy_gtt"])
            state = (status or {}).get("status", "gone")
            if state == "active":
                return False
            if state != "triggered":
                trade.update(state="buy_" + state, note=f"The buy GTT is {state}; nothing was bought.")
                return True
            trade["entry"] = status.get("fill_price") or trade["buy_trigger"]
            trade["state"] = "holding"
            if not (trade["stop_loss"] or trade["target"]):
                trade["note"] = "Bought. No stoploss or target was set."
                return True
            exchange, tradingsymbol = kite_instrument(trade["symbol"])
            gtt = {"exchange": exchange, "tradingsymbol": tradingsymbol, "side": "SELL", "quantity": trade["quantity"]}
            gtt.update({"kind": "oco", "target": trade["target"], "stop": trade["stop_loss"]}
                       if trade["stop_loss"] and trade["target"]
                       else {"kind": "single", "trigger": trade["stop_loss"] or trade["target"]})
            try:
                placed = self.desk.auto_gtt(gtt, mode, "scanner")
                trade["exit_gtt"] = placed["order_id"]
                trade["note"] = "Bought; stoploss GTT placed." + (
                    "" if mode == "paper" else " Check in Kite that the buy order filled.")
            except OrderRejected as exc:
                trade["note"] = f"Bought, but the stoploss GTT was refused: {exc}. The position is unprotected."
            return True
        if trade["exit_gtt"]:
            status = self.desk.gtt_status(mode, trade["exit_gtt"])
            state = (status or {}).get("status", "gone")
            if state == "active":
                return False
            if state == "triggered":
                leg = status.get("leg") or "stoploss or target"
                trade.update(state="closed", note=f"Exit GTT triggered ({leg}). Position closed." + (
                    "" if mode == "paper" else " Check in Kite that the sell order filled."))
            else:
                trade.update(state="holding_unprotected", note=f"The exit GTT is {state}. Shares are still held, "
                                                               f"with no stoploss.")
            return True
        return False
