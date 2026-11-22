"""Zerodha Kite Connect, called directly with your own API key and secret.

The desk can reach Kite two ways. Through a Kite MCP server (:mod:`desk.kite`), or, with a Kite Connect
app's API key and secret saved in Settings, straight to Kite's REST API from here. The direct way needs no
Node.js and no separate server, places orders and GTTs, has Zerodha's basket margin calculator, and keeps
the day's login in one place.

One login a day. Kite's access token is valid until 6 AM IST the next morning (Zerodha's regulatory rule),
whatever the desk does. The token is saved in the desk's home folder (``kite-session.json``, readable only by
you) the moment you sign in, so restarting the app, the desktop window or the server reuses it, and every
part of the desk (orders, algos, charts, F&O, margin) uses this one object. A new login is asked for only
when the token reaches 6 AM, Zerodha refuses it (you logged out of all apps, changed your password), or
the API key changes.

The answers have the shapes kite-mcp-server gives, and ``call(tool)`` takes its tool names, so the rest of
the desk does not care which way Kite is reached.

Signing in stays your step: Zerodha's login page, your password and your TOTP. Logging in for you with a
stored password and TOTP secret is against Kite Connect's terms, so the desk does not do it.
"""

from __future__ import annotations

import contextlib
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse

from .kite import KiteError, KiteMCP, _resampled_rows

logger = logging.getLogger(__name__)

API = "https://api.kite.trade"
LOGIN_URL = "https://kite.zerodha.com/connect/login"
# The kite-mcp-server tool names this client answers to.
TOOLS = ("login", "get_profile", "get_margins", "get_holdings", "get_positions", "get_orders", "get_trades",
         "get_ltp", "get_quotes", "get_historical_data", "search_instruments", "place_order", "modify_order",
         "cancel_order", "get_gtts", "place_gtt_order", "delete_gtt_order", "get_basket_margins")
QUOTE_BATCH, LTP_BATCH = 500, 1000          # Kite's most instruments per /quote and /quote/ltp call
EXPIRY_HOUR = 6                              # Kite access tokens end at 06:00 IST
_TOKEN = re.compile(r"^[A-Za-z0-9]{8,64}$")


def _ist_now() -> datetime:
    from . import market

    return market.now_ist()


def token_expiry(login: datetime) -> datetime:
    """When a token issued at ``login`` (IST, aware) stops working: the next 06:00 IST."""
    cutoff = login.replace(hour=EXPIRY_HOUR, minute=0, second=0, microsecond=0)
    return cutoff if login < cutoff else cutoff + timedelta(days=1)


def request_token_from(text: str) -> str:
    """The ``request_token`` in a pasted Kite redirect address, or the token itself."""
    text = str(text or "").strip()
    if "request_token" in text:
        query = parse_qs(urlparse(text).query) or parse_qs(text.split("?", 1)[-1])
        text = (query.get("request_token") or [""])[0].strip()
    if not _TOKEN.fullmatch(text):
        raise KiteError("That is not a Kite request token. Paste the whole address the Kite login sent you to "
                        "(it contains request_token=...).")
    return text


class KiteDirect:
    """Kite Connect over REST, in kite-mcp-server's shapes. One instance serves the whole desk."""

    name = "kite"
    label = "Zerodha Kite"

    def __init__(self, session_path: Path | None = None, http=None, instruments_dir: Path | None = None):
        from .settings import home

        self.api_key = ""
        self._secret = ""
        self.redirect_uri = ""
        self.token: str | None = None
        self.user: dict = {}
        self.expires: datetime | None = None
        self.error: str | None = None
        self.tools: dict[str, dict] = {}
        self._connected = False
        self._lock = threading.RLock()
        self._session_path = session_path or home() / "kite-session.json"
        self._instruments_dir = instruments_dir
        self._http = http                    # tests: callable(method, url, **kw) -> (status, body, headers)
        self._state: str | None = None
        self._instruments: tuple[str, list[dict]] | None = None
        self._tokens: dict[str, int] = {}
        self._last_call: dict[str, float] = {}

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._connected

    @property
    def can_trade(self) -> bool:
        return self._connected

    @property
    def signed_in(self) -> bool:
        if not self.token or not self.expires:
            return False
        if _ist_now() >= self.expires:
            self._forget("Your Kite login ended at 6 AM. Log in to Kite again for today.")
            return False
        return True

    def status(self) -> dict:
        return {
            "connected": self._connected, "can_trade": self.can_trade, "tools": sorted(self.tools),
            "missing_order_tools": [], "transport": "kite connect", "endpoint": API, "fallback_from": None,
            "error": self.error, "broker": "kite", "broker_label": self.label, "login_kind": "kite_connect",
            "direct": True, "signed_in": self.signed_in, "user_id": self.user.get("user_id"),
            "user_name": self.user.get("user_name"),
            "session_until": self.expires.strftime("%Y-%m-%d %H:%M IST") if self.signed_in and self.expires else None,
            "redirect_uri": self.redirect_uri or None,
        }

    def connect(self, entry: dict, timeout: float = 60.0) -> dict:
        """Take the API key and secret; picks up today's saved login, if there is one."""
        with self._lock:
            key = str(entry.get("api_key") or "").strip()
            secret = str(entry.get("api_secret") or "").strip()
            if not key or not secret:
                self.error = "Save your Kite Connect API key and API secret in Settings → Broker first."
                raise KiteError(self.error)
            if key != self.api_key:
                self.token, self.user, self.expires = None, {}, None
            self.api_key, self._secret = key, secret
            self.redirect_uri = str(entry.get("redirect_uri") or "").strip()
            self.tools = {tool: {"description": "", "schema": {}} for tool in TOOLS}
            self._connected, self.error = True, None
            if not self.signed_in:
                self._load_session()
            return self.status()

    def disconnect(self) -> None:
        """Stop using Kite. Today's login stays saved, so Connect picks it up again without a new login."""
        with self._lock:
            self._connected, self.tools = False, {}

    def logout(self) -> None:
        """End today's Kite session at Zerodha and forget it here."""
        with self._lock:
            if self.token:
                with contextlib.suppress(KiteError):
                    self.request("DELETE", "/session/token", params={"api_key": self.api_key,
                                                                     "access_token": self.token})
            self._forget(None)

    def _load_session(self) -> None:
        with contextlib.suppress(OSError, ValueError, TypeError, KeyError):
            saved = json.loads(self._session_path.read_text(encoding="utf-8"))
            expires = datetime.fromisoformat(saved["expires"])
            if saved.get("api_key") == self.api_key and saved.get("access_token") and _ist_now() < expires:
                self.token, self.expires, self.user = saved["access_token"], expires, saved.get("user") or {}

    def _save_session(self) -> None:
        data = {"api_key": self.api_key, "access_token": self.token, "expires": self.expires.isoformat(),
                "user": self.user, "saved": _ist_now().isoformat()}
        tmp = self._session_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)
        os.replace(tmp, self._session_path)

    def _forget(self, reason: str | None) -> None:
        self.token, self.expires, self.user = None, None, {}
        if reason:
            self.error = reason
        with contextlib.suppress(OSError):
            self._session_path.unlink()

    # -- sign-in -----------------------------------------------------------

    def login(self) -> dict:
        """Zerodha's login link. Kite sends the browser back to the redirect URL set on your Kite app."""
        if not self._connected:
            raise KiteError("Connect first: Settings → Broker needs your Kite Connect API key and secret.")
        if self.signed_in:
            who = self.user.get("user_name") or self.user.get("user_id") or "you"
            return {"url": None, "signed_in": True,
                    "message": f"Already logged in to Kite as {who} until "
                               f"{self.expires:%d %b, %I:%M %p} IST. No new login is needed today."}
        self._state = secrets.token_urlsafe(16)
        url = f"{LOGIN_URL}?{urlencode({'v': 3, 'api_key': self.api_key, 'redirect_params': urlencode({'state': self._state})})}"
        hint = (f" Your Kite app's redirect URL must be {self.redirect_uri}." if self.redirect_uri else "")
        return {"url": url, "message": "Open the link and log in to Kite. You come back here when it is done."
                + hint + " If the page you land on does not load, copy its address and paste it under "
                         "“Finish Kite login”."}

    def complete_login(self, params: dict) -> dict:
        """Finish the sign-in Kite redirected back from (``request_token`` and our one-time ``state``)."""
        if str(params.get("status") or "success").lower() != "success":
            raise KiteError("Kite did not complete the login. Press Log in to Kite again.")
        expected, self._state = self._state, None
        if not expected or not hmac.compare_digest(str(params.get("state") or ""), expected):
            raise KiteError("This Kite login link is stale or was not started from this desk. Press Log in to "
                            "Kite again.")
        return self.exchange(request_token_from(str(params.get("request_token") or "")))

    def exchange(self, request_token: str) -> dict:
        """Turn a request token into today's access token and save it."""
        with self._lock:
            if not self.api_key or not self._secret:
                raise KiteError("Save your Kite Connect API key and API secret in Settings → Broker first.")
            checksum = hashlib.sha256((self.api_key + request_token + self._secret).encode()).hexdigest()
            answer = self.request("POST", "/session/token", auth=False,
                                  form={"api_key": self.api_key, "request_token": request_token,
                                        "checksum": checksum})
            if not isinstance(answer, dict) or not answer.get("access_token"):
                raise KiteError("Kite did not return an access token. Press Log in to Kite again.")
            now = _ist_now()
            self.token, self.expires = answer["access_token"], token_expiry(now)
            self.user = {k: answer.get(k) for k in ("user_id", "user_name", "user_shortname", "email",
                                                    "broker", "exchanges", "products")}
            self.error = None
            self._save_session()
            return self.status()

    # -- HTTP --------------------------------------------------------------

    def _pace(self, bucket: str, per_second: float) -> None:
        """Keep under Kite's per-second limits (quote 1/s, historical 3/s, orders 10/s)."""
        gap = 1.0 / per_second
        with self._lock:
            wait = self._last_call.get(bucket, 0.0) + gap - time.monotonic()
            self._last_call[bucket] = time.monotonic() + max(wait, 0.0)
        if wait > 0:
            time.sleep(wait)

    def request(self, method: str, path: str, *, params=None, form=None, json_body=None, auth: bool = True,
                timeout: float = 30.0, raw: bool = False):
        """One Kite call; returns the ``data`` of the answer. Raises KiteError (``needs_login`` on a dead token)."""
        headers = {"X-Kite-Version": "3"}
        if auth:
            if not self.signed_in:
                raise KiteError("Log in to Kite on the Trade tab (one login lasts until 6 AM tomorrow).",
                                needs_login=True)
            headers["Authorization"] = f"token {self.api_key}:{self.token}"
        url = API + path
        if self._http is not None:
            status, body, _ = self._http(method, url, params=params, data=form, json=json_body, headers=headers)
        else:
            import httpx

            try:
                response = httpx.request(method, url, params=params, data=form, json=json_body, headers=headers,
                                         timeout=timeout)
            except httpx.HTTPError as exc:
                raise KiteError(f"Kite did not answer: {exc}") from exc
            status = response.status_code
            if raw and status < 400:
                return response.content
            try:
                body = response.json()
            except ValueError:
                body = {"message": response.text[:300]}
        if raw and status < 400:
            return body
        if not isinstance(body, dict):
            body = {"message": str(body)[:300]}
        if status == 403 or body.get("error_type") == "TokenException":
            self._forget("Zerodha ended this Kite session. Log in to Kite again.")
            raise KiteError(f"Kite session ended: {body.get('message') or 'token refused'}. Log in to Kite again.",
                            needs_login=True)
        if status >= 400 or body.get("status") == "error":
            message = body.get("message") or f"HTTP {status}"
            if body.get("error_type") == "PermissionException" or re.search(r"\bIP\b", str(message)):
                message += (" (Since 1 April 2026 Kite accepts API orders only from the static IP registered on your "
                            "Kite Connect app. See Settings → Broker.)")
            raise KiteError(f"Kite: {message}")
        return body.get("data", body)

    # -- the kite-mcp-server surface ----------------------------------------

    def call(self, tool: str, arguments: dict | None = None, timeout: float = 45.0):
        if not self._connected:
            raise KiteError("not connected to Kite")
        args = dict(arguments or {})
        simple = {"login": self.login, "get_profile": self.profile, "get_margins": self.margins,
                  "get_holdings": self.holdings, "get_orders": self.orders, "get_trades": self.trades,
                  "get_gtts": self.gtts}
        if tool in simple:
            return simple[tool]()
        if tool == "get_positions":
            return self.request("GET", "/portfolio/positions")
        if tool == "get_ltp":
            return self._ltp_raw(args.get("instruments") or [])
        if tool == "get_quotes":
            return self.quotes(args.get("instruments") or [])
        if tool == "search_instruments":
            return self.search(args.get("query", ""), args.get("filter_on", "id"))
        if tool == "get_historical_data":
            return self._historical(args)
        if tool == "place_order":
            return self.place_order(args)
        if tool == "modify_order":
            variety, order_id = args.pop("variety", "regular"), args.pop("order_id", "")
            self._pace("orders", 10)
            return self.request("PUT", f"/orders/{variety}/{order_id}", form=_form(args))
        if tool == "cancel_order":
            return self.cancel_order(args.get("order_id"), args.get("variety", "regular"))
        if tool == "place_gtt_order":
            return self.place_gtt(args)
        if tool == "delete_gtt_order":
            return self.delete_gtt(args.get("trigger_id"))
        if tool == "get_basket_margins":
            return self.basket_margins(args.get("orders") or [], bool(args.get("consider_positions", True)))
        raise KiteError(f'Kite has no "{tool}" here')

    def profile(self):
        return self.request("GET", "/user/profile")

    def margins(self):
        return self.request("GET", "/user/margins")

    def holdings(self) -> list:
        return _list(self.request("GET", "/portfolio/holdings"))

    def positions(self) -> list:
        data = self.request("GET", "/portfolio/positions")
        return list(data.get("net") or []) if isinstance(data, dict) else _list(data)

    def orders(self) -> list:
        return _list(self.request("GET", "/orders"))

    def trades(self) -> list:
        return _list(self.request("GET", "/trades"))

    def _ltp_raw(self, instruments: list[str]) -> dict:
        out: dict = {}
        names = list(dict.fromkeys(instruments))
        for start in range(0, len(names), LTP_BATCH):
            self._pace("quote", 1)
            data = self.request("GET", "/quote/ltp", params=[("i", n) for n in names[start:start + LTP_BATCH]])
            if isinstance(data, dict):
                out.update(data)
        return out

    def ltp(self, instruments: list[str]) -> dict[str, float]:
        return {k: float(v["last_price"]) for k, v in self._ltp_raw(instruments).items()
                if isinstance(v, dict) and isinstance(v.get("last_price"), (int, float)) and v["last_price"] > 0}

    def quotes(self, instruments: list[str]) -> dict[str, dict]:
        out: dict = {}
        names = list(dict.fromkeys(instruments))
        for start in range(0, len(names), QUOTE_BATCH):
            self._pace("quote", 1)
            data = self.request("GET", "/quote", params=[("i", n) for n in names[start:start + QUOTE_BATCH]],
                                timeout=60.0)
            if isinstance(data, dict):
                out.update({k: v for k, v in data.items() if isinstance(v, dict)})
        return out

    def place_order(self, order: dict):
        args = dict(order)
        variety = args.pop("variety", None) or "regular"
        self._pace("orders", 10)
        return self.request("POST", f"/orders/{variety}", form=_form(args))

    def cancel_order(self, order_id: str, variety: str = "regular"):
        self._pace("orders", 10)
        return self.request("DELETE", f"/orders/{variety or 'regular'}/{order_id}")

    def gtts(self) -> list:
        return _list(self.request("GET", "/gtt/triggers"))

    def place_gtt(self, args: dict):
        """A GTT from kite-mcp-server's ``place_gtt_order`` arguments."""
        exchange, symbol = str(args.get("exchange") or "NSE"), str(args.get("tradingsymbol") or "")
        side, product = str(args.get("transaction_type") or ""), str(args.get("product") or "CNC")
        kind = str(args.get("trigger_type") or "")

        def leg(quantity, price):
            return {"exchange": exchange, "tradingsymbol": symbol, "transaction_type": side, "product": product,
                    "order_type": "LIMIT", "quantity": int(float(quantity or 0)), "price": float(price or 0)}
        if kind == "single":
            triggers = [float(args.get("trigger_value") or 0)]
            orders = [leg(args.get("quantity"), args.get("limit_price"))]
        elif kind == "two-leg":
            triggers = [float(args.get("lower_trigger_value") or 0), float(args.get("upper_trigger_value") or 0)]
            orders = [leg(args.get("lower_quantity"), args.get("lower_limit_price")),
                      leg(args.get("upper_quantity"), args.get("upper_limit_price"))]
        else:
            raise KiteError("trigger_type must be single or two-leg")
        condition = {"exchange": exchange, "tradingsymbol": symbol, "trigger_values": triggers,
                     "last_price": float(args.get("last_price") or 0)}
        return self.request("POST", "/gtt/triggers", form={"type": kind, "condition": json.dumps(condition),
                                                           "orders": json.dumps(orders)})

    def delete_gtt(self, trigger_id):
        return self.request("DELETE", f"/gtt/triggers/{int(trigger_id)}")

    def basket_margins(self, orders: list[dict], consider_positions: bool = True):
        """Zerodha's margin for a basket (SPAN, exposure, hedge benefit): ``POST /margins/basket``."""
        return self.request("POST", "/margins/basket", params={"consider_positions": str(consider_positions).lower()},
                            json_body=list(orders))

    # -- candles -------------------------------------------------------------

    def _historical(self, args: dict) -> list:
        params = {"from": args.get("from_date"), "to": args.get("to_date")}
        if args.get("continuous"):
            params["continuous"] = 1
        if args.get("oi"):
            params["oi"] = 1
        self._pace("historical", 3)
        data = self.request("GET", f"/instruments/historical/{int(args['instrument_token'])}/{args['interval']}",
                            params=params, timeout=60.0)
        rows = []
        for candle in (data or {}).get("candles") or [] if isinstance(data, dict) else []:
            row = {"date": candle[0], "open": candle[1], "high": candle[2], "low": candle[3], "close": candle[4],
                   "volume": candle[5] if len(candle) > 5 else 0}
            if len(candle) > 6:
                row["oi"] = candle[6]
            rows.append(row)
        return rows

    def candles(self, instrument: str, interval: str, start, end, continuous: bool = False, oi: bool = False) -> list:
        """Kite's own candles for ``EXCHANGE:SYMBOL`` (needs historical data on your Kite Connect plan)."""
        from . import data

        token = self._tokens.get(instrument)
        if not token:
            row = self.instrument(instrument)
            token = row.get("instrument_token") if row else None
            if not token:
                answer = self._ltp_raw([instrument])
                token = (answer.get(instrument) or {}).get("instrument_token")
            if not token:
                raise KiteError(f"Kite does not know the instrument {instrument}")
            self._tokens[instrument] = int(token)
        spec = data.INTERVALS[interval]
        if spec.get("kite_resample"):
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        rows = self._historical({"instrument_token": token, "interval": spec["kite"],
                                 "from_date": start.strftime("%Y-%m-%d %H:%M:%S"),
                                 "to_date": end.strftime("%Y-%m-%d %H:%M:%S"), "continuous": continuous, "oi": oi})
        return _resampled_rows(rows, spec["minutes"]) if spec.get("kite_resample") else rows

    # -- instruments -----------------------------------------------------------

    def _instrument_rows(self) -> list[dict]:
        """Kite's instrument list (every exchange), downloaded once a day and kept on disk."""
        from .settings import home

        day = _ist_now().strftime("%Y-%m-%d")
        with self._lock:
            if self._instruments and self._instruments[0] == day:
                return self._instruments[1]
        folder = self._instruments_dir or home() / "instruments"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"kite-{day}.csv"
        if path.exists() and path.stat().st_size:
            text = path.read_text(encoding="utf-8")
        else:
            body = self.request("GET", "/instruments", raw=True, timeout=120.0)
            text = body.decode("utf-8") if isinstance(body, bytes) else str(body)
            if "instrument_token" not in text[:200]:
                raise KiteError("Kite's instrument list did not download. Try again in a minute.")
            for old in folder.glob("kite-*.csv"):
                with contextlib.suppress(OSError):
                    old.unlink()
            path.write_text(text, encoding="utf-8")
        rows = [_instrument(r) for r in csv.DictReader(io.StringIO(text))]
        with self._lock:
            self._instruments = (day, rows)
            self._by_id = {r["id"]: r for r in rows}
        return rows

    def instrument(self, name: str) -> dict | None:
        self._instrument_rows()
        return self._by_id.get(str(name).upper())

    def search(self, query: str, filter_on: str = "id") -> list[dict]:
        """Rows as kite-mcp-server's ``search_instruments`` returns them (substring, any case)."""
        q = str(query or "").strip().upper()
        if not q:
            return []
        rows = self._instrument_rows()
        if filter_on == "underlying":
            exchange, _, name = q.rpartition(":")
            exchange = {"NSE": "NFO", "BSE": "BFO"}.get(exchange, exchange)
            return [r for r in rows if r["instrument_type"] in ("FUT", "CE", "PE") and r["name"].upper() == name
                    and (not exchange or r["exchange"] == exchange)]
        key = filter_on if filter_on in ("id", "name", "tradingsymbol", "isin") else "id"
        return [r for r in rows if q in str(r.get(key) or "").upper()]


def _instrument(row: dict) -> dict:
    def number(value, kind=float):
        try:
            return kind(float(value))
        except (TypeError, ValueError):
            return kind(0)
    exchange, symbol = row.get("exchange", ""), row.get("tradingsymbol", "")
    return {"id": f"{exchange}:{symbol}", "instrument_token": number(row.get("instrument_token"), int),
            "exchange_token": number(row.get("exchange_token"), int), "tradingsymbol": symbol,
            "exchange": exchange, "name": (row.get("name") or "").strip() or symbol, "isin": "",
            "last_price": number(row.get("last_price")), "strike": number(row.get("strike")),
            "tick_size": number(row.get("tick_size")), "lot_size": number(row.get("lot_size"), int),
            "instrument_type": row.get("instrument_type", ""), "segment": row.get("segment", ""),
            "expiry_date": row.get("expiry") or "", "freeze_quantity": 0}


def _form(args: dict) -> dict:
    """Order fields as Kite's form body: no empty values, numbers as text."""
    return {k: (str(v).lower() if isinstance(v, bool) else str(v)) for k, v in args.items()
            if v is not None and v != "" and not (isinstance(v, (int, float)) and not isinstance(v, bool) and v == 0
                                                 and k in ("trigger_price", "disclosed_quantity", "validity_ttl",
                                                           "iceberg_legs", "iceberg_quantity", "price"))}


def _list(data) -> list:
    return data if isinstance(data, list) else []


def kite_entry(settings, redirect_uri: str = "") -> dict:
    """What to connect Kite with: your Kite Connect key and secret when both are saved, else the MCP server."""
    keys = settings.get("kite_connect") or {}
    if keys.get("api_key") and keys.get("api_secret"):
        return {"api_key": keys["api_key"], "api_secret": keys["api_secret"], "redirect_uri": redirect_uri}
    servers = settings.get("mcp")["mcpServers"]
    name = settings.get("mcp_server")
    if name not in servers:
        raise ValueError(f'No MCP server named "{name}" in Settings. Found: {", ".join(servers) or "none"}')
    return servers[name]


def resume(kite, settings, redirect_uri: str = "") -> bool:
    """At start-up: reconnect with saved Kite Connect keys, picking up today's login. True when signed in."""
    keys = settings.get("kite_connect") or {}
    if not (keys.get("api_key") and keys.get("api_secret")) or not hasattr(kite, "direct"):
        return False
    if getattr(kite, "name", "kite") != "kite":
        return False
    try:
        status = kite.connect(kite_entry(settings, redirect_uri))
    except KiteError as exc:
        logger.warning("Kite not reconnected at start: %s", exc)
        return False
    return bool(status.get("signed_in"))


class KiteLink:
    """The desk's Kite: direct Kite Connect when your API key and secret are saved, else a Kite MCP server.

    Every part of the desk holds this one object, so the day's login is shared by orders, algos, charts,
    F&O and the margin calculator.
    """

    def __init__(self, mcp: KiteMCP | None = None, direct: KiteDirect | None = None):
        self.mcp = mcp or KiteMCP()
        self.direct = direct or KiteDirect()
        self.active = self.mcp

    @property
    def is_direct(self) -> bool:
        return self.active is self.direct

    def connect(self, entry: dict, timeout: float = 240.0) -> dict:
        """``entry`` holds ``api_key`` and ``api_secret`` (direct) or an ``mcpServers`` entry (MCP)."""
        target = self.direct if entry.get("api_key") or entry.get("api_secret") else self.mcp
        if target is not self.active and self.active.connected:
            self.active.disconnect()
        self.active = target
        return target.connect(entry, timeout=timeout)

    def disconnect(self) -> None:
        self.active.disconnect()

    def status(self) -> dict:
        out = dict(self.active.status())
        out.setdefault("direct", False)
        return out

    def complete_login(self, params: dict) -> dict:
        if not self.is_direct:
            raise KiteError("Kite through an MCP server signs in on the server's own page, not here.")
        return self.direct.complete_login(params)

    def __getattr__(self, attr):
        active = self.__dict__.get("active")
        if active is None:
            raise AttributeError(attr)
        return getattr(active, attr)


__all__ = ["KiteDirect", "KiteLink", "token_expiry", "request_token_from", "TOOLS", "API"]
