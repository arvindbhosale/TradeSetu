"""Company financial statements from the exchange filings: quarterly results, profit and loss,
balance sheet, cash flow, ratios and peers, plus the latest results for Results Drift.

Source: each company's quarterly results XBRL filed with NSE (SEBI LODR Regulation 33),
listed by NSE's ``corporates-financial-results`` (to 2025) and ``integrated-filing-results``
(from 2025) and read with :mod:`desk.xbrl`. What the filings give:

- every quarter: the quarter's profit and loss (and the year to date),
- Q2 and Q4: the balance sheet (September and March) and the cash flow for six and twelve months,
- Q4: the full year's profit and loss.

So annual figures are the Q4 filing's twelve months, the balance sheet is half-yearly and
"TTM" is the sum of the last four quarters. Ratios are worked out here from those numbers
(the filings carry no ROE or ROCE). Banks file a different format, shown with bank lines
(interest earned, deposits, advances, NPAs).

Results Drift needs only the latest quarter against the same quarter a year before. That
comes from NSE's ``results-comparision`` (one request per company); when it has no year-ago
quarter, the two filings are read instead.

Filings are downloaded once, one request a second, and kept on disk per company; a company
seen for the first time takes about half a minute. Only NSE-listed companies are covered.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import time
from collections import deque
from datetime import date, datetime
from pathlib import Path

from tradingagents.dataflows.symbols import normalize_symbol

from . import xbrl
from .nse import NseClient
from .settings import home

logger = logging.getLogger(__name__)

CR = 1e7                                       # rupees in a crore
QUARTERS, YEARS = 12, 10                       # columns kept
LISTING_SECONDS = 12 * 3600                    # re-read a company's list of filings after this
COMPARE_RETRY_SECONDS = 6 * 3600               # results not yet out: ask again after this
RETRY_SECONDS = 10 * 60                        # after a failed download, wait this long before trying again
FIRST_YEAR = 2015                              # older filings use other formats
MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


# -- small helpers -------------------------------------------------------------------------------

def nse_symbol(symbol: str) -> str | None:
    """``RELIANCE`` for ``RELIANCE.NS`` / ``NSE:RELIANCE``; None for a BSE-only listing."""
    canonical = normalize_symbol(symbol)
    if canonical.endswith(".BO"):
        return None
    return canonical.removesuffix(".NS").upper() or None


def day_of(value) -> str | None:
    """ISO date from NSE's spellings: 31-Dec-2024, 31-DEC-2024, 31-12-2024, 2024-12-31, 31 Dec 2024."""
    text = str(value or "").strip()
    if not text:
        return None
    head = text.split()[0] if re.match(r"\d{1,2}-", text) or re.match(r"\d{4}-", text) else " ".join(text.split()[:3])
    for fmt in ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d %b %Y", "%d-%B-%Y"):
        with contextlib.suppress(ValueError):
            return datetime.strptime(head, fmt).strftime("%Y-%m-%d")
    return None


def when_of(value) -> str:
    """A sortable timestamp from a filing's broadcast time (``16-Jan-2025 20:20:21``); '' when unknown."""
    text = str(value or "").strip()
    day = day_of(text)
    if not day:
        return ""
    clock = re.search(r"\d{1,2}:\d{2}(:\d{2})?", text)
    return f"{day} {clock.group(0) if clock else '00:00'}"


def label(end: str) -> str:
    """``Dec 2025`` for 2025-12-31."""
    d = date.fromisoformat(end)
    return f"{MONTHS[d.month - 1]} {d.year}"


def months_between(start: str, end: str) -> int:
    a, b = date.fromisoformat(start), date.fromisoformat(end)
    return (b.year - a.year) * 12 + b.month - a.month + 1


def _get(values: dict, *names):
    for name in names:
        value = values.get(name)
        if value is not None:
            return value
    return None


def _sum(values: dict, *names):
    found = [values[n] for n in names if values.get(n) is not None]
    return sum(found) if found else None


def _sub(a, *others):
    if a is None:
        return None
    return a - sum(o for o in others if o is not None)


def pct(part, whole):
    if part is None or not whole:
        return None
    return part / whole * 100


def growth(now, before):
    """Percent change; None when the base is missing, zero or a loss (growth on a loss means nothing)."""
    if now is None or before is None or before <= 0:
        return None
    return (now / before - 1) * 100


# -- one period's statements from a filing's values (rupees) -------------------------------------

def profit_loss(values: dict, bank: bool) -> dict:
    if bank:
        revenue, interest = _get(values, "InterestEarned"), _get(values, "InterestExpended")
        expenses, provisions = _get(values, "OperatingExpenses"), _get(values, "ProvisionsOtherThanTaxAndContingencies")
        financing = _sub(revenue, interest, expenses, provisions) if None not in (revenue, interest, expenses) else None
        net = _get(values, "ProfitLossForThePeriod", "ProfitLossForPeriod")
        return {
            "revenue": revenue, "interest": interest, "expenses": expenses, "provisions": provisions,
            "financing_profit": financing, "other_income": _get(values, "OtherIncome"),
            "exceptional": _get(values, "ExceptionalItems", "ExceptionalItemsBeforeTax"),
            "pbt": _get(values, "ProfitLossFromOrdinaryActivitiesBeforeTax", "ProfitBeforeTax"),
            "tax": _get(values, "TaxExpense"), "net_profit": net,
            "owners_profit": _get(values, "ProfitLossAfterTaxesMinorityInterestAndShareOfProfitLossOfAssociates") or net,
            "eps": _get(values, "BasicEarningsPerShareAfterExtraordinaryItems", "BasicEarningsPerShareBeforeExtraordinaryItems"),
            "gross_npa_pct": (lambda v: None if v is None else v * 100)(_get(values, "PercentageOfGrossNpa")),
            "net_npa_pct": (lambda v: None if v is None else v * 100)(_get(values, "PercentageOfNpa")),
            "paid_up": _get(values, "PaidUpValueOfEquityShareCapital"), "face": _get(values, "FaceValueOfEquityShareCapital"),
        }
    sales = _get(values, "RevenueFromOperations")
    finance, depreciation = _get(values, "FinanceCosts"), _get(values, "DepreciationDepletionAndAmortisationExpense")
    expenses = _sub(_get(values, "Expenses"), finance, depreciation)
    net = _get(values, "ProfitLossForPeriod", "ProfitLossForThePeriod")
    return {
        "sales": sales, "expenses": expenses,
        "operating_profit": None if sales is None or expenses is None else sales - expenses,
        "other_income": _get(values, "OtherIncome"), "interest": finance, "depreciation": depreciation,
        "exceptional": _get(values, "ExceptionalItemsBeforeTax", "ExceptionalItems"),
        "pbt": _get(values, "ProfitBeforeTax"), "tax": _get(values, "TaxExpense"), "net_profit": net,
        "owners_profit": _get(values, "ProfitOrLossAttributableToOwnersOfParent") or net,
        "eps": _get(values, "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations",
                    "BasicEarningsLossPerShareFromContinuingOperations"),
        "paid_up": _get(values, "PaidUpValueOfEquityShareCapital"), "face": _get(values, "FaceValueOfEquityShareCapital"),
    }


def balance_sheet(values: dict, bank: bool) -> dict:
    if bank:
        capital, reserves = _get(values, "Capital"), _get(values, "ReservesAndSurplus")
        return {
            "capital": capital, "reserves": reserves, "deposits": _get(values, "Deposits"),
            "borrowings": _get(values, "Borrowings"), "other_liabilities": _get(values, "OtherLiabilitiesAndProvisions"),
            "total": _get(values, "CapitalAndLiabilities", "Assets"),
            "cash": _sum(values, "CashAndBalancesWithReserveBankOfIndia", "BalancesWithBanksAndMoneyAtCallAndShortNotice"),
            "investments": _get(values, "Investments"), "advances": _get(values, "Advances"),
            "fixed_assets": _get(values, "FixedAssets"), "other_assets": _get(values, "OtherAssets"),
            "owners_equity": _sum(values, "Capital", "ReservesAndSurplus"),
        }
    capital, reserves = _get(values, "EquityShareCapital"), _get(values, "OtherEquity")
    owners = _get(values, "EquityAttributableToOwnersOfParent") or _sum(values, "EquityShareCapital", "OtherEquity")
    total = _get(values, "EquityAndLiabilities", "Assets")
    equity = _get(values, "Equity") or owners
    borrowings = _sum(values, "BorrowingsNoncurrent", "BorrowingsCurrent")
    minority = _get(values, "NonControllingInterest")
    fixed = _sum(values, "PropertyPlantAndEquipment", "Goodwill", "OtherIntangibleAssets", "InvestmentProperty",
                 "RightOfUseAssets", "BiologicalAssetsOtherThanBearerPlants")
    cwip = _sum(values, "CapitalWorkInProgress", "IntangibleAssetsUnderDevelopment")
    investments = _sum(values, "NoncurrentInvestments", "CurrentInvestments", "InvestmentsAccountedForUsingEquityMethod")
    assets = _get(values, "Assets", "EquityAndLiabilities")
    return {
        "capital": capital, "reserves": reserves, "minority": minority, "borrowings": borrowings,
        "other_liabilities": _sub(total, equity, borrowings) if total is not None and equity is not None else None,
        "total": total, "fixed_assets": fixed, "cwip": cwip, "investments": investments,
        "other_assets": _sub(assets, fixed, cwip, investments) if assets is not None else None,
        "inventories": _get(values, "Inventories"), "receivables": _get(values, "TradeReceivablesCurrent"),
        "cash": _get(values, "CashAndCashEquivalents"), "current_assets": _get(values, "CurrentAssets"),
        "current_liabilities": _get(values, "CurrentLiabilities"), "owners_equity": owners,
    }


def cash_flow(values: dict) -> dict:
    operating = _get(values, "CashFlowsFromUsedInOperatingActivities")
    capex = _sum(values, "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities",
                 "PurchaseOfTangibleAssetsClassifiedAsInvestingActivities",
                 "PurchaseOfIntangibleAssetsClassifiedAsInvestingActivities")
    return {
        "operating": operating, "investing": _get(values, "CashFlowsFromUsedInInvestingActivities"),
        "financing": _get(values, "CashFlowsFromUsedInFinancingActivities"),
        "net": _get(values, "IncreaseDecreaseInCashAndCashEquivalents",
                    "IncreaseDecreaseInCashAndCashEquivalentsBeforeEffectOfExchangeRateChanges"),
        "capex": capex, "free_cash_flow": None if operating is None else operating - (capex or 0),
        "dividends_paid": _get(values, "DividendsPaidClassifiedAsFinancingActivities"),
    }


# -- rows shown for each statement: (key, label, kind) -------------------------------------------
# kind: money (₹ crore), pct, rs (rupees per share), times, days

PL_ROWS = [("sales", "Sales", "money"), ("expenses", "Expenses", "money"), ("operating_profit", "Operating profit", "money"),
           ("opm", "Operating margin %", "pct"), ("other_income", "Other income", "money"), ("interest", "Interest", "money"),
           ("depreciation", "Depreciation", "money"), ("exceptional", "Exceptional items", "money"),
           ("pbt", "Profit before tax", "money"), ("tax_pct", "Tax %", "pct"), ("net_profit", "Net profit", "money"),
           ("owners_profit", "Profit for shareholders", "money"), ("eps", "EPS ₹", "rs")]
BANK_PL_ROWS = [("revenue", "Revenue (interest earned)", "money"), ("interest", "Interest paid", "money"),
                ("expenses", "Expenses", "money"), ("provisions", "Provisions", "money"),
                ("financing_profit", "Financing profit", "money"), ("financing_margin", "Financing margin %", "pct"),
                ("other_income", "Other income", "money"), ("exceptional", "Exceptional items", "money"),
                ("pbt", "Profit before tax", "money"), ("tax_pct", "Tax %", "pct"), ("net_profit", "Net profit", "money"),
                ("owners_profit", "Profit for shareholders", "money"), ("eps", "EPS ₹", "rs"),
                ("gross_npa_pct", "Gross NPA %", "pct"), ("net_npa_pct", "Net NPA %", "pct")]
BS_ROWS = [("capital", "Equity capital", "money"), ("reserves", "Reserves", "money"), ("minority", "Minority interest", "money"),
           ("borrowings", "Borrowings", "money"), ("other_liabilities", "Other liabilities", "money"),
           ("total", "Total liabilities", "money"), ("fixed_assets", "Fixed assets", "money"),
           ("cwip", "Capital work in progress", "money"), ("investments", "Investments", "money"),
           ("other_assets", "Other assets", "money"), ("total", "Total assets", "money")]
BANK_BS_ROWS = [("capital", "Equity capital", "money"), ("reserves", "Reserves", "money"), ("deposits", "Deposits", "money"),
                ("borrowings", "Borrowings", "money"), ("other_liabilities", "Other liabilities and provisions", "money"),
                ("total", "Total liabilities", "money"), ("cash", "Cash and bank balances", "money"),
                ("investments", "Investments", "money"), ("advances", "Advances (loans)", "money"),
                ("fixed_assets", "Fixed assets", "money"), ("other_assets", "Other assets", "money"),
                ("total", "Total assets", "money")]
CF_ROWS = [("operating", "Cash from operations", "money"), ("investing", "Cash from investing", "money"),
           ("financing", "Cash from financing", "money"), ("net", "Net cash flow", "money"),
           ("capex", "Capital spending", "money"), ("free_cash_flow", "Free cash flow", "money"),
           ("dividends_paid", "Dividends paid", "money")]
RATIO_ROWS = [("sales_growth", "Sales growth %", "pct"), ("profit_growth", "Profit growth %", "pct"),
              ("opm", "Operating margin %", "pct"), ("net_margin", "Net margin %", "pct"), ("roe", "Return on equity %", "pct"),
              ("roce", "Return on capital employed %", "pct"), ("debt_equity", "Debt / equity", "times"),
              ("current_ratio", "Current ratio", "times"), ("interest_cover", "Interest cover", "times"),
              ("asset_turnover", "Asset turnover", "times"), ("debtor_days", "Debtor days", "days"),
              ("inventory_days", "Inventory days (on sales)", "days"), ("eps", "EPS ₹", "rs"),
              ("book_value", "Book value per share ₹", "rs")]
BANK_RATIO_ROWS = [("sales_growth", "Revenue growth %", "pct"), ("profit_growth", "Profit growth %", "pct"),
                   ("roe", "Return on equity %", "pct"), ("roa", "Return on assets %", "pct"),
                   ("cost_income", "Cost to income %", "pct"), ("credit_deposit", "Advances / deposits %", "pct"),
                   ("gross_npa_pct", "Gross NPA %", "pct"), ("net_npa_pct", "Net NPA %", "pct"),
                   ("eps", "EPS ₹", "rs"), ("book_value", "Book value per share ₹", "rs")]
FLOWS = {"sales", "expenses", "operating_profit", "other_income", "interest", "depreciation", "exceptional", "pbt", "tax",
         "net_profit", "owners_profit", "eps", "revenue", "provisions", "financing_profit"}


def derive(pl: dict, bank: bool) -> dict:
    """Add the margins and tax rate to one period's profit and loss."""
    out = dict(pl)
    out["tax_pct"] = pct(pl.get("tax"), pl.get("pbt")) if (pl.get("pbt") or 0) > 0 else None
    if bank:
        out["financing_margin"] = pct(pl.get("financing_profit"), pl.get("revenue"))
    else:
        out["opm"] = pct(pl.get("operating_profit"), pl.get("sales"))
    return out


def table(columns: list[str], periods: list[dict], rows: list[tuple]) -> dict:
    """{"columns", "rows": [{"key", "label", "kind", "values"}]} with money in crore; rows that are empty everywhere dropped."""
    out = []
    for key, name, kind in rows:
        values = []
        for period in periods:
            value = period.get(key)
            if value is not None and kind == "money":
                value = value / CR
            values.append(None if value is None else round(float(value), 2))
        if key == "exceptional" and not any(values):
            continue
        if any(v is not None for v in values):
            out.append({"key": key, "label": name, "kind": kind, "values": values})
    return {"columns": columns, "rows": out}


# -- the filings list -----------------------------------------------------------------------------

def parse_listing(answer) -> list[dict]:
    """NSE's results filings (either list) as [{end, basis, xbrl, when}], one per period and basis (the latest filed)."""
    rows = answer.get("data") if isinstance(answer, dict) else answer
    found: dict[tuple, dict] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        url = str(row.get("xbrl") or "").strip()
        if not url.lower().startswith("http") or not url.lower().endswith(".xml"):
            continue
        end = day_of(row.get("toDate") or row.get("qe_Date") or row.get("periodEnded") or row.get("period_ended"))
        if not end or end[:4] < str(FIRST_YEAR):
            continue
        start = day_of(row.get("fromDate"))
        if start and months_between(start, end) > 3:          # an annual or half-year row: the Q4/Q2 filing has it
            continue
        nature = str(row.get("consolidated") or "").lower()
        basis = "standalone" if ("non" in nature or "stand" in nature) else "consolidated" if "consol" in nature else None
        if basis is None:
            continue
        when = when_of(row.get("broadCastDate") or row.get("broadcast_Date") or row.get("filingDate")
                       or row.get("exchdisstime") or row.get("creation_Date"))
        key = (end, basis)
        if key not in found or when > found[key]["when"]:
            found[key] = {"end": end, "basis": basis, "xbrl": url, "when": when}
    return sorted(found.values(), key=lambda r: (r["end"], r["basis"]), reverse=True)


def wanted(listing: list[dict], basis: str, depth: str = "full") -> list[dict]:
    """The filings to read for ``basis``: the last 12 quarters, every March (years) and the latest September."""
    rows = [r for r in listing if r["basis"] == basis]
    if depth == "results":
        return rows[:5]                                  # latest quarter and the same quarter a year before
    pick = {r["end"]: r for r in rows[:QUARTERS]}
    marches = [r for r in rows if r["end"][5:7] == "03"][:YEARS + 1]
    pick.update({r["end"]: r for r in marches})
    september = next((r for r in rows if r["end"][5:7] == "09"), None)
    if september and (not marches or september["end"] > marches[0]["end"]):
        pick[september["end"]] = september
    return sorted(pick.values(), key=lambda r: r["end"], reverse=True)


# -- statements from stored filings ---------------------------------------------------------------

def _consecutive(ends: list[str]) -> bool:
    return all(months_between(a, b) == 4 for a, b in zip(ends, ends[1:], strict=False))


def statements(filings: list[dict], basis: str, price: float | None = None) -> dict:
    """Quarterly results, profit and loss (years and TTM), balance sheet, cash flow, ratios and a summary."""
    mine = sorted((f for f in filings if f["basis"] == basis), key=lambda f: f["quarter"]["end"])
    if not mine:
        return {"available": False}
    bank = mine[-1]["bank"]
    pl_rows, bs_rows, ratio_rows = (BANK_PL_ROWS, BANK_BS_ROWS, BANK_RATIO_ROWS) if bank else (PL_ROWS, BS_ROWS, RATIO_ROWS)

    quarters = [(f["quarter"]["end"], derive(profit_loss(f["quarter"]["values"], bank), bank)) for f in mine
                if months_between(f["quarter"]["start"] or f["quarter"]["end"], f["quarter"]["end"]) <= 3]
    quarters = quarters[-QUARTERS:]
    years = []
    for f in mine:
        ytd = f.get("ytd")
        if ytd and f["quarter"]["end"][5:7] == "03" and ytd.get("start") and months_between(ytd["start"], ytd["end"]) == 12:
            years.append((ytd["end"], f, derive(profit_loss(ytd["values"], bank), bank)))
    years = years[-YEARS:]

    ttm = None
    last4 = quarters[-4:]
    if len(last4) == 4 and _consecutive([e for e, _ in last4]):
        ttm = {k: sum(p[k] for _, p in last4) for k in FLOWS if all(p.get(k) is not None for _, p in last4)}
        ttm = derive(ttm, bank)
    pl_periods = [p for _, _, p in years] + ([ttm] if ttm else [])
    pl_columns = [label(e) for e, _, _ in years] + (["TTM"] if ttm else [])

    sheets = [(f["balance"]["date"], balance_sheet(f["balance"]["values"], bank)) for f in mine if f.get("balance")]
    sheets_by = dict(sheets)
    marches = [(e, s) for e, s in sheets if e[5:7] == "03"][-YEARS:]
    shown_sheets = marches + ([sheets[-1]] if sheets and sheets[-1][0][5:7] != "03" else [])

    flows = [(f["ytd"]["end"], cash_flow(f["ytd"]["values"])) for f in mine if f.get("ytd") and f["ytd"].get("start")
             and f["quarter"]["end"][5:7] == "03" and months_between(f["ytd"]["start"], f["ytd"]["end"]) == 12]
    flows = [(e, c) for e, c in flows if c.get("operating") is not None][-YEARS:]
    half = next((f for f in reversed(mine) if f["quarter"]["end"][5:7] == "09" and f.get("ytd")), None)
    half_flow = cash_flow(half["ytd"]["values"]) if half else None
    if half_flow and half_flow.get("operating") is not None and (not flows or half["quarter"]["end"] > flows[-1][0]):
        flow_columns = [label(e) for e, _ in flows] + [f"Apr–{label(half['quarter']['end'])} (6 months)"]
        flow_periods = [c for _, c in flows] + [half_flow]
    else:
        flow_columns, flow_periods = [label(e) for e, _ in flows], [c for _, c in flows]

    ratios, previous = [], None
    for end, _filing, pl in years:
        sheet = sheets_by.get(end, {})
        prior = sheets_by.get(f"{int(end[:4]) - 1}{end[4:]}", {})
        equity = sheet.get("owners_equity")
        average_equity = (equity + prior["owners_equity"]) / 2 if equity and prior.get("owners_equity") else equity
        shares = pl["paid_up"] / pl["face"] if pl.get("paid_up") and pl.get("face") else None
        top = pl.get("revenue") if bank else pl.get("sales")
        row = {
            "sales_growth": growth(top, previous and (previous.get("revenue") if bank else previous.get("sales"))),
            "profit_growth": growth(pl.get("owners_profit"), previous and previous.get("owners_profit")),
            "roe": pct(pl.get("owners_profit"), average_equity), "eps": pl.get("eps"),
            "book_value": equity / shares if equity and shares else None,
        }
        if bank:
            nii_other = (pl.get("revenue") or 0) - (pl.get("interest") or 0) + (pl.get("other_income") or 0)
            row.update(roa=pct(pl.get("net_profit"), sheet.get("total")),
                       cost_income=pct(pl.get("expenses"), nii_other) if nii_other > 0 else None,
                       credit_deposit=pct(sheet.get("advances"), sheet.get("deposits")),
                       gross_npa_pct=pl.get("gross_npa_pct"), net_npa_pct=pl.get("net_npa_pct"))
        else:
            sales = pl.get("sales")
            ebit = None if pl.get("pbt") is None else pl["pbt"] + (pl.get("interest") or 0)
            employed = None if sheet.get("owners_equity") is None else sheet["owners_equity"] + (sheet.get("borrowings") or 0) + (sheet.get("minority") or 0)
            row.update(opm=pl.get("opm"), net_margin=pct(pl.get("net_profit"), sales), roce=pct(ebit, employed),
                       debt_equity=(sheet["borrowings"] / equity) if equity and sheet.get("borrowings") is not None else None,
                       current_ratio=(sheet["current_assets"] / sheet["current_liabilities"])
                       if sheet.get("current_assets") and sheet.get("current_liabilities") else None,
                       interest_cover=(ebit / pl["interest"]) if ebit is not None and pl.get("interest") else None,
                       asset_turnover=(sales / sheet["total"]) if sales and sheet.get("total") else None,
                       debtor_days=(sheet["receivables"] / sales * 365) if sales and sheet.get("receivables") is not None else None,
                       inventory_days=(sheet["inventories"] / sales * 365) if sales and sheet.get("inventories") else None)
        ratios.append(row)
        previous = pl

    latest = mine[-1]
    latest_pl = profit_loss(latest["quarter"]["values"], bank)
    shares = latest_pl["paid_up"] / latest_pl["face"] if latest_pl.get("paid_up") and latest_pl.get("face") else None
    summary = {"shares": shares, "price": price, "bank": bank, "latest_quarter": label(latest["quarter"]["end"]),
               "audited": latest.get("audited"), "company": latest.get("company")}
    if price and shares:
        cap = price * shares
        summary["market_cap"] = cap
        profit = (ttm or {}).get("owners_profit")
        summary["pe"] = cap / profit if profit and profit > 0 else None
        equity = sheets[-1][1].get("owners_equity") if sheets else None
        summary["pb"] = cap / equity if equity and equity > 0 else None
        summary["book_value"] = equity / shares if equity else None
    if ttm:
        summary["sales_ttm"] = ttm.get("revenue" if bank else "sales")
        summary["profit_ttm"] = ttm.get("owners_profit")
    if ratios:
        summary.update({k: ratios[-1].get(k) for k in ("roe", "roce", "debt_equity", "roa", "gross_npa_pct", "net_npa_pct")})
    by_end = dict(quarters)
    if quarters:
        end, now = quarters[-1]
        ago = by_end.get(f"{int(end[:4]) - 1}{end[4:]}")
        key = "revenue" if bank else "sales"
        summary["quarter_sales_yoy"] = growth(now.get(key), ago and ago.get(key))
        summary["quarter_profit_yoy"] = growth(now.get("owners_profit"), ago and ago.get("owners_profit"))
    for k, v in list(summary.items()):
        if isinstance(v, float):
            summary[k] = round(v, 2)
    return {
        "available": True, "bank": bank, "basis": basis, "summary": summary,
        "quarterly": table([label(e) for e, _ in quarters], [p for _, p in quarters], pl_rows),
        "profit_loss": table(pl_columns, pl_periods, pl_rows),
        "balance_sheet": table([label(e) for e, _ in shown_sheets], [s for _, s in shown_sheets], bs_rows),
        "cash_flow": table(flow_columns, flow_periods, CF_ROWS),
        "ratios": table([label(e) for e, _, _ in years], ratios, ratio_rows),
    }


# -- results comparison (Results Drift) ------------------------------------------------------------

def _first_number(row: dict, *names):
    for name in names:
        value = xbrl._number(None if row.get(name) is None else str(row.get(name)))
        if value is not None:
            return value
    return None


def parse_comparison(answer) -> list[dict]:
    """NSE's results comparison: [{end, filed, sales, profit, eps}] newest first, money in rupees (NSE gives lakhs)."""
    rows = answer.get("resCmpData") if isinstance(answer, dict) else None
    out = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        end = day_of(row.get("re_to_dt"))
        if not end:
            continue

        sales = _first_number(row, "re_net_sale", "re_int_earned", "re_total_inc", "re_tot_inc")
        profit = _first_number(row, "re_net_profit", "re_con_pro_loss", "re_proloss_ord_act")
        out[end] = {"end": end, "filed": day_of(row.get("re_create_dt")),
                    "sales": None if sales is None else sales * 1e5, "profit": None if profit is None else profit * 1e5,
                    "eps": _first_number(row, "re_basic_eps_for_cont_dic_opr", "re_basic_eps", "re_bsc_eps_bfr_exi")}
    return sorted(out.values(), key=lambda r: r["end"], reverse=True)


def verdict(sales_yoy, profit_yoy, profit, profit_ago) -> str | None:
    """Strong, Weak or Mixed results from sales and profit against the same quarter a year before."""
    if profit is not None and profit_ago is not None and profit_ago <= 0 < profit:
        return "Strong"
    if profit is not None and profit_ago is not None and profit < 0 <= profit_ago:
        return "Weak"
    if profit_yoy is None:
        return None
    if profit_yoy >= 15 and (sales_yoy is None or sales_yoy >= 0):
        return "Strong"
    if profit_yoy <= -10 or (sales_yoy is not None and sales_yoy <= -5 and profit_yoy < 0):
        return "Weak"
    return "Mixed"


def results_summary(quarters: list[dict], result_date: str | None = None) -> dict | None:
    """The latest quarter against the same quarter a year before: [{end, sales, profit, eps}] rupees, newest first.

    None when the latest quarter is older than the results announced on ``result_date`` (not out yet).
    """
    if not quarters:
        return None
    latest = quarters[0]
    if result_date:
        # A quarter ends before its board meeting, at most about 75 days (Q4: 60 days, extended by filing delays).
        lag = (date.fromisoformat(result_date) - date.fromisoformat(latest["end"])).days
        if lag < 0 or lag > 120:
            return None
    ago_end = f"{int(latest['end'][:4]) - 1}{latest['end'][4:]}"
    ago = next((q for q in quarters if q["end"] == ago_end), None)
    if ago is None:
        return {"quarter": label(latest["end"]), "sales": latest.get("sales"), "profit": latest.get("profit"),
                "eps": latest.get("eps"), "sales_yoy": None, "profit_yoy": None, "verdict": None, "complete": False}
    sales_yoy, profit_yoy = growth(latest.get("sales"), ago.get("sales")), growth(latest.get("profit"), ago.get("profit"))
    return {"quarter": label(latest["end"]), "sales": latest.get("sales"), "profit": latest.get("profit"),
            "eps": latest.get("eps"), "sales_yoy": None if sales_yoy is None else round(sales_yoy, 1),
            "profit_yoy": None if profit_yoy is None else round(profit_yoy, 1),
            "verdict": verdict(sales_yoy, profit_yoy, latest.get("profit"), ago.get("profit")), "complete": True}


# -- the service ---------------------------------------------------------------------------------

class Financials:
    """Downloads and keeps each company's filings; answers statements and Results Drift numbers.

    One background thread works a queue: companies a user opened first, then Results Drift companies.
    """

    def __init__(self, client: NseClient | None = None, path: Path | None = None, now=time.time):
        self.client = client or NseClient()
        self.dir = path or home() / "financials"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._now = now
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._queue: deque = deque()
        self._status: dict[str, dict] = {}
        self._thread: threading.Thread | None = None
        self._compare: dict[str, dict] = {}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads((self.dir / "_results.json").read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._compare = stored

    # -- storage ---------------------------------------------------------------

    def _file(self, code: str) -> Path:
        return self.dir / (re.sub(r"[^A-Z0-9&_-]", "_", code) + ".json")

    def _load(self, code: str) -> dict:
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(self._file(code).read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                stored.setdefault("filings", {})
                return stored
        return {"filings": {}, "listing": [], "listed_at": 0, "note": None}

    def _save(self, code: str, store: dict) -> None:
        path = self._file(code)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(store), encoding="utf-8")
        tmp.replace(path)

    def _save_compare(self) -> None:
        path = self.dir / "_results.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._compare), encoding="utf-8")
        tmp.replace(path)

    def _needs(self, store: dict, row: dict, basis: str) -> bool:
        """A wanted filing not stored, or revised since, and not failed in the last LISTING_SECONDS."""
        key = f"{row['end']}:{basis}"
        if self._now() - store.get("failed", {}).get(key, 0) < LISTING_SECONDS:
            return False
        held = store["filings"].get(key)
        return held is None or held.get("when", "") < row["when"]

    # -- network steps ---------------------------------------------------------

    def _listing(self, code: str) -> list[dict]:
        found, failures = [], []
        for path, params in (("corporates-financial-results", {"index": "equities", "period": "Quarterly", "symbol": code}),
                             ("integrated-filing-results", {"index": "equities", "symbol": code, "type": "Integrated Filing- Financials",
                                                            "period_ended": "all", "page": 1, "size": 20})):
            try:
                found += parse_listing(self.client.json(path, params))
            except Exception as exc:  # noqa: BLE001 - one list may fail; use the other
                failures.append(f"{path}: {str(exc)[:160]}")
        if not found and failures:
            raise RuntimeError("; ".join(failures))
        merged: dict[tuple, dict] = {}
        for row in found:
            key = (row["end"], row["basis"])
            if key not in merged or row["when"] > merged[key]["when"]:
                merged[key] = row
        return sorted(merged.values(), key=lambda r: (r["end"], r["basis"]), reverse=True)

    def _refresh(self, code: str, basis: str | None, depth: str) -> None:
        status = self._status.setdefault(code, {})
        status.update(state="loading", done=0, total=0, note=None)
        store = self._load(code)
        try:
            if not store.get("listing") or self._now() - store.get("listed_at", 0) > LISTING_SECONDS:
                store["listing"], store["listed_at"] = self._listing(code), self._now()
                self._save(code, store)
        except Exception as exc:  # noqa: BLE001 - keep what is stored, say why
            store["note"] = f"NSE's list of results filings could not be read: {str(exc)[:200]}"
            self._save(code, store)
            status.update(state="error" if not store["filings"] else "ready", note=store["note"], at=self._now())
            return
        listing = store["listing"]
        if not listing:
            store["note"] = "NSE lists no results filings with XBRL for this company."
            self._save(code, store)
            status.update(state="ready", note=store["note"])
            return
        chosen = basis or self.default_basis(listing)
        todo = [r for r in wanted(listing, chosen, depth) if self._needs(store, r, chosen)]
        status.update(total=len(todo), basis=chosen)
        failed = []
        for row in todo:
            try:
                parsed = xbrl.parse(self.client.file(row["xbrl"]))
            except Exception as exc:  # noqa: BLE001 - skip this filing, keep going; retried after LISTING_SECONDS
                failed.append(f"{label(row['end'])}: {str(exc)[:80]}")
                store.setdefault("failed", {})[f"{row['end']}:{chosen}"] = self._now()
            else:
                parsed["when"], parsed["source"] = row["when"], row["xbrl"]
                store.get("failed", {}).pop(f"{row['end']}:{chosen}", None)
                store["filings"][f"{parsed['quarter']['end']}:{parsed['basis']}"] = parsed
                if parsed["quarter"]["end"] != row["end"] or parsed["basis"] != chosen:
                    store["filings"][f"{row['end']}:{chosen}"] = {"skip": True, "when": row["when"]}
            status["done"] = status.get("done", 0) + 1
            if status["done"] % 4 == 0:
                self._save(code, store)
        store["note"] = (f"{len(failed)} filing(s) could not be read: " + "; ".join(failed[:3])) if failed else None
        self._save(code, store)
        status.update(state="ready", note=store["note"], at=self._now())

    def _compare_one(self, code: str, result_date: str | None) -> None:
        try:
            rows = parse_comparison(self.client.json("results-comparision", {"symbol": code}))
            note = None
        except Exception as exc:  # noqa: BLE001 - try again later
            rows, note = [], str(exc)[:160]
        with self._lock:
            self._compare[code] = {"fetched": self._now(), "rows": rows, "for": result_date, "note": note}
            self._save_compare()
        summary = results_summary(rows, result_date)
        if summary is None or not summary.get("complete"):
            store = self._load(code)
            if not store["filings"] or self._now() - store.get("listed_at", 0) > COMPARE_RETRY_SECONDS:
                self._refresh(code, None, "results")

    # -- the queue -------------------------------------------------------------

    def start(self) -> None:
        if self._thread is None:
            self._thread = threading.Thread(target=self._run, name="desk-financials", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            self._wake.wait(30)
            self._wake.clear()
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    job = self._queue.popleft()
                try:
                    if job[0] == "statements":
                        self._refresh(job[1], job[2], "full")
                    else:
                        self._compare_one(job[1], job[2])
                except Exception:  # noqa: BLE001 - one company must not stop the queue
                    logger.exception("financials job %s failed", job)
                    self._status.setdefault(job[1], {}).update(state="error", note="Reading the filings failed.")

    def work(self) -> None:
        """Run the queued jobs now, in this thread (tests, and a desk without the background thread)."""
        while self._queue:
            job = self._queue.popleft()
            if job[0] == "statements":
                self._refresh(job[1], job[2], "full")
            else:
                self._compare_one(job[1], job[2])

    def request(self, symbol: str, basis: str | None = None) -> None:
        """Queue a company's statements, ahead of Results Drift work."""
        code = nse_symbol(symbol)
        if not code:
            return
        with self._lock:
            job = ("statements", code, basis)
            if job in self._queue:
                return
            self._queue.appendleft(job)
            self._status.setdefault(code, {}).update(state="loading", done=0, total=0)
        self._wake.set()

    def want_results(self, rows: list[tuple[str, str]], limit: int = 400) -> int:
        """Queue the results comparison for (symbol, result date) pairs not yet known. Returns how many were queued."""
        queued = 0
        with self._lock:
            pending = {j[1] for j in self._queue if j[0] == "compare"}
            for symbol, result_date in rows:
                code = nse_symbol(symbol)
                if not code or code in pending or queued >= limit:
                    continue
                cached = self._compare.get(code)
                if cached:
                    summary = self._summary(code, result_date)
                    if summary and summary.get("complete"):
                        continue
                    if self._now() - cached.get("fetched", 0) < COMPARE_RETRY_SECONDS:
                        continue
                self._queue.append(("compare", code, result_date))
                pending.add(code)
                queued += 1
        if queued:
            self._wake.set()
        return queued

    # -- answers ---------------------------------------------------------------

    def forget_listing(self, symbol: str) -> None:
        """Read the company's list of filings again on its next download (the Refresh button)."""
        code = nse_symbol(symbol)
        if code:
            store = self._load(code)
            store["listed_at"], store["failed"] = 0, {}
            self._save(code, store)

    @staticmethod
    def default_basis(listing: list[dict]) -> str:
        """Consolidated when the company files consolidated results lately, else standalone."""
        recent = listing[:8]
        return "consolidated" if any(r["basis"] == "consolidated" for r in recent) else "standalone"

    def filings(self, symbol: str) -> list[dict]:
        code = nse_symbol(symbol)
        if not code:
            return []
        return [f for f in self._load(code)["filings"].values() if not f.get("skip")]

    def view(self, symbol: str, basis: str | None = None, price: float | None = None, refresh: bool = True) -> dict:
        """Everything for the stock dialog. Starts a download when the company is new or its list is stale."""
        code = nse_symbol(symbol)
        if not code:
            return {"available": False, "state": "ready", "note": "Financials come from NSE filings; this stock is listed only on BSE."}
        store = self._load(code)
        listing = store.get("listing") or []
        chosen = basis if basis in ("consolidated", "standalone") else (self.default_basis(listing) if listing else None)
        status = dict(self._status.get(code, {}))
        have = [f for f in store["filings"].values() if not f.get("skip")]
        stale = not listing or self._now() - store.get("listed_at", 0) > LISTING_SECONDS
        missing = bool(listing) and chosen is not None and any(self._needs(store, r, chosen) for r in wanted(listing, chosen))
        failed_lately = self._now() - status.get("at", 0) < RETRY_SECONDS and (status.get("state") == "error" or status.get("note"))
        if refresh and status.get("state") != "loading" and not failed_lately and (stale or missing):
            self.request(symbol, chosen)
            status = dict(self._status.get(code, {}))
        out = statements(have, chosen, price) if chosen else {"available": False}
        out.update(state=status.get("state") or "ready", done=status.get("done", 0), total=status.get("total", 0),
                   note=status.get("note") or store.get("note"), basis=chosen,
                   bases=sorted({r["basis"] for r in listing}), symbol=code)
        return out

    def _summary(self, code: str, result_date: str | None) -> dict | None:
        cached = self._compare.get(code)
        summary = results_summary(cached["rows"], result_date) if cached else None
        if summary and summary.get("complete"):
            return summary
        store = self._load(code)
        filings = [f for f in store["filings"].values() if not f.get("skip")]
        if filings:
            basis = self.default_basis(store.get("listing") or []) if store.get("listing") else filings[0]["basis"]
            quarters = []
            for f in sorted((f for f in filings if f["basis"] == basis), key=lambda f: f["quarter"]["end"], reverse=True):
                pl = profit_loss(f["quarter"]["values"], f["bank"])
                quarters.append({"end": f["quarter"]["end"], "sales": pl.get("revenue") if f["bank"] else pl.get("sales"),
                                 "profit": pl.get("owners_profit"), "eps": pl.get("eps")})
            from_filings = results_summary(quarters, result_date)
            if from_filings and (from_filings.get("complete") or not summary):
                return from_filings
        return summary

    def results(self, symbol: str, result_date: str | None) -> dict | None:
        """Latest quarter's sales and profit against a year before (money in crore), or None when not known yet."""
        code = nse_symbol(symbol)
        if not code:
            return None
        summary = self._summary(code, result_date)
        if not summary:
            return None
        out = dict(summary)
        for key in ("sales", "profit"):
            if out.get(key) is not None:
                out[key] = round(out[key] / CR, 2)
        return out


def peer_rows(symbol: str, industry_of: dict, names: dict, fundamentals_get, price_of, limit: int = 12) -> dict:
    """Companies in the same industry (NSE's index lists) with Yahoo Finance's trailing figures, biggest first."""
    industry = industry_of.get(symbol)
    if not industry:
        return {"industry": None, "rows": [], "missing": []}
    rows, missing = [], []
    for peer in sorted(s for s, i in industry_of.items() if i == industry):
        facts = fundamentals_get(peer) or {}
        if not facts:
            missing.append(peer)
        price = None
        with contextlib.suppress(Exception):
            price = price_of(peer)
        rows.append({"symbol": peer, "name": names.get(peer) or facts.get("name"), "price": price,
                     "market_cap": facts.get("market_cap"), "pe": facts.get("pe"), "pb": facts.get("pb"),
                     "roe": facts.get("roe"), "sales": facts.get("revenue"), "sales_growth": facts.get("revenue_growth"),
                     "profit_growth": facts.get("earnings_growth"), "net_margin": facts.get("profit_margin"),
                     "dividend_yield": facts.get("dividend_yield"), "this": peer == symbol})
    rows.sort(key=lambda r: -(r["market_cap"] or 0))
    top = rows[:limit]
    if not any(r["this"] for r in top):
        top += [r for r in rows if r["this"]]
    return {"industry": industry, "rows": top, "missing": missing, "count": len(rows)}
