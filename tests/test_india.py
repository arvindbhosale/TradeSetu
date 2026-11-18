"""The Indian-market defaults this fork adds: NSE/BSE symbols, IST dates, the Nifty benchmark."""

import copy
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import tradingagents.dataflows.config as config_module
import tradingagents.default_config as default_config
from tradingagents.agents.context import build_instrument_context
from tradingagents.dataflows import date_window
from tradingagents.dataflows.symbols import (
    from_kite,
    is_indian,
    kite_instrument,
    normalize_symbol,
    safe_ticker_component,
)
from tradingagents.dataflows.vendors import reddit, stocktwits
from tradingagents.memory.settlement import resolve_benchmark

pytestmark = pytest.mark.unit


@pytest.fixture
def india(monkeypatch):
    """The config an Indian desk runs with, restored afterwards."""
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE", "market_timezone": "Asia/Kolkata"})
    yield
    config_module._config = saved


@pytest.mark.parametrize("typed,expected", [
    ("RELIANCE", "RELIANCE.NS"), ("reliance", "RELIANCE.NS"), ("TCS.NS", "TCS.NS"),
    ("RELIANCE.BO", "RELIANCE.BO"), ("500325", "500325.BO"), ("M&M", "M&M.NS"),
    ("BAJAJ-AUTO", "BAJAJ-AUTO.NS"), ("NSE:INFY", "INFY.NS"), ("BSE:500325", "500325.BO"),
    ("NSE:SBIN-EQ", "SBIN.NS"), ("INFY-EQ", "INFY.NS"), ("bse:reliance", "RELIANCE.BO"),
    ("NIFTY", "^NSEI"), ("NIFTY 50", "^NSEI"), ("NSE:NIFTY 50", "^NSEI"), ("BANKNIFTY", "^NSEBANK"),
    ("SENSEX", "^BSESN"), ("INDIAVIX", "^INDIAVIX"), ("USDINR", "INR=X"),
    ("^NSEI", "^NSEI"), ("GC=F", "GC=F"), ("AAPL", "AAPL.NS"),
    ("BTCUSD", "BTC-USD"), ("XAUUSD", "GC=F"), ("0700.HK", "0700.HK"),
])
def test_symbols_resolve_as_an_indian_trader_types_them(india, typed, expected):
    assert normalize_symbol(typed) == expected


def test_normalizing_twice_changes_nothing(india):
    for typed in ("RELIANCE", "500325", "NSE:INFY", "NIFTY", "M&M", "BSE:TCS", "BTCUSD", "INFY-EQ"):
        once = normalize_symbol(typed)
        assert normalize_symbol(once) == once


def test_bse_default_sends_a_bare_symbol_to_bse(india):
    config_module.set_config({"default_exchange": "BSE"})
    assert normalize_symbol("RELIANCE") == "RELIANCE.BO"
    assert normalize_symbol("NSE:RELIANCE") == "RELIANCE.NS"


def test_without_a_default_exchange_bare_symbols_stay_as_typed():
    # The suite's own setting: upstream behaviour.
    assert normalize_symbol("AAPL") == "AAPL"
    assert normalize_symbol("NSE:INFY") == "INFY.NS"     # an explicit exchange still resolves


def test_kite_instrument_round_trips(india):
    assert kite_instrument("RELIANCE") == ("NSE", "RELIANCE")
    assert kite_instrument("500325") == ("BSE", "500325")
    assert kite_instrument("M&M.NS") == ("NSE", "M&M")
    assert kite_instrument("NIFTY") is None and kite_instrument("BTCUSD") is None
    assert kite_instrument("FINNIFTY") is None
    assert from_kite("NSE", "INFY") == "INFY.NS" and from_kite("bse", "TCS") == "TCS.BO"


def test_indian_symbols_are_recognised():
    assert is_indian("TCS.NS") and is_indian("500325.BO") and is_indian("^NSEI") and is_indian("^NSEBANK")
    assert not is_indian("AAPL") and not is_indian("0700.HK") and not is_indian(None)


def test_ampersand_symbols_are_path_safe_but_traversal_is_not():
    assert safe_ticker_component("M&M.NS") == "M&M.NS"
    for bad in ("../x", "a/b", "NSE:INFY", "..", "A B"):
        with pytest.raises(ValueError):
            safe_ticker_component(bad)


def test_the_default_config_is_indian():
    cfg = default_config.build_default_config()
    # The suite blanks/overrides the env; read the literal defaults instead.
    assert cfg["benchmark_map"][".NS"] == "^NSEI" and cfg["benchmark_map"][".BO"] == "^BSESN"
    assert cfg["benchmark_map"][""] == "^NSEI"
    assert cfg["currency"] == "INR"
    assert cfg["data_vendors"]["fundamental_data"] == "yfinance"
    assert any("RBI" in q for q in cfg["global_news_queries"])


def test_benchmarks_follow_the_listing():
    cfg = default_config.build_default_config()
    assert resolve_benchmark("TCS.NS", cfg) == "^NSEI"
    assert resolve_benchmark("500325.BO", cfg) == "^BSESN"
    assert resolve_benchmark("^NSEBANK", cfg) == "^NSEI"


def test_today_is_the_market_date_in_ist(india, monkeypatch):
    class _Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            # 20:00 UTC on the 14th is already 01:30 on the 15th in India.
            return datetime(2026, 11, 14, 20, 0, tzinfo=ZoneInfo("UTC")).astimezone(tz)

    monkeypatch.setattr(date_window, "datetime", _Clock)
    assert date_window.get_current_date() == "2026-11-15"


def test_indian_listings_get_indian_market_context():
    context = build_instrument_context("TCS.NS")
    assert "INR" in context and "09:15-15:30 IST" in context and "T+1" in context
    assert "INR" not in build_instrument_context("AAPL")


def test_sentiment_sources_are_the_indian_ones():
    assert reddit.subreddits_for("TCS.NS") == reddit.INDIA_SUBREDDITS
    assert reddit.subreddits_for("AAPL") == reddit.DEFAULT_SUBREDDITS
    assert reddit._search_term("RELIANCE.NS") == "RELIANCE"
    assert stocktwits._stocktwits_symbol("RELIANCE.NS") == "RELIANCE.NSE"
    assert stocktwits._stocktwits_symbol("500325.BO") == "500325.BO"
    assert stocktwits._stocktwits_symbol("AAPL") == "AAPL"
