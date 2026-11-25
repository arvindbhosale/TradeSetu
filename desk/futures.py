"""Index futures backtest: long, short or both, in lots, with margin, rollover and futures charges.

Prices. Day candles are Kite's *continuous* futures series: each day is the
front-month contract, so the series jumps at every monthly roll by the gap
between the expiring and the next contract (the basis, about 0.5-0.7% for a
month at India's cost of carry). Holding through that jump is not profit or
loss, so on each roll the entry price is shifted by an estimate of the basis
(``carry_pct`` a year, over the days to the next expiry) and a round trip of
charges is paid, as if the position were rolled at the close. Without Kite the
index itself stands in (basis and rolls are then ignored, which the result
says).

Execution. Signals are read at a candle's close and filled at the next
candle's open, with slippage. A stoploss or target (in % of the futures
price) is checked against each candle's high and low; when both are inside
one candle the stop is assumed to have come first. Intraday runs enter only
inside the trade window and close everything at the square-off time. A positional run on intraday candles
(``positional``) reads each candle the same way but holds overnight (NRML) until the signal turns, a stoploss or a
target: no trade window, no square-off. Its candles are one contract's or the index's, so rolls are not modelled.

A maximum loss and target in rupees (``stop_rs`` / ``target_rs``), as an F&O algo has them, are price levels for the
lots held (with the charges paid so far), checked the same way; the stoploss the price reaches first is taken.

Margin. An entry needs ``margin_pct`` of the contract value (SPAN plus
exposure is roughly 11-14% for index futures) out of current equity; an
entry that cannot be afforded is skipped and counted.

Book profit in steps (``steps``, ``scaleout.Steps`` in lots): at each profit level, in % or points of the futures
price from the entry, that many lots are closed (at the level, or at the open when the candle opens past it), the
rest later. Levels reached in one candle are taken in price order, a target beyond them after them; a stoploss in
the same candle comes first. A stoploss moved by a step applies from the next candle.

Add to winners (``adds``, ``scalein.Adds`` in lots): at each profit level from the first entry, that many lots are
added (at the level, or at the open when the candle opens past it) when equity covers the margin of the larger
position; otherwise the add is skipped and counted. Profit is worked out on the average entry; levels, stoploss and
target stay measured from the first entry, and after an add the stoploss of every lot moves (``scalein.stop_price``)
from the next candle. Adds stop once a profit step has been booked.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .backtest import BacktestParams, _curve_stats, _metrics, _points
from .charges import futures_charges
from .scalein import NONE as NO_ADDS, Adds, stop_price
from .scaleout import NONE as NO_STEPS, Steps

DIRECTIONS = {"long": "Long only (buy on the signal, exit when it ends)",
              "short": "Short only (sell when the signal is off, cover when it turns on)",
              "both": "Long and short (always in: long on the signal, short otherwise)"}
TUESDAY_FROM = date(2025, 9, 1)        # NSE monthly F&O expiry moved from the last Thursday to the last Tuesday


def monthly_expiries(start, end, holidays=frozenset(), exchange: str = "NFO") -> list[date]:
    """Monthly index-futures expiry days between two dates: last Thursday (last Tuesday from Sep 2025 on NSE),
    moved back to the previous trading day when it is a holiday."""
    out, year, month = [], start.year, start.month
    while (year, month) <= (end.year, end.month):
        last = (date(year + (month == 12), month % 12 + 1, 1) - timedelta(days=1))
        weekday = 3 if exchange == "BFO" or last < TUESDAY_FROM else 1     # BSE monthly: last Thursday
        day = last - timedelta(days=(last.weekday() - weekday) % 7)
        while day.weekday() >= 5 or day.isoformat() in holidays:
            day -= timedelta(days=1)
        out.append(day)
        year, month = year + (month == 12), month % 12 + 1
    return [d for d in out if start <= d <= end + timedelta(days=40)]


def run_futures(bars: pd.DataFrame, signal: pd.Series, *, underlying: str, lot_size: int, lots: int = 1,
                capital: float = 500_000.0, direction: str = "long", margin_pct: float = 12.0,
                stop_pct: float = 0.0, target_pct: float = 0.0, slippage_pct: float = 0.02,
                intraday: bool = False, trade_start: str = "09:30", trade_end: str = "15:00", square_off: str = "15:15",
                continuous: bool = True, carry_pct: float = 7.0, holidays=frozenset(), exchange: str = "NFO",
                strategy_name: str = "", risk_free_rate_pct: float = 6.5, steps: Steps = NO_STEPS,
                adds: Adds = NO_ADDS, stop_rs: float = 0.0, target_rs: float = 0.0, positional: bool = False) -> dict:
    """``stop_rs`` / ``target_rs``: close the position when its profit or loss in rupees (with the charges paid so
    far, roll costs included, and any profit step booked) reaches this, as an F&O algo's maximum loss and target do.
    It is a price level for the lots held, checked inside the candle like the % stoploss and target."""
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
    if lots < 1 or lot_size < 1:
        raise ValueError("lots and lot size must be at least 1")
    if not 2 <= margin_pct <= 100:
        raise ValueError("margin must be 2-100% of the contract value")
    if not 0 <= stop_pct <= 50 or not 0 <= target_pct <= 100:
        raise ValueError("stoploss must be 0-50% and target 0-100% of the price")
    for label, value in (("Maximum loss (₹)", stop_rs), ("Target (₹)", target_rs)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1e8:
            raise ValueError(f"{label} must be between 0 and 10 crore")
    if not 0 <= slippage_pct <= 2 or not 0 <= carry_pct <= 20:
        raise ValueError("slippage must be 0-2% and carry 0-20% a year")
    if len(bars) < 30:
        raise ValueError("not enough candles")
    if steps.active and sum(steps.lots()) > lots:
        raise ValueError(f"the profit steps close {sum(steps.lots())} lots but the position has {lots}")
    if adds.active and lots + adds.total() > 50:
        raise ValueError(f"{lots} lots plus {adds.total():g} added is more than 50 lots")
    params = BacktestParams(capital=capital, risk_free_rate_pct=risk_free_rate_pct, intraday=intraday,
                            positional=intraday and positional, trade_start=trade_start, trade_end=trade_end, square_off=square_off)
    params.validate()
    stamps = bars.index
    opens, highs, lows, closes = (bars[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    raw = signal.reindex(stamps).fillna(0).astype(int).to_numpy()
    # Nothing is shorted before the strategy has said anything (its indicators are still warming up).
    started = np.maximum.accumulate(raw > 0)
    if direction == "long":
        want = np.where(raw > 0, 1, 0)
    elif direction == "short":
        want = np.where((raw == 0) & started, -1, 0)
    else:
        want = np.where(raw > 0, 1, np.where(started, -1, 0))
    qty_per = lots * lot_size
    slip = slippage_pct / 100
    t_start, t_end, t_close = (pd.Timestamp(f"2000-01-01 {t}").time() for t in (trade_start, trade_end, square_off))
    session = intraday and not positional            # an intraday trade: trade window and square-off

    roll_days: set = set()
    if continuous and not intraday:
        expiries = monthly_expiries(stamps[0].date(), stamps[-1].date(), holidays, exchange)
        days = pd.DatetimeIndex(stamps.normalize())
        for expiry, following in zip(expiries, expiries[1:] + [None], strict=True):
            later = days[days > pd.Timestamp(expiry)]
            if len(later):
                roll_days.add((later[0], (following - expiry).days if following else 30))
    roll_at = dict(roll_days)

    cash, pos, entry, entry_i, entry_cost = capital, 0, 0.0, 0, 0.0
    rolls_in_trade, trades, total_charges, in_market, skipped, rolls = 0, [], 0.0, 0, 0, 0
    equity = np.empty(len(bars))
    held = lots                              # lots open; fewer than ``lots`` after a profit step
    plan = steps.lots() if steps.active else []
    done, booked, partials, steps_booked = 0, 0.0, [], 0
    first, opened = 0.0, lots                # the first entry's price (levels are measured from it); lots bought
    adds_done, add_stop, risk0, added, adds_made, adds_skipped = 0, None, 0.0, [], 0, 0

    def fee(side, price, n=None):
        return futures_charges(side, (held if n is None else n) * lot_size, price, exchange).total

    def settle(i, price, n) -> float:
        """Close ``n`` of the open lots at ``price``; their profit after charges."""
        nonlocal cash, total_charges, entry_cost, held
        charge = fee("SELL" if pos > 0 else "BUY", price, n)
        gross = pos * n * lot_size * (price - entry)
        part = entry_cost * n / held
        cash += gross - charge
        total_charges += charge
        entry_cost -= part
        held -= n
        return gross - charge - part

    def close_trade(i, price, reason):
        nonlocal pos
        average = entry
        pnl = settle(i, price, held) + booked
        margin = margin_pct / 100 * opened * lot_size * average
        trade = {"side": "LONG" if pos > 0 else "SHORT", "lots": opened, "quantity": opened * lot_size,
                 "entry_date": stamps[entry_i].strftime(fmt), "exit_date": stamps[i].strftime(fmt),
                 "entry_price": round(first, 2), "exit_price": round(price, 2), "pnl": round(pnl, 2),
                 "return_pct": round(pnl / margin * 100, 2) if margin else None,
                 "days": int((stamps[i] - stamps[entry_i]).days), "rolls": rolls_in_trade, "exit": reason, "open": False}
        if partials:
            trade["partials"] = list(partials)
        if added:
            trade["adds"], trade["average_price"] = list(added), round(average, 2)
        trades.append(trade)
        pos = 0

    def book_step(i, price, n):
        nonlocal booked, steps_booked
        pnl = settle(i, price, n)
        booked += pnl
        steps_booked += 1
        partials.append({"date": stamps[i].strftime(fmt), "lots": n, "quantity": n * lot_size, "price": round(price, 2),
                         "pnl": round(pnl, 2), "why": f"profit step {done}"})

    def add_more(i, price) -> None:
        """Add ``adds_done``: that many lots at ``price`` when equity covers the margin of the whole position."""
        nonlocal cash, total_charges, entry_cost, entry, held, opened, adds_done, add_stop, adds_made, adds_skipped
        n = adds.lots()[adds_done]
        adds_done += 1
        equity_now = cash + pos * held * lot_size * (price - entry)
        if margin_pct / 100 * (held + n) * lot_size * price > equity_now:
            adds_skipped += 1
            added.append({"date": stamps[i].strftime(fmt), "lots": 0, "quantity": 0, "price": round(price, 2),
                          "why": f"add {adds_done} skipped: margin short"})
            return
        charge = fee("BUY" if pos > 0 else "SELL", price, n)
        cash -= charge
        total_charges += charge
        entry_cost += charge
        entry = (entry * held + price * n) / (held + n)
        held += n
        opened += n
        adds_made += 1
        moved = stop_price(pos, entry, held * lot_size, risk0, adds.stop_after)
        add_stop = moved if add_stop is None else (max(add_stop, moved) if pos > 0 else min(add_stop, moved))
        added.append({"date": stamps[i].strftime(fmt), "lots": n, "quantity": n * lot_size, "price": round(price, 2),
                      "why": f"add {adds_done}", "stop": round(add_stop, 2)})

    def take_steps(i, high, low, goal) -> None:
        """The profit steps and adds this candle reached, in price order, short of the target."""
        nonlocal done
        window = not session or t_start <= stamps[i].time() < t_end
        while pos:
            step_at = first + pos * steps.distance(first, steps.levels[done]) if done < len(plan) else None
            add_at = (first + pos * adds.distance(first, adds.levels[adds_done])
                      if adds.active and not done and adds_done < len(adds.levels) and window else None)
            if step_at is None and add_at is None:
                return
            adding = step_at is None or (add_at is not None and (add_at - step_at) * pos < 0)
            level = add_at if adding else step_at
            if (high < level if pos > 0 else low > level) or (goal is not None and (level - goal) * pos >= 0):
                return
            if adding:
                add_more(i, (max(opens[i], level) * (1 + slip)) if pos > 0 else (min(opens[i], level) * (1 - slip)))
                continue
            fill = (max(opens[i], level) * (1 - slip)) if pos > 0 else (min(opens[i], level) * (1 + slip))
            n, done = plan[done], done + 1
            if n >= held:
                close_trade(i, fill, f"profit step {done}")
            else:
                book_step(i, fill, n)

    def rupee_level(pnl) -> float:
        """The futures price at which the position's profit, with the charges paid so far and steps booked, is
        ``pnl``."""
        return entry + pos * (pnl + entry_cost - booked) / (held * lot_size)

    def base_stop():
        """The first entry's stoploss: the % one or the rupee one, whichever the price reaches first."""
        found = [(level, why) for level, why in ((first * (1 - pos * stop_pct / 100) if stop_pct else None, "stoploss"),
                                                 (rupee_level(-stop_rs) if stop_rs else None, "maximum loss in ₹"))
                 if level is not None]
        if not found:
            return None, "stoploss"
        return max(found) if pos > 0 else min(found)

    def base_goal():
        found = [(level, why) for level, why in ((first * (1 + pos * target_pct / 100) if target_pct else None, "target"),
                                                 (rupee_level(target_rs) if target_rs else None, "target in ₹"))
                 if level is not None]
        if not found:
            return None, "target"
        return min(found) if pos > 0 else max(found)

    def locked_stop(stop, kind="stoploss"):
        """The stoploss, moved by the profit steps taken and by the adds made."""
        moved = steps.lock(done) if steps.active else None
        if moved is not None:
            level = entry if not moved else first + pos * steps.distance(first, moved)
            if stop is None or (level - stop) * pos > 0:
                stop, kind = level, steps.lock_name(done)
        if add_stop is not None and (stop is None or (add_stop - stop) * pos > 0):
            stop, kind = add_stop, "stoploss moved after adding"
        return stop, kind

    fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
    for i in range(len(bars)):
        stamp = stamps[i]
        # 1. the roll: continuous prices jump by the basis on the first day after an expiry
        if pos and stamp.normalize() in roll_at and i > 0:
            basis = closes[i - 1] * carry_pct / 100 * roll_at[stamp.normalize()] / 365
            charge = fee("SELL", closes[i - 1]) + fee("BUY", closes[i - 1] + basis)
            cash -= charge
            total_charges += charge
            entry_cost += charge
            entry += basis
            first += basis
            if add_stop is not None:
                add_stop += basis
            rolls += 1
            rolls_in_trade += 1
        # 2. yesterday's decision, filled at today's open
        target = int(want[i - 1]) if i > 0 else 0
        moment = stamp.time()
        if session:
            if moment >= t_close:
                target = 0
            elif target and target != pos and not (t_start <= moment < t_end):
                target = 0                                 # outside the window: exits only, no new entries
        if target != pos:
            if pos:
                price = opens[i] * (1 - slip if pos > 0 else 1 + slip)
                close_trade(i, price, "square-off" if session and moment >= t_close else "signal")
            if target:
                price = opens[i] * (1 + slip if target > 0 else 1 - slip)
                equity_now = cash
                if margin_pct / 100 * qty_per * price > equity_now:
                    skipped += 1
                else:
                    charge = fee("BUY" if target > 0 else "SELL", price, lots)
                    cash -= charge
                    total_charges += charge
                    pos, entry, entry_i, entry_cost, rolls_in_trade = target, price, i, charge, 0
                    held, done, booked, partials = lots, 0, 0.0, []
                    first, opened, adds_done, add_stop, added = price, lots, 0, None, []
                    risk0 = max(qty_per * price * stop_pct / 100, stop_rs)
        # 3. stoploss, profit steps and target inside the candle
        if pos and (stop_pct or target_pct or stop_rs or target_rs or steps.active or adds.active):
            stop, kind = locked_stop(*base_stop())
            goal, reached = base_goal()
            if pos > 0:
                if stop is not None and lows[i] <= stop:
                    close_trade(i, min(opens[i], stop) * (1 - slip), kind)
                else:
                    take_steps(i, highs[i], lows[i], goal)
                    if pos:
                        goal, reached = base_goal()          # a step booked changes the rupee target's price
                    if pos and goal is not None and highs[i] >= goal:
                        close_trade(i, max(opens[i], goal) * (1 - slip), reached)
            else:
                if stop is not None and highs[i] >= stop:
                    close_trade(i, max(opens[i], stop) * (1 + slip), kind)
                else:
                    take_steps(i, highs[i], lows[i], goal)
                    if pos:
                        goal, reached = base_goal()
                    if pos and goal is not None and lows[i] <= goal:
                        close_trade(i, min(opens[i], goal) * (1 + slip), reached)
        # 4. intraday: nothing is held past the square-off candle
        if session and pos and (moment >= t_close or i + 1 == len(bars) or stamps[i + 1].normalize() != stamp.normalize()):
            close_trade(i, closes[i] * (1 - slip if pos > 0 else 1 + slip), "square-off")
        if pos:
            in_market += 1
        equity[i] = cash + pos * held * lot_size * (closes[i] - entry)

    if pos:
        gross = pos * held * lot_size * (closes[-1] - entry)
        pnl = gross - entry_cost + booked
        trade = {"side": "LONG" if pos > 0 else "SHORT", "lots": opened, "quantity": opened * lot_size,
                 "entry_date": stamps[entry_i].strftime(fmt), "exit_date": None, "entry_price": round(first, 2),
                 "exit_price": round(closes[-1], 2), "pnl": round(pnl, 2),
                 "return_pct": round(pnl / (margin_pct / 100 * opened * lot_size * entry) * 100, 2),
                 "days": int((stamps[-1] - stamps[entry_i]).days), "rolls": rolls_in_trade, "exit": "open", "open": True}
        if partials:
            trade["partials"] = list(partials)
        if added:
            trade["adds"], trade["average_price"] = list(added), round(entry, 2)
        trades.append(trade)

    curve = pd.Series(equity, index=stamps)
    hold = capital * bars["Close"] / bars["Close"].iloc[0]
    if intraday:
        day = stamps.normalize()
        curve, hold = curve.groupby(day).last(), hold.groupby(day).last()
        in_market = int(round(in_market / len(bars) * len(curve)))
    metrics = _metrics(curve, trades, params, total_charges, in_market)
    metrics.update(rolls=rolls, skipped_entries=skipped, long_trades=sum(t["side"] == "LONG" for t in trades),
                   short_trades=sum(t["side"] == "SHORT" for t in trades), steps_booked=steps_booked,
                   adds_made=adds_made, adds_skipped=adds_skipped,
                   margin_per_lot=round(margin_pct / 100 * lot_size * float(closes[-1]), 0))
    label = {"long": "long", "short": "short", "both": "long & short"}[direction]
    return {"strategy": {"name": f"{strategy_name} → {underlying} futures, {label}"},
            "params": {**params.__dict__, "direction": direction, "lots": lots, "lot_size": lot_size, "margin_pct": margin_pct,
                       "stop_pct": stop_pct, "target_pct": target_pct, "stop_rs": stop_rs, "target_rs": target_rs,
                       "slippage_pct": slippage_pct, "carry_pct": carry_pct,
                       "steps": steps.as_dict() if steps.active else None,
                       "adds": adds.as_dict() if adds.active else None},
            "period": {"start": curve.index[0].strftime("%Y-%m-%d"), "end": curve.index[-1].strftime("%Y-%m-%d"),
                       "bars": len(bars), "days": len(curve)},
            "metrics": metrics, "buy_hold": _curve_stats(hold, params), "trades": trades,
            "equity": _points(curve), "buy_hold_equity": _points(hold), "benchmark_equity": None, "benchmark": None,
            "symbol": underlying, "skipped_entries": skipped, "futures": True}
