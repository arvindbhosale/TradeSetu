"""Signal test: does a stock-picking signal really work across a whole index?

Every ``hold`` trading days, all stocks of the index are ranked by the signal,
using only prices up to that day. We then look at what each stock did over the
next ``hold`` days. Two checks:

* Rank match (information coefficient): how well the ranking matched the next
  returns, from -1 (exactly backwards) through 0 (no link) to +1 (perfect). A
  steady 0.03 to 0.05 is already useful in practice.
* Groups: the stocks are split into ``groups`` equal groups by signal, and each
  group's average next return is shown. A working signal has the top group
  ahead of the bottom one, with the groups in between roughly in order.

The top group is also run as a portfolio (equal money, rebalanced each time,
with costs on what changes) against all stocks equally weighted.

Caution: the stock list is today's index. Stocks that fell out of the index
are missing from the past, which flatters every signal a little.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd

SIGNALS = {
    "mom_12_1": ("12-month momentum", "Return over the last 12 months, leaving out the latest month. High is ranked first."),
    "mom_6m": ("6-month momentum", "Return over the last 6 months. High is ranked first."),
    "mom_3m": ("3-month momentum", "Return over the last 3 months. High is ranked first."),
    "reversal_1w": ("Last week's losers", "Return over the last 5 days, lowest first: tests whether last week's fallers bounce."),
    "near_high": ("Near the 52-week high", "Price as a share of its 52-week high. Closest to the high is ranked first."),
    "low_vol": ("Low volatility", "How much the price swung over the last 60 days. Calmest is ranked first."),
    "above_200dma": ("Above the 200-day average", "How far the price is above its 200-day average. Furthest above is ranked first."),
    "rsi_low": ("Low RSI (oversold)", "14-day RSI, lowest first: tests whether oversold stocks bounce."),
}
WARMUP = 260
MIN_STOCKS = 20


def _rsi(close: pd.DataFrame, n: int = 14) -> pd.DataFrame:
    delta = close.diff()
    gain = delta.clip(lower=0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    loss = (-delta.clip(upper=0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    return 100 - 100 / (1 + gain / loss.replace(0, np.nan))


def signal_values(key: str, close: pd.DataFrame) -> pd.DataFrame:
    """Higher = ranked first, for every day and stock, from past prices only."""
    if key == "mom_12_1":
        return close.shift(21) / close.shift(252) - 1
    if key == "mom_6m":
        return close / close.shift(126) - 1
    if key == "mom_3m":
        return close / close.shift(63) - 1
    if key == "reversal_1w":
        return -(close / close.shift(5) - 1)
    if key == "near_high":
        return close / close.rolling(252, min_periods=200).max()
    if key == "low_vol":
        return -np.log(close).diff().rolling(60, min_periods=50).std()
    if key == "above_200dma":
        return close / close.rolling(200, min_periods=180).mean() - 1
    if key == "rsi_low":
        return -_rsi(close)
    raise ValueError(f"signal must be one of {', '.join(SIGNALS)}")


def run(closes: dict[str, pd.Series], signal: str, *, start: str, end: str, hold: int = 5, groups: int = 5,
        cost_pct: float = 0.2, names: dict[str, str] | None = None, top_list: int = 20) -> dict:
    if signal not in SIGNALS:
        raise ValueError(f"signal must be one of {', '.join(SIGNALS)}")
    if not 1 <= int(hold) <= 63:
        raise ValueError("Hold for 1 to 63 trading days")
    if not 3 <= int(groups) <= 10:
        raise ValueError("Use 3 to 10 groups")
    if not 0 <= float(cost_pct) <= 2:
        raise ValueError("Costs must be between 0% and 2%")
    hold, groups, cost = int(hold), int(groups), float(cost_pct) / 100
    close = pd.DataFrame(closes).sort_index()
    close = close[close.index <= pd.Timestamp(end)]
    close = close.where(close > 0)
    if close.shape[1] < MIN_STOCKS:
        raise ValueError(f"Only {close.shape[1]} stocks had prices; the test needs at least {MIN_STOCKS}.")
    values = signal_values(signal, close)
    forward = close.shift(-hold) / close - 1
    days = close.index[close.index >= pd.Timestamp(start)]
    if len(days) < hold * 12:
        raise ValueError("The period is too short: it needs at least 12 rebalances. Start earlier or hold for fewer days.")
    dates = days[::hold]

    ic_rows, group_rows, top_curve, all_curve = [], [], [1.0], [1.0]
    turnover, held = [], set()
    curve_dates, ends = [], []
    for d in dates:
        s, f = values.loc[d], forward.loc[d]
        ok = s.notna() & f.notna()
        if ok.sum() < MIN_STOCKS:
            continue
        s, f = s[ok], f[ok]
        ic = float(s.rank().corr(f.rank()))
        if not math.isfinite(ic):
            continue
        ic_rows.append((d, ic))
        bucket = pd.qcut(s.rank(method="first"), groups, labels=False)      # 0 = lowest signal
        means = f.groupby(bucket).mean()
        group_rows.append(means.reindex(range(groups)).to_numpy())
        top = set(s.index[bucket == groups - 1])
        changed = 1.0 if not held else len(top - held) / max(len(top), 1)
        turnover.append(changed)
        trade_cost = cost * changed * (1 if not held else 2)     # buy the new names; after the first time, sell the old too
        held = top
        top_curve.append(top_curve[-1] * (1 + float(f[list(top)].mean()) - trade_cost))
        all_curve.append(all_curve[-1] * (1 + float(f.mean())))
        curve_dates.append(d)
        ends.append(close.index[min(close.index.get_loc(d) + hold, len(close.index) - 1)])
    if len(ic_rows) < 8:
        raise ValueError("Too few dates had enough stocks with history. Start later or choose a bigger index.")

    ics = np.array([v for _, v in ic_rows])
    mean_ic, sd_ic = float(ics.mean()), float(ics.std(ddof=1))
    t_stat = mean_ic / sd_ic * math.sqrt(len(ics)) if sd_ic > 0 else 0.0
    per_year = 252 / hold
    group_mean = np.nanmean(np.vstack(group_rows), axis=0)
    group_yearly = [round(((1 + g) ** per_year - 1) * 100, 2) for g in group_mean]
    spread = float(group_mean[-1] - group_mean[0])
    order = float(pd.Series(group_mean).rank().corr(pd.Series(range(groups), dtype=float)))
    hit = float((ics > 0).mean())
    if abs(t_stat) < 2:
        verdict, text = "none", ("Does not work: the link between the ranking and next returns is too weak to tell "
                                 "apart from chance over this period.")
    elif mean_ic > 0:
        if mean_ic >= 0.03 and order >= 0.7:
            verdict, text = "works", "Works: higher-ranked stocks did better, steadily, and the groups line up in order."
        else:
            verdict, text = "weak", "Weak: there is a real link, but it is small or the groups are not in order."
    else:
        verdict, text = "reverse", ("Works in reverse: the stocks this signal ranks first did worse. "
                                    "The opposite signal may be worth testing.")

    n_years = max((curve_dates[-1] - curve_dates[0]).days, 1) / 365.25
    top_total, all_total = top_curve[-1] - 1, all_curve[-1] - 1

    def cagr(total):
        return round(((1 + total) ** (1 / n_years) - 1) * 100, 2) if total > -1 else -100.0

    last = values.iloc[-1].dropna().sort_values(ascending=False)
    names = names or {}
    latest = [{"symbol": sym, "name": names.get(sym, sym.removesuffix(".NS")), "value": round(float(v), 4),
               "price": round(float(close[sym].dropna().iloc[-1]), 2)} for sym, v in last.head(top_list).items()]
    ic_series = pd.Series(ics, index=[d for d, _ in ic_rows])
    rolling = ic_series.rolling(12, min_periods=6).mean().dropna()
    return {
        "signal": signal, "signal_name": SIGNALS[signal][0], "signal_text": SIGNALS[signal][1],
        "verdict": verdict, "verdict_text": text,
        "mean_ic": round(mean_ic, 4), "ic_t": round(t_stat, 2), "ic_hit_pct": round(hit * 100, 1),
        "rebalances": len(ics), "stocks": int(close.shape[1]), "hold": hold, "groups": groups,
        "group_yearly_pct": group_yearly, "top_minus_bottom_yearly_pct": round(((1 + spread) ** per_year - 1) * 100, 2),
        "groups_in_order": round(order, 2), "avg_turnover_pct": round(float(np.mean(turnover[1:] or [1.0])) * 100, 1),
        "top": {"total_return_pct": round(top_total * 100, 2), "cagr_pct": cagr(top_total)},
        "all": {"total_return_pct": round(all_total * 100, 2), "cagr_pct": cagr(all_total)},
        "equity": [[d.strftime("%Y-%m-%d"), round(a, 4), round(b, 4)] for d, a, b in zip(
            [curve_dates[0]] + ends, top_curve, all_curve, strict=True)],
        "ic_rolling": [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in rolling.items()],
        "latest_date": str(close.index[-1].date()), "latest_top": latest,
        "period": [str(curve_dates[0].date()), str(curve_dates[-1].date())],
        "cost_pct": cost * 100,
    }
