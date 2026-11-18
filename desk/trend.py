"""How a line is called Uptrend, Downtrend or Sideways. One rule, set by the user, used everywhere.

Methods:

- ``ma`` (default): Uptrend when the last value is above the slow average and
  the fast average is above the slow one; Downtrend when both are below;
  otherwise Sideways.
- ``price_ma``: against one average. Uptrend more than ``band_pct`` above it,
  Downtrend more than ``band_pct`` below, Sideways in between.
- ``supertrend``: the Supertrend's direction; needs high and low as well.
- ``return``: the move over ``lookback`` bars. Up beyond ``+threshold_pct``,
  down beyond ``-threshold_pct``.
"""

from __future__ import annotations

import pandas as pd

from . import indicators as ind

DEFAULT = {"method": "ma", "fast": 20, "slow": 50, "period": 50, "band_pct": 1.0,
           "st_period": 10, "st_multiplier": 3.0, "lookback": 20, "threshold_pct": 3.0}
METHODS = {
    "ma": "Two moving averages",
    "price_ma": "Price against one moving average",
    "supertrend": "Supertrend direction",
    "return": "Return over a period",
}
_LIMITS = {"fast": (2, 200, int), "slow": (3, 400, int), "period": (2, 400, int), "band_pct": (0.0, 20.0, float),
           "st_period": (2, 100, int), "st_multiplier": (0.5, 10.0, float), "lookback": (1, 250, int),
           "threshold_pct": (0.0, 100.0, float)}


def validate(raw) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("The trend rule must be an object")
    rule = dict(DEFAULT)
    method = raw.get("method", DEFAULT["method"])
    if method not in METHODS:
        raise ValueError(f"Trend method must be one of {', '.join(METHODS)}")
    rule["method"] = method
    for key, (low, high, kind) in _LIMITS.items():
        value = raw.get(key, DEFAULT[key])
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or not low <= value <= high:
            raise ValueError(f"Trend rule: {key.replace('_', ' ')} must be between {low:g} and {high:g}")
        rule[key] = kind(value)
    if rule["fast"] >= rule["slow"]:
        raise ValueError("Trend rule: the fast average must be shorter than the slow one")
    return rule


def needed(rule: dict) -> int:
    """Bars of history the rule needs before it can call a trend."""
    return {"ma": rule["slow"], "price_ma": rule["period"], "supertrend": rule["st_period"] + 2,
            "return": rule["lookback"] + 1}[rule["method"]]


def classify(bars, rule: dict | None = None) -> str:
    """``bars`` is a close series or a frame with Close (and High, Low for Supertrend)."""
    rule = rule or DEFAULT
    frame = bars if isinstance(bars, pd.DataFrame) else pd.DataFrame({"Close": bars})
    close = frame["Close"].dropna()
    if len(close) < needed(rule):
        return "Not enough data"
    last = float(close.iloc[-1])
    method = rule["method"]
    if method == "ma":
        fast, slow = float(close.iloc[-rule["fast"]:].mean()), float(close.iloc[-rule["slow"]:].mean())
        if last > slow and fast > slow:
            return "Uptrend"
        if last < slow and fast < slow:
            return "Downtrend"
        return "Sideways"
    if method == "price_ma":
        average = float(close.iloc[-rule["period"]:].mean())
        gap = (last / average - 1) * 100
        return "Uptrend" if gap > rule["band_pct"] else "Downtrend" if gap < -rule["band_pct"] else "Sideways"
    if method == "return":
        move = (last / float(close.iloc[-1 - rule["lookback"]]) - 1) * 100
        return "Uptrend" if move > rule["threshold_pct"] else "Downtrend" if move < -rule["threshold_pct"] else "Sideways"
    if not {"High", "Low"} <= set(frame.columns):
        frame = frame.assign(High=frame["Close"], Low=frame["Close"])
    _, direction = ind.supertrend(frame.dropna(subset=["Close"]), rule["st_period"], rule["st_multiplier"])
    return "Uptrend" if int(direction.iloc[-1]) == 1 else "Downtrend"


def describe(rule: dict | None = None) -> str:
    rule = rule or DEFAULT
    method = rule["method"]
    if method == "ma":
        return (f"Uptrend: the last value is above its {rule['slow']}-day average and the {rule['fast']}-day average "
                f"is above the {rule['slow']}-day. Downtrend: both below. Otherwise Sideways.")
    if method == "price_ma":
        return (f"Uptrend: more than {rule['band_pct']:g}% above its {rule['period']}-day average. Downtrend: more "
                f"than {rule['band_pct']:g}% below. Otherwise Sideways.")
    if method == "return":
        return (f"Uptrend: up more than {rule['threshold_pct']:g}% over the last {rule['lookback']} days. Downtrend: "
                f"down more than {rule['threshold_pct']:g}%. Otherwise Sideways.")
    return (f"Uptrend or Downtrend by the direction of a Supertrend ({rule['st_period']}, {rule['st_multiplier']:g}) "
            f"on daily candles. It is never Sideways.")
