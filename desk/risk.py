"""Exit rules for one open position or option leg: stoploss, target, trailing stoploss, stoploss to cost, re-entry.

One set of rules serves the stock backtest, the stock algos, the option strategy backtest and the F&O algos, so a
backtest and the algo made from it exit the same way.

Every distance is in the rules' ``unit``: ``pct`` (percent of the entry price) or ``points`` (rupees per share, or
premium points per option). ``side`` is +1 for a buy (a rise is profit) and -1 for a sell (a fall is profit).

- **Stoploss**: exit when the price moves ``stop_loss`` against the entry.
- **Target**: exit when the price moves ``target`` in favour.
- **Trailing stoploss** (needs a stoploss): each time the price moves ``trail_every`` further in favour, the
  stoploss moves ``trail_by`` in favour. Buy at 100, stoploss 10, every 10 move it 5: at 110 the stoploss is 95,
  at 120 it is 100. It never moves back.
- **Stoploss to cost**: once the price has moved ``cost_after`` in favour, the stoploss moves to the entry price.
- **Re-entry** after a plain stoploss (not a trailed one, not one at cost), up to ``reentry`` times:
  ``back_to_entry`` waits until the price is back at the earlier entry price and enters there again;
  ``at_once`` enters again straight away (an option leg is then opened at the strike the entry rule picks now).
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass

UNITS = ("pct", "points")
REENTRY_MODES = ("back_to_entry", "at_once")
MAX_REENTRY = 5


@dataclass(frozen=True)
class ExitRules:
    unit: str = "pct"
    stop_loss: float = 0.0
    target: float = 0.0
    trail_every: float = 0.0
    trail_by: float = 0.0
    cost_after: float = 0.0
    reentry: int = 0
    reentry_mode: str = "back_to_entry"

    @property
    def active(self) -> bool:
        return bool(self.stop_loss or self.target or self.cost_after)

    def validate(self, what: str = "") -> ExitRules:
        where = f"{what}: " if what else ""
        if self.unit not in UNITS:
            raise ValueError(f"{where}choose % or points for the stoploss and target")
        top = 1000.0 if self.unit == "pct" else 1e7
        for label, value in (("Stoploss", self.stop_loss), ("Target", self.target),
                             ("Trailing stoploss: price move", self.trail_every),
                             ("Trailing stoploss: move stoploss by", self.trail_by),
                             ("Stoploss to cost", self.cost_after)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
                    or not 0 <= value <= top:
                raise ValueError(f"{where}{label} must be between 0 and {top:g} (0 for none)")
        if self.unit == "pct" and self.stop_loss >= 100 and what.lower().startswith("stock"):
            raise ValueError(f"{where}a stock's stoploss must be under 100%")
        if bool(self.trail_every) != bool(self.trail_by):
            raise ValueError(f"{where}a trailing stoploss needs both boxes: how far the price moves, and how far the "
                             f"stoploss follows")
        if self.trail_every and not self.stop_loss:
            raise ValueError(f"{where}a trailing stoploss needs a stoploss to start from")
        if self.trail_by > self.trail_every:
            raise ValueError(f"{where}the trailing stoploss cannot move further than the price did")
        if isinstance(self.reentry, bool) or not isinstance(self.reentry, int) or not 0 <= self.reentry <= MAX_REENTRY:
            raise ValueError(f"{where}re-entries must be a whole number from 0 to {MAX_REENTRY}")
        if self.reentry and not self.stop_loss:
            raise ValueError(f"{where}re-entry happens after a stoploss: set a stoploss first")
        if self.reentry_mode not in REENTRY_MODES:
            raise ValueError(f"{where}re-entry must be 'when price comes back' or 'straight away'")
        return self

    def distance(self, entry: float, value: float) -> float:
        return entry * value / 100 if self.unit == "pct" else value

    def levels(self, entry: float, best: float, side: int = 1) -> tuple[float | None, str, float | None]:
        """``(stop, kind, target)`` for a position entered at ``entry`` whose most favourable price so far is
        ``best`` (the highest for a buy, the lowest for a sell). ``kind`` is the stoploss's name for the exit:
        stoploss, trailing stoploss or stoploss at cost. ``stop`` or ``target`` is None when not set."""
        gain = (best - entry) * side
        stop, kind = None, "stoploss"
        if self.stop_loss:
            stop = entry - side * self.distance(entry, self.stop_loss)
            if self.trail_every and gain > 0:
                steps = math.floor(gain / self.distance(entry, self.trail_every) + 1e-9)
                if steps:
                    stop += side * steps * self.distance(entry, self.trail_by)
                    kind = "trailing stoploss"
        if self.cost_after and gain >= self.distance(entry, self.cost_after) - 1e-9 \
                and (stop is None or (entry - stop) * side > 0):
            stop, kind = entry, "stoploss at cost"
        target = entry + side * self.distance(entry, self.target) if self.target else None
        return stop, kind, target

    def text(self) -> str:
        if not (self.active or self.trail_every):
            return "no stoploss or target"
        u = "%" if self.unit == "pct" else " pts"
        parts = []
        if self.stop_loss:
            parts.append(f"stoploss {self.stop_loss:g}{u}")
        if self.target:
            parts.append(f"target {self.target:g}{u}")
        if self.trail_every:
            parts.append(f"trail: every {self.trail_every:g}{u} move stoploss {self.trail_by:g}{u}")
        if self.cost_after:
            parts.append(f"stoploss to cost after {self.cost_after:g}{u}")
        if self.reentry:
            how = "when price comes back" if self.reentry_mode == "back_to_entry" else "straight away"
            parts.append(f"re-enter up to {self.reentry}× {how}")
        return ", ".join(parts)

    def as_dict(self) -> dict:
        return asdict(self)


def _number(raw: dict, key: str, label: str, integer: bool = False):
    value = raw.get(key, 0)
    if value in (None, ""):
        return 0 if integer else 0.0
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be a number")
    if integer:
        if int(value) != value:
            raise ValueError(f"{label} must be a whole number")
        return int(value)
    return float(value)


def from_payload(raw: dict | None, what: str = "", legacy: dict | None = None) -> ExitRules:
    """ExitRules from a request's ``exits`` object, checked. ``legacy`` maps old percent fields still sent by
    saved algos and scripts: ``{"stop_loss": 2.0, "target": 4.0}`` is used when ``raw`` is missing."""
    if raw is None:
        raw = {"unit": "pct", **(legacy or {})}
    if not isinstance(raw, dict):
        raise ValueError("exits must be an object")
    rules = ExitRules(unit=str(raw.get("unit") or "pct"),
                      stop_loss=_number(raw, "stop_loss", "Stoploss"), target=_number(raw, "target", "Target"),
                      trail_every=_number(raw, "trail_every", "Trailing stoploss"),
                      trail_by=_number(raw, "trail_by", "Trailing stoploss"),
                      cost_after=_number(raw, "cost_after", "Stoploss to cost"),
                      reentry=_number(raw, "reentry", "Re-entries", integer=True),
                      reentry_mode=str(raw.get("reentry_mode") or "back_to_entry"))
    return rules.validate(what)


NONE = ExitRules()
