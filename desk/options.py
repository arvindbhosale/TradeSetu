"""Index options: the live option chain from Kite, option pricing, and a simulated options backtest.

The chain comes from Kite's instrument list and quotes. Implied volatility and
delta are worked out here with Black-Scholes from each option's last price.

The backtest is simulated: Kite does not keep candles for expired contracts,
so past option premiums cannot be replayed. Instead the underlying index is
replayed from real candles and each option is priced with Black-Scholes, using
India VIX (scaled by a factor you choose) as its volatility. Real premiums
differ, most around events and on expiry day; treat the result as a rough
check of an idea, not a measured edge.

Book profit in steps (``steps``, in lots, levels in % or points of the premium paid): checked on each candle's close
like the stoploss and target, closing that many lots of the option there and holding the rest.

Add to winners (``adds``, in lots, levels in % or points of the first premium paid): checked on each candle's close,
buying that many more lots of the same option when the capital covers the premium (else skipped and counted). After
an add the stoploss of every lot moves so a fall to it loses no more than the first lots' planned loss (their whole
premium without a stoploss); see ``scalein.stop_price``. Adds stop once a profit step has been booked.
"""

from __future__ import annotations

import math
from datetime import datetime, time as clock_time

import pandas as pd

from . import market
from .backtest import BacktestParams, _curve_stats, _metrics, _points
from .charges import order_charges
from .fno import exchange_of
from .scalein import NONE as NO_ADDS, Adds, stop_price
from .scaleout import NONE as NO_STEPS, Steps

RATE = 0.065            # risk-free rate for option pricing, about the 91-day T-bill
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100, "FINNIFTY": 50, "MIDCPNIFTY": 25, "NIFTYNXT50": 100,
               "SENSEX": 100, "BANKEX": 100}
SPOT = {"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK", "FINNIFTY": "NSE:NIFTY FIN SERVICE",
        "MIDCPNIFTY": "NSE:NIFTY MID SELECT", "NIFTYNXT50": "NSE:NIFTY NEXT 50", "SENSEX": "BSE:SENSEX",
        "BANKEX": "BSE:BANKEX", "SENSEX50": "BSE:SENSEX50"}
YAHOO_SPOT = {"NIFTY": "^NSEI", "BANKNIFTY": "^NSEBANK", "FINNIFTY": "NIFTY_FIN_SERVICE.NS", "SENSEX": "^BSESN"}
LOT_DEFAULT = {"NIFTY": 65, "BANKNIFTY": 30, "FINNIFTY": 60, "MIDCPNIFTY": 120, "NIFTYNXT50": 25, "SENSEX": 20, "BANKEX": 30}   # NSE lots from Jan 2026
EXPIRY_CLOSE = clock_time(15, 30)


def _cdf(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def bs_price(spot: float, strike: float, years: float, vol: float, kind: str, rate: float = RATE) -> float:
    if years <= 0 or vol <= 0:
        return max(spot - strike, 0.0) if kind == "CE" else max(strike - spot, 0.0)
    root = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / root
    d2 = d1 - root
    if kind == "CE":
        return spot * _cdf(d1) - strike * math.exp(-rate * years) * _cdf(d2)
    return strike * math.exp(-rate * years) * _cdf(-d2) - spot * _cdf(-d1)


def bs_delta(spot, strike, years, vol, kind, rate=RATE) -> float | None:
    if years <= 0 or vol <= 0:
        return None
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / (vol * math.sqrt(years))
    return _cdf(d1) if kind == "CE" else _cdf(d1) - 1


def bs_greeks(spot, strike, years, vol, kind, rate=RATE) -> dict:
    """Delta, gamma, theta (per calendar day) and vega (per 1 volatility point) of one option.

    At or after expiry the option is its intrinsic value: delta 1/0 (or -1/0), the rest zero.
    """
    if years <= 0 or vol <= 0:
        itm = spot > strike if kind == "CE" else spot < strike
        return {"delta": (1.0 if kind == "CE" else -1.0) if itm else 0.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0}
    root = vol * math.sqrt(years)
    d1 = (math.log(spot / strike) + (rate + vol * vol / 2) * years) / root
    d2 = d1 - root
    pdf = math.exp(-d1 * d1 / 2) / math.sqrt(2 * math.pi)
    discount = math.exp(-rate * years)
    decay = -spot * pdf * vol / (2 * math.sqrt(years))
    if kind == "CE":
        delta, theta = _cdf(d1), decay - rate * strike * discount * _cdf(d2)
    else:
        delta, theta = _cdf(d1) - 1, decay + rate * strike * discount * _cdf(-d2)
    return {"delta": delta, "gamma": pdf / (spot * root), "theta": theta / 365, "vega": spot * pdf * math.sqrt(years) / 100}


def implied_vol(price: float, spot: float, strike: float, years: float, kind: str) -> float | None:
    """The volatility that gives ``price``, by bisection; None when the price is below its intrinsic value."""
    if years <= 0 or price <= 0:
        return None
    intrinsic = max(spot - strike, 0) if kind == "CE" else max(strike - spot, 0)
    if price <= intrinsic * math.exp(-RATE * years) + 1e-9:
        return None
    low, high = 0.001, 5.0
    if bs_price(spot, strike, years, high, kind) < price:
        return None
    for _ in range(80):
        mid = (low + high) / 2
        if bs_price(spot, strike, years, mid, kind) > price:
            high = mid
        else:
            low = mid
    return (low + high) / 2


def years_to(expiry: str, moment: datetime, close=EXPIRY_CLOSE) -> float:
    end = datetime.combine(datetime.strptime(expiry, "%Y-%m-%d").date(), close, tzinfo=market.IST)
    return max((end - moment.astimezone(market.IST)).total_seconds(), 0) / (365 * 86400)


# -- the chain -----------------------------------------------------------------

def build_chain(contracts: list[dict], quotes: dict, spot: float, expiry: str, moment: datetime,
                width: int = 12, first_oi: dict | None = None, forward: bool = False, close=EXPIRY_CLOSE) -> dict:
    """The chain around the money for one expiry. ``contracts`` are that underlying's options.

    ``forward``: the underlying is a futures price (MCX options are written on futures), so IV and delta come from
    Black-76, which is Black-Scholes on the future discounted to today. ``close``: when the expiry day ends."""
    rows = [c for c in contracts if c["expiry"] == expiry]
    strikes = sorted({c["strike"] for c in rows})
    if not strikes:
        raise ValueError("No contracts for that expiry.")
    atm = min(strikes, key=lambda k: abs(k - spot))
    middle = strikes.index(atm)
    shown = strikes[max(middle - width, 0): middle + width + 1]
    years = years_to(expiry, moment, close)
    model = spot * math.exp(-RATE * years) if forward else spot
    by_key = {(c["strike"], c["type"]): c for c in rows}
    table, total = [], {"CE": 0, "PE": 0}
    for strike in shown:
        line = {"strike": strike, "atm": strike == atm}
        for kind in ("CE", "PE"):
            contract = by_key.get((strike, kind))
            if not contract:
                line[kind] = None
                continue
            name = f"{contract['exchange']}:{contract['tradingsymbol']}"
            quote = quotes.get(name) or {}
            last = quote.get("last_price")
            previous = (quote.get("ohlc") or {}).get("close")
            oi = quote.get("oi")
            iv = implied_vol(last, model, strike, years, kind) if last else None
            start_oi = (first_oi or {}).get(name)
            line[kind] = {
                "instrument": name, "tradingsymbol": contract["tradingsymbol"], "lot_size": contract["lot_size"],
                "tick_size": contract.get("tick_size"),
                "ltp": last, "change_pct": round((last / previous - 1) * 100, 2) if last and previous else None,
                "oi": oi, "oi_change": (oi - start_oi) if oi is not None and start_oi is not None else None,
                "volume": quote.get("volume"), "iv": round(iv * 100, 2) if iv else None,
                # Delta to the underlying: d(model)/d(future) is the discount factor under Black-76.
                "delta": round(bs_delta(model, strike, years, iv, kind) * (model / spot), 3) if iv else None,
                "bid": ((quote.get("depth") or {}).get("buy") or [{}])[0].get("price"),
                "ask": ((quote.get("depth") or {}).get("sell") or [{}])[0].get("price"),
                "itm": (strike < spot) if kind == "CE" else (strike > spot),
            }
            total[kind] += oi or 0
        table.append(line)
    # Max pain: the expiry price at which option buyers, in total, would collect the least.
    oi_at = {(c["strike"], c["type"]): (quotes.get(f"{c['exchange']}:{c['tradingsymbol']}") or {}).get("oi") or 0 for c in rows}
    pain = {k: sum(oi_at.get((s, "CE"), 0) * max(k - s, 0) + oi_at.get((s, "PE"), 0) * max(s - k, 0) for s in strikes)
            for k in strikes}
    atm_line = next(line for line in table if line["atm"])
    straddle = None
    if atm_line.get("CE") and atm_line.get("PE") and atm_line["CE"]["ltp"] and atm_line["PE"]["ltp"]:
        straddle = round(atm_line["CE"]["ltp"] + atm_line["PE"]["ltp"], 2)
    ivs = [x["iv"] for x in (atm_line.get("CE"), atm_line.get("PE")) if x and x.get("iv")]
    return {"expiry": expiry, "spot": round(spot, 2), "atm": atm, "rows": table,
            "days_left": round(years * 365, 2), "pcr": round(total["PE"] / total["CE"], 2) if total["CE"] else None,
            "max_pain": min(pain, key=pain.get) if any(oi_at.values()) else None, "straddle": straddle,
            "atm_iv": round(sum(ivs) / len(ivs), 2) if ivs else None,
            "expected_move": round(straddle * 0.85, 2) if straddle else None,
            "total_oi": total}


# -- simulated backtest ----------------------------------------------------------

def next_expiry(moment: pd.Timestamp, weekday: int, min_days: float) -> pd.Timestamp:
    """The first expiry on ``weekday`` (Mon=0) at least ``min_days`` away, at 15:30."""
    day = moment.normalize()
    for _ in range(30):
        if day.weekday() == weekday:
            stamp = day + pd.Timedelta(hours=15, minutes=30)
            if (stamp - moment).total_seconds() / 86400 >= min_days:
                return stamp
        day += pd.Timedelta(days=1)
    raise ValueError("no expiry found")


def option_charges(side: str, quantity: int, premium: float, exchange: str) -> float:
    return order_charges(side, quantity, max(premium, 0.05), "NRML", exchange).total


def run_options(underlying: str, bars: pd.DataFrame, positions: pd.Series, vix: pd.Series, *, capital: float,
                lots: int, lot_size: int, side: str = "calls", moneyness: int = 0, min_days: float = 2,
                expiry_weekday: int = 1, stop_pct: float = 0, target_pct: float = 0, iv_scale: float = 1.0,
                slippage_pct: float = 1.0, intraday: bool = False, square_off: str = "15:00", strategy_name: str = "",
                steps: Steps = NO_STEPS, adds: Adds = NO_ADDS) -> dict:
    """Buy options on the underlying strategy's signals, priced with Black-Scholes. See the module note."""
    if side not in ("calls", "puts", "both"):
        raise ValueError("side must be calls, puts or both")
    if lots < 1 or lot_size < 1:
        raise ValueError("lots and lot size must be at least 1")
    if not 0 <= stop_pct <= 100 or not 0 <= target_pct <= 1000:
        raise ValueError("stoploss must be 0-100% and target 0-1000% of the premium")
    if len(bars) < 30:
        raise ValueError("not enough candles")
    if steps.active and sum(steps.lots()) > lots:
        raise ValueError(f"the profit steps close {sum(steps.lots())} lots but the position has {lots}")
    if adds.active and lots + adds.total() > 50:
        raise ValueError(f"{lots} lots plus {adds.total():g} added is more than 50 lots")
    exchange = exchange_of(underlying)
    step = STRIKE_STEP.get(underlying, 50)
    vol_by_day = (vix / 100).reindex(bars.index.normalize().unique()).ffill().bfill()
    if vol_by_day.isna().all():
        raise ValueError("No India VIX history for this period; it is needed as the volatility.")
    if float(vol_by_day.max()) > 1.5:
        raise ValueError("The India VIX series does not look like VIX (values above 150). Check the data feed.")
    stamps, opens, closes = bars.index, bars["Open"].to_numpy(float), bars["Close"].to_numpy(float)
    want = positions.reindex(stamps).fillna(0).astype(int).to_numpy()
    quantity = lots * lot_size
    slip = slippage_pct / 100
    cutoff = pd.Timestamp(f"2000-01-01 {square_off}").time()
    cash, held, trades, total_charges, in_market = capital, None, [], 0.0, 0
    skipped = 0
    equity = []
    plan = steps.lots() if steps.active else []
    steps_booked = 0
    adds_made = adds_skipped = 0

    def vol_at(stamp):
        return float(vol_by_day.get(stamp.normalize(), vol_by_day.iloc[-1])) * iv_scale

    def price(option, stamp, spot):
        years = max((option["expiry"] - stamp).total_seconds(), 0) / (365 * 86400)
        return bs_price(spot, option["strike"], years, vol_at(stamp), option["kind"])

    def settle(premium, n_lots) -> tuple[float, float]:
        """Sell ``n_lots`` of the held option at ``premium``: (fill, profit after charges)."""
        nonlocal cash, total_charges
        fill = max(premium * (1 - slip), 0.05)
        qty = n_lots * lot_size
        fees = option_charges("SELL", qty, fill, exchange)
        part = held["open_cost"] * n_lots / held["lots"]
        cash += qty * fill - fees
        total_charges += fees
        held["open_cost"] -= part
        held["lots"] -= n_lots
        return fill, qty * fill - fees - part

    def close(i, premium, reason):
        nonlocal held
        fill, pnl = settle(premium, held["lots"])
        pnl += held["booked"]
        trade = {"entry_date": held["opened"].strftime("%Y-%m-%d %H:%M"), "exit_date": stamps[i].strftime("%Y-%m-%d %H:%M"),
                 "contract": held["label"], "quantity": held["bought"] * lot_size, "lots": held["bought"],
                 "entry_price": round(held["premium"], 2),
                 "exit_price": round(fill, 2), "pnl": round(pnl, 2), "return_pct": round(pnl / held["cost"] * 100, 2),
                 "days": (stamps[i] - held["opened"]).days, "exit": reason, "open": False}
        if held["partials"]:
            trade["partials"] = held["partials"]
        if held["adds"]:
            trade["adds"], trade["average_price"] = held["adds"], round(held["avg"], 2)
        trades.append(trade)
        held = None

    def take_steps(i, premium) -> None:
        """Profit steps on this candle's closing premium; the stoploss moved by them."""
        nonlocal steps_booked
        while held is not None and held["done"] < len(plan):
            level = held["premium"] + steps.distance(held["premium"], steps.levels[held["done"]])
            if premium < level:
                return
            n = plan[held["done"]]
            held["done"] += 1
            if n >= held["lots"]:
                close(i, premium, f"profit step {held['done']}")
                return
            fill, pnl = settle(premium, n)
            held["booked"] += pnl
            steps_booked += 1
            held["partials"].append({"date": stamps[i].strftime("%Y-%m-%d %H:%M"), "lots": n, "quantity": n * lot_size,
                                     "price": round(fill, 2), "pnl": round(pnl, 2), "why": f"profit step {held['done']}"})

    def add_more(i, premium) -> None:
        """Add ``adds_done``: buy that many more lots of the held option at ``premium`` when the cash covers it."""
        nonlocal cash, total_charges, adds_made, adds_skipped
        n = adds.lots()[held["adds_done"]]
        held["adds_done"] += 1
        fill = premium * (1 + slip)
        qty = n * lot_size
        fees = option_charges("BUY", qty, fill, exchange)
        stamp_text = stamps[i].strftime("%Y-%m-%d %H:%M")
        if qty * fill + fees > cash:
            adds_skipped += 1
            held["adds"].append({"date": stamp_text, "lots": 0, "quantity": 0, "price": round(fill, 2),
                                 "why": f"add {held['adds_done']} skipped: cash short"})
            return
        cash -= qty * fill + fees
        total_charges += fees
        held["avg"] = (held["avg"] * held["lots"] + fill * n) / (held["lots"] + n)
        held["lots"] += n
        held["bought"] += n
        held["open_cost"] += qty * fill + fees
        held["cost"] += qty * fill + fees
        adds_made += 1
        moved = stop_price(1, held["avg"], held["lots"] * lot_size, held["risk"], adds.stop_after)
        held["add_stop"] = moved if held["add_stop"] is None else max(held["add_stop"], moved)
        held["adds"].append({"date": stamp_text, "lots": n, "quantity": qty, "price": round(fill, 2),
                             "why": f"add {held['adds_done']}", "stop": round(held["add_stop"], 2)})

    for i in range(len(stamps)):
        stamp, spot_open = stamps[i], opens[i]
        signal = want[i - 1] if i else 0
        # Expiry passed while held: settled at intrinsic value against the open.
        if held is not None and stamp >= held["expiry"]:
            intrinsic = max(spot_open - held["strike"], 0) if held["kind"] == "CE" else max(held["strike"] - spot_open, 0)
            close(i, intrinsic, "expiry")
        if held is not None and intraday and (i == 0 or stamp.normalize() != stamps[i - 1].normalize() or stamp.time() >= cutoff):
            close(i, price(held, stamp, spot_open), "square-off")
        wanted_kind = None
        if signal == 1 and side in ("calls", "both"):
            wanted_kind = "CE"
        elif signal == 0 and side in ("puts", "both") and i > 1 and want[:i].any():
            wanted_kind = "PE"
        if held is not None and held["kind"] != wanted_kind:
            close(i, price(held, stamp, spot_open), "signal")
        if held is None and wanted_kind and not (intraday and stamp.time() >= cutoff):
            expiry = next_expiry(stamp, expiry_weekday, min_days)
            atm = round(spot_open / step) * step
            shift = step * moneyness
            strike = atm - shift if wanted_kind == "CE" else atm + shift          # positive moneyness = in the money
            option = {"strike": strike, "kind": wanted_kind, "expiry": expiry}
            premium = price(option, stamp, spot_open) * (1 + slip)
            if premium >= 0.05:
                fees = option_charges("BUY", quantity, premium, exchange)
                cost = quantity * premium + fees
                if cost > cash:
                    skipped += 1
                else:
                    cash -= cost
                    total_charges += fees
                    held = {**option, "premium": premium, "cost": cost, "opened": stamp,
                            "label": f"{underlying} {expiry:%d %b} {strike:g} {wanted_kind}",
                            "lots": lots, "open_cost": cost, "done": 0, "booked": 0.0, "partials": [],
                            "avg": premium, "bought": lots, "adds_done": 0, "add_stop": None, "adds": [],
                            # the loss the first lots planned: to the stoploss, or the whole premium without one
                            "risk": quantity * premium * (stop_pct / 100 if stop_pct else 1)}
        if held is not None:
            now_premium = price(held, stamp + pd.Timedelta(minutes=1), closes[i])
            moved = steps.lock(held["done"]) if steps.active else None
            locked = (held["avg"] if not moved else held["premium"] + steps.distance(held["premium"], moved)) \
                if moved is not None else None
            if stop_pct and now_premium <= held["premium"] * (1 - stop_pct / 100):
                close(i, now_premium, "stoploss")
            elif locked is not None and now_premium <= locked:
                close(i, now_premium, steps.lock_name(held["done"]))
            elif held["add_stop"] is not None and now_premium <= held["add_stop"]:
                close(i, now_premium, "stoploss moved after adding")
            else:
                take_steps(i, now_premium)
                while (held is not None and adds.active and not held["done"] and held["adds_done"] < len(adds.levels)
                       and now_premium >= held["premium"] + adds.distance(held["premium"], adds.levels[held["adds_done"]])
                       and not (intraday and stamp.time() >= cutoff)
                       and not (target_pct and now_premium >= held["premium"] * (1 + target_pct / 100))):
                    add_more(i, now_premium)
                if held is not None and target_pct and now_premium >= held["premium"] * (1 + target_pct / 100):
                    close(i, now_premium, "target")
        value = cash + (held["lots"] * lot_size * price(held, stamp + pd.Timedelta(minutes=1), closes[i]) if held is not None else 0)
        in_market += held is not None
        equity.append(value)
    if held is not None:
        last = price(held, stamps[-1] + pd.Timedelta(minutes=1), closes[-1])
        pnl = held["lots"] * lot_size * last - held["open_cost"] + held["booked"]
        trades.append({"entry_date": held["opened"].strftime("%Y-%m-%d %H:%M"), "exit_date": None, "contract": held["label"],
                       "quantity": quantity, "lots": lots, "entry_price": round(held["premium"], 2), "exit_price": round(last, 2),
                       "pnl": round(pnl, 2), "return_pct": round(pnl / held["cost"] * 100, 2),
                       "days": (stamps[-1] - held["opened"]).days, "exit": "open", "open": True,
                       **({"partials": held["partials"]} if held["partials"] else {}),
                       **({"adds": held["adds"], "average_price": round(held["avg"], 2)} if held["adds"] else {})})
        trades[-1].update(quantity=held["bought"] * lot_size, lots=held["bought"])
    curve = pd.Series(equity, index=stamps)
    hold = capital * bars["Close"] / bars["Close"].iloc[0]
    if intraday:
        days = stamps.normalize()
        curve, hold = curve.groupby(days).last(), hold.groupby(days).last()
        in_market = int(round(in_market / len(stamps) * len(curve)))
    params = BacktestParams(capital=capital)
    if moneyness == 0:
        where = "ATM"
    else:
        plural = "s" if abs(moneyness) > 1 else ""
        where = f"{abs(moneyness)} strike{plural} {'ITM' if moneyness > 0 else 'OTM'}"
    return {"strategy": {"name": f"{strategy_name} → {side} ({where})"},
            "params": params.__dict__, "simulated": True, "lot_size": lot_size,
            "period": {"start": curve.index[0].strftime("%Y-%m-%d"), "end": curve.index[-1].strftime("%Y-%m-%d"), "bars": len(bars), "days": len(curve)},
            "metrics": {**_metrics(curve, trades, params, total_charges, in_market), "steps_booked": steps_booked,
                        "adds_made": adds_made, "adds_skipped": adds_skipped},
            "buy_hold": _curve_stats(hold, params),
            "trades": trades, "equity": _points(curve), "buy_hold_equity": _points(hold), "benchmark_equity": None, "benchmark": None,
            "symbol": underlying, "skipped_entries": skipped}
