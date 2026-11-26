"""Live intraday algos load enough Kite candles; when they cannot, the reason is shown, not a bare HTTPError."""

import copy
from datetime import timedelta

import pandas as pd
import pytest

pytest.importorskip("fastapi")
pytest.importorskip("mcp")

import tradingagents.dataflows.config as config_module
from desk import data
from tests.test_desk_positional import SPEC, engine_for
from tests.test_desk_trading import Clock, candles_for

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    yield
    config_module._config = saved


def session_bars(start, end, minutes):
    """The NSE candles of ``minutes`` that open between ``start`` and ``end``, weekdays only."""
    out = []
    for day in pd.bdate_range(start.normalize(), end.normalize()):
        stamps = pd.date_range(day + pd.Timedelta("9h15min"), day + pd.Timedelta("15h29min"), freq=f"{minutes}min")
        out += [t for t in stamps if start <= t <= end]
    return out


@pytest.mark.parametrize("interval", ["1m", "5m", "15m", "30m", "1h"])
@pytest.mark.parametrize("weekday", range(7))
def test_every_day_of_the_week_kite_is_asked_for_enough_candles(interval, weekday):
    # Five fixed days gave 21 to 27 hourly candles, so 1-hour algos dropped Kite for Yahoo except on Fridays.
    now = pd.Timestamp("2026-11-23 10:20") + pd.Timedelta(days=weekday)       # Monday 23 Nov + weekday
    bars = data.live_bars_needed({"fast": 12, "slow": 26})
    days = data.live_kite_days(interval, bars)
    got = session_bars(now - pd.Timedelta(days=days), now, data.INTERVALS[interval]["minutes"])
    assert len(got) >= bars >= 78


def test_bars_needed_follow_the_longest_setting_within_limits():
    assert data.live_bars_needed({}) == data.LIVE_MIN_BARS
    assert data.live_bars_needed({"fast": 50, "slow": 200, "flag": True, "mult": 2.5}) == 600
    assert data.live_bars_needed({"slow": 10_000}) == data.LIVE_MAX_BARS
    assert data.live_kite_days("1h", 10**6) == data.KITE_CHUNK["1h"]
    assert data.live_kite_days("1h", 60, history_days=40) == 40          # weekly/monthly pivots need more


class FakeKite:
    def __init__(self, frame=None, error=None, connected=True):
        self.frame, self.error, self.connected, self.asked = frame, error, connected, []
        self.tools = {"get_historical_data": {}}

    def candles(self, instrument, interval, start, end, **kwargs):
        self.asked.append((instrument, interval, start, end))
        if self.error:
            raise ValueError(self.error)
        return [{"date": t.isoformat(), "open": r.Open, "high": r.High, "low": r.Low, "close": r.Close,
                 "volume": r.Volume} for t, r in zip(self.frame.index, self.frame.itertuples(), strict=True)]


def hourly(days):
    frames = []
    for day in days:
        index = pd.date_range(f"{day} 09:15", periods=7, freq="60min")
        frames.append(pd.DataFrame({"Open": 100.0, "High": 101.0, "Low": 99.0, "Close": 100.0, "Volume": 1000.0},
                                   index=index))
    return pd.concat(frames)


def live_engine(tmp_path, kite):
    feed, clock = {"bars": candles_for("2026-11-25", [100.0] * 75)}, Clock("2026-11-26 14:30")
    engine, _ = engine_for(tmp_path, feed, clock)
    engine.fetch = None                      # live: Kite first, then Yahoo
    engine.desk.kite = kite
    algo = engine.get(engine.create({**SPEC, "name": "MARUTI", "symbol": "MARUTI", "interval": "1h",
                                     "strategy_params": {"fast": 12, "slow": 26}})["id"])
    return engine, algo, clock


def test_a_1_hour_algo_takes_kite_candles_on_a_thursday(tmp_path, monkeypatch):
    kite = FakeKite(hourly(pd.bdate_range("2026-11-02", "2026-11-26").strftime("%Y-%m-%d")))
    engine, algo, clock = live_engine(tmp_path, kite)
    monkeypatch.setattr(data, "candles", lambda *a, **k: pytest.fail("Yahoo was asked although Kite had the candles"))
    bars = engine._candles(algo, "NSE:MARUTI", clock().replace(tzinfo=None))
    (_, interval, start, end), = kite.asked
    assert interval == "1h" and end - start >= timedelta(days=14) and len(bars) >= 78


@pytest.mark.parametrize("kite, why", [
    (FakeKite(error="Too many requests"), "Kite: Too many requests"),
    (FakeKite(connected=False), "Kite is not logged in"),
])
def test_when_kite_and_yahoo_both_fail_both_reasons_are_shown(tmp_path, monkeypatch, kite, why):
    engine, algo, clock = live_engine(tmp_path, kite)

    def refused(*args, **kwargs):
        raise data.NoData("could not load 1h candles for MARUTI.NS: Yahoo Finance request failed: HTTPError (HTTP 401)")
    monkeypatch.setattr(data, "candles", refused)
    with pytest.raises(data.NoData) as caught:
        engine._candles(algo, "NSE:MARUTI", clock().replace(tzinfo=None))
    assert "HTTP 401" in str(caught.value) and why in str(caught.value)


def test_yahoo_errors_carry_the_http_status(monkeypatch):
    from tradingagents.dataflows.errors import VendorUnavailableError
    from tradingagents.dataflows.vendors.yahoo.common import yf_retry

    class Refused(Exception):
        response = type("R", (), {"status_code": 403})()

    with pytest.raises(VendorUnavailableError, match=r"request failed: Refused \(HTTP 403\)"):
        yf_retry(lambda: (_ for _ in ()).throw(Refused("Forbidden")))
