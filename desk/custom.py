"""Strategies the user builds by combining indicators.

A custom strategy is two rule groups, entry and exit. Each rule compares one
indicator with another indicator or a number: ``RSI(14) < 30``,
``Close crosses above SMA(50)``. A group passes when ALL or ANY of its rules
do. The strategy goes long when the entry group passes and flat when the exit
group passes; exit wins when both do. It then runs anywhere a built-in
strategy does: backtest, paper and live algos.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import uuid
from pathlib import Path

import pandas as pd

from . import indicators as ind
from .settings import home
from .strategies import STRATEGIES, Strategy, _latch

OPS = {
    ">": "is above", "<": "is below", ">=": "is at or above", "<=": "is at or below",
    "crosses_above": "crosses above", "crosses_below": "crosses below",
    "is_one_of": "is one of",
}
TIME_OPS = {">": "is after", "<": "is before", ">=": "is at or after", "<=": "is at or before"}
MAX_RULES = 8
MAX_STRATEGIES = 50
PREFIX = "custom:"


def _side(raw, label: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} is missing")
    if "value" in raw and raw.get("indicator") in (None, "", "value"):
        value = raw["value"]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != value or abs(value) > 1e12:
            raise ValueError(f"{label}: enter a number")
        return {"value": float(value)}
    key = raw.get("indicator")
    if key not in ind.OPERANDS:
        raise ValueError(f"{label}: choose an indicator")
    return {"indicator": key, "params": ind.resolve_params(ind.OPERANDS[key], raw.get("params"))}


def _is_clock(value: float) -> bool:
    hhmm = int(value)
    return hhmm == value and 0 <= hhmm // 100 <= 23 and 0 <= hhmm % 100 <= 59


def _days(raw, where: str) -> list[int]:
    days = raw.get("days") if isinstance(raw, dict) else raw
    if (not isinstance(days, list) or not days
            or not all(isinstance(d, int) and not isinstance(d, bool) and 1 <= d <= 7 for d in days)):
        raise ValueError(f"{where}: tick at least one day")
    return sorted(set(days))


def _patterns(raw, where: str) -> list[str]:
    from .candles import PATTERNS
    chosen = raw.get("patterns") if isinstance(raw, dict) else raw
    if (not isinstance(chosen, list) or not chosen or len(chosen) > len(PATTERNS)
            or not all(isinstance(k, str) and k in PATTERNS for k in chosen)):
        raise ValueError(f"{where}: tick at least one candle pattern")
    return [k for k in PATTERNS if k in chosen]


def _group(raw, label: str, required: bool) -> dict:
    if not isinstance(raw, dict):
        raise ValueError(f"{label} rules are missing")
    match = raw.get("match", "all")
    if match not in ("all", "any"):
        raise ValueError(f"{label}: match must be all or any")
    rules = raw.get("rules")
    if not isinstance(rules, list) or (required and not rules):
        raise ValueError(f"Add at least one {label.lower()} rule")
    if len(rules) > MAX_RULES:
        raise ValueError(f"{label}: at most {MAX_RULES} rules")
    out = []
    for number, rule in enumerate(rules, 1):
        where = f"{label} rule {number}"
        if not isinstance(rule, dict) or rule.get("op") not in OPS:
            raise ValueError(f"{where}: choose a comparison")
        left = _side(rule.get("left"), f"{where}, left side")
        if "value" in left:
            raise ValueError(f"{where}: the left side must be an indicator")
        kind = ind.OPERANDS[left["indicator"]].kind
        if kind == "pattern":
            if rule["op"] != "is_one_of":
                raise ValueError(f"{where}: Candle pattern takes \"is one of\"")
            out.append({"left": left, "op": "is_one_of", "right": {"patterns": _patterns(rule.get("right"), where)}})
            continue
        if kind == "weekday":
            if rule["op"] != "is_one_of":
                raise ValueError(f"{where}: Day of week takes \"is one of\"")
            out.append({"left": left, "op": "is_one_of", "right": {"days": _days(rule.get("right"), where)}})
            continue
        if rule["op"] == "is_one_of":
            raise ValueError(f"{where}: \"is one of\" is for Day of week and Candle pattern")
        right = _side(rule.get("right"), f"{where}, right side")
        if kind == "time":
            if rule["op"] not in TIME_OPS:
                raise ValueError(f"{where}: Time of day takes after or before, not crosses")
            if "value" not in right or not _is_clock(right["value"]):
                raise ValueError(f"{where}: enter a time like 09:20")
        elif right.get("indicator") and ind.OPERANDS[right["indicator"]].kind != "number":
            raise ValueError(f"{where}: {ind.OPERANDS[right['indicator']].label} can only be on the left side")
        out.append({"left": left, "op": rule["op"], "right": right})
    return {"match": match, "rules": out}


def validate(raw: dict) -> dict:
    """A clean spec from untrusted input; raises ValueError naming the problem."""
    if not isinstance(raw, dict):
        raise ValueError("the strategy must be an object")
    name = " ".join(str(raw.get("name", "")).split())
    if not 1 <= len(name) <= 50 or not re.fullmatch(r"[\w &+\-.,()%/]+", name):
        raise ValueError("Give the strategy a name of up to 50 letters, digits or spaces")
    return {"name": name, "entry": _group(raw.get("entry"), "Entry", True),
            "exit": _group(raw.get("exit"), "Exit", True)}


def _series(side: dict, bars: pd.DataFrame, cache: dict) -> pd.Series:
    if "value" in side:
        return pd.Series(side["value"], index=bars.index)
    key = (side["indicator"], tuple(sorted(side["params"].items())))
    if key not in cache:
        cache[key] = ind.compute(bars, side["indicator"], side["params"])
    return cache[key]


def _rule(rule: dict, bars: pd.DataFrame, cache: dict) -> pd.Series:
    left = _series(rule["left"], bars, cache)
    op = rule["op"]
    if op == "is_one_of" and "patterns" in rule["right"]:
        return left[rule["right"]["patterns"]].any(axis=1).astype(bool)
    if op == "is_one_of":
        return left.isin([float(d) for d in rule["right"]["days"]]).astype(bool)
    right = _series(rule["right"], bars, cache)
    if op == ">":
        out = left > right
    elif op == "<":
        out = left < right
    elif op == ">=":
        out = left >= right
    elif op == "<=":
        out = left <= right
    else:
        above = left > right
        before = above.shift(1)
        known = left.notna() & right.notna() & left.shift(1).notna() & right.shift(1).notna()
        out = (above & (before == False) if op == "crosses_above"      # noqa: E712 - elementwise
               else (~above) & (before == True)) & known               # noqa: E712
    return out.where(left.notna() & right.notna(), False).astype(bool)


def _passes(group: dict, bars: pd.DataFrame, cache: dict) -> pd.Series:
    signals = [_rule(rule, bars, cache) for rule in group["rules"]]
    if not signals:
        return pd.Series(False, index=bars.index)
    combined = signals[0]
    for signal in signals[1:]:
        combined = (combined & signal) if group["match"] == "all" else (combined | signal)
    return combined


def positions(spec: dict, bars: pd.DataFrame) -> pd.Series:
    cache: dict = {}
    return _latch(_passes(spec["entry"], bars, cache), _passes(spec["exit"], bars, cache))


def _text(side: dict, kind: str = "number") -> str:
    if "days" in side:
        return ", ".join(ind.WEEKDAYS[d] for d in side["days"])
    if "patterns" in side:
        from .candles import PATTERNS
        return ", ".join(PATTERNS[k][0] for k in side["patterns"])
    if "value" in side:
        if kind == "time":
            return f"{int(side['value']) // 100:02d}:{int(side['value']) % 100:02d}"
        return f"{side['value']:g}"
    operand = ind.OPERANDS[side["indicator"]]
    args = ", ".join(f"{v:g}" for v in side["params"].values())
    return f"{operand.label}({args})" if args else operand.label


def describe_group(group: dict) -> str:
    joiner = " AND " if group["match"] == "all" else " OR "
    parts = []
    for r in group["rules"]:
        kind = ind.OPERANDS[r["left"]["indicator"]].kind
        word = TIME_OPS[r["op"]] if kind == "time" else OPS[r["op"]]
        parts.append(f"{_text(r['left'])} {word} {_text(r['right'], kind)}")
    return joiner.join(parts)


def as_strategy(strategy_id: str, spec: dict) -> Strategy:
    summary = f"Buy when {describe_group(spec['entry'])}. Sell when {describe_group(spec['exit'])}."
    used = [side["indicator"] for group in (spec["entry"], spec["exit"]) for rule in group["rules"]
            for side in (rule["left"], rule["right"]) if side.get("indicator")]
    return Strategy(PREFIX + strategy_id, spec["name"], summary, (), lambda bars: positions(spec, bars),
                    intraday_only=any(ind.OPERANDS[key].intraday for key in used), source="custom",
                    history_days=max((ind.OPERANDS[key].history_days for key in used), default=0),
                    entries=lambda bars: _passes(spec["entry"], bars, {}))


class CustomStrategies:
    def __init__(self, path: Path | None = None):
        self.path = path or home() / "custom_strategies.json"
        self._lock = threading.RLock()
        self._specs: dict[str, dict] = {}
        with contextlib.suppress(OSError, json.JSONDecodeError, ValueError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            for strategy_id, spec in stored.items():
                with contextlib.suppress(ValueError):       # an indicator this version dropped
                    self._specs[strategy_id] = validate(spec)

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._specs, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def save(self, raw: dict, strategy_id: str | None = None) -> dict:
        spec = validate(raw)
        with self._lock:
            if strategy_id is not None and strategy_id not in self._specs:
                raise ValueError("no such custom strategy")
            if any(other["name"].lower() == spec["name"].lower() and key != strategy_id
                   for key, other in self._specs.items()):
                raise ValueError("A custom strategy with that name already exists")
            if strategy_id is None:
                if len(self._specs) >= MAX_STRATEGIES:
                    raise ValueError(f"At most {MAX_STRATEGIES} custom strategies")
                strategy_id = uuid.uuid4().hex[:10]
            self._specs[strategy_id] = spec
            self._save()
            return self.view(strategy_id)

    def delete(self, strategy_id: str) -> None:
        with self._lock:
            if strategy_id not in self._specs:
                raise ValueError("no such custom strategy")
            del self._specs[strategy_id]
            self._save()

    def view(self, strategy_id: str) -> dict:
        spec = self._specs[strategy_id]
        return {"id": strategy_id, "key": PREFIX + strategy_id, **json.loads(json.dumps(spec)),
                "entry_text": describe_group(spec["entry"]), "exit_text": describe_group(spec["exit"])}

    def list(self) -> list[dict]:
        with self._lock:
            return [self.view(key) for key in self._specs]

    def strategy(self, key: str) -> Strategy:
        """The built-in or custom strategy for ``key``; raises ValueError."""
        if key in STRATEGIES:
            return STRATEGIES[key]
        with self._lock:
            if key.startswith(PREFIX) and key[len(PREFIX):] in self._specs:
                return as_strategy(key[len(PREFIX):], self._specs[key[len(PREFIX):]])
        raise ValueError(f"unknown strategy: {key}")

    def all(self) -> list[Strategy]:
        with self._lock:
            return list(STRATEGIES.values()) + [as_strategy(k, s) for k, s in self._specs.items()]


def inline(raw: dict) -> Strategy:
    """A strategy from rules typed on the Backtest screen, not saved. Raises ValueError naming the problem."""
    if not isinstance(raw, dict):
        raise ValueError("the rules must be an object")
    spec = validate({**raw, "name": raw.get("name") or "My rules"})
    return as_strategy("rules", spec)
