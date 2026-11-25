"""Charges part by part on every export, and the tax statement: set-off and carry-forward of losses."""

import io

import pytest

from desk import taxreport as tax
from desk.charges import PARTS, order_charges
from desk.pnl import Ledger


def _parts_sum(row):
    return round(sum(row[k] for k in PARTS), 2)


def test_charges_split_into_parts_that_add_up_on_fills_trades_and_lots(tmp_path):
    ledger = Ledger(tmp_path / "fills.jsonl")
    add = lambda when, side, qty, price, product="CNC", exchange="NSE", symbol="TCS": ledger.add(      # noqa: E731
        "paper", "manual", exchange, symbol, side, qty, price, product, time_ist=f"{when} 10:00:00")
    add("2026-05-04", "BUY", 10, 4000.0)
    add("2026-11-20", "SELL", 10, 4200.0)                                   # delivery: STCG
    add("2026-11-20", "BUY", 50, 1500.0, "MIS", symbol="INFY")
    add("2026-11-20", "SELL", 50, 1510.0, "MIS", symbol="INFY")             # intraday
    add("2026-11-20", "SELL", 65, 200.0, "NRML", "NFO", "NIFTY26D0125800CE")
    add("2026-11-21", "BUY", 65, 120.0, "NRML", "NFO", "NIFTY26D0125800CE")  # short option
    add("2026-11-20", "BUY", 65, 24000.0, "NRML", "NFO", "NIFTY26DECFUT")
    add("2026-11-24", "SELL", 65, 24100.0, "NRML", "NFO", "NIFTY26DECFUT")  # future
    from desk.pnl import settle
    for fill in settle(ledger.fills()):
        assert round(sum(fill["parts"].values()), 2) == pytest.approx(fill["charges"], abs=0.011)
    # On an equity delivery buy the parts are the charge schedule's own: GST on brokerage and fees, none on STT.
    buy = settle(ledger.fills())[0]["parts"]
    expected = order_charges("BUY", 10, 4000.0, "CNC")
    assert buy["stt"] == pytest.approx(expected.stt) and buy["stamp_duty"] == pytest.approx(expected.stamp_duty)
    assert buy["gst"] == pytest.approx((expected.brokerage + expected.exchange_txn + expected.sebi) * 0.18)
    for trade in ledger.closed_trades("paper"):
        assert _parts_sum(trade) == pytest.approx(trade["charges"], abs=0.011), trade["symbol"]
    lots = tax.tax_lots(ledger.fills(), "paper")
    assert len(lots) == 4
    for lot in lots:
        assert _parts_sum(lot) == lot["charges"], lot["symbol"]
    by = {lot["symbol"]: lot for lot in lots}
    tcs = by["TCS"]
    assert tcs["category"] == "STCG" and tcs["stt"] == pytest.approx(0.001 * (40000 + 42000), abs=0.02)
    assert tcs["dp"] == pytest.approx(15.34) and tcs["taxable_gain"] == pytest.approx(tcs["gain"] + tcs["stt"], abs=0.01)
    assert by["INFY"]["taxable_gain"] == by["INFY"]["gain"]                  # business income: STT is a cost
    assert by["NIFTY26D0125800CE"]["stt"] == pytest.approx(0.0015 * 65 * 200, abs=0.02)
    # Same in P&L and tax lots, part by part.
    trades = {t["symbol"]: t for t in ledger.closed_trades("paper")}
    for symbol, lot in by.items():
        for k in PARTS:
            assert trades[symbol][k] == pytest.approx(lot[k], abs=0.02), (symbol, k)
    summary, = tax.summary(lots)
    assert summary["STCG"] == tcs["taxable_gain"] and summary["stt"] == pytest.approx(sum(lot["stt"] for lot in lots))


def _lot(fy, category, gain, turnover=0.0):
    return {"fy": fy, "category": category, "gain": gain, "taxable_gain": gain, "sell_value": turnover, "buy_value": 0.0,
            "charges": 0.0}


def test_statement_sets_off_losses_under_each_heads_rules():
    lots = [_lot("2025-26", "F&O (non-speculative)", -50000, 50000), _lot("2025-26", "STCG", 20000),
            _lot("2025-26", "Speculative (intraday)", -5000, 5000),
            _lot("2026-27", "F&O (non-speculative)", 40000, 40000), _lot("2026-27", "Speculative (intraday)", 3000, 3000),
            _lot("2026-27", "LTCG", 200000)]
    st = tax.statement(lots, [{"fy": "2020-21", "head": "spec", "amount": 1000},
                              {"fy": "2023-24", "head": "ltcg", "amount": 30000}], slab_rate=0.30)
    years = {y["fy"]: y for y in st["years"]}
    assert list(years) == ["2024-25", "2025-26", "2026-27"]       # quiet years left out; 2024-25 has a lapse
    assert years["2024-25"]["expired"] == 1000                     # an intraday loss lasts 4 years
    y = years["2025-26"]
    # The F&O loss takes the short-term gain; an intraday loss never touches F&O or capital gains.
    assert (y["fno_vs_stcg"], y["fno_after"], y["spec_after"], y["stcg_after"]) == (20000, -30000, -5000, 0)
    assert (y["cf_fno"], y["cf_spec"], y["cf_ltcg"], y["total_tax"]) == (30000, 5000, 30000, 0)
    y = years["2026-27"]
    assert (y["bf_spec"], y["bf_fno"], y["bf_ltcl"]) == (3000, 30000, 30000)
    assert (y["business_income"], y["ltcg_after"], y["ltcg_exemption"], y["ltcg_taxable"]) == (10000, 170000, 125000, 45000)
    assert y["total_tax"] == pytest.approx((10000 * 0.30 + 45000 * 0.125) * 1.04)
    assert y["cf_spec"] == 2000 and y["cf_fno"] == 0
    left = {(row["fy"], row["head"]): row for row in st["losses"]}
    assert left[("2025-26", "spec")]["status"] == "Carried forward" and left[("2025-26", "spec")]["last_year"] == "2029-30"
    assert left[("2020-21", "spec")]["status"] == "Lapsed after FY 2024-25"
    assert left[("2025-26", "fno")]["status"] == "Used up" and left[("2025-26", "fno")]["last_year"] == "2033-34"


def test_statement_capital_losses_never_reduce_business_income():
    lots = [_lot("2026-27", "STCG", -40000), _lot("2026-27", "LTCG", -10000), _lot("2026-27", "LTCG", 15000),
            _lot("2026-27", "F&O (non-speculative)", 60000, 60000)]
    y, = tax.statement(lots)["years"]
    # Net long-term +5,000 takes 5,000 of the short-term loss; the rest is carried, F&O stays taxable in full.
    assert (y["stcl_vs_ltcg"], y["ltcg_after"], y["fno_after"], y["cf_stcg"]) == (5000, 0, 60000, 35000)
    # A long-term loss is never set off against short-term gains.
    y, = tax.statement([_lot("2026-27", "LTCG", -10000), _lot("2026-27", "STCG", 8000)])["years"]
    assert (y["stcg_after"], y["cf_ltcg"]) == (8000, 10000)
    assert y["stcg_tax"] == pytest.approx(1600)


def test_statement_flags_the_audit_limit_and_overlapping_losses():
    st = tax.statement([_lot("2026-27", "F&O (non-speculative)", 1e8 + 1, 1e8 + 1)],
                       [{"fy": "2026-27", "head": "fno", "amount": 500}])
    assert st["years"][0]["audit"] and any("audit" in n for n in st["notes"])
    assert any("Both are counted" in n for n in st["notes"])
    assert tax.statement([], [])["years"] == []


@pytest.mark.parametrize("bad", [[{"fy": "2024-26", "head": "fno", "amount": 1}], [{"fy": "2024-25", "head": "salary", "amount": 1}],
                                 [{"fy": "2024-25", "head": "fno", "amount": -5}], [{"fy": "2024-25", "head": "fno", "amount": "x"}],
                                 "nope"])
def test_earlier_losses_are_checked(bad):
    with pytest.raises(ValueError):
        tax.clean_carried(bad)


def test_tax_inputs_save_and_reach_the_statement_and_excel(tmp_path):
    path = tmp_path / "tax_inputs.json"
    assert tax.load_inputs(path) == {"losses": [], "slab_rate_pct": 30.0}
    saved = tax.save_inputs(path, {"losses": [{"fy": "2025-26", "head": "stcg", "amount": "1200.5"}], "slab_rate_pct": 20})
    assert saved == tax.load_inputs(path) == {"losses": [{"fy": "2025-26", "head": "stcg", "amount": 1200.5}], "slab_rate_pct": 20.0}
    with pytest.raises(ValueError):
        tax.save_inputs(path, {"losses": [], "slab_rate_pct": 80})
    openpyxl = pytest.importorskip("openpyxl")
    from desk import xlsxexport as x
    lots = [_lot("2026-27", "STCG", 5000)]
    st = tax.statement(lots, saved["losses"], 0.2)
    book = openpyxl.load_workbook(io.BytesIO(x.workbook("tax-lots", lots, "paper", statement=st)))
    ws = book["Tax statement"]
    assert ws["B4"].value == "FY 2026-27" and "practice figures" in ws["A2"].value
    labels = {ws.cell(r, 1).value: r for r in range(5, ws.max_row + 1)}
    assert ws.cell(labels["Short-term capital losses (against short- or long-term gains)"], 2).value == 1200.5
    assert ws.cell(labels["Total estimated tax"], 2).value == pytest.approx((5000 - 1200.5) * 0.2 * 1.04, abs=0.01)
    losses = book["Losses carried forward"]
    assert losses["A5"].value == "FY 2025-26" and losses["C5"].value == "You entered" and losses["H5"].value == "Used up"
