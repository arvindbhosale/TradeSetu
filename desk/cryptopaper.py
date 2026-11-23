"""Crypto paper trading: a virtual USDT balance with market and limit orders, positions, P&L and history.

Practice only. Nothing here can place a real order, on any exchange or broker. It is separate from the desk's
rupee paper book (``broker.PaperBook``) because crypto trades fractional coins around the clock in USDT and
needs limit orders that wait for their price.

- A market order fills at once at the last price; a buy limit at or above the price (a sell limit at or below)
  is marketable and fills at once too, at the last price.
- Any other limit order waits. The server checks waiting orders against the last price every few seconds
  and fills them at their limit price. A price that touches the limit between two checks can be missed.
- A waiting buy holds its cash, a waiting sell holds its coins, so nothing is spent twice.
- A buy can carry a stoploss and a target. Once it fills they guard the coins it bought: the stoploss sells at the
  last price when the price falls to it, the target sells at the target when the price rises to it, and whichever
  goes first cancels the other. They can be moved or removed, and added to coins already held. Coins they guard
  are held for them, so a manual sell cannot sell them twice.
- Every fill pays ``FEE`` (Binance's standard spot fee). Average cost excludes fees; total P&L includes them.
- History keeps the last ``MAX_TRADES`` fills. Reset deletes everything and starts again.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path

from .marketdata.crypto import BY_SYMBOL, pair

FEE = 0.001
MIN_ORDER = 1.0                  # USDT
MAX_TRADES = 2000
CHECK_SECONDS = 5
DEFAULT_BALANCE = 10_000.0
BALANCE_LIMITS = (100.0, 10_000_000.0)
EPS = 1e-9


class PaperError(ValueError):
    """An order the paper book refuses. The message is shown to the user."""


def _fresh(balance: float, now: float) -> dict:
    return {"start": balance, "cash": balance, "positions": {}, "orders": [], "trades": [], "realized": 0.0,
            "fees": 0.0, "created": now, "brackets": []}


def _positive(value, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise PaperError(f"{name} must be a number.") from None
    if not math.isfinite(number) or number <= 0:
        raise PaperError(f"{name} must be above zero.")
    return number


class CryptoPaper:
    def __init__(self, path: Path, prices, now=time.time, start_worker: bool = True):
        """``prices(symbols)`` -> {symbol: {"price": float} or {"error": str}}."""
        self.path, self.prices, self.now = Path(path), prices, now
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._start_worker = start_worker
        self.last_error = ""
        self._state = self._load()
        self._state.setdefault("brackets", [])
        if self._state["orders"] or self._state["brackets"]:
            self._ensure_worker()

    # -- storage -------------------------------------------------------------------------------------------
    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(state, dict) and {"cash", "positions", "orders", "trades"} <= set(state):
                return state
        except (OSError, ValueError):
            pass
        return _fresh(DEFAULT_BALANCE, self.now())

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self.path)

    # -- prices --------------------------------------------------------------------------------------------
    def _price(self, symbol: str) -> float:
        got = self.prices([symbol]).get(symbol) or {}
        if "price" not in got:
            raise PaperError(f"No live price for {symbol}, so the order was not placed. {got.get('error', '')}".strip())
        return float(got["price"])

    # -- orders --------------------------------------------------------------------------------------------
    def _reserved_cash(self) -> float:
        return sum(o["reserved"] for o in self._state["orders"] if o["side"] == "BUY")

    def _reserved_qty(self, symbol: str) -> float:
        return (sum(o["quantity"] for o in self._state["orders"] if o["side"] == "SELL" and o["symbol"] == symbol)
                + self._guarded(symbol))

    def _guarded(self, symbol: str) -> float:
        return sum(b["quantity"] for b in self._state["brackets"] if b["symbol"] == symbol)

    @staticmethod
    def _levels(raw: dict) -> tuple[float | None, float | None]:
        out = []
        for key, label in (("stop", "Stoploss"), ("target", "Target")):
            value = raw.get(key)
            out.append(None if value in (None, "", 0) else _positive(value, label))
        return out[0], out[1]

    @staticmethod
    def _check_levels(stop, target, price: float, what: str) -> None:
        if stop is not None and stop >= price:
            raise PaperError(f"The stoploss {stop:,.8g} must be below {what} {price:,.8g}.")
        if target is not None and target <= price:
            raise PaperError(f"The target {target:,.8g} must be above {what} {price:,.8g}.")

    def _add_bracket(self, symbol: str, quantity: float, stop, target, entry: float) -> dict:
        bracket = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "quantity": quantity, "stop": stop, "target": target,
                   "entry": entry, "created": self.now()}
        self._state["brackets"].append(bracket)
        return bracket

    def _held(self, symbol: str) -> float:
        return self._state["positions"].get(symbol, {}).get("quantity", 0.0)

    def place(self, raw: dict) -> dict:
        symbol = pair(str(raw.get("symbol", "")))[0]
        side = str(raw.get("side", "")).upper()
        kind = str(raw.get("type", "MARKET")).upper()
        if side not in ("BUY", "SELL"):
            raise PaperError("Choose Buy or Sell.")
        if kind not in ("MARKET", "LIMIT"):
            raise PaperError("Order type must be Market or Limit.")
        quantity = round(_positive(raw.get("quantity"), "Quantity"), 8)
        if quantity <= 0:
            raise PaperError("Quantity is too small (8 decimal places at most).")
        limit = _positive(raw.get("price"), "Limit price") if kind == "LIMIT" else None
        stop, target = self._levels(raw)
        if side == "SELL" and (stop or target):
            raise PaperError("Stoploss and target are for buys. A sell sells coins you hold.")
        with self._lock:
            last = self._price(symbol)
            marketable = kind == "MARKET" or (side == "BUY" and limit >= last) or (side == "SELL" and limit <= last)
            price = last if marketable else limit
            self._check_levels(stop, target, price, "the buy price")
            notional = quantity * price
            if notional < MIN_ORDER:
                raise PaperError(f"The order is worth {notional:.2f} USDT; the smallest is {MIN_ORDER:.0f} USDT.")
            if side == "BUY":
                need = notional * (1 + FEE)
                free = self._state["cash"] - self._reserved_cash()
                if need > free + EPS:
                    raise PaperError(f"Not enough paper cash: this needs {need:,.2f} USDT with the fee; "
                                     f"{free:,.2f} USDT is free.")
            else:
                free = self._held(symbol) - self._reserved_qty(symbol)
                if quantity > free + EPS:
                    guarded = self._guarded(symbol)
                    raise PaperError(f"You hold {free:.8g} {BY_SYMBOL[symbol][1]} free to sell; this sells {quantity:.8g}. "
                                     + (f"{guarded:.8g} are held for your stoploss and target: remove them first to sell "
                                        f"those. " if guarded > EPS else "") + "Paper trading does not short.")
            if marketable:
                trade = self._fill(symbol, side, quantity, price, kind.lower())
                bracket = self._add_bracket(symbol, quantity, stop, target, price) if stop or target else None
                self._save()
                self._ensure_worker()
                return {"filled": True, "trade": trade, "bracket": bracket}
            order = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "side": side, "quantity": quantity, "price": limit,
                     "reserved": quantity * limit * (1 + FEE) if side == "BUY" else 0.0, "created": self.now(),
                     "stop": stop, "target": target}
            self._state["orders"].append(order)
            self._save()
        self._ensure_worker()
        return {"filled": False, "order": order}

    def cancel(self, order_id: str) -> dict:
        with self._lock:
            orders = self._state["orders"]
            found = next((o for o in orders if o["id"] == order_id), None)
            if found is None:
                raise PaperError("That order is no longer open (it may have filled).")
            orders.remove(found)
            self._save()
            return found

    def _fill(self, symbol: str, side: str, quantity: float, price: float, kind: str) -> dict:
        state, notional = self._state, quantity * price
        fee = notional * FEE
        position = state["positions"].get(symbol, {"quantity": 0.0, "avg": 0.0})
        realized = None
        if side == "BUY":
            total = position["quantity"] + quantity
            position["avg"] = (position["quantity"] * position["avg"] + notional) / total
            position["quantity"] = total
            state["cash"] -= notional + fee
        else:
            realized = (price - position["avg"]) * quantity
            position["quantity"] -= quantity
            state["cash"] += notional - fee
            state["realized"] += realized
        state["fees"] += fee
        if position["quantity"] <= EPS:
            state["positions"].pop(symbol, None)
        else:
            state["positions"][symbol] = position
        trade = {"id": uuid.uuid4().hex[:12], "time": self.now(), "symbol": symbol, "side": side, "quantity": quantity,
                 "price": price, "fee": fee, "realized": realized, "kind": kind}
        state["trades"].append(trade)
        del state["trades"][:-MAX_TRADES]
        return trade

    def check(self) -> list[dict]:
        """Fill the waiting orders whose limit the last price has reached. Returns the fills."""
        with self._lock:
            orders, brackets = list(self._state["orders"]), list(self._state["brackets"])
            if not orders and not brackets:
                return []
            prices = self.prices(sorted({o["symbol"] for o in orders} | {b["symbol"] for b in brackets}))
            fills, self.last_error = [], ""
            for order in orders:
                got = prices.get(order["symbol"]) or {}
                if "price" not in got:
                    self.last_error = got.get("error", "no price")
                    continue
                last = got["price"]
                if (order["side"] == "BUY" and last <= order["price"]) or (order["side"] == "SELL" and last >= order["price"]):
                    self._state["orders"].remove(order)
                    fills.append(self._fill(order["symbol"], order["side"], order["quantity"], order["price"], "limit"))
                    if order.get("stop") or order.get("target"):
                        self._add_bracket(order["symbol"], order["quantity"], order.get("stop"), order.get("target"),
                                          order["price"])
            for bracket in brackets:
                got = prices.get(bracket["symbol"]) or {}
                if "price" not in got:
                    self.last_error = got.get("error", "no price")
                    continue
                last = got["price"]
                if bracket["stop"] is not None and last <= bracket["stop"]:
                    price, kind = last, "stoploss"
                elif bracket["target"] is not None and last >= bracket["target"]:
                    price, kind = bracket["target"], "target"
                else:
                    continue
                self._state["brackets"].remove(bracket)
                quantity = min(bracket["quantity"], self._held(bracket["symbol"]))
                if quantity > EPS:
                    fills.append(self._fill(bracket["symbol"], "SELL", quantity, price, kind))
            if fills:
                self._save()
            return fills

    def _ensure_worker(self) -> None:
        if not self._start_worker or (self._worker and self._worker.is_alive()):
            return

        def run():
            while True:
                time.sleep(CHECK_SECONDS)
                try:
                    self.check()
                except Exception as exc:  # noqa: BLE001 - keep checking; the view shows the reason
                    self.last_error = f"check failed: {exc}"
                with self._lock:
                    if not self._state["orders"] and not self._state["brackets"]:
                        return

        self._worker = threading.Thread(target=run, name="crypto-paper", daemon=True)
        self._worker.start()

    # -- stoploss and target -------------------------------------------------------------------------------------
    def protect(self, raw: dict) -> dict:
        """Put a stoploss and/or target on coins already held (all the free coins unless a quantity is given)."""
        symbol = pair(str(raw.get("symbol", "")))[0]
        stop, target = self._levels(raw)
        if not stop and not target:
            raise PaperError("Give a stoploss, a target, or both.")
        with self._lock:
            free = self._held(symbol) - self._reserved_qty(symbol)
            quantity = round(_positive(raw.get("quantity"), "Quantity"), 8) if raw.get("quantity") else round(free, 8)
            if free <= EPS:
                raise PaperError(f"No free {BY_SYMBOL[symbol][1]} to protect: buy some first, or move the stoploss and "
                                 f"target you already have.")
            if quantity > free + EPS:
                raise PaperError(f"You hold {free:.8g} {BY_SYMBOL[symbol][1]} free; this protects {quantity:.8g}.")
            self._check_levels(stop, target, self._price(symbol), "the last price")
            avg = self._state["positions"][symbol]["avg"]
            bracket = self._add_bracket(symbol, quantity, stop, target, avg)
            self._save()
        self._ensure_worker()
        return bracket

    def change_bracket(self, bracket_id: str, raw: dict) -> dict:
        """Move a stoploss and target. Both are given as they should be after the change; a missing one is removed."""
        stop, target = self._levels(raw)
        if not stop and not target:
            raise PaperError("Keep a stoploss or a target, or remove both with Remove.")
        with self._lock:
            bracket = next((b for b in self._state["brackets"] if b["id"] == bracket_id), None)
            if bracket is None:
                raise PaperError("That stoploss and target are no longer open (they may have sold).")
            self._check_levels(stop, target, self._price(bracket["symbol"]), "the last price")
            bracket.update(stop=stop, target=target)
            self._save()
            return dict(bracket)

    def cancel_bracket(self, bracket_id: str) -> dict:
        with self._lock:
            bracket = next((b for b in self._state["brackets"] if b["id"] == bracket_id), None)
            if bracket is None:
                raise PaperError("That stoploss and target are no longer open (they may have sold).")
            self._state["brackets"].remove(bracket)
            self._save()
            return bracket

    def reset(self, balance=DEFAULT_BALANCE) -> dict:
        balance = _positive(balance, "Starting balance")
        low, high = BALANCE_LIMITS
        if not low <= balance <= high:
            raise PaperError(f"Starting balance must be between {low:,.0f} and {high:,.0f} USDT.")
        with self._lock:
            self.path.unlink(missing_ok=True)
            self._state = _fresh(balance, self.now())
            self._save()
        return self.view()

    # -- the view ------------------------------------------------------------------------------------------
    def view(self) -> dict:
        """Balances, positions at the last price, open orders and recent fills. A coin without a price shows its
        reason and leaves the totals blank rather than counting it as zero."""
        try:
            self.check()
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"check failed: {exc}"
        with self._lock:
            state = json.loads(json.dumps(self._state))
        held = list(state["positions"])
        prices = self.prices(held) if held else {}
        positions, value, missing = [], 0.0, []
        for symbol, p in sorted(state["positions"].items()):
            got = prices.get(symbol) or {}
            last = got.get("price")
            row = {"symbol": symbol, "coin": BY_SYMBOL[symbol][1], "quantity": p["quantity"], "avg": p["avg"],
                   "last": last, "error": got.get("error")}
            if last is None:
                missing.append(symbol)
            else:
                row["value"] = p["quantity"] * last
                row["unrealized"] = (last - p["avg"]) * p["quantity"]
                row["unrealized_pct"] = (last / p["avg"] - 1) * 100 if p["avg"] else None
                value += row["value"]
            positions.append(row)
        equity = None if missing else state["cash"] + value
        reserved = sum(o["reserved"] for o in state["orders"] if o["side"] == "BUY")
        return {"start": state["start"], "cash": state["cash"], "free_cash": state["cash"] - reserved,
                "positions_value": None if missing else value, "equity": equity,
                "pnl": None if equity is None else equity - state["start"],
                "pnl_pct": None if equity is None else (equity / state["start"] - 1) * 100,
                "realized": state["realized"], "fees": state["fees"], "positions": positions,
                "orders": sorted(state["orders"], key=lambda o: o["created"], reverse=True),
                "brackets": sorted(state.get("brackets", []), key=lambda b: b["created"], reverse=True),
                "trades": list(reversed(state["trades"][-200:])), "trade_count": len(state["trades"]),
                "missing_prices": missing, "check_error": self.last_error or None,
                "fee_pct": FEE * 100, "min_order": MIN_ORDER, "max_trades": MAX_TRADES, "check_seconds": CHECK_SECONDS}
