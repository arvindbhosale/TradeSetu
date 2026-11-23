"""Add to winners: buy more of a position that is going your way, at set profit levels, up to a set size.

One set of adds serves every backtest and algo: stocks (intraday or positional), index futures, bought options and
option strategies (presets or your own legs). Buy 2 lots, add 1 when the profit reaches X, 1 more at Y.

- **Levels.** How far the trade has moved in your favour since the *first* entry, in the adds' ``unit``: ``pct``
  (% of the first entry price; for an option strategy % of its premium), ``points`` (₹ a share, index points,
  premium points) or ``rs`` (option strategies: profit of one lot in rupees). Levels rise; each add is taken once.
- **Sizes.** Stocks: % of the shares of the first buy, rounded down to whole shares. Futures and options: whole lots.
  The adds together are the cap: the position never grows past the first entry plus every add.
- **Option strategies** (``scope``). ``position`` (the default) adds whole lots of the position: one lot of every
  leg (``ratio`` lots of a leg marked ×2), bought legs first, so a sold leg is never added without its hedge.
  ``leg`` adds lots of each *bought* option leg on its own, when that leg's own premium has risen by the level: a
  sold leg is never added alone.
- **Stoploss after an add** (``stop_after``). Adding to a winner raises the average entry, so a fall back to the
  first entry now loses money. ``risk`` (the default) moves the stoploss of the whole position so that, if it is
  hit, the loss is no more than the loss the first entry's stoploss planned (``stop_price``); without a stoploss
  that is the average entry (breakeven). ``cost`` moves it to the average entry. Before charges, and a gap through
  the stoploss can still lose more.
- **Money.** Every add is checked against the money (margin) available first, and skipped when it is short. A stock
  algo's budget covers the first buy and every add: the first buy uses only the part of it that leaves room for
  every add at its level's price (``first_budget``).
- **With profit steps.** Once a profit step has booked part of the position, no more adds: the trade is being
  scaled out, not in.
- A backtest with adds is also run without them, so the result says whether adding helped (``adds_compare``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from . import scaleout

KINDS = scaleout.KINDS
UNITS = scaleout.UNITS
STOP_AFTER = ("risk", "cost")
SCOPES = ("position", "leg")
MAX_ADDS = 5
MAX_STOCK_PCT = 300.0          # every add together: at most three times the first buy
MAX_LOTS = 50                  # an F&O position: the first lots and every add together
_TOP = {"pct": 1000.0, "points": 1e7, "rs": 1e8}


@dataclass(frozen=True)
class Adds:
    unit: str = "pct"
    levels: tuple = ()          # profit distances from the first entry in ``unit``, rising
    sizes: tuple = ()           # stock: % of the first buy's shares; futures and options: lots
    stop_after: str = "risk"
    scope: str = "position"     # option strategies: the whole position, or each bought leg on its own

    @property
    def active(self) -> bool:
        return bool(self.levels)

    def distance(self, entry: float, value: float) -> float:
        """A level as a price distance from ``entry`` (``pct`` and ``points`` only)."""
        return entry * value / 100 if self.unit == "pct" else value

    def total(self) -> float:
        return float(sum(self.sizes))

    def shares(self, first: int, n: int) -> int:
        """Whole shares add ``n`` buys, from a first buy of ``first`` shares."""
        return int(math.floor(first * self.sizes[n] / 100 + 1e-9))

    def lots(self) -> list[int]:
        return [int(s) for s in self.sizes]

    def first_budget(self, budget: float, price: float) -> float:
        """The part of a stock budget the first buy at ``price`` may use, so that every add fits in the rest when
        it is bought at its level (before charges and slippage)."""
        if not self.active or price <= 0:
            return budget
        at = lambda v: price + self.distance(price, v)  # noqa: E731 - the price an add's level is reached at
        return budget / (1 + sum(s / 100 * at(v) / price for v, s in zip(self.levels, self.sizes, strict=True)))

    def text(self, kind: str = "futures") -> str:
        if not self.active:
            return ""
        unit = {"pct": "%", "points": " pts", "rs": ""}[self.unit]
        size = (lambda s: f"{s:g}% more shares") if kind == "stock" else (lambda s: f"{int(s)} lot{'s' if s != 1 else ''}")
        level = (lambda v: f"₹{v:,.0f} profit a lot") if self.unit == "rs" else (lambda v: f"+{v:g}{unit}")
        parts = [f"{size(s)} at {level(v)}" for v, s in zip(self.levels, self.sizes, strict=True)]
        whom = " of each bought leg that gains" if self.scope == "leg" else ""
        moved = {"risk": "; then the stoploss keeps the loss within the first plan",
                 "cost": "; then the stoploss moves to the average entry"}[self.stop_after]
        return "add to winners" + whom + ": " + ", ".join(parts) + moved

    def as_dict(self) -> dict:
        return {"unit": self.unit, "stop_after": self.stop_after, "scope": self.scope,
                "adds": [{"at": v, "size": s} for v, s in zip(self.levels, self.sizes, strict=True)]}


NONE = Adds()


def _number(value, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{label} must be a number")
    return float(value)


def from_payload(raw, kind: str, lots: int | None = None, legs: list[dict] | None = None) -> Adds:
    """Adds from a request's ``adds`` object, checked. Missing, empty or without rows: no adds.

    ``lots``: the first entry's lots (futures and options), so the whole position stays within ``MAX_LOTS``.
    ``legs``: an option strategy's legs (``optstrat.legs_for``), for adds to each bought leg."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {', '.join(KINDS)}")
    if raw in (None, "", {}):
        return NONE
    if not isinstance(raw, dict):
        raise ValueError("Add to winners: send an object with unit, adds and stop_after")
    listed = raw.get("adds") or []
    if not isinstance(listed, list):
        raise ValueError("Add to winners: adds must be a list")
    blank = (None, "", 0)          # a row left empty on the form is no add
    rows = [r for r in listed if isinstance(r, dict) and (r.get("at") not in blank or r.get("size") not in blank)]
    if not rows:
        return NONE
    unit = str(raw.get("unit") or "pct")
    if unit not in UNITS[kind]:
        names = {"pct": "%", "points": "points", "rs": "₹ a lot"}
        raise ValueError(f"Add to winners: measure in {' or '.join(names[u] for u in UNITS[kind])}")
    stop_after = str(raw.get("stop_after") or "risk")
    if stop_after not in STOP_AFTER:
        raise ValueError("Add to winners: after an add the stoploss keeps the first plan's loss, or moves to the "
                         "average entry")
    scope = str(raw.get("scope") or "position")
    if scope not in SCOPES:
        raise ValueError("Add to winners: add to the whole position, or to each bought leg")
    if scope == "leg":
        if kind != "structure":
            raise ValueError("Add to winners: adding to one leg is for option strategies")
        if legs is not None and not any(x["action"] > 0 and x["type"] != "FUT" for x in legs):
            raise ValueError("Add to winners: only a bought option leg can be added to on its own, and this "
                             "strategy has none. Add to the whole position instead")
    if len(rows) > MAX_ADDS:
        raise ValueError(f"Add to winners: at most {MAX_ADDS} adds")
    levels, sizes = [], []
    for n, row in enumerate(rows, 1):
        at = _number(row.get("at", 0) or 0, f"Add {n}: profit level")
        size = _number(row.get("size", 0) or 0, f"Add {n}: size")
        if not 0 < at <= _TOP[unit]:
            raise ValueError(f"Add {n}: the profit level must be above 0 and at most {_TOP[unit]:g}")
        if levels and at <= levels[-1]:
            raise ValueError(f"Add {n}: each add's profit level must be higher than the one before")
        if kind == "stock":
            if not 0 < size <= 100:
                raise ValueError(f"Add {n}: buy between 0 and 100% of the first buy's shares")
        else:
            if size != int(size) or size < 1:
                raise ValueError(f"Add {n}: add a whole number of lots, at least 1. Futures and options trade in "
                                 f"whole lots only")
            size = int(size)
        levels.append(at)
        sizes.append(size)
    total = sum(sizes)
    if kind == "stock" and total > MAX_STOCK_PCT + 1e-9:
        raise ValueError(f"Add to winners: the adds buy {total:g}% of the first buy; at most {MAX_STOCK_PCT:g}%")
    if kind != "stock" and lots is not None and lots + total > MAX_LOTS:
        raise ValueError(f"Add to winners: {lots} lots plus {total} added is more than {MAX_LOTS} lots")
    return Adds(unit=unit, levels=tuple(levels), sizes=tuple(sizes), stop_after=stop_after, scope=scope)


def of(saved: dict | None) -> Adds:
    """Adds stored on an algo (``as_dict``), unchecked: they were checked when saved."""
    if not saved or not saved.get("adds"):
        return NONE
    return Adds(unit=saved.get("unit", "pct"), levels=tuple(float(r["at"]) for r in saved["adds"]),
                sizes=tuple(r["size"] for r in saved["adds"]), stop_after=saved.get("stop_after", "risk"),
                scope=saved.get("scope", "position"))


def stop_price(side: int, average: float, quantity: float, risk: float, stop_after: str) -> float:
    """The stoploss of a position of ``quantity`` at ``average`` (``side`` +1 long, -1 short) after an add.

    ``risk``: the loss the first entry's stoploss planned, in rupees (0 when it had none). ``risk`` keeps the loss at
    that stoploss to ``risk``; ``cost`` (or no planned risk) is the average entry."""
    if stop_after == "cost" or risk <= 0 or quantity <= 0:
        return average
    return average - side * risk / quantity


def floor_after(risk: float, stop_after: str) -> float:
    """The lowest the whole position's profit may fall to after an add, in rupees (option strategies)."""
    return 0.0 if stop_after == "cost" or risk <= 0 else -risk


COMPARED = scaleout.COMPARED


def compare(with_adds: dict, without: dict) -> dict:
    """The same backtest with the adds and without them, side by side: did adding to winners help?"""
    a, b = with_adds["metrics"], without["metrics"]
    gain = round(a["final_value"] - b["final_value"], 2)
    verdict = "adds" if gain > 0 else "without" if gain < 0 else "same"
    return {"with_adds": {k: a.get(k) for k in COMPARED}, "without_adds": {k: b.get(k) for k in COMPARED},
            "difference": gain, "better": verdict, "adds_made": a.get("adds_made", 0),
            "adds_skipped": a.get("adds_skipped", 0)}


def run_all(run, steps: scaleout.Steps, adds: Adds) -> dict:
    """``run(steps, adds)`` → a backtest result. With active steps it also carries ``steps_compare`` (the same run
    with one exit), with active adds ``adds_compare`` (the same run without adds)."""
    result = run(steps, adds)
    if steps.active:
        result["steps_compare"] = scaleout.compare(result, run(scaleout.NONE, adds))
    if adds.active:
        result["adds_compare"] = compare(result, run(steps, NONE))
    return result
