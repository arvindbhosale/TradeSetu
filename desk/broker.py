"""Orders: validation, risk limits, the paper book, and live placement on Kite.

Every order, paper or live, takes the same path: ``preview`` checks it and
returns a one-time token with the exact phrase the user must type; ``place``
accepts that token and phrase once. A live order can therefore only be the one
the user was shown, and a double click cannot send it twice.
"""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
from collections import deque
from dataclasses import asdict, dataclass
from pathlib import Path

from tradingagents.dataflows.symbols import from_kite, kite_instrument

from . import market, mcx
from .charges import commodity_charges, futures_charges, order_charges
from .kite import KiteError, KiteMCP
from .settings import Settings, home

TOKEN_SECONDS = 120
ORDER_TAG = "tradesetu"
# SEBI's retail algo rules (from 1 April 2026): at most 10 API orders a second per client, and orders only from a
# static IP registered on the Kite Connect app. The desk stays under the rate itself and explains an IP refusal.
LIVE_ORDERS_PER_SECOND = 8


def explain_kite_refusal(message: str) -> str:
    text = str(message)
    low = text.lower()
    if "not allowed to place orders" in low or ("ip" in low.split() and "allowed" in low):
        return (f"{text}. Since 1 April 2026 Kite accepts API orders only from a static IP registered on your Kite "
                f"Connect app (developers.kite.trade → your app → IP Whitelist). Register the public IP of the machine "
                f"that runs kite-mcp-server, or run kite-mcp-server on a server with a static IP and point the desk at it.")
    if "429" in low or "too many requests" in low:
        return f"{text}. Kite allows at most 10 API orders a second; the order was not placed. Try again."
    return text
FUT_MARGIN_PCT = 12.0     # a futures order is held to the per-order limit by about its margin, not its contract value
DERIVATIVES = ("NFO", "BFO", "MCX")


def is_future(exchange: str, tradingsymbol: str) -> bool:
    """Kite names every futures contract ...FUT (NIFTY26DECFUT, GOLDM26DECFUT); options end in CE or PE."""
    return exchange in DERIVATIVES and tradingsymbol.endswith("FUT")


def multiplier(exchange: str, tradingsymbol: str) -> float:
    """Rupees per ₹1 of price per unit of order quantity: 1 on NSE/BSE, the lot factor on MCX (quantity in lots)."""
    return mcx.multiplier(exchange, tradingsymbol)


def fees(side: str, quantity: int, price: float, product: str, exchange: str, tradingsymbol: str):
    """The charges on one fill: MCX commodities, futures, options or shares."""
    if exchange == mcx.EXCHANGE:
        return commodity_charges(side, quantity, price, multiplier(exchange, tradingsymbol),
                                 option=not tradingsymbol.endswith("FUT"))
    if is_future(exchange, tradingsymbol):
        return futures_charges(side, quantity, price, exchange)
    return order_charges(side, quantity, price, product, exchange)


class OrderRejected(Exception):
    """The order failed validation; ``reasons`` lists every failed check."""

    def __init__(self, reasons: list[str]):
        super().__init__("; ".join(reasons))
        self.reasons = reasons


@dataclass
class Order:
    exchange: str
    tradingsymbol: str
    side: str                 # BUY / SELL
    quantity: int
    product: str = "CNC"      # CNC delivery, MIS intraday
    order_type: str = "LIMIT"  # LIMIT / MARKET / SL / SL-M
    price: float = 0.0
    trigger_price: float = 0.0
    variety: str = "regular"  # regular / amo
    # F&O only: "open" starts or adds to a position, "close" reduces one. Empty: a BUY opens and a SELL closes,
    # as for shares. A SELL that opens (writing an option, shorting a future) is taken only from an F&O algo.
    intent: str = ""

    @classmethod
    def parse(cls, raw: dict) -> Order:
        """Build an order from untrusted input; raises OrderRejected."""
        problems = []

        def text(key, allowed, default=None):
            value = str(raw.get(key, default or "")).strip().upper()
            if value not in allowed:
                problems.append(f"{key} must be one of {', '.join(allowed)}")
            return value

        def number(key):
            value = raw.get(key, 0) or 0
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                problems.append(f"{key} must be a non-negative number")
                return 0.0
            return float(value)

        exchange = text("exchange", ("NSE", "BSE", "NFO", "BFO", "MCX"))
        side = text("side", ("BUY", "SELL"))
        derivative = exchange in DERIVATIVES
        product = text("product", ("NRML", "MIS") if derivative else ("CNC", "MIS"), "NRML" if derivative else "CNC")
        order_type = text("order_type", ("LIMIT", "MARKET", "SL", "SL-M"), "LIMIT")
        variety = str(raw.get("variety", "regular")).strip().lower()
        if variety not in ("regular", "amo"):
            problems.append("variety must be regular or amo")
        symbol = str(raw.get("tradingsymbol", "")).strip().upper()
        if not symbol or len(symbol) > 40 or not all(c.isalnum() or c in "&-_" for c in symbol):
            problems.append("tradingsymbol is not a valid NSE/BSE symbol")
        quantity = raw.get("quantity")
        if isinstance(quantity, float) and quantity.is_integer():
            quantity = int(quantity)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            problems.append("quantity must be a whole number above zero")
            quantity = 0
        price, trigger = number("price"), number("trigger_price")
        if order_type in ("LIMIT", "SL") and price <= 0:
            problems.append(f"a {order_type} order needs a price")
        if order_type in ("SL", "SL-M") and trigger <= 0:
            problems.append(f"a {order_type} order needs a trigger price")
        for label, value in (("price", price), ("trigger price", trigger)):
            if value and not market.is_tick_multiple(value):
                problems.append(f"{label} {value} is not a multiple of the {market.TICK} tick")
        intent = str(raw.get("intent", "") or "").strip().lower()
        if intent not in ("", "open", "close"):
            problems.append("intent must be open or close")
        elif intent and not derivative:
            problems.append("intent is for F&O orders only")
        if exchange == mcx.EXCHANGE and symbol and mcx.name_of(symbol) is None:
            problems.append(f"{symbol} is not an MCX gold or silver contract; the desk trades only "
                            f"{', '.join(mcx.METALS)} on MCX")
        if problems:
            raise OrderRejected(problems)
        return cls(exchange, symbol, side, quantity, product, order_type,
                   price if order_type in ("LIMIT", "SL") else 0.0,
                   trigger if order_type in ("SL", "SL-M") else 0.0, variety, intent)

    @property
    def opens(self) -> bool:
        """Whether the order adds risk: a buy of shares, or an F&O order that opens a position."""
        return self.intent == "open" or (not self.intent and self.side == "BUY")

    @property
    def future(self) -> bool:
        return is_future(self.exchange, self.tradingsymbol)

    @property
    def commodity(self) -> bool:
        return self.exchange == mcx.EXCHANGE

    @property
    def multiplier(self) -> float:
        return multiplier(self.exchange, self.tradingsymbol)

    @property
    def instrument(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def phrase(self) -> str:
        """What the user types to confirm exactly this order."""
        return f"{self.side} {self.quantity} {self.tradingsymbol}"

    def kite_args(self) -> dict:
        args = {
            "variety": self.variety, "exchange": self.exchange, "tradingsymbol": self.tradingsymbol,
            "transaction_type": self.side, "quantity": self.quantity, "product": self.product,
            "order_type": self.order_type, "validity": "DAY", "tag": ORDER_TAG,
        }
        if self.price:
            args["price"] = self.price
        if self.trigger_price:
            args["trigger_price"] = self.trigger_price
        return args


@dataclass
class Gtt:
    """A Good Till Triggered order: ``single`` (one trigger) or ``oco`` (target and stoploss, one cancels the other)."""

    exchange: str
    tradingsymbol: str
    side: str
    quantity: int
    kind: str = "single"
    trigger: float = 0.0          # single
    limit: float = 0.0            # single: the limit price of the order the trigger sends
    target: float = 0.0           # oco upper trigger
    stop: float = 0.0             # oco lower trigger
    product: str = "CNC"

    @classmethod
    def parse(cls, raw: dict) -> Gtt:
        problems = []

        def number(key):
            value = raw.get(key, 0) or 0
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
                problems.append(f"{key} must be a non-negative number")
                return 0.0
            return market.round_to_tick(float(value))

        exchange = str(raw.get("exchange", "")).strip().upper()
        side = str(raw.get("side", "")).strip().upper()
        kind = str(raw.get("kind", "single")).strip().lower()
        symbol = str(raw.get("tradingsymbol", "")).strip().upper()
        if exchange not in ("NSE", "BSE"):
            problems.append("exchange must be NSE or BSE")
        if side not in ("BUY", "SELL"):
            problems.append("side must be BUY or SELL")
        if kind not in ("single", "oco"):
            problems.append("kind must be single or oco")
        if not symbol or len(symbol) > 32 or not all(c.isalnum() or c in "&-_" for c in symbol):
            problems.append("tradingsymbol is not a valid NSE/BSE symbol")
        quantity = raw.get("quantity")
        if isinstance(quantity, float) and quantity.is_integer():
            quantity = int(quantity)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            problems.append("quantity must be a whole number above zero")
            quantity = 0
        trigger, limit, target, stop = (number(k) for k in ("trigger", "limit", "target", "stop"))
        if kind == "single":
            if trigger <= 0:
                problems.append("a GTT needs a trigger price")
            limit = limit or trigger
        elif kind == "oco":
            if side != "SELL":
                problems.append("a target + stoploss (OCO) GTT is a SELL")
            if target <= 0 or stop <= 0 or stop >= target:
                problems.append("an OCO GTT needs a target above its stoploss")
        if problems:
            raise OrderRejected(problems)
        return cls(exchange, symbol, side, quantity, kind, trigger, limit, target, stop)

    @property
    def instrument(self) -> str:
        return f"{self.exchange}:{self.tradingsymbol}"

    @property
    def phrase(self) -> str:
        return f"GTT {self.side} {self.quantity} {self.tradingsymbol}"

    def kite_args(self, last_price: float) -> dict:
        args = {"exchange": self.exchange, "tradingsymbol": self.tradingsymbol, "last_price": last_price,
                "transaction_type": self.side, "product": self.product}
        if self.kind == "single":
            return {**args, "trigger_type": "single", "trigger_value": self.trigger,
                    "quantity": self.quantity, "limit_price": self.limit}
        return {**args, "trigger_type": "two-leg",
                "upper_trigger_value": self.target, "upper_quantity": self.quantity, "upper_limit_price": self.target,
                "lower_trigger_value": self.stop, "lower_quantity": self.quantity, "lower_limit_price": self.stop}


class PaperBook:
    """A simulated account: cash, holdings and an order log, kept in one file."""

    def __init__(self, capital: float, path: Path | None = None):
        self.path = path or home() / "paper.json"
        self._lock = threading.RLock()
        self._state = {"cash": float(capital), "capital": float(capital), "positions": {}, "orders": [],
                       "gtts": []}
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict) and {"cash", "positions", "orders"} <= set(stored):
                self._state = {**self._state, **stored}
        except (OSError, json.JSONDecodeError):
            pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def reset(self, capital: float) -> None:
        with self._lock:
            self._state = {"cash": float(capital), "capital": float(capital), "positions": {}, "orders": [],
                           "gtts": []}
            self._save()

    def snapshot(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._state))

    def quantity(self, instrument: str) -> int:
        with self._lock:
            return int(self._state["positions"].get(instrument, {}).get("quantity", 0))

    @property
    def cash(self) -> float:
        with self._lock:
            return float(self._state["cash"])

    # -- simulated GTTs -----------------------------------------------------

    def add_gtt(self, gtt: Gtt, last_price: float, source: str) -> dict:
        with self._lock:
            record = {"id": f"PGTT{len(self._state['gtts']) + 1:05d}", **asdict(gtt), "status": "active",
                      "last_price": last_price, "source": source,
                      "created": market.now_ist().strftime("%Y-%m-%d %H:%M:%S")}
            self._state["gtts"].append(record)
            self._save()
            return dict(record)

    def gtts(self) -> list[dict]:
        with self._lock:
            return json.loads(json.dumps(self._state["gtts"]))

    def gtt(self, gtt_id: str) -> dict | None:
        return next((g for g in self.gtts() if g["id"] == gtt_id), None)

    def cancel_gtt(self, gtt_id: str) -> None:
        with self._lock:
            for record in self._state["gtts"]:
                if record["id"] == gtt_id and record["status"] == "active":
                    record["status"] = "cancelled"
            self._save()

    def check_gtts(self, price_of) -> list[dict]:
        """Fire the paper GTTs whose trigger the price has reached. ``price_of(instrument)`` -> price or None."""
        fired = []
        with self._lock:
            for record in self._state["gtts"]:
                if record["status"] != "active":
                    continue
                price = price_of(f"{record['exchange']}:{record['tradingsymbol']}")
                if not price:
                    continue
                if record["kind"] == "single":
                    # A trigger is hit when the price crosses it from where it stood at placement.
                    below = record["trigger"] < record["last_price"]
                    hit = price <= record["trigger"] if below else price >= record["trigger"]
                    leg = "trigger"
                else:
                    hit, leg = (True, "target") if price >= record["target"] else \
                               (True, "stop") if price <= record["stop"] else (False, "")
                if not hit:
                    continue
                order = Order(record["exchange"], record["tradingsymbol"], record["side"], record["quantity"],
                              record["product"], "LIMIT", market.round_to_tick(price))
                try:
                    fill = self.fill(order, price)
                    record.update(status="triggered", leg=leg, fill_price=fill["fill_price"],
                                  order_id=fill["order_id"],
                                  triggered=market.now_ist().strftime("%Y-%m-%d %H:%M:%S"))
                except OrderRejected as exc:
                    record.update(status="rejected", reason=str(exc))
                fired.append(dict(record))
            if fired:
                self._save()
        return fired

    def settle(self, instrument: str, quantity: int, price: float, future: bool = False) -> dict:
        """Close an expired F&O position at its settlement price: an option's intrinsic value (0 when worthless),
        a future's final settlement price. ``quantity`` is signed: below zero for a short position."""
        exchange, symbol = instrument.split(":", 1)
        factor = multiplier(exchange, symbol)
        with self._lock:
            held = self._state["positions"].get(instrument, {"quantity": quantity, "average_price": 0.0})
            if future:
                charges = 0.0
                self._state["cash"] += (price - held["average_price"]) * quantity * factor
            else:
                # STT (0.15% of intrinsic value since 1 April 2026) on an exercised in-the-money option falls on its
                # buyer; the writer pays the intrinsic value.
                charges = price * quantity * factor * 0.0015 if price > 0 and quantity > 0 else 0.0
                self._state["cash"] += price * quantity * factor - charges
            self._state["positions"].pop(instrument, None)
            record = {"order_id": f"PEXP{len(self._state['orders']) + 1:06d}", "time_ist": market.now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                      "instrument": instrument, "side": "SELL" if quantity > 0 else "BUY", "quantity": abs(quantity),
                      "fill_price": round(price, 2), "charges": round(charges, 2), "status": "EXPIRED"}
            self._state["orders"].append(record)
            self._save()
            return record

    def fill(self, order: Order, price: float) -> dict:
        """Execute ``order`` in full at ``price``; raises OrderRejected."""
        with self._lock:
            if order.exchange in DERIVATIVES:
                return self._fill_derivative(order, price)
            fees = order_charges(order.side, order.quantity, price, order.product, order.exchange)
            positions = self._state["positions"]
            held = positions.get(order.instrument, {"quantity": 0, "average_price": 0.0})
            if order.side == "BUY":
                cost = fees.turnover + fees.total
                if cost > self._state["cash"] + 1e-6:
                    raise OrderRejected([f"paper cash ₹{self._state['cash']:,.2f} does not cover ₹{cost:,.2f}"])
                total = held["quantity"] + order.quantity
                held = {"quantity": total,
                        "average_price": (held["quantity"] * held["average_price"] + fees.turnover) / total}
                self._state["cash"] -= cost
                positions[order.instrument] = held
            else:
                if order.quantity > held["quantity"]:
                    raise OrderRejected([f"paper book holds {held['quantity']} {order.tradingsymbol}, "
                                         f"cannot sell {order.quantity}"])
                self._state["cash"] += fees.turnover - fees.total
                held = {**held, "quantity": held["quantity"] - order.quantity}
                if held["quantity"]:
                    positions[order.instrument] = held
                else:
                    positions.pop(order.instrument, None)
            record = {"order_id": f"PAPER{len(self._state['orders']) + 1:06d}",
                      "time_ist": market.now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                      **asdict(order), "fill_price": round(price, 2), "charges": fees.as_dict(),
                      "status": "COMPLETE"}
            self._state["orders"].append(record)
            self._save()
            return record

    def _fill_derivative(self, order: Order, price: float) -> dict:
        """An F&O fill. Positions are signed (below zero is short). An option's premium moves cash both ways; a
        future moves cash only by its realised profit or loss. Margin is not blocked here: the F&O algo checks it
        before an entry. Whether a short may be opened is the order path's decision, not the book's."""
        charge = fees(order.side, order.quantity, price, order.product, order.exchange, order.tradingsymbol)
        positions = self._state["positions"]
        held = positions.get(order.instrument, {"quantity": 0, "average_price": 0.0})
        before, average = int(held["quantity"]), float(held["average_price"])
        signed = order.quantity if order.side == "BUY" else -order.quantity
        if order.future:
            closing = min(abs(signed), abs(before)) if before * signed < 0 else 0
            realised = closing * (price - average) * (1 if before > 0 else -1) * order.multiplier
            cash_change = realised - charge.total
        elif order.side == "BUY":
            cash_change = -(charge.turnover + charge.total)
        else:
            cash_change = charge.turnover - charge.total
        if cash_change < 0 and -cash_change > self._state["cash"] + 1e-6:
            raise OrderRejected([f"paper cash ₹{self._state['cash']:,.2f} does not cover ₹{-cash_change:,.2f}"])
        after = before + signed
        if after == 0:
            positions.pop(order.instrument, None)
        else:
            if before == 0 or before * signed > 0:
                average = (abs(before) * average + abs(signed) * price) / abs(after)
            elif before * after < 0:
                average = price                     # turned from long to short or back: the rest opened at this price
            positions[order.instrument] = {"quantity": after, "average_price": average}
        self._state["cash"] += cash_change
        record = {"order_id": f"PAPER{len(self._state['orders']) + 1:06d}",
                  "time_ist": market.now_ist().strftime("%Y-%m-%d %H:%M:%S"),
                  **asdict(order), "fill_price": round(price, 2), "charges": charge.as_dict(), "status": "COMPLETE"}
        self._state["orders"].append(record)
        self._save()
        return record


class Desk:
    """The order path shared by paper and live trading."""

    def __init__(self, settings: Settings, kite: KiteMCP, paper: PaperBook | None = None,
                 journal: Path | None = None, price_lookup=None, now=market.now_ist, ledger=None, notifier=None,
                 contract_lookup=None):
        from .notify import Notifier
        from .pnl import Ledger

        self.settings, self.kite, self._now = settings, kite, now
        self.ledger = ledger or Ledger((journal.parent / "fills.jsonl") if journal else None, now=now)
        self.notifier = notifier or Notifier(settings, (journal.parent / "alerts.jsonl") if journal else None, now=now)
        self._loss_state: dict[str, str] = {}       # mode -> the day its loss limit last tripped
        self.contract_lookup = contract_lookup      # (exchange, tradingsymbol) -> option contract, for F&O orders
        self.algo_instruments = None                # () -> instruments F&O algos hold (they manage their own exits)
        self.algo_claims = None                     # (instrument, source) -> signed quantity the OTHER F&O algos hold
        self.flatten_day: str | None = None         # intraday algos close out on this day
        self.paper = paper or PaperBook(settings.get("paper_capital"))
        self.journal = journal or home() / "orders.jsonl"
        self._price_lookup = price_lookup     # (yahoo_symbol) -> last close, when Kite is not connected
        self._tokens: dict[str, dict] = {}
        self._lock = threading.Lock()
        self._sent: deque = deque()                 # monotonic times of recent live orders, for the rate cap
        self._rate_lock = threading.Lock()

    # -- prices and account ------------------------------------------------

    def last_price(self, order_or_instrument) -> tuple[float | None, str]:
        """The last price for an instrument and where it came from."""
        instrument = getattr(order_or_instrument, "instrument", order_or_instrument)
        if self.kite.connected and "get_ltp" in self.kite.tools:
            try:
                price = self.kite.ltp([instrument]).get(instrument)
                if price:
                    return price, "Kite last traded price"
            except KiteError:
                pass
        if self._price_lookup and not instrument.startswith(f"{mcx.EXCHANGE}:"):    # no delayed source for MCX
            exchange, symbol = instrument.split(":", 1)
            try:
                return float(self._price_lookup(from_kite(exchange, symbol))), "Yahoo last close (delayed)"
            except Exception:  # noqa: BLE001 - no price is a reportable state, not a crash
                pass
        return None, "no price available"

    def orders_today(self, mode: str) -> int:
        today = self._now().strftime("%Y-%m-%d")
        count = 0
        try:
            with open(self.journal, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    opens = row.get("intent") == "open" or (not row.get("intent") and row.get("side") == "BUY")
                    if (row.get("mode") == mode and row.get("ok") and opens
                            and not str(row.get("source", "")).startswith("gtt:")
                            and str(row.get("time_ist", "")).startswith(today)):
                        count += 1
        except OSError:
            pass
        return count

    def live_holding(self, order: Order) -> int | None:
        """Shares of the order's symbol held at the broker, or None if unknown."""
        try:
            total = 0
            for row in self.kite.holdings():
                if str(row.get("tradingsymbol", "")).upper() == order.tradingsymbol:
                    total += int(row.get("quantity", 0) or 0) + int(row.get("t1_quantity", 0) or 0)
            return total
        except (KiteError, ValueError, TypeError):
            return None

    # -- the order path ----------------------------------------------------

    def preview(self, raw: dict, mode: str, allow_short: bool = False, source: str = "") -> dict:
        """Check an order and, if it passes, issue the token that places it.

        ``allow_short`` lets an F&O order with intent "open" sell: write an option or short a future. Only the F&O
        algo engine passes it, for a leg of a structure the user armed; a manual order never opens a short.
        ``source`` names the algo placing the order: a closing order from an F&O algo is checked against that
        algo's share of the account's position, not the account's net, which other algos' legs also make up.
        """
        if mode not in ("paper", "live"):
            raise OrderRejected(["mode must be paper or live"])
        order = Order.parse(raw)
        cfg = self.settings.all()
        errors, warnings = [], []

        if cfg["kill_switch"] and order.opens:
            errors.append("The kill switch is on: no new buys. Turn it off in Settings. Sells are still allowed."
                          if not order.intent else
                          "The kill switch is on: no new F&O positions. Orders that close a position still go through.")
        commodity = order.commodity
        session = mcx.session_status(self._now()) if commodity else market.session_status(self._now())
        ltp, ltp_source = self.last_price(order)
        window = self.mcx_window_status() if commodity else self.window_status()
        if commodity and mode == "paper" and not session["open"]:
            errors.append(f"MCX is closed ({session['note']}). Paper MCX orders fill only in the MCX session.")
        elif cfg["enforce_trade_window"]:
            if order.opens and not window["entries"]:
                errors.append(f"New {'buys' if not order.intent else 'positions'} are taken only between "
                              f"{window['trade_start']} and {window['trade_end']} IST "
                              f"on {'MCX ' if commodity else ''}trading days (your "
                              f"{'MCX ' if commodity else ''}trade window in Settings). It is {window['time']} IST.")
            elif not order.opens and not session["open"] and mode == "paper":
                errors.append("The market is closed. Paper sells fill only during market hours."
                              if not order.intent else "The market is closed. Paper orders fill only during market hours.")
        reference = order.price or ltp
        if mode == "paper" and ltp and order.order_type == "LIMIT":
            reference = ltp      # a paper limit fills at the last price
        if reference is None:
            errors.append("No price is available to value this order. Connect Kite or use a LIMIT price.")
        value = (reference or 0) * order.quantity * order.multiplier

        # The size and count limits cap new risk. They never stand in the way of
        # an exit: a position that has grown past the limit must still be closable.
        # A futures order is weighed by about its margin; an option by its premium.
        margin_pct = cfg["mcx_margin_pct"] if commodity else FUT_MARGIN_PCT
        weighed = value * margin_pct / 100 if order.future else value
        if order.opens and weighed > cfg["max_order_value"]:
            what = f"Margin of about ₹{weighed:,.0f} ({margin_pct:g}% of ₹{value:,.0f})" if order.future \
                else f"Order value ₹{value:,.0f}"
            errors.append(f"{what} is above your ₹{cfg['max_order_value']:,.0f} limit per order.")
        if order.opens and self.orders_today(mode) >= cfg["max_orders_per_day"]:
            errors.append(f"You have reached your limit of {cfg['max_orders_per_day']} {mode} orders today.")
        derivative = order.exchange in DERIVATIVES
        band = 25 if derivative else 5             # option premiums move far more than shares
        if derivative:
            contract = self.contract_lookup(order.exchange, order.tradingsymbol) if self.contract_lookup else None
            if contract is None:
                errors.append(f"{order.instrument} is not a contract the desk knows. Open "
                              f"{'F&O → MCX gold & silver' if commodity else 'F&O'}, refresh the contract list from "
                              f"Kite, and pick it from the list.")
            else:
                tick = float(contract.get("tick_size") or 0)
                for label, price in (("price", order.price), ("trigger price", order.trigger_price)):
                    if tick and price and not market.is_tick_multiple(price, tick):
                        errors.append(f"The {label} {price:g} is not a multiple of this contract's ₹{tick:g} tick.")
                if commodity and int(contract.get("lot_size") or 1) != 1:
                    errors.append(f"Kite lists {order.tradingsymbol} with a lot size of {contract.get('lot_size')}, not 1: "
                                  f"its quantity convention has changed and the desk cannot value it safely. Not placed.")
                if commodity and order.opens and mcx.in_delivery_window(contract["expiry"], self._now(), cfg["mcx_exit_days"]):
                    errors.append(f"{order.tradingsymbol} expires {contract['expiry']} and is compulsorily delivered. "
                                  f"The desk opens no position within {cfg['mcx_exit_days']} days of expiry (Settings): "
                                  f"Zerodha does not take retail positions into physical delivery and squares them off "
                                  f"before it. Trade the next contract.")
                elif commodity and mcx.in_delivery_window(contract["expiry"], self._now(), cfg["mcx_exit_days"]):
                    warnings.append(f"{order.tradingsymbol} is inside the desk's {cfg['mcx_exit_days']}-day delivery "
                                    f"cut-off. Close it now: Zerodha squares off physically settled MCX positions before "
                                    f"the delivery period (see its 'MCX precious metals expiry' bulletin).")
                lot = int(contract.get("lot_size") or 1)
                if order.quantity % lot:
                    errors.append(f"F&O quantity must be a whole number of lots: {lot} a lot for this contract.")
                freeze = int(contract.get("freeze_quantity") or 0)
                if freeze and order.quantity > freeze:
                    errors.append(f"The exchange accepts at most {freeze} in one order for this contract.")
                if contract["expiry"] < self._now().strftime("%Y-%m-%d"):
                    errors.append("This contract has expired.")
                elif contract["expiry"] == self._now().strftime("%Y-%m-%d") and order.side == "BUY" and not order.future:
                    warnings.append("This option expires today: its premium can fall to zero within hours.")
            if order.order_type != "LIMIT":
                errors.append("F&O is traded with LIMIT orders only here: market orders on options can fill "
                              "far from the last price.")
            if order.intent == "open" and order.side == "SELL":
                if not allow_short:
                    errors.append("Opening a short position (writing an option or selling a future) is done only by an "
                                  "armed F&O algo, never by a single order.")
            elif order.side == "SELL" or order.intent == "close":
                held = self.paper.quantity(order.instrument) if mode == "paper" else self.live_position(order)
                if held is not None and source.startswith("fno:") and order.intent == "close" and self.algo_claims:
                    held -= self.algo_claims(order.instrument, source)
                if held is None:
                    errors.append("Could not read your positions to check this order.")
                elif order.side == "SELL" and order.quantity > held:
                    errors.append(f"You hold {held} of {order.tradingsymbol}; selling {order.quantity} would "
                                  + ("open a short future, which only an armed F&O algo does." if order.future else
                                     "write (short) options, which this desk does not do.")
                                  if not order.intent else
                                  f"You hold {held} of {order.tradingsymbol}; a closing sell of {order.quantity} would "
                                  f"open a short.")
                elif order.side == "BUY" and order.quantity > -held:
                    errors.append(f"You are short {max(-held, 0)} of {order.tradingsymbol}; a closing buy of "
                                  f"{order.quantity} would open a long.")
        if order.order_type == "LIMIT" and ltp:
            away = (order.price - ltp) / ltp * 100
            if order.side == "BUY" and away > band:
                errors.append(f"Buy limit ₹{order.price:,.2f} is {away:.1f}% above the last price ₹{ltp:,.2f}.")
            if order.side == "SELL" and away < -band:
                errors.append(f"Sell limit ₹{order.price:,.2f} is {-away:.1f}% below the last price ₹{ltp:,.2f}.")
        if order.order_type in ("MARKET", "SL-M"):
            warnings.append("A market order fills at whatever price is available. A LIMIT is safer, "
                            "and Kite may reject market orders sent without market protection.")

        fill_price = None
        if mode == "paper":
            # The paper book keeps no resting orders: an order fills now, at
            # the last price, or it is refused.
            if order.order_type in ("SL", "SL-M"):
                errors.append("The paper book does not hold stop orders. Use LIMIT or MARKET.")
            elif ltp is None:
                if order.order_type == "LIMIT":
                    fill_price = order.price
                    warnings.append("No market price is available, so this fills at your limit price.")
                else:
                    errors.append("The paper book needs a price to fill at.")
            elif order.order_type == "LIMIT" and order.side == "BUY" and order.price < ltp:
                errors.append(f"A buy limit of ₹{order.price:,.2f} is below the last price ₹{ltp:,.2f} and "
                              f"would not fill now. The paper book keeps no pending orders.")
            elif order.order_type == "LIMIT" and order.side == "SELL" and order.price > ltp:
                errors.append(f"A sell limit of ₹{order.price:,.2f} is above the last price ₹{ltp:,.2f} and "
                              f"would not fill now. The paper book keeps no pending orders.")
            else:
                fill_price = ltp
            if order.side == "SELL" and not derivative and order.quantity > self.paper.quantity(order.instrument):
                errors.append(f"The paper book holds {self.paper.quantity(order.instrument)} "
                              f"{order.tradingsymbol}; it cannot sell {order.quantity}.")
            warnings.append("Paper orders fill at once, in full, at the last price. "
                            "A real order may fill partly, later, or not at all.")
        else:
            if not cfg["live_enabled"]:
                errors.append("Live trading is switched off. Enable it in Settings.")
            if not self.kite.connected:
                errors.append("Not connected to Kite.")
            elif not self.kite.can_trade:
                errors.append("The connected Kite server is read-only: it has no place_order tool. "
                              "Zerodha's hosted server (mcp.kite.trade) does not place orders; "
                              "self-host kite-mcp-server with your Kite Connect API key.")
            if order.variety == "amo" and commodity:
                errors.append("The desk sends MCX orders only during the MCX session, not as AMO.")
            if order.variety == "amo" and cfg["enforce_trade_window"] and order.opens:
                errors.append("After-market buys are outside your trade window. Turn the window off in "
                              "Settings to send AMO buys.")
            if order.variety == "regular" and not session["open"]:
                errors.append(f"MCX is closed. {session['note']}" if commodity else
                              "The market is closed. Send this as an AMO (after-market order) instead.")
            if order.variety == "amo" and session["open"]:
                errors.append("The market is open. Send a regular order, not an AMO.")
            if order.side == "SELL" and order.product == "CNC" and self.kite.connected:
                held = self.live_holding(order)
                if held is None:
                    errors.append("Could not read your holdings to check this delivery sell.")
                elif order.quantity > held:
                    errors.append(f"You hold {held} {order.tradingsymbol}; a delivery (CNC) sell of "
                                  f"{order.quantity} would be a short sale.")
            if order.product == "MIS":
                warnings.append(f"MIS is intraday: Zerodha squares it off shortly before the MCX close "
                                f"({session['closes']} IST today) if you do not." if commodity else
                                "MIS is intraday: Zerodha squares it off around 15:20 IST if you do not.")

        charges = None
        if reference:
            charges = fees(order.side, order.quantity, reference, order.product, order.exchange,
                           order.tradingsymbol).as_dict()
        result = {
            "ok": not errors, "mode": mode, "order": asdict(order), "errors": errors, "warnings": warnings,
            "last_price": ltp, "last_price_source": ltp_source, "value": round(value, 2),
            "charges": charges, "session": session, "phrase": order.phrase, "token": None,
        }
        if not errors:
            token = secrets.token_urlsafe(24)
            with self._lock:
                now = time.monotonic()
                self._tokens = {t: v for t, v in self._tokens.items() if v["expires"] > now}
                self._tokens[token] = {"order": order, "mode": mode, "expires": now + TOKEN_SECONDS,
                                       "fill_price": fill_price}
            result["token"], result["expires_in"] = token, TOKEN_SECONDS
        return result

    def mcx_window_status(self) -> dict:
        """Where the clock stands against the MCX trade window in Settings (inside the MCX session)."""
        cfg, now = self.settings.all(), self._now()
        clock, session = now.strftime("%H:%M"), mcx.session_status(now)
        start = max(cfg["mcx_trade_start"], session["opens"] or "99:99")
        end = min(cfg["mcx_trade_end"], session["closes"])
        return {"time": clock, "weekday": session["opens"] is not None, "entries": start <= clock < end,
                "market": session["open"], "trade_start": start if session["opens"] else cfg["mcx_trade_start"],
                "trade_end": end, "square_off": False, "square_off_at": session["closes"]}

    def window_status(self) -> dict:
        """Where the clock stands against the trade window in Settings."""
        cfg, now = self.settings.all(), self._now()
        clock = now.strftime("%H:%M")
        weekday = market.is_trading_day(now)
        return {"time": clock, "weekday": weekday,
                "entries": weekday and cfg["trade_start"] <= clock < cfg["trade_end"],
                "square_off": weekday and clock >= cfg["square_off"],
                "market": weekday and "09:15" <= clock < "15:30",
                "trade_start": cfg["trade_start"], "trade_end": cfg["trade_end"], "square_off_at": cfg["square_off"]}

    def place(self, token: str, phrase: str) -> dict:
        """Place the previewed order once the user has typed its phrase. The token works once."""
        pending = self._take(token)
        subject = pending.get("gtt") or pending["order"]
        if " ".join(str(phrase).upper().split()) != subject.phrase:
            raise OrderRejected([f'Type "{subject.phrase}" exactly to confirm.'])
        return self._execute_gtt(pending, "manual") if pending.get("gtt") else self._execute(pending, "manual")

    # -- GTT ----------------------------------------------------------------

    def gtt_preview(self, raw: dict, mode: str) -> dict:
        """Check a GTT and issue the token that places it."""
        if mode not in ("paper", "live"):
            raise OrderRejected(["mode must be paper or live"])
        gtt = Gtt.parse(raw)
        cfg = self.settings.all()
        errors, warnings = [], []
        if cfg["kill_switch"] and gtt.side == "BUY":
            errors.append("The kill switch is on: no new buys. Turn it off in Settings.")
        ltp, source = self.last_price(gtt.instrument)
        if not ltp:
            errors.append("No price is available for this symbol; a GTT needs the last price.")
        worst = max(gtt.trigger, gtt.limit, gtt.target)
        value = worst * gtt.quantity
        if gtt.side == "BUY" and value > cfg["max_order_value"]:
            errors.append(f"Order value ₹{value:,.0f} is above your ₹{cfg['max_order_value']:,.0f} limit per order.")
        if ltp and gtt.kind == "single" and abs(gtt.trigger - ltp) / ltp < 0.0025:
            errors.append(f"The trigger ₹{gtt.trigger:,.2f} is within 0.25% of the last price ₹{ltp:,.2f}. "
                          f"Zerodha rejects such GTTs; place a normal order instead.")
        if ltp and gtt.kind == "oco" and not gtt.stop < ltp < gtt.target:
            errors.append(f"The last price ₹{ltp:,.2f} must lie between the stoploss ₹{gtt.stop:,.2f} "
                          f"and the target ₹{gtt.target:,.2f}.")
        if mode == "paper":
            if gtt.side == "SELL" and gtt.quantity > self.paper.quantity(gtt.instrument):
                errors.append(f"The paper book holds {self.paper.quantity(gtt.instrument)} {gtt.tradingsymbol}; "
                              f"a GTT cannot sell {gtt.quantity}.")
            warnings.append("A paper GTT is checked about once a minute against the last price and fills in "
                            "full at that price. A real GTT sends a limit order that may not fill.")
        else:
            if not cfg["live_enabled"]:
                errors.append("Live trading is switched off. Enable it in Settings.")
            if not self.kite.connected:
                errors.append("Not connected to Kite.")
            elif "place_gtt_order" not in self.kite.tools:
                errors.append("The connected Kite server has no GTT tool (the hosted server is read-only).")
            warnings.append("A GTT is valid for one year and triggers only once. When it triggers, Zerodha "
                            "sends a limit order, which fills only if the market trades at that price. "
                            "GTTs are for delivery (CNC); a sell needs the shares in your demat when it triggers.")
        result = {"ok": not errors, "mode": mode, "gtt": asdict(gtt), "errors": errors, "warnings": warnings,
                  "last_price": ltp, "last_price_source": source, "value": round(value, 2),
                  "phrase": gtt.phrase, "token": None}
        if not errors:
            token = secrets.token_urlsafe(24)
            with self._lock:
                now = time.monotonic()
                self._tokens = {t: v for t, v in self._tokens.items() if v["expires"] > now}
                self._tokens[token] = {"gtt": gtt, "mode": mode, "expires": now + TOKEN_SECONDS, "ltp": ltp}
            result["token"], result["expires_in"] = token, TOKEN_SECONDS
        return result

    def auto_gtt(self, raw: dict, mode: str, source: str) -> dict:
        """Place a GTT for an algo or the scanner: every check, no typed phrase."""
        review = self.gtt_preview(raw, mode)
        if not review["ok"]:
            raise OrderRejected(review["errors"])
        return self._execute_gtt(self._take(review["token"]), source)

    def _execute_gtt(self, pending: dict, source: str) -> dict:
        gtt, mode = pending["gtt"], pending["mode"]
        cfg = self.settings.all()
        if cfg["kill_switch"] and gtt.side == "BUY":
            raise OrderRejected(["The kill switch is on."])
        if mode == "live" and not cfg["live_enabled"]:
            raise OrderRejected(["Live trading is switched off."])
        entry = {"time_ist": self._now().strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "source": source,
                 "gtt": True, **asdict(gtt), "price": gtt.trigger or gtt.target}
        try:
            if mode == "paper":
                record = self.paper.add_gtt(gtt, pending["ltp"], source)
                entry.update(ok=True, order_id=record["id"])
            else:
                answer = self.kite.place_gtt(gtt.kite_args(pending["ltp"]))
                gtt_id = answer.get("trigger_id") if isinstance(answer, dict) else None
                if gtt_id is None:
                    raise KiteError(f"Kite did not return a GTT id: {answer}")
                entry.update(ok=True, order_id=str(gtt_id))
        except (KiteError, OrderRejected) as exc:
            entry.update(ok=False, error=str(exc))
            self._journal(entry)
            raise OrderRejected([str(exc)]) from exc
        self._journal(entry)
        return entry

    def gtt_status(self, mode: str, gtt_id: str) -> dict | None:
        """One GTT as ``{status, leg?, fill_price?}``; None when the broker no longer lists it."""
        if mode == "paper":
            return self.paper.gtt(gtt_id)
        for row in self.kite.gtts():
            if str(row.get("id")) == str(gtt_id):
                return {"status": str(row.get("status", "")).lower(), "raw": row}
        return None

    def cancel_gtt(self, mode: str, gtt_id: str) -> None:
        if mode == "paper":
            self.paper.cancel_gtt(gtt_id)
        else:
            self.kite.delete_gtt(gtt_id)

    def live_position(self, order: Order) -> int | None:
        """Net quantity of an F&O contract held at the broker, or None if unknown."""
        try:
            return sum(int(row.get("quantity") or 0) for row in self.kite.positions()
                       if str(row.get("tradingsymbol", "")).upper() == order.tradingsymbol
                       and str(row.get("exchange", "")).upper() == order.exchange)
        except (KiteError, ValueError, TypeError):
            return None

    def settle_paper_options(self, spot_at) -> list[dict]:
        """Close paper option positions whose expiry has passed, at intrinsic value.

        ``spot_at(underlying, expiry)`` gives the underlying's closing level on the expiry day.
        """
        if not self.contract_lookup:
            return []
        now = self._now()
        today, after_close = now.strftime("%Y-%m-%d"), now.time() >= market.CLOSE
        settled = []
        for instrument, held in self.paper.snapshot()["positions"].items():
            exchange, symbol = instrument.split(":", 1)
            quantity = int(held["quantity"])
            if exchange not in DERIVATIVES or exchange == mcx.EXCHANGE or not quantity:
                continue                # MCX positions are closed before delivery (close_paper_mcx), never settled
            contract = self.contract_lookup(exchange, symbol)
            if contract is None or contract["expiry"] > today or (contract["expiry"] == today and not after_close):
                continue
            spot = spot_at(contract["underlying"], contract["expiry"])
            if not spot:
                continue
            future = contract.get("type") == "FUT"
            if future:
                price = float(spot)
            else:
                price = max(spot - contract["strike"], 0) if contract["type"] == "CE" else max(contract["strike"] - spot, 0)
            record = self.paper.settle(instrument, quantity, price, future=future)
            # Book the close under the source that opened it, so the P&L pairs the two.
            opening = "BUY" if quantity > 0 else "SELL"
            opens = [f for f in self.ledger.fills() if f.get("mode") == "paper" and f["exchange"] == exchange
                     and f["tradingsymbol"] == symbol and f["side"] == opening]
            source = opens[-1]["source"] if opens else "manual"
            self.ledger.add("paper", source, exchange, symbol, record["side"], abs(quantity), max(price, 0.0001),
                            "NRML", record["charges"])
            record["source"] = source
            what = "future" if future else "option"
            self.notifier.notify(f"Paper {what} expired: {symbol}", f"Settled at ₹{price:,.2f} (underlying {spot:,.2f}).",
                                 "warn" if price == 0 or quantity < 0 else "good")
            settled.append(record)
        return settled

    def close_paper_mcx(self) -> list[dict]:
        """Do for the paper book what Zerodha's risk desk does for a real account: close every paper MCX position
        whose contract is inside the delivery cut-off (``mcx_exit_days``), at the last price, in the MCX session.
        Positions an F&O algo holds are left to that algo, which closes them itself at the same point."""
        if not self.contract_lookup or not mcx.session_status(self._now())["open"]:
            return []
        cfg = self.settings.all()
        held_by_algos = set(self.algo_instruments() if self.algo_instruments else [])
        closed = []
        for instrument, held in self.paper.snapshot()["positions"].items():
            exchange, symbol = instrument.split(":", 1)
            quantity = int(held["quantity"])
            if exchange != mcx.EXCHANGE or not quantity or instrument in held_by_algos:
                continue
            contract = self.contract_lookup(exchange, symbol)
            if contract is None or not mcx.in_delivery_window(contract["expiry"], self._now(), cfg["mcx_exit_days"]):
                continue
            last, _ = self.last_price(instrument)
            if not last:
                continue
            side = "SELL" if quantity > 0 else "BUY"
            order = Order(exchange, symbol, side, abs(quantity), "NRML", "LIMIT", float(last), 0.0, "regular", "close")
            try:
                record = self.paper.fill(order, float(last))
            except OrderRejected as exc:
                self.notifier.notify(f"Paper MCX square-off failed: {symbol}", str(exc), "bad")
                continue
            opening = "BUY" if quantity > 0 else "SELL"
            opens = [f for f in self.ledger.fills() if f.get("mode") == "paper" and f["exchange"] == exchange
                     and f["tradingsymbol"] == symbol and f["side"] == opening]
            source = opens[-1]["source"] if opens else "manual"
            self.ledger.add("paper", source, exchange, symbol, side, abs(quantity), float(last), "NRML",
                            record["charges"]["total"])
            self._journal({"time_ist": self._now().strftime("%Y-%m-%d %H:%M:%S"), "mode": "paper",
                           "source": "mcx-delivery-cutoff", "ok": True, **asdict(order),
                           "order_id": record["order_id"], "fill_price": record["fill_price"]})
            self.notifier.notify(f"Paper MCX position closed: {symbol}",
                                 f"{side} {abs(quantity)} at ₹{last:,.2f}: {contract['expiry']} expiry is within "
                                 f"{cfg['mcx_exit_days']} days and the contract is physically delivered.", "warn")
            closed.append(record)
        return closed

    def check_paper_gtts(self) -> list[dict]:
        if not self.window_status()["market"]:
            return []
        fired = self.paper.check_gtts(lambda instrument: self.last_price(instrument)[0])
        for record in fired:
            if record["status"] == "triggered":
                self.ledger.add("paper", record.get("source") or "manual", record["exchange"], record["tradingsymbol"],
                                record["side"], record["quantity"], record["fill_price"], record.get("product", "CNC"))
                self.notifier.notify(f"Paper GTT triggered: {record['side']} {record['quantity']} {record['tradingsymbol']}",
                                     f"{record.get('leg', 'trigger')} at ₹{record['fill_price']:,.2f}", "good")
            self._journal({"time_ist": self._now().strftime("%Y-%m-%d %H:%M:%S"), "mode": "paper",
                           "source": f"gtt:{record['id']}", "ok": record["status"] == "triggered",
                           "exchange": record["exchange"], "tradingsymbol": record["tradingsymbol"],
                           "side": record["side"], "quantity": record["quantity"],
                           "fill_price": record.get("fill_price"), "order_id": record.get("order_id"),
                           "error": record.get("reason")})
        return fired

    def auto_place(self, raw: dict, mode: str, source: str, allow_short: bool = False) -> dict:
        """Place an order for an algo: every check a manual order passes, no typed phrase.

        The typed confirmation is given once, when the algo is armed; this is
        the only path that skips it, and the journal records who used it.
        """
        review = self.preview(raw, mode, allow_short=allow_short, source=source)
        if not review["ok"]:
            raise OrderRejected(review["errors"])
        return self._execute(self._take(review["token"]), source)

    def _take(self, token: str) -> dict:
        with self._lock:
            pending = self._tokens.pop(token, None)
        if not pending or pending["expires"] < time.monotonic():
            raise OrderRejected(["This confirmation has expired or was already used. Preview the order again."])
        return pending

    def _execute(self, pending: dict, source: str) -> dict:
        order, mode = pending["order"], pending["mode"]
        cfg = self.settings.all()
        if cfg["kill_switch"] and order.opens:
            raise OrderRejected(["The kill switch is on."])
        if mode == "live" and not cfg["live_enabled"]:
            raise OrderRejected(["Live trading is switched off."])

        entry = {"time_ist": self._now().strftime("%Y-%m-%d %H:%M:%S"), "mode": mode,
                 "source": source, **asdict(order)}
        try:
            if mode == "paper":
                record = self.paper.fill(order, pending["fill_price"])
                entry.update(ok=True, order_id=record["order_id"], fill_price=record["fill_price"])
                self.ledger.add("paper", source, order.exchange, order.tradingsymbol, order.side, order.quantity,
                                record["fill_price"], order.product, record["charges"]["total"])
            else:
                self._pace()
                try:
                    answer = self.kite.place_order(order.kite_args())
                except KiteError as exc:
                    raise KiteError(explain_kite_refusal(str(exc)), exc.needs_login) from exc
                order_id = answer.get("order_id") if isinstance(answer, dict) else None
                entry.update(ok=True, order_id=order_id, broker_response=answer)
        except (KiteError, OrderRejected) as exc:
            entry.update(ok=False, error=str(exc))
            self._journal(entry)
            self.notifier.notify(f"{mode.capitalize()} order refused: {order.phrase}", f"{source}: {exc}", "bad")
            raise OrderRejected([str(exc)]) from exc
        self._journal(entry)
        if mode == "paper":
            self.notifier.notify(f"Paper {order.side} {order.quantity} {order.tradingsymbol} filled",
                                 f"at ₹{entry['fill_price']:,.2f} · {source}", "good")
        else:
            self.notifier.notify(f"LIVE {order.side} {order.quantity} {order.tradingsymbol} sent to Kite",
                                 f"limit ₹{order.price:,.2f} · order {entry.get('order_id')} · {source}. Not yet filled.", "warn")
        return entry

    def _pace(self) -> None:
        """Hold a live order back just long enough to stay under the exchange's orders-a-second cap."""
        with self._rate_lock:
            now = time.monotonic()
            while self._sent and now - self._sent[0] >= 1.0:
                self._sent.popleft()
            if len(self._sent) >= LIVE_ORDERS_PER_SECOND:
                time.sleep(max(1.0 - (now - self._sent[0]), 0) + 0.01)
                now = time.monotonic()
                while self._sent and now - self._sent[0] >= 1.0:
                    self._sent.popleft()
            self._sent.append(now)

    # -- fills, P&L and the daily loss limit ---------------------------------

    def sync_live_fills(self) -> int:
        """Copy today's Kite trades for orders this desk placed into the ledger. Returns how many were new."""
        if not (self.kite.connected and "get_trades" in self.kite.tools):
            return 0
        placed = {str(row.get("order_id")): row for row in self.journal_rows(500)
                  if row.get("mode") == "live" and row.get("ok") and not row.get("gtt")}
        if not placed:
            return 0
        try:
            trades = self.kite.call("get_trades")
        except KiteError:
            return 0
        rows = trades if isinstance(trades, list) else (trades.get("data") or [] if isinstance(trades, dict) else [])
        added = 0
        for trade in rows:
            order = placed.get(str(trade.get("order_id")))
            if not order or not trade.get("trade_id"):
                continue
            quantity, price = int(trade.get("quantity") or 0), float(trade.get("average_price") or 0)
            if self.ledger.add("live", order.get("source", "manual"), order["exchange"], order["tradingsymbol"],
                               order["side"], quantity, price, order.get("product", "CNC"), trade_id=str(trade["trade_id"])):
                added += 1
                self.notifier.notify(f"LIVE {order['side']} {quantity} {order['tradingsymbol']} filled",
                                     f"at ₹{price:,.2f} · {order.get('source', 'manual')}", "good")
        return added

    def pnl(self, mode: str) -> dict:
        return self.ledger.report(mode, lambda instrument: self.last_price(instrument)[0])

    def check_loss_limit(self) -> list[str]:
        """Turn the kill switch on when an account's loss today reaches the limit. Returns the accounts tripped."""
        limit = self.settings.get("daily_loss_limit")
        if not limit:
            return []
        today, tripped = self._now().strftime("%Y-%m-%d"), []
        for mode in ("live", "paper"):
            if self._loss_state.get(mode) == today:
                continue                     # once a day: switching the kill switch off again is the user's call
            report = self.ledger.report(mode, lambda instrument: self.last_price(instrument)[0])
            if report["today_total"] <= -limit:
                self._loss_state[mode] = today
                self.flatten_day = today
                self.settings.update({"kill_switch": True})
                tripped.append(mode)
                self.notifier.notify(
                    f"Daily loss limit hit on the {mode} account",
                    f"Today's P&L is ₹{report['today_total']:,.0f} against a limit of ₹{limit:,.0f}. The kill switch is "
                    f"ON: no new buys. Intraday algos are closing their positions. Delivery holdings are untouched.",
                    "bad")
        return tripped

    def _journal(self, entry: dict) -> None:
        with self._lock, open(self.journal, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")

    def journal_rows(self, limit: int = 100) -> list[dict]:
        try:
            lines = self.journal.read_text(encoding="utf-8").splitlines()[-limit:]
        except OSError:
            return []
        rows = []
        for line in reversed(lines):
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    # -- from a rating to a suggested order ---------------------------------

    def plan(self, symbol: str, rating: str, mode: str) -> dict:
        """The order a rating suggests, sized by the risk settings. Never placed here."""
        instrument = kite_instrument(symbol)
        if not instrument:
            return {"order": None, "reason": f"{symbol} is not an NSE/BSE equity; nothing to order."}
        exchange, tradingsymbol = instrument
        cfg = self.settings.all()
        key = f"{exchange}:{tradingsymbol}"
        ltp, source = self.last_price(key)
        if not ltp:
            return {"order": None, "reason": "No price available to size an order."}
        probe = Order(exchange, tradingsymbol, "SELL", 1, cfg["product"])
        if mode == "live":
            held = self.live_holding(probe) if self.kite.connected else None
            capital = self._live_cash()
        else:
            held, capital = self.paper.quantity(key), self.paper.snapshot()["capital"]
        rating_key = (rating or "").strip().title()
        buffer = cfg["limit_buffer_pct"] / 100
        base = {"exchange": exchange, "tradingsymbol": tradingsymbol, "product": cfg["product"],
                "order_type": "LIMIT",
                "variety": "regular" if market.session_status(self._now())["open"] or mode == "paper" else "amo"}

        if rating_key in ("Buy", "Overweight"):
            if capital is None:
                return {"order": None, "reason": "Could not read available funds from Kite."}
            share = cfg["position_pct"] * (1.0 if rating_key == "Buy" else 0.5) / 100
            room = cfg["max_position_pct"] / 100 * capital - (held or 0) * ltp
            budget = min(share * capital, cfg["max_order_value"], max(room, 0))
            price = market.round_to_tick(ltp * (1 + buffer), "BUY")
            quantity = int(budget // price)
            if quantity < 1:
                return {"order": None, "last_price": ltp,
                        "reason": f"{rating_key}: the ₹{budget:,.0f} budget from your limits does not buy "
                                  f"one share at ₹{price:,.2f}."}
            return {"order": {**base, "side": "BUY", "quantity": quantity, "price": price},
                    "last_price": ltp, "last_price_source": source,
                    "reason": f"{rating_key}: {share * 100:.1f}% of ₹{capital:,.0f}, capped by your order "
                              f"and position limits; limit {cfg['limit_buffer_pct']}% above the last price."}
        if rating_key in ("Sell", "Underweight"):
            if not held:
                return {"order": None, "last_price": ltp,
                        "reason": f"{rating_key}: you hold no {tradingsymbol}. Delivery shares cannot be shorted."}
            quantity = held if rating_key == "Sell" else max(held // 2, 1)
            price = market.round_to_tick(ltp * (1 - buffer), "SELL")
            return {"order": {**base, "side": "SELL", "quantity": quantity, "price": price},
                    "last_price": ltp, "last_price_source": source,
                    "reason": f"{rating_key}: sell {'all' if rating_key == 'Sell' else 'half'} of {held} held; "
                              f"limit {cfg['limit_buffer_pct']}% below the last price."}
        return {"order": None, "last_price": ltp, "reason": f"{rating_key or 'No rating'}: no order suggested."}

    def _live_cash(self, segment: str = "equity") -> float | None:
        """Funds Kite reports for a segment: ``equity`` (NSE/BSE, F&O) or ``commodity`` (MCX)."""
        try:
            margins = self.kite.margins()
            equity = margins.get(segment, margins if segment == "equity" else {}) if isinstance(margins, dict) else {}
            available = equity.get("available", {}) if isinstance(equity, dict) else {}
            for value in (available.get("live_balance"), available.get("cash"), equity.get("net")):
                if isinstance(value, (int, float)) and value > 0:
                    return float(value)
        except KiteError:
            pass
        return None
