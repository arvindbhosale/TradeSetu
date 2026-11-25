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

This covers only trades this desk recorded, ignores grandfathering, corporate
actions, losses carried forward and surcharge. It is a working aid, not a tax
computation: use Kite Console's tax P&L and a chartered accountant.
"""

from __future__ import annotations

import csv
import io
from collections import defaultdict, deque
from datetime import date, datetime

STCG_RATE, LTCG_RATE, LTCG_EXEMPTION = 0.20, 0.125, 125_000.0
JOURNAL_FIELDS = ["time_ist", "mode", "source", "exchange", "tradingsymbol", "side", "quantity", "product", "order_type",
                  "price", "fill_price", "variety", "order_id", "ok", "error", "gtt"]
PNL_FIELDS = ["time_ist", "source", "symbol", "side", "quantity", "buy_price", "sell_price", "pnl", "return_pct", "charges"]
FILL_FIELDS = ["time_ist", "mode", "source", "exchange", "tradingsymbol", "side", "quantity", "price", "product", "charges", "trade_id"]


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
    from .pnl import settle

    lots: dict[tuple, deque] = defaultdict(deque)
    out = []
    for fill in settle(sorted((f for f in fills if f.get("mode") == mode), key=lambda f: f["time_ist"])):
        kind = _kind(fill)
        key = (kind, fill["exchange"], fill["tradingsymbol"])
        per_unit_charge = (fill.get("charges") or 0) / fill["quantity"] if fill["quantity"] else 0
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
            out.append({"fy": financial_year(closed), "category": category, "symbol": fill["tradingsymbol"],
                        "exchange": fill["exchange"], "side": "Long" if lot["long"] else "Short", "quantity": used,
                        "buy_date": buy["time"][:10] if buy is lot else closed,
                        "sell_date": sell["time"][:10] if sell is lot else closed,
                        "days_held": held, "buy_price": round(buy_price, 2), "sell_price": round(sell_price, 2),
                        "buy_value": round(cost, 2), "sell_value": round(value, 2), "charges": round(charges, 2),
                        "gain": round(value - cost - charges, 2), "source": lot["source"],
                        **({"closed_by": fill.get("source", "manual")} if fill.get("source", "manual") != lot["source"] else {})})
            lot["quantity"] -= used
            remaining -= used
            if lot["quantity"] == 0:
                book.popleft()
        if remaining > 0 and (buying or kind == "fno" or fill.get("short")):
            book.append({"time": fill["time_ist"], "quantity": remaining, "price": fill["price"],
                         "charge": per_unit_charge, "long": buying, "source": fill.get("source", "manual")})
    return out


def summary(lots: list[dict]) -> list[dict]:
    years: dict[str, dict] = {}
    for lot in lots:
        year = years.setdefault(lot["fy"], {"fy": lot["fy"], "STCG": 0.0, "LTCG": 0.0, "Speculative (intraday)": 0.0,
                                            "F&O (non-speculative)": 0.0, "fno_turnover": 0.0, "charges": 0.0, "trades": 0})
        year[lot["category"]] += lot["gain"]
        year["charges"] += lot["charges"]
        year["trades"] += 1
        if lot["category"] == "F&O (non-speculative)":
            year["fno_turnover"] += abs(lot["sell_value"] - lot["buy_value"])     # options: sum of absolute profits
    out = []
    for fy in sorted(years):
        year = {k: (round(v, 2) if isinstance(v, float) else v) for k, v in years[fy].items()}
        stcg, ltcg = year["STCG"], year["LTCG"]
        year["stcg_tax_estimate"] = round(max(stcg, 0) * STCG_RATE, 2)
        year["ltcg_taxable"] = round(max(ltcg - LTCG_EXEMPTION, 0), 2)
        year["ltcg_tax_estimate"] = round(year["ltcg_taxable"] * LTCG_RATE, 2)
        out.append(year)
    return out


LOT_FIELDS = ["fy", "category", "symbol", "exchange", "side", "quantity", "buy_date", "sell_date", "days_held", "buy_price",
              "sell_price", "buy_value", "sell_value", "charges", "gain", "source"]
