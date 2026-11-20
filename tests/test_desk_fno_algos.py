"""F&O algos: option strategies and index futures run by themselves, paper and live, through the desk's order path."""

import copy
import json
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import broker as broker_module, market
from desk.algo import AlgoError
from desk.broker import Desk, OrderRejected, PaperBook, explain_kite_refusal
from desk.fnoalgo import FnoAlgoEngine, arm_phrase, naked
from desk.kite import KiteError
from desk.pnl import Ledger
from desk.settings import Settings

pytestmark = pytest.mark.unit
SPOT = "NSE:NIFTY 50"
NEAR, NEXT, MONTH_END = "2026-11-24", "2026-12-01", "2026-12-29"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


class Clock:
    def __init__(self, text):
        self.set(text)

    def set(self, text):
        self.now = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)

    def __call__(self):
        return self.now


def symbol(expiry, strike, kind):
    return f"NIFTY{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{int(strike)}{kind}"


class Contracts:
    """The F&O contract list as Kite would give it: NIFTY weeklies and monthlies, and two futures."""

    def __init__(self):
        self.options = [{"exchange": "NFO", "tradingsymbol": symbol(e, k, t), "underlying": "NIFTY", "strike": float(k),
                         "type": t, "expiry": e, "lot_size": 65, "freeze_quantity": 1800, "tick_size": 0.05}
                        for e in (NEAR, NEXT, MONTH_END) for k in range(24000, 26050, 50) for t in ("CE", "PE")]
        self.futs = [{"exchange": "NFO", "tradingsymbol": "NIFTY26NOVFUT", "underlying": "NIFTY", "type": "FUT",
                      "expiry": NEAR, "lot_size": 65, "tick_size": 0.1},
                     {"exchange": "NFO", "tradingsymbol": "NIFTY26DECFUT", "underlying": "NIFTY", "type": "FUT",
                      "expiry": MONTH_END, "lot_size": 65, "tick_size": 0.1}]
        self.by = {(c["exchange"], c["tradingsymbol"]): c for c in self.options + self.futs}

    def contracts(self, underlying):
        return list(self.options) if underlying == "NIFTY" else []

    def futures(self):
        return list(self.futs)

    def contract(self, exchange, tradingsymbol):
        return self.by.get((exchange, tradingsymbol))


def premium(name, spot):
    """A rough premium: intrinsic plus time value that falls off away from the money."""
    if name.endswith("FUT"):
        return round(spot + 40, 1)
    strike, kind = float(name[-7:-2]), name[-2:]
    intrinsic = max(spot - strike, 0) if kind == "CE" else max(strike - spot, 0)
    return round(intrinsic + max(120 - abs(spot - strike) * 0.25, 2), 2)


class Market:
    def __init__(self, spot=25000.0):
        self.spot, self.override = spot, {}

    def price(self, instrument):
        if instrument == SPOT:
            return self.spot
        if instrument in self.override:
            return self.override[instrument]
        return premium(instrument.split(":", 1)[1], self.spot)


class StubKite:
    """A trading Kite: orders rest until the test fills them."""

    def __init__(self, prices):
        self.prices, self.placed, self.book, self.cancelled = prices, [], {}, []
        self.connected, self.tools = True, {"get_ltp": {}, "place_order": {}, "cancel_order": {}, "get_orders": {}}
        self.positions_rows = []

    @property
    def can_trade(self):
        return True

    def ltp(self, instruments):
        return {i: self.prices.price(i) for i in instruments}

    def place_order(self, args):
        oid = str(1000 + len(self.placed))
        self.placed.append({**args, "order_id": oid})
        self.book[oid] = {"order_id": oid, "status": "OPEN", "filled_quantity": 0, "average_price": 0}
        return {"order_id": oid}

    def fill(self, oid, price=None):
        args = next(p for p in self.placed if p["order_id"] == oid)
        self.book[oid].update(status="COMPLETE", filled_quantity=args["quantity"], average_price=price or args["price"])
        signed = args["quantity"] if args["transaction_type"] == "BUY" else -args["quantity"]
        row = next((r for r in self.positions_rows if r["tradingsymbol"] == args["tradingsymbol"]), None)
        if row is None:
            self.positions_rows.append({"tradingsymbol": args["tradingsymbol"], "exchange": args["exchange"], "quantity": signed})
        else:
            row["quantity"] += signed

    def orders(self):
        return list(self.book.values())

    def positions(self):
        return list(self.positions_rows)

    def cancel_order(self, oid, variety="regular"):
        self.cancelled.append(oid)
        self.book[oid]["status"] = "CANCELLED"

    def margins(self):
        return {"equity": {"available": {"live_balance": 900000.0}, "net": 900000.0}}


class OfflineKite:
    connected, tools, can_trade = False, {}, False


def setup(tmp_path, clock, kite=None, **settings):
    prices = Market()
    cfg = Settings(tmp_path / "settings.json")
    if settings:
        cfg.update(settings)
    contracts = Contracts()
    desk = Desk(cfg, kite or OfflineKite(), PaperBook(1_000_000, tmp_path / "paper.json"), tmp_path / "orders.jsonl",
                price_lookup=lambda s: None, now=clock, contract_lookup=contracts.contract,
                ledger=Ledger(tmp_path / "fills.jsonl", now=clock))
    desk.last_price = lambda instrument: (prices.price(getattr(instrument, "instrument", instrument)), "test")
    engine = FnoAlgoEngine(desk, contracts, path=tmp_path / "fno.json", now=clock)
    desk.algo_claims = engine.claimed                       # as the server wires it
    return engine, desk, prices


CONDOR = {"name": "Nifty condor", "kind": "structure", "structure": "iron_condor", "underlying": "NIFTY",
          "expiry_kind": "weekly", "schedule": "intraday", "entry_time": "09:35", "exit_time": "14:55",
          "k": 4, "w": 4, "lots": 1, "mode": "paper"}


def journal(desk):
    return list(reversed(desk.journal_rows(200)))


# -- structures and the gates around them -------------------------------------------------------------------------

def test_naked_structures_are_told_apart_from_covered_ones():
    assert {s for s in ("short_straddle", "short_strangle", "call_ratio_spread", "jade_lizard", "synthetic_long",
                        "short_guts", "put_ratio_spread", "synthetic_short") if naked(s)} == {
        "short_straddle", "short_strangle", "call_ratio_spread", "jade_lizard", "synthetic_long", "short_guts",
        "put_ratio_spread", "synthetic_short"}
    for covered in ("iron_condor", "iron_fly", "bull_put_spread", "bear_call_spread", "covered_call", "collar",
                    "long_call_butterfly", "short_call_butterfly", "call_ratio_backspread", "long_call_calendar",
                    "short_straddle_hedged", "long_straddle"):
        assert not naked(covered), covered


@pytest.mark.parametrize("change, fragment", [
    ({"name": ""}, "name"), ({"underlying": "XYZ"}, "Index"), ({"structure": "nope"}, "Unknown option strategy"),
    ({"underlying": "BANKNIFTY"}, "only monthly"), ({"schedule": "signal"}, "directional"),
    ({"entry_time": "15:20", "exit_time": "15:10"}, "before the exit"), ({"lots": 0}, "Lots"),
    ({"mode": "live"}, "maximum loss"), ({"structure": "short_guts", "k": 0}, "at least 1 strike"),
    ({"kind": "futures", "direction": "sideways"}, "Direction"), ({"weekdays": [5]}, "Weekdays"),
    ({"kind": "futures", "strategy": "buy_hold"}, "nothing to automate"),
])
def test_bad_fno_algos_are_refused(tmp_path, change, fragment):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    with pytest.raises(AlgoError, match=fragment):
        engine.create({**CONDOR, **change})


def test_a_live_algo_needs_the_phrase_live_orders_a_max_loss_and_permission_for_naked_shorts(tmp_path):
    clock = Clock("2026-11-17 09:00")
    engine, desk, prices = setup(tmp_path, clock, kite=StubKite(Market()))
    straddle = engine.create({**CONDOR, "name": "Straddle", "structure": "short_straddle", "mode": "live", "max_loss": 5000})
    with pytest.raises(AlgoError, match="ARM LIVE FNO STRADDLE"):
        engine.set_enabled(straddle["id"], True, "yes")
    with pytest.raises(AlgoError, match="switched off"):
        engine.set_enabled(straddle["id"], True, arm_phrase("Straddle"))
    desk.settings.update({"live_enabled": True})
    with pytest.raises(AlgoError, match="Allow naked F&O shorts"):
        engine.set_enabled(straddle["id"], True, arm_phrase("Straddle"))
    desk.settings.update({"fno_naked_live": True})
    assert engine.set_enabled(straddle["id"], True, arm_phrase("Straddle"))["enabled"]
    condor = engine.create({**CONDOR, "mode": "live", "max_loss": 3000})
    assert engine.set_enabled(condor["id"], True, arm_phrase(condor["name"]))["enabled"]


# -- paper: the same path as live, filled at once --------------------------------------------------------------------

def test_a_paper_condor_enters_hedge_first_exits_shorts_first_and_books_the_pnl(tmp_path):
    clock = Clock("2026-11-17 09:30")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create(CONDOR)
    engine.set_enabled(algo["id"], True)
    assert engine.evaluate(algo["id"])["status"] == "Running: flat"          # before the entry time
    clock.set("2026-11-17 09:35")
    out = engine.evaluate(algo["id"])
    assert out["status"].startswith("Running: open"), out
    book = out["algo"]["book"]
    assert book["near"] == NEAR
    strikes = {(g["type"], g["strike"], g["action"]) for g in book["legs"]}
    assert strikes == {("CE", 25200.0, -1), ("CE", 25400.0, 1), ("PE", 24800.0, -1), ("PE", 24600.0, 1)}
    sent = journal(desk)
    assert [r["side"] for r in sent] == ["BUY", "BUY", "SELL", "SELL"]             # wings before the sold legs
    assert all(r["intent"] == "open" and r["product"] == "MIS" and r["order_type"] == "LIMIT" for r in sent)
    assert desk.paper.quantity(f"NFO:{symbol(NEAR, 25200, 'CE')}") == -65                 # a real short in the book
    assert desk.paper.quantity(f"NFO:{symbol(NEAR, 25400, 'CE')}") == 65
    # Quiet market, premiums decay a little: the condor is in profit at the exit time.
    clock.set("2026-11-17 14:55")
    for g in book["legs"]:
        prices.override[g["instrument"]] = round(g["avg"] * 0.8, 2)
    out = engine.evaluate(algo["id"])
    assert out["status"] == "Running: flat" and out["algo"]["book"] is None
    closes = journal(desk)[4:]
    assert [r["side"] for r in closes] == ["BUY", "BUY", "SELL", "SELL"]            # sold legs bought back first
    assert all(r["intent"] == "close" for r in closes)
    assert all(q == 0 for q in (desk.paper.quantity(f"NFO:{g['tradingsymbol']}") for g in book["legs"]))
    trade = out["algo"]["history"][-1]
    assert trade["reason"] == "exit time 14:55" and trade["pnl"] > 0
    report = desk.pnl("paper")
    assert report["realized"] == pytest.approx(trade["pnl"], abs=0.05)              # the ledger sees the shorts
    assert report["open"] == []
    # One entry a day: nothing more today, even inside the window.
    clock.set("2026-11-17 10:00")
    assert engine.evaluate(algo["id"])["algo"]["book"] is None


def test_the_max_loss_closes_everything_and_blocks_reentry_for_the_day(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "name": "Straddle", "structure": "short_straddle", "max_loss": 4000})
    engine.set_enabled(algo["id"], True)
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"]["phase"] == "open"
    clock.set("2026-11-17 11:00")
    prices.spot = 25400.0                                    # the call side runs: the short straddle loses
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["blocked_day"] == "2026-11-17"
    assert out["algo"]["history"][-1]["reason"].startswith("maximum loss") and out["algo"]["history"][-1]["pnl"] < -4000
    prices.spot = 25000.0
    assert engine.evaluate(algo["id"])["status"] == "Running: done for today"


def test_a_sold_leg_stop_buys_back_only_that_leg(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "name": "Hedged", "structure": "short_straddle_hedged", "leg_sl_pct": 30, "w": 9})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    call = next(g for g in book["legs"] if g["action"] < 0 and g["type"] == "CE")
    prices.override[call["instrument"]] = round(call["avg"] * 1.35, 2)
    clock.set("2026-11-17 10:30")
    book = engine.evaluate(algo["id"])["algo"]["book"]
    legs = {g["tradingsymbol"]: g for g in book["legs"]}
    assert legs[call["tradingsymbol"]]["open_qty"] == 0 and legs[call["tradingsymbol"]]["stopped"]
    assert all(g["open_qty"] == g["quantity"] for name, g in legs.items() if name != call["tradingsymbol"])
    assert book["phase"] == "open"


def test_the_kill_switch_stops_entries_but_never_an_exit(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create(CONDOR)
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    desk.settings.update({"kill_switch": True})
    out = engine.request_exit(algo["id"])
    assert out["book"] is None and not out["enabled"]                    # bought back and sold with the switch on
    other = engine.create({**CONDOR, "name": "Second"})
    engine.set_enabled(other["id"], True)
    assert engine.evaluate(other["id"])["status"] == "Blocked: kill switch is on"


def test_an_algo_closes_its_own_leg_when_another_algo_holds_the_opposite_side(tmp_path):
    # A condor sells the 25200 call; an iron butterfly buys it as a wing. The account nets to 0, yet each algo
    # must still close its own leg. Before, the condor's buy-back was refused: "You are short 0 ...".
    clock = Clock("2026-11-17 09:35")
    engine, desk, _ = setup(tmp_path, clock)
    condor = engine.create(CONDOR)
    fly = engine.create({**CONDOR, "name": "Fly", "structure": "iron_fly", "w": 4})
    for a in (condor, fly):
        engine.set_enabled(a["id"], True)
        engine.evaluate(a["id"])
    call = f"NFO:{symbol(NEAR, 25200, 'CE')}"
    assert desk.paper.quantity(call) == 0
    assert engine.claimed(call, "fno:Fly") == -65 and engine.claimed(call, "fno:Nifty condor") == 65
    out = engine.request_exit(condor["id"])
    assert out["book"] is None, out["log"][-3:]
    assert desk.paper.quantity(call) == 65                       # the butterfly's wing is still there
    assert engine.request_exit(fly["id"])["book"] is None
    assert desk.paper.quantity(call) == 0
    # The check still bites: a closing buy larger than the algo's own short is refused.
    over = {"exchange": "NFO", "tradingsymbol": symbol(NEAR, 25200, "CE"), "side": "BUY", "quantity": 65,
            "product": "MIS", "order_type": "LIMIT", "price": 200, "intent": "close"}
    assert any("would open a long" in e for e in desk.preview(over, "paper", source="fno:Fly")["errors"])


def test_a_price_failure_names_what_kite_said(tmp_path):
    clock = Clock("2026-11-17 09:35")
    kite = StubKite(Market())
    engine, desk, _ = setup(tmp_path, clock, kite=kite)
    algo = engine.create(CONDOR)
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    desk.settings.update({"live_enabled": False})

    def refuse(instruments):
        raise KiteError("Too many requests")
    kite.ltp = refuse
    desk.last_price = lambda instrument: (None, "no price available")
    clock.set("2026-11-17 10:00")
    out = engine.evaluate(algo["id"])
    assert out["status"].startswith("Blocked: No live price")
    assert "Too many requests" in out["algo"]["log"][-1]["message"]
    assert "Log in to Kite" not in out["algo"]["log"][-1]["message"]
    del kite.ltp                                             # Kite answers again
    assert engine.evaluate(algo["id"])["status"].startswith("Running: open")    # recovers on its own


def test_a_single_order_never_opens_a_short(tmp_path):
    clock = Clock("2026-11-17 10:00")
    engine, desk, _ = setup(tmp_path, clock)
    raw = {"exchange": "NFO", "tradingsymbol": symbol(NEAR, 25000, "CE"), "side": "SELL", "quantity": 65,
           "product": "NRML", "order_type": "LIMIT", "price": 100.0, "intent": "open"}
    assert any("armed F&O algo" in e for e in desk.preview(raw, "paper")["errors"])
    assert desk.preview(raw, "paper", allow_short=True)["ok"]
    close = {**raw, "side": "BUY", "intent": "close"}
    assert any("would open a long" in e for e in desk.preview(close, "paper")["errors"])
    with pytest.raises(OrderRejected, match="F&O orders only"):
        desk.preview({**raw, "exchange": "NSE", "tradingsymbol": "RELIANCE", "product": "CNC"}, "paper")


def test_short_losses_count_towards_the_daily_loss_limit(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock, daily_loss_limit=3000.0)
    algo = engine.create({**CONDOR, "name": "Straddle", "structure": "short_straddle"})
    engine.set_enabled(algo["id"], True)
    engine.evaluate(algo["id"])
    prices.spot = 25300.0
    assert desk.pnl("paper")["today_total"] < -3000                     # an open short's loss is seen
    assert desk.check_loss_limit() == ["paper"] and desk.settings.get("kill_switch")
    clock.set("2026-11-17 10:00")
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"] == "daily loss limit"


def test_a_positional_structure_holds_overnight_and_leaves_before_expiry(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "name": "Weekly condor", "schedule": "positional", "weekdays": [1],
                          "exit_days_before": 1, "min_days": 2})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["near"] == NEAR and all(g["tradingsymbol"] in {c["tradingsymbol"] for c in engine.derivatives.options} for g in book["legs"])
    journal_products = {r["product"] for r in journal(desk)}
    assert journal_products == {"NRML"}
    clock.set("2026-11-17 18:00")
    assert engine.evaluate(algo["id"])["status"] == "Running: market closed"
    clock.set("2026-11-20 15:00")
    assert engine.evaluate(algo["id"])["algo"]["book"]["phase"] == "open"
    clock.set("2026-11-23 14:55")                                   # one day before expiry, at the exit time
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"].startswith("expiry")


def test_entry_is_skipped_when_the_margin_is_not_there(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, _ = setup(tmp_path, clock)
    desk.paper.reset(50_000)
    algo = engine.create({**CONDOR, "name": "Big straddle", "structure": "short_straddle", "lots": 5})
    engine.set_enabled(algo["id"], True)
    out = engine.evaluate(algo["id"])
    assert out["status"].startswith("Blocked: Margin of about") and out["algo"]["book"] is None
    assert journal(desk) == []


def test_preview_shows_the_legs_in_sending_order_and_the_margin(tmp_path):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:00"))
    view = engine.preview({**CONDOR, "structure": "bull_put_spread", "k": 0, "w": 2})
    assert [leg["side"] for leg in view["legs"]] == ["BUY", "SELL"]
    assert view["legs"][0]["strike"] == 24900.0 and view["legs"][1]["strike"] == 25000.0
    assert 0 < view["margin"] < 20_000 and view["funds"] == 1_000_000 and view["net_premium"] > 0
    calendar = engine.preview({**CONDOR, "structure": "long_call_calendar"})
    assert {leg["expiry"] for leg in calendar["legs"]} == {NEAR, NEXT}


# -- live: the same path, waiting for Kite's fills ------------------------------------------------------------------

def test_live_sells_wait_until_every_hedge_has_filled_and_unfilled_orders_are_cancelled(tmp_path):
    clock = Clock("2026-11-17 09:35")
    kite = StubKite(Market())
    engine, desk, prices = setup(tmp_path, clock, kite=kite, live_enabled=True)
    kite.prices = prices
    algo = engine.create({**CONDOR, "mode": "live", "max_loss": 3000})
    engine.set_enabled(algo["id"], True, arm_phrase(algo["name"]))
    out = engine.evaluate(algo["id"])
    assert out["status"] == "Running: entering, waiting for fills"
    assert [p["transaction_type"] for p in kite.placed] == ["BUY", "BUY"]         # no sell before the wings fill
    kite.fill(kite.placed[0]["order_id"])
    engine.evaluate(algo["id"])
    assert len(kite.placed) == 2                                                    # one wing still open: still waiting
    clock.set("2026-11-17 09:38")                                                   # unfilled for 3 minutes
    engine.evaluate(algo["id"])
    assert kite.cancelled == [kite.placed[1]["order_id"]]
    engine.evaluate(algo["id"])                                                     # cancelled: sent again, at a new price
    assert len(kite.placed) == 3 and kite.placed[2]["tradingsymbol"] == kite.placed[1]["tradingsymbol"]
    kite.fill(kite.placed[2]["order_id"])
    engine.evaluate(algo["id"])
    assert [p["transaction_type"] for p in kite.placed[3:]] == ["SELL", "SELL"]
    assert all(p["product"] == "MIS" and p["order_type"] == "LIMIT" for p in kite.placed)
    for p in kite.placed[3:]:
        kite.fill(p["order_id"])
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"]["phase"] == "open"
    # Exit: the sold legs are bought back before the wings are sold.
    out = engine.request_exit(algo["id"])
    closing = kite.placed[5:]
    assert [p["transaction_type"] for p in closing] == ["BUY", "BUY"]
    for p in closing:
        kite.fill(p["order_id"])
    engine.evaluate(algo["id"])
    assert [p["transaction_type"] for p in kite.placed[7:]] == ["SELL", "SELL"]
    for p in kite.placed[7:]:
        kite.fill(p["order_id"])
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and not out["algo"]["enabled"]


def test_a_refused_sell_after_the_hedges_filled_unwinds_the_entry(tmp_path):
    clock = Clock("2026-11-17 09:35")
    kite = StubKite(Market())
    engine, desk, prices = setup(tmp_path, clock, kite=kite, live_enabled=True)
    kite.prices = prices
    algo = engine.create({**CONDOR, "mode": "live", "max_loss": 3000})
    engine.set_enabled(algo["id"], True, arm_phrase(algo["name"]))
    engine.evaluate(algo["id"])
    for p in list(kite.placed):
        kite.fill(p["order_id"])
    real = kite.place_order

    def refuse(args):
        if args["transaction_type"] == "SELL" and args["tradingsymbol"].endswith(("25200CE", "24800PE")):
            raise KiteError("Insufficient funds")
        return real(args)
    kite.place_order = refuse
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"]["phase"] == "closing" and "entry given up" in out["algo"]["book"]["reason"]
    unwind = kite.placed[2:]
    assert [(p["transaction_type"], p["tradingsymbol"][-7:]) for p in unwind] == [("SELL", "25400CE"), ("SELL", "24600PE")]


# -- futures on a signal -------------------------------------------------------------------------------------------

def test_a_futures_algo_follows_the_day_signal_and_rolls_at_expiry(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock, max_order_value=500_000.0)
    days = pd.bdate_range(end="2026-11-16", periods=120)
    closes = np.r_[np.linspace(26000, 24000, 80), np.linspace(24000, 25500, 40)]
    bars = pd.DataFrame({"Open": closes, "High": closes * 1.003, "Low": closes * 0.997, "Close": closes, "Volume": 1e6}, index=days)
    engine.fetch = lambda symbol, interval="1d": bars
    algo = engine.create({"name": "Nifty fut", "kind": "futures", "direction": "long", "underlying": "NIFTY",
                          "strategy": "sma_cross", "strategy_params": {"fast": 5, "slow": 20}, "mode": "paper",
                          "entry_time": "09:35", "exit_time": "15:10"})
    engine.set_enabled(algo["id"], True)
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and "fresh signal" in out["algo"]["log"][-1]["message"]   # no joining midway
    algo_state = engine.get(algo["id"])
    algo_state["ready"] = True                              # as if the cross happened today
    out = engine.evaluate(algo["id"])
    leg = out["algo"]["book"]["legs"][0]
    assert leg["tradingsymbol"] == "NIFTY26NOVFUT" and leg["quantity"] == 65
    assert desk.paper.quantity("NFO:NIFTY26NOVFUT") == 65
    assert any(r["product"] == "NRML" for r in journal(desk))
    clock.set("2026-11-24 15:10")                          # expiry day, exit time: closed, ready to roll
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"] is None and out["algo"]["history"][-1]["reason"].startswith("expiry")
    clock.set("2026-11-25 09:40")
    engine.fetch = lambda symbol, interval="1d": pd.concat([bars, bars.iloc[-1:].set_axis(pd.DatetimeIndex(["2026-11-24"]))])
    out = engine.evaluate(algo["id"])
    assert out["algo"]["book"]["legs"][0]["tradingsymbol"] == "NIFTY26DECFUT"


def test_futures_orders_are_weighed_by_margin_and_charged_as_futures(tmp_path):
    clock = Clock("2026-11-17 10:00")
    _, desk, prices = setup(tmp_path, clock)
    raw = {"exchange": "NFO", "tradingsymbol": "NIFTY26NOVFUT", "side": "BUY", "quantity": 65, "product": "NRML",
           "order_type": "LIMIT", "price": 25040.0, "intent": "open"}
    review = desk.preview(raw, "paper")
    assert any("Margin of about" in e for e in review["errors"])           # 12% of ₹16.3 lakh is above ₹50,000
    desk.settings.update({"max_order_value": 250_000.0})
    review = desk.preview(raw, "paper")
    assert review["ok"] and review["charges"]["stt"] == 0                  # futures STT is on the sell side only
    fill = desk.place(review["token"], review["phrase"])
    cash = desk.paper.cash
    assert cash > 1_000_000 - 100                                          # no notional paid for a future
    prices.override["NFO:NIFTY26NOVFUT"] = 25140.0
    sell = desk.preview({**raw, "side": "SELL", "intent": "close", "price": 25140.0}, "paper")
    desk.place(sell["token"], sell["phrase"])
    assert desk.paper.cash == pytest.approx(cash + 65 * 100 - sell["charges"]["total"], abs=0.01)
    assert desk.pnl("paper")["realized"] == pytest.approx(65 * 100 - review["charges"]["total"] - sell["charges"]["total"], abs=0.05)
    assert fill["ok"]


def test_short_option_positions_settle_against_the_writer(tmp_path):
    clock = Clock("2026-11-17 10:00")
    engine, desk, prices = setup(tmp_path, clock)
    raw = {"exchange": "NFO", "tradingsymbol": symbol(NEAR, 25000, "PE"), "side": "SELL", "quantity": 65,
           "product": "NRML", "order_type": "LIMIT", "price": 100.0, "intent": "open"}
    prices.override[f"NFO:{raw['tradingsymbol']}"] = 100.0
    review = desk.preview(raw, "paper", allow_short=True)
    desk._execute(desk._take(review["token"]), "fno:test")
    clock.set("2026-11-24 15:45")
    settled = desk.settle_paper_options(lambda u, e: 24900.0)               # the put expires ₹100 in the money
    assert settled[0]["side"] == "BUY" and settled[0]["charges"] == 0
    assert desk.paper.quantity(f"NFO:{raw['tradingsymbol']}") == 0
    report = desk.pnl("paper")
    assert report["realized"] == pytest.approx(-review["charges"]["total"], abs=0.05)   # premium in, intrinsic out


# -- SEBI's API order rules -----------------------------------------------------------------------------------------

def test_kite_ip_and_rate_refusals_are_explained():
    assert "IP Whitelist" in explain_kite_refusal("IP 1.2.3.4 is not allowed to place orders for this app")
    assert "10 API orders a second" in explain_kite_refusal("HTTP 429 Too Many Requests")
    assert explain_kite_refusal("Insufficient funds") == "Insufficient funds"


def test_live_orders_are_paced_under_the_exchange_rate_cap(tmp_path, monkeypatch):
    clock = Clock("2026-11-17 10:00")
    _, desk, _ = setup(tmp_path, clock)
    now = [100.0]
    slept = []
    monkeypatch.setattr(broker_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(broker_module.time, "sleep", lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
    for _ in range(broker_module.LIVE_ORDERS_PER_SECOND + 2):
        desk._pace()
    assert len(slept) == 1 and 0.9 < slept[0] <= 1.02


def test_a_sold_leg_reenters_when_its_premium_comes_back(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "name": "Again", "structure": "short_straddle_hedged", "w": 9,
                          "leg_exits": {"unit": "pct", "stop_loss": 30, "reentry": 1}})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    call = next(g for g in book["legs"] if g["action"] < 0 and g["type"] == "CE")
    entry = call["avg"]
    prices.override[call["instrument"]] = round(entry * 1.35, 2)                 # stoploss: bought back
    clock.set("2026-11-17 10:30")
    book = engine.evaluate(algo["id"])["algo"]["book"]
    stopped = next(g for g in book["legs"] if g["tradingsymbol"] == call["tradingsymbol"])
    assert stopped["open_qty"] == 0 and stopped["rearm"]["left"] == 1
    prices.override[call["instrument"]] = round(entry * 1.1, 2)                 # not back yet
    clock.set("2026-11-17 10:40")
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert sum(1 for g in book["legs"] if g["tradingsymbol"] == call["tradingsymbol"]) == 1
    prices.override[call["instrument"]] = round(entry * 0.99, 2)                # back at the entry: sold again
    clock.set("2026-11-17 10:50")
    out = engine.evaluate(algo["id"])["algo"]
    again = [g for g in out["book"]["legs"] if g["tradingsymbol"] == call["tradingsymbol"]]
    assert len(again) == 2 and again[1]["open_qty"] == again[1]["quantity"] and again[1]["reentries_left"] == 0
    assert any("Re-entry 1 of 1" in e["message"] for e in out["log"])
    prices.override[call["instrument"]] = round(entry * 1.5, 2)                 # stopped again: no re-entry left
    clock.set("2026-11-17 11:00")
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert not any(g.get("rearm") for g in book["legs"])
    assert engine.list()[0]["leg_exits_text"].startswith("stoploss 30%")


def test_a_bought_hedge_exit_closes_every_leg(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create({**CONDOR, "name": "Wings", "leg_scope": "all",
                          "leg_exits": {"unit": "pct", "stop_loss": 50}})
    engine.set_enabled(algo["id"], True)
    book = engine.evaluate(algo["id"])["algo"]["book"]
    wing = next(g for g in book["legs"] if g["action"] > 0)
    prices.override[wing["instrument"]] = round(wing["avg"] * 0.4, 2)
    clock.set("2026-11-17 10:30")
    out = engine.evaluate(algo["id"])["algo"]
    assert out["book"] is None and "covers a sold leg" in out["history"][-1]["reason"]


def test_bad_leg_exit_rules_are_refused(tmp_path):
    engine, _, _ = setup(tmp_path, Clock("2026-11-17 09:35"))
    with pytest.raises(AlgoError, match="re-entry happens after a stoploss"):
        engine.create({**CONDOR, "name": "Bad", "leg_exits": {"reentry": 2}})
    with pytest.raises(AlgoError, match="sold legs, bought legs or all legs"):
        engine.create({**CONDOR, "name": "Bad", "leg_scope": "wings"})


# -- editing and the per-leg detail ---------------------------------------------------------------------------------

def test_an_algo_not_trading_live_can_be_edited_without_touching_its_open_legs(tmp_path):
    clock = Clock("2026-11-17 09:35")
    engine, desk, prices = setup(tmp_path, clock)
    algo = engine.create(CONDOR)
    edited = engine.update(algo["id"], {**CONDOR, "name": "Renamed", "k": 3, "lots": 2, "entry_time": "09:40"})
    assert edited["name"] == "Nifty condor" and edited["k"] == 3 and edited["lots"] == 2      # the name is fixed
    engine.set_enabled(algo["id"], True)
    clock.set("2026-11-17 09:40")
    book = engine.evaluate(algo["id"])["algo"]["book"]
    assert book["phase"] == "open"
    with pytest.raises(AlgoError, match="open legs"):
        engine.update(algo["id"], {**CONDOR, "k": 3, "lots": 1, "entry_time": "09:40"})
    with pytest.raises(AlgoError, match="open legs"):
        engine.update(algo["id"], {**CONDOR, "k": 3, "lots": 2, "entry_time": "09:40", "mode": "live", "max_loss": 9000})
    legs_before = json.dumps(engine.get(algo["id"])["book"])
    edited = engine.update(algo["id"], {**CONDOR, "k": 3, "lots": 2, "entry_time": "09:40", "max_loss": 7000, "target": 900})
    assert edited["max_loss"] == 7000 and edited["target"] == 900 and edited["enabled"]
    assert json.dumps(engine.get(algo["id"])["book"]) == legs_before                          # legs untouched
    # Per-leg detail from the ledger: bought and sold prices, booked and open P&L, active or closed.
    legs = desk.ledger.legs("fno:Nifty condor", lambda i: prices.price(i))
    assert len(legs) == 4 and all(g["status"] == "Active" and g["realized"] == 0 for g in legs)
    sold = [g for g in legs if g["sold"]]
    assert len(sold) == 2 and all(g["open_quantity"] == -130 and g["sell_price"] and g["buy_price"] is None for g in sold)
    clock.set("2026-11-17 14:55")
    engine.evaluate(algo["id"])
    legs = desk.ledger.legs("fno:Nifty condor", lambda i: prices.price(i))
    assert all(g["status"] == "Closed" and g["unrealized"] == 0 and g["buy_price"] and g["sell_price"] for g in legs)
    assert sum(g["realized"] for g in legs) == pytest.approx(engine.get(algo["id"])["history"][-1]["pnl"], abs=0.05)


def test_a_live_running_algo_cannot_be_edited_and_a_mode_change_stops_it(tmp_path):
    clock = Clock("2026-11-17 09:00")
    engine, desk, _ = setup(tmp_path, clock, kite=StubKite(Market()))
    desk.settings.update({"live_enabled": True})
    algo = engine.create({**CONDOR, "mode": "live", "max_loss": 3000})
    engine.set_enabled(algo["id"], True, arm_phrase(algo["name"]))
    with pytest.raises(AlgoError, match="trading live"):
        engine.update(algo["id"], {**CONDOR, "mode": "live", "max_loss": 4000})
    engine.set_enabled(algo["id"], False)
    assert engine.update(algo["id"], {**CONDOR, "mode": "live", "max_loss": 4000})["max_loss"] == 4000
    paper = engine.create({**CONDOR, "name": "Paper one"})
    engine.set_enabled(paper["id"], True)
    moved = engine.update(paper["id"], {**CONDOR, "mode": "live", "max_loss": 3000})
    assert moved["mode"] == "live" and not moved["enabled"]                                    # Start and the phrase again
