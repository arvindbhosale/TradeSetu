"""Crypto candles from exchanges' public market data: Binance first, Coinbase Exchange when Binance fails.

Neither needs an API key. Binance's ``data-api.binance.vision`` host serves market data only; ``api.binance.com``
is tried after it. Coinbase lists US-dollar pairs, so a Binance USDT pair falls back to the coin's USD pair and
the chart says so (USDT and USD usually differ by well under 1%).
"""
from __future__ import annotations

import threading
import time
from datetime import UTC, datetime

from . import IST_OFFSET, MAX_BARS, TIMEFRAMES, MarketDataError, check_timeframe, merge_rows

# Pairs the symbol search offers: Binance symbol, coin, name, Coinbase product (None: not on Coinbase).
PAIRS = [
    ("BTCUSDT", "BTC", "Bitcoin", "BTC-USD"),
    ("ETHUSDT", "ETH", "Ethereum", "ETH-USD"),
    ("SOLUSDT", "SOL", "Solana", "SOL-USD"),
    ("XRPUSDT", "XRP", "XRP", "XRP-USD"),
    ("BNBUSDT", "BNB", "BNB", None),
    ("DOGEUSDT", "DOGE", "Dogecoin", "DOGE-USD"),
    ("ADAUSDT", "ADA", "Cardano", "ADA-USD"),
    ("AVAXUSDT", "AVAX", "Avalanche", "AVAX-USD"),
    ("LINKUSDT", "LINK", "Chainlink", "LINK-USD"),
    ("DOTUSDT", "DOT", "Polkadot", "DOT-USD"),
    ("LTCUSDT", "LTC", "Litecoin", "LTC-USD"),
    ("BCHUSDT", "BCH", "Bitcoin Cash", "BCH-USD"),
    ("TRXUSDT", "TRX", "TRON", None),
    ("SHIBUSDT", "SHIB", "Shiba Inu", "SHIB-USD"),
    ("UNIUSDT", "UNI", "Uniswap", "UNI-USD"),
    ("ATOMUSDT", "ATOM", "Cosmos", "ATOM-USD"),
    ("NEARUSDT", "NEAR", "NEAR Protocol", "NEAR-USD"),
    ("APTUSDT", "APT", "Aptos", "APT-USD"),
    ("ARBUSDT", "ARB", "Arbitrum", "ARB-USD"),
    ("OPUSDT", "OP", "Optimism", "OP-USD"),
    ("SUIUSDT", "SUI", "Sui", "SUI-USD"),
    ("POLUSDT", "POL", "Polygon", "POL-USD"),
]
BY_SYMBOL = {p[0]: p for p in PAIRS}

BINANCE_HOSTS = ("https://data-api.binance.vision", "https://api.binance.com")
BINANCE_WS = "wss://data-stream.binance.vision/ws"
COINBASE_HOST = "https://api.exchange.coinbase.com"
COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"

# Coinbase's candle sizes; 4 hours and 1 week are built from 1 hour and 1 day.
COINBASE_BASE = {"1m": ("1m", 60), "5m": ("5m", 300), "15m": ("15m", 900), "1h": ("1h", 3600),
                 "4h": ("1h", 3600), "1d": ("1d", 86400), "1w": ("1d", 86400)}
COINBASE_PAGES = {"4h": 3, "1w": 3}          # 300 candles a request
MONDAY_UTC = 4 * 86400                        # 1970-01-05 00:00 UTC, a Monday

SKIP_AFTER_FAILURE = 60                       # seconds to go straight to the fallback after Binance fails


def pair(symbol: str) -> tuple:
    symbol = (symbol or "").strip().upper().replace("/", "").replace("-", "")
    found = BY_SYMBOL.get(symbol)
    if found is None:
        raise ValueError(f"{symbol or 'That symbol'} is not in the crypto list. Pick one from the search.")
    return found


def _get_json(url: str, params: dict):
    """GET JSON. Returns (status, payload); network failures raise OSError-like errors."""
    import httpx

    response = httpx.get(url, params=params, timeout=8, headers={"User-Agent": "desk-charts"})
    try:
        payload = response.json()
    except ValueError:
        payload = None
    return response.status_code, payload


def _reason(status: int, payload) -> str:
    if status == 451:
        return "Binance refuses requests from this location (HTTP 451)"
    if status == 429 or status == 418:
        return f"rate limit reached (HTTP {status})"
    message = payload.get("msg") or payload.get("message") if isinstance(payload, dict) else None
    return f"HTTP {status}" + (f": {str(message)[:120]}" if message else "")


def _number(value) -> float:
    number = float(value)
    if number != number:
        raise ValueError("not a number")
    return number


class Binance:
    name = "Binance"

    def __init__(self, get=None):
        self.get = get or _get_json

    def candles(self, symbol: str, timeframe: str) -> list[dict]:
        errors = []
        for host in BINANCE_HOSTS:
            try:
                status, payload = self.get(f"{host}/api/v3/klines",
                                           {"symbol": symbol, "interval": timeframe, "limit": 1000})
            except Exception as exc:  # noqa: BLE001 - any network failure is reported, then the next host
                errors.append(f"{host.split('//')[1]}: {type(exc).__name__}: {str(exc)[:100]}")
                continue
            if status != 200 or not isinstance(payload, list):
                errors.append(f"{host.split('//')[1]}: {_reason(status, payload)}")
                continue
            rows = []
            for item in payload:
                try:
                    rows.append({"time": int(item[0]) // 1000 + IST_OFFSET, "open": _number(item[1]),
                                 "high": _number(item[2]), "low": _number(item[3]), "close": _number(item[4]),
                                 "volume": _number(item[5])})
                except (TypeError, ValueError, IndexError):
                    continue
            if not rows:
                errors.append(f"{host.split('//')[1]}: no candles for {symbol}")
                continue
            return rows
        raise MarketDataError("Binance did not answer: " + "; ".join(errors), self.name)


def _iso(stamp: int) -> str:
    return datetime.fromtimestamp(stamp, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Coinbase:
    name = "Coinbase"

    def __init__(self, get=None, now=None):
        self.get = get or _get_json
        self.now = now or time.time

    def candles(self, product: str, timeframe: str) -> list[dict]:
        base, seconds = COINBASE_BASE[timeframe]
        end = int(self.now())
        by_time: dict[int, dict] = {}
        for _ in range(COINBASE_PAGES.get(timeframe, 2)):
            start = end - seconds * 300
            try:
                status, payload = self.get(f"{COINBASE_HOST}/products/{product}/candles",
                                           {"granularity": seconds, "start": _iso(start), "end": _iso(end)})
            except Exception as exc:  # noqa: BLE001
                raise MarketDataError(f"Coinbase did not answer: {type(exc).__name__}: {str(exc)[:100]}",
                                      self.name) from exc
            if status != 200 or not isinstance(payload, list):
                raise MarketDataError(f"Coinbase did not answer: {_reason(status, payload)}", self.name)
            for item in payload:
                try:
                    stamp = int(item[0]) + IST_OFFSET
                    by_time[stamp] = {"time": stamp, "low": _number(item[1]), "high": _number(item[2]),
                                      "open": _number(item[3]), "close": _number(item[4]),
                                      "volume": _number(item[5])}
                except (TypeError, ValueError, IndexError):
                    continue
            if len(payload) < 300:
                break
            end = start
        rows = [by_time[t] for t in sorted(by_time)]
        if not rows:
            raise MarketDataError(f"Coinbase returned no candles for {product}.", self.name)
        if timeframe == "4h":
            rows = merge_rows(rows, 14400, IST_OFFSET)
        elif timeframe == "1w":
            rows = merge_rows(rows, 604800, MONDAY_UTC + IST_OFFSET)
        return rows


class CryptoFeed:
    """Binance, then Coinbase. Answers the rows, the provider that served them and a note for the chart."""

    def __init__(self, get=None, now=None):
        self.binance = Binance(get)
        self.coinbase = Coinbase(get, now)
        self.now = now or time.time
        self._cache: dict = {}
        self._binance_failed_at = 0.0
        self._binance_error = ""
        self._lock = threading.Lock()

    def candles(self, symbol: str, timeframe: str) -> dict:
        check_timeframe(timeframe)
        symbol, coin, name, product = pair(symbol)
        key = (symbol, timeframe)
        ttl = 30 if timeframe in ("1d", "1w") else 3
        with self._lock:
            cached = self._cache.get(key)
            if cached and self.now() - cached[0] < ttl:
                return cached[1]
        notes, errors = [], []
        rows, source = None, None
        skip = self.now() - self._binance_failed_at < SKIP_AFTER_FAILURE
        if not skip:
            try:
                rows, source = self.binance.candles(symbol, timeframe), "Binance"
            except MarketDataError as exc:
                self._binance_failed_at, self._binance_error = self.now(), str(exc)
                errors.append(str(exc))
        else:
            errors.append(self._binance_error + " (retrying in a minute)")
        if rows is None:
            if product is None:
                raise MarketDataError(" ".join(errors) + f" Coinbase does not list {coin}, so there is no fallback.",
                                      "Binance")
            try:
                rows, source = self.coinbase.candles(product, timeframe), "Coinbase"
            except MarketDataError as exc:
                raise MarketDataError(" ".join(errors) + f" Fallback failed too. {exc}", "Coinbase") from exc
            notes.append(f"{errors[0]} Showing Coinbase {product} (US dollar) prices instead.")
        result = {"symbol": symbol, "name": f"{name} ({coin}/USDT)" if source == "Binance" else f"{name} ({product})",
                  "candles": rows[-MAX_BARS:], "source": source, "notes": notes,
                  "session_offset": IST_OFFSET,   # crypto's day starts at 00:00 UTC
                  "live": ({"kind": "binance", "url": f"{BINANCE_WS}/{symbol.lower()}@kline_{timeframe}"}
                           if source == "Binance" else
                           {"kind": "coinbase", "url": COINBASE_WS, "product": product}),
                  "seconds": TIMEFRAMES[timeframe]["seconds"],
                  "anchor": (MONDAY_UTC + IST_OFFSET) if timeframe == "1w" else IST_OFFSET}
        with self._lock:
            self._cache[key] = (self.now(), result)
        return result


def _tickers_binance(get, symbols: list[str]) -> dict:
    errors = []
    for host in BINANCE_HOSTS:
        try:
            status, payload = get(f"{host}/api/v3/ticker/24hr",
                                  {"symbols": "[" + ",".join(f'"{s}"' for s in symbols) + "]", "type": "MINI"})
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{host.split('//')[1]}: {type(exc).__name__}: {str(exc)[:100]}")
            continue
        if status != 200 or not isinstance(payload, list):
            errors.append(f"{host.split('//')[1]}: {_reason(status, payload)}")
            continue
        out = {}
        for item in payload:
            try:
                last, open_ = _number(item["lastPrice"]), _number(item["openPrice"])
            except (KeyError, TypeError, ValueError):
                continue
            if last > 0:
                out[item.get("symbol")] = {"price": last, "change_pct": (last / open_ - 1) * 100 if open_ > 0 else None,
                                           "source": "Binance"}
        return out
    raise MarketDataError("Binance did not answer: " + "; ".join(errors), "Binance")


def _ticker_coinbase(get, product: str) -> dict:
    try:
        status, payload = get(f"{COINBASE_HOST}/products/{product}/stats", {})
    except Exception as exc:  # noqa: BLE001
        raise MarketDataError(f"Coinbase did not answer: {type(exc).__name__}: {str(exc)[:100]}", "Coinbase") from exc
    if status != 200 or not isinstance(payload, dict):
        raise MarketDataError(f"Coinbase did not answer: {_reason(status, payload)}", "Coinbase")
    try:
        last, open_ = _number(payload["last"]), _number(payload["open"])
    except (KeyError, TypeError, ValueError) as exc:
        raise MarketDataError(f"Coinbase sent no price for {product}.", "Coinbase") from exc
    if last <= 0:
        raise MarketDataError(f"Coinbase sent no price for {product}.", "Coinbase")
    return {"price": last, "change_pct": (last / open_ - 1) * 100 if open_ > 0 else None, "source": "Coinbase"}


def tickers(feed: CryptoFeed, symbols: list[str]) -> dict:
    """Last price and 24-hour change for each symbol: {symbol: {price, change_pct, source} | {error}}.

    One Binance request for all of them; when Binance fails, Coinbase one coin at a time (USD price).
    Cached for 2 seconds, so a watchlist and the paper book polling together cost one request.
    """
    wanted = [pair(s)[0] for s in dict.fromkeys(symbols)]
    if not wanted:
        return {}
    now = feed.now()
    with feed._lock:
        cache = feed._cache.setdefault("tickers", {})
    fresh = {s: v for s, (t, v) in cache.items() if now - t < 2 and s in wanted}
    missing = [s for s in wanted if s not in fresh]
    if missing:
        got, reason = {}, ""
        if now - feed._binance_failed_at >= SKIP_AFTER_FAILURE:
            try:
                got = _tickers_binance(feed.binance.get, missing)
            except MarketDataError as exc:
                feed._binance_failed_at, feed._binance_error = now, str(exc)
                reason = str(exc)
        else:
            reason = feed._binance_error
        for symbol in missing:
            if symbol in got:
                continue
            product = BY_SYMBOL[symbol][3]
            if product is None:
                got[symbol] = {"error": (reason or "Binance sent no price.") + f" Coinbase does not list {BY_SYMBOL[symbol][1]}."}
                continue
            try:
                got[symbol] = _ticker_coinbase(feed.coinbase.get, product)
            except MarketDataError as exc:
                got[symbol] = {"error": f"{reason} {exc}".strip()}
        with feed._lock:
            for symbol, value in got.items():
                if "error" not in value:
                    cache[symbol] = (now, value)
        fresh.update(got)
    return {s: fresh[s] for s in wanted}
