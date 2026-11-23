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


def test_levels_on_the_wrong_side_or_too_much_risk_are_refused_and_the_token_dies(tmp_path):
    charts, desk, _ = make(tmp_path, Clock("2026-11-16 10:00"), max_risk_pct=1.0, risk_capital=100_000)
    bad = charts.preview(buy(stop=1010.0, target=990.0))
    assert not bad["ok"] and bad["token"] is None
    assert any("stoploss" in e and "below" in e for e in bad["errors"]) and any("target" in e and "above" in e for e in bad["errors"])
    risky = charts.preview(buy(stop=850.0, quantity=100))
    assert not risky["ok"] and any("Your limit is 1% a trade" in e for e in risky["errors"])
    close = charts.preview(buy(stop=999.0, target=None))
    assert not close["ok"] and any("0.25%" in e for e in close["errors"])
    with pytest.raises(OrderRejected, match="for buys"):
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
        assert r.status_code == 422 and "for buys" in r.json()["error"]
        r = client.post("/api/chart-orders/missing/change/preview", json={"stop": 900})
        assert r.status_code == 422
