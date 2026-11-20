"""Write the learning notes from the template and the numbers the figures computed.

``desk/dev/notes_template.md`` holds the text with ``{{NAME}}`` placeholders; ``notes-stats.json`` (written by
``notes_figures --data``) holds every number. The result is ``desk/static/notes/LEARNING_NOTES.md``. The prose
around some numbers describes this data snapshot (which example failed, which level held): after a rebuild on
newer data, read the chapters again before publishing.
"""

from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "notes_template.md"
STATIC = HERE.parent / "static" / "notes"
OUTPUT = STATIC / "LEARNING_NOTES.md"


def _d(text: str) -> str:
    return date.fromisoformat(text).strftime("%-d %B %Y")


def _pct(value: float, places: int = 1) -> str:
    return f"{value:.{places}f}%".replace("-", "−")


def _signed(value: float) -> str:
    return f"{value:+.2f}%".replace("-", "−")


def _rs(value: float) -> str:
    """Indian digit grouping: 16,77,039."""
    whole = f"{round(value):d}"
    if len(whole) <= 3:
        return whole
    head, tail = whole[:-3], whole[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def _table(header: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def values(s: dict) -> dict:
    c, cp, bt, lots = s["candles"], s["chart_patterns"], s["backtest_nifty"], s["lots"]
    base = c["baseline_all"]
    order = ["Hammer", "Bullish engulfing", "Morning star", "Doji", "Shooting star", "Bearish engulfing", "Evening star"]
    candle_rows = [[name, f"{c['patterns'][name]['count']:,}", _pct(c["patterns"][name]["up_after_10d_pct"]),
                    _signed(c["patterns"][name]["avg_10d_pct"])] for name in order]
    candle_rows.append(["*Any day (baseline)*", f"{base['count']:,}", _pct(base["up_pct"]), _signed(base["avg_pct"])])
    pattern_rows = []
    for kind, label in (("Double top", "lower"), ("Double bottom", "higher"), ("Head and shoulders", "lower")):
        r = cp[kind]
        pattern_rows.append([kind, str(r["count"]), _pct(r["target_before_invalidation_pct"]),
                             _pct(r["invalidated_first_pct"]), f"{_pct(r['moved_pattern_way_after_20d_pct'])} {label}",
                             _signed(r["avg_20d_pct"])])
    bt_rows = [[r["strategy"], _pct(r["cagr_pct"], 2), _pct(r["max_drawdown_pct"]), str(r["trades"]),
                "–" if r["win_rate_pct"] is None else _pct(r["win_rate_pct"]), _pct(r["exposure_pct"]),
                f"₹{_rs(r['charges'])}", f"₹{_rs(r['final_value'])}"] for r in bt["rows"]]
    stock_rows = [[label, _pct(r["median_cagr_pct"], 2), _pct(r["median_max_drawdown_pct"]),
                   "–" if label == "Buy and hold" else _pct(r["beat_buy_hold_pct"]), _pct(r["lost_money_pct"])]
                  for label, r in s["backtest_stocks"]["rows"].items()]
    beats = [r["beat_buy_hold_pct"] for label, r in s["backtest_stocks"]["rows"].items() if label != "Buy and hold"]
    bears = [c["patterns"][n]["up_after_10d_pct"] for n in ("Shooting star", "Bearish engulfing", "Evening star")]
    d, vix, ind, sr, srb = s["daily_moves"], s["vix"], s["indicators_latest"], s["sr"], s["sr_break"]
    nifty, bank = s["closes"]["^NSEI"], s["closes"]["^NSEBANK"]
    nlot, blot = lots["NIFTY"], lots["BANKNIFTY"]
    worst_rs = abs(d["worst"]) / 100 * nifty * nlot
    dd = s["nifty_drawdowns_over_20pct"]
    dd_lines = ["Every fall of more than 20% from a peak since the Nifty 50 data begins (September 2007):", "",
                _table(["Peak", "Lowest close", "Fall", "Back at the old peak"],
                       [[_d(e["peak"]), _d(e["trough"]), _pct(e["fall_pct"]),
                         _d(e["recovered"]) if e["recovered"] else "not yet"] for e in dd]),
                "", "A buyer at the January 2008 peak waited almost three years to be level again, before costs "
                    "and inflation. Size every position so that a fall like these does not end your trading."]
    atr = ind["atr14"]
    return {
        "DATA_LAST": _d(s["data"]["last"]),
        "CANDLE_UNIVERSE": "the current Nifty 50 stocks",
        "CANDLE_TABLE": _table(["Pattern", "Times found", "Higher 10 sessions later", "Average move"], candle_rows),
        "BASE_UP": _pct(base["up_pct"]), "BASE_AVG": _signed(base["avg_pct"]),
        "BASE_UP_FALL": _pct(c["baseline_after_3pct_fall"]["up_pct"]),
        "BASE_UP_RISE": _pct(c["baseline_after_3pct_rise"]["up_pct"]),
        "BEAR_RANGE": f"{min(bears):.0f}% to {max(bears):.0f}%",
        "HAMMER_UP": _pct(c["patterns"]["Hammer"]["up_after_10d_pct"]),
        "ENGULF_UP": _pct(c["patterns"]["Bullish engulfing"]["up_after_10d_pct"]),
        "MSTAR_UP": _pct(c["patterns"]["Morning star"]["up_after_10d_pct"]),
        "MSTAR_N": str(c["patterns"]["Morning star"]["count"]),
        "GOLDEN": ", ".join(_d(x) for x in s["ma_crosses"]["golden"]),
        "DEATH": ", ".join(_d(x) for x in s["ma_crosses"]["death"]),
        "ABOVE200": _pct(s["nifty_days_above_200dma_pct"]),
        "SR_HIGH": f"{sr['high']:,.0f}", "SR_HIGH_DATE": _d(sr["high_date"]), "SR_LOW": f"{sr['low']:,.0f}",
        "SR_FIRST_ABOVE": _d(srb["first_high_above"]), "SR_MAX": f"{srb['max_high']:,.0f}",
        "SR_MAX_DATE": _d(srb["max_high_date"]),
        "SR_CLOSES_ABOVE": "one day" if srb["closes_above"] == 1 else f"{srb['closes_above']} days",
        "LOW_A": f"{srb['low_2026_a'][1]:,.0f}", "LOW_A_DATE": _d(srb["low_2026_a"][0]),
        "LOW_B": f"{srb['low_2026_b'][1]:,.0f}", "LOW_B_DATE": _d(srb["low_2026_b"][0]),
        "PATTERN_TABLE": _table(["Pattern", "Confirmed", "Target reached first", "Failed first",
                                 "20 sessions later", "Average 20-session move"], pattern_rows),
        "DT_DOWN": _pct(cp["Double top"]["moved_pattern_way_after_20d_pct"]),
        "HS_DOWN": _pct(cp["Head and shoulders"]["moved_pattern_way_after_20d_pct"]),
        "RB_COUNT": f"{s['range_breakout']['count']:,}", "RB_UP": _pct(s["range_breakout"]["up_after_20d_pct"]),
        "RB_AVG": _signed(s["range_breakout"]["avg_20d_pct"]),
        "RSI70": _pct(s["rsi_extremes_since_2008"]["days_above_70_pct"]),
        "RSI30": _pct(s["rsi_extremes_since_2008"]["days_below_30_pct"]),
        "IND_DATE": _d(ind["date"]), "IND_RSI": f"{ind['rsi14']:.1f}", "IND_ATR": f"{atr:,.0f}",
        "IND_ADX": f"{ind['adx14']:.1f}",
        "ATR_STOP_LO": f"{1.5 * atr:,.0f}", "ATR_STOP_HI": f"{2 * atr:,.0f}",
        "ATR_STOP_LO_RS": _rs(1.5 * atr * nlot), "ATR_STOP_HI_RS": _rs(2 * atr * nlot),
        "BT_PERIOD": f"{_d(bt['period'].split(' to ')[0])} to {_d(bt['period'].split(' to ')[1])}",
        "BT_NIFTY_TABLE": _table(["Rule", "Yearly return", "Deepest fall", "Trades", "Winning trades", "Time in market",
                                  "Charges paid", "₹10 lakh became"], bt_rows),
        "BT_STOCKS_TABLE": _table(["Rule", "Median yearly return", "Median deepest fall", "Beat buy and hold",
                                   "Ended below the start"], stock_rows),
        "BEAT_RANGE": f"{min(beats):.0f}% to {max(beats):.0f}%",
        "NIFTY_CLOSE": f"{nifty:,.2f}", "BANKNIFTY_CLOSE": f"{bank:,.2f}",
        "NIFTY_LOT_VALUE": _rs(nifty * nlot), "BANKNIFTY_LOT_VALUE": _rs(bank * blot),
        "NIFTY_1PCT": _rs(nifty * nlot / 100), "HEDGE_LOTS": f"{3_000_000 / (nifty * nlot):.1f}",
        "D2": str(d["abs_over_2pct_since_2015"]), "D2_ALL": str(d["abs_over_2pct"]), "D3_ALL": str(d["abs_over_3pct"]),
        "D5": str(d["abs_over_5pct"]), "D_DAYS": f"{d['days']:,}",
        "WORST": f"{d['worst']:.2f}%".replace("-", "−"), "WORST_DATE": _d(d["worst_date"]), "WORST_RS": _rs(worst_rs),
        "VIX_LATEST": f"{vix['latest']:.2f}", "VIX_LATEST_DATE": _d(vix["latest_date"]),
        "VIX_MEDIAN": f"{vix['median_since_2015']:.2f}", "VIX_MAX": f"{vix['max']:.2f}",
        "VIX_MAX_DATE": _d(vix["max_date"]), "VIX_ABOVE": _pct(vix["vix_above_realized_pct_since_2015"]),
        "DD_TEXT": "\n".join(dd_lines),
    }


def write() -> Path:
    stats = json.loads((STATIC / "notes-stats.json").read_text(encoding="utf-8"))
    text = TEMPLATE.read_text(encoding="utf-8")
    for key, value in values(stats).items():
        text = text.replace("{{" + key + "}}", value)
    left = sorted(set(re.findall(r"\{\{[A-Z0-9_]+\}\}", text)))
    if left:
        raise ValueError(f"placeholders with no value: {', '.join(left)}")
    OUTPUT.write_text(text, encoding="utf-8")
    return OUTPUT


if __name__ == "__main__":
    print(f"Wrote {write()}")
