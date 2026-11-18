"""BSE index derivatives: Sensex, Bankex, Sensex 50 and any other index Kite lists on BFO."""

from datetime import date, datetime

import pandas as pd

from desk import options as opt, optstrat
from desk.fno import BSE_INDICES, INDICES, Derivatives, exchange_of


class InstrumentList:
    """Answers search_instruments the way Kite does, from a fixed instrument list."""

    connected, tools = True, {"search_instruments": {}}

    def __init__(self, rows):
        self.rows = rows

    def call(self, tool, arguments, timeout=45.0):
        query, on = arguments["query"], arguments["filter_on"]
        if on == "tradingsymbol":
            return [r for r in self.rows if query.lower() in r["tradingsymbol"].lower()]
        if on == "underlying":
            exchange, name = query.split(":")
            return [r for r in self.rows if r["exchange"] == exchange and r["name"] == name]
        return []


def fut(exchange, name, expiry, lot):
    return {"exchange": exchange, "name": name, "tradingsymbol": f"{name}{expiry[2:4]}{expiry[5:7]}FUT",
            "instrument_type": "FUT", "expiry_date": expiry, "lot_size": lot, "tick_size": 0.05}


def opt_row(exchange, name, expiry, strike, kind, lot):
    return {"exchange": exchange, "name": name, "tradingsymbol": f"{name}{expiry[2:4]}{expiry[5:7]}{strike}{kind}",
            "instrument_type": kind, "strike": strike, "expiry_date": expiry, "lot_size": lot, "tick_size": 0.05}


def test_bse_indices_trade_on_bfo():
    assert set(BSE_INDICES) == {"SENSEX", "BANKEX", "SENSEX50"} and INDICES["SENSEX50"] == "BFO"
    assert exchange_of("SENSEX50") == "BFO" and exchange_of("NIFTY") == "NFO" and exchange_of("RELIANCE") == "NFO"
    assert opt.SPOT["SENSEX50"] == "BSE:SENSEX50"
    assert optstrat.weekly_weekday("SENSEX50", date(2026, 11, 18)) == 3          # BSE expiries are Thursdays


def test_sensex50_and_other_bfo_indices_come_from_kite(tmp_path):
    rows = [fut("NFO", "NIFTY", "2026-11-24", 65), fut("NFO", "RELIANCE", "2026-11-24", 500),
            fut("BFO", "SENSEX50", "2026-11-26", 60), fut("BFO", "SENSEX50", "2026-12-31", 60),
            fut("BFO", "RELIANCE", "2026-11-26", 500),             # a BSE stock future: not an index
            fut("BFO", "NEWBSEIDX", "2026-11-26", 40),             # an index Kite lists that the desk does not trade yet
            opt_row("BFO", "SENSEX50", "2026-11-26", 25800, "CE", 60), opt_row("BFO", "SENSEX50", "2026-11-26", 25900, "PE", 60),
            opt_row("BFO", "SENSEX50", "2026-12-31", 25800, "CE", 60)]
    book = Derivatives(InstrumentList(rows), tmp_path / "fno.json", now=lambda: datetime(2026, 11, 18, 10, 0))
    view = book.refresh()
    by_name = {i["name"]: i for i in view["indices"]}
    sensex50 = by_name["SENSEX50"]
    assert sensex50["exchange"] == "BFO" and sensex50["lot_size"] == 60                # Kite's lot, not a constant
    assert [(e["date"], e["type"]) for e in sensex50["expiries"]] == [("2026-11-26", "monthly"), ("2026-12-31", "monthly")]
    assert by_name["NEWBSEIDX"]["lot_size"] == 40 and by_name["NEWBSEIDX"]["exchange"] == "BFO"
    assert "RELIANCE" not in by_name and [s["name"] for s in view["stocks"]] == ["RELIANCE"]
    assert {c["strike"] for c in book.contracts("SENSEX50")} == {25800, 25900}
    assert book.contract("BFO", "SENSEX502611FUT")["lot_size"] == 60
    assert next(u for u in book.underlyings() if u["name"] == "SENSEX50")["expiries"] == ["2026-11-26", "2026-12-31"]
    assert pd.Timestamp(view["indices"][0]["next"]["date"]) >= pd.Timestamp("2026-11-18")
