"""Orders from the chart: a buy or an intraday short sale with its stoploss and target, placed, moved and removed
from the price chart.

A chart order is an ordinary desk order (``Desk.preview`` / ``Desk.place``: the same risk limits, kill switch,
trade window, typed confirmation and journal) plus the protection the desk places for it once the entry fills.
For a buy (long):

- Paper, and live delivery (CNC): one GTT. Stoploss and target together are an OCO GTT (one cancels the other);
  only one of them is a single-trigger GTT. Zerodha holds a live GTT, so it works with the desk closed.
- Live intraday (MIS): Zerodha has no GTT for MIS, and two resting sell orders could both fill. So the desk keeps
  ONE sell order at the exchange: a stoploss (SL) order at the stoploss. When the price reaches the target the
  desk changes that same order into a limit sell at the target. The stoploss works with the desk closed; the
  target needs the desk running. Zerodha squares off whatever is left near the close.

A short sale of shares is intraday (MIS) only: Indian exchanges allow no delivery short. Its stoploss sits above the
price and its target below, and the exit is a buy:

- Live: the same single order as an intraday buy, mirrored: a stoploss (SL) buy, changed into a limit buy at the target.
- Paper: the desk watches the stoploss and target and buys back at the last price (every 20 seconds while it runs),
  and buys back every paper short at the square-off time in Settings, as Zerodha does for a real one.

A sale counts as a new position for the kill switch, the trade window, the size and count limits and the risk limit
per trade. A Sell of shares you hold, and a Buy while you are short intraday (it buys back), are plain orders with
no stoploss or target.

Every change to a placed stoploss or target is checked again (the price must still lie between them, and a
looser stoploss must stay inside the risk limit per trade) and needs a confirmation.
"""

from __future__ import annotations

import json
import math
import secrets
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

from . import market
from .broker import TOKEN_SECONDS, Desk, OrderRejected
from .kite import KiteError

KEEP_CLOSED = 40                 # finished chart orders kept for the chart and its list
STOP_LIMIT_GAP = 0.005           # an intraday stoploss order's limit price sits 0.5% under its trigger
EXIT_GAP = 0.005                 # a limit that must fill now (stop hit after the target was working) sits 0.5% under
GTT_MIN_GAP = 0.0025             # Zerodha refuses a single-trigger GTT within 0.25% of the last price
OPEN_STATES = ("waiting", "protected", "unprotected")


def _price(raw: dict, key: str, label: str) -> float | None:
    value = raw.get(key)
    if value in (None, "", 0):
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value <= 0:
        raise OrderRejected([f"{label} must be a price above zero."])
    return market.round_to_tick(float(value))


def _rs(value: float) -> str:
    return f"₹{value:,.2f}"


def _short(item: dict) -> bool:
    return item.get("side") == "short"


def _exit_side(item: dict) -> str:
    return "BUY" if _short(item) else "SELL"


ENTRY_WORD = {"long": "buy", "short": "short sale"}


class ChartOrders:
    def __init__(self, desk: Desk, path: Path):
        self.desk, self.path = desk, Path(path)
        self._lock = threading.RLock()
        self._pending: dict[str, dict] = {}        # desk order token -> the stoploss and target it carries
        self._changes: dict[str, dict] = {}        # change token -> the change previewed
        self._items: list[dict] = self._load()

    # -- storage ---------------------------------------------------------------------------------------------
    def _load(self) -> list[dict]:
        try:
            items = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(items, list):
                return [i for i in items if isinstance(i, dict) and i.get("id")]
        except (OSError, ValueError):
            pass
        return []

    def _save(self) -> None:
        done = [i for i in self._items if i["state"] not in OPEN_STATES]
        drop = {i["id"] for i in done[:-KEEP_CLOSED]} if len(done) > KEEP_CLOSED else set()
        self._items = [i for i in self._items if i["id"] not in drop]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._items, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _now(self) -> str:
        return self.desk._now().strftime("%Y-%m-%d %H:%M:%S")

    def _log(self, item: dict, text: str) -> None:
        item.setdefault("log", []).append(f"{self._now()[11:16]} {text}")
        del item["log"][:-20]
        item["updated"] = self._now()

    def _find(self, item_id: str) -> dict:
        found = next((i for i in self._items if i["id"] == item_id), None)
        if found is None:
            raise OrderRejected(["That chart order is not on the desk any more."])
        return found

    # -- how an order is protected ------------------------------------------------------------------------------
    @staticmethod
    def uses_gtt(mode: str, product: str, side: str = "long") -> bool:
        return side == "long" and (mode == "paper" or product == "CNC")

    @classmethod
    def kind_of(cls, mode: str, product: str, side: str = "long") -> str:
        """How a chart order is protected: ``gtt``, ``order`` (one live intraday exit order) or ``watch`` (a paper
        short, followed by the desk)."""
        if cls.uses_gtt(mode, product, side):
            return "gtt"
        return "watch" if mode == "paper" else "order"

    def _kind(self, item: dict) -> str:
        return self.kind_of(item["mode"], item["product"], item.get("side", "long"))

    def _protection_text(self, mode: str, product: str, stop, target, side: str = "long") -> str:
        if not stop and not target:
            return ""
        both = stop and target
        if side == "short":
            what = "the stoploss and target" if both else "the " + ("stoploss" if stop else "target")
            if mode == "paper":
                return (f"The desk watches {what} and buys back at the last price when the price reaches "
                        f"{'either' if both else 'it'} (checked every 20 seconds while the desk runs). Every paper "
                        f"short is bought back at the square-off time in Settings.")
            if both:
                return ("After the short sale fills, the desk places a stoploss buy order at Zerodha. When the price "
                        "falls to the target, the desk changes that same order into a limit buy at the target, so only "
                        "one buy order is ever working. The target needs the desk running; the stoploss does not. "
                        "Zerodha buys back intraday shorts near 15:20 IST.")
            return ("After the short sale fills, the desk places a stoploss buy order at Zerodha." if stop else
                    "After the short sale fills, the desk places a limit buy at the target at Zerodha.")
        if self.uses_gtt(mode, product):
            where = "on the paper book, checked about once a minute in market hours" if mode == "paper" \
                else "at Zerodha, valid for a year, so it works with the desk closed"
            kind = "an OCO GTT (when one triggers the other is cancelled)" if both else "a GTT"
            return f"After the buy fills, the desk places {'the stoploss and target' if both else 'the ' + ('stoploss' if stop else 'target')} as {kind} {where}."
        if both:
            return ("After the buy fills, the desk places a stoploss order at Zerodha. When the price reaches the target, "
                    "the desk changes that same order into a limit sell at the target, so only one sell order is ever "
                    "working. The target needs the desk running; the stoploss does not. Zerodha squares off intraday "
                    "positions near 15:20 IST.")
        if stop:
            return "After the buy fills, the desk places a stoploss order at Zerodha."
        return "After the buy fills, the desk places a limit sell at the target at Zerodha."

    def _margin(self, order: dict, mode: str, value: float) -> dict:
        from . import margin as margin_module

        if self.desk.kite.connected and margin_module.supported(self.desk.kite):
            try:
                got = margin_module.basket(self.desk.kite, [{**order, "price": order.get("price") or 0}], order["product"])
                return {"amount": got.get("total"), "source": "Zerodha"}
            except margin_module.MarginUnavailable as exc:
                reason = str(exc)
            except KiteError as exc:
                reason = str(exc)
        else:
            reason = margin_module.why_not(self.desk.kite) or ""
        if order["product"] == "CNC":
            return {"amount": round(value, 2), "source": "estimate", "note": "Delivery needs the full order value."}
        return {"amount": round(value / 5, 2), "source": "estimate",
                "note": "About 20% of the order value; Zerodha's intraday margin differs by stock. " + reason}

    # -- a new order ------------------------------------------------------------------------------------------------
    def preview(self, raw: dict) -> dict:
        mode = str(raw.get("mode", "paper"))
        order_raw = dict(raw.get("order") or {})
        if str(order_raw.get("exchange", "")).upper() not in ("NSE", "BSE"):
            raise OrderRejected(["Chart orders are for NSE and BSE shares."])
        side = str(order_raw.get("side", "")).upper()
        stop, target = _price(raw, "stop", "Stoploss"), _price(raw, "target", "Target")
        kind = self._entry_kind(mode, order_raw)
        if kind in ("sell", "cover") and (stop or target):
            raise OrderRejected(["Stoploss and target are for a new buy or short sale. "
                                 + ("This sell sells shares you hold." if kind == "sell" else
                                    "This buy buys back your intraday short.")])
        order_raw.pop("intent", None)
        if kind in ("short", "cover"):
            order_raw["intent"] = "open" if kind == "short" else "close"
        order_raw.setdefault("variety", "regular")
        review = self.desk.preview(order_raw, mode, stop=stop if kind in ("long", "short") else None, manual=True)
        errors, warnings = list(review["errors"]), list(review["warnings"])
        o = review["order"]
        entry = (review["last_price"] if mode == "paper" and review["last_price"] else None) or o["price"] or review["last_price"]
        reward = None
        if kind in ("sell", "cover") and mode == "live" and self._open_exit_orders(o["exchange"], o["tradingsymbol"]):
            errors.append("A chart stoploss or target order for this share is still working at Zerodha. Remove it first "
                          "(Remove in the list under the ticket): after this order it would open a new position when it fills.")
        if kind == "short" and mode == "paper" and self.desk.window_status()["square_off"]:
            errors.append(f"It is past the square-off time ({self.desk.settings.get('square_off')} IST, Settings): the desk "
                          f"buys paper shorts back then, so this one would be bought back at once.")
        if kind == "short" and o["product"] != "MIS":
            errors.append("Shares can be sold short only intraday. Indian exchanges allow no delivery (positional) "
                          "short; that needs a futures contract.")
        if entry and kind in ("long", "short"):
            long = kind == "long"
            word = "buy" if long else "sale"
            if stop and (stop >= entry if long else stop <= entry):
                errors.append(f"The stoploss {_rs(stop)} must be {'below' if long else 'above'} the {word} price {_rs(entry)}.")
            if target and (target <= entry if long else target >= entry):
                errors.append(f"The target {_rs(target)} must be {'above' if long else 'below'} the {word} price {_rs(entry)}.")
            if target and (target > entry if long else target < entry):
                from .sizing import round_trip_charges
                reward = round(abs(target - entry) * o["quantity"]
                               - round_trip_charges(side, o["quantity"], entry, target, o["product"], o["exchange"],
                                                    o["tradingsymbol"]), 2)
            if self.uses_gtt(mode, o["product"], kind) and bool(stop) != bool(target):
                level = stop or target
                if abs(level - entry) / entry < GTT_MIN_GAP:
                    errors.append(f"The {'stoploss' if stop else 'target'} {_rs(level)} is within 0.25% of the price. "
                                  f"Zerodha refuses a GTT that close; move it further away.")
            if mode == "live" and (stop or target):
                if self.uses_gtt(mode, o["product"], kind) and "place_gtt_order" not in self.desk.kite.tools:
                    errors.append("The connected Kite server cannot place GTTs, so the stoploss and target could not "
                                  "be placed. Use the desk's own kite-mcp-server.")
                if not self.uses_gtt(mode, o["product"], kind) and stop and target and "modify_order" not in self.desk.kite.tools:
                    errors.append("The connected Kite server cannot change orders, which an intraday stoploss with a "
                                  "target needs. Use the desk's own kite-mcp-server, or set only a stoploss.")
        if mode == "live" and o["product"] == "MIS" and kind in ("long", "short") and o["order_type"] == "LIMIT" \
                and (stop or target):
            warnings.append(f"A limit {ENTRY_WORD[kind]} that has not filled carries no stoploss yet: the desk places it "
                            f"once Kite reports the fill (checked every 20 seconds while the desk runs).")
        if kind == "short":
            warnings.append("A short sale loses money when the price rises, with no limit to how far it can rise. "
                            + ("Zerodha buys it back near 15:20 IST if you do not." if mode == "live" else
                               "The desk buys paper shorts back at the square-off time in Settings."))
        token = review["token"]
        if token and errors:
            self.desk._take(token)          # this order is not to be placed: its token must not work
            token = None
        if token:
            with self._lock:
                now = time.monotonic()
                self._pending = {t: v for t, v in self._pending.items() if v["expires"] > now}
                self._pending[token] = {"stop": stop, "target": target, "mode": mode, "side": kind,
                                        "expires": now + TOKEN_SECONDS}
        value = review["value"]
        return {**review, "ok": not errors, "errors": errors, "warnings": warnings, "token": token, "kind": kind,
                "stop": stop, "target": target, "entry": entry, "reward": reward,
                "protection": self._protection_text(mode, o["product"], stop, target, kind)
                if kind in ("long", "short") else "",
                "margin": self._margin(o, mode, value) if value and kind in ("long", "short") else None}

    def _entry_kind(self, mode: str, order_raw: dict) -> str:
        """What a chart Buy or Sell does here: ``long`` (a new buy), ``sell`` (sells shares held), ``short`` (an
        intraday short sale) or ``cover`` (a buy that buys back an intraday short)."""
        side = str(order_raw.get("side", "")).upper()
        product = str(order_raw.get("product", "CNC")).upper()
        where = SimpleNamespace(exchange=str(order_raw.get("exchange", "")).upper(),
                                tradingsymbol=str(order_raw.get("tradingsymbol", "")).upper())
        if mode == "paper":
            held = self.desk.paper.quantity(f"{where.exchange}:{where.tradingsymbol}")
        elif product == "MIS" and self.desk.kite.connected:
            held = self.desk.live_intraday_position(where)
            if held is None:
                raise OrderRejected(["Could not read your intraday positions from Kite to tell whether this order "
                                     "opens or closes one. Try again."])
        else:
            held = 0
        if side == "SELL":
            if order_raw.pop("short", False) is True:
                if held > 0:
                    raise OrderRejected([f"You hold {held} {where.tradingsymbol}. Sell those first; a short sale "
                                         f"starts from no position."])
                return "short"
            quantity = order_raw.get("quantity")
            if mode == "live" and product == "MIS" and isinstance(quantity, int) and quantity > max(held, 0):
                raise OrderRejected([f"You hold {max(held, 0)} {where.tradingsymbol} intraday; selling {quantity} would "
                                     f"sell short. To sell short, choose Short sell."])
            return "sell"
        order_raw.pop("short", None)
        return "cover" if product == "MIS" and held < 0 else "long"

    def _open_exit_orders(self, exchange: str, tradingsymbol: str) -> bool:
        with self._lock:
            return any(i["state"] == "protected" and (i.get("protection") or {}).get("kind") == "order"
                       and i["exchange"] == exchange and i["tradingsymbol"] == tradingsymbol for i in self._items)

    def place(self, token: str, phrase: str) -> dict:
        with self._lock:
            plan = self._pending.pop(token, None)
        placed = self.desk.place(token, phrase)
        if not plan or not (plan["stop"] or plan["target"]):
            return {"placed": placed, "chart_order": None}
        item = {"id": uuid.uuid4().hex[:10], "mode": placed["mode"], "side": plan.get("side", "long"),
                "exchange": placed["exchange"],
                "tradingsymbol": placed["tradingsymbol"], "quantity": placed["quantity"], "product": placed["product"],
                "entry_order_id": placed.get("order_id"), "entry_price": placed.get("fill_price") or placed.get("price") or None,
                "stop": plan["stop"], "target": plan["target"], "state": "waiting", "protection": None,
                "created": self._now(), "updated": self._now(), "log": []}
        with self._lock:
            self._items.append(item)
            word = ENTRY_WORD[item["side"]]
            if item["mode"] == "paper":
                self._log(item, f"Paper {word} filled at {_rs(item['entry_price'])}.")
                self._protect(item)
            else:
                self._log(item, f"{word.capitalize()} sent to Kite (order {item['entry_order_id']}). Stoploss and "
                                f"target follow the fill.")
            self._save()
        return {"placed": placed, "chart_order": dict(item)}

    def _source(self, item: dict) -> str:
        return f"chart:{item['id']}"

    def _exit(self, item: dict, order_type: str, price: float, trigger: float = 0.0) -> dict:
        """The order that closes a chart position: a sell for a buy, a buy back (cover) for a short sale."""
        raw = {"exchange": item["exchange"], "tradingsymbol": item["tradingsymbol"], "side": _exit_side(item),
               "quantity": item["quantity"], "product": item["product"], "order_type": order_type, "price": price}
        if trigger:
            raw["trigger_price"] = trigger
        if _short(item):
            raw["intent"] = "close"
        return raw

    def _gtt_raw(self, item: dict, stop, target) -> dict:
        raw = {"exchange": item["exchange"], "tradingsymbol": item["tradingsymbol"], "side": "SELL",
               "quantity": item["quantity"]}
        return {**raw, "kind": "oco", "target": target, "stop": stop} if stop and target \
            else {**raw, "kind": "single", "trigger": stop or target}

    def _stop_order(self, item: dict, stop: float) -> dict:
        gap = STOP_LIMIT_GAP if _short(item) else -STOP_LIMIT_GAP
        return self._exit(item, "SL", market.round_to_tick(stop * (1 + gap), _exit_side(item)), stop)

    def _protect(self, item: dict) -> None:
        """Place the stoploss and target for a filled buy. On failure the item is marked unprotected and the user
        is told at once: an open position without its stoploss must not pass unseen."""
        stop, target = item["stop"], item["target"]
        try:
            if self._kind(item) == "watch":
                item["protection"] = {"kind": "watch"}
                self._log(item, "The desk watches the "
                          + " and ".join(x for x in (stop and f"stoploss {_rs(stop)}", target and f"target {_rs(target)}") if x)
                          + " and buys back at the last price when one is reached.")
            elif self._kind(item) == "gtt":
                placed = self.desk.auto_gtt(self._gtt_raw(item, stop, target), item["mode"], self._source(item))
                item["protection"] = {"kind": "gtt", "id": placed["order_id"]}
                what = " and ".join(x for x in (stop and f"stoploss {_rs(stop)}", target and f"target {_rs(target)}") if x)
                self._log(item, f"GTT {placed['order_id']} placed: {what}.")
            elif stop:
                placed = self.desk.auto_place(self._stop_order(item, stop), "live", self._source(item))
                item["protection"] = {"kind": "order", "id": placed.get("order_id"), "working": "stop"}
                self._log(item, f"Stoploss order {placed.get('order_id')} placed at {_rs(stop)}"
                                + (f"; the desk watches the target {_rs(target)}." if target else "."))
            else:
                placed = self.desk.auto_place(self._exit(item, "LIMIT", target), "live", self._source(item))
                item["protection"] = {"kind": "order", "id": placed.get("order_id"), "working": "target"}
                self._log(item, f"Target order {placed.get('order_id')} placed at {_rs(target)}.")
            item["state"] = "protected"
        except (OrderRejected, KiteError) as exc:
            item["state"], item["protection"] = "unprotected", None
            self._log(item, f"Stoploss and target NOT placed: {exc}")
            self.desk.notifier.notify(f"{item['tradingsymbol']}: stoploss and target not placed",
                                      f"{item['mode']} chart order {item['id']}: {exc}. The position is unprotected; "
                                      f"place them again from the chart or close it.", "bad")

    # -- changes to a placed order ---------------------------------------------------------------------------------
    def change_preview(self, item_id: str, raw: dict) -> dict:
        """Check a new stoploss and target for a placed chart order (a dragged line) and issue the token that applies
        it. ``raw`` gives both levels as they should be after the change; a missing one is removed."""
        with self._lock:
            item = dict(self._find(item_id))
        stop, target = _price(raw, "stop", "Stoploss"), _price(raw, "target", "Target")
        errors, warnings = [], []
        if item["state"] not in OPEN_STATES:
            errors.append("This chart order is finished; there is nothing to change.")
        if not stop and not target:
            errors.append("Keep a stoploss or a target, or remove both with Remove.")
        ltp = self.desk.last_price(f"{item['exchange']}:{item['tradingsymbol']}")[0]
        if not ltp:
            errors.append("No price is available to check the change against.")
        else:
            short = _short(item)
            now = "To buy back now, use Buy." if short else "To sell now, use Sell."
            if stop and (stop <= ltp if short else stop >= ltp):
                errors.append(f"The stoploss {_rs(stop)} must be {'above' if short else 'below'} the last price {_rs(ltp)}. {now}")
            if target and (target >= ltp if short else target <= ltp):
                errors.append(f"The target {_rs(target)} must be {'below' if short else 'above'} the last price {_rs(ltp)}. {now}")
            if self._kind(item) == "gtt" and bool(stop) != bool(target) \
                    and abs((stop or target) - ltp) / ltp < GTT_MIN_GAP:
                errors.append("Zerodha refuses a GTT within 0.25% of the last price; move the line further away.")
        if item["state"] == "protected" and self._kind(item) == "order":
            if bool(stop) != bool(item["stop"]) or bool(target) != bool(item["target"]):
                errors.append("An intraday chart order can move its stoploss and target but not add or drop one. "
                              "Remove it and place the stoploss and target again.")
            if not market.session_status(self.desk._now())["open"]:
                errors.append("The market is closed: an intraday order can be changed only in market hours.")
            if "modify_order" not in self.desk.kite.tools:
                errors.append("The connected Kite server cannot change orders. Use the desk's own kite-mcp-server.")
        if item["state"] == "protected" and item["mode"] == "live" and self._kind(item) == "gtt" \
                and "modify_gtt_order" not in self.desk.kite.tools:
            warnings.append("This Kite server cannot change a GTT, so the desk deletes the old GTT and places a new "
                            "one. For a moment nothing protects the position, and if the new GTT is refused the "
                            "position stays unprotected (you are told at once).")
        risk = None
        entry = item.get("entry_price")
        if stop and entry and (not item["stop"] or (stop > item["stop"] if _short(item) else stop < item["stop"])):
            cfg = self.desk.settings.all()
            from .sizing import trade_risk
            capital = cfg["risk_capital"] or cfg["paper_capital"]
            loss = trade_risk("SELL" if _short(item) else "BUY", item["quantity"], entry, stop, item["product"],
                              item["exchange"], item["tradingsymbol"])
            share = loss / capital * 100 if capital else 0.0
            risk = {"loss": round(loss, 2), "of_capital_pct": round(share, 2), "limit_pct": cfg["max_risk_pct"]}
            if cfg["max_risk_pct"] and share > cfg["max_risk_pct"]:
                errors.append(f"At {_rs(stop)} this trade loses about ₹{loss:,.0f}, {share:.1f}% of your ₹{capital:,.0f} "
                              f"capital. Your limit is {cfg['max_risk_pct']:g}% a trade (Settings).")
        token = None
        if not errors:
            token = secrets.token_urlsafe(18)
            with self._lock:
                now = time.monotonic()
                self._changes = {t: v for t, v in self._changes.items() if v["expires"] > now}
                self._changes[token] = {"id": item_id, "stop": stop, "target": target, "expires": now + TOKEN_SECONDS}
        return {"ok": not errors, "errors": errors, "warnings": warnings, "token": token, "mode": item["mode"],
                "before": {"stop": item["stop"], "target": item["target"]}, "after": {"stop": stop, "target": target},
                "last_price": ltp, "risk": risk, "item": item}

    def change(self, token: str) -> dict:
        with self._lock:
            change = self._changes.pop(str(token), None)
            if not change or change["expires"] < time.monotonic():
                raise OrderRejected(["This confirmation has expired or was already used. Move the line again."])
            item = self._find(change["id"])
            stop, target = change["stop"], change["target"]
            before = (item["stop"], item["target"])
            if item["state"] in ("waiting", "unprotected"):
                item["stop"], item["target"] = stop, target
                self._log(item, f"Stoploss {_rs(stop) if stop else 'none'}, target {_rs(target) if target else 'none'}.")
                if item["state"] == "unprotected":
                    self._protect(item)
            elif self._kind(item) == "gtt":
                self._change_gtt(item, stop, target)
            elif self._kind(item) == "watch":
                item["stop"], item["target"] = stop, target
                self._log(item, f"Now watching stoploss {_rs(stop) if stop else 'none'}, target "
                                f"{_rs(target) if target else 'none'}.")
            else:
                self._change_order(item, stop, target)
            if (item["stop"], item["target"]) != before:
                self.desk._journal({"time_ist": self._now(), "mode": item["mode"], "source": self._source(item), "ok": True,
                                    "exchange": item["exchange"], "tradingsymbol": item["tradingsymbol"],
                                    "side": _exit_side(item), "quantity": item["quantity"], "change": True,
                                    "note": f"stoploss {before[0]} -> {item['stop']}, target {before[1]} -> {item['target']}"})
            self._save()
            return dict(item)

    def _change_gtt(self, item: dict, stop, target) -> None:
        old = item["protection"]["id"]
        raw = self._gtt_raw(item, stop, target)
        if item["mode"] == "live" and "modify_gtt_order" in self.desk.kite.tools:
            review = self.desk.gtt_preview(raw, "live")
            if not review["ok"]:
                raise OrderRejected(review["errors"])
            pending = self.desk._take(review["token"])
            try:
                self.desk.kite.modify_gtt(old, pending["gtt"].kite_args(pending["ltp"]))
            except KiteError as exc:
                raise OrderRejected([f"Kite did not change GTT {old}: {exc}. The old stoploss and target still stand."]) from exc
            item["stop"], item["target"] = stop, target
            self._log(item, f"GTT {old} changed: stoploss {_rs(stop) if stop else 'none'}, target {_rs(target) if target else 'none'}.")
            return
        status = (self.desk.gtt_status(item["mode"], old) or {}).get("status", "gone")
        if status != "active":
            raise OrderRejected([f"GTT {old} is {status}; it cannot be changed. Refresh the chart."])
        review = self.desk.gtt_preview(raw, item["mode"])
        if not review["ok"]:
            raise OrderRejected(review["errors"])
        self.desk._take(review["token"])
        try:
            self.desk.cancel_gtt(item["mode"], old)
        except KiteError as exc:
            raise OrderRejected([f"GTT {old} could not be deleted to change it: {exc}. The old one still stands."]) from exc
        item["stop"], item["target"] = stop, target
        self._log(item, f"GTT {old} deleted to change it.")
        self._protect(item)

    def _change_order(self, item: dict, stop, target) -> None:
        protection = item["protection"]
        if protection["working"] == "stop" and stop != item["stop"]:
            order = self._stop_order(item, stop)
            try:
                self.desk.kite.modify_order(protection["id"], order_type="SL", quantity=item["quantity"],
                                            trigger_price=order["trigger_price"], price=order["price"])
            except KiteError as exc:
                raise OrderRejected([f"Kite did not change the stoploss order: {exc}. The old stoploss stands."]) from exc
            self._log(item, f"Stoploss order {protection['id']} moved to {_rs(stop)}.")
        elif protection["working"] == "target" and target != item["target"]:
            try:
                self.desk.kite.modify_order(protection["id"], order_type="LIMIT", quantity=item["quantity"], price=target)
            except KiteError as exc:
                raise OrderRejected([f"Kite did not change the target order: {exc}. The old target stands."]) from exc
            self._log(item, f"Target order {protection['id']} moved to {_rs(target)}.")
        item["stop"], item["target"] = stop, target

    def remove(self, item_id: str) -> dict:
        """Remove a chart order's stoploss and target (and cancel its entry if it has not filled). The position stays."""
        with self._lock:
            item = self._find(item_id)
            if item["state"] not in OPEN_STATES:
                raise OrderRejected(["This chart order is already finished."])
            try:
                if item["state"] == "waiting" and item["mode"] == "live" and item.get("entry_order_id"):
                    self.desk.kite.cancel_order(item["entry_order_id"])
                    self._log(item, f"Cancel sent for the {ENTRY_WORD[item.get('side', 'long')]} order {item['entry_order_id']}.")
                p = item.get("protection")
                if p and p["kind"] == "gtt":
                    self.desk.cancel_gtt(item["mode"], p["id"])
                elif p and p["kind"] == "order":
                    self.desk.kite.cancel_order(p["id"])
            except KiteError as exc:
                raise OrderRejected([f"Kite refused: {exc}. Nothing was removed; check the order in Kite."]) from exc
            item["state"], item["closed"] = "removed", "Stoploss and target removed by you."
            self._log(item, item["closed"])
            self._save()
            return dict(item)

    # -- the clock ---------------------------------------------------------------------------------------------------
    def tick(self) -> None:
        """Follow every open chart order: a live entry's fill, a GTT that triggered, the intraday target, a paper
        short's levels; and buy back paper shorts at the square-off time."""
        with self._lock:
            window = self.desk.window_status()
            if window["market"]:
                self._square_off_paper_shorts(window)
            open_items = [i for i in self._items if i["state"] in ("waiting", "protected")]
            if not open_items:
                return
            market_open = window["market"]
            live = [i for i in open_items if i["mode"] == "live"]
            orders = None
            if live and market_open and self.desk.kite.connected:
                try:
                    orders = {str(o.get("order_id")): o for o in self.desk.kite.orders()}
                except KiteError:
                    orders = None
            changed = False
            for item in open_items:
                try:
                    changed |= self._follow(item, orders, market_open)
                except (KiteError, OrderRejected) as exc:
                    self._log(item, f"Check failed: {exc}")
                    changed = True
            if changed:
                self._save()

    def _cover_paper(self, instrument: str, quantity: int, ltp: float, source: str) -> dict:
        exchange, symbol = instrument.split(":", 1)
        return self.desk.auto_place({"exchange": exchange, "tradingsymbol": symbol, "side": "BUY", "quantity": quantity,
                                     "product": "MIS", "order_type": "LIMIT", "intent": "close",
                                     "price": market.round_to_tick(ltp * (1 + EXIT_GAP), "BUY")}, "paper", source)

    def _square_off_paper_shorts(self, window: dict) -> None:
        """Buy back every paper short sale at the square-off time in Settings, or at the first chance on a later day
        (the desk was not running at the close): a paper short is intraday, like a real one."""
        today = self._now()[:10]
        for instrument, held in self.desk.paper.short_positions().items():
            if not window["square_off"] and (held.get("opened") or today) >= today:
                continue
            ltp = self.desk.last_price(instrument)[0]
            if not ltp:
                continue
            exchange, symbol = instrument.split(":", 1)
            mine = [i for i in self._items if i["mode"] == "paper" and i["state"] in OPEN_STATES
                    and i["exchange"] == exchange and i["tradingsymbol"] == symbol and _short(i)]
            try:
                placed = self._cover_paper(instrument, -int(held["quantity"]),
                                           ltp, self._source(mine[0]) if mine else "manual")
            except OrderRejected as exc:
                self.desk.notifier.notify(f"Paper short not bought back: {symbol}", str(exc), "bad")
                continue
            text = f"Square-off: bought back {-int(held['quantity'])} at {_rs(placed['fill_price'])}."
            for item in mine:
                self._close(item, text, "warn")
            if not mine:
                self.desk.notifier.notify(f"Paper short {symbol}: {text}", "intraday square-off", "warn")
            self._save()

    def _close(self, item: dict, text: str, level: str = "good") -> None:
        item["state"], item["closed"] = "closed", text
        self._log(item, text)
        self.desk.notifier.notify(f"Chart order {item['tradingsymbol']}: {text}", f"{item['mode']} · {item['id']}", level)

    def _follow(self, item: dict, orders: dict | None, market_open: bool) -> bool:
        p = item.get("protection")
        if item["state"] == "waiting":
            if orders is None:
                return False
            row = orders.get(str(item.get("entry_order_id")))
            if row is None:
                if item["created"][:10] != self._now()[:10]:
                    self._close(item, "The entry order is no longer listed by Kite, so it did not fill. Nothing to protect.", "warn")
                    return True
                return False
            status, filled = str(row.get("status", "")).upper(), int(row.get("filled_quantity") or 0)
            if status == "COMPLETE" or (status in ("CANCELLED", "REJECTED") and filled):
                item["quantity"] = filled or item["quantity"]
                item["entry_price"] = float(row.get("average_price") or item["entry_price"] or 0) or item["entry_price"]
                self._log(item, f"{ENTRY_WORD[item.get('side', 'long')].capitalize()} filled: {item['quantity']} at "
                                f"{_rs(item['entry_price'] or 0)}.")
                self._protect(item)
                return True
            if status in ("CANCELLED", "REJECTED"):
                self._close(item, f"The {ENTRY_WORD[item.get('side', 'long')]} was {status.lower()} "
                                  f"({row.get('status_message') or 'no reason given'}). Nothing to protect.", "warn")
                return True
            return False
        if p and p["kind"] == "watch":
            return self._watch(item, market_open)
        if p and p["kind"] == "gtt":
            if item["mode"] == "live" and not (market_open and self.desk.kite.connected):
                return False
            status = self.desk.gtt_status(item["mode"], p["id"])
            state = (status or {}).get("status", "gone")
            if state == "active":
                return False
            if state == "triggered":
                leg = (status or {}).get("leg")
                fill = (status or {}).get("fill_price")
                name = {"stop": "Stoploss", "target": "Target"}.get(leg, "Stoploss or target")
                self._close(item, f"{name} hit{f' at {_rs(fill)}' if fill else ''}: GTT {p['id']} sold the position."
                            + ("" if item["mode"] == "paper" else " Check in Kite that its sell filled."),
                            "warn" if leg == "stop" else "good")
            else:
                self._close(item, f"GTT {p['id']} is {state}. The position is no longer protected by it.", "bad")
            return True
        if p and p["kind"] == "order":
            if orders is None:
                return False
            row = orders.get(str(p["id"]))
            if row is None:
                return False
            status = str(row.get("status", "")).upper()
            if status == "COMPLETE":
                price = float(row.get("average_price") or 0)
                name = "Target" if p["working"] == "target" else "Stoploss"
                self._close(item, f"{name} hit: {'bought back' if _short(item) else 'sold'} {item['quantity']} at "
                                  f"{_rs(price)}.", "good" if name == "Target" else "warn")
                return True
            if status in ("CANCELLED", "REJECTED"):
                self._close(item, f"The {'target' if p['working'] == 'target' else 'stoploss'} order was {status.lower()} "
                                  f"({row.get('status_message') or 'outside the desk'}). Check your position in Kite.", "bad")
                return True
            ltp = self.desk.last_price(f"{item['exchange']}:{item['tradingsymbol']}")[0]
            if not ltp or not market_open:
                return False
            short = _short(item)
            word = "buy" if short else "sell"
            if p["working"] == "stop" and item["target"] and (ltp <= item["target"] if short else ltp >= item["target"]):
                self.desk.kite.modify_order(p["id"], order_type="LIMIT", quantity=item["quantity"], price=item["target"])
                p["working"] = "target"
                self._log(item, f"Price {_rs(ltp)} reached the target: order {p['id']} is now a limit {word} at {_rs(item['target'])}.")
                return True
            if p["working"] == "target" and item["stop"] and (ltp >= item["stop"] if short else ltp <= item["stop"]):
                price = market.round_to_tick(ltp * (1 + EXIT_GAP if short else 1 - EXIT_GAP), _exit_side(item))
                self.desk.kite.modify_order(p["id"], order_type="LIMIT", quantity=item["quantity"], price=price)
                p["working"] = "exit"
                self._log(item, f"Price {_rs(ltp)} went back to the stoploss before the target filled: order {p['id']} "
                                f"changed to a limit {word} at {_rs(price)} to get out.")
                return True
        return False

    def _watch(self, item: dict, market_open: bool) -> bool:
        """A paper short's stoploss and target: buy back at the last price when either is reached."""
        instrument = f"{item['exchange']}:{item['tradingsymbol']}"
        short_now = max(-self.desk.paper.quantity(instrument), 0)
        if short_now == 0:
            self._close(item, "The short was bought back outside this chart order; nothing is left to protect.", "warn")
            return True
        changed = short_now < item["quantity"]
        if changed:
            self._log(item, f"Part of the short was bought back elsewhere: {short_now} left to protect.")
            item["quantity"] = short_now
        ltp = self.desk.last_price(instrument)[0] if market_open else None
        if not ltp:
            return changed
        leg = "stop" if item["stop"] and ltp >= item["stop"] else "target" if item["target"] and ltp <= item["target"] else ""
        if not leg:
            return changed
        placed = self._cover_paper(instrument, item["quantity"], ltp, self._source(item))
        self._close(item, f"{'Stoploss' if leg == 'stop' else 'Target'} hit: bought back {item['quantity']} at "
                          f"{_rs(placed['fill_price'])}.", "warn" if leg == "stop" else "good")
        return True

    # -- the view ----------------------------------------------------------------------------------------------------
    def view(self, exchange: str = "", tradingsymbol: str = "") -> dict:
        with self._lock:
            items = [dict(i) for i in self._items
                     if (not tradingsymbol or (i["tradingsymbol"] == tradingsymbol.upper()
                                               and (not exchange or i["exchange"] == exchange.upper())))]
        tools = self.desk.kite.tools if self.desk.kite.connected else {}
        return {"items": list(reversed(items)), "open": sum(1 for i in items if i["state"] in OPEN_STATES),
                "can_modify": {"gtt": "modify_gtt_order" in tools, "order": "modify_order" in tools},
                "check_seconds": 20}

