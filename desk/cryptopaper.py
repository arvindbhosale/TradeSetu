"""Crypto paper trading: a virtual USDT balance with market and limit orders, positions, P&L and history.

Practice only. Nothing here can place a real order, on any exchange or broker. It is separate from the desk's
rupee paper book (``broker.PaperBook``) because crypto trades fractional coins around the clock in USDT and
needs limit orders that wait for their price.

- A market order fills at once at the last price; a buy limit at or above the price (a sell limit at or below)
  is marketable and fills at once too, at the last price.
- Any other limit order waits. The server checks waiting orders against the last price every few seconds
  and fills them at their limit price. A price that touches the limit between two checks can be missed.
- A waiting buy holds its cash, a waiting sell holds its coins, so nothing is spent twice.
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
            "fees": 0.0, "created": now}


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
        if self._state["orders"]:
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
        return sum(o["quantity"] for o in self._state["orders"] if o["side"] == "SELL" and o["symbol"] == symbol)

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
        with self._lock:
            last = self._price(symbol)
            marketable = kind == "MARKET" or (side == "BUY" and limit >= last) or (side == "SELL" and limit <= last)
            price = last if marketable else limit
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
                    raise PaperError(f"You hold {free:.8g} {BY_SYMBOL[symbol][1]} free to sell; this sells {quantity:.8g}. "
                                     f"Paper trading does not short.")
            if marketable:
                trade = self._fill(symbol, side, quantity, price, kind.lower())
                self._save()
                return {"filled": True, "trade": trade}
            order = {"id": uuid.uuid4().hex[:12], "symbol": symbol, "side": side, "quantity": quantity, "price": limit,
                     "reserved": quantity * limit * (1 + FEE) if side == "BUY" else 0.0, "created": self.now()}
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
            orders = list(self._state["orders"])
            if not orders:
                return []
            prices = self.prices(sorted({o["symbol"] for o in orders}))
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
                    if not self._state["orders"]:
                        return

        self._worker = threading.Thread(target=run, name="crypto-paper", daemon=True)
        self._worker.start()

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
                "trades": list(reversed(state["trades"][-200:])), "trade_count": len(state["trades"]),
                "missing_prices": missing, "check_error": self.last_error or None,
                "fee_pct": FEE * 100, "min_order": MIN_ORDER, "max_trades": MAX_TRADES, "check_seconds": CHECK_SECONDS}
