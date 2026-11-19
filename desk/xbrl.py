"""Read a listed company's financial results from the XBRL file it files with NSE and BSE.

Every Indian listed company files each quarter's results as XBRL under SEBI's
taxonomy (namespaces ``in-bse-fin`` up to 2024, ``in-capmkt`` from 2025; the
element names are the same). The files use fixed context ids:

- ``OneD``: the quarter (three months),
- ``FourD``: the year to date (six months in Q2, twelve in Q4),
- ``OneI``: the balance sheet at the period end (in Q2 and Q4 filings),
- dimensional contexts (segments, expense details) that are ignored here.

Some exchange copies leave out the context definitions themselves, so the
period of each context is read from the ``DateOfStartOfReportingPeriod`` and
``DateOfEndOfReportingPeriod`` facts filed in it, not from the context. Values
are in rupees whatever rounding the company used for its PDF.

Banks file a different format (interest earned, deposits, advances, NPAs);
``bank`` is set when the filing is in that format.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

QUARTER, YTD, BALANCE = "OneD", "FourD", "OneI"
TEXT = {"Symbol", "ScripCode", "NameOfTheCompany", "NameOfBank", "NatureOfReportStandaloneConsolidated",
        "WhetherResultsAreAuditedOrUnaudited", "DateOfStartOfReportingPeriod", "DateOfEndOfReportingPeriod",
        "DateOfStartOfFinancialYear", "DateOfEndOfFinancialYear", "DateOfBoardMeetingWhenFinancialResultsWereApproved",
        "ResultType"}
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


class NotAResultsFiling(ValueError):
    """The file is not a financial results XBRL instance."""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _number(text: str | None) -> float | None:
    if text is None:
        return None
    text = text.strip().replace(",", "")
    if not text or len(text) > 30:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if value == value and abs(value) != float("inf") else None


def facts(content: bytes | str) -> dict[str, dict[str, object]]:
    """{context id: {element: value}} for the quarter, year-to-date and balance sheet contexts.

    Numbers become floats; the first value filed for an element in a context is kept (a few elements, such as
    opening and closing cash in the cash flow, are filed twice in one context and are not used).
    """
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise NotAResultsFiling(f"not XML: {exc}") from None
    out: dict[str, dict[str, object]] = {QUARTER: {}, YTD: {}, BALANCE: {}}
    for element in root:
        context = element.get("contextRef")
        if context not in out:
            continue
        name = _local(element.tag)
        if name in out[context]:
            continue
        if name in TEXT:
            out[context][name] = " ".join((element.text or "").split())
        else:
            value = _number(element.text)
            if value is not None:
                out[context][name] = value
    return out


def _date(value) -> str | None:
    match = DATE.search(str(value or ""))
    return match.group(0) if match else None


def parse(content: bytes | str) -> dict:
    """One filing: company, basis, the quarter's and the year-to-date figures, and the balance sheet.

    ``quarter`` and ``ytd`` are {"start", "end", "values"}; ``balance`` is {"date", "values"} or None when the
    filing has no balance sheet (Q1 and Q3). ``ytd`` is None when the filing has no year-to-date figures.
    """
    found = facts(content)
    q, ytd, bal = found[QUARTER], found[YTD], found[BALANCE]
    start, end = _date(q.get("DateOfStartOfReportingPeriod")), _date(q.get("DateOfEndOfReportingPeriod"))
    if not end or not any(isinstance(v, float) for v in q.values()):
        raise NotAResultsFiling("no quarter figures in the filing")
    nature = str(q.get("NatureOfReportStandaloneConsolidated") or ytd.get("NatureOfReportStandaloneConsolidated") or "")
    bank = "InterestEarned" in q or "InterestEarned" in ytd or "bank" in str(q.get("ResultType", "")).lower()

    def numbers(values: dict) -> dict[str, float]:
        return {k: v for k, v in values.items() if isinstance(v, float)}

    out = {
        "symbol": str(q.get("Symbol") or "").strip().upper() or None,
        "company": q.get("NameOfTheCompany") or q.get("NameOfBank") or None,
        "basis": "consolidated" if nature.lower().startswith("consol") else "standalone",
        "bank": bool(bank),
        "audited": str(q.get("WhetherResultsAreAuditedOrUnaudited") or "") or None,
        "board_date": _date(q.get("DateOfBoardMeetingWhenFinancialResultsWereApproved")),
        "fy_start": _date(q.get("DateOfStartOfFinancialYear")),
        "quarter": {"start": start, "end": end, "values": numbers(q)},
        "ytd": None,
        "balance": None,
    }
    ytd_end = _date(ytd.get("DateOfEndOfReportingPeriod"))
    ytd_numbers = numbers(ytd)
    if ytd_numbers and ytd_end == end:
        out["ytd"] = {"start": _date(ytd.get("DateOfStartOfReportingPeriod")) or out["fy_start"], "end": end,
                      "values": ytd_numbers}
    balance = numbers(bal)
    if balance.get("Assets") or balance.get("EquityAndLiabilities") or balance.get("CapitalAndLiabilities"):
        out["balance"] = {"date": end, "values": balance}
    return out
