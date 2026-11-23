"""Book profit in steps: close part of a position at each profit level and let the rest run.

One set of steps serves every backtest and algo: stocks (intraday or positional), index futures, bought options and
option strategies (presets or your own legs). Buy 4 lots, close 1 when the profit reaches X, 1 more at Y, and so on.

- **Sizes.** Futures and options close whole lots. A lot of an option strategy is one lot of every leg (two of a leg
  marked ×2), so a step closes the bought and the sold legs together: a sold leg is never left without its hedge.
  Stocks close a share of the position, in % of the shares bought, rounded down to whole shares; a step that rounds
  to 0 shares is skipped. Steps may add up to the whole position: the last one then closes everything.
- **Levels.** How far the trade has moved in your favour since the entry, in the steps' ``unit``:
  ``pct`` (% of the entry price; for an option strategy % of the premium received or paid), ``points`` (₹ a share,
  index points, premium points) or ``rs`` (option strategies: profit of one lot in rupees). The levels are distances
  from the entry, not fixed prices, because an algo and a backtest enter many times at different prices.
- **Move the stoploss** (``stop_after``). Booking part of a winner lowers the average winner; the lots still open can
  then turn the trade into a loss. ``cost`` moves the stoploss of what is left to the entry price after the first
  step; ``previous`` moves it to the cost after the first step and to the previous step's level after each later one.
- A backtest with steps is also run without them, so the result says whether the steps helped (``compare``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

KINDS = ("stock", "futures", "option", "structure")
UNITS = {"stock": ("pct", "points"), "futures": ("pct", "points"), "option": ("pct", "points"),
         "structure": ("pct", "rs")}
STOP_AFTER = ("none", "cost", "previous")
MAX_STEPS = 6
_TOP = {"pct": 1000.0, "points": 1e7, "rs": 1e8}


@dataclass(frozen=True)
class Steps:
    unit: str = "pct"
    levels: tuple = ()          # profit distances in ``unit``, rising
    sizes: tuple = ()           # stock: % of the shares bought; futures and options: lots
    stop_after: str = "none"

    @property
    def active(self) -> bool:
        return bool(self.levels)

    def distance(self, entry: float, value: float) -> float:
        """A level as a price distance from ``entry`` (``pct`` and ``points`` only)."""
        return entry * value / 100 if self.unit == "pct" else value

    def lock(self, done: int) -> float | None:
        """The profit level the stoploss has moved to after ``done`` steps (0 is the entry), or None."""
        if not done or self.stop_after == "none":
            return None
        if self.stop_after == "cost" or done == 1:
            return 0.0
        return float(self.levels[done - 2])

    def lock_name(self, done: int) -> str:
        level = self.lock(done)
        return "stoploss at cost after a profit step" if not level else f"stoploss at profit step {done - 1}"

    def shares(self, total: int) -> list[int]:
        """Whole shares each step sells from a position of ``total`` shares."""
        out, left, share = [], int(total), 0.0
        for n, size in enumerate(self.sizes):
            share += size
            last = n == len(self.sizes) - 1 and share >= 100 - 1e-9
            quantity = left if last else min(int(math.floor(total * size / 100 + 1e-9)), left)
            out.append(quantity)
            left -= quantity
        return out

    def lots(self) -> list[int]:
        return [int(s) for s in self.sizes]

    def text(self, kind: str = "futures") -> str:
        if not self.active:
            return ""
        unit = {"pct": "%", "points": " pts", "rs": ""}[self.unit]
        size = (lambda s: f"{s:g}% of the shares") if kind == "stock" else (lambda s: f"{int(s)} lot{'s' if s != 1 else ''}")
        level = (lambda v: f"₹{v:,.0f} profit a lot") if self.unit == "rs" else (lambda v: f"+{v:g}{unit}")
        parts = [f"{size(s)} at {level(v)}" for v, s in zip(self.levels, self.sizes, strict=True)]
        moved = {"none": "", "cost": "; then stoploss to cost",
                 "previous": "; then stoploss to cost, then to the previous step"}[self.stop_after]
        return "book profit in steps: " + ", ".join(parts) + moved

    def as_dict(self) -> dict:
        return {"unit": self.unit, "stop_after": self.stop_after,
                "steps": [{"at": v, "size": s} for v, s in zip(self.levels, self.sizes, strict=True)]}


NONE = Steps()


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a number")
    return float(value)


def from_payload(raw, kind: str, lots: int | None = None) -> Steps:
    """Steps from a request's ``steps`` object, checked. Missing, empty or without rows: no steps.

    ``lots`` is the position's size for futures and options: the steps may not close more lots than it has."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if raw in (None, "", {}):
        return NONE
    if not isinstance(raw, dict):
        raise ValueError("Book profit in steps: send an object with unit, steps and stop_after")
    listed = raw.get("steps") or []
    if not isinstance(listed, list):
        raise ValueError("Book profit in steps: steps must be a list")
    blank = (None, "", 0)          # a row left empty on the form is no step
    rows = [r for r in listed if isinstance(r, dict) and (r.get("at") not in blank or r.get("size") not in blank)]
    if not rows:
        return NONE
    unit = str(raw.get("unit") or "pct")
    if unit not in UNITS[kind]:
        names = {"pct": "%", "points": "points", "rs": "₹ a lot"}
        raise ValueError(f"Book profit in steps: measure in {' or '.join(names[u] for u in UNITS[kind])}")
    stop_after = str(raw.get("stop_after") or "none")
    if stop_after not in STOP_AFTER:
        raise ValueError("Book profit in steps: after a step the stoploss stays, moves to cost, or moves to the "
                         "previous step")
    if len(rows) > MAX_STEPS:
        raise ValueError(f"Book profit in steps: at most {MAX_STEPS} steps")
    levels, sizes = [], []
    for n, row in enumerate(rows, 1):
        at = _number(row.get("at", 0) or 0, f"Step {n}: profit level")
        size = _number(row.get("size", 0) or 0, f"Step {n}: size")
        if not 0 < at <= _TOP[unit]:
            raise ValueError(f"Step {n}: the profit level must be above 0 and at most {_TOP[unit]:g}")
        if levels and at <= levels[-1]:
            raise ValueError(f"Step {n}: each step's profit level must be higher than the one before")
        if kind == "stock":
            if not 0 < size <= 100:
                raise ValueError(f"Step {n}: sell between 0 and 100% of the shares")
        else:
            if size != int(size) or size < 1:
                raise ValueError(f"Step {n}: close a whole number of lots, at least 1. Futures and options trade "
                                 f"in whole lots only")
            size = int(size)
        levels.append(at)
        sizes.append(size)
    total = sum(sizes)
    if kind == "stock" and total > 100 + 1e-9:
        raise ValueError(f"Book profit in steps: the steps sell {total:g}% of the shares, more than the 100% bought")
    if kind != "stock" and lots is not None and total > lots:
        raise ValueError(f"Book profit in steps: the steps close {total} lots but the position has {lots}. "
                         f"Close fewer lots in the steps, or trade more lots")
    return Steps(unit=unit, levels=tuple(levels), sizes=tuple(sizes), stop_after=stop_after)


def of(saved: dict | None) -> Steps:
    """Steps stored on an algo (``as_dict``), unchecked: they were checked when saved."""
    if not saved or not saved.get("steps"):
        return NONE
    return Steps(unit=saved.get("unit", "pct"), levels=tuple(float(r["at"]) for r in saved["steps"]),
                 sizes=tuple(r["size"] for r in saved["steps"]), stop_after=saved.get("stop_after", "none"))


COMPARED = ("total_return_pct", "final_value", "max_drawdown_pct", "win_rate_pct", "avg_win", "avg_loss",
            "profit_factor", "trades")


def compare(with_steps: dict, single: dict) -> dict:
    """The same backtest with the steps and with one exit, side by side: did booking in steps help?"""
    a, b = with_steps["metrics"], single["metrics"]
    gain = round(a["final_value"] - b["final_value"], 2)
    verdict = "steps" if gain > 0 else "single" if gain < 0 else "same"
    return {"with_steps": {k: a.get(k) for k in COMPARED}, "single_exit": {k: b.get(k) for k in COMPARED},
            "difference": gain, "better": verdict, "steps_booked": a.get("steps_booked", 0)}


def run_both(run, steps: Steps) -> dict:
    """``run(steps)`` → a backtest result; with active steps the result also carries ``steps_compare`` against the
    same run with one exit."""
    result = run(steps)
    if steps.active:
        result["steps_compare"] = compare(result, run(NONE))
    return result
