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
inside the trade window and close everything at the square-off time.

Margin. An entry needs ``margin_pct`` of the contract value (SPAN plus
exposure is roughly 11-14% for index futures) out of current equity; an
entry that cannot be afforded is skipped and counted.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd

from .backtest import BacktestParams, _curve_stats, _metrics, _points
from .charges import futures_charges

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
                strategy_name: str = "", risk_free_rate_pct: float = 6.5) -> dict:
    if direction not in DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(DIRECTIONS)}")
    if lots < 1 or lot_size < 1:
        raise ValueError("lots and lot size must be at least 1")
    if not 2 <= margin_pct <= 100:
        raise ValueError("margin must be 2-100% of the contract value")
    if not 0 <= stop_pct <= 50 or not 0 <= target_pct <= 100:
        raise ValueError("stoploss must be 0-50% and target 0-100% of the price")
    if not 0 <= slippage_pct <= 2 or not 0 <= carry_pct <= 20:
        raise ValueError("slippage must be 0-2% and carry 0-20% a year")
    if len(bars) < 30:
        raise ValueError("not enough candles")
    params = BacktestParams(capital=capital, risk_free_rate_pct=risk_free_rate_pct, intraday=intraday,
                            trade_start=trade_start, trade_end=trade_end, square_off=square_off)
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

    def fee(side, price):
        return futures_charges(side, qty_per, price, exchange).total

    def close_trade(i, price, reason):
        nonlocal cash, pos, total_charges
        side = "SELL" if pos > 0 else "BUY"
        charge = fee(side, price)
        gross = pos * qty_per * (price - entry)
        cash += gross - charge
        total_charges += charge
        pnl = gross - charge - entry_cost
        margin = margin_pct / 100 * qty_per * entry
        trades.append({"side": "LONG" if pos > 0 else "SHORT", "lots": lots, "quantity": qty_per,
                       "entry_date": stamps[entry_i].strftime(fmt), "exit_date": stamps[i].strftime(fmt),
                       "entry_price": round(entry, 2), "exit_price": round(price, 2), "pnl": round(pnl, 2),
                       "return_pct": round(pnl / margin * 100, 2) if margin else None,
                       "days": int((stamps[i] - stamps[entry_i]).days), "rolls": rolls_in_trade, "exit": reason, "open": False})
        pos = 0

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
            rolls += 1
            rolls_in_trade += 1
        # 2. yesterday's decision, filled at today's open
        target = int(want[i - 1]) if i > 0 else 0
        moment = stamp.time()
        if intraday:
            if moment >= t_close:
                target = 0
            elif target and target != pos and not (t_start <= moment < t_end):
                target = 0                                 # outside the window: exits only, no new entries
        if target != pos:
            if pos:
                price = opens[i] * (1 - slip if pos > 0 else 1 + slip)
                close_trade(i, price, "square-off" if intraday and moment >= t_close else "signal")
            if target:
                price = opens[i] * (1 + slip if target > 0 else 1 - slip)
                equity_now = cash
                if margin_pct / 100 * qty_per * price > equity_now:
                    skipped += 1
                else:
                    charge = fee("BUY" if target > 0 else "SELL", price)
                    cash -= charge
                    total_charges += charge
                    pos, entry, entry_i, entry_cost, rolls_in_trade = target, price, i, charge, 0
        # 3. stoploss and target inside the candle
        if pos and (stop_pct or target_pct):
            if pos > 0:
                stop = entry * (1 - stop_pct / 100) if stop_pct else None
                goal = entry * (1 + target_pct / 100) if target_pct else None
                if stop is not None and lows[i] <= stop:
                    close_trade(i, min(opens[i], stop) * (1 - slip), "stoploss")
                elif goal is not None and highs[i] >= goal:
                    close_trade(i, max(opens[i], goal) * (1 - slip), "target")
            else:
                stop = entry * (1 + stop_pct / 100) if stop_pct else None
                goal = entry * (1 - target_pct / 100) if target_pct else None
                if stop is not None and highs[i] >= stop:
                    close_trade(i, max(opens[i], stop) * (1 + slip), "stoploss")
                elif goal is not None and lows[i] <= goal:
                    close_trade(i, min(opens[i], goal) * (1 + slip), "target")
        # 4. intraday: nothing is held past the square-off candle
        if intraday and pos and (moment >= t_close or i + 1 == len(bars) or stamps[i + 1].normalize() != stamp.normalize()):
            close_trade(i, closes[i] * (1 - slip if pos > 0 else 1 + slip), "square-off")
        if pos:
            in_market += 1
        equity[i] = cash + pos * qty_per * (closes[i] - entry)

    if pos:
        gross = pos * qty_per * (closes[-1] - entry)
        trades.append({"side": "LONG" if pos > 0 else "SHORT", "lots": lots, "quantity": qty_per,
                       "entry_date": stamps[entry_i].strftime(fmt), "exit_date": None, "entry_price": round(entry, 2),
                       "exit_price": round(closes[-1], 2), "pnl": round(gross - entry_cost, 2),
                       "return_pct": round((gross - entry_cost) / (margin_pct / 100 * qty_per * entry) * 100, 2),
                       "days": int((stamps[-1] - stamps[entry_i]).days), "rolls": rolls_in_trade, "exit": "open", "open": True})

    curve = pd.Series(equity, index=stamps)
    hold = capital * bars["Close"] / bars["Close"].iloc[0]
    if intraday:
        day = stamps.normalize()
        curve, hold = curve.groupby(day).last(), hold.groupby(day).last()
        in_market = int(round(in_market / len(bars) * len(curve)))
    metrics = _metrics(curve, trades, params, total_charges, in_market)
    metrics.update(rolls=rolls, skipped_entries=skipped, long_trades=sum(t["side"] == "LONG" for t in trades),
                   short_trades=sum(t["side"] == "SHORT" for t in trades),
                   margin_per_lot=round(margin_pct / 100 * lot_size * float(closes[-1]), 0))
    label = {"long": "long", "short": "short", "both": "long & short"}[direction]
    return {"strategy": {"name": f"{strategy_name} → {underlying} futures, {label}"},
            "params": {**params.__dict__, "direction": direction, "lots": lots, "lot_size": lot_size, "margin_pct": margin_pct,
                       "stop_pct": stop_pct, "target_pct": target_pct, "slippage_pct": slippage_pct, "carry_pct": carry_pct},
            "period": {"start": curve.index[0].strftime("%Y-%m-%d"), "end": curve.index[-1].strftime("%Y-%m-%d"),
                       "bars": len(bars), "days": len(curve)},
            "metrics": metrics, "buy_hold": _curve_stats(hold, params), "trades": trades,
            "equity": _points(curve), "buy_hold_equity": _points(hold), "benchmark_equity": None, "benchmark": None,
            "symbol": underlying, "skipped_entries": skipped, "futures": True}
