"""What every non-Kite broker shares: the Kite-shaped surface the desk calls, over the broker's REST API.

The desk was written against :class:`desk.kite.KiteMCP`: methods such as ``holdings()`` or ``quotes()`` and
``call(tool)`` for the MCP tool names (``search_instruments``, ``get_trades``), with answers in Kite's
shape. A :class:`RestBroker` answers the same way, so nothing else in the desk changes with the broker.

An adapter implements the ``_native_*`` methods and the instrument file reader; this class does the
symbol translation (Kite names in, Kite names out), sessions, errors and candle resampling.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta

from ..kite import KiteError, _resampled_rows
from .instruments import Master, MasterCache

logger = logging.getLogger(__name__)

# The Kite MCP tools a REST broker stands in for. GTT tools are not offered: each broker's GTT model differs
# and the desk already handles a broker without them (it says so and keeps GTTs to paper).
TOOLS = ("login", "get_profile", "get_margins", "get_holdings", "get_positions", "get_orders", "get_trades",
         "get_ltp", "get_quotes", "get_historical_data", "search_instruments", "place_order", "cancel_order")

ORDER_STATUS = {
    # Kite's words: COMPLETE, OPEN, CANCELLED, REJECTED, TRIGGER PENDING. Anything unknown stays OPEN so the
    # desk keeps watching it rather than assuming a fill.
    "COMPLETE": "COMPLETE", "COMPLETED": "COMPLETE", "TRADED": "COMPLETE", "EXECUTED": "COMPLETE", "FILLED": "COMPLETE",
    "CANCELLED": "CANCELLED", "CANCELED": "CANCELLED", "EXPIRED": "CANCELLED",
    "REJECTED": "REJECTED", "FAILED": "REJECTED",
    "TRIGGER PENDING": "TRIGGER PENDING", "TRIGGER_PENDING": "TRIGGER PENDING",
}


def kite_status(raw) -> str:
    text = str(raw or "").strip().upper()
    return ORDER_STATUS.get(text, "OPEN")


def ist_now() -> datetime:
    from .. import market

    return market.now_ist()


class RestBroker:
    """Base for a broker reached over its own REST API. Subclasses set the class attributes below."""

    name = ""                 # registry key, e.g. "upstox"
    label = ""                # shown to the user, e.g. "Upstox"
    base_url = ""
    fields: tuple = ()        # credential fields: dicts {key, label, secret, required, hint}
    login_kind = "token"      # "oauth" (browser redirect), "totp" (desk signs in itself) or "token" (pasted)
    minutes = (1,)            # intraday candle sizes the broker serves natively, in minutes
    candle_days = 30          # most days of intraday candles per request
    day_candle_days = 365     # most days of day candles per request

    def __init__(self, session_path=None, http=None):
        from ..settings import home

        self.creds: dict = {}
        self.token: str | None = None
        self.error: str | None = None
        self._connected = False
        self._lock = threading.RLock()
        self._session_path = session_path or home() / "broker-sessions.json"
        self._http = http                    # injected in tests: callable(method, url, **kw) -> (status, json)
        self.masters = MasterCache(self._load_instruments)
        self.tools = {}

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def can_trade(self) -> bool:
        return self._connected and "place_order" in self.tools

    def status(self) -> dict:
        return {
            "connected": self.connected, "can_trade": self.can_trade, "tools": sorted(self.tools),
            "missing_order_tools": [], "transport": "rest", "endpoint": self.base_url, "fallback_from": None,
            "error": self.error, "broker": self.name, "broker_label": self.label, "signed_in": bool(self.token),
            "login_kind": self.login_kind,
        }

    def connect(self, entry: dict, timeout: float = 60.0) -> dict:
        """Take the broker's credentials from Settings. Signing in is :meth:`login`."""
        with self._lock:
            entry = {k: str(v).strip() for k, v in (entry or {}).items() if v is not None}
            missing = [f["label"] for f in self.fields if f.get("required") and not entry.get(f["key"])]
            if missing:
                self.error = f"{self.label} needs {', '.join(missing)} in Settings → Broker."
                raise KiteError(self.error)
            self.creds, self.error = entry, None
            self.tools = {tool: {"description": "", "schema": {}} for tool in TOOLS}
            self._connected = True
            self.token = entry.get("access_token") or self._saved_token()
            return self.status()

    def disconnect(self) -> None:
        with self._lock:
            self._connected, self.tools = False, {}

    def _saved_token(self) -> str | None:
        """Today's session token from an earlier sign-in, if any. Broker sessions end every night."""
        with contextlib.suppress(OSError, ValueError):
            saved = json.loads(self._session_path.read_text(encoding="utf-8")).get(self.name) or {}
            if saved.get("day") == ist_now().strftime("%Y-%m-%d") and saved.get("token"):
                return saved["token"]
        return None

    def _save_token(self, token: str, extra: dict | None = None) -> None:
        self.token = token
        try:
            data = json.loads(self._session_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        data[self.name] = {"token": token, "day": ist_now().strftime("%Y-%m-%d"), **(extra or {})}
        tmp = self._session_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._session_path)

    def login(self) -> dict:
        """Start a sign-in. OAuth brokers answer with a link; the others sign in here and say so."""
        raise NotImplementedError

    def complete_login(self, params: dict) -> dict:
        """Finish an OAuth sign-in with the query the broker redirected back with."""
        raise KiteError(f"{self.label} does not sign in through a browser redirect.")

    def new_state(self) -> str:
        """A one-time value for an OAuth sign-in: the redirect back must carry it (no forged sign-ins)."""
        import secrets

        self._oauth_state = f"{self.name}-{secrets.token_urlsafe(16)}"
        return self._oauth_state

    def check_state(self, params: dict) -> None:
        import hmac

        expected, self._oauth_state = getattr(self, "_oauth_state", None), None
        if not expected or not hmac.compare_digest(str(params.get("state") or ""), expected):
            raise KiteError(f"This {self.label} sign-in link is stale or was not started from this desk. "
                            f"Press Log in again.")

    # -- HTTP --------------------------------------------------------------

    def headers(self) -> dict:
        return {"Accept": "application/json", "Content-Type": "application/json"}

    def request(self, method: str, path: str, *, params=None, json_body=None, data=None, headers=None,
                auth: bool = True, base: str | None = None, timeout: float = 30.0):
        """One API call; returns the decoded JSON. Raises KiteError (``needs_login`` on 401/403)."""
        if auth and not self.token:
            raise KiteError(f"Not signed in to {self.label}. Press Log in on the Trade tab.", needs_login=True)
        url = (base or self.base_url) + path
        sent = {**self.headers(), **(headers or {})}
        if self._http is not None:
            status, body = self._http(method, url, params=params, json=json_body, data=data, headers=sent)
        else:
            import httpx

            try:
                response = httpx.request(method, url, params=params, json=json_body, data=data, headers=sent,
                                         timeout=timeout)
            except httpx.HTTPError as exc:
                raise KiteError(f"{self.label} did not answer: {exc}") from exc
            status = response.status_code
            try:
                body = response.json()
            except ValueError:
                body = {"message": response.text[:300]}
        if status in (401, 403) or self.session_expired(status, body):
            self.token = None
            raise KiteError(f"{self.label} session expired or was refused: {self.message(body)}. Log in again.",
                            needs_login=True)
        if status >= 400 or self.failed(body):
            raise KiteError(f"{self.label}: {self.message(body) or f'HTTP {status}'}")
        return body

    def session_expired(self, status: int, body) -> bool:
        return False

    def failed(self, body) -> bool:
        return isinstance(body, dict) and str(body.get("status", "")).lower() in ("error", "failure", "false")

    @staticmethod
    def message(body) -> str:
        if not isinstance(body, dict):
            return str(body)[:300]
        for key in ("message", "errorMessage", "error_message", "remarks", "emsg"):
            if body.get(key):
                return str(body[key])[:300]
        errors = body.get("errors") or body.get("error")
        if isinstance(errors, list) and errors and isinstance(errors[0], dict):
            return str(errors[0].get("message") or errors[0])[:300]
        if isinstance(errors, dict):
            return str(errors.get("message") or errors.get("displayMessage") or errors)[:300]
        return ""

    # -- instruments ---------------------------------------------------------

    def _load_instruments(self):
        raise NotImplementedError

    def download(self, url: str, timeout: float = 120.0) -> bytes:
        """A public instrument file, cached on disk for the day."""
        import hashlib

        from ..settings import home

        folder = home() / "instruments"
        folder.mkdir(exist_ok=True)
        day = ist_now().strftime("%Y-%m-%d")
        path = folder / f"{self.name}-{hashlib.sha1(url.encode()).hexdigest()[:10]}-{day}"
        if path.exists() and path.stat().st_size:
            return path.read_bytes()
        import httpx

        try:
            response = httpx.get(url, timeout=timeout, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            raise KiteError(f"Could not download {self.label}'s instrument list: {exc}") from exc
        for old in folder.glob(f"{self.name}-*"):
            with contextlib.suppress(OSError):
                old.unlink()
        path.write_bytes(response.content)
        return response.content

    def master(self) -> Master:
        return self.masters.get(ist_now().strftime("%Y-%m-%d"))

    def row(self, instrument: str) -> dict:
        found = self.master().get(instrument)
        if not found:
            raise KiteError(f"{self.label} does not list {instrument}.")
        return found

    def rows(self, instruments: list[str]) -> dict[str, dict]:
        """Kite name → row for the instruments the broker lists (unknown ones are left out)."""
        master, out = self.master(), {}
        for instrument in instruments:
            found = master.get(instrument)
            if found:
                out[instrument] = found
        return out

    def by_key(self, key) -> dict | None:
        return self.master().by_key.get(str(key))

    def kite_names(self, row: dict, native_symbol: str = "", exchange: str = "") -> tuple[str, str]:
        """(exchange, tradingsymbol) as Kite spells them, for a row of the broker's own answer."""
        if row:
            return row["exchange"], row["tradingsymbol"]
        return exchange, native_symbol

    # -- the Kite-shaped surface ---------------------------------------------

    def call(self, tool: str, arguments: dict | None = None, timeout: float = 45.0):
        if not self._connected:
            raise KiteError(f"not connected to {self.label}")
        if tool not in self.tools:
            raise KiteError(f'{self.label} has no "{tool}" here')
        args = arguments or {}
        if tool == "search_instruments":
            return self.master().search(args.get("query", ""), args.get("filter_on", "id"))
        if tool == "get_trades":
            return self.trades()
        if tool == "get_historical_data":
            raise KiteError("Use candles() with a Kite instrument name for this broker.")
        simple = {"get_profile": self.profile, "get_margins": self.margins, "get_holdings": self.holdings,
                  "get_positions": self.positions, "get_orders": self.orders, "login": self.login}
        if tool in simple:
            return simple[tool]()
        if tool == "get_ltp":
            return {k: {"last_price": v} for k, v in self.ltp(args.get("instruments") or []).items()}
        if tool == "get_quotes":
            return self.quotes(args.get("instruments") or [])
        if tool == "place_order":
            return self.place_order(args)
        if tool == "cancel_order":
            return self.cancel_order(args.get("order_id"), args.get("variety", "regular"))
        raise KiteError(f'{self.label} has no "{tool}" here')

    def profile(self) -> dict:
        return self._native_profile()

    def margins(self) -> dict:
        return self._native_margins()

    def holdings(self) -> list:
        return self._native_holdings()

    def positions(self) -> list:
        return self._native_positions()

    def orders(self) -> list:
        return self._native_orders()

    def trades(self) -> list:
        return self._native_trades()

    def gtts(self) -> list:
        return []

    def place_gtt(self, args: dict):
        raise KiteError(f"GTT orders are not supported for {self.label} in TradeSetu yet.")

    def delete_gtt(self, trigger_id):
        raise KiteError(f"GTT orders are not supported for {self.label} in TradeSetu yet.")

    def ltp(self, instruments: list[str]) -> dict[str, float]:
        if not instruments:
            return {}
        return {k: float(q["last_price"]) for k, q in self.quotes(instruments).items()
                if isinstance(q.get("last_price"), (int, float)) and q["last_price"] > 0}

    def quotes(self, instruments: list[str]) -> dict[str, dict]:
        known = self.rows(list(dict.fromkeys(instruments)))
        return self._native_quotes(known) if known else {}

    def place_order(self, order: dict):
        if order.get("variety") not in (None, "", "regular", "amo"):
            raise KiteError(f"{self.label}: only regular and after-market orders are supported here.")
        row = self.row(f"{order.get('exchange')}:{order.get('tradingsymbol')}")
        return self._native_place(row, order)

    def cancel_order(self, order_id: str, variety: str = "regular"):
        return self._native_cancel(str(order_id))

    def candles(self, instrument: str, interval: str, start, end, continuous: bool = False, oi: bool = False) -> list:
        """Candles in Kite's row shape (``date`` with +05:30, open, high, low, close, volume[, oi])."""
        from .. import data

        row = self.row(instrument)
        spec = data.INTERVALS[interval]
        if interval == "1d":
            step, native = self.day_candle_days, None
        else:
            native = max((m for m in self.minutes if spec["minutes"] % m == 0), default=None)
            if native is None:
                raise KiteError(f"{self.label} has no candles that make {spec['label']} ones.")
            step = self.candle_days
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        rows, cursor = [], start
        while cursor <= end:
            stop = min(cursor + timedelta(days=step), end)
            rows.extend(self._native_candles(row, native, cursor, stop, oi))
            cursor = stop + timedelta(seconds=1)
            if stop >= end:
                break
        seen, unique = set(), []
        for r in sorted(rows, key=lambda r: r["date"]):
            if r["date"] not in seen:
                seen.add(r["date"])
                unique.append(r)
        if native and native != spec["minutes"]:
            return _resampled_rows(unique, spec["minutes"])
        return unique

    # -- adapter hooks -------------------------------------------------------

    def _native_profile(self) -> dict: raise NotImplementedError
    def _native_margins(self) -> dict: raise NotImplementedError
    def _native_holdings(self) -> list: raise NotImplementedError
    def _native_positions(self) -> list: raise NotImplementedError
    def _native_orders(self) -> list: raise NotImplementedError
    def _native_trades(self) -> list: raise NotImplementedError
    def _native_quotes(self, rows: dict[str, dict]) -> dict[str, dict]: raise NotImplementedError
    def _native_place(self, row: dict, order: dict) -> dict: raise NotImplementedError
    def _native_cancel(self, order_id: str) -> dict: raise NotImplementedError
    def _native_candles(self, row: dict, minutes: int | None, start, end, oi: bool) -> list: raise NotImplementedError


def margins_shape(equity_available: float | None, equity_used: float | None = None,
                  commodity_available: float | None = None, commodity_used: float | None = None) -> dict:
    """Funds in Kite's ``get_margins`` shape (``equity`` and ``commodity`` segments)."""
    def segment(available, used):
        available = float(available or 0)
        return {"enabled": True, "net": available, "available": {"cash": available, "live_balance": available},
                "utilised": {"debits": float(used or 0)}}
    out = {"equity": segment(equity_available, equity_used)}
    if commodity_available is not None:
        out["commodity"] = segment(commodity_available, commodity_used)
    return out


def ist_stamp(value) -> str:
    """A candle time as Kite writes it, ``2025-11-14T09:15:00+0530``, from epoch seconds/ms or ISO text."""
    from .instruments import _IST

    if isinstance(value, (int, float)) or (isinstance(value, str) and value.strip().isdigit()):
        number = float(value)
        stamp = datetime.fromtimestamp(number / 1000 if number > 1e11 else number, tz=_IST)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        stamp = datetime.fromisoformat(text)
        stamp = stamp.replace(tzinfo=_IST) if stamp.tzinfo is None else stamp.astimezone(_IST)
    return stamp.strftime("%Y-%m-%dT%H:%M:%S+0530")


def kite_quote(row: dict, last=None, open_=None, high=None, low=None, close=None, volume=None, oi=None,
               average=None, buy_qty=None, sell_qty=None, lower=None, upper=None, last_time=None,
               bids=None, asks=None) -> dict:
    """A quote in Kite's ``get_quotes`` shape."""
    def num(v):
        try:
            return float(v) if v is not None and v != "" else None
        except (TypeError, ValueError):
            return None
    quote = {"instrument_token": row.get("instrument_token"), "last_price": num(last),
             "ohlc": {"open": num(open_), "high": num(high), "low": num(low), "close": num(close)},
             "volume": num(volume), "oi": num(oi), "average_price": num(average), "buy_quantity": num(buy_qty),
             "sell_quantity": num(sell_qty), "lower_circuit_limit": num(lower), "upper_circuit_limit": num(upper),
             "last_trade_time": last_time or ""}
    if quote["last_price"] is not None and quote["ohlc"]["close"]:
        quote["net_change"] = round(quote["last_price"] - quote["ohlc"]["close"], 4)
    if bids is not None or asks is not None:
        quote["depth"] = {"buy": bids or [], "sell": asks or []}
    return quote

