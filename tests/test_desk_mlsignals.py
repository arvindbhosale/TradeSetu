"""The ML forecast strategy: walk-forward only, finds a planted edge, finds none in a random walk."""

import numpy as np
import pandas as pd
import pytest

from desk import aiscan
from desk.mlsignals import RETRAIN, features, forecast
from desk.strategies import STRATEGIES

pytestmark = pytest.mark.unit


def series(closes, volume=1e5):
    idx = pd.bdate_range("2020-01-01", periods=len(closes))
    c = np.asarray(closes, dtype=float)
    return pd.DataFrame({"Open": np.r_[c[0], c[:-1]], "High": c * 1.005, "Low": c * 0.995, "Close": c,
                         "Volume": volume}, index=idx)


def planted(n=1500, seed=0):
    """Returns that carry over for about five days: a pattern the features can see."""
    rng = np.random.default_rng(seed)
    shocks = rng.normal(0, 0.01, n)
    r = np.convolve(shocks, np.ones(5) / 2.2, mode="full")[:n] * 0.6 + rng.normal(0, 0.004, n)
    return series(100 * np.exp(np.cumsum(r)))


def random_walk(n=1500, seed=1):
    return series(100 * np.exp(np.cumsum(np.random.default_rng(seed).normal(0, 0.01, n))))


def ic(bars, horizon=5):
    return forecast(bars, horizon, 250).corr(bars["Close"].shift(-horizon) / bars["Close"] - 1)


def test_the_forecast_never_uses_a_later_bar():
    bars = planted(900)
    params = {"horizon": 5, "threshold_pct": 0.2, "train": 150}
    strategy = STRATEGIES["ml_forecast"]
    full, cut = strategy.positions(bars, params), strategy.positions(bars.iloc[:600], params)
    assert full.iloc[:600].equals(cut) and full.iloc[:600].sum() > 0
    f_full, f_cut = forecast(bars, 5, 150), forecast(bars.iloc[:600], 5, 150)
    assert np.allclose(f_full.iloc[:600].fillna(-9), f_cut.fillna(-9))
    assert np.allclose(features(bars).iloc[:600].fillna(-9), features(bars.iloc[:600]).fillna(-9))


def test_it_holds_nothing_until_it_has_the_training_bars():
    bars = planted(600)
    f = forecast(bars, 5, 250)
    assert f.iloc[:255].isna().all() and f.iloc[255:255 + RETRAIN].notna().all()
    assert STRATEGIES["ml_forecast"].positions(bars, {"train": 250}).iloc[:255].sum() == 0
    assert STRATEGIES["ml_forecast"].positions(bars.iloc[:200]).sum() == 0          # too short: flat, no error


def test_it_finds_a_planted_edge_and_none_in_a_random_walk():
    assert ic(planted()) > 0.1
    assert abs(ic(random_walk())) < 0.07


def test_it_works_without_volume_and_joins_the_ai_scan():
    bars = planted(800)
    bars["Volume"] = 0.0                                                          # indices have none
    assert forecast(bars, 5, 250).notna().sum() > 400
    keys = {c["strategy"] for c in aiscan.candidates("quick")}
    assert "ml_forecast" in keys
    assert STRATEGIES["ml_forecast"].describe()["style"] == "Machine learning"
