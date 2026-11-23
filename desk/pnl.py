"""Profit and loss from the desk's own fills.

Every confirmed fill is written to a ledger: paper fills as they happen, live
fills from Kite's trade book for orders this desk placed. P&L is worked out
from that ledger by average cost, per account (paper or live), per source (a
named algo, the scanner, or manual) and per stock.

What it does not see: trades placed in Kite directly, and the order a
triggered live GTT sends (Kite gives it a new order id). Kite's own console
is the authority for the live account; this is the desk's view of its work.
"""

from __future__ import annotations

import contextlib
import json
import threading
from collections import defaultdict
from pathlib import Path

from . import market, mcx
from .charges import commodity_charges, futures_charges, order_charges
from .settings import home


def trade_key(mode: str, source: str, exchange: str, symbol: str, opened_at: str) -> str:
    """Names one trade: a position from the fill that opened it until it is flat again. Partial exits share it."""
    return f"{mode}|{source}|{exchange}|{symbol}|{opened_at}"


class Ledger:
    def __init__(self, path: Path | None = None, now=market.now_ist):
        self.path = path or home() / "fills.jsonl"
        self._now = now
        self._lock = threading.RLock()
        self._seen: set[str] = set()
        for fill in self.fills():
            if fill.get("trade_id"):
                self._seen.add(str(fill["trade_id"]))

    def add(self, mode: str, source: str, exchange: str, tradingsymbol: str, side: str, quantity: int,
            price: float, product: str = "CNC", charges: float | None = None, trade_id: str | None = None,
            time_ist: str | None = None, short: bool = False) -> bool:
        """Record one fill. A ``trade_id`` already recorded is ignored. ``short`` marks an intraday short sale of
        shares, which opens a position below zero like an F&O sale."""
        if quantity <= 0 or price <= 0:
            return False
        with self._lock:
            if trade_id is not None:
                if str(trade_id) in self._seen:
                    return False
                self._seen.add(str(trade_id))
            factor = 1.0
            if exchange == mcx.EXCHANGE:
                try:
                    factor = mcx.multiplier(exchange, tradingsymbol)
                except ValueError:
                    return False
            if charges is None:
                with contextlib.suppress(ValueError):
                    if exchange == mcx.EXCHANGE:
                        charges = commodity_charges(side, quantity, price, factor, not tradingsymbol.endswith("FUT")).total
                    elif exchange in ("NFO", "BFO") and tradingsymbol.endswith("FUT"):
                        charges = futures_charges(side, quantity, price, exchange).total
                    else:
                        charges = order_charges(side, quantity, price, product if product in ("CNC", "MIS") else "CNC",
                                                exchange).total
            entry = {"time_ist": time_ist or self._now().strftime("%Y-%m-%d %H:%M:%S"), "mode": mode,
                     "source": source or "manual", "exchange": exchange, "tradingsymbol": tradingsymbol,
                     "side": side, "quantity": int(quantity), "price": round(float(price), 2), "product": product,
                     "charges": round(float(charges or 0), 2), "trade_id": trade_id}
            if short:
                entry["short"] = True
            if factor != 1.0:
                entry["multiplier"] = factor      # MCX: quantity is in lots; rupees = price x lots x multiplier
            with open(self.path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry) + "\n")
            return True

    def fills(self) -> list[dict]:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        out = []
        for line in lines:
            with contextlib.suppress(json.JSONDecodeError):
                out.append(json.loads(line))
        return out

    # -- reports -------------------------------------------------------------

    def _walk(self, mode: str) -> tuple[dict, dict, dict, list]:
        """Every fill of one account by average cost: the books (per source and instrument), the per-source
        totals, realized P&L by day, and the closed trades."""
        books: dict[tuple, dict] = {}
        sources: dict[str, dict] = defaultdict(lambda: {"realized": 0.0, "charges": 0.0, "trades": 0, "wins": 0,
                                                        "fills": 0, "turnover": 0.0})
        days: dict[str, float] = defaultdict(float)
        closed = []
        for fill in sorted((f for f in self.fills() if f.get("mode") == mode), key=lambda f: f["time_ist"]):
            # A chart order's exits (its GTT, stoploss or target order) close the position its manual entry opened.
            source = "manual" if str(fill["source"]).startswith("chart:") else fill["source"]
            day = fill["time_ist"][:10]
            key = (source, fill["exchange"], fill["tradingsymbol"])
            factor = fill.get("multiplier") or 1
            book = books.setdefault(key, {"quantity": 0, "cost": 0.0, "opened": day, "opened_at": fill["time_ist"],
                                          "factor": factor, "realized": 0.0,
                                          "charges": 0.0, "bought": 0, "buy_value": 0.0, "sold": 0, "sell_value": 0.0,
                                          "fills": []})
            lots = fill["quantity"]
            if fill["side"] == "BUY":
                book["bought"] += lots
                book["buy_value"] += lots * fill["price"]
            else:
                book["sold"] += lots
                book["sell_value"] += lots * fill["price"]
            book["charges"] += fill["charges"]
            book["fills"].append({"time_ist": fill["time_ist"], "side": fill["side"], "quantity": lots,
                                  "price": fill["price"], "charges": fill["charges"]})
            stats = sources[source]
            stats["fills"] += 1
            stats["charges"] += fill["charges"]
            stats["turnover"] += fill["quantity"] * fill["price"] * factor
            # A book is signed: above zero long, below zero short (F&O, or an intraday short sale of shares marked
            # ``short``; any other sale of shares the desk never recorded buying is not the desk's trade). ``cost`` is the quantity times the average entry price,
            # with entry charges in it, so it is negative for a short: the proceeds less the sell charges.
            # MCX quantities are lots; the book counts price units (lots x multiplier) so every sum below is rupees.
            quantity, price, charge = fill["quantity"] * factor, fill["price"], fill["charges"]
            buying = fill["side"] == "BUY"
            held = book["quantity"]
            closing = min(quantity, -held if buying else held) if (held < 0 if buying else held > 0) else 0
            if closing:
                average = book["cost"] / held
                part = charge * closing / quantity
                profit = (average - price) * closing - part if buying else (price - average) * closing - part
                book["quantity"] = held + closing if buying else held - closing
                book["cost"] = average * book["quantity"]
                stats["realized"] += profit
                book["realized"] += profit
                stats["trades"] += 1
                stats["wins"] += profit > 0
                days[day] += profit
                closed.append({"time_ist": fill["time_ist"], "source": source, "symbol": fill["tradingsymbol"],
                               "exchange": fill["exchange"], "opened_at": book["opened_at"],
                               "key": trade_key(mode, source, fill["exchange"], fill["tradingsymbol"], book["opened_at"]),
                               **({"multiplier": factor} if factor != 1 else {}),
                               "quantity": round(closing / factor, 6) if factor != 1 else closing, "side": "SHORT" if buying else "LONG",
                               "buy_price": round(price if buying else average, 2),
                               "sell_price": round(average if buying else price, 2),
                               "pnl": round(profit, 2),
                               "return_pct": round(profit / (closing * average) * 100, 2) if average else 0.0})
            rest = quantity - closing
            if rest and (buying or fill["exchange"] in ("NFO", "BFO", "MCX") or fill.get("short")):
                if book["quantity"] == 0:
                    book["opened"], book["opened_at"] = day, fill["time_ist"]
                part = charge * rest / quantity
                book["quantity"] += rest if buying else -rest
                book["cost"] += rest * price + part if buying else -(rest * price - part)
        return books, sources, days, closed

    def report(self, mode: str, price_of=None) -> dict:
        """Everything the P&L screen shows for one account."""
        today = self._now().strftime("%Y-%m-%d")
        books, sources, days, closed = self._walk(mode)
        open_rows, unrealized, unrealized_today = [], 0.0, 0.0
        for (source, exchange, symbol), book in books.items():
            if not book["quantity"]:
                continue
            average = book["cost"] / book["quantity"]
            last = price_of(f"{exchange}:{symbol}") if price_of else None
            gain = (last - average) * book["quantity"] if last else None       # signed quantity: a short gains on a fall
            if gain is not None:
                unrealized += gain
                if book["opened"] == today:
                    unrealized_today += gain
            lots = book["quantity"] / book["factor"]
            open_rows.append({"source": source, "symbol": symbol, "exchange": exchange,
                              "quantity": int(round(lots)) if book["factor"] != 1 else book["quantity"],
                              "average": round(average, 2), "last": last, "unrealized": None if gain is None else round(gain, 2),
                              "opened": book["opened"], "opened_at": book["opened_at"],
                              "key": trade_key(mode, source, exchange, symbol, book["opened_at"])})

        curve, running, peak, drawdown = [], 0.0, 0.0, 0.0
        for day in sorted(days):
            running += days[day]
            peak = max(peak, running)
            drawdown = min(drawdown, running - peak)
            curve.append({"time": day, "value": round(running, 2), "day": round(days[day], 2)})
        realized = sum(s["realized"] for s in sources.values())
        wins = sum(s["wins"] for s in sources.values())
        trades = sum(s["trades"] for s in sources.values())
        best = max(days.values()) if days else None
        worst = min(days.values()) if days else None
        return {
            "mode": mode, "today": today,
            "realized": round(realized, 2), "unrealized": round(unrealized, 2),
            "today_realized": round(days.get(today, 0.0), 2), "today_unrealized": round(unrealized_today, 2),
            "today_total": round(days.get(today, 0.0) + unrealized_today, 2),
            "charges": round(sum(s["charges"] for s in sources.values()), 2),
            "trades": trades, "win_rate": round(wins / trades * 100, 1) if trades else None,
            "max_drawdown": round(drawdown, 2), "best_day": None if best is None else round(best, 2),
            "worst_day": None if worst is None else round(worst, 2),
            "sources": [{"source": name, "realized": round(s["realized"], 2), "charges": round(s["charges"], 2),
                         "trades": s["trades"], "fills": s["fills"],
                         "win_rate": round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else None,
                         "open": sum(abs(r["quantity"]) for r in open_rows if r["source"] == name),
                         "unrealized": round(sum(r["unrealized"] or 0 for r in open_rows if r["source"] == name), 2)}
                        for name, s in sorted(sources.items(), key=lambda item: -item[1]["realized"])],
            "curve": curve, "open": open_rows, "closed": list(reversed(closed[-200:])),
        }

    def closed_trades(self, mode: str) -> list[dict]:
        """Every closed trade of one account, oldest first (the P&L screen shows the latest 200)."""
        return self._walk(mode)[3]

    def legs(self, source: str, price_of=None, modes: tuple[str, ...] = ("paper", "live")) -> list[dict]:
        """One row per instrument a source (an algo) traded: what it bought and sold at, its booked and open
        profit and loss, and whether it is still held. Realized P&L is after charges; open P&L is not."""
        rows = []
        for mode in modes:
            books = self._walk(mode)[0]
            for (src, exchange, symbol), book in books.items():
                if src != source:
                    continue
                factor, held = book["factor"], book["quantity"]
                average = book["cost"] / held if held else None
                last = price_of(f"{exchange}:{symbol}") if price_of and held else None
                open_pnl = (last - average) * held if held and last else None
                rows.append({
                    "mode": mode, "exchange": exchange, "symbol": symbol,
                    "status": "Active" if held else "Closed",
                    "bought": book["bought"], "buy_price": round(book["buy_value"] / book["bought"], 2) if book["bought"] else None,
                    "sold": book["sold"], "sell_price": round(book["sell_value"] / book["sold"], 2) if book["sold"] else None,
                    "open_quantity": int(round(held / factor)) if factor != 1 else held,
                    "open_price": round(average, 2) if average is not None else None, "last": last,
                    "realized": round(book["realized"], 2),
                    "unrealized": 0.0 if not held else None if open_pnl is None else round(open_pnl, 2),
                    "charges": round(book["charges"], 2), "opened": book["opened"],
                    "opened_at": book["opened_at"],
                    "closed_at": None if held or not book["fills"] else book["fills"][-1]["time_ist"],
                    "fills": book["fills"][-100:]})
        rows.sort(key=lambda r: r["fills"][-1]["time_ist"] if r["fills"] else "", reverse=True)
        rows.sort(key=lambda r: r["status"] != "Active")          # held first, then the latest closed
        return rows
