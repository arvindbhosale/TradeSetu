"""Trend rules, index board, fundamentals, stock details, optimiser, tax export, options."""

import copy
import math
import sys
from datetime import datetime

import numpy as np
import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import market, options as opt, taxreport as tax, trend
from desk.backtest import BacktestParams, run_strategy
from desk.broker import Desk, PaperBook
from desk.fundamentals import Fundamentals
from desk.indices import CATALOG, Indices, parse_constituents
from desk.kite import KiteMCP
from desk.optimiser import grid, optimise
from desk.pnl import Ledger
from desk.sectors import Sectors
from desk.settings import Settings
from desk.strategies import STRATEGIES

pytestmark = pytest.mark.unit
FAKE = {"command": sys.executable, "args": ["-m", "desk.dev.fake_kite_mcp"]}


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path / "desk"))
    saved, holidays = copy.deepcopy(config_module._config), set(market.HOLIDAYS)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved
    market.set_holidays(holidays)


class Clock:
    def __init__(self, text):
        self.set(text)

    def set(self, text):
        self.now = datetime.strptime(text, "%Y-%m-%d %H:%M").replace(tzinfo=market.IST)

    def __call__(self):
        return self.now


def line(values, end="2026-11-13"):
    index = pd.bdate_range(end=end, periods=len(values))
    close = pd.Series(values, index=index, dtype=float)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": 1e5})


# -- trend rule --------------------------------------------------------------------

def test_every_trend_method_calls_up_down_and_sideways_as_described():
    rising, falling = line(np.linspace(100, 160, 120)), line(np.linspace(160, 100, 120))
    flat = line([100.0 + (i % 2) * 0.1 for i in range(120)])
    for method in ("ma", "price_ma", "return"):
        rule = trend.validate({"method": method})
        assert trend.classify(rising, rule) == "Uptrend", method
        assert trend.classify(falling, rule) == "Downtrend", method
        assert trend.classify(flat, rule) == "Sideways", method
    st = trend.validate({"method": "supertrend"})
    assert trend.classify(rising, st) == "Uptrend" and trend.classify(falling, st) == "Downtrend"
    short = line([100.0] * 10)
    assert trend.classify(short, trend.validate({"method": "ma"})) == "Not enough data"
    # The band and threshold are the user's: a 2% rise is an uptrend at 1% and not at 5%.
    two_pct = line([100.0] * 40 + [102.0] * 20)
    assert trend.classify(two_pct, trend.validate({"method": "return", "lookback": 20, "threshold_pct": 1})) == "Uptrend"
    assert trend.classify(two_pct, trend.validate({"method": "return", "lookback": 20, "threshold_pct": 5})) == "Sideways"
    for bad in ({"method": "astrology"}, {"fast": 60, "slow": 50}, {"band_pct": -1}, {"lookback": 0}):
        with pytest.raises(ValueError):
            trend.validate(bad)
    assert "50-day" in trend.describe(trend.validate({})) and "Supertrend" in trend.describe(st)


def test_the_trend_rule_is_a_setting_the_boards_use(tmp_path):
    settings = Settings(tmp_path / "s.json")
    board = Sectors(path=tmp_path / "sectors.json", fetch=lambda s, interval="1d": line(np.linspace(100, 130, 320)),
                    quotes=lambda symbols: {}, settings=settings)
    made = board.save_sector("Test", "TCS INFY")
    assert made["trend"] == "Uptrend"
    settings.update({"trend_rule": {"method": "return", "lookback": 5, "threshold_pct": 50}})
    assert board.detail("test")["trend"] == "Sideways"                     # same data, the user's rule
    assert Settings(tmp_path / "s.json").get("trend_rule")["method"] == "return"


# -- sector candles ------------------------------------------------------------------

def test_a_sector_index_has_real_candles_from_its_stocks(tmp_path):
    def fetch(symbol, interval="1d"):
        n = 300
        index = pd.bdate_range(end="2026-11-13", periods=n)
        close = pd.Series(100.0, index=index)
        frame = pd.DataFrame({"Open": close * 0.99, "High": close * 1.03, "Low": close * 0.98, "Close": close, "Volume": 1.0})
        if symbol == "INFY.NS":
            frame[["Open", "High", "Low", "Close"]] *= 2            # a dearer stock: returns matter, not price
        return frame

    board = Sectors(path=tmp_path / "s.json", fetch=fetch, quotes=lambda symbols: {})
    made = board.save_sector("Pair", "TCS INFY")
    last = made["candles"][-1]
    # Each day opens 1% below the previous close, reaches 3% above and 2% below it, and closes flat.
    assert last["close"] == pytest.approx(100.0) and last["open"] == pytest.approx(99.0)
    assert last["high"] == pytest.approx(103.0) and last["low"] == pytest.approx(98.0)
    assert len(made["candles"]) == 251 and made["candles"][0]["close"] == 100.0
    assert made["points"][-1]["value"] == last["close"]


def test_live_quotes_form_todays_candle(tmp_path):
    clock = Clock("2026-11-16 11:00")
    quotes = {"q": {}}
    board = Sectors(path=tmp_path / "s.json", fetch=lambda s, interval="1d": line([100.0] * 300), quotes=lambda symbols: quotes["q"], now=clock)
    board.save_sector("One", "TCS")
    quotes["q"] = {"TCS.NS": {"open": 101.0, "high": 106.0, "low": 99.0, "last": 105.0}}
    board.refresh_live()
    today = board.detail("one")["candles"][-1]
    assert today["time"] == "2026-11-16" and today["open"] == pytest.approx(101.0) and today["high"] == pytest.approx(106.0)
    assert today["close"] == pytest.approx(105.0) and board.detail("one")["change_1d"] == pytest.approx(5.0, abs=0.01)


# -- index board -----------------------------------------------------------------------

NIFTY50_CSV = "Company Name,Industry,Symbol,Series,ISIN Code\nTata Consultancy Services Ltd.,Information Technology,TCS,EQ,INE467B01029\nInfosys Ltd.,Information Technology,INFY,EQ,INE009A01021\n"


def test_constituent_lists_parse_and_the_catalogue_is_sound():
    rows = parse_constituents("﻿" + NIFTY50_CSV + "Bad,,A B,EQ,x\n")
    assert rows == [{"symbol": "TCS.NS", "name": "Tata Consultancy Services Ltd.", "industry": "Information Technology"},
                    {"symbol": "INFY.NS", "name": "Infosys Ltd.", "industry": "Information Technology"}]
    assert len(CATALOG) >= 50 and len({c[0] for c in CATALOG}) == len(CATALOG)
    assert all(c[3].startswith("ind_") and c[4] for c in CATALOG if c[2] != "BSE")
    bse = [c for c in CATALOG if c[2] == "BSE"]
    assert {c[0] for c in bse} == {"bsesensex", "bsesensex50", "bsebankex"}
    assert all(c[3] == "bse" and all(n.startswith("BSE:") for n in c[4]) for c in bse)


def test_bse_rows_from_kite_become_board_entries():
    from desk.indices import bse_entry, kite_key

    assert kite_key("NIFTY 50") == "NSE:NIFTY 50" and kite_key("BSE:SENSEX") == "BSE:SENSEX"
    assert bse_entry({"exchange": "BSE", "segment": "INDICES", "tradingsymbol": "BSE IT", "name": "BSE IT"}) == (
        "bse_bseit", "BSE IT", "BSE", "bse", ["BSE:BSE IT"], None)
    assert bse_entry({"exchange": "BSE", "segment": "INDICES", "tradingsymbol": "AUTO", "name": "AUTO"})[1] == "BSE AUTO"
    assert bse_entry({"exchange": "BSE", "segment": "BSE", "tradingsymbol": "RELIANCE"}) is None       # a stock
    assert bse_entry({"exchange": "NSE", "segment": "INDICES", "tradingsymbol": "NIFTY 50"}) is None   # an NSE index
    assert bse_entry({"exchange": "BSE", "segment": "INDICES", "tradingsymbol": "BAD;NAME"}) is None


def make_indices(tmp_path, kite=None, caps=None):
    fetched = []

    def download(url):
        fetched.append(url)
        if url.endswith("ind_nifty50list.csv"):
            return NIFTY50_CSV
        raise OSError("not in this test")

    def fetch(symbol, interval="1d"):
        return line(np.linspace(100, 120, 300)) if symbol in ("^NSEI", "TCS.NS", "INFY.NS") else None

    sectors = Sectors(path=tmp_path / "s.json", fetch=fetch, quotes=lambda symbols: {
        s: {"last": 110.0, "prev_close": 100.0, "open": 101.0, "high": 111.0, "low": 99.0} for s in symbols})
    funds = Fundamentals(tmp_path / "f.json", fetch=lambda symbol: {"marketCap": (caps or {}).get(symbol)} if caps else {})
    board = Indices(sectors, funds, kite, path=tmp_path / "i.json", download=download, fetch=fetch,
                    get_json=lambda url, params: (_ for _ in ()).throw(OSError("not in this test")))
    return board, funds, fetched


def test_index_cards_details_and_weights(tmp_path):
    board, funds, fetched = make_indices(tmp_path, caps={"TCS.NS": 3e12, "INFY.NS": 1e12})
    board.refresh_lists()
    assert any(u.endswith("ind_nifty50list.csv") for u in fetched) and len(board.constituents("nifty50")) == 2
    board.refresh_levels()
    cards = {c["id"]: c for c in board.cards(trend.DEFAULT)["indices"]}
    assert cards["nifty50"]["source"] == "Yahoo" and cards["nifty50"]["trend"] == "Uptrend" and len(cards["nifty50"]["candles"]) == 45
    assert cards["niftyit"]["trend"] == "Loading"                         # nothing serves it in this test
    detail = board.detail("nifty50", trend.DEFAULT)
    assert detail["weights"].startswith("equal")                         # caps not loaded yet
    funds.load("TCS.NS"), funds.load("INFY.NS")
    detail = board.detail("nifty50", trend.DEFAULT)
    shares = {r["symbol"]: r["weight_pct"] for r in detail["stocks"]}
    assert detail["weights"].startswith("market cap") and shares == {"TCS.NS": 75.0, "INFY.NS": 25.0}
    tcs = next(r for r in detail["stocks"] if r["symbol"] == "TCS.NS")
    board.sectors._quotes = lambda symbols: {s: {"last": 121.0} for s in symbols}        # a quote with no previous close
    board.sectors.ensure_history()
    plain = next(r for r in board.detail("nifty50", trend.DEFAULT)["stocks"] if r["symbol"] == "TCS.NS")
    assert plain["change_1d"] == pytest.approx((121.0 / 120.0 - 1) * 100, abs=0.01)     # from the daily candles
    assert tcs["change_1d"] == 10.0 and tcs["contribution_1d"] == 7.5 and tcs["industry"] == "Information Technology"
    assert board.industry_of()["INFY.NS"] == "Information Technology"
    with pytest.raises(ValueError, match="no such index"):
        board.detail("nifty9000", trend.DEFAULT)
    # A failed download keeps yesterday's list.
    board2, _, _ = make_indices(tmp_path)
    board2._download = lambda url: (_ for _ in ()).throw(OSError("down"))
    board2.refresh_lists(force=True)
    assert len(board2.constituents("nifty50")) == 2


def test_an_index_kite_does_not_name_is_built_from_its_stocks(tmp_path):
    board, _, _ = make_indices(tmp_path)
    board._state["lists"]["niftyit"] = parse_constituents(NIFTY50_CSV)
    assert board.level("niftyit") == (None, "")                          # no stock prices yet
    board.sectors.extra = ["TCS.NS", "INFY.NS"]
    board.sectors.ensure_history()
    frame, source = board.level("niftyit")
    assert source.startswith("built from its stocks") and len(frame) > 200


def test_index_levels_come_from_kite_once_signed_in(tmp_path):
    kite = KiteMCP()
    kite.connect(FAKE)
    try:
        kite.login()
        board, _, _ = make_indices(tmp_path, kite=kite)
        names = board.resolve_kite_names()
        assert names["nifty50"] == "NSE:NIFTY 50" and names["niftybank"] == "NSE:NIFTY BANK" and "niftyit" in names
        board._state["kite_names"] = {}
        board._level_source.clear()
        board.kick()
        for _ in range(100):
            if "Kite" in board._level_source.values():
                break
            import time
            time.sleep(0.05)
        card = next(c for c in board.cards(trend.DEFAULT)["indices"] if c["id"] == "nifty50")
        assert card["source"] == "Kite" and card["value"] == pytest.approx(24500.0, rel=0.01)
        # Every BSE index Kite lists joins the board under BSE; the catalogue's Sensex, Sensex 50, Bankex keep their names.
        names = board.resolve_kite_names()
        assert names["bsesensex"] == "BSE:SENSEX" and names["bsesensex50"] == "BSE:SENSEX50" and names["bsebankex"] == "BSE:BANKEX"
        bse = [e for e in board.entries() if e[2] == "BSE"]
        assert [e[0] for e in bse] == ["bsesensex", "bsesensex50", "bsebankex", "bse_bse500"]     # no duplicate, no stock
        assert names["bse_bse500"] == "BSE:BSE500" and board.entry("bse_bse500")[1] == "BSE 500"
        board.refresh_levels(force=True)
        cards = {c["id"]: c for c in board.cards(trend.DEFAULT)["indices"]}
        assert cards["bse_bse500"]["source"] == "Kite" and cards["bse_bse500"]["value"] == pytest.approx(36000.0, rel=0.01)
        assert cards["bsesensex"]["has_list"] is True and cards["nifty50"]["has_list"] is True
        detail = board.detail("bse_bse500", trend.DEFAULT)
        assert detail["stocks"] == [] and detail["kite_name"] == "BSE:BSE500"            # BSE's lists not read here
        # Kept on disk, and an empty answer (instrument list still loading) does not wipe it.
        again, _, _ = make_indices(tmp_path, kite=kite)
        assert again.entry("bse_bse500") is not None
        again.kite.call = lambda *a, **k: []
        again.discover_bse(force=True)
        assert again.entry("bse_bse500") is not None
    finally:
        kite.disconnect()


def test_fundamentals_are_cached_and_scaled(tmp_path):
    calls = []
    funds = Fundamentals(tmp_path / "f.json", fetch=lambda s: calls.append(s) or {
        "marketCap": 1.5e13, "trailingPE": 25.123, "returnOnEquity": 0.456, "debtToEquity": 12.0, "sector": "Technology", "dividendYield": 1.9})
    values = funds.load("TCS")
    assert values == {"market_cap": 1.5e13, "pe": 25.12, "roe": 45.6, "debt_to_equity": 0.12, "dividend_yield": 1.9, "sector": "Technology"}
    funds.load("TCS.NS")
    assert calls == ["TCS.NS"]                                            # cached
    assert Fundamentals(tmp_path / "f.json").get("TCS.NS")["pe"] == 25.12
    empty = Fundamentals(tmp_path / "g.json", fetch=lambda s: (_ for _ in ()).throw(RuntimeError("feed down")))
    assert empty.load("XYZ") == {}


def test_stock_technicals_match_the_indicators():
    from desk.stockinfo import technicals

    rng = np.random.default_rng(1)
    close = 100 * np.exp(np.cumsum(rng.normal(0.001, 0.01, 300)))
    bars = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": 1.0},
                        index=pd.bdate_range(end="2026-11-13", periods=300))
    t = technicals(bars, trend.DEFAULT)
    assert t["sma50"] == pytest.approx(close[-50:].mean(), abs=0.01)
    assert t["high_52w"] == pytest.approx(float(bars["High"].iloc[-250:].max()), abs=0.01)
    assert t["return_1m"] == pytest.approx((close[-1] / close[-22] - 1) * 100, abs=0.01)
    assert 0 <= t["rsi14"] <= 100 and t["supertrend"] in ("up", "down") and t["trend"] in ("Uptrend", "Downtrend", "Sideways")


# -- optimiser ---------------------------------------------------------------------------

def walk(n=900, seed=7, end=None):
    rng = np.random.default_rng(seed)
    close = 1000 * np.exp(np.cumsum(rng.normal(0.0004, 0.014, n)))
    open_ = np.r_[close[0], close[:-1]]
    index = pd.bdate_range(end=end, periods=n) if end else pd.bdate_range("2022-01-03", periods=n)
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.005, "Low": np.minimum(open_, close) * 0.995,
                         "Close": close, "Volume": 1e5}, index=index)


def test_the_grid_covers_the_ranges_and_refuses_too_many():
    sma = STRATEGIES["sma_cross"]
    combos = grid(sma, {"fast": [5, 20, 5], "slow": [30, 60, 10]})
    assert len(combos) == 16 and {"fast": 5, "slow": 30} in combos and {"fast": 20, "slow": 60} in combos
    assert grid(sma, {})[0] == {"fast": 20, "slow": 50}
    with pytest.raises(ValueError, match="more than 400"):
        grid(sma, {"fast": [2, 200, 1], "slow": [5, 400, 1]})
    with pytest.raises(ValueError, match="stay between"):
        grid(sma, {"fast": [1, 10, 1]})
    with pytest.raises(ValueError, match="step must be positive"):
        grid(sma, {"fast": [5, 10, 0]})


def test_walk_forward_tests_each_choice_only_on_data_after_it():
    bars = walk()
    result = optimise(bars, STRATEGIES["sma_cross"], {"fast": [5, 25, 10], "slow": [30, 90, 30]}, BacktestParams(capital=100_000),
                      "sharpe", folds=3, train_pct=70, min_trades=1)
    assert result["combos"] == 9 and len(result["windows"]) == 3
    for window in result["windows"]:
        assert window["train"][1] < window["test"][0]                    # chosen strictly before it is traded
        assert window["params"] in [{"fast": f, "slow": s} for f in (5, 15, 25) for s in (30, 60, 90)]
    wf = result["walk_forward"]
    assert wf["period"][0] == result["windows"][0]["test"][0] and wf["equity"][0][0] >= result["windows"][0]["test"][0]
    # The grid's best is the best of what it tried, on the same objective.
    best = max(result["grid"], key=lambda r: r["score"] if r["score"] is not None else -1e9)
    assert result["grid"][0] == best
    direct = run_strategy(bars, "sma_cross", result["grid"][0]["params"], BacktestParams(capital=100_000))
    assert direct["metrics"]["sharpe"] == pytest.approx(result["grid"][0]["score"], abs=0.01)
    with pytest.raises(ValueError, match="no settings"):
        optimise(bars, STRATEGIES["buy_hold"], {}, BacktestParams())
    with pytest.raises(ValueError, match="too short"):
        optimise(bars.iloc[:200], STRATEGIES["sma_cross"], {}, BacktestParams(), folds=4)


# -- tax and export ---------------------------------------------------------------------------

def test_tax_lots_match_fifo_and_classify_by_holding_and_product(tmp_path):
    ledger = Ledger(tmp_path / "fills.jsonl")
    add = lambda when, side, qty, price, product="CNC", exchange="NSE", symbol="TCS": ledger.add(      # noqa: E731
        "live", "manual", exchange, symbol, side, qty, price, product, charges=0.0, time_ist=f"{when} 10:00:00")
    add("2024-01-10", "BUY", 10, 100.0)
    add("2025-03-01", "BUY", 10, 200.0)
    add("2025-03-20", "SELL", 15, 300.0)            # FY 2024-25: 10 long-term (held > 12 months), 5 short-term
    add("2025-06-02", "BUY", 50, 50.0, "MIS", symbol="INFY")
    add("2025-06-02", "SELL", 50, 52.0, "MIS", symbol="INFY")
    add("2025-07-01", "BUY", 75, 100.0, "NRML", "NFO", "NIFTY25JUL24500CE")
    add("2025-07-03", "SELL", 75, 60.0, "NRML", "NFO", "NIFTY25JUL24500CE")
    lots = tax.tax_lots(ledger.fills(), "live")
    by = {(lot["category"], lot["fy"]): lot for lot in lots}
    assert by[("LTCG", "2024-25")]["quantity"] == 10 and by[("LTCG", "2024-25")]["gain"] == 2000.0
    assert by[("STCG", "2024-25")]["quantity"] == 5 and by[("STCG", "2024-25")]["gain"] == 500.0
    assert by[("Speculative (intraday)", "2025-26")]["gain"] == 100.0
    assert by[("F&O (non-speculative)", "2025-26")]["gain"] == -3000.0
    years = {y["fy"]: y for y in tax.summary(lots)}
    assert years["2024-25"]["stcg_tax_estimate"] == 100.0 and years["2024-25"]["ltcg_taxable"] == 0.0     # under ₹1.25 lakh
    assert years["2025-26"]["fno_turnover"] == 3000.0
    assert tax.financial_year("2025-03-31") == "2024-25" and tax.financial_year("2025-04-01") == "2025-26"
    csv_text = tax.to_csv(lots, tax.LOT_FIELDS)
    assert csv_text.splitlines()[0].startswith("fy,category,symbol") and len(csv_text.splitlines()) == len(lots) + 1


# -- options -----------------------------------------------------------------------------------

def test_black_scholes_parity_and_implied_volatility():
    spot, strike, years, vol = 24500.0, 24600.0, 10 / 365, 0.14
    call, put = opt.bs_price(spot, strike, years, vol, "CE"), opt.bs_price(spot, strike, years, vol, "PE")
    assert call - put == pytest.approx(spot - strike * math.exp(-opt.RATE * years), abs=1e-6)      # put-call parity
    assert opt.implied_vol(call, spot, strike, years, "CE") == pytest.approx(vol, abs=1e-4)
    assert opt.implied_vol(1.0, spot, 24000, years, "CE") is None                                  # below intrinsic
    assert opt.bs_price(spot, 24000, 0, vol, "CE") == 500.0 and opt.bs_price(spot, 24000, 0, vol, "PE") == 0.0
    assert 0.45 < opt.bs_delta(spot, 24500, years, vol, "CE") < 0.6


def test_the_chain_finds_atm_pcr_and_max_pain():
    contracts = [{"exchange": "NFO", "tradingsymbol": f"N{k}{t}", "strike": float(k), "type": t, "expiry": "2026-11-24", "lot_size": 75}
                 for k in range(24000, 25050, 50) for t in ("CE", "PE")]
    moment = datetime(2026, 11, 17, 10, 0, tzinfo=market.IST)
    years = opt.years_to("2026-11-24", moment)
    quotes = {}
    for c in contracts:
        price = round(opt.bs_price(24510, c["strike"], years, 0.13, c["type"]), 2)
        oi = 5000 if (c["type"] == "CE" and c["strike"] == 24700) or (c["type"] == "PE" and c["strike"] == 24300) else 100
        quotes[f"NFO:{c['tradingsymbol']}"] = {"last_price": max(price, 0.05), "oi": oi, "ohlc": {"close": max(price, 0.05)}}
    chain = opt.build_chain(contracts, quotes, 24510, "2026-11-24", moment, width=4)
    assert chain["atm"] == 24500 and len(chain["rows"]) == 9 and next(r for r in chain["rows"] if r["atm"])["strike"] == 24500
    atm = next(r for r in chain["rows"] if r["atm"])
    assert atm["CE"]["iv"] == pytest.approx(13.0, abs=0.3) and atm["CE"]["itm"] and not atm["PE"]["itm"]
    assert chain["pcr"] == pytest.approx(1.0) and 24300 <= chain["max_pain"] <= 24700
    assert chain["straddle"] == pytest.approx(atm["CE"]["ltp"] + atm["PE"]["ltp"], abs=0.01)
    with pytest.raises(ValueError, match="No contracts"):
        opt.build_chain(contracts, quotes, 24510, "2026-12-29", moment)


def test_next_expiry_respects_the_weekday_and_minimum_days():
    stamp = pd.Timestamp("2026-11-16 10:00")                      # a Monday
    assert opt.next_expiry(stamp, 1, 0) == pd.Timestamp("2026-11-17 15:30")
    assert opt.next_expiry(stamp, 1, 2) == pd.Timestamp("2026-11-24 15:30")
    assert opt.next_expiry(stamp, 3, 0) == pd.Timestamp("2026-11-19 15:30")


def test_a_simulated_options_backtest_trades_settles_and_charges():
    bars = walk(400, seed=3)
    vix = pd.Series(14.0, index=bars.index)
    positions = STRATEGIES["supertrend"].positions(bars)
    result = opt.run_options("NIFTY", bars * 24.5, positions, vix, capital=300_000, lots=1, lot_size=75, side="both",
                             stop_pct=40, target_pct=80, min_days=2)
    trades = result["trades"]
    assert result["simulated"] and len(trades) >= 5 and result["skipped_entries"] == 0
    assert {t["contract"].split()[-1] for t in trades} == {"CE", "PE"}
    assert {t["exit"] for t in trades} <= {"signal", "stoploss", "target", "expiry", "open"}
    for t in trades:
        if t["exit"] == "stoploss":
            assert t["exit_price"] <= t["entry_price"] * 0.6 + 0.5
    closed = sum(t["pnl"] for t in trades if not t["open"])
    assert result["metrics"]["total_charges"] > 20 * len(trades)            # ₹20 brokerage a side and the rest
    assert closed == pytest.approx(sum(t["pnl"] for t in trades if not t["open"]))
    with pytest.raises(ValueError, match="does not look like VIX"):
        opt.run_options("NIFTY", bars, positions, vix * 100, capital=1e5, lots=1, lot_size=75)
    poor = opt.run_options("NIFTY", bars * 24.5, positions, vix, capital=1_000, lots=1, lot_size=75)
    assert poor["trades"] == [] and poor["skipped_entries"] > 0             # said, not hidden


# -- F&O orders -------------------------------------------------------------------------------

CONTRACT = {"exchange": "NFO", "tradingsymbol": "NIFTY26112424500CE", "underlying": "NIFTY", "strike": 24500.0, "type": "CE",
            "expiry": "2026-11-24", "lot_size": 75, "freeze_quantity": 1800}


def fno_desk(tmp_path, clock, price=100.0):
    cfg = Settings(tmp_path / "s.json")
    desk = Desk(cfg, KiteMCP(), PaperBook(1_000_000, tmp_path / "paper.json"), tmp_path / "orders.jsonl",
                price_lookup=lambda s: price, now=clock,
                contract_lookup=lambda exchange, symbol: CONTRACT if symbol == CONTRACT["tradingsymbol"] else None)
    desk.last_price = lambda instrument: (price, "test")
    return desk


def option_order(**changes):
    return {"exchange": "NFO", "tradingsymbol": "NIFTY26112424500CE", "side": "BUY", "quantity": 75, "product": "NRML",
            "order_type": "LIMIT", "price": 100.0, "variety": "regular", **changes}


def test_option_orders_need_whole_lots_limits_and_no_writing(tmp_path):
    clock = Clock("2026-11-17 10:00")
    desk = fno_desk(tmp_path, clock)
    review = desk.preview(option_order(), "paper")
    assert review["ok"], review["errors"]
    assert review["charges"]["brokerage"] == 20.0 and review["charges"]["stt"] == 0
    for change, fragment in (({"quantity": 50}, "whole number of lots"), ({"quantity": 1875}, "at most 1800"),
                             ({"order_type": "MARKET", "price": 0}, "LIMIT orders only"), ({"tradingsymbol": "NIFTY99"}, "not a contract"),
                             ({"side": "SELL"}, "write (short) options"), ({"product": "CNC"}, "product")):
        if "product" in change:
            from desk.broker import OrderRejected
            with pytest.raises(OrderRejected):
                desk.preview(option_order(**change), "paper")
            continue
        errors = desk.preview(option_order(**change), "paper")["errors"]
        assert any(fragment in e for e in errors), (change, errors)
    desk.place(review["token"], review["phrase"])
    assert desk.paper.quantity("NFO:NIFTY26112424500CE") == 75
    sell = desk.preview(option_order(side="SELL", price=100.0), "paper")
    assert sell["ok"] and sell["charges"]["stt"] == pytest.approx(11.25)          # 0.15% of ₹7,500 premium


def test_paper_options_settle_at_intrinsic_value_after_expiry(tmp_path):
    clock = Clock("2026-11-17 10:00")
    desk = fno_desk(tmp_path, clock)
    review = desk.preview(option_order(quantity=150), "paper")
    desk.place(review["token"], review["phrase"])
    assert desk.settle_paper_options(lambda u, e: 24700.0) == []          # not expired yet
    clock.set("2026-11-24 15:00")
    assert desk.settle_paper_options(lambda u, e: 24700.0) == []          # expiry day, before the close
    clock.set("2026-11-24 15:45")
    settled = desk.settle_paper_options(lambda u, e: 24700.0)
    assert settled[0]["fill_price"] == 200.0 and desk.paper.quantity("NFO:NIFTY26112424500CE") == 0
    report = desk.pnl("paper")
    assert report["realized"] == pytest.approx(150 * 100 - review["charges"]["total"] - settled[0]["charges"], abs=1)


# -- through the API ------------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path):
    from fastapi.testclient import TestClient

    from desk.server import create_app

    settings = Settings(tmp_path / "settings.json")
    settings.update({"mcp": {"mcpServers": {"kite": {**FAKE, "env": {"FAKE_KITE_ANCHOR": "2026-11-16"}}}},
                     "enforce_trade_window": False})

    def fetch(symbol, interval="1d"):
        if symbol == "^INDIAVIX":
            return line([14.0] * 900)
        return walk(900, seed=sum(map(ord, symbol)) % 50, end="2026-11-13") * (24.0 if symbol.startswith("^") else 1)

    app = create_app(settings=settings, token="t", fetch=fetch)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as test_client:
        yield test_client
    app.state.kite.disconnect()


def test_api_state_lists_paid_models_and_trend_methods(client):
    state = client.get("/api/state").json()
    assert {"openai", "anthropic", "google"} <= set(state["llm_models"])
    assert any(m["id"] == "claude-opus-5-5" for m in state["llm_models"]["anthropic"]["deep"])
    assert {p["provider"] for p in state["llm_presets"] if p.get("paid")} >= {"openai", "anthropic", "google", "xai", "deepseek"}
    assert set(state["trend_methods"]) == {"ma", "price_ma", "supertrend", "return"}
    changed = client.put("/api/trend-rule", json={"method": "price_ma", "period": 30, "band_pct": 2}).json()
    assert changed["rule"]["method"] == "price_ma" and "30-day" in changed["text"]
    assert client.put("/api/trend-rule", json={"method": "x"}).status_code == 400


def test_api_option_chain_trade_and_positions(client):
    assert "Kite" in client.get("/api/options/chain?underlying=NIFTY").json()["error"]
    client.post("/api/kite/connect")
    client.post("/api/kite/login")
    underlyings = client.get("/api/options/underlyings").json()["underlyings"]
    assert underlyings[0]["name"] == "NIFTY" and underlyings[0]["lot_size"] == 75
    chain = client.get("/api/options/chain?underlying=NIFTY&width=5").json()
    assert chain["spot"] == 24500.0 and chain["atm"] == 24500 and len(chain["rows"]) == 11
    ce = next(r for r in chain["rows"] if r["atm"])["CE"]
    assert ce["ltp"] > 0 and ce["iv"] and ce["lot_size"] == 75
    order = {"exchange": "NFO", "tradingsymbol": ce["tradingsymbol"], "side": "BUY", "quantity": 75, "product": "NRML",
             "order_type": "LIMIT", "price": round(ce["ltp"] * 1.01 / 0.05) * 0.05, "variety": "regular"}
    review = client.post("/api/order/preview", json={"mode": "paper", "order": order}).json()
    assert review["ok"], review["errors"]
    client.post("/api/order/place", json={"token": review["token"], "phrase": review["phrase"]})
    positions = client.get("/api/options/positions?mode=paper").json()["positions"]
    assert positions[0]["quantity"] == 75 and positions[0]["contract"]["strike"] == 24500.0
    chart = client.get(f"/api/chart?instrument={ce['instrument']}&interval=5m").json()
    assert chart["source"] == "Kite" and len(chart["candles"]) > 20
    assert "EXCHANGE:NAME" in client.get("/api/chart?instrument=../etc&interval=5m").json()["error"]
    client.put("/api/settings", json={"live_enabled": True})
    live = client.post("/api/order/preview", json={"mode": "live", "order": order}).json()
    if any("AMO" in e for e in live["errors"]):                           # the test ran after hours
        live = client.post("/api/order/preview", json={"mode": "live", "order": {**order, "variety": "amo"}}).json()
    assert live["ok"], live["errors"]
    client.post("/api/order/place", json={"token": live["token"], "phrase": live["phrase"]})
    assert client.get("/api/options/positions?mode=live").json()["positions"][0]["quantity"] == 75


def test_api_options_backtest_optimiser_and_exports(client):
    import time

    result = client.post("/api/backtest/options", json={"underlying": "NIFTY", "start": "2024-06-01", "end": "2026-11-01",
                                                        "strategy": "supertrend", "side": "calls", "capital": 300000}).json()
    assert result.get("simulated"), result.get("error")
    assert "simulated with Black-Scholes" in result["data_note"] and result["metrics"]["trades"] > 0
    started = client.post("/api/optimise", json={"symbol": "INFY", "start": "2023-01-01", "end": "2026-11-01", "strategy": "sma_cross",
                                                 "ranges": {"fast": [5, 25, 10], "slow": [40, 80, 20]}, "folds": 3}).json()
    job_id = started["job"]["id"]
    for _ in range(300):
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("done", "error"):
            break
        time.sleep(0.1)
    assert job["status"] == "done", job["error"]
    assert job["result"]["combos"] == 9 and len(job["result"]["windows"]) == 3
    assert "more than 400" in client.post("/api/optimise", json={"symbol": "INFY", "start": "2023-01-01", "end": "2026-11-01",
                                                                  "strategy": "sma_cross", "ranges": {"fast": [2, 200, 1], "slow": [5, 400, 1]}}).json()["error"]
    review = client.post("/api/order/preview", json={"mode": "paper", "order": {"exchange": "NSE", "tradingsymbol": "TCS", "side": "BUY",
                                                                                "quantity": 2, "order_type": "MARKET"}}).json()
    client.post("/api/order/place", json={"token": review["token"], "phrase": review["phrase"]})
    review = client.post("/api/order/preview", json={"mode": "paper", "order": {"exchange": "NSE", "tradingsymbol": "TCS", "side": "SELL",
                                                                                "quantity": 2, "order_type": "MARKET"}}).json()
    client.post("/api/order/place", json={"token": review["token"], "phrase": review["phrase"]})
    journal = client.get("/api/export/journal.csv?mode=paper")
    assert journal.headers["content-type"].startswith("text/csv") and "attachment" in journal.headers["content-disposition"]
    assert journal.text.count("\n") == 3 and "TCS" in journal.text
    lots = client.get("/api/export/tax-lots.csv?mode=paper").text.splitlines()
    assert len(lots) == 2 and lots[1].split(",")[1] == "STCG"
    years = client.get("/api/tax?mode=paper").json()["years"]
    assert years[0]["trades"] == 1 and client.get("/api/export/other.csv").status_code == 404


def test_api_indices_and_stock_detail(client):
    board = client.get("/api/indices").json()
    assert len(board["indices"]) == len(CATALOG)
    detail = client.get("/api/indices/nifty50").json()
    assert detail["id"] == "nifty50" and isinstance(detail["stocks"], list)
    stock = client.get("/api/stock?symbol=TCS").json()
    assert stock["symbol"] == "TCS.NS" and stock["technicals"]["trend"] and stock["quote"]["last"]
    assert "not an NSE/BSE stock" in client.get("/api/stock?symbol=NIFTY").json()["error"]


def test_api_fno_algo_runs_a_paper_condor_on_kite_prices(client):
    from datetime import datetime

    from desk import market as market_module

    app = client.app
    moment = datetime(2026, 11, 17, 10, 0, tzinfo=market_module.IST)
    app.state.desk._now = app.state.fno_algos._now = lambda: moment
    body = {"name": "API condor", "kind": "structure", "structure": "iron_condor", "underlying": "NIFTY",
            "expiry_kind": "weekly", "schedule": "intraday", "entry_time": "09:30", "exit_time": "15:10", "k": 2, "w": 2,
            "lots": 1, "mode": "paper", "max_loss": 5000}
    assert "Log in to Kite" in client.post("/api/fno-algos/preview", json=body).json()["error"]
    client.post("/api/kite/connect")
    client.post("/api/kite/login")
    client.post("/api/fno/refresh")
    view = client.post("/api/fno-algos/preview", json=body).json()
    assert [leg["side"] for leg in view["legs"]] == ["BUY", "BUY", "SELL", "SELL"], view
    assert view["margin"] > 0 and view["expiry_day_margin"]
    created = client.post("/api/fno-algos", json=body).json()
    assert client.post(f"/api/fno-algos/{created['id']}/start", json={}).json()["enabled"]
    run = client.post(f"/api/fno-algos/{created['id']}/run").json()
    assert run["algo"]["book"]["phase"] == "open", run["algo"]["log"]
    listed = client.get("/api/fno-algos").json()["algos"][0]
    assert listed["book"]["pnl"] is not None
    assert client.post(f"/api/fno-algos/{created['id']}/stop", json={}).status_code == 400     # legs open: Exit, not Stop
    done = client.post(f"/api/fno-algos/{created['id']}/exit").json()
    assert done["book"] is None and not done["enabled"]
    assert client.get("/api/options/positions?mode=paper").json()["positions"] == []
    live = client.post("/api/fno-algos", json={**body, "name": "Live straddle", "structure": "short_straddle", "mode": "live"}).json()
    phrase = client.get(f"/api/fno-algos/{live['id']}/phrase").json()["phrase"]
    assert phrase == "ARM LIVE FNO LIVE STRADDLE"
    assert "switched off" in client.post(f"/api/fno-algos/{live['id']}/start", json={"phrase": phrase}).json()["error"]


def test_api_market_tools(client):
    pulse = client.get("/api/tools/pulse").json()
    assert {"score", "zone", "parts", "history", "sectors", "stocks"} <= set(pulse) and "error" not in pulse
    deals = client.get("/api/tools/deals?days=5").json()
    assert deals["deals"] == [] and deals["spikes"] == [] and deals["threshold"] == 3.0
    assert client.get("/api/tools/drift?min_move=4").json()["rows"] == []
    assert "rows" in client.get("/api/tools/rotation?period=weekly").json()
    radar = client.get("/api/tools/radar").json()
    assert isinstance(radar["rows"], list) and "universe" in radar
