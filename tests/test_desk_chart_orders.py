"""Orders from the chart: a buy with its stoploss and target (paper, live delivery GTT, live intraday stoploss order
with a desk-watched target), changed by dragging a line, removed; and crypto paper stoploss/target."""

import copy
from datetime import datetime

import pytest

pytest.importorskip("fastapi")

import tradingagents.dataflows.config as config_module
from desk import market
from desk.broker import Desk, OrderRejected, PaperBook
from desk.chartorders import ChartOrders
from desk.cryptopaper import CryptoPaper, PaperError
from desk.kite import KiteError
from desk.settings import Settings

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
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


class FakeKite:
    """Just enough of KiteMCP for the order path, with order states the test sets."""

    def __init__(self, price=1000.0, tools=None):
        self.connected = True
        self.price = price
        self.tools = dict.fromkeys(tools if tools is not None else (
            "place_order", "get_ltp", "get_orders", "place_gtt_order", "get_gtts", "delete_gtt_order",
            "cancel_order", "modify_order", "modify_gtt_order"))
        self.can_trade = True
        self.orders_list, self.gtt_list, self.calls = [], [], []

    def ltp(self, instruments):
        return dict.fromkeys(instruments, self.price)

    def place_order(self, args):
        order_id = f"O{len(self.orders_list) + 1}"
        self.orders_list.append({"order_id": order_id, "status": "OPEN", "filled_quantity": 0, **args})
        return {"order_id": order_id}

    def orders(self):
        return [dict(o) for o in self.orders_list]

    def order(self, order_id):
        return next(o for o in self.orders_list if o["order_id"] == order_id)

    def modify_order(self, order_id, variety="regular", **changes):
        row = self.order(order_id)
        if row["status"] != "OPEN":
            raise KiteError("order is not open")
        self.calls.append(("modify_order", order_id, changes))
        row.update(changes)

    def cancel_order(self, order_id, variety="regular"):
        self.order(order_id)["status"] = "CANCELLED"

    def place_gtt(self, args):
        trigger_id = 500 + len(self.gtt_list)
        self.gtt_list.append({"id": trigger_id, "status": "active", **args})
        return {"trigger_id": trigger_id}

    def modify_gtt(self, trigger_id, args):
        self.calls.append(("modify_gtt", trigger_id, args))
        row = next(g for g in self.gtt_list if g["id"] == int(trigger_id))
        row.update(args)

    def delete_gtt(self, trigger_id):
        next(g for g in self.gtt_list if g["id"] == int(trigger_id))["status"] = "deleted"

    def gtts(self):
        return [dict(g) for g in self.gtt_list]

    def positions(self):
        """Net positions from the orders that filled, as Kite lists them (below zero when short)."""
        net = {}
        for o in self.orders_list:
            if o["status"] == "COMPLETE":
                key = (o["exchange"], o["tradingsymbol"], o["product"])
                net[key] = net.get(key, 0) + int(o.get("filled_quantity") or o["quantity"]) * (1 if o["transaction_type"] == "BUY" else -1)
        return [{"exchange": e, "tradingsymbol": t, "product": p, "quantity": q} for (e, t, p), q in net.items()]


def make(tmp_path, clock, kite=None, **settings):
    cfg = Settings(tmp_path / "settings.json")
    cfg.update({"live_enabled": True, "max_order_value": 1_000_000, **settings})
    kite = kite or FakeKite()
    desk = Desk(cfg, kite, PaperBook(1_000_000, tmp_path / "paper.json"), tmp_path / "orders.jsonl",
                price_lookup=lambda s: kite.price, now=clock)
    return ChartOrders(desk, tmp_path / "chart_orders.json"), desk, kite


def buy(mode="paper", product="CNC", stop=980.0, target=1050.0, quantity=10, **order):
    return {"mode": mode, "stop": stop, "target": target,
            "order": {"exchange": "NSE", "tradingsymbol": "RELIANCE", "side": "BUY", "quantity": quantity,
                      "product": product, "order_type": "LIMIT", "price": 1000.0, **order}}


def place(charts, raw):
    review = charts.preview(raw)
    assert review["ok"], review["errors"]
    return charts.place(review["token"], review["phrase"])


def test_a_paper_buy_from_the_chart_fills_and_gets_an_oco_gtt_that_sells_at_the_target(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    review = charts.preview(buy())
    assert review["ok"] and review["entry"] == 1000.0 and review["risk"]["loss"] > 200
    assert 400 < review["reward"] < 500        # 50 a share on 10, less the round trip's charges
    assert "OCO GTT" in review["protection"] and review["margin"]["amount"] == 10_000
    out = charts.place(review["token"], review["phrase"])
    item = out["chart_order"]
    assert item["state"] == "protected" and item["protection"]["kind"] == "gtt"
    gtt = desk.paper.gtt(item["protection"]["id"])
    assert gtt["kind"] == "oco" and gtt["stop"] == 980.0 and gtt["target"] == 1050.0 and gtt["quantity"] == 10
    kite.price = 1051.0
    desk.check_paper_gtts()
    charts.tick()
    done = charts.view()["items"][0]
    assert done["state"] == "closed" and "Target hit" in done["closed"]
    assert desk.paper.quantity("NSE:RELIANCE") == 0
    closed = desk.ledger.closed_trades("paper")      # the GTT's sell closes the trade the chart buy opened
    assert len(closed) == 1 and closed[0]["side"] == "LONG" and closed[0]["pnl"] > 0


def test_levels_on_the_wrong_side_or_too_much_risk_are_refused_and_the_token_dies(tmp_path):
    charts, desk, _ = make(tmp_path, Clock("2026-11-16 10:00"), max_risk_pct=1.0, risk_capital=100_000)
    bad = charts.preview(buy(stop=1010.0, target=990.0))
    assert not bad["ok"] and bad["token"] is None
    assert any("stoploss" in e and "below" in e for e in bad["errors"]) and any("target" in e and "above" in e for e in bad["errors"])
    risky = charts.preview(buy(stop=850.0, quantity=100))
    assert not risky["ok"] and any("Your limit is 1% a trade" in e for e in risky["errors"])
    close = charts.preview(buy(stop=999.0, target=None))
    assert not close["ok"] and any("0.25%" in e for e in close["errors"])
    with pytest.raises(OrderRejected, match="for a new buy or short sale"):
        charts.preview(buy(side="SELL"))
    # a refused chart order leaves no working desk token behind
    assert not desk._tokens


def test_the_kill_switch_and_trade_window_stop_a_chart_buy(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, _, _ = make(tmp_path, clock, kill_switch=True)
    assert any("kill switch" in e for e in charts.preview(buy())["errors"])
    clock.set("2026-11-16 15:10")
    (tmp_path / "b").mkdir()
    charts2, _, _ = make(tmp_path / "b", clock, enforce_trade_window=True, trade_end="15:00")
    assert any("trade window" in e for e in charts2.preview(buy())["errors"])


def test_a_live_delivery_buy_is_protected_by_a_gtt_after_kite_reports_the_fill_and_a_drag_modifies_it(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    item = place(charts, buy(mode="live"))["chart_order"]
    assert item["state"] == "waiting" and not kite.gtt_list
    charts.tick()
    assert charts.view()["items"][0]["state"] == "waiting"
    kite.order("O1").update(status="COMPLETE", filled_quantity=10, average_price=999.5)
    charts.tick()
    item = charts.view()["items"][0]
    assert item["state"] == "protected" and item["entry_price"] == 999.5
    assert kite.gtt_list[0]["trigger_type"] == "two-leg" and kite.gtt_list[0]["lower_trigger_value"] == 980.0
    change = charts.change_preview(item["id"], {"stop": 990.0, "target": 1060.0})
    assert change["ok"] and change["before"] == {"stop": 980.0, "target": 1050.0}
    charts.change(change["token"])
    assert kite.calls[-1][0] == "modify_gtt" and kite.gtt_list[0]["lower_trigger_value"] == 990.0
    assert kite.gtt_list[0]["upper_trigger_value"] == 1060.0 and len(kite.gtt_list) == 1
    with pytest.raises(OrderRejected, match="expired or was already used"):
        charts.change(change["token"])
    above = charts.change_preview(item["id"], {"stop": 1001.0, "target": 1060.0})
    assert not above["ok"] and "To sell now, use Sell" in above["errors"][0]
    kite.gtt_list[0]["status"] = "triggered"
    charts.tick()
    assert charts.view()["items"][0]["state"] == "closed"


def test_without_a_gtt_modify_tool_the_desk_replaces_the_gtt_and_says_so(tmp_path):
    kite = FakeKite(tools=("place_order", "get_ltp", "get_orders", "place_gtt_order", "get_gtts", "delete_gtt_order",
                           "cancel_order"))
    charts, _, _ = make(tmp_path, Clock("2026-11-16 10:00"), kite=kite)
    item = place(charts, buy(mode="live"))["chart_order"]
    kite.order("O1").update(status="COMPLETE", filled_quantity=10, average_price=1000.0)
    charts.tick()
    change = charts.change_preview(item["id"], {"stop": 970.0, "target": 1050.0})
    assert change["ok"] and any("deletes the old GTT" in w for w in change["warnings"])
    after = charts.change(change["token"])
    assert kite.gtt_list[0]["status"] == "deleted" and kite.gtt_list[1]["status"] == "active"
    assert after["protection"]["id"] == "501" and after["stop"] == 970.0


def test_a_live_intraday_buy_keeps_one_sell_order_that_becomes_the_target_when_the_price_gets_there(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    item = place(charts, buy(mode="live", product="MIS"))["chart_order"]
    kite.order("O1").update(status="COMPLETE", filled_quantity=10, average_price=1000.0)
    charts.tick()
    stop_order = kite.order("O2")
    assert stop_order["order_type"] == "SL" and stop_order["trigger_price"] == 980.0 and stop_order["price"] < 980.0
    assert stop_order["transaction_type"] == "SELL" and stop_order["product"] == "MIS"
    # dragging the stoploss moves that order
    change = charts.change_preview(item["id"], {"stop": 985.0, "target": 1050.0})
    charts.change(change["token"])
    assert kite.order("O2")["trigger_price"] == 985.0
    # adding or dropping a leg is refused for intraday
    assert not charts.change_preview(item["id"], {"stop": 985.0})["ok"]
    kite.price = 1050.5
    charts.tick()
    assert kite.order("O2")["order_type"] == "LIMIT" and kite.order("O2")["price"] == 1050.0
    assert len(kite.orders_list) == 2           # never a second sell order
    kite.price = 984.0                          # fell back before the target sold
    charts.tick()
    assert kite.order("O2")["price"] < 984.0
    kite.order("O2")["status"] = "COMPLETE"
    kite.order("O2")["average_price"] = 980.0
    charts.tick()
    assert charts.view()["items"][0]["state"] == "closed"


def short(mode="paper", stop=1020.0, target=950.0, quantity=10, **order):
    raw = buy(mode=mode, product="MIS", stop=stop, target=target, quantity=quantity, side="SELL", **order)
    raw["order"]["short"] = True
    return raw


def test_a_paper_short_sale_fills_counts_as_a_new_position_and_the_desk_buys_back_at_the_stoploss(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    review = charts.preview(short())
    assert review["ok"], review["errors"]
    assert review["kind"] == "short" and review["phrase"] == "SHORT SELL 10 RELIANCE" and review["risk"]["loss"] > 200
    assert review["order"]["intent"] == "open" and "buys back" in review["protection"]
    cash = desk.paper.cash
    placed = charts.place(review["token"], review["phrase"])
    assert desk.paper.quantity("NSE:RELIANCE") == -10 and desk.paper.cash > cash          # the sale's proceeds
    item = placed["chart_order"]
    assert item["side"] == "short" and item["state"] == "protected" and item["protection"] == {"kind": "watch"}
    assert desk.orders_today("paper") == 1                       # counted against the orders-a-day limit
    # a drag moves the levels; the stoploss must stay above the price
    assert not charts.change_preview(item["id"], {"stop": 990.0, "target": 950.0})["ok"]
    charts.change(charts.change_preview(item["id"], {"stop": 1015.0, "target": 950.0})["token"])
    kite.price = 1010.0
    charts.tick()
    assert desk.paper.quantity("NSE:RELIANCE") == -10
    kite.price = 1016.0
    charts.tick()
    assert desk.paper.quantity("NSE:RELIANCE") == 0
    done = charts.view()["items"][0]
    assert done["state"] == "closed" and "Stoploss hit: bought back 10" in done["closed"]
    closed = desk.ledger.closed_trades("paper")
    assert len(closed) == 1 and closed[0]["side"] == "SHORT" and closed[0]["pnl"] < 0


def test_a_short_sale_obeys_the_kill_switch_window_and_limits_and_is_intraday_only(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    desk.settings.update({"kill_switch": True})
    assert any("no new short sales" in e for e in charts.preview(short())["errors"])
    desk.settings.update({"kill_switch": False, "max_order_value": 5000})
    assert any("limit per order" in e for e in charts.preview(short())["errors"])
    desk.settings.update({"max_order_value": 1_000_000, "max_risk_pct": 0.01})
    assert any("Your limit is" in e for e in charts.preview(short())["errors"])
    desk.settings.update({"max_risk_pct": 2})
    with pytest.raises(OrderRejected, match="Delivery \\(CNC\\) shares cannot be sold short"):
        raw = short()
        raw["order"]["product"] = "CNC"
        charts.preview(raw)
    bad = charts.preview(short(stop=990.0, target=1010.0))
    assert any("above the sale price" in e for e in bad["errors"]) and any("below the sale price" in e for e in bad["errors"])
    clock.set("2026-11-16 15:10")
    assert any("New short sales are taken only between" in e for e in charts.preview(short())["errors"])
    # a plain Sell never turns into a short sale, and a short never starts from a long
    clock.set("2026-11-16 10:00")
    assert any("cannot sell 10" in e for e in charts.preview(buy(side="SELL", stop=None, target=None))["errors"])
    place(charts, buy(stop=None, target=None, product="MIS"))
    with pytest.raises(OrderRejected, match="Sell those first"):
        charts.preview(short())


def test_a_buy_while_short_buys_back_and_paper_shorts_are_bought_back_at_the_square_off(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    place(charts, short(stop=None, target=None, quantity=20))
    cover = charts.preview(buy(stop=None, target=None, product="MIS", quantity=5))
    assert cover["ok"] and cover["kind"] == "cover" and cover["order"]["intent"] == "close"
    desk.settings.update({"kill_switch": True})            # buying back is never blocked
    cover = charts.preview(buy(stop=None, target=None, product="MIS", quantity=5))
    assert cover["ok"], cover["errors"]
    charts.place(cover["token"], cover["phrase"])
    assert desk.paper.quantity("NSE:RELIANCE") == -15
    assert any("would buy more than you sold" in e
               for e in charts.preview(buy(stop=None, target=None, product="MIS", quantity=16))["errors"])
    with pytest.raises(OrderRejected, match="for a new buy or short sale"):
        charts.preview(buy(product="MIS", quantity=5))
    desk.settings.update({"kill_switch": False})
    item = place(charts, short(quantity=5))["chart_order"]
    assert desk.paper.quantity("NSE:RELIANCE") == -20
    clock.set("2026-11-16 14:59")
    charts.tick()
    assert desk.paper.quantity("NSE:RELIANCE") == -20
    clock.set("2026-11-16 15:00")                             # the square-off time in Settings
    charts.tick()
    assert desk.paper.quantity("NSE:RELIANCE") == 0
    assert next(i for i in charts.view()["items"] if i["id"] == item["id"])["state"] == "closed"
    desk.settings.update({"enforce_trade_window": False})
    assert any("past the square-off time" in e for e in charts.preview(short())["errors"])


def test_a_live_intraday_short_keeps_one_buy_order_that_becomes_the_target_when_the_price_falls(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    review = charts.preview(short(mode="live"))
    assert review["ok"], review["errors"]
    item = charts.place(review["token"], review["phrase"])["chart_order"]
    assert kite.order("O1")["transaction_type"] == "SELL" and kite.order("O1")["product"] == "MIS"
    kite.order("O1").update(status="COMPLETE", filled_quantity=10, average_price=1000.0)
    charts.tick()
    stop_order = kite.order("O2")
    assert stop_order["transaction_type"] == "BUY" and stop_order["order_type"] == "SL"
    assert stop_order["trigger_price"] == 1020.0 and stop_order["price"] > 1020.0
    # while the stoploss order works, a Buy from the chart is refused: it would leave that order to open a long
    assert any("still working at Zerodha" in e
               for e in charts.preview(buy(mode="live", product="MIS", stop=None, target=None))["errors"])
    kite.price = 949.5
    charts.tick()
    assert kite.order("O2")["order_type"] == "LIMIT" and kite.order("O2")["price"] == 950.0
    assert len(kite.orders_list) == 2
    kite.price = 1021.0
    charts.tick()
    assert kite.order("O2")["price"] > 1021.0
    kite.order("O2").update(status="COMPLETE", average_price=1022.0)
    charts.tick()
    assert "bought back 10" in charts.view()["items"][0]["closed"]
    assert item["side"] == "short"


def test_a_live_intraday_sell_beyond_what_you_hold_is_refused_unless_it_is_a_short_sale(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    with pytest.raises(OrderRejected, match="choose Short sell"):
        charts.preview(buy(mode="live", product="MIS", side="SELL", stop=None, target=None))
    assert charts.preview(short(mode="live", stop=None, target=None))["ok"]


def test_a_protection_that_kite_refuses_is_reported_and_removing_cancels_an_unfilled_buy(tmp_path):
    clock = Clock("2026-11-16 10:00")
    charts, desk, kite = make(tmp_path, clock)
    place(charts, buy(mode="live"))
    kite.price = 970.0                           # the price fell under the stoploss before the fill was seen
    kite.order("O1").update(status="COMPLETE", filled_quantity=10, average_price=1000.0)
    charts.tick()
    got = charts.view()["items"][0]
    assert got["state"] == "unprotected" and "NOT placed" in got["log"][-1]
    second = place(charts, buy(mode="live", price=969.0, stop=950.0, target=990.0))["chart_order"]
    removed = charts.remove(second["id"])
    assert removed["state"] == "removed" and kite.order(second["entry_order_id"])["status"] == "CANCELLED"


def test_chart_orders_survive_a_restart(tmp_path):
    charts, desk, _ = make(tmp_path, Clock("2026-11-16 10:00"))
    place(charts, buy())
    again = ChartOrders(desk, tmp_path / "chart_orders.json")
    assert again.view("NSE", "RELIANCE")["open"] == 1 and again.view("NSE", "INFY")["items"] == []


# -- crypto paper -----------------------------------------------------------------------------------------------

class Prices:
    def __init__(self, **prices):
        self.prices = prices

    def __call__(self, symbols):
        return {s: {"price": self.prices[s]} for s in symbols if s in self.prices}


def test_crypto_paper_stoploss_and_target_guard_the_coins_and_sell_once(tmp_path):
    prices = Prices(BTCUSDT=100.0)
    paper = CryptoPaper(tmp_path / "c.json", prices, start_worker=False)
    r = paper.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 2, "stop": 90, "target": 120})
    assert r["bracket"]["quantity"] == 2
    with pytest.raises(PaperError, match="held for your stoploss and target"):
        paper.place({"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": 1})
    with pytest.raises(PaperError, match="below the last price"):
        paper.change_bracket(r["bracket"]["id"], {"stop": 101, "target": 120})
    paper.change_bracket(r["bracket"]["id"], {"stop": 95, "target": 120})
    prices.prices["BTCUSDT"] = 94.0
    fills = paper.check()
    assert len(fills) == 1 and fills[0]["kind"] == "stoploss" and fills[0]["price"] == 94.0
    assert paper.view()["positions"] == [] and paper.view()["brackets"] == []
    assert paper.check() == []


def test_crypto_paper_limit_buy_carries_its_levels_and_protect_adds_them_to_held_coins(tmp_path):
    prices = Prices(BTCUSDT=100.0)
    paper = CryptoPaper(tmp_path / "c.json", prices, start_worker=False)
    with pytest.raises(PaperError, match="below the buy price"):
        paper.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": 95, "quantity": 1, "stop": 96})
    paper.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "price": 95, "quantity": 1, "stop": 90, "target": 110})
    prices.prices["BTCUSDT"] = 94.0
    paper.check()
    assert paper.view()["brackets"][0]["entry"] == 95
    prices.prices["BTCUSDT"] = 111.0
    assert paper.check()[0]["kind"] == "target"
    paper.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 3})
    bracket = paper.protect({"symbol": "BTCUSDT", "target": 130})
    assert bracket["quantity"] == 3 and bracket["stop"] is None
    with pytest.raises(PaperError, match="No free"):
        paper.protect({"symbol": "BTCUSDT", "stop": 100})
    paper.cancel_bracket(bracket["id"])
    assert paper.view()["brackets"] == []


def test_chart_order_api_round_trip(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app

    def offline(*args, **kwargs):
        raise OSError("offline")

    app = create_app(settings=Settings(tmp_path / "s.json"), token="t", fetch=offline)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as client:
        r = client.post("/api/chart-orders/preview", json={"symbol": "IDX:nifty", "side": "BUY", "quantity": 1})
        assert r.status_code == 400 and "index" in r.json()["error"]
        r = client.get("/api/chart-orders?symbol=RELIANCE")
        assert r.status_code == 200 and r.json()["tradingsymbol"] == "RELIANCE" and r.json()["items"] == []
        r = client.post("/api/chart-orders/preview", json={"symbol": "RELIANCE", "side": "SELL", "quantity": 1,
                                                           "stop": 900})
        assert r.status_code == 422 and "for a new buy or short sale" in r.json()["error"]
        r = client.post("/api/chart-orders/missing/change/preview", json={"stop": 900})
        assert r.status_code == 422
