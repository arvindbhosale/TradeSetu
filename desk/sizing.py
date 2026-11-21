"""How many to buy so that a stoploss hit loses a set share of capital.

Position size = (capital x risk %) / (distance from entry to stoploss), rounded down to whole lots, with the
round-trip charges of the trade counted inside the risk. The stoploss is a plan, not a guarantee: a price that
gaps through it (overnight news, an upper or lower circuit) fills worse, so the real loss can be larger.
"""

from __future__ import annotations

import math

from . import broker

# Largest risk on one trade the sizer accepts, % of capital. Above this one bad trade is a large hole.
MAX_RISK_PCT = 10.0


def _number(raw: dict, key: str, label: str, *, required: bool = True, minimum: float = 0.0,
            maximum: float | None = None) -> float | None:
    value = raw.get(key)
    if value in (None, ""):
        if required:
            raise ValueError(f"{label} is needed")
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a number")
    if value <= minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{label} must be above {minimum:g}" + (f" and at most {maximum:g}" if maximum else ""))
    return float(value)


def round_trip_charges(side: str, quantity: int, entry: float, exit_price: float, product: str, exchange: str,
                       tradingsymbol: str) -> float:
    """Charges of opening at ``entry`` and closing at ``exit_price``."""
    if quantity <= 0:
        return 0.0
    close = "SELL" if side == "BUY" else "BUY"
    try:
        return (broker.fees(side, quantity, entry, product, exchange, tradingsymbol).total
                + broker.fees(close, quantity, exit_price, product, exchange, tradingsymbol).total)
    except ValueError:
        return 0.0


def trade_risk(side: str, quantity: int, entry: float, stop: float, product: str = "CNC", exchange: str = "NSE",
               tradingsymbol: str = "", factor: float = 1.0) -> float:
    """Rupees lost if the stoploss fills at its price, charges included."""
    per_unit = (entry - stop) if side == "BUY" else (stop - entry)
    return max(per_unit, 0) * quantity * factor + round_trip_charges(side, quantity, entry, stop, product, exchange,
                                                                     tradingsymbol)


def size(raw: dict, capital: float, max_order_value: float = 0.0) -> dict:
    """The quantity for one trade. ``raw``: entry, stop, risk_pct, and optionally target, side (BUY for a long,
    SELL for a short future), lot (lot size, default 1), exchange, product, tradingsymbol."""
    entry = _number(raw, "entry", "Entry price")
    stop = _number(raw, "stop", "Stoploss price")
    risk_pct = _number(raw, "risk_pct", "Risk per trade %", maximum=MAX_RISK_PCT)
    target = _number(raw, "target", "Target price", required=False)
    side = str(raw.get("side") or "BUY").upper()
    if side not in ("BUY", "SELL"):
        raise ValueError("side must be BUY or SELL")
    lot = raw.get("lot") or 1
    if isinstance(lot, bool) or not isinstance(lot, (int, float)) or int(lot) != lot or lot < 1:
        raise ValueError("Lot size must be a whole number of 1 or more")
    lot = int(lot)
    exchange = str(raw.get("exchange") or "NSE").upper()
    product = str(raw.get("product") or ("NRML" if exchange in broker.DERIVATIVES else "CNC")).upper()
    symbol = str(raw.get("tradingsymbol") or "").upper()
    if capital <= 0:
        raise ValueError("Capital must be above zero")
    if side == "BUY" and stop >= entry:
        raise ValueError("For a buy the stoploss must be below the entry price")
    if side == "SELL" and stop <= entry:
        raise ValueError("For a short the stoploss must be above the entry price")
    if target is not None and ((side == "BUY" and target <= entry) or (side == "SELL" and target >= entry)):
        raise ValueError("The target must be on the profit side of the entry price")
    try:
        factor = broker.multiplier(exchange, symbol) if exchange == "MCX" else 1.0
    except ValueError:
        factor = 1.0

    budget = capital * risk_pct / 100
    per_unit = abs(entry - stop) * factor
    quantity = int(budget // per_unit) // lot * lot
    # Charges sit inside the risk: step down a lot at a time until the whole loss fits.
    while quantity > 0 and trade_risk(side, quantity, entry, stop, product, exchange, symbol, factor) > budget:
        quantity -= lot
    limited, by_risk = [], quantity
    if side == "BUY" and exchange not in broker.DERIVATIVES and quantity * entry > capital:
        quantity = int(capital // entry) // lot * lot
        limited.append("your capital (a delivery buy is paid in full)")
    if max_order_value and exchange not in broker.DERIVATIVES and quantity * entry * factor > max_order_value:
        quantity = int(max_order_value // (entry * factor)) // lot * lot
        limited.append(f"your ₹{max_order_value:,.0f} maximum order value (Settings)")
    risk = trade_risk(side, quantity, entry, stop, product, exchange, symbol, factor) if quantity else 0.0
    out = {
        "quantity": quantity, "lots": quantity // lot if lot > 1 else None, "by_risk": by_risk, "limited_by": limited,
        "capital": round(capital, 2), "risk_pct": risk_pct, "risk_budget": round(budget, 2),
        "risk": round(risk, 2), "risk_of_capital_pct": round(risk / capital * 100, 2),
        "value": round(quantity * entry * factor, 2),
        "charges": round(round_trip_charges(side, quantity, entry, stop, product, exchange, symbol), 2),
        "stop_loss_pct": round(abs(entry - stop) / entry * 100, 2),
    }
    if target is not None and quantity:
        reward = abs(target - entry) * quantity * factor - round_trip_charges(side, quantity, entry, target, product,
                                                                              exchange, symbol)
        out["reward"] = round(reward, 2)
        out["reward_to_risk"] = round(reward / risk, 2) if risk else None
        out["target_pct"] = round(abs(target - entry) / entry * 100, 2)
        # The win rate at which this reward-to-risk breaks even: risk / (risk + reward).
        out["breakeven_win_rate_pct"] = round(risk / (risk + reward) * 100, 1) if reward > 0 else None
    if not quantity:
        out["note"] = (f"Even one {'lot' if lot > 1 else 'share'} risks more than ₹{budget:,.0f} "
                       f"({risk_pct:g}% of ₹{capital:,.0f}). Use a closer stoploss, a smaller instrument, or skip "
                       f"the trade.")
    return out
