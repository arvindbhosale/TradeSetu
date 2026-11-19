"""Company financials from NSE results filings (XBRL), and their use in Results Drift."""

import pandas as pd
import pytest

from desk import financials as fin, markettools as mt, xbrl

pytestmark = pytest.mark.unit

CR = 1e7
NS = {"old": "http://www.bseindia.com/xbrl/fin/2020-03-31/in-bse-fin", "new": "http://www.sebi.gov.in/xbrl/2025-01-31/in-capmkt"}


def filing(end, *, quarter, ytd=None, balance=None, basis="Consolidated", symbol="ABC", ns="old", contexts=True,
           extra_q=None):
    """A results XBRL instance in the exchanges' layout: OneD quarter, FourD year to date, OneI balance sheet."""
    start_q = (pd.Timestamp(end) - pd.offsets.QuarterBegin(startingMonth=1)).strftime("%Y-%m-%d")
    end_ts = pd.Timestamp(end)
    fy_start = f"{end_ts.year - (1 if end_ts.month <= 3 else 0)}-04-01"
    parts = [f'<xbrli:xbrl xmlns:xbrli="http://www.xbrl.org/2003/instance" xmlns:in-x="{NS[ns]}">']
    if contexts:
        parts.append(f'<xbrli:context id="OneD"><xbrli:period><xbrli:startDate>{start_q}</xbrli:startDate>'
                     f'<xbrli:endDate>{end}</xbrli:endDate></xbrli:period></xbrli:context>')
        # a dimensional context that must be ignored
        parts.append('<xbrli:context id="OneOperatingExpenses01D"><xbrli:segment/></xbrli:context>')

    def fact(context, name, value):
        parts.append(f'<in-x:{name} contextRef="{context}" unitRef="INR" decimals="-5">{value}</in-x:{name}>')

    for name, value in {"Symbol": symbol, "NameOfTheCompany": "ABC Limited", "NatureOfReportStandaloneConsolidated": basis,
                        "WhetherResultsAreAuditedOrUnaudited": "Audited", "DateOfStartOfReportingPeriod": start_q,
                        "DateOfEndOfReportingPeriod": end, "DateOfStartOfFinancialYear": fy_start,
                        "DateOfBoardMeetingWhenFinancialResultsWereApproved": (end_ts + pd.Timedelta(days=30)).strftime("%Y-%m-%d"),
                        **quarter, **(extra_q or {})}.items():
        fact("OneD", name, value)
    fact("OneOperatingExpenses01D", "OtherExpenses", 1)
    if ytd:
        fact("FourD", "DateOfStartOfReportingPeriod", fy_start)
        fact("FourD", "DateOfEndOfReportingPeriod", end)
        for name, value in ytd.items():
            fact("FourD", name, value)
    for name, value in (balance or {}).items():
        fact("OneI", name, value)
    parts.append("</xbrli:xbrl>")
    return "".join(parts).encode()


def pl(sales, profit, *, eps=None, scale=CR):
    """A quarter's Ind-AS profit and loss in crore: expenses 80% of sales, interest 2, depreciation 3."""
    expenses = sales * 0.8 + 5
    pbt = sales + 1 - expenses
    return {"RevenueFromOperations": sales * scale, "OtherIncome": 1 * scale, "Income": (sales + 1) * scale,
            "FinanceCosts": 2 * scale, "DepreciationDepletionAndAmortisationExpense": 3 * scale, "Expenses": expenses * scale,
            "ProfitBeforeTax": pbt * scale, "TaxExpense": (pbt - profit) * scale, "ProfitLossForPeriod": profit * scale,
            "ProfitOrLossAttributableToOwnersOfParent": profit * scale, "PaidUpValueOfEquityShareCapital": 10 * scale,
            "FaceValueOfEquityShareCapital": 10, "BasicEarningsLossPerShareFromContinuingAndDiscontinuedOperations":
                eps if eps is not None else round(profit / 1, 2)}


def sheet(equity, borrowings=50):
    total = equity + borrowings + 100
    return {"EquityShareCapital": 10 * CR, "OtherEquity": (equity - 10) * CR, "EquityAttributableToOwnersOfParent": equity * CR,
            "Equity": equity * CR, "BorrowingsNoncurrent": borrowings * CR, "PropertyPlantAndEquipment": 200 * CR,
            "CapitalWorkInProgress": 10 * CR, "NoncurrentInvestments": 30 * CR, "Inventories": 40 * CR,
            "TradeReceivablesCurrent": 50 * CR, "CashAndCashEquivalents": 20 * CR, "CurrentAssets": 150 * CR,
            "CurrentLiabilities": 75 * CR, "Assets": total * CR, "EquityAndLiabilities": total * CR}


CASH = {"CashFlowsFromUsedInOperatingActivities": 120 * CR, "CashFlowsFromUsedInInvestingActivities": -60 * CR,
        "CashFlowsFromUsedInFinancingActivities": -40 * CR, "IncreaseDecreaseInCashAndCashEquivalents": 20 * CR,
        "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities": 50 * CR}


# -- the XBRL reader -------------------------------------------------------------------------------

def test_a_q4_filing_gives_the_quarter_the_year_and_the_march_balance_sheet():
    content = filing("2025-03-31", quarter=pl(100, 10), ytd={**pl(380, 36), **CASH}, balance=sheet(400))
    parsed = xbrl.parse(content)
    assert parsed["symbol"] == "ABC" and parsed["basis"] == "consolidated" and parsed["bank"] is False
    assert parsed["quarter"]["start"] == "2025-01-01" and parsed["quarter"]["end"] == "2025-03-31"
    assert parsed["quarter"]["values"]["RevenueFromOperations"] == 100 * CR
    assert "OtherExpenses" not in parsed["quarter"]["values"]                      # the dimensional fact is ignored
    assert parsed["ytd"]["start"] == "2024-04-01" and parsed["ytd"]["values"]["RevenueFromOperations"] == 380 * CR
    assert parsed["balance"]["date"] == "2025-03-31" and parsed["balance"]["values"]["Equity"] == 400 * CR


def test_the_reader_needs_no_context_definitions_and_reads_the_2025_namespace():
    parsed = xbrl.parse(filing("2024-12-31", quarter=pl(90, 8), basis="Standalone", ns="new", contexts=False))
    assert parsed["basis"] == "standalone" and parsed["quarter"]["start"] == "2024-10-01"
    assert parsed["ytd"] is None and parsed["balance"] is None


def test_bank_format_is_recognised():
    bank_q = {"InterestEarned": 700 * CR, "InterestExpended": 400 * CR, "OperatingExpenses": 150 * CR,
              "ProvisionsOtherThanTaxAndContingencies": 30 * CR, "OtherIncome": 100 * CR,
              "ProfitLossFromOrdinaryActivitiesBeforeTax": 220 * CR, "TaxExpense": 55 * CR, "ProfitLossForThePeriod": 165 * CR,
              "BasicEarningsPerShareAfterExtraordinaryItems": 21.5, "PercentageOfGrossNpa": 0.0136, "PercentageOfNpa": 0.0041}
    parsed = xbrl.parse(filing("2024-09-30", quarter=bank_q, basis="Standalone"))
    assert parsed["bank"] is True
    p = fin.derive(fin.profit_loss(parsed["quarter"]["values"], True), True)
    assert p["financing_profit"] == 120 * CR and p["gross_npa_pct"] == pytest.approx(1.36)
    assert p["pbt"] == p["financing_profit"] + p["other_income"]                    # the bank P&L adds up


def test_not_a_results_file_is_refused():
    with pytest.raises(xbrl.NotAResultsFiling):
        xbrl.parse(b"<html>blocked</html>")
    with pytest.raises(xbrl.NotAResultsFiling):
        xbrl.parse(b"not xml at all")


# -- statements -------------------------------------------------------------------------------------

def company():
    """Five quarters to Mar 2025 plus the Mar 2024 and Sep 2024 filings with balance sheets."""
    return [xbrl.parse(c) for c in (
        filing("2024-03-31", quarter=pl(90, 8), ytd={**pl(340, 30), **CASH}, balance=sheet(360)),
        filing("2024-06-30", quarter=pl(92, 9)),
        filing("2024-09-30", quarter=pl(94, 9), ytd={**pl(186, 18), **CASH}, balance=sheet(380)),
        filing("2024-12-31", quarter=pl(96, 10)),
        filing("2025-03-31", quarter=pl(100, 12), ytd={**pl(382, 40), **CASH}, balance=sheet(400)),
    )]


def test_statements_build_quarters_years_ttm_balance_sheet_cash_flow_and_ratios():
    out = fin.statements(company(), "consolidated", price=500.0)
    assert out["available"] and not out["bank"]
    q = out["quarterly"]
    assert q["columns"] == ["Mar 2024", "Jun 2024", "Sep 2024", "Dec 2024", "Mar 2025"]
    sales = next(r for r in q["rows"] if r["key"] == "sales")
    assert sales["values"] == [90, 92, 94, 96, 100]
    opm = next(r for r in q["rows"] if r["key"] == "opm")
    assert opm["values"][-1] == pytest.approx(20.0)              # expenses 85 less interest 2 and depreciation 3
    p = out["profit_loss"]
    assert p["columns"] == ["Mar 2024", "Mar 2025", "TTM"]
    assert next(r for r in p["rows"] if r["key"] == "sales")["values"] == [340, 382, 382]   # TTM = last four quarters
    assert next(r for r in p["rows"] if r["key"] == "owners_profit")["values"] == [30, 40, 40]
    assert out["balance_sheet"]["columns"] == ["Mar 2024", "Mar 2025"]               # Sep 2024 is older than the latest March
    total = [r for r in out["balance_sheet"]["rows"] if r["key"] == "total"]
    assert len(total) == 2 and total[0]["values"] == [510, 550]
    assert out["cash_flow"]["columns"] == ["Mar 2024", "Mar 2025"]
    fcf = next(r for r in out["cash_flow"]["rows"] if r["key"] == "free_cash_flow")
    assert fcf["values"] == [70, 70]
    ratios = {r["key"]: r["values"] for r in out["ratios"]["rows"]}
    assert ratios["roe"][-1] == pytest.approx(40 / 380 * 100, abs=0.01)              # on average equity (360 + 400) / 2
    assert ratios["sales_growth"][-1] == pytest.approx((382 / 340 - 1) * 100, abs=0.01)
    assert ratios["debt_equity"][-1] == pytest.approx(50 / 400, abs=0.01)
    s = out["summary"]
    assert s["shares"] == 1e7 and s["market_cap"] == 500 * 1e7
    assert s["pe"] == pytest.approx(500 * 1e7 / (40 * CR), abs=0.01)
    assert s["quarter_sales_yoy"] == pytest.approx((100 / 90 - 1) * 100, abs=0.01)


def test_a_september_newer_than_the_last_march_adds_a_half_year_column():
    filings = [f for f in company() if f["quarter"]["end"] <= "2024-09-30"]
    out = fin.statements(filings, "consolidated")
    assert out["balance_sheet"]["columns"] == ["Mar 2024", "Sep 2024"]
    assert out["cash_flow"]["columns"] == ["Mar 2024", "Apr–Sep 2024 (6 months)"]
    assert "TTM" not in out["profit_loss"]["columns"]                               # only three quarters known
    assert fin.statements(filings, "standalone") == {"available": False}


# -- the filings lists ---------------------------------------------------------------------------

def test_listing_reads_both_nse_lists_keeps_revisions_and_skips_rows_without_xbrl():
    old = [{"toDate": "31-Dec-2024", "fromDate": "01-Oct-2024", "consolidated": "Non-Consolidated",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/A.xml", "broadCastDate": "16-Jan-2025 20:20:21"},
           {"toDate": "31-Dec-2024", "fromDate": "01-Oct-2024", "consolidated": "Non-Consolidated",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/A2.xml", "broadCastDate": "20-Jan-2025 10:00:00"},
           {"toDate": "31-Mar-2024", "fromDate": "01-Apr-2023", "consolidated": "Consolidated",
            "xbrl": "https://nsearchives.nseindia.com/corporate/xbrl/Y.xml", "broadCastDate": "22-Apr-2024 07:47:04"},
           {"toDate": "30-Sep-2024", "consolidated": "Consolidated", "xbrl": "-"}]
    new = {"data": [{"qe_Date": "30-SEP-2025", "consolidated": "Consolidated", "xbrl": "https://nsearchives.nseindia.com/x/B.xml",
                     "broadcast_Date": "17-Oct-2025 19:32:59"}], "totalCount": 1}
    rows = fin.parse_listing(old) + fin.parse_listing(new)
    assert [(r["end"], r["basis"], r["xbrl"].rsplit("/", 1)[1]) for r in rows] == [
        ("2024-12-31", "standalone", "A2.xml"), ("2025-09-30", "consolidated", "B.xml")]


def test_wanted_reads_twelve_quarters_every_march_and_the_latest_september():
    ends = pd.date_range("2019-03-31", "2025-12-31", freq="QE").strftime("%Y-%m-%d")[::-1]
    listing = [{"end": e, "basis": "consolidated", "xbrl": f"https://x/{e}.xml", "when": e} for e in ends]
    picked = [r["end"] for r in fin.wanted(listing, "consolidated")]
    assert picked[:12] == list(ends[:12]) and "2019-03-31" in picked and "2019-09-30" not in picked
    assert [r["end"] for r in fin.wanted(listing, "consolidated", "results")] == list(ends[:5])


# -- the service --------------------------------------------------------------------------------

class FakeNse:
    def __init__(self, files, comparison=None):
        self.files, self.comparison, self.calls = files, comparison or {}, []

    def json(self, path, params):
        self.calls.append(path)
        if path == "corporates-financial-results":
            return [{"toDate": pd.Timestamp(end).strftime("%d-%b-%Y"), "consolidated": "Consolidated",
                     "xbrl": f"https://nsearchives.nseindia.com/corporate/xbrl/{end}.xml", "broadCastDate": f"{end} 18:00:00"}
                    for end in self.files]
        if path == "integrated-filing-results":
            raise OSError("not found")
        if path == "results-comparision":
            return self.comparison
        raise AssertionError(path)

    def file(self, url):
        self.calls.append("file")
        end = url.rsplit("/", 1)[1].removesuffix(".xml")
        content = self.files[end]
        if isinstance(content, Exception):
            raise content
        return content


def test_financials_download_once_keep_on_disk_and_say_what_failed(tmp_path):
    raw = {f["quarter"]["end"]: None for f in company()}
    files = {"2024-03-31": filing("2024-03-31", quarter=pl(90, 8), ytd={**pl(340, 30), **CASH}, balance=sheet(360)),
             "2024-06-30": filing("2024-06-30", quarter=pl(92, 9)),
             "2024-09-30": OSError("HTTP 403"),
             "2024-12-31": filing("2024-12-31", quarter=pl(96, 10)),
             "2025-03-31": filing("2025-03-31", quarter=pl(100, 12), ytd={**pl(382, 40), **CASH}, balance=sheet(400))}
    assert set(raw) == set(files)
    nse = FakeNse(files)
    store = fin.Financials(client=nse, path=tmp_path)
    first = store.view("ABC.NS", price=500.0)
    assert first["state"] == "loading" and not first["available"]
    store.work()
    ready = store.view("ABC.NS", price=500.0)
    assert ready["state"] == "ready" and ready["available"] and ready["basis"] == "consolidated"
    assert "Sep 2024" in ready["note"] and "403" in ready["note"]
    assert ready["quarterly"]["columns"] == ["Mar 2024", "Jun 2024", "Dec 2024", "Mar 2025"]
    downloads = nse.calls.count("file")
    again = fin.Financials(client=nse, path=tmp_path)                       # a restart reads the disk
    assert again.view("ABC.NS")["available"]
    again.work()
    assert nse.calls.count("file") == downloads                            # nothing read twice, failure not retried yet
    assert fin.Financials(client=nse, path=tmp_path).view("ABC.BO")["note"].startswith("Financials come from NSE")


def test_results_for_drift_use_the_comparison_and_fall_back_to_filings(tmp_path):
    comparison = {"resCmpData": [
        {"re_from_dt": "01-JUL-2026", "re_to_dt": "30-SEP-2026", "re_create_dt": "20-OCT-2026", "re_net_sale": "13000",
         "re_net_profit": "1200", "re_basic_eps_for_cont_dic_opr": "6.5"},
        {"re_from_dt": "01-APR-2026", "re_to_dt": "30-JUN-2026", "re_net_sale": "12500", "re_net_profit": "1100"},
        {"re_from_dt": "01-JUL-2025", "re_to_dt": "30-SEP-2025", "re_net_sale": "11000", "re_net_profit": "900"}]}
    store = fin.Financials(client=FakeNse({}, comparison), path=tmp_path)
    assert store.want_results([("ABC.NS", "2026-10-20")]) == 1
    assert store.want_results([("ABC.NS", "2026-10-20")]) == 0                         # already queued
    store.work()
    got = store.results("ABC.NS", "2026-10-20")
    assert got["quarter"] == "Sep 2026" and got["sales"] == 130.0 and got["profit"] == 12.0      # lakhs to crore
    assert got["sales_yoy"] == pytest.approx(18.2) and got["profit_yoy"] == pytest.approx(33.3)
    assert got["verdict"] == "Strong"
    assert store.want_results([("ABC.NS", "2026-10-20")]) == 0                         # known: not asked again
    assert store.results("ABC.NS", "2027-02-10") is None                               # a later quarter is not out yet

    files = {"2025-03-31": filing("2025-03-31", quarter=pl(100, 12)), "2026-03-31": filing("2026-03-31", quarter=pl(90, 6))}
    nse = FakeNse(files, {"resCmpData": []})
    fallback = fin.Financials(client=nse, path=tmp_path / "b")
    fallback.want_results([("ABC.NS", "2026-05-02")])
    fallback.work()
    got = fallback.results("ABC.NS", "2026-05-02")
    assert got["profit_yoy"] == pytest.approx(-50.0) and got["verdict"] == "Weak" and nse.calls.count("file") == 2


def test_verdicts():
    assert fin.verdict(10, 20, 12, 10) == "Strong"
    assert fin.verdict(-8, -5, 9, 9.5) == "Weak"
    assert fin.verdict(5, 5, 10, 9.5) == "Mixed"
    assert fin.verdict(None, None, 5, -3) == "Strong"                                  # loss to profit
    assert fin.verdict(None, None, -5, 3) == "Weak"
    assert fin.growth(10, -5) is None


def test_drift_rows_say_whether_the_results_back_the_move():
    up = mt.results_columns({"quarter": "Sep 2026", "sales_yoy": 18.2, "profit_yoy": 33.3, "profit": 12.0, "verdict": "Strong"},
                            "Drifting up")
    assert up["agrees"] is True and up["profit_yoy"] == 33.3
    assert mt.results_columns({"verdict": "Strong"}, "Holding down")["agrees"] is False
    assert mt.results_columns({"verdict": "Mixed"}, "Drifting up")["agrees"] is None
    assert mt.results_columns(None, "No edge")["verdict"] is None


def test_peers_are_the_same_industry_biggest_first():
    industry = {"A.NS": "IT", "B.NS": "IT", "C.NS": "Banks", "D.NS": "IT"}
    facts = {"A.NS": {"market_cap": 5e12, "pe": 25.0}, "B.NS": {"market_cap": 9e12, "pe": 30.0}}
    out = fin.peer_rows("D.NS", industry, {"A.NS": "A Ltd"}, facts.get, lambda s: 100.0, limit=2)
    assert out["industry"] == "IT" and [r["symbol"] for r in out["rows"]] == ["B.NS", "A.NS", "D.NS"]
    assert out["rows"][-1]["this"] and out["missing"] == ["D.NS"] and out["count"] == 3
    assert fin.peer_rows("X.NS", industry, {}, facts.get, lambda s: None)["rows"] == []


def test_financials_api_says_why_when_nse_cannot_be_reached(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    app = create_app(settings=Settings(tmp_path / "settings.json"), token="t0ken", fetch=lambda symbol, interval="1d": None)
    with TestClient(app, headers={"X-Desk-Token": "t0ken"}) as client:
        first = client.get("/api/financials?symbol=TCS").json()
        assert first["state"] == "loading" and first["symbol"] == "TCS" and "peers" in first
        app.state.financials.work()
        after = client.get("/api/financials?symbol=TCS").json()
        assert after["state"] == "error" and "could not be read" in after["note"] and not after["available"]
        assert client.post("/api/financials/refresh", json={"symbol": "TCS"}).json() == {"queued": True}
        assert "not an NSE/BSE stock" in client.get("/api/financials?symbol=NIFTY").json()["error"]
        assert client.get("/api/tools/drift").json()["with_numbers"] == 0
