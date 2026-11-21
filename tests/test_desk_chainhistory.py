"""Option chain history: NSE bhavcopy parsing and import, the live recorder, and real premiums in the backtest.

The bhavcopy samples below are built in the column layouts NSE publishes (old ``fo<DD><MON><YYYY>bhav.csv`` and
UDiFF ``BhavCopy_NSE_FO_..._F_0000.csv``); the prices in them are made up for the tests.
"""

import io
import zipfile
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from desk.chainhistory import Blocked, ChainHistory, Recorder, parse, urls, ymd
from desk.optstrat import run_structure

pytestmark = pytest.mark.unit

OLD_HEADER = "INSTRUMENT,SYMBOL,EXPIRY_DT,STRIKE_PR,OPTION_TYP,OPEN,HIGH,LOW,CLOSE,SETTLE_PR,CONTRACTS,VAL_INLAKH,OPEN_INT,CHG_IN_OI,TIMESTAMP,"
UDIFF_HEADER = ("TradDt,BizDt,Sgmt,Src,FinInstrmTp,FinInstrmId,ISIN,TckrSymb,SctySrs,XpryDt,FininstrmActlXpryDt,StrkPric,OptnTp,"
                "FinInstrmNm,OpnPric,HghPric,LwPric,ClsPric,LastPric,PrvsClsgPric,UndrlygPric,SttlmPric,OpnIntrst,"
                "ChngInOpnIntrst,TtlTradgVol,TtlTrfVal,TtlNbOfTxsExctd,SsnId,NewBrdLotQty,Rmks,Rsvd1,Rsvd2,Rsvd3,Rsvd4")


def zipped(name: str, text: str) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, text)
    return buffer.getvalue()


def old_file() -> str:
    rows = [
        "FUTIDX,NIFTY,28-Sep-2023,0,XX,19409.5,19528.95,19377,19512.1,19512.1,116553,1133742.89,9601900,-487600,01-SEP-2023,",
        "FUTIDX,NIFTY,26-Oct-2023,0,XX,19500,19620,19475,19606.1,19606.1,5595,54690.98,561050,44750,01-SEP-2023,",
        "OPTIDX,NIFTY,07-Sep-2023,19500,CE,120,150,95,140.5,140.5,250000,1,3000000,1000,01-SEP-2023,",
        "OPTIDX,NIFTY,07-Sep-2023,19500,PE,110,130,70,75.25,75.25,240000,1,2800000,1000,01-SEP-2023,",
        "OPTIDX,NIFTY,07-Sep-2023,19550,CE,0,0,0,0,112.4,0,0,5000,0,01-SEP-2023,",          # untraded, open interest: kept
        "OPTIDX,NIFTY,07-Sep-2023,19600,CE,0,0,0,0,90,0,0,0,0,01-SEP-2023,",                # no trade, no OI: dropped
        "OPTIDX,NIFTY,07-Sep-2023,25000,CE,0.05,0.05,0.05,0.05,0.05,10,0,100,0,01-SEP-2023,",  # outside the band
        "OPTIDX,NIFTY,26-Jun-2025,19500,CE,2000,2000,2000,2000,2000,1,0,100,0,01-SEP-2023,",  # far expiry: dropped
        "OPTIDX,BANKNIFTY,07-Sep-2023,44500,PE,300,310,250,260,260,1000,1,5000,0,01-SEP-2023,",
        "OPTSTK,RELIANCE,28-Sep-2023,2500,CE,10,11,9,10,10,100,1,100,0,01-SEP-2023,",
    ]
    return "\n".join([OLD_HEADER, *rows]) + "\n"


def udiff_row(day, kind, sym, expiry, strike, opt, o, h, lo, c, settle, oi, vol):
    return (f"{day},{day},FO,NSE,{kind},1,,{sym},,{expiry},{expiry},{strike},{opt},{sym}X,{o},{h},{lo},{c},{c},{o},,{settle},"
            f"{oi},0,{vol},0,0,F1,65,,,,,")


def test_parses_the_old_bhavcopy():
    out = parse(zipped("fo01SEP2023bhav.csv", old_file()), ["NIFTY", "BANKNIFTY"])
    assert out["day"] == 20230901 and out["format"] == "old"
    keys = {(r[0], r[3], r[4]) for r in out["opt"]}
    assert keys == {("NIFTY", 1950000, "CE"), ("NIFTY", 1950000, "PE"), ("NIFTY", 1955000, "CE"), ("BANKNIFTY", 4450000, "PE")}
    ce = next(r for r in out["opt"] if r[3] == 1950000 and r[4] == "CE")
    assert ce == ("NIFTY", 20230901, 20230907, 1950000, "CE", 12000, 15000, 9500, 14050, 14050, 3000000, 250000)
    assert len(out["fut"]) == 2 and out["fut"][0][6] == 1951210


def test_parses_udiff_and_rejects_other_files():
    text = "\n".join([UDIFF_HEADER,
                      udiff_row("2026-10-29", "IDF", "NIFTY", "2026-11-25", "", "", 23000, 23200, 22900, 23100, 23100, 100, 10),
                      udiff_row("2026-10-29", "IDO", "NIFTY", "2026-11-23", "23800.00", "PE", 1062.95, 1070, 803.9, 808.95,
                                808.95, 335335, 472),
                      udiff_row("2026-10-29", "STO", "IEX", "2026-11-23", "106.00", "CE", 2.5, 4.1, 2.5, 3.9, 3.9, 10, 1)])
    out = parse(zipped("BhavCopy.csv", text), ["NIFTY"])
    assert out["format"] == "udiff" and out["day"] == 20261029
    assert out["opt"] == [("NIFTY", 20261029, 20261123, 2380000, "PE", 106295, 107000, 80390, 80895, 80895, 335335, 472)]
    with pytest.raises(ValueError, match="not an NSE F&O bhavcopy"):
        parse(b"SYMBOL,SERIES,OPEN\nTCS,EQ,1\n", ["NIFTY"])


def test_urls_follow_the_2024_format_change():
    assert urls(date(2023, 7, 7))[0] == ("old", "https://nsearchives.nseindia.com/content/historical/DERIVATIVES/2023/JUL/fo07JUL2023bhav.csv.zip")
    assert urls(date(2024, 7, 8))[0] == ("udiff", "https://nsearchives.nseindia.com/content/fo/BhavCopy_NSE_FO_0_0_0_20240708_F_0000.csv.zip")


class HTTPError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.response = type("R", (), {"status_code": status})()


class FakeNse:
    """``file(url)``: a bhavcopy for the days in ``files``, 404 for the rest, or ``refuse`` for every call."""

    def __init__(self, files, refuse=None):
        self.files, self.refuse, self.calls = files, refuse, []

    def file(self, url):
        self.calls.append(url)
        if self.refuse:
            raise HTTPError(self.refuse)
        for day, raw in self.files.items():
            if day.strftime("%Y%m%d") in url or f"fo{day:%d}{day:%b}{day:%Y}".upper() in url.upper():
                return raw
        raise HTTPError(404)


def day_file(day: date) -> bytes:
    text = old_file().replace("01-SEP-2023", day.strftime("%d-%b-%Y").upper())
    return zipped("x.csv", text)


def test_import_is_resumable_and_tells_holidays_from_refusals(tmp_path):
    days = [date(2023, 8, 28), date(2023, 8, 29), date(2023, 8, 31)]            # 30 Aug is a "holiday" (404)
    nse = FakeNse({d: day_file(d) for d in days})
    store = ChainHistory(tmp_path / "h.db", client=nse, now=lambda: datetime(2023, 9, 20))
    counts = store.import_range(date(2023, 8, 26), date(2023, 8, 31), ["NIFTY"])
    assert counts["ok"] == 3 and counts["none"] == 1 and counts["error"] == 0
    assert store.done_days()[ymd(date(2023, 8, 30))] == "none"
    status = store.status()
    assert status["underlyings"][0]["underlying"] == "NIFTY" and status["underlyings"][0]["days"] == 3
    asked = len(nse.calls)
    again = store.import_range(date(2023, 8, 26), date(2023, 8, 31), ["NIFTY"])
    assert again["todo"] == 0 and len(nse.calls) == asked                        # nothing fetched twice
    # A 404 for a day only just past may mean "not published yet": not stored as a holiday.
    recent = ChainHistory(tmp_path / "r.db", client=FakeNse({}), now=lambda: datetime(2023, 9, 1, 20))
    assert recent.import_range(date(2023, 8, 31), date(2023, 8, 31), ["NIFTY"])["pending"] == 1
    assert recent.done_days() == {}


def test_import_stops_when_nse_refuses(tmp_path):
    store = ChainHistory(tmp_path / "h.db", client=FakeNse({}, refuse=403), now=lambda: datetime(2023, 9, 20))
    with pytest.raises(Blocked, match="DESK_EXCHANGE_PROXY"):
        store.import_range(date(2023, 8, 1), date(2023, 8, 31), ["NIFTY"])
    status = store.status()
    assert status["days"] == {"error": 5} and status["underlyings"] == []         # refusals are never stored as data


# -- real premiums in the backtest ----------------------------------------------------------------------------------

def index_bars(n=40, start="2025-01-01"):
    rng = np.random.default_rng(3)
    close = 24000 * np.exp(np.cumsum(rng.normal(0, 0.004, n)))
    open_ = np.r_[close[0], close[:-1]]
    return pd.DataFrame({"Open": open_, "High": np.maximum(open_, close) * 1.002, "Low": np.minimum(open_, close) * 0.998,
                         "Close": close, "Volume": 0.0}, index=pd.bdate_range(start, periods=n))


def history_with_monday_expiries(path, bars):
    """Every strike 22000-26000 each day, open 200 / high 230 / low 150 / close 180, expiring on Mondays."""
    store = ChainHistory(path)
    for stamp in bars.index:
        d = stamp.date()
        mondays = [d + timedelta(days=(7 - d.weekday()) % 7 + 7 * i) for i in range(3)]
        rows = [("NIFTY", ymd(d), ymd(e), k * 100, t, 20000, 23000, 15000, 18000, 18000, 1000, 500)
                for e in mondays for k in range(22000, 26001, 50) for t in ("CE", "PE")]
        store.store({"day": ymd(d), "opt": rows, "fut": []}, "test")
    return store


def test_positional_backtest_uses_stored_premiums_and_listed_expiries(tmp_path):
    bars = index_bars()
    vix = pd.Series(14.0, index=bars.index)
    store = history_with_monday_expiries(tmp_path / "h.db", bars)
    real = run_structure("NIFTY", bars, vix, structure="short_straddle", mode="positional", lot_size=65, k=0,
                         slippage_pct=1.0, min_days=1, history=store)
    first = real["trades"][0]
    assert first["entry_price"] == pytest.approx(2 * 200 * 0.99)                 # both legs at the stored open, less slippage
    assert pd.Timestamp(first["exit_date"]).weekday() == 0                       # settled on a listed (Monday) expiry
    assert real["pricing"]["model"] == 0 and real["pricing"]["traded"] >= 2 * len(real["trades"])
    assert real["simulated"] is False
    model = run_structure("NIFTY", bars, vix, structure="short_straddle", mode="positional", lot_size=65, k=0, min_days=1)
    assert model["pricing"] is None and model["simulated"] is True
    assert model["trades"][0]["entry_price"] != first["entry_price"]


def test_missing_strikes_fall_back_to_the_model_and_are_counted(tmp_path):
    bars = index_bars()
    store = ChainHistory(tmp_path / "h.db")
    d = bars.index[0].date()
    store.store({"day": ymd(d), "opt": [("NIFTY", ymd(d), ymd(d + timedelta(days=6)), 100, "CE", 1, 1, 1, 1, 1, 1, 1)],
                 "fut": []}, "test")
    out = run_structure("NIFTY", bars, pd.Series(14.0, index=bars.index), structure="short_straddle", mode="positional",
                        lot_size=65, k=0, min_days=1, history=store)
    assert out["pricing"]["model"] > 0 and out["pricing"]["traded"] == 0 and out["simulated"] is True


# -- the live recorder ----------------------------------------------------------------------------------------------

class FakeKite:
    connected = True
    tools = {"get_quotes", "get_ltp"}

    def __init__(self):
        self.calls = 0

    def ltp(self, names):
        return {names[0]: 24010.0}

    def quotes(self, names):
        self.calls += 1
        return {n: {"last_price": 100.0 + i, "oi": 1000, "volume": 50, "ohlc": {"close": 90.0},
                    "depth": {"buy": [{"price": 99.5}], "sell": [{"price": 100.5}]}} for i, n in enumerate(names)}


class FakeDerivatives:
    def contracts(self, underlying):
        out = []
        for expiry in ("2026-11-24", "2026-12-01", "2026-12-08"):
            for k in range(23000, 25001, 50):
                for t in ("CE", "PE"):
                    out.append({"exchange": "NFO", "tradingsymbol": f"NIFTY{expiry}{k}{t}", "expiry": expiry,
                                "strike": float(k), "type": t, "lot_size": 65})
        return out


class FakeSettings(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def test_recorder_saves_the_near_expiries_and_backtests_read_them(tmp_path):
    from desk import market
    clock = {"now": datetime(2026, 11, 23, 10, 0, tzinfo=market.IST)}
    store = ChainHistory(tmp_path / "h.db")
    kite = FakeKite()
    recorder = Recorder(store, kite, FakeDerivatives(), FakeSettings(chain_recorder=["NIFTY"], chain_recorder_minutes=5),
                        now=lambda: clock["now"])
    recorder.tick()
    assert kite.calls == 1 and recorder.last["NIFTY"]["rows"] == 2 * 41 * 2
    recorder.tick()                                                               # not due yet
    assert kite.calls == 1
    clock["now"] += timedelta(minutes=5)
    recorder.tick()
    assert kite.calls == 2
    price = store.snapshot("NIFTY", datetime(2026, 11, 23, 10, 3), date(2026, 11, 24), 24000.0, "CE")
    assert price is not None and price >= 100.0
    assert store.snapshot("NIFTY", datetime(2026, 11, 23, 9, 50), date(2026, 11, 24), 24000.0, "CE") is None
    assert store.snapshot("NIFTY", datetime(2026, 11, 23, 11, 30), date(2026, 11, 24), 24000.0, "CE") is None   # too old
    clock["now"] = datetime(2026, 11, 23, 16, 0, tzinfo=market.IST)              # market closed
    recorder.tick()
    assert kite.calls == 2


def test_api_reports_history_and_the_backtest_note(tmp_path, monkeypatch):
    pytest.importorskip("fastapi")
    pytest.importorskip("mcp")
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings
    from tests.test_desk_optstrat import walk

    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path))

    def fetch(symbol, interval="1d"):
        if symbol == "^INDIAVIX":
            return walk(300, vol=0.0, drift=0.0, base=14.0, start="2025-06-02")
        return walk(300, vol=0.004, drift=0.0, start="2025-06-02")

    app = create_app(settings=Settings(tmp_path / "settings.json"), token="t", fetch=fetch)
    with TestClient(app, headers={"X-Desk-Token": "t"}) as client:
        out = client.get("/api/options/history").json()
        assert out["underlyings"] == [] and out["recorder"]["underlyings"] == ["NIFTY"] and "NIFTY" in out["choices"]
        assert client.post("/api/options/history/import", json={"start": "2024-01-01", "end": "2023-01-01"}).status_code == 400
        changed = client.put("/api/settings", json={"chain_recorder": "nifty, banknifty", "chain_recorder_minutes": 3}).json()
        assert changed["settings"]["chain_recorder"] == ["NIFTY", "BANKNIFTY"]
        assert client.put("/api/settings", json={"chain_recorder": ["XYZ"]}).status_code == 400
        body = {"structure": "short_straddle", "underlying": "NIFTY", "mode": "positional", "start": "2025-06-02",
                "end": "2026-07-01", "k": 0, "min_days": 1}
        r = client.post("/api/backtest/structure", json=body).json()
        assert "no NSE history imported" in r["data_note"], r.get("error")
        bars = walk(300, vol=0.004, drift=0.0, start="2025-06-02")
        history_with_monday_expiries(app.state.chain_history.path, bars)
        r = client.post("/api/backtest/structure", json=body).json()
        assert "from NSE traded prices" in r["data_note"] and r["pricing"]["traded"] > 0, r.get("data_note")
        r = client.post("/api/backtest/structure", json={**body, "premiums": "model"}).json()
        assert "as you chose" in r["data_note"] and r["pricing"] is None
