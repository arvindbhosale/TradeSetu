"""Asset-allocation backtests: Ray Dalio's All Weather and Harry Browne's Permanent Portfolio, with Indian ETFs.

Each portfolio holds fixed weights in a few ETFs and is put back to those
weights every quarter (or month, or year). Cash earns a fixed rate, because
the liquid ETF (LIQUIDBEES) pays its return as daily dividends that the price
series does not show. The test starts on the first day every ETF has a price.

All Weather (Dalio, as published by Tony Robbins, *Money: Master the Game*,
2014): 30% stocks, 40% long-term government bonds, 15% intermediate bonds,
7.5% gold, 7.5% commodities. India's ETFs: NIFTYBEES, LTGILTBEES (long gilt),
GILT5YBEES (5-year gilt), GOLDBEES, SILVERBEES. GILT5YBEES starts in 2021 and
SILVERBEES in 2022, so the "longer history" variant swaps them for long gilt
and gold and starts in 2018.
"""

from __future__ import annotations

import pandas as pd

from .backtest import BacktestParams, _curve_stats, _points

CASH = "CASH"
PORTFOLIOS = {
    "all_weather": {"name": "Ray Dalio: All Weather (India ETFs)", "legend": "Ray Dalio", "markets": ("bull", "bear", "sideways"),
                    "weights": {"NIFTYBEES.NS": 30, "LTGILTBEES.NS": 40, "GILT5YBEES.NS": 15, "GOLDBEES.NS": 7.5, "SILVERBEES.NS": 7.5},
                    "about": "Balanced across growth and inflation surprises so no single economic season sinks it. Lower return "
                             "than stocks in a boom, far smaller falls in a bust. History from 2022 (SILVERBEES)."},
    "all_weather_long": {"name": "Ray Dalio: All Weather, longer history", "legend": "Ray Dalio", "markets": ("bull", "bear", "sideways"),
                         "weights": {"NIFTYBEES.NS": 30, "LTGILTBEES.NS": 55, "GOLDBEES.NS": 15},
                         "about": "The same idea with the ETFs that go back to 2018: long gilts take the bond share, gold the "
                                  "commodity share."},
    "permanent": {"name": "Harry Browne: Permanent Portfolio", "legend": "Harry Browne", "markets": ("bull", "bear", "sideways"),
                  "weights": {"NIFTYBEES.NS": 25, "LTGILTBEES.NS": 25, "GOLDBEES.NS": 25, CASH: 25},
                  "about": "A quarter each in stocks, long bonds, gold and cash: one asset for prosperity, deflation, inflation "
                           "and recession."},
    "nifty_gold": {"name": "60/40 Nifty and gold", "legend": "", "markets": ("bull", "bear"),
                   "weights": {"NIFTYBEES.NS": 60, "GOLDBEES.NS": 40},
                   "about": "Gold has often risen when Indian stocks fell; the simplest hedge, history from 2009."},
}
FREQUENCIES = {"M": "every month", "Q": "every quarter", "Y": "every year", "N": "never (buy and hold)"}


def repair_splits(series: pd.Series) -> tuple[pd.Series, list]:
    """Mend bad prints and unadjusted splits.

    A fall of 50%+ by close to a whole number (2, 5, 10, 100...) that reverses within 5 days is a bad print
    (Yahoo shows NIFTYBEES and GOLDBEES at 1/10 and 1/100 on 19-20 Dec 2019): those days are scaled back. One
    that does not reverse is an unadjusted split: every earlier price is divided by the ratio.
    """
    s = series.dropna().astype(float).copy()
    found = []
    i = 1
    while i < len(s):
        r = s.iloc[i - 1] / s.iloc[i]
        whole = round(r)
        if r >= 1.9 and whole >= 2 and abs(r / whole - 1) < 0.06:
            back = next((j for j in range(i + 1, min(i + 6, len(s)))
                         if abs(s.iloc[j] / s.iloc[j - 1] / whole - 1) < 0.06), None)
            if back is not None:
                s.iloc[i:back] = s.iloc[i:back] * whole
                found.append((s.index[i].strftime("%Y-%m-%d"), f"bad print ×1/{whole}"))
                i = back
                continue
            s.iloc[:i] = s.iloc[:i] / whole
            found.append((s.index[i].strftime("%Y-%m-%d"), f"split 1:{whole}"))
        i += 1
    return s, found


def run_portfolio(prices: dict[str, pd.Series], weights: dict[str, float], *, capital: float = 1_000_000.0,
                  rebalance: str = "Q", cash_rate_pct: float = 6.5, cost_pct: float = 0.1, start=None, end=None,
                  risk_free_rate_pct: float = 6.5, benchmark: str = "NIFTYBEES.NS") -> dict:
    if rebalance not in FREQUENCIES:
        raise ValueError(f"rebalance must be one of {', '.join(FREQUENCIES)}")
    total = sum(weights.values())
    if not weights or abs(total - 100) > 0.01 or any(w < 0 for w in weights.values()):
        raise ValueError("Weights must be zero or more and add up to 100%")
    assets = [a for a in weights if a != CASH and weights[a] > 0]
    repaired, splits = {}, []
    for a in assets:
        repaired[a], found = repair_splits(prices[a])
        splits += [f"{a} {what} on {d}" for d, what in found]
    frame = pd.DataFrame(repaired).sort_index().dropna()
    if start:
        frame = frame[frame.index >= pd.Timestamp(start)]
    if end:
        frame = frame[frame.index <= pd.Timestamp(end)]
    if len(frame) < 60:
        raise ValueError("Fewer than 60 days when every ETF has a price; choose a later start or other ETFs.")
    daily_cash = (1 + cash_rate_pct / 100) ** (1 / 252) - 1
    target = {a: weights[a] / 100 for a in weights if weights[a] > 0}
    units = {a: capital * target[a] / frame[a].iloc[0] for a in assets}
    cash = capital * target.get(CASH, 0.0)
    costs, rebalances, values = capital * cost_pct / 100, 0, []
    value = capital - costs
    cash -= costs
    periods = frame.index.to_period(rebalance if rebalance != "N" else "Y")
    for i in range(len(frame)):
        if i:
            cash *= 1 + daily_cash
        value = cash + sum(units[a] * frame[a].iloc[i] for a in assets)
        if rebalance != "N" and i + 1 < len(frame) and periods[i + 1] != periods[i]:
            turnover = 0.0
            for a in assets:
                want = value * target[a]
                turnover += abs(want - units[a] * frame[a].iloc[i])
                units[a] = want / frame[a].iloc[i]
            turnover += abs(value * target.get(CASH, 0.0) - cash)
            fee = turnover / 2 * cost_pct / 100
            cash = value * target.get(CASH, 0.0) - fee
            value -= fee
            costs += fee
            rebalances += 1
        values.append(value)
    curve = pd.Series(values, index=frame.index)
    params = BacktestParams(capital=capital, risk_free_rate_pct=risk_free_rate_pct)
    out = {"strategy": {"name": "Portfolio"}, "params": {**params.__dict__, "weights": weights, "rebalance": rebalance},
           "period": {"start": frame.index[0].strftime("%Y-%m-%d"), "end": frame.index[-1].strftime("%Y-%m-%d"),
                      "bars": len(frame), "days": len(frame)},
           "metrics": {**_curve_stats(curve, params), "trades": rebalances, "open_trades": 0, "win_rate_pct": None,
                       "profit_factor": None, "avg_win": None, "avg_loss": None, "avg_days_held": None, "exposure_pct": 100.0,
                       "total_charges": round(costs, 2)},
           "equity": _points(curve), "trades": [], "portfolio": True,
           "assets": [{"symbol": a, "weight": weights[a]} for a in weights if weights[a] > 0]}
    out["splits"] = splits
    if benchmark in prices:
        bench = repair_splits(prices[benchmark])[0].reindex(frame.index).ffill().dropna()
        if len(bench) > 1:
            scaled = capital * bench / bench.iloc[0]
            out["buy_hold"] = _curve_stats(scaled, params)
            out["buy_hold_equity"] = _points(scaled)
    out.setdefault("buy_hold", None)
    out.setdefault("buy_hold_equity", [])
    out.update(benchmark_equity=None, benchmark=None)
    return out
