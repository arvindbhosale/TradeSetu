"""Zerodha's margin through the Kite server's get_basket_margins tool: the calculator, F&O algo entries and starts."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

from desk import margin
from desk.fno import Derivatives
from desk.kite import KiteError
from tests.test_desk import client  # noqa: F401 - the fixture
from tests.test_desk_fno_algos import CONDOR, Clock, Market, OfflineKite, StubKite, setup

pytestmark = pytest.mark.unit


def answer(final=60_000.0, initial=180_000.0):
    leg = lambda total: {"span": total * 0.7, "exposure": total * 0.3, "option_premium": 0, "total": total}  # noqa: E731
    return {"initial": {"span": initial * 0.7, "exposure": initial * 0.3, "option_premium": 1500.0, "total": initial},
            "final": {"span": final * 0.6, "exposure": final * 0.3, "option_premium": 1500.0, "additional": 0, "total": final},
            "orders": [leg(1500.0), leg(178_500.0)]}


class MarginKite:
    """A logged-in Kite whose server has the desk's margin tool."""

    def __init__(self, reply=None, error=None):
        self.connected, self.tools = True, {"get_basket_margins": {}}
        self.reply, self.error, self.calls = reply or answer(), error, []

    def call(self, tool, arguments=None, timeout=45.0):
        self.calls.append((tool, arguments))
        if self.error:
            raise self.error
        return self.reply


LEGS = [{"exchange": "NFO", "tradingsymbol": "nifty26nov24900pe", "side": "BUY", "quantity": 65, "price": 23.5},
        {"exchange": "NFO", "tradingsymbol": "NIFTY26NOV25000PE", "side": "SELL", "quantity": 65}]


def test_basket_asks_zerodha_and_reports_the_hedge_benefit():
    kite = MarginKite()
    out = margin.basket(kite, LEGS, product="MIS")
    tool, args = kite.calls[0]
    assert tool == "get_basket_margins" and args["consider_positions"] is False
    assert args["orders"][0] == {"exchange": "NFO", "tradingsymbol": "NIFTY26NOV24900PE", "transaction_type": "BUY",
                                 "quantity": 65, "product": "MIS", "variety": "regular", "order_type": "LIMIT",
                                 "price": 23.5}
    assert args["orders"][1]["order_type"] == "MARKET" and "price" not in args["orders"][1]
    assert out["source"] == "kite" and out["total"] == 60_000 and out["without_hedge"] == 180_000
    assert out["hedge_benefit"] == 120_000 and out["premium"] == 1500
    assert [r["total"] for r in out["legs"]] == [1500, 178_500] and out["legs"][1]["side"] == "SELL"


@pytest.mark.parametrize("kite, fragment, login", [
    (OfflineKite(), "Log in to Kite", True),
    (type("K", (), {"connected": True, "tools": {"get_ltp": {}}})(), "no margin calculator", False),
])
def test_basket_says_why_zerodha_cannot_be_asked(kite, fragment, login):
    assert not margin.supported(kite)
    with pytest.raises(margin.MarginUnavailable, match=fragment) as caught:
        margin.basket(kite, LEGS)
    assert caught.value.needs_login is login


@pytest.mark.parametrize("error, fragment, login", [
    (KiteError("Please log in first using the login tool", needs_login=True), "Log in to Kite", True),
    (KiteError("Failed to calculate margins: Invalid `tradingsymbol`"), "Invalid `tradingsymbol`", False),
])
def test_kite_failures_are_explained(error, fragment, login):
    with pytest.raises(margin.MarginUnavailable, match=fragment) as caught:
        margin.basket(MarginKite(error=error), LEGS)
    assert caught.value.needs_login is login


@pytest.mark.parametrize("bad", [
    [], [{**LEGS[0], "side": "HOLD"}], [{**LEGS[0], "quantity": 0}], [{**LEGS[0], "tradingsymbol": ""}], LEGS * 11,
])
def test_bad_baskets_are_refused_before_kite_is_asked(bad):
    kite = MarginKite()
    with pytest.raises(margin.MarginUnavailable):
        margin.basket(kite, bad)
    assert kite.calls == []


def test_a_strange_answer_is_not_taken_as_a_margin():
    with pytest.raises(margin.MarginUnavailable, match="does not understand"):
        margin.basket(MarginKite(reply={"oops": 1}), LEGS)


# -- F&O algos ------------------------------------------------------------------------------------------------------

class TradingMarginKite(StubKite):
    def __init__(self, prices, final=60_000.0):
        super().__init__(prices)
        self.tools["get_basket_margins"] = {}
        self.reply, self.calls = answer(final=final), []

    def call(self, tool, arguments=None, timeout=45.0):
        self.calls.append((tool, arguments))
        return self.reply


def test_preview_uses_zerodhas_margin_when_the_server_has_it(tmp_path):
    clock = Clock("2026-11-17 09:00")
    kite = TradingMarginKite(Market())
    engine, _, prices = setup(tmp_path, clock, kite=kite)
    kite.prices = prices
    view = engine.preview({**CONDOR, "structure": "bull_put_spread", "k": 0, "w": 2})
    assert view["margin_source"] == "kite" and view["margin"] == 60_000 and view["hedge_benefit"] == 120_000
    assert 0 < view["margin_estimate"] < 20_000 and view["margin_note"] is None
    orders = kite.calls[0][1]["orders"]
    assert sorted(o["transaction_type"] for o in orders) == ["BUY", "SELL"] and {o["product"] for o in orders} == {"MIS"}
    assert all(o["quantity"] == 65 and o["price"] > 0 for o in orders)
    assert "Zerodha's figure" in view["notes"][1]


def test_preview_falls_back_to_the_estimate_and_says_why(tmp_path):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    view = engine.preview({**CONDOR, "structure": "bull_put_spread", "k": 0, "w": 2})
    assert view["margin_source"] == "estimate" and view["margin"] == view["margin_estimate"]
    assert "Log in to Kite" in view["margin_note"] and "estimate" in view["notes"][1]


def test_entry_is_checked_against_zerodhas_margin(tmp_path):
    clock = Clock("2026-11-17 09:35")
    kite = TradingMarginKite(Market(), final=2_000_000.0)         # more than the paper cash, though the estimate fits
    engine, desk, prices = setup(tmp_path, clock, kite=kite)
    kite.prices = prices
    algo = engine.create(CONDOR)
    engine.set_enabled(algo["id"], True)
    out = engine.evaluate(algo["id"])
    assert out["status"].startswith("Blocked: Zerodha's margin of ₹2,000,000 is more than")
    assert out["algo"]["book"] is None


def test_margin_check_before_start_flags_too_little_money(tmp_path):
    clock = Clock("2026-11-17 09:00")
    kite = TradingMarginKite(Market(), final=1_500_000.0)
    engine, desk, prices = setup(tmp_path, clock, kite=kite)
    kite.prices = prices
    algo = engine.create(CONDOR)
    check = engine.margin_check(algo["id"])
    assert check["margin"] == 1_500_000 and check["funds"] == 1_000_000 and check["short"] is True
    assert check["margin_source"] == "kite" and check["error"] is None
    kite.reply = answer(final=50_000.0)
    assert engine.margin_check(algo["id"])["short"] is False


def test_margin_check_reports_a_plan_that_cannot_be_made(tmp_path):
    engine, _, prices = setup(tmp_path, Clock("2026-11-17 09:00"))
    algo = engine.create(CONDOR)
    engine.desk.last_price = lambda instrument: (None, "test")
    check = engine.margin_check(algo["id"])
    assert check["margin"] is None and check["error"]


# -- the calculator's contract list -----------------------------------------------------------------------------------

class SearchKite:
    connected = True
    tools = {"search_instruments": {}}

    def __init__(self):
        self.queries = []

    def call(self, tool, arguments=None, timeout=45.0):
        self.queries.append(arguments["query"])
        return [{"exchange": "NFO", "tradingsymbol": "RELIANCE26NOVFUT", "name": "RELIANCE", "instrument_type": "FUT",
                 "expiry_date": "2026-11-24", "lot_size": 500},
                {"exchange": "NFO", "tradingsymbol": "RELIANCE26NOV1400CE", "name": "RELIANCE", "instrument_type": "CE",
                 "strike": 1400, "expiry_date": "2026-11-24", "lot_size": 500},
                {"exchange": "NFO", "tradingsymbol": "RELIANCE26OCT1400CE", "name": "RELIANCE", "instrument_type": "CE",
                 "strike": 1400, "expiry_date": "2026-10-27", "lot_size": 500}]


def test_a_stocks_contracts_are_read_from_kite_once_a_day(tmp_path):
    kite = SearchKite()
    clock = Clock("2026-11-17 09:00")
    d = Derivatives(kite, path=tmp_path / "fno.json", now=clock)
    d._data["stocks"] = [{"name": "RELIANCE", "exchange": "NFO", "lot_size": 500, "expiries": []}]
    rows = d.chain("reliance")
    assert [r["tradingsymbol"] for r in rows] == ["RELIANCE26NOVFUT", "RELIANCE26NOV1400CE"]
    assert rows[1]["strike"] == 1400.0 and rows[0]["strike"] is None and rows[0]["lot_size"] == 500
    d.chain("RELIANCE")
    assert kite.queries == ["NFO:RELIANCE"]
    assert {"name": "RELIANCE", "exchange": "NFO", "lot_size": 500, "group": "Stocks"} in d.margin_underlyings()
    with pytest.raises(KiteError, match="no futures or options"):
        d.chain("NOPE")


def test_the_calculator_end_to_end_through_the_kite_server(client):  # noqa: F811 - the imported fixture
    refused = client.post("/api/margin", json={"legs": [{"underlying": "NIFTY", "tradingsymbol": "X", "side": "BUY",
                                                        "lots": 1}]})
    assert refused.status_code in (400, 502) and "NIFTY" in refused.json()["error"]     # not a contract (or none loaded)
    client.post("/api/kite/connect")
    client.post("/api/kite/login")
    client.post("/api/fno/refresh")
    listed = client.get("/api/margin/underlyings").json()
    assert listed["available"] is True and {"NIFTY", "RELIANCE"} <= {u["name"] for u in listed["underlyings"]}
    contracts = client.get("/api/margin/contracts", params={"underlying": "NIFTY"}).json()["contracts"]
    expiry = contracts[0]["expiry"]
    pick = lambda strike, kind: next(c for c in contracts  # noqa: E731
                                     if c["expiry"] == expiry and c["strike"] == strike and c["type"] == kind)
    legs = [{"underlying": "NIFTY", "tradingsymbol": pick(24400.0, "PE")["tradingsymbol"], "side": "BUY", "lots": 2},
            {"underlying": "NIFTY", "tradingsymbol": pick(24500.0, "PE")["tradingsymbol"], "side": "SELL", "lots": 2}]
    out = client.post("/api/margin", json={"legs": legs, "mode": "paper"}).json()
    assert out["source"] == "kite" and out["hedge_benefit"] > 0 and out["total"] < out["without_hedge"]
    assert [r["lots"] for r in out["legs"]] == [2, 2] and [r["quantity"] for r in out["legs"]] == [150, 150]
    assert out["funds"] > 0 and out["short"] is (out["total"] > out["funds"])
    live = client.post("/api/margin", json={"legs": legs, "mode": "live"}).json()
    assert live["funds"] == 250000.0
    for bad in ({**legs[0], "lots": 0}, {**legs[0], "side": "HOLD"}, {**legs[0], "tradingsymbol": "NIFTYNOPE"}):
        assert client.post("/api/margin", json={"legs": [bad]}).status_code == 400
    stock = client.get("/api/margin/contracts", params={"underlying": "RELIANCE"})
    assert stock.status_code == 502 and "no live RELIANCE contracts" in stock.json()["error"]   # the fake lists none


def test_another_broker_is_told_the_calculator_is_zerodhas():
    other = type("Switch", (), {"name": "upstox", "connected": True, "tools": {"get_ltp": {}}})()
    with pytest.raises(margin.MarginUnavailable, match="only with Zerodha .*Upstox is selected"):
        margin.basket(other, LEGS)
