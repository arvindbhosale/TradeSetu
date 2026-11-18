"""Everything the desk knows about one stock: live quote, technicals, fundamentals."""

from __future__ import annotations

import contextlib

import pandas as pd

from tradingagents.dataflows.symbols import kite_instrument

from . import data, indicators as ind, market, trend


def technicals(bars: pd.DataFrame, rule: dict) -> dict:
    close, last = bars["Close"], float(bars["Close"].iloc[-1])
    year = bars.iloc[-250:]

    def pct(a, b):
        return round((a / b - 1) * 100, 2) if b else None

    def back(days):
        return pct(last, float(close.iloc[-1 - days])) if len(close) > days else None

    out = {"last_close": round(last, 2), "trend": trend.classify(bars, rule)}
    for period in (20, 50, 200):
        if len(close) >= period:
            average = float(close.iloc[-period:].mean())
            out[f"sma{period}"] = round(average, 2)
            out[f"vs_sma{period}"] = pct(last, average)
    rsi = ind.rsi(close, 14).iloc[-1]
    line, signal, _ = ind.macd(close)
    adx, plus_di, minus_di = ind.adx(bars)
    atr = ind.atr(bars, 14).iloc[-1]
    _, st_dir = ind.supertrend(bars, 10, 3.0)
    out.update(
        rsi14=None if pd.isna(rsi) else round(float(rsi), 1),
        macd=None if pd.isna(line.iloc[-1]) else round(float(line.iloc[-1]), 2),
        macd_signal=None if pd.isna(signal.iloc[-1]) else round(float(signal.iloc[-1]), 2),
        macd_state=None if pd.isna(signal.iloc[-1]) else ("above signal" if line.iloc[-1] > signal.iloc[-1] else "below signal"),
        adx14=None if pd.isna(adx.iloc[-1]) else round(float(adx.iloc[-1]), 1),
        di_state=None if pd.isna(plus_di.iloc[-1]) else ("+DI leads" if plus_di.iloc[-1] > minus_di.iloc[-1] else "−DI leads"),
        atr14_pct=None if pd.isna(atr) else round(float(atr) / last * 100, 2),
        supertrend="up" if int(st_dir.iloc[-1]) == 1 else "down",
        high_52w=round(float(year["High"].max()), 2), low_52w=round(float(year["Low"].min()), 2),
        from_high_52w=pct(last, float(year["High"].max())), from_low_52w=pct(last, float(year["Low"].min())),
        return_1w=back(5), return_1m=back(21), return_3m=back(63), return_6m=back(126), return_1y=back(250),
    )
    return out


def stock_view(symbol: str, kite, fundamentals, industries: dict, rule: dict, fetch=None) -> dict:
    """One stock's quote (Kite), technicals (from daily candles) and fundamentals."""
    instrument = "{}:{}".format(*kite_instrument(symbol))
    out = {"symbol": symbol, "instrument": instrument, "quote": None, "quote_source": None, "industry": industries.get(symbol)}
    if kite is not None and kite.connected and "get_quotes" in kite.tools:
        with contextlib.suppress(Exception):
            quote = kite.quotes([instrument]).get(instrument)
            if quote:
                day = quote.get("ohlc") or {}
                out["quote"] = {"last": quote.get("last_price"), "open": day.get("open"), "high": day.get("high"),
                                "low": day.get("low"), "prev_close": day.get("close"), "change_pct": round(
                                    (quote["last_price"] / day["close"] - 1) * 100, 2) if day.get("close") else None,
                                "volume": quote.get("volume"), "average_price": quote.get("average_price"),
                                "buy_quantity": quote.get("buy_quantity"), "sell_quantity": quote.get("sell_quantity"),
                                "lower_circuit": quote.get("lower_circuit_limit"), "upper_circuit": quote.get("upper_circuit_limit"),
                                "last_trade_time": str(quote.get("last_trade_time") or "")}
                out["quote_source"] = "Kite (live)"
    bars, source = None, None
    if kite is not None and kite.connected:
        with contextlib.suppress(Exception):
            bars, source = data.kite_bars(kite, symbol, "1d", market.now_ist()), "Kite daily candles"
    if bars is None:
        with contextlib.suppress(data.NoData):
            bars, source = data.history(symbol, fetch=fetch), "Yahoo daily candles (adjusted)"
    if bars is not None and len(bars) > 30:
        out["technicals"], out["technicals_source"] = technicals(bars, rule), source
        if out["quote"] is None:
            last, previous = float(bars["Close"].iloc[-1]), float(bars["Close"].iloc[-2])
            out["quote"] = {"last": round(last, 2), "prev_close": round(previous, 2),
                            "change_pct": round((last / previous - 1) * 100, 2), "open": round(float(bars["Open"].iloc[-1]), 2),
                            "high": round(float(bars["High"].iloc[-1]), 2), "low": round(float(bars["Low"].iloc[-1]), 2),
                            "volume": int(bars["Volume"].iloc[-1]) if "Volume" in bars else None}
            out["quote_source"] = "last close (delayed)"
    out["fundamentals"] = fundamentals.load(symbol)
    return out
