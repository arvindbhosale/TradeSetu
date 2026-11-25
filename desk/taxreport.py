"""Trade journal export and an indicative tax P&L, per Indian financial year (April to March).

Classification, for listed equity and F&O:

- Delivery (CNC) shares: matched first-in, first-out. Held more than 12 months:
  long-term capital gain; otherwise short-term.
- Shares bought and sold the same day (MIS, or CNC squared off the same day): speculative business income.
- Futures and options (NFO/BFO) and MCX gold and silver derivatives (CTT paid): non-speculative business
  income; turnover is reported separately.

Rates shown are those in force from 23 July 2024: short-term gains on listed
equity 20%, long-term 12.5% on gains above ₹1.25 lakh a year. Securities
transaction tax is not deductible from capital gains; other charges are.

Each lot carries its charges part by part (brokerage, STT, exchange and SEBI fees, stamp duty, GST, DP).
``statement()`` sets off losses under each head's rules and carries them forward (intraday 4 years, the others
8), with losses from earlier years typed in by the user.

This covers only trades this desk recorded, ignores grandfathering, corporate actions, surcharge, the basic
exemption and rebates. It is an estimate, not a tax computation: use your broker's tax P&L and a chartered
accountant.
"""

from __future__ import annotations

import csv
import io
import re
from collections import defaultdict, deque
from datetime import date, datetime

from .charges import PARTS

STCG_RATE, LTCG_RATE, LTCG_EXEMPTION = 0.20, 0.125, 125_000.0
JOURNAL_FIELDS = ["time_ist", "mode", "source", "exchange", "tradingsymbol", "side", "quantity", "product", "order_type",
                  "price", "fill_price", "variety", "order_id", "ok", "error", "gtt"]
PNL_FIELDS = ["time_ist", "source", "symbol", "side", "quantity", "buy_price", "sell_price", "pnl", "return_pct", "charges",
              "brokerage", "stt", "exchange_fees", "stamp_duty", "gst", "dp"]
FILL_FIELDS = ["time_ist", "mode", "source", "exchange", "tradingsymbol", "side", "quantity", "price", "product", "charges",
               "brokerage", "stt", "exchange_fees", "stamp_duty", "gst", "dp", "trade_id"]


def financial_year(day: str | date) -> str:
    stamp = day if isinstance(day, date) else datetime.strptime(str(day)[:10], "%Y-%m-%d").date()
    start = stamp.year if stamp.month >= 4 else stamp.year - 1
    return f"{start}-{str(start + 1)[2:]}"


def to_csv(rows: list[dict], fields: list[str]) -> str:
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: _cell(row.get(k, "")) for k in fields})
    return out.getvalue()


def _cell(value):
    """A text cell a spreadsheet would run as a formula (=, +, -, @) is written as text."""
    if isinstance(value, str) and value[:1] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + value
    return value


def _kind(fill: dict) -> str:
    if fill["exchange"] in ("NFO", "BFO", "MCX"):
        return "fno"
    return "intraday" if fill.get("product") == "MIS" else "delivery"


def tax_lots(fills: list[dict], mode: str) -> list[dict]:
    """Each closing fill matched to the open lots it closes, first in, first out.

    A sale with nothing held opens a short lot (F&O, or a share short sale marked ``short``) that a later purchase
    closes; any other sale of shares the desk never recorded buying is not counted, as on the P&L screen. A delivery
    sale closes shares bought the same day first, since the exchange squares those off as intraday.
    """
    from .pnl import round_parts, settle

    lots: dict[tuple, deque] = defaultdict(deque)
    out = []
    for fill in settle(sorted((f for f in fills if f.get("mode") == mode), key=lambda f: f["time_ist"])):
        kind = _kind(fill)
        key = (kind, fill["exchange"], fill["tradingsymbol"])
        per_unit_charge = (fill.get("charges") or 0) / fill["quantity"] if fill["quantity"] else 0
        per_unit_parts = {k: v / fill["quantity"] if fill["quantity"] else 0.0 for k, v in fill["parts"].items()}
        buying = fill["side"] == "BUY"
        book = lots[key]
        day = fill["time_ist"][:10]
        if not buying and kind == "delivery" and book and book[0]["long"]:
            # Same-day purchases are squared off first: move them to the front of the queue.
            today = [lot for lot in book if lot["time"][:10] == day]
            if today and len(today) < len(book):
                lots[key] = book = deque(today + [lot for lot in book if lot["time"][:10] != day])
        remaining = fill["quantity"]
        while remaining > 0 and book and book[0]["long"] != buying:
            lot = book[0]
            used = min(remaining, lot["quantity"])
            opened, closed = lot["time"][:10], day
            held = (datetime.strptime(closed, "%Y-%m-%d") - datetime.strptime(opened, "%Y-%m-%d")).days
            if kind == "fno":
                category = "F&O (non-speculative)"
            elif opened == closed:
                category = "Speculative (intraday)"
            else:
                category = "LTCG" if held > 365 else "STCG"
            factor = fill.get("multiplier") or 1          # MCX: quantity in lots, rupees = price x lots x multiplier
            buy, sell = (fill, lot) if buying else (lot, fill)
            buy_price, sell_price = buy["price"], sell["price"]
            cost, value = used * buy_price * factor, used * sell_price * factor
            charges = used * (lot["charge"] + per_unit_charge)
            parts = round_parts({k: used * (lot["parts"][k] + per_unit_parts[k]) for k in PARTS}, charges)
            gain = value - cost - charges
            # Securities transaction tax is not a deductible cost of a capital gain; it is of business income.
            taxable = gain + parts["stt"] if category in ("STCG", "LTCG") else gain
            out.append({"fy": financial_year(closed), "category": category, "symbol": fill["tradingsymbol"],
                        "exchange": fill["exchange"], "side": "Long" if lot["long"] else "Short", "quantity": used,
                        "buy_date": buy["time"][:10] if buy is lot else closed,
                        "sell_date": sell["time"][:10] if sell is lot else closed,
                        "days_held": held, "buy_price": round(buy_price, 2), "sell_price": round(sell_price, 2),
                        "buy_value": round(cost, 2), "sell_value": round(value, 2), **parts, "charges": round(charges, 2),
                        "gain": round(gain, 2), "taxable_gain": round(taxable, 2), "source": lot["source"],
                        **({"closed_by": fill.get("source", "manual")} if fill.get("source", "manual") != lot["source"] else {})})
            lot["quantity"] -= used
            remaining -= used
            if lot["quantity"] == 0:
                book.popleft()
        if remaining > 0 and (buying or kind == "fno" or fill.get("short")):
            book.append({"time": fill["time_ist"], "quantity": remaining, "price": fill["price"],
                         "charge": per_unit_charge, "parts": per_unit_parts, "long": buying, "source": fill.get("source", "manual")})
    return out


HEADS = {"Speculative (intraday)": "spec", "F&O (non-speculative)": "fno", "STCG": "stcg", "LTCG": "ltcg"}
HEAD_NAMES = {"spec": "Intraday shares (speculative)", "fno": "F&O (business)", "stcg": "Short-term capital loss",
              "ltcg": "Long-term capital loss"}
LOSS_LIFE = {"spec": 4, "fno": 8, "stcg": 8, "ltcg": 8}      # years a loss can be carried forward
AUDIT_LIMIT = 100_000_000.0                                  # ₹10 crore: trading paid through a bank account
CESS = 0.04                                                  # health and education cess on the tax
DEFAULT_SLAB = 0.30


def rates(fy: str) -> tuple[float, float, float]:
    """Short-term rate, long-term rate and the long-term exemption for a financial year. From 23 July 2024: 20%,
    12.5% and ₹1.25 lakh; before that 15%, 10% and ₹1 lakh. FY 2024-25 is shown at the new rates."""
    return (STCG_RATE, LTCG_RATE, LTCG_EXEMPTION) if int(fy[:4]) >= 2024 else (0.15, 0.10, 100_000.0)


def _turnover(lot: dict) -> float:
    return abs(lot["sell_value"] - lot["buy_value"])           # the sum of absolute profits and losses


def summary(lots: list[dict]) -> list[dict]:
    """Totals per financial year: the gain under each tax head, the charges part by part and the turnover."""
    years: dict[str, dict] = {}
    for lot in lots:
        year = years.setdefault(lot["fy"], {"fy": lot["fy"], "STCG": 0.0, "LTCG": 0.0, "Speculative (intraday)": 0.0,
                                            "F&O (non-speculative)": 0.0, "fno_turnover": 0.0, "intraday_turnover": 0.0,
                                            **dict.fromkeys(PARTS, 0.0), "charges": 0.0, "trades": 0})
        capital = lot["category"] in ("STCG", "LTCG")
        year[lot["category"]] += lot.get("taxable_gain", lot["gain"]) if capital else lot["gain"]
        year["charges"] += lot["charges"]
        for k in PARTS:
            year[k] += lot.get(k) or 0.0
        year["trades"] += 1
        if lot["category"] == "F&O (non-speculative)":
            year["fno_turnover"] += _turnover(lot)        # futures and options: sum of absolute profits and losses
        elif lot["category"] == "Speculative (intraday)":
            year["intraday_turnover"] += _turnover(lot)
    out = []
    for fy in sorted(years):
        year = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in years[fy].items()}
        stcg_rate, ltcg_rate, exemption = rates(fy)
        stcg, ltcg = year["STCG"], year["LTCG"]
        year["stcg_tax_estimate"] = round(max(stcg, 0) * stcg_rate, 2)
        year["ltcg_taxable"] = round(max(ltcg - exemption, 0), 2)
        year["ltcg_tax_estimate"] = round(year["ltcg_taxable"] * ltcg_rate, 2)
        out.append(year)
    return out


# -- tax statement: set-off and carry-forward of losses ------------------------------------------------------

_FY = re.compile(r"(\d{4})-(\d{2})")


def _fy_start(fy: str) -> int:
    match = _FY.fullmatch(str(fy).strip())
    if not match or (int(match[1]) + 1) % 100 != int(match[2]):
        raise ValueError(f"financial year must look like 2024-25, got {fy!r}")
    return int(match[1])


def _fy_name(start: int) -> str:
    return f"{start}-{str(start + 1)[2:]}"


def clean_carried(rows) -> list[dict]:
    """Losses from earlier years, as typed in: [{fy, head, amount}], the year the loss was made, its tax head
    (spec, fno, stcg, ltcg) and the amount still to be set off. Raises ValueError on a bad row."""
    if not isinstance(rows, list):
        raise ValueError("losses must be a list")
    if len(rows) > 100:
        raise ValueError("at most 100 rows of earlier losses")
    out = []
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("each loss needs a year, a tax head and an amount")
        head = str(row.get("head", ""))
        if head not in LOSS_LIFE:
            raise ValueError(f"tax head must be one of {', '.join(LOSS_LIFE)}")
        try:
            amount = float(row.get("amount"))
        except (TypeError, ValueError):
            raise ValueError("amount must be a number") from None
        if not 0 < amount < 1e12:
            raise ValueError("amount must be above zero (type the loss as a positive number)")
        start = _fy_start(row.get("fy", ""))
        if not 2000 <= start <= 2100:
            raise ValueError("financial year out of range")
        out.append({"fy": _fy_name(start), "head": head, "amount": round(amount, 2)})
    return out


def statement(lots: list[dict], carried: list[dict] | None = None, slab_rate: float = DEFAULT_SLAB) -> dict:
    """An estimated tax statement for each financial year: gains by tax head, losses set off this year and from
    earlier years under each head's rules, taxable income, the tax on it and the losses carried forward.

    Rules (Income-tax, for an individual):
    - A speculative (intraday shares) loss is set off only against speculative income, this year or the next 4.
    - An F&O (non-speculative business) loss is set off this year against intraday profit, then capital gains (and
      other income except salary, which the desk does not see); carried forward 8 years against business income.
    - A short-term capital loss is set off against short- or long-term gains; a long-term loss only against
      long-term gains; both carried forward 8 years. Capital losses never reduce business income.
    - This year's losses are set off before losses from earlier years; earlier losses are used oldest first.
    - A loss is carried forward only if the return for that year was filed by its due date.
    """
    carried = clean_carried(carried or [])
    years = {lot["fy"] for lot in lots}
    gross = {y["fy"]: y for y in summary(lots)}
    starts = {_fy_start(fy) for fy in years}
    notes = []
    for row in carried:
        if _fy_start(row["fy"]) in starts:
            notes.append(f"You entered a loss for FY {row['fy']}, a year with trades on this desk too. Both are "
                         "counted: enter only losses from trades this desk did not record.")
    if not starts and not carried:
        return {"years": [], "losses": [], "notes": notes}
    first = min(starts | {_fy_start(r["fy"]) + 1 for r in carried})
    last = max(starts) if starts else max(_fy_start(r["fy"]) + 1 for r in carried)
    pool = [{"origin": _fy_start(r["fy"]), "head": r["head"], "amount": r["amount"], "left": r["amount"],
             "used": 0.0, "expired": None, "entered": True} for r in carried]
    pool.sort(key=lambda e: e["origin"])
    for entry in pool:
        if entry["origin"] + LOSS_LIFE[entry["head"]] < first:
            entry["expired"], entry["left"] = _fy_name(entry["origin"] + LOSS_LIFE[entry["head"]]), 0.0

    def use(head: str, year: int, income: float) -> float:
        """Set off earlier losses of one head against ``income``, oldest first. Returns the amount used."""
        used = 0.0
        for entry in pool:
            if income - used <= 0:
                break
            if entry["head"] != head or entry["left"] <= 0 or not entry["origin"] < year <= entry["origin"] + LOSS_LIFE[head]:
                continue
            take = min(entry["left"], income - used)
            entry["left"] -= take
            entry["used"] += take
            used += take
        return used

    out = []
    for year in range(first, last + 1):
        fy = _fy_name(year)
        g = gross.get(fy, {})
        spec, fno = g.get("Speculative (intraday)", 0.0), g.get("F&O (non-speculative)", 0.0)
        stcg, ltcg = g.get("STCG", 0.0), g.get("LTCG", 0.0)
        row = {"fy": fy, "spec": spec, "fno": fno, "stcg": stcg, "ltcg": ltcg}
        # This year's losses, within a head first, then across heads.
        take = min(-fno, spec) if fno < 0 < spec else 0.0
        spec, fno, row["fno_vs_spec"] = spec - take, fno + take, take
        take = min(-stcg, ltcg) if stcg < 0 < ltcg else 0.0
        ltcg, stcg, row["stcl_vs_ltcg"] = ltcg - take, stcg + take, take
        take = min(-fno, stcg) if fno < 0 < stcg else 0.0
        stcg, fno, row["fno_vs_stcg"] = stcg - take, fno + take, take
        take = min(-fno, ltcg) if fno < 0 < ltcg else 0.0
        ltcg, fno, row["fno_vs_ltcg"] = ltcg - take, fno + take, take
        # Losses from earlier years.
        row["bf_spec"] = use("spec", year, max(spec, 0))
        spec -= row["bf_spec"]
        row["bf_fno"] = use("fno", year, max(fno, 0) + max(spec, 0))
        from_fno = min(row["bf_fno"], max(fno, 0))
        fno, spec = fno - from_fno, spec - (row["bf_fno"] - from_fno)
        row["bf_stcl_stcg"] = use("stcg", year, max(stcg, 0))
        stcg -= row["bf_stcl_stcg"]
        row["bf_ltcl"] = use("ltcg", year, max(ltcg, 0))
        ltcg -= row["bf_ltcl"]
        row["bf_stcl_ltcg"] = use("stcg", year, max(ltcg, 0))
        ltcg -= row["bf_stcl_ltcg"]
        row["bf_stcl"] = row.pop("bf_stcl_stcg") + row.pop("bf_stcl_ltcg")
        # This year's losses left over are carried forward.
        for head, value in (("spec", spec), ("fno", fno), ("stcg", stcg), ("ltcg", ltcg)):
            if value < 0:
                pool.append({"origin": year, "head": head, "amount": -value, "left": -value, "used": 0.0,
                             "expired": None, "entered": False})
        stcg_rate, ltcg_rate, exemption = rates(fy)
        business = max(spec, 0) + max(fno, 0)
        stcg_taxable, ltcg_after = max(stcg, 0), max(ltcg, 0)
        ltcg_taxable = max(ltcg_after - exemption, 0)
        tax = {"business": business * slab_rate, "stcg": stcg_taxable * stcg_rate, "ltcg": ltcg_taxable * ltcg_rate}
        expired = 0.0
        for entry in pool:
            if entry["left"] > 0 and entry["origin"] + LOSS_LIFE[entry["head"]] == year:
                expired += entry["left"]
                entry["expired"], entry["left"] = fy, 0.0
        row.update({
            "spec_after": spec, "fno_after": fno, "stcg_after": stcg, "ltcg_after": ltcg,
            "business_income": business, "stcg_taxable": stcg_taxable, "ltcg_exemption": min(ltcg_after, exemption),
            "ltcg_taxable": ltcg_taxable, "slab_rate_pct": slab_rate * 100, "stcg_rate_pct": stcg_rate * 100,
            "ltcg_rate_pct": ltcg_rate * 100, "business_tax": tax["business"], "stcg_tax": tax["stcg"],
            "ltcg_tax": tax["ltcg"], "cess": sum(tax.values()) * CESS, "total_tax": sum(tax.values()) * (1 + CESS),
            **{f"cf_{head}": sum(e["left"] for e in pool if e["head"] == head and e["origin"] <= year
                                 and year < e["origin"] + LOSS_LIFE[head]) for head in LOSS_LIFE},
            "expired": expired,
            "intraday_turnover": g.get("intraday_turnover", 0.0), "fno_turnover": g.get("fno_turnover", 0.0),
            "stt": g.get("stt", 0.0), "charges": g.get("charges", 0.0)})
        row["turnover"] = row["intraday_turnover"] + row["fno_turnover"]
        row["audit"] = row["turnover"] > AUDIT_LIMIT
        quiet = fy not in years and not (expired or row["bf_spec"] or row["bf_fno"] or row["bf_stcl"] or row["bf_ltcl"])
        if not quiet:              # a year with no trades and no loss used or lapsed is left out
            out.append({k: v if isinstance(v, (bool, str)) else round(float(v), 2) for k, v in row.items()})
    losses = [{"fy": _fy_name(e["origin"]), "head": e["head"], "head_name": HEAD_NAMES[e["head"]],
               "from": "You entered" if e["entered"] else "This desk", "amount": round(e["amount"], 2),
               "used": round(e["used"], 2), "left": round(e["left"], 2),
               "last_year": _fy_name(e["origin"] + LOSS_LIFE[e["head"]]),
               "status": f"Lapsed after FY {e['expired']}" if e["expired"] else ("Used up" if e["left"] <= 0.005 else "Carried forward")}
              for e in sorted(pool, key=lambda e: (e["origin"], e["head"]))]
    if any(r["audit"] for r in out):
        notes.append("Turnover is above ₹10 crore in a year: a tax audit is needed. Ask your chartered accountant.")
    return {"years": out, "losses": losses, "notes": notes, "slab_rate_pct": round(slab_rate * 100, 2)}


# The tax statement: one column per financial year, one line per step. (label, key, kind); a None key is a heading.
STATEMENT = [
    ("Gains and losses this year (after charges)", None, None),
    ("Intraday shares (speculative business)", "spec", "money"),
    ("F&O (non-speculative business)", "fno", "money"),
    ("Short-term capital gains (STT added back)", "stcg", "money"),
    ("Long-term capital gains (STT added back)", "ltcg", "money"),
    ("Set off this year", None, None),
    ("F&O loss set off against intraday profit", "fno_vs_spec", "money"),
    ("Short-term loss set off against long-term gains", "stcl_vs_ltcg", "money"),
    ("F&O loss set off against short-term gains", "fno_vs_stcg", "money"),
    ("F&O loss set off against long-term gains", "fno_vs_ltcg", "money"),
    ("Losses from earlier years set off", None, None),
    ("Intraday losses (against intraday profit)", "bf_spec", "money"),
    ("F&O losses (against F&O and intraday profit)", "bf_fno", "money"),
    ("Short-term capital losses (against short- or long-term gains)", "bf_stcl", "money"),
    ("Long-term capital losses (against long-term gains)", "bf_ltcl", "money"),
    ("After set-off (below zero: a loss carried forward)", None, None),
    ("Intraday shares", "spec_after", "money"),
    ("F&O", "fno_after", "money"),
    ("Short-term capital gains", "stcg_after", "money"),
    ("Long-term capital gains", "ltcg_after", "money"),
    ("Taxable income", None, None),
    ("Business income (intraday + F&O), taxed at your slab rate", "business_income", "money"),
    ("Short-term capital gains", "stcg_taxable", "money"),
    ("Long-term gains exempt (first ₹1.25 lakh)", "ltcg_exemption", "money"),
    ("Long-term capital gains taxable", "ltcg_taxable", "money"),
    ("Estimated tax", None, None),
    ("Your slab rate on business income", "slab_rate_pct", "pct"),
    ("Tax on business income", "business_tax", "money"),
    ("Short-term rate", "stcg_rate_pct", "pct"),
    ("Tax on short-term gains", "stcg_tax", "money"),
    ("Long-term rate", "ltcg_rate_pct", "pct"),
    ("Tax on long-term gains", "ltcg_tax", "money"),
    ("Health and education cess (4%)", "cess", "money"),
    ("Total estimated tax", "total_tax", "money"),
    ("Losses carried to next year", None, None),
    ("Intraday losses (kept 4 years)", "cf_spec", "money"),
    ("F&O losses (kept 8 years)", "cf_fno", "money"),
    ("Short-term capital losses (kept 8 years)", "cf_stcg", "money"),
    ("Long-term capital losses (kept 8 years)", "cf_ltcg", "money"),
    ("Losses that lapsed this year (too old)", "expired", "money"),
    ("Turnover and tax audit", None, None),
    ("Intraday turnover (sum of profits and losses)", "intraday_turnover", "money"),
    ("F&O turnover (sum of profits and losses)", "fno_turnover", "money"),
    ("Total business turnover", "turnover", "money"),
    ("Tax audit needed? (above ₹10 crore)", "audit", "yes"),
    ("STT paid (in the charges above)", "stt", "money"),
]
STATEMENT_NOTES = [
    "An estimate, not a tax computation. Check with your broker's tax P&L and a chartered accountant before filing.",
    "Only trades this desk recorded, plus the earlier losses you entered under Tax statement on the P&L tab.",
    "Business income is shown at one slab rate; your real tax depends on your other income, the regime you choose, "
    "the basic exemption, rebates and surcharge, none of which this sheet sees.",
    "Rates: short-term 20% and long-term 12.5% above ₹1.25 lakh from 23 July 2024 (FY 2024-25 is shown at these "
    "rates); 15% and 10% above ₹1 lakh before that.",
    "An F&O loss left after set-off here can also be set off this year against other income except salary (for "
    "example interest or rent), which this sheet does not see.",
    "Losses are carried forward only if that year's return was filed by its due date.",
    "Turnover for trading is the sum of absolute profits and losses on each trade. The audit limit is ₹10 crore when "
    "almost all receipts and payments are through a bank, as they are for trading; other cases can also need an "
    "audit, so ask your chartered accountant.",
]


LOT_FIELDS = ["fy", "category", "symbol", "exchange", "side", "quantity", "buy_date", "sell_date", "days_held", "buy_price",
              "sell_price", "buy_value", "sell_value", "brokerage", "stt", "exchange_fees", "stamp_duty", "gst", "dp",
              "charges", "gain", "taxable_gain", "source"]


# -- the user's inputs: losses from earlier years and the slab rate ----------------------------------------------

def load_inputs(path) -> dict:
    """{losses, slab_rate_pct} saved by the tax screen; defaults when nothing is saved or the file is unreadable."""
    import json
    from pathlib import Path
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return clean_inputs(raw)
    except (OSError, ValueError, TypeError, AttributeError):
        return {"losses": [], "slab_rate_pct": DEFAULT_SLAB * 100}


def clean_inputs(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("expected {losses, slab_rate_pct}")
    try:
        slab = float(raw.get("slab_rate_pct", DEFAULT_SLAB * 100))
    except (TypeError, ValueError):
        raise ValueError("tax slab rate must be a number") from None
    if not 0 <= slab <= 50:
        raise ValueError("tax slab rate must be between 0% and 50%")
    return {"losses": clean_carried(raw.get("losses") or []), "slab_rate_pct": round(slab, 2)}


def save_inputs(path, raw: dict) -> dict:
    import json
    import os
    from pathlib import Path
    clean = clean_inputs(raw)
    path = Path(path)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(clean, indent=1), encoding="utf-8")
    os.replace(temp, path)
    return clean
