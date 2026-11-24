"""Backtest results as Excel: every sheet the page needs, native charts, and the phone-app download hand-off."""

import io

import pytest

openpyxl = pytest.importorskip("openpyxl")
pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from desk import btexport  # noqa: E402
from tests.test_desk_markets import client  # noqa: E402,F401  (the fixture)

CHECKS = [{"tone": "warn", "text": "Only 17 closed trades."}, {"tone": "ok", "text": "=cmd|' /C calc'!A0"}]


def _book(data: bytes):
    return openpyxl.load_workbook(io.BytesIO(data))


def _export(client, result, **extra):  # noqa: F811
    response = client.post("/api/backtest/export.xlsx", json={"result": result, "checks": CHECKS, **extra})
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("application/vnd.openxmlformats")
    return response, _book(response.content)


def test_stock_backtest_workbook_matches_the_page(client):  # noqa: F811
    body = {"symbol": "reliance", "start": "2023-06-01", "end": "2026-11-01", "strategy": "ema_cross",
            "steps": {"unit": "pct", "steps": [{"at": 5, "size": 50}]}}
    r = client.post("/api/backtest/strategy", json=body).json()
    assert r.get("trades"), r.get("error")
    response, book = _export(client, r, notes=["How to read this."])
    assert 'filename="tradesetu-backtest-reliance-' in response.headers["content-disposition"]
    assert book.sheetnames == ["Summary", "Charts", "Trades", "Monthly", "Yearly", "Equity curve", "Settings"]

    summary = book["Summary"]
    labels = {summary.cell(i, 1).value: summary.cell(i, 2).value for i in range(1, summary.max_row + 1)}
    m = r["metrics"]
    assert labels["Total return"] == m["total_return_pct"] and labels["Final value"] == m["final_value"]
    assert labels["Charges paid"] == m["total_charges"]
    texts = [summary.cell(i, 2).value for i in range(1, summary.max_row + 1)]
    assert "Only 17 closed trades." in texts
    trap = next(summary.cell(i, 2) for i in range(1, summary.max_row + 1) if summary.cell(i, 2).value == CHECKS[1]["text"])
    assert trap.data_type == "s"                                  # the page's text never runs as a formula
    assert any(summary.cell(i, 1).value == "Book profit in steps, against one exit" for i in range(1, summary.max_row + 1))

    trades = book["Trades"]
    head = [c.value for c in trades[4]]
    pnl = head.index("Net Profit/Loss") + 1
    rows = [trades.cell(i, pnl).value for i in range(5, 5 + len(r["trades"]))]
    assert rows == [t["pnl"] for t in r["trades"]]                   # oldest first, every trade
    running = trades.cell(4 + len(r["trades"]), head.index("Running total") + 1).value
    assert running == pytest.approx(sum(t["pnl"] for t in r["trades"]), abs=0.05)
    assert "Booked in steps" in head and trades.cell(5, head.index("Entry") + 1).is_date

    equity = book["Equity curve"]
    assert equity.max_row - 4 == len({d for d, _ in r["equity"]})
    falls = [equity.cell(i, 5).value for i in range(5, equity.max_row + 1)]
    assert min(falls) == pytest.approx(m["max_drawdown_pct"], abs=0.05) and max(falls) == 0

    monthly = book["Monthly"]
    total = sum(monthly.cell(i, 4).value or 0 for i in range(5, monthly.max_row) if isinstance(monthly.cell(i, 4).value, float))
    assert total == pytest.approx(m["final_value"] - r["params"]["capital"], abs=1)

    charts = book["Charts"]._charts
    assert len(charts) == 5                                         # value, fall, months, years, each trade
    settings = {row[0].value: row[1].value for row in book["Settings"].iter_rows(min_row=5)}
    assert settings["Strategy"] == "EMA crossover" and settings["Capital"] == r["params"]["capital"]


@pytest.mark.parametrize("path,body,trades_sheet", [
    ("/api/backtest/options", {"underlying": "NIFTY", "start": "2024-06-01", "end": "2026-11-01", "strategy": "supertrend",
                               "side": "calls", "capital": 300000}, "Trades"),
    ("/api/backtest/futures", {"underlying": "NIFTY", "start": "2024-01-01", "end": "2026-11-13", "strategy": "supertrend",
                               "direction": "both"}, "Trades"),
    ("/api/backtest/portfolio", {"portfolio": "all_weather_long"}, "ETFs"),
])
def test_every_backtest_kind_exports(client, path, body, trades_sheet):  # noqa: F811
    r = client.post(path, json=body).json()
    assert "metrics" in r, r.get("error")
    _, book = _export(client, r)
    assert trades_sheet in book.sheetnames and book["Charts"]._charts
    if trades_sheet == "Trades":
        head = [c.value for c in book["Trades"][4]]
        assert "Contract" in head or "Side" in head


def test_option_strategy_workbook_names_the_legs():
    r = {"structure": "iron_condor", "simulated": True, "symbol": "NIFTY", "interval": "1d", "mode": "positional",
         "strategy": {"name": "Iron condor · positional"}, "legs_text": "Sell ATM+2 CE, Buy ATM+6 CE",
         "period": {"start": "2025-07-01", "end": "2025-07-04"}, "params": {"capital": 100000, "lots": 1},
         "metrics": {"total_return_pct": 1.0, "final_value": 101000, "trades": 1}, "buy_hold": {},
         "equity": [["2025-07-01", 100000], ["2025-07-02", 99000], ["2025-07-04", 101000]],
         "trades": [{"contract": "NIFTY 03 Jul −18200CE +18400CE", "entry_date": "2025-07-01", "exit_date": "2025-07-03",
                     "quantity": 65, "lots": 1, "entry_price": 66.76, "exit_price": 18559.36, "pnl": 1000, "return_pct": 7.7,
                     "days": 2, "exit": "expiry", "legs_stopped": 0, "reentries": 0, "margin": 13000.0, "open": False}]}
    book = _book(btexport.workbook(r))
    head = [c.value for c in book["Trades"][4]]
    assert "Net credit (+) / debit (−) per unit" in head and "Legs stopped out" in head
    assert book["Trades"].cell(5, head.index("Exit reason") + 1).value == "Expiry"
    settings = {row[0].value: row[1].value for row in book["Settings"].iter_rows(min_row=5)}
    assert settings["Legs"] == "Sell ATM+2 CE, Buy ATM+6 CE" and settings["Trade type"] == "Positional"
    assert book["Equity curve"].cell(6, 4).value == -1.0 and book["Equity curve"].cell(6, 5).value == -1000


def test_export_without_a_result_says_so(client):  # noqa: F811
    response = client.post("/api/backtest/export.xlsx", json={"result": {"error": "x"}})
    assert response.status_code == 400 and "Run a backtest first" in response.json()["error"]


def test_phone_download_handoff(client):  # noqa: F811
    data = b"PK\x03\x04 workbook bytes"
    made = client.post("/api/download/handoff", content=data,
                       headers={"Content-Type": "application/vnd.ms-excel", "X-File-Name": 'a"b/../c.xlsx'}).json()
    url = made["url"]
    assert url.startswith("/dl/") and len(url) > 40
    got = client.get(url, headers={"X-Desk-Token": ""})            # the phone's browser sends no desk token
    assert got.status_code == 200 and got.content == data
    assert got.headers["content-disposition"] == 'attachment; filename="a-b-..-c.xlsx"'
    assert client.get("/dl/not-a-token").status_code == 404
    assert client.post("/api/download/handoff", content=b"").status_code == 400
