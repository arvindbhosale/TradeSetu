"""Pair trading: two stocks that usually move together, traded when the gap between them stretches.

The gap is the log-price of A minus a hedge ratio times the log-price of B. The
ratio and the gap's usual size come only from the past ``lookback`` days, so
the test never uses tomorrow's prices. When the gap is unusually wide (its
z-score beyond ``entry_z``), the desk buys the cheap stock and sells the dear
one, equal rupees in each. It closes when the gap is back near normal
(``exit_z``), when it stretches even further (``stop_z``, a stoploss), or after
``max_days``.

Signals are read at a day's close and traded at the next day's open.

In India a sold share must be bought back the same day, so holding a pair
overnight needs the stock futures of both. This test uses share prices and
ignores lot sizes; costs are charged per leg on each entry and exit.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

EXAMPLES = [
    ("HDFCBANK", "ICICIBANK", "Private banks"), ("KOTAKBANK", "AXISBANK", "Private banks"),
    ("TCS", "INFY", "IT services"), ("HCLTECH", "WIPRO", "IT services"),
    ("BPCL", "HINDPETRO", "Oil marketing"), ("ULTRACEMCO", "GRASIM", "Cement"),
    ("TATASTEEL", "JSWSTEEL", "Steel"), ("MARUTI", "M&M", "Cars"),
]


def _check(name, value, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be between {low:g} and {high:g}")
    return float(value)


def backtest(a: pd.DataFrame, b: pd.DataFrame, *, lookback: int = 60, entry_z: float = 2.0, exit_z: float = 0.5,
             stop_z: float = 4.0, max_days: int = 0, capital: float = 1_000_000.0, cost_pct: float = 0.05,
             names: tuple[str, str] = ("A", "B"), start: str | None = None) -> dict:
    lookback = int(_check("Look-back days", lookback, 20, 250))
    entry_z = _check("Open when the gap is", entry_z, 0.5, 5)
    exit_z = _check("Close when the gap is back to", exit_z, 0, entry_z - 0.1 if entry_z > 0.1 else 0)
    stop_z = _check("Stoploss gap", stop_z, 0, 10)
    if stop_z and stop_z <= entry_z:
        raise ValueError("The stoploss gap must be wider than the opening gap, or 0 for none")
    max_days = int(_check("Close after (days)", max_days, 0, 250))
    capital = _check("Capital", capital, 10_000, 1e10)
    cost = _check("Costs per trade", cost_pct, 0, 2) / 100

    frame = pd.DataFrame({"a": a["Close"], "b": b["Close"], "ao": a["Open"], "bo": b["Open"]}).dropna()
    frame = frame[(frame > 0).all(axis=1)]
    if len(frame) < lookback + 40:
        raise ValueError(f"Only {len(frame)} days with prices for both stocks; need at least {lookback + 40}.")
    la, lb = np.log(frame["a"]), np.log(frame["b"])
    # Hedge ratio from a rolling regression of log A on log B, using the past lookback days only.
    cov = la.rolling(lookback).cov(lb)
    var_a, var_b = la.rolling(lookback).var(), lb.rolling(lookback).var()
    beta = (cov / var_b).clip(0.1, 10)
    # Each day's z-score uses that day's ratio on the whole window, so a drifting ratio cannot fake a wide gap.
    spread = la - beta * lb
    mean = la.rolling(lookback).mean() - beta * lb.rolling(lookback).mean()
    std = np.sqrt((var_a + beta ** 2 * var_b - 2 * beta * cov).clip(lower=0))
    z = ((spread - mean) / std).replace([np.inf, -np.inf], np.nan)

    dates = frame.index
    start_at = lookback  # the first day with a full look-back behind it
    if start is not None:
        start_at = max(start_at, int(dates.searchsorted(pd.Timestamp(start))))
    if start_at >= len(dates) - 20:
        raise ValueError("Too few days between the start date and the end date for this look-back.")
    equity, cash_curve = capital, []
    position, entry, trades = 0, None, []       # +1 = bought A / sold B; -1 = sold A / bought B
    qty_a = qty_b = 0.0
    pending = None
    for i in range(len(frame)):
        row = frame.iloc[i]
        # 1. Orders decided yesterday fill at today's open.
        if pending is not None:
            kind = pending
            pending = None
            if kind == "close" and position:
                pnl = qty_a * (row["ao"] - entry["pa"]) + qty_b * (row["bo"] - entry["pb"])
                fees = cost * (abs(qty_a) * row["ao"] + abs(qty_b) * row["bo"])
                equity += pnl - fees
                trades.append({**entry["info"], "exit_date": str(dates[i].date()), "exit_a": round(row["ao"], 2),
                               "exit_b": round(row["bo"], 2), "days": int((dates[i] - entry["date"]).days),
                               "pnl": round(pnl - fees - entry["fees"], 2),
                               "return_pct": round((pnl - fees - entry["fees"]) / entry["capital"] * 100, 2),
                               "why": entry["why_close"]})
                position, entry, qty_a, qty_b = 0, None, 0.0, 0.0
            elif kind in (1, -1) and not position:
                half = equity / 2
                qty_a, qty_b = kind * half / row["ao"], -kind * half / row["bo"]
                fees = cost * equity
                equity -= fees
                position = kind
                entry = {"date": dates[i], "pa": row["ao"], "pb": row["bo"], "fees": fees, "capital": equity + fees,
                         "why_close": "", "info": {
                             "entry_date": str(dates[i].date()), "side": (f"Buy {names[0]}, sell {names[1]}" if kind == 1
                                                                          else f"Sell {names[0]}, buy {names[1]}"),
                             "entry_a": round(row["ao"], 2), "entry_b": round(row["bo"], 2),
                             "entry_z": round(float(z.iloc[i - 1]), 2)}}
        # 2. Mark to the close.
        open_pnl = qty_a * (row["a"] - entry["pa"]) + qty_b * (row["b"] - entry["pb"]) if position else 0.0
        cash_curve.append(equity + open_pnl)
        # 3. Decide at the close.
        zi = z.iloc[i]
        if i < start_at or i == len(frame) - 1 or not np.isfinite(zi):
            continue
        if position:
            why = None
            if (position == 1 and zi >= -exit_z) or (position == -1 and zi <= exit_z):
                why = "Gap back to normal"
            elif stop_z and abs(zi) >= stop_z:
                why = "Stoploss: gap kept widening"
            elif max_days and (dates[i] - entry["date"]).days >= max_days:
                why = f"Held {max_days} days"
            if why:
                entry["why_close"], pending = why, "close"
        elif zi <= -entry_z and not (stop_z and zi <= -stop_z):
            pending = 1                                   # A cheap against B: buy A, sell B
        elif zi >= entry_z and not (stop_z and zi >= stop_z):
            pending = -1

    curve = pd.Series(cash_curve, index=dates)
    curve = curve.iloc[start_at:]
    years = max((curve.index[-1] - curve.index[0]).days, 1) / 365.25
    total = curve.iloc[-1] / capital - 1
    daily = curve.pct_change().dropna()
    sharpe = float(daily.mean() / daily.std() * math.sqrt(252)) if daily.std() > 0 else None
    wins = [t for t in trades if t["pnl"] > 0]
    ra, rb = np.log(frame["a"]).diff(), np.log(frame["b"]).diff()
    correlation = float(ra.corr(rb))
    # How fast the gap closes: half-life from a one-step regression of the change on the level.
    # (Descriptive only, not used to trade: one ratio fitted on the whole period.)
    s = la - float(np.polyfit(lb, la, 1)[0]) * lb
    lag, change = s.shift(1).dropna(), s.diff().dropna()
    slope = float(np.polyfit(lag.loc[change.index], change, 1)[0]) if len(change) > 30 else 0.0
    half_life = round(-math.log(2) / slope, 1) if slope < 0 else None
    latest = float(z.iloc[-1]) if np.isfinite(z.iloc[-1]) else None
    if latest is None:
        now = "Not enough recent prices."
    elif latest <= -entry_z:
        now = f"{names[0]} is cheap against {names[1]} (gap {latest:.2f}). The rule would buy {names[0]} and sell {names[1]}."
    elif latest >= entry_z:
        now = f"{names[0]} is expensive against {names[1]} (gap {latest:.2f}). The rule would sell {names[0]} and buy {names[1]}."
    else:
        now = f"The gap is normal ({latest:.2f}). No trade."
    quality = []
    if correlation < 0.5:
        quality.append(f"The two move together only weakly (correlation {correlation:.2f}); pairs work best above about 0.7.")
    if half_life is None:
        quality.append("The gap does not tend to close over this period: a poor pair.")
    elif half_life > lookback:
        quality.append(f"The gap takes about {half_life:g} days to halve, longer than the look-back of {lookback} days.")
    peak = curve.cummax()
    return {
        "names": list(names),
        "metrics": {"total_return_pct": round(total * 100, 2),
                    "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2) if total > -1 else -100.0,
                    "max_drawdown_pct": round(float((curve / peak - 1).min()) * 100, 2),
                    "sharpe": None if sharpe is None else round(sharpe, 2), "trades": len(trades),
                    "win_rate_pct": round(len(wins) / len(trades) * 100, 1) if trades else None,
                    "avg_days": round(float(np.mean([t["days"] for t in trades])), 1) if trades else None,
                    "open_position": bool(position)},
        "correlation": round(correlation, 2), "half_life_days": half_life, "quality": quality,
        "now": now, "latest_z": None if latest is None else round(latest, 2),
        "equity": [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in curve.items()],
        "z": [[d.strftime("%Y-%m-%d"), round(float(v), 3)] for d, v in z.iloc[start_at:].items() if np.isfinite(v)],
        "trades": trades[-200:],
        "params": {"lookback": lookback, "entry_z": entry_z, "exit_z": exit_z, "stop_z": stop_z, "max_days": max_days,
                   "capital": capital, "cost_pct": cost * 100},
        "period": [str(curve.index[0].date()), str(curve.index[-1].date())],
    }
