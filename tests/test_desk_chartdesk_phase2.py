"""Charts tab phase 2: crypto paper trading, watchlist and drawings storage, CSV import, and their API."""
from __future__ import annotations

import sys
from datetime import datetime

import pytest

from desk import market
from desk.chartstore import ChartStore
from desk.cryptopaper import FEE, MAX_TRADES, CryptoPaper, PaperError
from desk.marketdata import MarketDataError
from desk.marketdata.crypto import CryptoFeed, tickers
from desk.marketdata.csvfeed import Imports, parse
from desk.settings import Settings

FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}


class Prices:
    def __init__(self, **prices):
        self.prices = prices

    def __call__(self, symbols):
        return {s: ({"price": self.prices[s]} if s in self.prices else {"error": "feed down"}) for s in symbols}


@pytest.fixture
def book(tmp_path):
    prices = Prices(BTCUSDT=100.0, ETHUSDT=10.0)
    clock = [1000.0]
    paper = CryptoPaper(tmp_path / "p.json", prices, now=lambda: clock[0], start_worker=False)
    return paper, prices, tmp_path


def test_market_buy_and_sell_with_fees_and_realised_profit(book):
    paper, prices, _ = book
    r = paper.place({"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 2})
    assert r["filled"] and r["trade"]["price"] == 100.0
    view = paper.view()
    assert view["cash"] == pytest.approx(10_000 - 200 - 200 * FEE)
    assert view["positions"][0]["quantity"] == 2 and view["positions"][0]["avg"] == 100.0
    prices.prices["BTCUSDT"] = 150.0
    view = paper.view()
    assert view["positions"][0]["unrealized"] == pytest.approx(100.0)
    assert view["equity"] == pytest.approx(10_000 - 0.2 + 100.0)
    paper.place({"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": 2})
    view = paper.view()
    assert view["positions"] == [] and view["realized"] == pytest.approx(100.0)
    assert view["pnl"] == pytest.approx(100.0 - 0.2 - 0.3)
    assert [t["side"] for t in view["trades"]] == ["SELL", "BUY"]


def test_limit_orders_wait_reserve_and_fill_at_their_price(book):
    paper, prices, _ = book
    r = paper.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": 50, "price": 90})
    assert not r["filled"]
    with pytest.raises(PaperError, match="Not enough paper cash"):
        paper.place({"symbol": "ETHUSDT", "side": "BUY", "type": "MARKET", "quantity": 600})   # 6,000 + fee > free
    assert paper.check() == []
    prices.prices["BTCUSDT"] = 89.0
    fills = paper.check()
    assert len(fills) == 1 and fills[0]["price"] == 90.0 and fills[0]["kind"] == "limit"
    assert paper.view()["orders"] == []
    # a sell limit holds its coins
    paper.place({"symbol": "BTCUSDT", "side": "SELL", "type": "LIMIT", "quantity": 50, "price": 120})
    with pytest.raises(PaperError, match="free to sell"):
        paper.place({"symbol": "BTCUSDT", "side": "SELL", "type": "MARKET", "quantity": 1})
    order = paper.view()["orders"][0]
    paper.cancel(order["id"])
    with pytest.raises(PaperError, match="no longer open"):
        paper.cancel(order["id"])


def test_marketable_limit_fills_now_at_the_last_price(book):
    paper, _, _ = book
    r = paper.place({"symbol": "BTCUSDT", "side": "BUY", "type": "LIMIT", "quantity": 1, "price": 105})
    assert r["filled"] and r["trade"]["price"] == 100.0


def test_refusals_and_no_price_never_trade(book):
    paper, prices, _ = book
    for bad, match in [({"side": "BUY", "quantity": 0}, "above zero"), ({"side": "HOLD", "quantity": 1}, "Buy or Sell"),
                       ({"side": "BUY", "quantity": 0.001}, "smallest is 1 USDT"),
                       ({"side": "SELL", "quantity": 1}, "does not short"),
                       ({"side": "BUY", "quantity": 1, "type": "LIMIT", "price": -1}, "above zero")]:
        with pytest.raises(PaperError, match=match):
            paper.place({"symbol": "BTCUSDT", "type": "MARKET", **bad})
    with pytest.raises(ValueError, match="not in the crypto list"):
        paper.place({"symbol": "NOPE", "side": "BUY", "quantity": 1})
    with pytest.raises(PaperError, match="No live price for SOLUSDT"):
        paper.place({"symbol": "SOLUSDT", "side": "BUY", "quantity": 1})
    paper.place({"symbol": "ETHUSDT", "side": "BUY", "quantity": 5})
    del prices.prices["ETHUSDT"]
    view = paper.view()
    assert view["equity"] is None and view["pnl"] is None and view["missing_prices"] == ["ETHUSDT"]


def test_reset_deletes_everything_and_history_is_capped(book):
    paper, _, tmp = book
    paper.place({"symbol": "ETHUSDT", "side": "BUY", "quantity": 5})
    paper.place({"symbol": "ETHUSDT", "side": "BUY", "type": "LIMIT", "quantity": 1, "price": 5})
    view = paper.reset(50_000)
    assert view["cash"] == 50_000 and view["positions"] == [] and view["orders"] == [] and view["trade_count"] == 0
    with pytest.raises(PaperError, match="between"):
        paper.reset(5)
    again = CryptoPaper(tmp / "p.json", Prices(BTCUSDT=1.0), start_worker=False)
    assert again.view()["start"] == 50_000
    for _ in range(MAX_TRADES + 5):
        again._fill("BTCUSDT", "BUY", 1.0, 1.0, "market")
    assert again.view()["trade_count"] == MAX_TRADES


def test_tickers_use_binance_then_coinbase():
    calls = []

    def http(url, params):
        calls.append(url)
        if "binance" in url:
            return 451, {}
        return 200, {"open": "100", "last": "110"}

    feed = CryptoFeed(http, now=lambda: 5000)
    out = tickers(feed, ["BTCUSDT", "BNBUSDT"])
    assert out["BTCUSDT"] == {"price": 110.0, "change_pct": pytest.approx(10.0), "source": "Coinbase"}
    assert "Coinbase does not list BNB" in out["BNBUSDT"]["error"]

    def good(url, params):
        return 200, [{"symbol": "BTCUSDT", "lastPrice": "50", "openPrice": "40"}]

    assert tickers(CryptoFeed(good), ["BTCUSDT"])["BTCUSDT"]["change_pct"] == pytest.approx(25.0)


def test_watchlist_and_drawings_are_validated(tmp_path):
    store = ChartStore(tmp_path / "c.json")
    assert [i["symbol"] for i in store.watchlist()] == ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
    items = store.set_watchlist([{"venue": "india", "symbol": "reliance"}, {"venue": "india", "symbol": "RELIANCE"}])
    assert items == [{"venue": "india", "symbol": "RELIANCE"}]
    with pytest.raises(ValueError, match="venue"):
        store.set_watchlist([{"venue": "forex", "symbol": "EURUSD"}])
    fib = {"kind": "fib", "points": [{"time": 1, "price": 10}, {"time": 2, "price": 20}]}
    assert store.set_drawings("crypto", "BTCUSDT", [fib, {"kind": "hline", "points": [{"time": 1, "price": 5}]}])
    assert len(store.drawings("crypto", "btcusdt")) == 2
    with pytest.raises(ValueError, match="2 point"):
        store.set_drawings("crypto", "BTCUSDT", [{"kind": "trend", "points": [{"time": 1, "price": 1}]}])
    with pytest.raises(ValueError, match="above zero"):
        store.set_drawings("crypto", "BTCUSDT", [{"kind": "hline", "points": [{"time": 1, "price": -1}]}])
    store.set_drawings("crypto", "BTCUSDT", [])
    assert store.drawings("crypto", "BTCUSDT") == []


DAILY = """Date,Open,High,Low,Close,Volume
20-11-2026,100,110,95,105,1000
21-11-2026,105,112,101,111,1200
22-11-2026,111,0,101,111,1200
23-11-2026,111,115,108,114,900
24-11-2026,114,118,110,116,800
25-11-2026,116,119,112,113,700
26-11-2026,113,117,111,115,600
27-11-2026,115,121,114,120,650
28-11-2026,120,122,118,119,500
29-11-2026,119,125,118,124,400
30-11-2026,124,126,121,122,450
"""


def test_csv_parse_reads_day_first_dates_and_drops_bad_rows():
    frame, report = parse(DAILY)
    assert report["rows"] == 10 and report["dropped"] == 1 and report["dropped_lines"] == [4]
    assert report["base_seconds"] == 86400 and report["dates"] == "day first"
    assert str(frame.index[0].date()) == "2026-11-20"
    with pytest.raises(ValueError, match="needs columns Close"):
        parse("Date,Open,High,Low\n2026-01-01,1,2,1\n")
    with pytest.raises(ValueError, match="First bad lines: 2, 3"):
        parse("Date,Open,High,Low,Close\n2026-01-01,0,1,1,1\n2026-01-02,1,0.5,1,1\n2026-01-03,1,2,1,1\n")
    _, intraday = parse("Datetime,Open,High,Low,Close\n2026-11-20 09:15,1,2,1,1.5\n2026-11-20 09:20,1.5,2,1,1.2\n"
                        "2026-11-20 09:25,1.2,2,1,1.1\n")
    assert intraday["base_seconds"] == 300 and intraday["dates"] == "year first (ISO)"


def test_imports_build_weeks_and_refuse_smaller_candles(tmp_path):
    imports = Imports(tmp_path)
    meta = imports.save("reliance", DAILY)
    assert meta["symbol"] == "RELIANCE" and meta["candle"] == "1 day"
    out = imports.candles("RELIANCE", "1w")
    assert out["source"] == "Imported file" and out["live"]["kind"] == "none" and "Not live" in out["notes"][0]
    assert [c["open"] for c in out["candles"]] == [100, 111, 124]      # weeks from Mon 16, 23 and 30 Nov
    with pytest.raises(MarketDataError, match="cannot show 1 hour"):
        imports.candles("RELIANCE", "1h")
    with pytest.raises(ValueError, match="letters and digits"):
        imports.save("../etc", DAILY)
    imports.delete("RELIANCE")
    with pytest.raises(MarketDataError, match="No imported file"):
        imports.candles("RELIANCE", "1d")


# -- through the API ----------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": {**FAKE, "env": {"FAKE_KITE_ANCHOR": "2026-11-16"}}}},
                     "enforce_trade_window": False})

    def no_yahoo(*args, **kwargs):
        raise AssertionError("the Charts tab must never ask Yahoo")

    app = create_app(settings=settings, token="t", fetch=no_yahoo)
    app.state.derivatives._now = lambda: datetime(2026, 11, 16, 10, 0, tzinfo=market.IST)
    app.state.cpaper.prices = Prices(BTCUSDT=100.0)
    app.state.cpaper._start_worker = False

    def http(url, params):
        if "ticker/24hr" in url:
            return 200, [{"symbol": "BTCUSDT", "lastPrice": "100", "openPrice": "95"}]
        return 451, {}

    app.state.crypto = CryptoFeed(http)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


def test_api_paper_watchlist_drawings_and_imports(client):
    r = client.post("/api/cpaper/order", json={"symbol": "BTCUSDT", "side": "BUY", "type": "MARKET", "quantity": 1})
    assert r.status_code == 200 and r.json()["book"]["positions"][0]["quantity"] == 1
    bad = client.post("/api/cpaper/order", json={"symbol": "BTCUSDT", "side": "SELL", "quantity": 5})
    assert bad.status_code == 400 and "does not short" in bad.json()["error"]
    assert client.post("/api/cpaper/reset", json={"balance": 2000}).json()["cash"] == 2000

    client.put("/api/md/watchlist", json={"items": [{"venue": "crypto", "symbol": "BTCUSDT"},
                                                    {"venue": "india", "symbol": "RELIANCE"},
                                                    {"venue": "imported", "symbol": "TCS"}]})
    prices = client.get("/api/md/prices").json()["prices"]
    assert prices["crypto:BTCUSDT"]["price"] == 100.0
    assert "Log in to Kite" in prices["india:RELIANCE"]["error"]
    assert "No imported file" in prices["imported:TCS"]["error"]

    assert client.post("/api/md/imports", json={"symbol": "TCS", "text": DAILY}).json()["rows"] == 10
    assert client.get("/api/md/catalog").json()["imports"][0]["symbol"] == "TCS"
    out = client.get("/api/md/candles?venue=imported&symbol=TCS&tf=1d").json()
    assert out["source"] == "Imported file" and len(out["candles"]) == 10
    assert client.get("/api/md/prices").json()["prices"]["imported:TCS"]["price"] == 122.0
    assert client.post("/api/md/imports", json={"symbol": "X", "text": "nope"}).status_code == 400

    client.post("/api/kite/connect")
    client.post("/api/kite/login")
    assert client.get("/api/md/prices").json()["prices"]["india:RELIANCE"]["price"] > 0

    body = {"venue": "crypto", "symbol": "BTCUSDT", "items": [{"kind": "hline", "points": [{"time": 5, "price": 99}]}]}
    assert len(client.put("/api/md/drawings", json=body).json()["items"]) == 1
    assert client.get("/api/md/drawings?venue=crypto&symbol=BTCUSDT").json()["items"][0]["kind"] == "hline"
    assert client.delete("/api/md/imports/TCS").json()["items"] == []
