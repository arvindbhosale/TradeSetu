"""Fyers, over API v3 (https://myapi.fyers.in/docsv3).

Sign-in is OAuth: Fyers redirects back to the desk with an auth code, which the desk swaps for the day's
access token using the app's secret.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from urllib.parse import urlencode

from ..kite import KiteError
from .base import RestBroker, ist_stamp, kite_quote, margins_shape
from .instruments import Contract, number, parse_date

FILES = ("NSE_CM", "BSE_CM", "NSE_FO", "BSE_FO", "MCX_COM")
INSTRUMENTS_URL = "https://public.fyers.in/sym_details/{}.csv"
DATA_URL = "https://api-t1.fyers.in/data"
PRODUCTS = {"CNC": "CNC", "MIS": "INTRADAY", "NRML": "MARGIN"}
KITE_PRODUCTS = {"CNC": "CNC", "INTRADAY": "MIS", "MARGIN": "NRML", "MTF": "MTF", "CO": "CO", "BO": "BO"}
ORDER_TYPES = {"LIMIT": 1, "MARKET": 2, "SL-M": 3, "SL": 4}
STATUS = {1: "CANCELLED", 2: "COMPLETE", 4: "OPEN", 5: "REJECTED", 6: "OPEN", 7: "CANCELLED"}
EXPIRED_CODES = {-8, -15, -16, -17, -300}


def contracts(text: str, file: str):
    """One of Fyers' symbol master CSVs (no header row) as contracts.

    Columns: 0 FyToken, 1 symbol details, 2 instrument type, 3 lot size, 4 tick size, 5 ISIN, 6 session,
    7 last update, 8 expiry (epoch), 9 ticker (``NSE:SBIN-EQ``), 10 exchange, 11 segment, 12 scrip code,
    13 underlying, 14 underlying scrip code, 15 strike, 16 option type.
    """
    cash = file.endswith("_CM")
    for c in csv.reader(io.StringIO(text.lstrip("﻿"))):
        if len(c) < 17 or ":" not in c[9]:
            continue
        ticker = c[9].strip()
        prefix, _, symbol = ticker.partition(":")
        prefix = prefix.upper()
        if cash:
            exchange = prefix
            kind = "INDEX" if symbol.upper().endswith("-INDEX") else "EQ"
        else:
            exchange = {"NSE": "NFO", "BSE": "BFO", "MCX": "MCX"}.get(prefix, "")
            option = c[16].strip().upper()
            kind = option if option in ("CE", "PE") else ("FUT" if symbol.upper().endswith("FUT") else "")
        if exchange not in ("NSE", "BSE", "NFO", "BFO", "MCX") or not kind:
            continue
        derivative = kind in ("FUT", "CE", "PE")
        name = c[1].strip()
        yield Contract(
            exchange=exchange, kind=kind, key=ticker,
            symbol=(name if kind == "INDEX" else symbol) if not derivative else "",
            underlying=c[13].strip().upper() if derivative else "", name=name if kind != "INDEX" else symbol,
            expiry=parse_date(c[8]) if derivative else None, strike=number(c[15]) if derivative else 0.0,
            lot_size=int(number(c[3], 1)) or 1, tick_size=number(c[4], 0.05) or 0.05, isin=c[5].strip())


class Fyers(RestBroker):
    name, label = "fyers", "Fyers"
    base_url = "https://api-t1.fyers.in/api/v3"
    login_kind = "oauth"
    fields = (
        {"key": "app_id", "label": "App ID", "required": True, "secret": False,
         "hint": "From myapi.fyers.in/dashboard, for example ABCD1234-100."},
        {"key": "secret_key", "label": "Secret key", "required": True, "secret": True},
        {"key": "redirect_uri", "label": "Redirect URL", "required": False, "secret": False,
         "hint": "Leave empty to use this desk's own address; register the same URL on your Fyers app."},
    )
    minutes = (1, 2, 3, 5, 10, 15, 20, 30, 60)
    candle_days, day_candle_days = 95, 360

    def headers(self) -> dict:
        out = super().headers()
        if self.token:
            out["Authorization"] = f"{self.creds.get('app_id', '')}:{self.token}"
        return out

    def failed(self, body) -> bool:
        return isinstance(body, dict) and str(body.get("s", "")).lower() == "error"

    def session_expired(self, status: int, body) -> bool:
        return isinstance(body, dict) and body.get("s") == "error" and body.get("code") in EXPIRED_CODES

    # -- sign-in -----------------------------------------------------------

    def login(self) -> dict:
        query = urlencode({"client_id": self.creds["app_id"], "redirect_uri": self.creds.get("redirect_uri", ""),
                           "response_type": "code", "state": self.new_state()})
        return {"url": f"{self.base_url}/generate-authcode?{query}",
                "message": "Open the link and sign in to Fyers. You will come back here signed in."}

    def complete_login(self, params: dict) -> dict:
        self.check_state(params)
        code = params.get("auth_code") or params.get("code")
        if not code or str(params.get("s", "ok")).lower() != "ok":
            raise KiteError("Fyers did not send a sign-in code back.")
        app_hash = hashlib.sha256(f"{self.creds['app_id']}:{self.creds['secret_key']}".encode()).hexdigest()
        body = self.request("POST", "/validate-authcode", auth=False,
                            json_body={"grant_type": "authorization_code", "appIdHash": app_hash, "code": code})
        token = body.get("access_token") if isinstance(body, dict) else None
        if not token:
            raise KiteError(f"Fyers did not return an access token: {self.message(body)}")
        self._save_token(token)
        return {}

    def _load_instruments(self):
        out = []
        for file in FILES:
            out.extend(contracts(self.download(INSTRUMENTS_URL.format(file)).decode("utf-8", "replace"), file))
        return out

    def _names(self, ticker) -> tuple[str, str]:
        row = self.by_key(ticker)
        prefix, _, symbol = str(ticker or "").partition(":")
        return self.kite_names(row, symbol, prefix)

    # -- account -------------------------------------------------------------

    def _native_profile(self) -> dict:
        data = (self.request("GET", "/profile") or {}).get("data") or {}
        return {"user_id": data.get("fy_id"), "user_name": data.get("name"), "email": data.get("email_id"),
                "broker": "FYERS"}

    def _native_margins(self) -> dict:
        rows = (self.request("GET", "/funds") or {}).get("fund_limit") or []
        by_title = {str(r.get("title", "")).lower(): r for r in rows if isinstance(r, dict)}
        available, used = by_title.get("available balance") or {}, by_title.get("utilized amount") or {}
        return margins_shape(number(available.get("equityAmount")), number(used.get("equityAmount")),
                             number(available.get("commodityAmount")), number(used.get("commodityAmount")))

    def _native_holdings(self) -> list:
        merged: dict[str, dict] = {}
        for r in (self.request("GET", "/holdings") or {}).get("holdings") or []:
            exchange, symbol = self._names(r.get("symbol"))
            item = merged.setdefault(f"{exchange}:{symbol}", {
                "tradingsymbol": symbol, "exchange": exchange, "isin": r.get("isin"), "product": "CNC", "quantity": 0,
                "t1_quantity": 0, "average_price": number(r.get("costPrice")), "last_price": number(r.get("ltp")), "pnl": 0.0})
            quantity = int(number(r.get("remainingQuantity", r.get("quantity"))))
            item["t1_quantity" if str(r.get("holdingType")).upper() == "T1" else "quantity"] += quantity
            item["pnl"] += number(r.get("pl"))
        return list(merged.values())

    def _native_positions(self) -> list:
        out = []
        for r in (self.request("GET", "/positions") or {}).get("netPositions") or []:
            exchange, symbol = self._names(r.get("symbol"))
            out.append({"tradingsymbol": symbol, "exchange": exchange, "quantity": int(number(r.get("netQty"))),
                        "average_price": number(r.get("netAvg")), "last_price": number(r.get("ltp")),
                        "product": KITE_PRODUCTS.get(str(r.get("productType")), r.get("productType")),
                        "pnl": number(r.get("pl")), "multiplier": 1.0})
        return out

    def _native_orders(self) -> list:
        out = []
        for r in (self.request("GET", "/orders") or {}).get("orderBook") or []:
            exchange, symbol = self._names(r.get("symbol"))
            kind = {v: k for k, v in ORDER_TYPES.items()}.get(int(number(r.get("type"))), "LIMIT")
            out.append({"order_id": str(r.get("id")), "tradingsymbol": symbol, "exchange": exchange,
                        "transaction_type": "BUY" if int(number(r.get("side"))) == 1 else "SELL",
                        "quantity": int(number(r.get("qty"))), "filled_quantity": int(number(r.get("filledQty"))),
                        "price": number(r.get("limitPrice")), "average_price": number(r.get("tradedPrice")),
                        "trigger_price": number(r.get("stopPrice")), "order_type": kind,
                        "status": STATUS.get(int(number(r.get("status"))), "OPEN"), "status_message": r.get("message"),
                        "product": KITE_PRODUCTS.get(str(r.get("productType")), r.get("productType")),
                        "tag": r.get("orderTag"), "order_timestamp": r.get("orderDateTime")})
        return out

    def _native_trades(self) -> list:
        out = []
        for r in (self.request("GET", "/tradebook") or {}).get("tradeBook") or []:
            exchange, symbol = self._names(r.get("symbol"))
            out.append({"trade_id": str(r.get("tradeNumber")), "order_id": str(r.get("orderNumber")),
                        "tradingsymbol": symbol, "exchange": exchange,
                        "transaction_type": "BUY" if int(number(r.get("side"))) == 1 else "SELL",
                        "quantity": int(number(r.get("tradedQty"))), "average_price": number(r.get("tradePrice"))})
        return out

    # -- market data ---------------------------------------------------------

    def _native_quotes(self, rows: dict[str, dict]) -> dict[str, dict]:
        by_ticker = {row["instrument_token"]: name for name, row in rows.items()}
        tickers, out = list(by_ticker), {}
        for start in range(0, len(tickers), 50):
            body = self.request("GET", "/quotes", params={"symbols": ",".join(tickers[start:start + 50])}, base=DATA_URL)
            for item in (body or {}).get("d") or []:
                v = item.get("v") or {}
                name = by_ticker.get(str(item.get("n") or v.get("symbol") or ""))
                if not name or item.get("s") not in (None, "ok"):
                    continue
                out[name] = kite_quote(rows[name], v.get("lp"), v.get("open_price"), v.get("high_price"),
                                       v.get("low_price"), v.get("prev_close_price"), v.get("volume"), v.get("oi"),
                                       v.get("atp"), None, None, v.get("lower_ckt"), v.get("upper_ckt"), v.get("tt"),
                                       [{"price": v.get("bid"), "quantity": None}] if v.get("bid") else None,
                                       [{"price": v.get("ask"), "quantity": None}] if v.get("ask") else None)
        return out

    def _native_candles(self, row: dict, minutes: int | None, start, end, oi: bool) -> list:
        params = {"symbol": row["instrument_token"], "resolution": "D" if minutes is None else str(minutes),
                  "date_format": "1", "range_from": f"{start:%Y-%m-%d}", "range_to": f"{end:%Y-%m-%d}", "cont_flag": "1"}
        if oi:
            params["oi_flag"] = "1"
        body = self.request("GET", "/history", params=params, base=DATA_URL) or {}
        out = []
        for c in body.get("candles") or []:
            if isinstance(c, list) and len(c) >= 5:
                item = {"date": ist_stamp(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                        "close": float(c[4]), "volume": float(c[5] if len(c) > 5 and c[5] is not None else 0)}
                if oi and len(c) > 6 and c[6] is not None:
                    item["oi"] = float(c[6])
                out.append(item)
        return out

    # -- orders --------------------------------------------------------------

    def _native_place(self, row: dict, order: dict) -> dict:
        kind = ORDER_TYPES.get(str(order.get("order_type") or "LIMIT"), 1)
        body = {"symbol": row["instrument_token"], "qty": int(order["quantity"]), "type": kind,
                "side": 1 if order["transaction_type"] == "BUY" else -1,
                "productType": PRODUCTS.get(str(order.get("product")), "CNC"),
                "limitPrice": float(order.get("price") or 0) if kind in (1, 4) else 0,
                "stopPrice": float(order.get("trigger_price") or 0) if kind in (3, 4) else 0,
                "validity": order.get("validity") or "DAY", "disclosedQty": 0,
                "offlineOrder": order.get("variety") == "amo",
                "orderTag": re.sub(r"[^A-Za-z0-9]", "", str(order.get("tag") or ""))[:30]}
        answer = self.request("POST", "/orders/sync", json_body=body) or {}
        return {"order_id": str(answer.get("id")) if answer.get("id") else None, "broker": answer}

    def _native_cancel(self, order_id: str) -> dict:
        return {"order_id": order_id, "broker": self.request("DELETE", "/orders/sync", json_body={"id": order_id})}
