"""Parameter optimiser with walk-forward testing.

A plain optimiser tries every setting on the whole history and keeps the best,
which mostly finds settings that fitted the past by luck. Walk-forward guards
against that: the history is cut into consecutive windows; in each, the best
setting is chosen on the first part (in-sample) and then traded, unchanged, on
the part that follows (out-of-sample). Only the out-of-sample results are
counted, so the figure reflects choosing settings without knowing the future.
"""

from __future__ import annotations

import itertools
import math

import numpy as np
import pandas as pd

from .backtest import BacktestParams, run_strategy
from .luck import heatmap, overfitting_score

MAX_COMBOS = 400
OBJECTIVES = {
    "sharpe": "Sharpe ratio", "cagr_pct": "Yearly return (CAGR)", "total_return_pct": "Total return",
    "calmar": "Return ÷ drawdown", "profit_factor": "Profit factor",
}


def grid(strategy, ranges: dict) -> list[dict]:
    """Every combination of the parameter ranges ``{name: [start, stop, step]}``; others stay at their defaults."""
    axes = {}
    for param in strategy.params:
        if param.name not in ranges:
            axes[param.name] = [param.default]
            continue
        spec = ranges[param.name]
        if not isinstance(spec, (list, tuple)) or len(spec) != 3:
            raise ValueError(f"{param.label}: give a start, stop and step")
        start, stop, step = (float(x) for x in spec)
        if step <= 0 or stop < start:
            raise ValueError(f"{param.label}: the step must be positive and the stop at least the start")
        if start < param.minimum or stop > param.maximum:
            raise ValueError(f"{param.label}: stay between {param.minimum:g} and {param.maximum:g}")
        count = int(math.floor((stop - start) / step + 1e-9)) + 1
        values = [start + i * step for i in range(count)]
        axes[param.name] = sorted({int(round(v)) if param.integer else round(v, 4) for v in values})
    total = math.prod(len(v) for v in axes.values())
    if total > MAX_COMBOS:
        raise ValueError(f"{total} combinations is more than {MAX_COMBOS}. Use larger steps or narrower ranges.")
    names = list(axes)
    return [dict(zip(names, combo, strict=True)) for combo in itertools.product(*(axes[n] for n in names))]


def score(metrics: dict, objective: str) -> float:
    if objective == "calmar":
        drawdown = abs(metrics.get("max_drawdown_pct") or 0)
        return (metrics.get("cagr_pct") or 0) / drawdown if drawdown > 0.5 else -math.inf
    value = metrics.get(objective)
    return -math.inf if value is None else float(value)


def _evaluate(bars, strategy, combos, params, objective, min_trades, returns=None):
    """Every setting, best first. When ``returns`` is a dict, each setting's bar-by-bar returns go in it."""
    rows = []
    for combo in combos:
        try:
            result = run_strategy(bars, strategy, combo, params)
        except ValueError:
            continue                      # e.g. fast >= slow: not a valid setting
        metrics = result["metrics"]
        ok = metrics["trades"] + metrics["open_trades"] >= min_trades
        if returns is not None and ok and result.get("equity"):
            curve = pd.Series({pd.Timestamp(d): float(v) for d, v in result["equity"]})
            returns[tuple(sorted(combo.items()))] = curve.pct_change().dropna()
        rows.append({"params": combo, "score": score(metrics, objective) if ok else -math.inf,
                     "metrics": {k: metrics.get(k) for k in ("total_return_pct", "cagr_pct", "sharpe", "max_drawdown_pct",
                                                             "trades", "win_rate_pct", "profit_factor")}})
    rows.sort(key=lambda r: r["score"], reverse=True)
    return rows


def optimise(bars: pd.DataFrame, strategy, ranges: dict, params: BacktestParams, objective: str = "sharpe",
             folds: int = 4, train_pct: float = 70.0, min_trades: int = 3, progress=None) -> dict:
    if objective not in OBJECTIVES:
        raise ValueError(f"objective must be one of {', '.join(OBJECTIVES)}")
    if not strategy.params:
        raise ValueError(f"{strategy.name} has no settings to optimise.")
    if not 1 <= folds <= 12 or not 40 <= train_pct <= 90:
        raise ValueError("Use 1 to 12 walk-forward windows and an in-sample share between 40% and 90%")
    combos = grid(strategy, ranges)
    n = len(bars)
    if n < 120:
        raise ValueError(f"only {n} candles; walk-forward needs at least 120")

    # 1. The whole history, for the table of every setting (in-sample only: it is not a forecast).
    if progress:
        progress("Testing every setting on the whole period")
    daily: dict = {}
    full = _evaluate(bars, strategy, combos, params, objective, min_trades, returns=daily)

    # 2. Walk forward: consecutive windows, each split into a training and a testing part.
    window = n // folds
    test_len = max(int(window * (1 - train_pct / 100)), 30)
    train_len = window - test_len
    if train_len < 60:
        raise ValueError("Each window is too short to choose settings in. Use fewer windows or a longer period.")
    windows, oos_curves, capital = [], [], params.capital
    for k in range(folds):
        start = k * window
        train = bars.iloc[start:start + train_len]
        test_end = start + window if k < folds - 1 else n
        test = bars.iloc[max(start + train_len - 60, 0):test_end]      # 60 bars of warm-up for indicators
        if progress:
            progress(f"Window {k + 1} of {folds}: choosing settings on {train.index[0]:%d %b %Y} to {train.index[-1]:%d %b %Y}")
        ranked = _evaluate(train, strategy, combos, params, objective, min_trades)
        if not ranked or ranked[0]["score"] == -math.inf:
            windows.append({"window": k + 1, "train": [str(train.index[0].date()), str(train.index[-1].date())],
                            "params": None, "note": f"No setting made {min_trades} trades in-sample."})
            continue
        best = ranked[0]
        test_params = BacktestParams(**{**params.__dict__, "capital": capital})
        out = run_strategy(test, strategy, best["params"], test_params)
        first_test_day = bars.index[start + train_len]
        curve = pd.Series({pd.Timestamp(d): v for d, v in out["equity"]})
        curve = curve[curve.index >= first_test_day]
        if len(curve):
            capital = float(curve.iloc[-1])
            oos_curves.append(curve)
        windows.append({
            "window": k + 1, "params": best["params"],
            "train": [str(train.index[0].date()), str(train.index[-1].date())],
            "test": [str(first_test_day.date()), str(bars.index[test_end - 1].date())],
            "in_sample": best["metrics"], "out_of_sample": {k2: out["metrics"].get(k2) for k2 in
                                                              ("total_return_pct", "max_drawdown_pct", "trades", "win_rate_pct")},
        })

    result = {"objective": objective, "objective_name": OBJECTIVES[objective], "combos": len(combos),
              "grid": [{**r, "score": None if r["score"] == -math.inf else round(r["score"], 3)} for r in full[:50]],
              "params_names": [p.name for p in strategy.params], "windows": windows,
              "heatmap": heatmap(full, [p.name for p in strategy.params]),
              "luck": overfitting_score(pd.DataFrame({str(k): v for k, v in daily.items()})) if len(daily) >= 2 else None}
    if oos_curves:
        stitched = pd.concat(oos_curves)
        stitched = stitched[~stitched.index.duplicated(keep="last")]
        years = max((stitched.index[-1] - stitched.index[0]).days, 1) / 365.25
        start_value = params.capital
        total = float(stitched.iloc[-1]) / start_value - 1
        peak = stitched.cummax()
        result["walk_forward"] = {
            "equity": [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in stitched.items()],
            "total_return_pct": round(total * 100, 2),
            "cagr_pct": round(((1 + total) ** (1 / years) - 1) * 100, 2) if total > -1 else -100.0,
            "max_drawdown_pct": round(float((stitched / peak - 1).min()) * 100, 2),
            "period": [str(stitched.index[0].date()), str(stitched.index[-1].date())],
        }
        in_sample = [w["in_sample"]["cagr_pct"] for w in windows if w.get("in_sample") and w["in_sample"]["cagr_pct"] is not None]
        if in_sample and np.mean(in_sample) > 0:
            result["walk_forward"]["efficiency_pct"] = round(result["walk_forward"]["cagr_pct"] / float(np.mean(in_sample)) * 100, 1)
        chosen = [tuple(sorted(w["params"].items())) for w in windows if w.get("params")]
        result["walk_forward"]["stable"] = len(set(chosen)) <= max(1, len(chosen) // 2)
    return result
