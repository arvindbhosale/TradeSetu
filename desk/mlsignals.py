"""Machine-learning strategies: a model that forecasts the next few days' return from price and volume features.

The features follow the ideas of Microsoft Qlib's Alpha158 set (github.com/microsoft/qlib, MIT licence): returns
over several windows, price against its moving averages, volatility, where the close sits in its recent range,
volume against its average, and the shape of the day's candle. The model is a ridge regression, Qlib's linear
baseline, fitted with numpy alone.

It is trained **walk-forward**, so no forecast uses a bar it has not seen:

- At bar ``t`` the label of a past bar ``i`` (its return over the next ``horizon`` bars) is known only when
  ``i + horizon <= t``. Only those rows train the model.
- The model is fitted on the last ``train`` such rows, used for the next ``RETRAIN`` bars, then fitted again.
- Features are scaled with the training rows' mean and spread only.

The strategy is long while the forecast return over ``horizon`` bars is above ``threshold_pct``, flat otherwise.
It holds nothing until it has ``train`` bars to learn from, so a backtest or an algo needs that much history first.

What to expect: a forecast this simple has little edge on its own, and the costs of trading often eat it. Judge it
the way the AI scan does, on prices the settings never saw, against buy and hold.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

RETRAIN = 21                 # bars between refits (about a month of daily bars)
RIDGE = 10.0                 # shrinkage on the scaled features
MIN_ROWS = 60


def features(bars: pd.DataFrame) -> pd.DataFrame:
    """One row of features per bar, from that bar and the bars before it only."""
    close, open_, high, low = (bars[k].astype(float) for k in ("Close", "Open", "High", "Low"))
    volume = bars["Volume"].astype(float).where(lambda v: v > 0) if "Volume" in bars else pd.Series(np.nan, bars.index)
    ret = close.pct_change()
    out = {}
    for n in (1, 5, 10, 20, 60):
        out[f"roc{n}"] = close / close.shift(n) - 1
    for n in (5, 10, 20, 60):
        out[f"ma{n}"] = close / close.rolling(n).mean() - 1
    for n in (5, 20, 60):
        out[f"std{n}"] = ret.rolling(n).std()
    for n in (10, 20, 60):
        lo, hi = low.rolling(n).min(), high.rolling(n).max()
        out[f"rsv{n}"] = (close - lo) / (hi - lo).where(lambda x: x > 0)
    out["kmid"] = (close - open_) / open_
    out["klen"] = (high - low) / open_
    out["kup"] = (high - np.maximum(open_, close)) / open_
    out["klow"] = (np.minimum(open_, close) - low) / open_
    # Volume: missing for indices and some feeds, so these default to 0 (no information) rather than drop rows.
    logv = np.log(volume)
    for n in (5, 20):
        out[f"vma{n}"] = (logv - logv.rolling(n).mean()).fillna(0.0)
    out["corr20"] = close.rolling(20).corr(logv).fillna(0.0)
    frame = pd.DataFrame(out, index=bars.index).replace([np.inf, -np.inf], np.nan)
    frame["rsv10"] = frame["rsv10"].fillna(0.5)
    frame["rsv20"] = frame["rsv20"].fillna(0.5)
    frame["rsv60"] = frame["rsv60"].fillna(0.5)
    return frame


def forecast(bars: pd.DataFrame, horizon: int, train: int) -> pd.Series:
    """The walk-forward forecast of each bar's return over the next ``horizon`` bars (NaN until trained)."""
    x = features(bars).to_numpy()
    close = bars["Close"].astype(float).to_numpy()
    n = len(close)
    label = np.full(n, np.nan)
    if n > horizon:
        label[:-horizon] = close[horizon:] / close[:-horizon] - 1
    usable = ~np.isnan(x).any(axis=1)
    pred = np.full(n, np.nan)
    for start in range(train + horizon, n, RETRAIN):
        newest = start - horizon                    # rows up to here have a label that bar ``start`` has seen
        rows = np.arange(max(0, newest - train + 1), newest + 1)
        keep = rows[usable[rows] & ~np.isnan(label[rows])]
        if len(keep) < max(MIN_ROWS, train // 2):
            continue
        xt, yt = x[keep], label[keep]
        lo, hi = np.percentile(yt, [1, 99])
        yt = np.clip(yt, lo, hi)                    # one gap day should not set the model
        mu, sd = xt.mean(axis=0), xt.std(axis=0)
        sd[sd == 0] = 1.0
        z = (xt - mu) / sd
        y_mean = yt.mean()
        weights = np.linalg.solve(z.T @ z + RIDGE * np.eye(z.shape[1]), z.T @ (yt - y_mean))
        seg = np.arange(start, min(start + RETRAIN, n))
        seg = seg[usable[seg]]
        pred[seg] = ((x[seg] - mu) / sd) @ weights + y_mean
    return pd.Series(pred, index=bars.index)


def ml_forecast(bars: pd.DataFrame, horizon: int, threshold_pct: float, train: int) -> pd.Series:
    pred = forecast(bars, int(horizon), int(train))
    return (pred > threshold_pct / 100).astype(int)
