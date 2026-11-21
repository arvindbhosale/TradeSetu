"""Your results: what your own closed trades say, overall and by setup, source, time and mistake.

A trade here is a whole position (``pnl.trade_key``): partial exits add up into one result. Figures are after
charges. Under MIN_TRADES trades a group is shown but marked too small: a handful of trades says little about
an edge. The 95% range of the average result uses the normal approximation (average +/- 1.96 x standard error);
when it spans zero the trades so far cannot tell an edge from luck.
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime

MIN_TRADES = 30
WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _when(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
    except (TypeError, ValueError):
        return None


def positions(closed: list[dict], open_rows: list[dict], notes: dict[str, dict]) -> list[dict]:
    """One row per trade, newest first: closed rows of a position added up, still-open positions after them."""
    groups: dict[str, list[dict]] = defaultdict(list)
    for row in closed:
        groups[row["key"]].append(row)
    still_open = {r["key"]: r for r in open_rows}
    out = []
    for key, rows in groups.items():
        first, last = rows[0], rows[-1]
        quantity = sum(r["quantity"] for r in rows)
        long = first["side"] == "LONG"
        entry = first["buy_price"] if long else first["sell_price"]
        exit_value = sum((r["sell_price"] if long else r["buy_price"]) * r["quantity"] for r in rows)
        pnl = round(sum(r["pnl"] for r in rows), 2)
        opened, closed_at = _when(first["opened_at"]), _when(last["time_ist"])
        note = notes.get(key)
        trade = {"key": key, "mode": key.split("|", 1)[0], "source": first["source"], "exchange": first["exchange"],
                 "symbol": first["symbol"], "side": first["side"], "quantity": quantity, "entry": entry,
                 "exit": round(exit_value / quantity, 2) if quantity else None, "pnl": pnl,
                 "return_pct": round(pnl / (entry * quantity * first.get("multiplier", 1)) * 100, 2)
                 if entry and quantity else None,
                 "opened_at": first["opened_at"], "closed_at": last["time_ist"],
                 "days": (closed_at.date() - opened.date()).days if opened and closed_at else None,
                 "status": "Part closed" if key in still_open else "Closed", "note": note,
                 "r": None}
        if note and note.get("stop") and entry and quantity:
            risk = abs(entry - note["stop"]) * quantity * first.get("multiplier", 1)
            trade["r"] = round(pnl / risk, 2) if risk else None
        out.append(trade)
    for key, row in still_open.items():
        if key in groups:
            continue
        out.append({"key": key, "mode": key.split("|", 1)[0], "source": row["source"], "exchange": row["exchange"],
                    "symbol": row["symbol"], "side": "SHORT" if row["quantity"] < 0 else "LONG",
                    "quantity": abs(row["quantity"]), "entry": row["average"], "exit": None,
                    "pnl": row["unrealized"], "return_pct": None, "opened_at": row["opened_at"], "closed_at": None,
                    "days": None, "status": "Open", "note": notes.get(key), "r": None})
    out.sort(key=lambda t: t["closed_at"] or "9999", reverse=True)
    out.sort(key=lambda t: t["status"] != "Open")
    return out


def summarize(trades: list[dict]) -> dict:
    """Statistics of closed trades (in time order for the streak and drawdown)."""
    trades = sorted(trades, key=lambda t: t["closed_at"] or "")
    results = [t["pnl"] for t in trades]
    n = len(results)
    out: dict = {"trades": n, "enough": n >= MIN_TRADES}
    if not n:
        return out
    wins = [p for p in results if p > 0]
    losses = [p for p in results if p <= 0]
    mean = sum(results) / n
    out.update({
        "total": round(sum(results), 2), "win_rate": round(len(wins) / n * 100, 1),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy": round(mean, 2),
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else None,
        "best": round(max(results), 2), "worst": round(min(results), 2),
    })
    if out["avg_win"] is not None and out["avg_loss"]:
        out["payoff"] = round(out["avg_win"] / -out["avg_loss"], 2)
    if n >= 2:
        sd = math.sqrt(sum((p - mean) ** 2 for p in results) / (n - 1))
        half = 1.96 * sd / math.sqrt(n)
        out["range95"] = [round(mean - half, 2), round(mean + half, 2)]
        out["edge"] = ("unclear" if mean - half <= 0 <= mean + half else "positive" if mean - half > 0 else "negative")
    streak = worst_streak = 0
    for p in results:
        streak = streak + 1 if p <= 0 else 0
        worst_streak = max(worst_streak, streak)
    out["losing_streak"] = worst_streak
    running = peak = drawdown = 0.0
    for p in results:
        running += p
        peak = max(peak, running)
        drawdown = min(drawdown, running - peak)
    out["max_drawdown"] = round(drawdown, 2)
    rs = [t["r"] for t in trades if t.get("r") is not None]
    if rs:
        out["r_trades"] = len(rs)
        out["expectancy_r"] = round(sum(rs) / len(rs), 2)
    held = [t["days"] for t in trades if t.get("days") is not None]
    if held:
        out["avg_days"] = round(sum(held) / len(held), 1)
    return out


def _hour(trade: dict) -> str | None:
    opened = _when(trade["opened_at"])
    if not opened:
        return None
    return f"{opened.hour:02d}:00 to {opened.hour + 1:02d}:00"


def _hold(trade: dict) -> str | None:
    days = trade.get("days")
    if days is None:
        return None
    return "Same day" if days == 0 else "1 to 5 days" if days <= 5 else "6 to 30 days" if days <= 30 else "Over 30 days"


GROUPS = {
    "setup": ("Setup", lambda t: (t["note"] or {}).get("setup") or "No setup written"),
    "source": ("Placed by", lambda t: t["source"]),
    "hour": ("Hour bought (IST)", _hour),
    "weekday": ("Day bought", lambda t: WEEKDAYS[_when(t["opened_at"]).weekday()] if _when(t["opened_at"]) else None),
    "hold": ("Held for", _hold),
    "side": ("Long or short", lambda t: "Long (bought first)" if t["side"] == "LONG" else "Short (sold first)"),
    "plan": ("Followed the plan", lambda t: {"yes": "Yes", "no": "No"}.get((t["note"] or {}).get("followed_plan"),
                                                                        "Not reviewed")),
}


def breakdown(trades: list[dict], group: str) -> list[dict]:
    if group == "mistake":
        buckets: dict[str, list] = defaultdict(list)
        for t in trades:
            for m in (t["note"] or {}).get("mistakes") or ["No mistake ticked"]:
                buckets[m].append(t)
    else:
        if group not in GROUPS:
            raise ValueError("group must be one of " + ", ".join([*GROUPS, "mistake"]))
        pick = GROUPS[group][1]
        buckets = defaultdict(list)
        for t in trades:
            name = pick(t)
            if name is not None:
                buckets[name].append(t)
    rows = [{"name": name, **summarize(items)} for name, items in buckets.items()]
    if group == "weekday":
        rows.sort(key=lambda r: WEEKDAYS.index(r["name"]))
    elif group == "hour":
        rows.sort(key=lambda r: r["name"])
    else:
        rows.sort(key=lambda r: -r["trades"])
    return rows


def report(trades: list[dict]) -> dict:
    closed = [t for t in trades if t["status"] == "Closed"]
    return {"overall": summarize(closed), "min_trades": MIN_TRADES,
            "groups": {key: label for key, (label, _) in GROUPS.items()} | {"mistake": "Mistake"},
            "by": {key: breakdown(closed, key) for key in [*GROUPS, "mistake"]}}
