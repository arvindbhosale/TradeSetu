"""Upstox, over its v2/v3 REST API (https://upstox.com/developer/api-documentation).

Sign-in is OAuth: the desk opens Upstox's login page, Upstox redirects back to the desk with a code, and the
desk swaps it for the day's access token. Tokens end at 03:30 IST the next morning.
"""

from __future__ import annotations

import gzip
import json
from urllib.parse import quote, urlencode

from ..kite import KiteError
from .base import RestBroker, ist_stamp, kite_quote, kite_status, margins_shape
from .instruments import Contract, number, parse_date

INSTRUMENTS_URL = "https://assets.upstox.com/market-quote/instruments/exchange/complete.json.gz"
ORDER_HOST = "https://api-hft.upstox.com"
SEGMENTS = {"NSE_EQ": "NSE", "BSE_EQ": "BSE", "NSE_FO": "NFO", "BSE_FO": "BFO", "MCX_FO": "MCX", "NCD_FO": "CDS",
            "BCD_FO": "BCD", "NSE_INDEX": "NSE", "BSE_INDEX": "BSE"}
PRODUCTS = {"CNC": "D", "NRML": "D", "MIS": "I"}
KITE_PRODUCTS = {"D": "CNC", "I": "MIS", "CO": "CO", "MTF": "MTF"}


def contracts(raw: list[dict]):
    """Upstox's instrument file (a JSON list) as contracts."""
    rows = sorted((r for r in raw if isinstance(r, dict)), key=lambda r: r.get("instrument_type") != "EQ")
    for r in rows:
        segment, kind = str(r.get("segment") or ""), str(r.get("instrument_type") or "").upper()
        exchange = SEGMENTS.get(segment)
        if not exchange or not r.get("instrument_key"):
            continue
        if segment.endswith("_INDEX"):
            kind = "INDEX"
        elif segment.endswith("_EQ"):
            kind = "EQ"
        elif kind not in ("FUT", "CE", "PE"):
            continue
        yield Contract(
            exchange=exchange, kind=kind, key=str(r["instrument_key"]), symbol=str(r.get("trading_symbol") or ""),
            underlying=str(r.get("underlying_symbol") or "").upper(), name=str(r.get("name") or ""),
            expiry=parse_date(r.get("expiry")), strike=number(r.get("strike_price")),
            lot_size=int(number(r.get("lot_size"), 1)) or 1,
            # Upstox gives the tick in paise.
            tick_size=(number(r.get("tick_size"), 5.0) / 100) or 0.05, isin=str(r.get("isin") or ""),
            freeze_quantity=int(number(r.get("freeze_quantity"))),
            weekly=r.get("weekly") if isinstance(r.get("weekly"), bool) else None)


class Upstox(RestBroker):
    name, label = "upstox", "Upstox"
    base_url = "https://api.upstox.com"
    login_kind = "oauth"
    fields = (
        {"key": "api_key", "label": "API key", "required": True, "secret": False,
         "hint": "From the Upstox developer apps page (account.upstox.com/developer/apps)."},
        {"key": "api_secret", "label": "API secret", "required": True, "secret": True},
        {"key": "redirect_uri", "label": "Redirect URL", "required": False, "secret": False,
         "hint": "Leave empty to use this desk's own address; register the same URL on your Upstox app."},
    )
    minutes = (1, 3, 5, 15, 30, 60)
    candle_days, day_candle_days = 28, 3000

    def headers(self) -> dict:
        out = super().headers()
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        return out

    # -- sign-in -----------------------------------------------------------

    def login(self) -> dict:
        query = urlencode({"response_type": "code", "client_id": self.creds["api_key"],
                           "redirect_uri": self.creds.get("redirect_uri", ""), "state": self.new_state()})
        url = f"{self.base_url}/v2/login/authorization/dialog?{query}"
        return {"url": url, "message": "Open the link and sign in to Upstox. You will come back here signed in."}

    def complete_login(self, params: dict) -> dict:
        self.check_state(params)
        code = params.get("code")
        if not code:
            raise KiteError("Upstox did not send a sign-in code back.")
        body = self.request("POST", "/v2/login/authorization/token", auth=False, data={
            "code": code, "client_id": self.creds["api_key"], "client_secret": self.creds["api_secret"],
            "redirect_uri": self.creds.get("redirect_uri", ""), "grant_type": "authorization_code"},
            headers={"Content-Type": "application/x-www-form-urlencoded"})
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise KiteError("Upstox did not return an access token.")
        self._save_token(token)
        return {"user_name": body.get("user_name"), "user_id": body.get("user_id")}

    # -- instruments ---------------------------------------------------------

    def _load_instruments(self):
        raw = self.download(INSTRUMENTS_URL)
        with_gzip = raw[:2] == b"\x1f\x8b"
        return list(contracts(json.loads(gzip.decompress(raw) if with_gzip else raw)))

    # -- account -------------------------------------------------------------

    def _data(self, path: str, **kw):
        body = self.request("GET", path, **kw)
        return body.get("data") if isinstance(body, dict) else None

    def _native_profile(self) -> dict:
        data = self._data("/v2/user/profile") or {}
        return {"user_id": data.get("user_id"), "user_name": data.get("user_name"), "email": data.get("email"),
                "broker": "UPSTOX"}

    def _native_margins(self) -> dict:
        data = self._data("/v2/user/get-funds-and-margin") or {}
        equity, commodity = data.get("equity") or {}, data.get("commodity") or {}
        return margins_shape(equity.get("available_margin"), equity.get("used_margin"),
                             commodity.get("available_margin") if commodity else None, commodity.get("used_margin"))

    def _names(self, r: dict) -> tuple[str, str]:
        row = self.by_key(r.get("instrument_token"))
        return self.kite_names(row, str(r.get("tradingsymbol") or r.get("trading_symbol") or ""), str(r.get("exchange") or ""))

    def _native_holdings(self) -> list:
        out = []
        for r in self._data("/v2/portfolio/long-term-holdings") or []:
            exchange, symbol = self._names(r)
            out.append({"tradingsymbol": symbol, "exchange": exchange, "isin": r.get("isin"), "product": "CNC",
                        "quantity": int(number(r.get("quantity"))), "t1_quantity": int(number(r.get("t1_quantity"))),
                        "average_price": number(r.get("average_price")), "last_price": number(r.get("last_price")),
                        "close_price": number(r.get("close_price")), "pnl": number(r.get("pnl"))})
        return out

    def _native_positions(self) -> list:
        out = []
        for r in self._data("/v2/portfolio/short-term-positions") or []:
            exchange, symbol = self._names(r)
            out.append({"tradingsymbol": symbol, "exchange": exchange,
                        "product": KITE_PRODUCTS.get(str(r.get("product")), str(r.get("product") or "")),
                        "quantity": int(number(r.get("quantity"))), "average_price": number(r.get("average_price")),
                        "last_price": number(r.get("last_price")), "pnl": number(r.get("pnl")),
                        "multiplier": number(r.get("multiplier"), 1.0)})
        return out

    def _native_orders(self) -> list:
        out = []
        for r in self._data("/v2/order/retrieve-all") or []:
            exchange, symbol = self._names(r)
            out.append({"order_id": str(r.get("order_id")), "tradingsymbol": symbol, "exchange": exchange,
                        "transaction_type": r.get("transaction_type"), "quantity": int(number(r.get("quantity"))),
                        "filled_quantity": int(number(r.get("filled_quantity"))), "price": number(r.get("price")),
                        "average_price": number(r.get("average_price")), "trigger_price": number(r.get("trigger_price")),
                        "status": kite_status(r.get("status")), "status_message": r.get("status_message"),
                        "product": KITE_PRODUCTS.get(str(r.get("product")), r.get("product")),
                        "order_type": r.get("order_type"), "tag": r.get("tag"), "order_timestamp": r.get("order_timestamp")})
        return out

    def _native_trades(self) -> list:
        out = []
        for r in self._data("/v2/order/trades/get-trades-for-day") or []:
            exchange, symbol = self._names(r)
            out.append({"trade_id": str(r.get("trade_id")), "order_id": str(r.get("order_id")), "tradingsymbol": symbol,
                        "exchange": exchange, "transaction_type": r.get("transaction_type"),
                        "quantity": int(number(r.get("quantity"))), "average_price": number(r.get("average_price"))})
        return out

    # -- market data ---------------------------------------------------------

    def _native_quotes(self, rows: dict[str, dict]) -> dict[str, dict]:
        by_key = {row["instrument_token"]: name for name, row in rows.items()}
        keys, out = list(by_key), {}
        for start in range(0, len(keys), 500):
            data = self._data("/v2/market-quote/quotes", params={"instrument_key": ",".join(keys[start:start + 500])}) or {}
            for q in data.values():
                name = by_key.get(str(q.get("instrument_token") or ""))
                if not name:
                    continue
                last, change, ohlc = q.get("last_price"), q.get("net_change"), q.get("ohlc") or {}
                previous = (last - change) if isinstance(last, (int, float)) and isinstance(change, (int, float)) else ohlc.get("close")
                depth = q.get("depth") or {}
                out[name] = kite_quote(
                    rows[name], last, ohlc.get("open"), ohlc.get("high"), ohlc.get("low"), previous, q.get("volume"),
                    q.get("oi"), q.get("average_price"), q.get("total_buy_quantity"), q.get("total_sell_quantity"),
                    q.get("lower_circuit_limit"), q.get("upper_circuit_limit"), q.get("last_trade_time"),
                    [{"price": d.get("price"), "quantity": d.get("quantity")} for d in depth.get("buy") or []],
                    [{"price": d.get("price"), "quantity": d.get("quantity")} for d in depth.get("sell") or []])
        return out

    def _native_candles(self, row: dict, minutes: int | None, start, end, oi: bool) -> list:
        from .base import ist_now

        key = quote(row["instrument_token"], safe="")
        unit, size = ("days", 1) if minutes is None else ("minutes", minutes)
        candles = []
        today = ist_now().date()
        if start.date() < today:
            to_day = min(end.date(), today)
            data = self._data(f"/v3/historical-candle/{key}/{unit}/{size}/{to_day:%Y-%m-%d}/{start:%Y-%m-%d}") or {}
            candles.extend(data.get("candles") or [])
        if end.date() >= today:
            data = self._data(f"/v3/historical-candle/intraday/{key}/{unit}/{size}") or {}
            candles.extend(data.get("candles") or [])
        out = []
        for c in candles:
            if not isinstance(c, list) or len(c) < 5:
                continue
            item = {"date": ist_stamp(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                    "close": float(c[4]), "volume": float(c[5] if len(c) > 5 and c[5] is not None else 0)}
            if oi and len(c) > 6 and c[6] is not None:
                item["oi"] = float(c[6])
            if start.strftime("%Y-%m-%dT%H:%M:%S") <= item["date"][:19] <= end.strftime("%Y-%m-%dT%H:%M:%S"):
                out.append(item)
        return out

    # -- orders --------------------------------------------------------------

    def _native_place(self, row: dict, order: dict) -> dict:
        product = PRODUCTS.get(str(order.get("product")), "D")
        body = {"quantity": int(order["quantity"]), "product": product, "validity": order.get("validity") or "DAY",
                "price": float(order.get("price") or 0), "tag": str(order.get("tag") or "")[:20],
                "instrument_token": row["instrument_token"], "order_type": order.get("order_type") or "LIMIT",
                "transaction_type": order["transaction_type"], "disclosed_quantity": 0,
                "trigger_price": float(order.get("trigger_price") or 0), "is_amo": order.get("variety") == "amo",
                "slice": False}
        answer = self.request("POST", "/v3/order/place", json_body=body, base=ORDER_HOST)
        ids = ((answer or {}).get("data") or {}).get("order_ids") or []
        return {"order_id": str(ids[0]) if ids else None, "broker": answer}

    def _native_cancel(self, order_id: str) -> dict:
        answer = self.request("DELETE", "/v3/order/cancel", params={"order_id": order_id}, base=ORDER_HOST)
        return {"order_id": order_id, "broker": answer}
