"""Market Pulse, Whale Deals, Results Drift, Sector Compass, Stock Radar and BSE index lists."""

import numpy as np
import pandas as pd
import pytest

from desk import bselists, markettools as mt, trend


def frame(values, end="2026-11-13", volume=1e5):
    index = pd.bdate_range(end=end, periods=len(values))
    close = pd.Series(values, index=index, dtype=float)
    vol = volume if isinstance(volume, pd.Series) else pd.Series(volume, index=index, dtype=float)
    return pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close, "Volume": vol.values},
                        index=index)


# -- BSE index lists ---------------------------------------------------------------------------

MOVERS = [{"scripname": "RELIANCE", "LONG_NAME": "Reliance Industries Ltd", "index_code": "|S&P BSE SENSEX|,|S&P BSE 500|,|S&P BSE Oil & Gas|"},
          {"scripname": "CREDITACC", "LONG_NAME": "CreditAccess Grameen Ltd",
           "index_code": "|BSE 500|,|BSE SmallCap|,|BSE 250 SmallCap Index|"},
          {"scripname": "BAD NAME", "index_code": "|BSE 500|"}]


def test_bse_index_names_share_one_key():
    assert bselists.key("S&P BSE 500") == bselists.key("BSE 500") == bselists.key("BSE500") == "500"
    assert bselists.key("S&P BSE SmallCap") == bselists.key("SMLCAP") == "SMALLCAP"
    assert bselists.key("S&P BSE SENSEX") == bselists.key("SENSEX") == "SENSEX"
    assert bselists.key("S&P BSE SENSEX 50") == bselists.key("SENSEX50") == "SENSEX50"
    assert bselists.key("BSE 250 SmallCap Index") == "250SMALLCAP"


def test_bse_lists_from_market_movers_are_kept_ten_days():
    calls = []

    def get_json(url, params):
        calls.append(params["GLtype"])
        return {"Table": MOVERS[:1] if params["GLtype"] == "gainer" else MOVERS[1:]}

    fresh = bselists.fetch(get_json)
    assert calls == ["gainer", "loser"]
    assert fresh["500"]["stocks"] == {"RELIANCE.NS": "Reliance Industries Ltd", "CREDITACC.NS": "CreditAccess Grameen Ltd"}
    assert list(fresh["SENSEX"]["stocks"]) == ["RELIANCE.NS"] and "OILGAS" in fresh
    day = 86400
    stored = bselists.merge({}, fresh, now=0)
    later = bselists.merge(stored, {"500": {"name": "BSE 500", "stocks": {"RELIANCE.NS": "Reliance"}}}, now=5 * day)
    assert set(later["500"]["stocks"]) == {"RELIANCE.NS", "CREDITACC.NS"}        # flat today, still a member
    gone = bselists.merge(later, {}, now=11 * day)
    assert set(gone["500"]["stocks"]) == {"RELIANCE.NS"} and "SMALLCAP" not in gone


def test_bse_indices_get_their_stocks(tmp_path):
    from desk.fundamentals import Fundamentals
    from desk.indices import Indices
    from desk.sectors import Sectors

    sectors = Sectors(path=tmp_path / "s.json", fetch=lambda s, interval="1d": None, quotes=lambda s: {})
    board = Indices(sectors, Fundamentals(tmp_path / "f.json"), path=tmp_path / "i.json",
                    download=lambda url: (_ for _ in ()).throw(OSError("offline")),
                    get_json=lambda url, params: {"Table": MOVERS if params["GLtype"] == "gainer" else []})
    board.refresh_lists(force=True)
    assert [r["symbol"] for r in board.constituents("bsesensex")] == ["RELIANCE.NS"]
    ids = {e[0]: e for e in board.entries()}
    assert "bsel_500" in ids and ids["bsel_500"][1] == "BSE 500" and ids["bsel_500"][2] == "BSE"
    assert {r["symbol"] for r in board.constituents("bsel_500")} == {"RELIANCE.NS", "CREDITACC.NS"}
    assert board.detail("bsel_smallcap", trend.DEFAULT)["stocks"][0]["symbol"] == "CREDITACC.NS"
    # A Kite-named BSE index takes the same list: BSE:BSE500 is "500" too, so no duplicate card.
    board._state["bse"] = [["bse_bse500", "BSE 500", "BSE", "bse", ["BSE:BSE500"], None]]
    assert "bsel_500" not in {e[0] for e in board.entries()} and len(board.constituents("bse_bse500")) == 2
    # BSE down: the stored lists stay.
    board._get_json = lambda url, params: (_ for _ in ()).throw(OSError("down"))
    board.refresh_bse_lists(force=True)
    assert len(board.constituents("bse_bse500")) == 2


# -- Market Pulse ------------------------------------------------------------------------------

def test_pulse_reads_greed_in_a_rising_market_and_fear_in_a_falling_one():
    up = frame(np.linspace(100, 160, 400))
    down = frame(np.linspace(160, 100, 400))
    vix_low = pd.Series(np.r_[np.full(300, 20.0), np.full(100, 11.0)], index=up.index)
    vix_high = pd.Series(np.r_[np.full(300, 12.0), np.full(100, 28.0)], index=up.index)
    greed = mt.pulse(up, vix_low, {f"S{i}": frame(np.linspace(100, 150 + i, 400)) for i in range(5)})
    fear = mt.pulse(down, vix_high, {f"S{i}": frame(np.linspace(150 + i, 100, 400)) for i in range(5)})
    assert greed["score"] > 70 and greed["zone"] in ("Greed", "Extreme greed")
    assert fear["score"] < 30 and fear["zone"] in ("Fear", "Extreme fear")
    assert {p["key"] for p in greed["parts"]} == {"trend", "momentum", "volatility", "breadth", "highs", "advances"}
    assert len(greed["history"]) == 60 and greed["history"][-1]["score"] == greed["score"]
    assert greed["week_ago"] is not None and greed["month_ago"] is not None
    assert mt.zone(None) == "No reading" and mt.zone(19.9) == "Extreme fear" and mt.zone(100) == "Extreme greed"
    assert mt.pulse(None, None, {})["score"] is None


# -- Whale Deals -------------------------------------------------------------------------------

BULK = ("Date,Symbol,Security Name,Client Name,Buy/Sell,Quantity Traded,Trade Price / Wght. Avg. Price,Remarks\n"
        "13-Nov-2026,ABCLTD,ABC Limited,BIG FUND LP,BUY,\"12,00,000\",250.50,-\n"
        "13-Nov-2026,XYZ,XYZ Ltd,SOME TRADER,SELL,500000,40,-\n"
        "bad,row\n")


def test_deals_parse_merge_and_spikes():
    deals = mt.parse_deals(BULK, "Bulk")
    assert deals[0] == {"date": "2026-11-13", "kind": "Bulk", "symbol": "ABCLTD.NS", "ticker": "ABCLTD", "security": "ABC Limited",
                        "client": "BIG FUND LP", "side": "Buy", "quantity": 1200000, "price": 250.5, "value_cr": 30.06}
    assert deals[1]["side"] == "Sell" and len(deals) == 2
    assert mt.parse_deals("no,useful,header\n1,2,3\n", "Block") == []
    old = [{**deals[1], "date": "2026-09-01"}]
    kept = mt.merge_deals(old, deals + deals, "2026-11-13")
    assert len(kept) == 2 and kept[0]["value_cr"] == 30.06                     # duplicates and month-old deals dropped
    history = pd.Series([1e5] * 25, index=pd.bdate_range(end="2026-11-12", periods=25))
    spikes = mt.volume_spikes({"A.NS": (history, 4e5), "B.NS": (history, 2e5), "C.NS": (None, 9e9)},
                              {"A.NS": frame([100, 103])})
    assert [s["symbol"] for s in spikes] == ["A.NS"] and spikes[0]["times"] == 4.0 and spikes[0]["change_1d"] == 3.0


# -- Results Drift -----------------------------------------------------------------------------

def test_results_drift_finds_a_stock_still_moving_after_strong_results():
    rows = mt.parse_results([{"short_name": "ABCLTD", "meeting_date": "02 Nov 2026", "Long_Name": "ABC LTD."},
                             {"short_name": "XYZ", "meeting_date": "not a date"}])
    assert rows == [{"symbol": "ABCLTD.NS", "date": "2026-11-02", "company": "ABC LTD."}]
    index = pd.bdate_range(end="2026-11-13", periods=60)
    close = pd.Series(100.0, index=index)
    close[index >= pd.Timestamp("2026-11-03")] = 110.0                       # +10% the session after the meeting
    close[index >= pd.Timestamp("2026-11-09")] = 114.0                       # and kept going
    volume = pd.Series(1e5, index=index)
    volume[pd.Timestamp("2026-11-03")] = 5e5
    bars = pd.DataFrame({"Open": close, "High": close * 1.01, "Low": close * 0.99, "Close": close})
    row = mt.drift_row(bars, "2026-11-02", volume)
    assert row["reaction"] == pytest.approx(10.0) and row["since"] == pytest.approx(3.64, abs=0.01)
    assert row["signal"] == "Drifting up" and row["volume_times"] == 5.0 and row["reaction_date"] == "2026-11-03"
    flat = mt.drift_row(pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0}, index=index), "2026-11-02")
    assert flat["signal"] == "No edge"
    assert mt.drift_row(bars, "2027-01-01") is None


# -- Sector Compass ----------------------------------------------------------------------------

def test_rotation_places_a_leader_and_a_laggard():
    index = pd.bdate_range(end="2026-11-13", periods=400)
    bench = pd.Series(np.linspace(100, 120, 400), index=index)
    leader = bench * np.exp(np.linspace(0, 0.4, 400) ** 2)                   # pulling away faster and faster
    laggard = bench * np.exp(-np.linspace(0, 0.4, 400) ** 2)
    rows = {r["name"]: r for r in mt.rotation({"Lead": leader, "Lag": laggard}, bench)}
    assert rows["Lead"]["quadrant"] == "Leading" and rows["Lag"]["quadrant"] == "Lagging"
    assert len(rows["Lead"]["tail"]) == 6 and rows["Lead"]["ahead"] > 0 > rows["Lag"]["ahead"]
    assert mt.rotation({"Short": bench.iloc[-20:]}, bench) == []


def test_rotation_groups_match_the_numbers_shown():
    index = pd.bdate_range(end="2026-11-13", periods=300)
    bench = pd.Series(100.0, index=index)
    up_then_down = pd.Series(np.r_[np.linspace(100, 130, 280), np.linspace(130, 128, 20)], index=index)
    down_then_up = pd.Series(np.r_[np.linspace(100, 80, 280), np.linspace(80, 82, 20)], index=index)
    rows = {r["name"]: r for r in mt.rotation({"Fade": up_then_down, "Turn": down_then_up}, bench)}
    fade, turn = rows["Fade"], rows["Turn"]
    assert fade["quadrant"] == "Weakening" and turn["quadrant"] == "Improving"
    weekly = up_then_down.resample("W-FRI").last()
    assert fade["change_long"] == round((weekly.iloc[-1] / weekly.iloc[-14] - 1) * 100, 2) and fade["bench_long"] == 0
    assert fade["ahead"] == round(fade["change_long"] - fade["bench_long"], 2)
    assert fade["gaining"] == round(fade["change_short"] - fade["bench_short"], 2) < 0


# -- Stock Radar -------------------------------------------------------------------------------

def test_radar_scores_the_stronger_stock_higher():
    nifty = frame(np.linspace(100, 110, 300))
    strong, weak = frame(np.linspace(100, 160, 300)), frame(np.linspace(160, 100, 300))
    history = pd.Series([1e5] * 30, index=pd.bdate_range(end="2026-11-12", periods=30))
    rows = mt.radar({"S.NS": strong, "W.NS": weak}, nifty, {"S.NS": (history, 3e5), "W.NS": (history, 1e5)},
                    lambda s: {"pe": 20.0} if s == "S.NS" else {}, {"S.NS": "Strong Ltd"}, {"S.NS": ["IT"]},
                    trend.DEFAULT, {"S.NS": ["Bulk buy"]})
    assert [r["symbol"] for r in rows] == ["S.NS", "W.NS"] and rows[0]["score"] > rows[1]["score"]
    s = rows[0]
    assert s["above_50"] and s["above_200"] and s["from_high"] == 0.0 and s["rs_3m"] > 0 and s["volume_x"] == 3.0
    assert s["name"] == "Strong Ltd" and s["sector"] == "IT" and s["flags"] == ["Bulk buy"] and s["pe"] == 20.0
    assert mt.finite({"a": float("nan"), "b": [np.float64(1.5)]}) == {"a": None, "b": [1.5]}


# -- the service -------------------------------------------------------------------------------

def test_market_tools_download_once_and_keep_old_data(tmp_path):
    from datetime import datetime

    from desk import market

    gets = []

    def download(url):
        gets.append(url)
        if url.endswith("bulk.csv"):
            return BULK
        raise OSError("block file missing")

    tools = mt.MarketTools(sectors=None, indices=None, fundamentals=None, path=tmp_path / "t.json", download=download,
                           get_json=lambda url, params: [{"short_name": "ABCLTD", "meeting_date": "02 Nov 2026"}],
                           now=lambda: datetime(2026, 11, 13, 18, 0, tzinfo=market.IST))
    tools.tick()
    assert len(tools._state["deals"]) == 2 and "Block" in tools._state["deals_note"]
    assert tools._state["results"][0]["symbol"] == "ABCLTD.NS"
    tools.tick()
    assert len(gets) == 2                                                     # not read again within two hours
    assert tools.result_symbols({"ABCLTD.NS", "OTHER.NS"}) == ["ABCLTD.NS"]
    again = mt.MarketTools(None, None, None, path=tmp_path / "t.json",
                           get_json=lambda url, params: (_ for _ in ()).throw(OSError("BSE down")))
    again.refresh_results(force=True)
    assert again._state["results"][0]["symbol"] == "ABCLTD.NS" and "could not be read" in again._state["results_note"]


# -- Results calendar sources -------------------------------------------------------------------

NSE_EVENTS = [
    {"symbol": "ABCLTD", "company": "ABC  Limited", "purpose": "Financial Results/Dividend",
     "bm_desc": "To consider and approve the financial results for the period ended September 30, 2026", "date": "02-Nov-2026"},
    {"symbol": "XYZ", "company": "XYZ Ltd", "purpose": "Fund Raising", "bm_desc": "To consider raising funds", "date": "03-Nov-2026"},
    {"symbol": "PQR", "company": "PQR Ltd", "purpose": "Other business matters",
     "bm_desc": "Inter alia, to consider Financial Results", "date": "04-Nov-2026"},
    {"symbol": "ABCLTD", "company": "ABC Limited", "purpose": "Financial Results", "bm_desc": "", "date": "02-Nov-2026"},
    {"symbol": "BAD", "purpose": "Financial Results", "date": "not a date"}]


def test_nse_event_calendar_keeps_only_results_meetings():
    rows = mt.parse_nse_events(NSE_EVENTS)
    assert [(r["symbol"], r["date"]) for r in rows] == [("ABCLTD.NS", "2026-11-02"), ("PQR.NS", "2026-11-04")]
    assert rows[0]["company"] == "ABC Limited"
    assert mt.parse_nse_events({"data": NSE_EVENTS[:1]})[0]["symbol"] == "ABCLTD.NS"
    assert mt.parse_nse_events("nonsense") == []


def test_why_gives_a_short_reason():
    import httpx

    request = httpx.Request("GET", "https://api.bseindia.com/x")
    exc = httpx.HTTPStatusError("Client error '403 Forbidden' for url ...", request=request,
                                response=httpx.Response(403, request=request))
    assert mt.why(exc) == "403 Forbidden"
    assert mt.why(RuntimeError("NSE did not answer (www.nseindia.com: Client error '403 Forbidden' for url 'x')")) == "403 Forbidden"
    assert mt.why(OSError("offline")) == "offline"


class _Nse:
    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def json(self, path, params):
        self.calls.append((path, params))
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def test_results_calendar_falls_back_to_nse_when_bse_refuses(tmp_path):
    from datetime import datetime

    from desk import market

    nse = _Nse(NSE_EVENTS)
    tools = mt.MarketTools(None, None, None, path=tmp_path / "t.json", nse=nse,
                           get_json=lambda url, params: (_ for _ in ()).throw(RuntimeError("Client error '403 Forbidden'")),
                           now=lambda: datetime(2026, 11, 13, 18, 0, tzinfo=market.IST))
    tools.refresh_results(force=True)
    assert nse.calls == [("event-calendar", {"index": "equities", "from_date": "14-09-2026", "to_date": "13-11-2026"})]
    assert tools._state["results_source"] == "NSE" and tools._state["results_note"] is None
    assert {r["symbol"] for r in tools._state["results"]} == {"ABCLTD.NS", "PQR.NS"}


def test_results_calendar_explains_a_blocked_server_and_retries_sooner(tmp_path, monkeypatch):
    from datetime import datetime

    from desk import market

    tools = mt.MarketTools(None, None, None, path=tmp_path / "t.json", nse=_Nse(RuntimeError("Client error '403 Forbidden'")),
                           get_json=lambda url, params: (_ for _ in ()).throw(RuntimeError("Client error '403 Forbidden'")),
                           now=lambda: datetime(2026, 11, 13, 18, 0, tzinfo=market.IST))
    clock = [1_000_000.0]
    monkeypatch.setattr(mt.time, "time", lambda: clock[0])
    tools.refresh_results(force=True)
    note = tools._state["results_note"]
    assert "BSE: 403 Forbidden" in note and "NSE: 403 Forbidden" in note and "DESK_EXCHANGE_PROXY" in note
    calls = tools.nse.calls
    clock[0] += 20 * 60
    tools.refresh_results()
    assert len(calls) == 1                                                     # not before 30 minutes
    clock[0] += 11 * 60
    tools.refresh_results()
    assert len(calls) == 2


def test_exchange_proxy_only_when_set(monkeypatch):
    from desk import nse

    monkeypatch.delenv("DESK_EXCHANGE_PROXY", raising=False)
    assert nse.proxy_kwargs() == {}
    monkeypatch.setenv("DESK_EXCHANGE_PROXY", " http://u:p@10.0.0.2:3128 ")
    assert nse.proxy_kwargs() == {"proxy": "http://u:p@10.0.0.2:3128"}
