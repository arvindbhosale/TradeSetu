"""Fundamental screens after famous investors, run on an index's stocks with today's figures.

These are screens, not backtests: Yahoo gives only the latest figures, and a
backtest on today's P/E or ROE would know the future (look-ahead bias).
Each applies the investor's published numeric rules as far as the available
figures allow and says what it leaves out. A stock passes only when every
figure the screen needs is known.

- Warren Buffett: high return on equity with little debt, steady margins, a sensible price
  (Berkshire letters; Hagstrom, *The Warren Buffett Way*).
- Benjamin Graham: the defensive investor's limits, P/E ≤ 15, P/B ≤ 1.5, P/E × P/B ≤ 22.5, dividends
  (*The Intelligent Investor*, ch. 14).
- Peter Lynch: PEG (P/E ÷ growth) ≤ 1, moderate growth, low debt (*One Up on Wall Street*).
- Joel Greenblatt: the Magic Formula, ranked by earnings yield and return on capital, banks and
  financials left out (*The Little Book That Beats the Market*).
- Vijay Kedia: SMILE, Small in size, Medium in experience, Large in aspiration, eXtra-large market
  potential (his talks and interviews); here small caps with fast growth, good ROE, low debt
  and high promoter holding.
"""

from __future__ import annotations

CRORE = 1e7

SCREENS = {
    "buffett": {
        "name": "Warren Buffett: quality at a sensible price", "legend": "Warren Buffett", "universe": "nifty200",
        "about": "Durable businesses: ROE at least 15%, debt under half of equity, net margin at least 10%, P/E at most 35, "
                 "and a company of size (₹10,000 Cr+). Ranked by ROE. Left out: the moat, management and the 10-year record, "
                 "which no screen measures.",
        "rules": [("roe", ">=", 15), ("debt_to_equity", "<=", 0.5), ("profit_margin", ">=", 10), ("pe", "<=", 35),
                  ("pe", ">", 0), ("market_cap", ">=", 10_000 * CRORE)],
        "rank": ("roe", True)},
    "graham": {
        "name": "Benjamin Graham: defensive value", "legend": "Benjamin Graham", "universe": "nifty500",
        "about": "P/E at most 15, price at most 1.5 × book, P/E × P/B at most 22.5 (the Graham number), debt at most equity, "
                 "and a dividend. Ranked by P/E × P/B, cheapest first. Left out: 20 years of dividends and 10 of profits.",
        "rules": [("pe", "<=", 15), ("pe", ">", 0), ("pb", "<=", 1.5), ("pb", ">", 0), ("pe_x_pb", "<=", 22.5),
                  ("debt_to_equity", "<=", 1.0), ("dividend_yield", ">", 0)],
        "rank": ("pe_x_pb", False)},
    "lynch": {
        "name": "Peter Lynch: growth at a reasonable price", "legend": "Peter Lynch", "universe": "nifty500",
        "about": "PEG (P/E ÷ earnings growth %) at most 1, earnings growth between 15% and 50% (faster rarely lasts), debt "
                 "under 0.8 × equity. Ranked by PEG, lowest first.",
        "rules": [("peg", "<=", 1.0), ("peg", ">", 0), ("earnings_growth", ">=", 15), ("earnings_growth", "<=", 50),
                  ("debt_to_equity", "<=", 0.8)],
        "rank": ("peg", False)},
    "greenblatt": {
        "name": "Joel Greenblatt: Magic Formula", "legend": "Joel Greenblatt", "universe": "nifty500",
        "about": "Good businesses at bargain prices: every stock ranked by earnings yield (1 ÷ P/E) and by return on capital "
                 "(ROA stands in: Yahoo does not give EBIT over tangible capital), the two ranks added, lowest total first. "
                 "Banks, finance and utilities are left out, as Greenblatt does; ₹5,000 Cr and larger.",
        "rules": [("pe", ">", 0), ("roa", ">", 0), ("market_cap", ">=", 5_000 * CRORE), ("sector", "not in", ("Financial Services", "Utilities"))],
        "rank": ("magic", False)},
    "kedia": {
        "name": "Vijay Kedia: SMILE small caps", "legend": "Vijay Kedia", "universe": "niftysmallcap250",
        "about": "Small (₹500 to ₹20,000 Cr), growing fast (sales +15%, profit +20% a year), ROE 15%+, debt at most equity, "
                 "promoters holding at least half. Ranked by profit growth. Left out: management quality and the size of "
                 "the opportunity, the M and the XL of SMILE, which are for you to judge.",
        "rules": [("market_cap", ">=", 500 * CRORE), ("market_cap", "<=", 20_000 * CRORE), ("revenue_growth", ">=", 15),
                  ("earnings_growth", ">=", 20), ("roe", ">=", 15), ("debt_to_equity", "<=", 1.0), ("promoter_holding", ">=", 50)],
        "rank": ("earnings_growth", True)},
}

_OPS = {">=": lambda a, b: a >= b, "<=": lambda a, b: a <= b, ">": lambda a, b: a > b, "<": lambda a, b: a < b,
        "not in": lambda a, b: a not in b}


def _derived(values: dict) -> dict:
    out = dict(values)
    pe, pb, growth = values.get("pe"), values.get("pb"), values.get("earnings_growth")
    if pe is not None and pb is not None:
        out["pe_x_pb"] = round(pe * pb, 2)
    if pe is not None and growth:
        out["peg"] = round(pe / growth, 2) if growth > 0 else None
    return out


_LABELS = {"roe": "ROE %", "roa": "ROA %", "pe": "P/E", "pb": "P/B", "pe_x_pb": "P/E × P/B", "peg": "PEG",
           "debt_to_equity": "Debt/equity", "profit_margin": "Net margin %", "revenue_growth": "Sales growth %",
           "earnings_growth": "Profit growth %", "dividend_yield": "Dividend yield %", "promoter_holding": "Promoter holding %",
           "market_cap": "Market cap", "sector": "Sector"}


def _rule_text(field, op, limit) -> str:
    if field == "market_cap":
        limit = f"₹{limit / CRORE:,.0f} Cr"
    elif isinstance(limit, tuple):
        limit = ", ".join(limit)
    return f"{_LABELS.get(field, field)} {op.replace('>=', '≥').replace('<=', '≤')} {limit}"


def run(screen_key: str, rows: list[dict], figures) -> dict:
    """``rows``: [{"symbol", "name", "industry"}]; ``figures(symbol)`` gives the cached fundamentals or None."""
    if screen_key not in SCREENS:
        raise ValueError(f"screen must be one of {', '.join(SCREENS)}")
    screen = SCREENS[screen_key]
    passed, missing, checked = [], 0, 0
    for row in rows:
        values = figures(row["symbol"])
        if not values:
            missing += 1
            continue
        checked += 1
        v = _derived(values)
        ok = True
        for field, op, limit in screen["rules"]:
            value = v.get(field)
            if field == "sector" and value is None:
                continue                                       # unknown sector: not excluded on that ground
            if value is None or not _OPS[op](value, limit):
                ok = False
                break
        if ok:
            passed.append({**row, **{k: v.get(k) for k in ("market_cap", "pe", "pb", "roe", "roa", "debt_to_equity",
                                                             "profit_margin", "revenue_growth", "earnings_growth",
                                                             "dividend_yield", "promoter_holding", "peg", "pe_x_pb", "sector")}})
    field, descending = screen["rank"]
    if field == "magic":
        by_yield = sorted(passed, key=lambda r: -1 / r["pe"])
        by_return = sorted(passed, key=lambda r: -r["roa"])
        for r in passed:
            r["magic"] = by_yield.index(r) + 1 + by_return.index(r) + 1
    passed.sort(key=lambda r: (r.get(field) is None, -(r.get(field) or 0) if descending else (r.get(field) or 0)))
    return {"screen": screen_key, "name": screen["name"], "about": screen["about"], "legend": screen["legend"],
            "rules": [_rule_text(f, op, lim) for f, op, lim in screen["rules"]],
            "stocks": passed[:50], "passed": len(passed), "checked": checked, "missing": missing, "total": len(rows)}
