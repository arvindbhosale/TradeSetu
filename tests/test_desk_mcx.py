"""MCX gold and silver: specs, the session, the delivery cut-off, charges, the order path, P&L and the F&O algos."""

import copy
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import market, mcx
from desk.algo import AlgoError
from desk.broker import Desk, PaperBook
from desk.charges import commodity_charges
from desk.fno import Derivatives
from desk.fnoalgo import FnoAlgoEngine, arm_phrase
from desk.pnl import Ledger
from desk.settings import Settings
from desk.taxreport import summary, tax_lots

pytestmark = pytest.mark.unit
FUT_DEC, FUT_FEB = "GOLDM26DECFUT", "GOLDM27FEBFUT"
DEC, FEB, OPT = "2026-12-04", "2027-02-05", "2026-11-26"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    mcx.set_holidays([])
    yield
    config_module._config = saved
    mcx.set_holidays([])


class Clock:
    def __init__(self, text):
        self.set(text)

    def set(self, text):
        self.now = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)

    def __call__(self):
        return self.now


def opt(strike, kind):
    return f"GOLDM26DEC{int(strike)}{kind}"


class Metals:
    """The contract list as the desk holds it after a Kite refresh: GOLDM futures and the December options."""

    def __init__(self):
        self.futs = [{"exchange": "MCX", "tradingsymbol": FUT_DEC, "underlying": "GOLDM", "type": "FUT", "strike": None,
                      "expiry": DEC, "lot_size": 1, "multiplier": 10, "freeze_quantity": 0, "tick_size": 1.0},
                     {"exchange": "MCX", "tradingsymbol": FUT_FEB, "underlying": "GOLDM", "type": "FUT", "strike": None,
                      "expiry": FEB, "lot_size": 1, "multiplier": 10, "freeze_quantity": 0, "tick_size": 1.0}]
        self.options = [{"exchange": "MCX", "tradingsymbol": opt(k, t), "underlying": "GOLDM", "type": t, "strike": float(k),
                         "expiry": OPT, "lot_size": 1, "multiplier": 10, "freeze_quantity": 0, "tick_size": 0.5}
                        for k in range(127000, 133100, 500) for t in ("CE", "PE")]
        self.by = {(c["exchange"], c["tradingsymbol"]): c for c in self.futs + self.options}

    def metal_contracts(self, name):
        return self.futs + self.options if name == "GOLDM" else []

    def metal_futures(self, name):
        return list(self.futs) if name == "GOLDM" else []

    def contracts(self, underlying):
        return []

    def futures(self):
        return []

    def contract(self, exchange, tradingsymbol):
        return self.by.get((exchange, tradingsymbol))


class Prices:
    def __init__(self, future=130000.0):
        self.future, self.override = future, {}

    def __call__(self, instrument):
        if instrument in self.override:
            return self.override[instrument]
        symbol = instrument.split(":", 1)[1]
        if symbol.endswith("FUT"):
            return self.future + (300 if symbol == FUT_FEB else 0)
        strike, kind = float(symbol[10:-2]), symbol[-2:]
        intrinsic = max(self.future - strike, 0) if kind == "CE" else max(strike - self.future, 0)
        return round(intrinsic + max(1500 - abs(self.future - strike) * 0.3, 20), 1)


class OfflineKite:
    connected, tools, can_trade = False, {}, False


class TradingKite:
    def __init__(self, prices):
        self.prices, self.placed = prices, []
        self.connected, self.tools, self.can_trade = True, {"get_ltp": {}, "place_order": {}, "get_orders": {}}, True

    def ltp(self, instruments):
        return {i: self.prices(i) for i in instruments}

    def place_order(self, args):
        self.placed.append(args)
        return {"order_id": str(5000 + len(self.placed))}

    def orders(self):
        return []

    def positions(self):
        return []

    def margins(self):
        return {"equity": {"available": {"live_balance": 50_000.0}}, "commodity": {"available": {"live_balance": 600_000.0}}}


def setup(tmp_path, clock, kite=None, **settings):
    prices = Prices()
    cfg = Settings(tmp_path / "settings.json")
    cfg.update({"max_order_value": 400_000.0, **settings})
    metals = Metals()
    desk = Desk(cfg, kite or OfflineKite(), PaperBook(1_000_000, tmp_path / "paper.json"), tmp_path / "orders.jsonl",
                price_lookup=lambda s: None, now=clock, contract_lookup=metals.contract,
                ledger=Ledger(tmp_path / "fills.jsonl", now=clock))
    desk.last_price = lambda instrument: (prices(getattr(instrument, "instrument", instrument)), "test")
    engine = FnoAlgoEngine(desk, metals, path=tmp_path / "fno.json", now=clock)
    desk.algo_instruments = engine.held_instruments
    return engine, desk, prices


def order(**change):
    return {"exchange": "MCX", "tradingsymbol": FUT_DEC, "side": "BUY", "quantity": 2, "product": "NRML",
            "order_type": "LIMIT", "price": 130000.0, **change}


# -- specs, session, cut-off --------------------------------------------------------------------------------------

def test_contract_names_and_lot_multipliers():
    assert mcx.name_of("GOLDM26DECFUT") == "GOLDM" and mcx.name_of("GOLD26DECFUT") == "GOLD"
    assert mcx.name_of("SILVERMIC26NOV95000CE") == "SILVERMIC" and mcx.name_of("CRUDEOIL26DECFUT") is None
    assert mcx.multiplier("MCX", "GOLD26DECFUT") == 100          # ₹ per 10 g, 1 kg lots
    assert mcx.multiplier("MCX", "GOLDM26DECFUT") == 10
    assert mcx.multiplier("MCX", "GOLDPETAL26DECFUT") == 1
    assert mcx.multiplier("MCX", "SILVER26DECFUT") == 30         # ₹ per kg, 30 kg lots
    assert mcx.multiplier("MCX", "SILVERM26NOVFUT") == 5
    assert mcx.multiplier("MCX", "GOLD26DECFUT", lot_size=100) == 1    # if Kite ever quotes quantity in units
    assert mcx.multiplier("NFO", "NIFTY26NOVFUT") == 1
    with pytest.raises(ValueError):
        mcx.multiplier("MCX", "CRUDEOIL26DECFUT")


def test_the_mcx_session_follows_us_daylight_time_and_its_own_holidays():
    at = lambda text: datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)   # noqa: E731
    assert mcx.close_time(at("2026-11-16 12:00")).strftime("%H:%M") == "23:55"     # US standard time
    assert mcx.close_time(at("2026-07-15 12:00")).strftime("%H:%M") == "23:30"     # US daylight time
    assert mcx.session_status(at("2026-11-16 23:40"))["open"]
    assert not mcx.session_status(at("2026-07-15 23:40"))["open"]
    assert not mcx.session_status(at("2026-11-16 08:59"))["open"]
    assert mcx.session_status(at("2026-11-16 18:00"))["open"]                      # after NSE closes
    assert not mcx.session_status(at("2026-11-21 12:00"))["open"]                  # Saturday
    mcx.set_holidays(mcx.parse_holidays("2026-12-25\n2026-11-24 evening"))
    assert not mcx.session_status(at("2026-12-25 18:00"))["open"]
    evening = mcx.session_status(at("2026-11-24 12:00"))
    assert not evening["open"] and evening["evening_only"] and evening["opens"] == "17:00"
    assert mcx.session_status(at("2026-11-24 17:30"))["open"]
    with pytest.raises(ValueError, match="evening"):
        mcx.parse_holidays(["2026-11-24 morning"])
    assert mcx.parse_holidays(["2026-11-24  Evening", "2026-12-25"]) == ["2026-11-24 evening", "2026-12-25"]


def test_mcx_settings_are_validated(tmp_path):
    cfg = Settings(tmp_path / "settings.json")
    assert cfg.get("mcx_exit_days") == 8 and cfg.get("mcx_trade_start") == "09:00"
    for bad in ({"mcx_exit_days": 0}, {"mcx_exit_days": 2.5}, {"mcx_trade_end": "23:58"}, {"mcx_margin_pct": 1},
                {"mcx_trade_start": "23:30", "mcx_trade_end": "22:00"}, {"mcx_holidays": "25-12-2026"}):
        with pytest.raises(ValueError):
            cfg.update(bad)
    cfg.update({"mcx_holidays": "2026-12-25, 2026-11-24 evening", "mcx_trade_end": "23:45"})
    assert mcx.HOLIDAYS == {"2026-12-25": "closed", "2026-11-24": "evening"}


def test_commodity_charges_follow_zerodhas_mcx_schedule():
    buy, sell = (commodity_charges(s, 2, 130000, 10, option=False) for s in ("BUY", "SELL"))
    assert buy.turnover == 2_600_000 and buy.brokerage == 20.0 and buy.stt == 0
    assert buy.exchange_txn == pytest.approx(2_600_000 * 0.000021) and buy.stamp_duty == pytest.approx(52.0)
    assert sell.stt == pytest.approx(260.0) and sell.stamp_duty == 0                 # CTT 0.01% on the sell side
    assert buy.gst == pytest.approx((20 + 54.6 + 2.6) * 0.18)
    option = commodity_charges("SELL", 1, 1500, 10, option=True)                      # ₹15,000 of premium
    assert option.brokerage == 20 and option.stt == pytest.approx(7.5) and option.exchange_txn == pytest.approx(6.27)


def test_kite_mcx_rows_are_loaded_by_metal_and_odd_lot_sizes_are_left_out(tmp_path):
    rows = [{"exchange": "MCX", "name": "GOLDM", "tradingsymbol": FUT_DEC, "instrument_type": "FUT", "expiry_date": DEC,
             "lot_size": 1, "tick_size": 1},
            {"exchange": "MCX", "name": "GOLDM", "tradingsymbol": opt(130000, "CE"), "instrument_type": "CE",
             "expiry_date": OPT, "strike": 130000, "lot_size": 1, "tick_size": 0.5},
            {"exchange": "MCX", "name": "GOLD", "tradingsymbol": "GOLD26DECFUT", "instrument_type": "FUT", "expiry_date": DEC,
             "lot_size": 100, "tick_size": 1},
            {"exchange": "MCX", "name": "GOLDM", "tradingsymbol": "GOLDM26OCTFUT", "instrument_type": "FUT",
             "expiry_date": "2026-10-05", "lot_size": 1},
            {"exchange": "NFO", "name": "NIFTY", "tradingsymbol": "NIFTY26NOVFUT", "instrument_type": "FUT",
             "expiry_date": "2026-11-24", "lot_size": 65}]

    class Kite:
        connected, tools = True, {"search_instruments": {}}

        def call(self, tool, args, timeout=0):
            query = args["query"]
            if query == "FUT":
                return [r for r in rows if r["exchange"] == "NFO"]
            name = query.split(":")[-1]
            return [r for r in rows if r["name"] == name]

    clock = Clock("2026-11-17 10:00")
    d = Derivatives(Kite(), path=tmp_path / "fno.json", now=clock)
    d.refresh()
    assert [c["tradingsymbol"] for c in d.metal_futures("GOLDM")] == [FUT_DEC]          # the expired one dropped
    assert d.contract("MCX", opt(130000, "CE"))["strike"] == 130000.0
    assert d.contract("MCX", "GOLD26DECFUT") is None                                    # lot size 100: not trusted
    view = d.metals()
    assert "lot size other than 1" in view["note"]
    goldm = next(m for m in view["metals"] if m["name"] == "GOLDM")
    assert goldm["option_expiries"] == [OPT] and goldm["units_per_lot"] == 10
    assert d.contract("NFO", "NIFTY26NOVFUT")                                            # NSE list intact


# -- the order path ------------------------------------------------------------------------------------------------

def test_an_mcx_future_is_valued_in_lots_times_the_multiplier_and_weighed_by_margin(tmp_path):
    clock = Clock("2026-11-17 20:30")                                  # NSE shut, MCX open
    _, desk, _ = setup(tmp_path, clock, max_order_value=50_000.0)
    review = desk.preview(order(), "paper")
    assert review["value"] == 2_600_000                               # 2 lots x ₹1,30,000 per 10 g x 10
    assert any("Margin of about ₹390,000 (15% of ₹2,600,000)" in e for e in review["errors"]), review["errors"]
    desk.settings.update({"max_order_value": 400_000.0})
    review = desk.preview(order(), "paper")
    assert review["ok"], review["errors"]
    assert review["charges"]["total"] == pytest.approx(commodity_charges("BUY", 2, 130000, 10, False).total, abs=0.01)


@pytest.mark.parametrize("change, when, fragment", [
    ({"price": 130000.5}, "2026-11-17 20:30", "₹1 tick"),
    ({"order_type": "MARKET", "price": 0}, "2026-11-17 20:30", "LIMIT orders only"),
    ({"tradingsymbol": "GOLDM99FUT"}, "2026-11-17 20:30", "not a contract the desk knows"),
    ({"side": "SELL"}, "2026-11-17 20:30", "open a short future"),
    ({}, "2026-11-27 20:30", "compulsorily delivered"),               # 7 days to the December expiry
    ({}, "2026-11-17 23:58", "MCX is closed"),
    ({}, "2026-11-17 23:20", "MCX trade window"),                     # trade window ends 23:00
])
def test_mcx_orders_are_refused_for_the_right_reasons(tmp_path, change, when, fragment):
    _, desk, _ = setup(tmp_path, Clock(when))
    errors = desk.preview(order(**change), "paper")["errors"]
    assert any(fragment in e for e in errors), errors


def test_crude_and_other_mcx_contracts_are_not_accepted(tmp_path):
    from desk.broker import OrderRejected
    _, desk, _ = setup(tmp_path, Clock("2026-11-17 20:30"))
    with pytest.raises(OrderRejected, match="gold or silver"):
        desk.preview(order(tradingsymbol="CRUDEOIL26DECFUT"), "paper")


def test_live_mcx_orders_go_in_the_session_only_and_never_as_amo(tmp_path):
    clock = Clock("2026-11-17 20:30")
    _, desk, prices = setup(tmp_path, clock, kite=TradingKite(Prices()), live_enabled=True)
    assert desk.preview(order(), "live")["ok"]
    assert any("not as AMO" in e for e in desk.preview(order(variety="amo"), "live")["errors"])
    clock.set("2026-11-18 08:30")
    errors = desk.preview(order(), "live")["errors"]
    assert any("MCX is closed" in e for e in errors), errors


def test_a_closing_order_inside_the_cut_off_is_allowed_and_warned(tmp_path):
    clock = Clock("2026-11-17 20:30")
    _, desk, _ = setup(tmp_path, clock)
    review = desk.preview(order(), "paper")
    desk.place(review["token"], review["phrase"])
    clock.set("2026-11-27 20:30")
    review = desk.preview(order(side="SELL"), "paper")
    assert review["ok"], review["errors"]
    assert any("delivery" in w for w in review["warnings"])


def test_paper_pnl_ledger_and_tax_use_the_multiplier(tmp_path):
    clock = Clock("2026-11-17 20:30")
    _, desk, prices = setup(tmp_path, clock)
    buy = desk.preview(order(), "paper")
    desk.place(buy["token"], buy["phrase"])
    cash = desk.paper.cash
    assert cash == pytest.approx(1_000_000 - buy["charges"]["total"], abs=0.01)    # a future pays no notional
    prices.future = 130250.0
    report = desk.pnl("paper")
    assert report["open"][0]["quantity"] == 2
    assert report["unrealized"] == pytest.approx(250 * 2 * 10 - buy["charges"]["total"], abs=0.05)
    sell = desk.preview(order(side="SELL", price=130250.0), "paper")
    desk.place(sell["token"], sell["phrase"])
    assert desk.paper.cash == pytest.approx(cash + 5000 - sell["charges"]["total"], abs=0.01)
    report = desk.pnl("paper")
    net = 5000 - buy["charges"]["total"] - sell["charges"]["total"]
    assert report["realized"] == pytest.approx(net, abs=0.05) and report["open"] == []
    assert report["closed"][0]["quantity"] == 2
    lots = tax_lots(desk.ledger.fills(), "paper")
    assert lots[0]["category"] == "F&O (non-speculative)" and lots[0]["gain"] == pytest.approx(net, abs=0.05)
    assert summary(lots)[0]["fno_turnover"] == pytest.approx(5000)


def test_a_paper_option_buy_pays_premium_times_the_multiplier(tmp_path):
    clock = Clock("2026-11-17 20:30")
    _, desk, prices = setup(tmp_path, clock)
    symbol = opt(130000, "CE")
    premium = prices(f"MCX:{symbol}")
    review = desk.preview(order(tradingsymbol=symbol, quantity=1, price=premium), "paper")
    assert review["ok"], review["errors"]
    assert review["value"] == pytest.approx(premium * 10)
    desk.place(review["token"], review["phrase"])
    assert desk.paper.cash == pytest.approx(1_000_000 - premium * 10 - review["charges"]["total"], abs=0.01)


def test_paper_positions_are_closed_at_the_delivery_cut_off_like_zerodha_would(tmp_path):
    clock = Clock("2026-11-17 20:30")
    _, desk, prices = setup(tmp_path, clock)
    review = desk.preview(order(), "paper")
    desk.place(review["token"], review["phrase"])
    assert desk.close_paper_mcx() == []                               # 17 days out: nothing to do
    clock.set("2026-11-26 10:00")                                     # 8 days to the 4 December expiry
    prices.future = 129800.0
    closed = desk.close_paper_mcx()
    assert len(closed) == 1 and desk.paper.quantity(f"MCX:{FUT_DEC}") == 0
    assert desk.pnl("paper")["realized"] < 0                          # -₹200 x 2 lots x 10, less charges
    assert any(r.get("source") == "mcx-delivery-cutoff" for r in desk.journal_rows(10))


# -- algos ---------------------------------------------------------------------------------------------------------

SPREAD = {"name": "Goldm spread", "kind": "structure", "structure": "bull_call_spread", "underlying": "GOLDM",
          "schedule": "positional", "entry_time": "19:00", "exit_time": "23:00", "w": 2, "lots": 1, "mode": "paper"}


def test_bad_mcx_algos_are_refused(tmp_path):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    with pytest.raises(AlgoError, match="23:25"):
        engine.create({**SPREAD, "exit_time": "23:40"})
    with pytest.raises(AlgoError, match="09:00"):
        engine.create({**SPREAD, "entry_time": "08:30"})
    assert engine.create({**SPREAD, "expiry_kind": "weekly"})["expiry_kind"] == "monthly"


def test_a_paper_goldm_spread_trades_in_the_evening_and_closes_at_the_cut_off(tmp_path):
    clock = Clock("2026-11-17 19:00")                                 # NSE is shut; MCX trades
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create(SPREAD)
    engine.set_enabled(algo["id"], True)
    assert algo["id"] in engine.due()
    out = engine.evaluate(algo["id"])
    assert out["status"].startswith("Running: open"), out
    book = out["algo"]["book"]
    assert book["spot"] == 130000.0 and book["near"] == OPT          # centred on the December future
    assert {(g["strike"], g["action"], g["quantity"]) for g in book["legs"]} == {(130000.0, 1, 1), (131000.0, -1, 1)}
    assert all(g["mult"] == 10 for g in book["legs"])
    debit = sum(g["avg"] * g["action"] for g in book["legs"]) * 10
    assert out["algo"]["book"]["margin"] == pytest.approx(debit, rel=0.01)       # a debit spread: the premium
    prices.future = 130600.0
    pnl = engine.mtm(engine.get(algo["id"]))
    moved = sum((prices(g["instrument"]) - g["avg"]) * g["action"] for g in book["legs"]) * 10
    assert pnl == pytest.approx(moved - sum(g["charges"] for g in book["legs"]), abs=0.05)
    clock.set("2026-11-18 10:00")                                     # 8 days to the 26 Nov option expiry
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"].startswith("expiry")
    assert desk.pnl("paper")["realized"] == pytest.approx(out["algo"]["history"][-1]["pnl"], abs=0.05)
    clock.set("2026-11-18 19:00")                                     # the only option expiry is inside the cut-off
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["status"].startswith("Blocked"), out["status"]


def test_mcx_algos_wait_for_the_mcx_session(tmp_path):
    clock = Clock("2026-11-17 23:58")
    engine, _, _ = setup(tmp_path, clock)
    algo = engine.create(SPREAD)
    engine.set_enabled(algo["id"], True)
    assert algo["id"] not in engine.due()
    clock.set("2026-11-17 12:00")
    assert algo["id"] in engine.due()


def test_a_goldm_futures_algo_shorts_and_uses_commodity_funds_live(tmp_path, monkeypatch):
    clock = Clock("2026-11-17 19:05")
    kite = TradingKite(Prices())
    engine, desk, prices = setup(tmp_path, clock, kite=kite, live_enabled=True)
    days = pd.bdate_range(end="2026-11-16", periods=120)
    closes = np.r_[np.linspace(120000, 132000, 80), np.linspace(132000, 126000, 40)]
    bars = pd.DataFrame({"Open": closes, "High": closes * 1.003, "Low": closes * 0.997, "Close": closes, "Volume": 1e4}, index=days)
    monkeypatch.setattr(engine, "_day_bars", lambda underlying: bars)
    algo = engine.create({"name": "Goldm short", "kind": "futures", "direction": "short", "underlying": "GOLDM",
                          "strategy": "sma_cross", "strategy_params": {"fast": 5, "slow": 20}, "mode": "live",
                          "max_loss": 20000, "entry_time": "19:00", "exit_time": "23:00", "margin_pct": 15})
    engine.set_enabled(algo["id"], True, arm_phrase("Goldm short"))
    engine.get(algo["id"])["ready"] = True
    plan = engine.preview({**algo, "name": "x"})
    assert plan["funds"] == 600_000.0                                  # Kite's commodity segment, not equity
    assert plan["margin"] == pytest.approx(0.15 * 130000 * 10)
    engine.evaluate(algo["id"])
    sent = kite.placed[-1]
    assert (sent["exchange"], sent["tradingsymbol"], sent["transaction_type"], sent["quantity"]) == ("MCX", FUT_DEC, "SELL", 1)
    assert sent["product"] == "NRML" and sent["price"] == round(sent["price"])        # on the ₹1 tick


# -- the API, against the fake Kite server --------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    import sys

    from fastapi.testclient import TestClient

    from desk.server import create_app

    fake = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"], "env": {"FAKE_KITE_ANCHOR": "2026-11-16"}}
    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": fake}}, "max_order_value": 300_000.0})
    app = create_app(settings=settings, token="t", fetch=lambda symbol, interval="1d": pd.DataFrame())
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


def test_api_lists_mcx_metals_trades_goldm_and_reports_lot_pnl(client, monkeypatch):
    moment = datetime(2026, 11, 17, 20, 0, tzinfo=market.IST)
    app = client.app
    app.state.desk._now = app.state.fno_algos._now = app.state.derivatives._now = lambda: moment
    monkeypatch.setattr(market, "now_ist", lambda: moment)
    client.post("/api/kite/connect")
    client.post("/api/kite/login")
    assert client.post("/api/fno/refresh").status_code == 200
    view = client.get("/api/mcx").json()
    goldm = next(m for m in view["metals"] if m["name"] == "GOLDM")
    fut = goldm["futures"][0]
    assert fut["ltp"] == 130000.0 and fut["value_per_lot"] == 1_300_000 and fut["margin_per_lot"] == 195_000
    assert view["session"]["open"] and view["session"]["closes"] == "23:55" and not fut["cutoff"]
    chain = client.get("/api/mcx/chain", params={"name": "GOLDM"}).json()
    assert chain["spot"] == 130000.0 and chain["future"]["tradingsymbol"] == fut["tradingsymbol"] and chain["multiplier"] == 10
    atm = next(r for r in chain["rows"] if r["atm"])
    assert atm["strike"] == 130000 and atm["CE"]["iv"] and 0.4 < atm["CE"]["delta"] < 0.6
    body = {"exchange": "MCX", "tradingsymbol": fut["tradingsymbol"], "side": "BUY", "quantity": 1, "product": "NRML",
            "order_type": "LIMIT", "price": 130000.0}
    review = client.post("/api/order/preview", json={"mode": "paper", "order": body}).json()
    assert review["ok"], review["errors"]
    client.post("/api/order/place", json={"token": review["token"], "phrase": review["phrase"]})
    rows = client.get("/api/mcx/positions?mode=paper").json()["positions"]
    assert rows[0]["quantity"] == 1 and rows[0]["multiplier"] == 10 and rows[0]["pnl"] == 0
    created = client.post("/api/fno-algos", json={**SPREAD, "name": "API goldm"}).json()
    assert created["exchange"] == "MCX"
    preview = client.post("/api/fno-algos/preview", json={**SPREAD, "name": "x"}).json()
    assert [g["side"] for g in preview["legs"]] == ["BUY", "SELL"] and preview["funds"] > 0
