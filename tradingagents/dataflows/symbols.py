"""Symbol normalization for vendor calls, and ticker values safe to use in a path.

Yahoo Finance (the default vendor) uses specific ticker conventions that
differ from the broker / TradingView / MT5 style symbols users often type:

    user types        Yahoo wants       why
    ---------------   ---------------   -----------------------------------
    XAUUSD, XAUUSD+   GC=F              gold has no forex pair on Yahoo;
                                        it is quoted as a COMEX future
    EURUSD            EURUSD=X          spot forex pairs take a ``=X`` suffix
    BTCUSD            BTC-USD           crypto pairs use a ``-`` separator
    SPX500, US500     ^GSPC             index CFDs map to Yahoo index symbols
    09992.HK, 700.HK  9992.HK, 0700.HK  HK codes are zero-padded to 4 digits
    600519.SH         600519.SS         Yahoo spells Shanghai ``.SS``

Passing the raw broker symbol to Yahoo returns an empty result, which the
agents previously received as free text and could hallucinate a price
around (see issue #781). Centralizing the mapping here means every yfinance
entry point resolves symbols the same way, and new instruments are added by
appending a table row rather than editing call sites.
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)


# ISO-4217 codes common enough to appear in retail forex pairs. A bare
# six-letter symbol whose halves are BOTH in this set is treated as a spot
# forex pair and given Yahoo's ``=X`` suffix.
_FOREX_CURRENCIES = frozenset(
    {
        "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD",
        "CNY", "CNH", "HKD", "SGD", "SEK", "NOK", "DKK", "PLN",
        "MXN", "ZAR", "TRY", "INR", "KRW", "BRL", "RUB", "THB",
    }
)

# Crypto bases that brokers quote against USD without a separator.
_CRYPTO_BASES = frozenset(
    {"BTC", "ETH", "SOL", "XRP", "ADA", "DOGE", "LTC", "BCH", "DOT", "AVAX", "LINK"}
)

# Explicit aliases for instruments whose broker symbol does not map to a
# Yahoo symbol by rule. Metals/energy resolve to their front-month future;
# index CFD names resolve to the underlying Yahoo index symbol. Extend by
# adding rows — no call site changes required.
_ALIASES = {
    # Precious metals (spot names -> COMEX/NYMEX futures)
    "XAUUSD": "GC=F", "XAU": "GC=F", "GOLD": "GC=F",
    "XAGUSD": "SI=F", "XAG": "SI=F", "SILVER": "SI=F",
    "XPTUSD": "PL=F", "XPDUSD": "PA=F",
    # Energy
    "WTICOUSD": "CL=F", "USOIL": "CL=F", "WTI": "CL=F",
    "BCOUSD": "BZ=F", "UKOIL": "BZ=F", "BRENT": "BZ=F",
    "NATGAS": "NG=F", "XNGUSD": "NG=F",
    "COPPER": "HG=F", "XCUUSD": "HG=F",
    # Index CFDs -> Yahoo index symbols
    "SPX500": "^GSPC", "US500": "^GSPC", "SPX": "^GSPC",
    "NAS100": "^NDX", "US100": "^NDX", "USTEC": "^NDX",
    "US30": "^DJI", "DJI30": "^DJI", "WS30": "^DJI",
    "GER40": "^GDAXI", "GER30": "^GDAXI", "DE40": "^GDAXI",
    "UK100": "^FTSE", "JP225": "^N225", "JPN225": "^N225",
    "FRA40": "^FCHI", "EU50": "^STOXX50E", "HK50": "^HSI",
    # Indian indices, as traders and Kite spell them -> Yahoo index symbols
    "NIFTY": "^NSEI", "NIFTY50": "^NSEI", "NIFTY 50": "^NSEI", "NIFTY_50": "^NSEI",
    "BANKNIFTY": "^NSEBANK", "NIFTYBANK": "^NSEBANK", "NIFTY BANK": "^NSEBANK",
    "SENSEX": "^BSESN", "BSESENSEX": "^BSESN",
    "FINNIFTY": "NIFTY_FIN_SERVICE.NS", "NIFTY FIN SERVICE": "NIFTY_FIN_SERVICE.NS",
    "NIFTYNXT50": "^NSMIDCP", "NIFTY NEXT 50": "^NSMIDCP",
    "NIFTYIT": "^CNXIT", "NIFTY IT": "^CNXIT",
    "NIFTYAUTO": "^CNXAUTO", "NIFTYPHARMA": "^CNXPHARMA", "NIFTYFMCG": "^CNXFMCG",
    "NIFTYMETAL": "^CNXMETAL", "NIFTYREALTY": "^CNXREALTY", "NIFTYENERGY": "^CNXENERGY",
    "NIFTYPSUBANK": "^CNXPSUBANK", "NIFTY500": "^CRSLDX", "NIFTY 500": "^CRSLDX",
    "INDIAVIX": "^INDIAVIX", "INDIA VIX": "^INDIAVIX",
    "USDINR": "INR=X",
}

# Yahoo's suffix for each Indian exchange, keyed by the exchange code Kite and
# most Indian terminals prefix a symbol with (``NSE:INFY``, ``BSE:500325``).
INDIA_EXCHANGE_SUFFIX = {"NSE": ".NS", "BSE": ".BO"}
INDIA_SUFFIXES = tuple(INDIA_EXCHANGE_SUFFIX.values())

_EXCHANGE_PREFIXED = re.compile(r"^(NSE|BSE):(.+)$")
# A symbol with no exchange marking at all: an NSE trading symbol (letters,
# digits, ``&`` and ``-``: M&M, BAJAJ-AUTO, 3MINDIA) or a BSE scrip code.
_BARE_EQUITY = re.compile(r"^[A-Z0-9][A-Z0-9&\-]*$")
_BSE_SCRIP_CODE = re.compile(r"^\d{6}$")
# NSE series markers Kite and bhavcopies append; Yahoo lists the plain symbol.
_NSE_SERIES = re.compile(r"-(EQ|BE|BZ|SM|ST)$")


def default_exchange() -> str:
    """The exchange a bare symbol is assumed to list on: ``NSE``, ``BSE`` or ``""``.

    Read from the active config (``default_exchange``). Empty leaves a bare
    symbol as typed, which is how the upstream project treats it (a US listing).
    With an Indian default a bare US ticker cannot be told from an NSE symbol,
    so US listings need the default switched off.
    """
    from tradingagents.dataflows.config import get_config_value

    value = str(get_config_value("default_exchange") or "").strip().upper()
    return value if value in INDIA_EXCHANGE_SUFFIX else ""


def is_indian(symbol: str) -> bool:
    """Whether a canonical symbol is an NSE or BSE listing or an Indian index."""
    if not isinstance(symbol, str):
        return False
    s = symbol.strip().upper()
    return s.endswith(INDIA_SUFFIXES) or s in _INDIAN_INDEX_SYMBOLS


def kite_instrument(symbol: str) -> tuple[str, str] | None:
    """``(exchange, tradingsymbol)`` as Kite spells an NSE/BSE equity, else None.

    ``RELIANCE.NS`` -> ``("NSE", "RELIANCE")``; an index or a non-Indian symbol
    has no cash-market order to place, so it yields None.
    """
    s = normalize_symbol(symbol)
    if not isinstance(s, str):
        return None
    for exchange, suffix in INDIA_EXCHANGE_SUFFIX.items():
        if s.endswith(suffix) and not s.startswith("^") and "_" not in s and s not in _INDIAN_INDEX_SYMBOLS:
            return exchange, s[: -len(suffix)]
    return None


def from_kite(exchange: str, tradingsymbol: str) -> str:
    """The Yahoo symbol for a Kite ``exchange`` and ``tradingsymbol``."""
    return normalize_symbol(f"{exchange.strip().upper()}:{tradingsymbol.strip()}")

# HKEX codes as Yahoo spells them: the number zero-padded to 4 digits (#957).
_HK_CODE = re.compile(r"^(\d{1,5})\.HK$")
_SHANGHAI_SH = re.compile(r"^(\d{6})\.SH$")


# Crypto quote currencies that all map to Yahoo's USD pair. Yahoo lists only
# ``<BASE>-USD`` (not the USDT/USDC stablecoin pairs), so a broker symbol quoted
# in any of these resolves to ``-USD`` (#982). Longest first so ``USDT``/``USDC``
# match before the ``USD`` substring.
_CRYPTO_QUOTES = ("USDT", "USDC", "USD")


def crypto_base(raw: str) -> str | None:
    """Return the crypto base (e.g. ``BTC``) for a known USD/USDT/USDC-quoted
    crypto symbol in any form the pipeline may hold — ``BTC-USD``, ``BTCUSD``,
    ``BTC-USDT`` — or None for non-crypto symbols. Purely syntactic.
    """
    if not isinstance(raw, str):
        return None
    compact = raw.strip().upper().rstrip("+").replace("-", "")
    for quote in _CRYPTO_QUOTES:
        if compact.endswith(quote):
            base = compact[: -len(quote)]
            return base if base in _CRYPTO_BASES else None
    return None


def _normalize_crypto(s: str) -> str | None:
    """Return ``<BASE>-USD`` for a known USD/USDT/USDC-quoted crypto, else None."""
    base = crypto_base(s)
    return f"{base}-USD" if base else None


def normalize_symbol(raw: str) -> str:
    """Map a user/broker symbol to its canonical Yahoo Finance symbol.

    Resolution order (first match wins):
      1. Explicit alias table (metals, energy, index CFDs).
      2. Crypto rule: a known crypto base quoted in USD/USDT/USDC (dashed or
         not) -> ``BASE-USD``.
      3. Forex rule: six letters that are two ISO currency codes -> ``PAIR=X``.
      4. HK rule: a numeric ``.HK`` code -> Yahoo's 4-digit padding
         (``09992.HK`` -> ``9992.HK``, ``700.HK`` -> ``0700.HK``).
      5. Shanghai rule: ``600519.SH`` -> ``600519.SS``.
      6. Otherwise the upper-cased symbol is returned unchanged (plain
         equities, ETFs, Yahoo-native symbols like ``GC=F`` or ``^GSPC``).

    A trailing ``+`` (broker CFD marker, e.g. ``XAUUSD+``) is stripped before
    matching. The function is purely syntactic — it performs no network
    calls — so it is safe to apply on every request.
    """
    if not isinstance(raw, str) or not raw.strip():
        return raw

    s = raw.strip().upper()
    # Broker CFD/qualifier suffixes Yahoo never uses.
    s = s.rstrip("+")

    crypto = _normalize_crypto(s)
    prefixed = _EXCHANGE_PREFIXED.match(s)
    if prefixed and prefixed.group(2).strip() in _ALIASES:
        # ``NSE:NIFTY 50`` is how Kite names an index; the alias wins.
        s = prefixed.group(2).strip()
        prefixed = None
    if prefixed:
        body = _NSE_SERIES.sub("", prefixed.group(2).strip())
        canonical = f"{body}{INDIA_EXCHANGE_SUFFIX[prefixed.group(1)]}"
    elif s in _ALIASES:
        canonical = _ALIASES[s]
    elif crypto is not None:
        canonical = crypto
    elif len(s) == 6 and s[:3] in _FOREX_CURRENCIES and s[3:] in _FOREX_CURRENCIES:
        canonical = f"{s}=X"
    elif hk := _HK_CODE.match(s):
        canonical = f"{int(hk.group(1)):04d}.HK"
    elif sh := _SHANGHAI_SH.match(s):
        canonical = f"{sh.group(1)}.SS"
    elif (exchange := default_exchange()) and _BARE_EQUITY.match(s):
        # A bare symbol on an Indian desk is an NSE/BSE listing. A six-digit
        # number is a BSE scrip code whatever the default exchange is.
        body = _NSE_SERIES.sub("", s)
        suffix = ".BO" if _BSE_SCRIP_CODE.match(body) else INDIA_EXCHANGE_SUFFIX[exchange]
        canonical = f"{body}{suffix}"
    else:
        canonical = s

    if canonical != raw.strip().upper():
        logger.info("Resolved symbol %r to Yahoo symbol %r", raw, canonical)
    return canonical


_INDIAN_INDEX_SYMBOLS = frozenset(
    v for k, v in _ALIASES.items() if k.startswith(("NIFTY", "BANKNIFTY", "SENSEX", "BSESENSEX",
                                                    "FINNIFTY", "INDIA"))
)


# Tickers can contain letters, digits, dot, dash, underscore, caret
# (index symbols like ^GSPC), equals (futures like GC=F), plus
# (forex/CFD symbols like XAUUSD+), and ampersand (NSE symbols like M&M.NS). None of these enable directory
# traversal, so the value never escapes a containing directory when
# interpolated into a path. Anything else is rejected.
_TICKER_PATH_RE = re.compile(r"^[A-Za-z0-9._\-\^=+&]+$")


def safe_ticker_component(value: str, *, max_len: int = 32) -> str:
    """Validate ``value`` is safe to interpolate into a filesystem path.

    Tickers come from user CLI input or from LLM tool calls, both of which
    can be influenced by attacker-controlled content (e.g. prompt injection
    embedded in fetched news). Without validation, a value like
    ``"../../../etc/foo"`` flows into ``os.path.join`` / ``Path /`` and
    escapes the configured cache, checkpoint, or results directory.

    Returns ``value`` unchanged when it matches the allowed pattern; raises
    ``ValueError`` otherwise.
    """
    if not isinstance(value, str) or not value:
        raise ValueError(f"ticker must be a non-empty string, got {value!r}")
    if len(value) > max_len:
        raise ValueError(f"ticker exceeds {max_len} chars: {value!r}")
    if not _TICKER_PATH_RE.fullmatch(value):
        raise ValueError(
            f"ticker contains characters not allowed in a filesystem path: {value!r}"
        )
    # The regex above allows '.', so values like '.', '..', '...' would pass,
    # and as a path component they traverse the parent directory. Reject any
    # value that's only dots.
    if set(value) == {"."}:
        raise ValueError(f"ticker cannot consist solely of dots: {value!r}")
    return value
