"""Angel One, over SmartAPI (https://smartapi.angelone.in/docs).

The desk signs in itself with the client code, the account PIN and a one-time code made from the TOTP
secret Angel One shows when TOTP is enabled for SmartAPI. Sessions end at midnight.
"""

from __future__ import annotations

import json
import uuid

from ..kite import KiteError
from .base import RestBroker, ist_stamp, kite_quote, kite_status, margins_shape
from .instruments import Contract, number, parse_date
from .totp import totp

INSTRUMENTS_URL = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
SECURE = "/rest/secure/angelbroking"
PRODUCTS = {"CNC": "DELIVERY", "MIS": "INTRADAY", "NRML": "CARRYFORWARD"}
KITE_PRODUCTS = {"DELIVERY": "CNC", "INTRADAY": "MIS", "CARRYFORWARD": "NRML", "MARGIN": "NRML"}
ORDER_TYPES = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL": "STOPLOSS_LIMIT", "SL-M": "STOPLOSS_MARKET"}
INTERVALS = {1: "ONE_MINUTE", 3: "THREE_MINUTE", 5: "FIVE_MINUTE", 10: "TEN_MINUTE", 15: "FIFTEEN_MINUTE",
             30: "THIRTY_MINUTE", 60: "ONE_HOUR"}
EXCHANGES = ("NSE", "BSE", "NFO", "BFO", "MCX")


def contracts(raw: list[dict]):
    """Angel One's scrip master (a JSON list) as contracts."""
    for r in raw:
        if not isinstance(r, dict):
            continue
        exchange, kind_raw = str(r.get("exch_seg") or "").upper(), str(r.get("instrumenttype") or "").upper()
        token, symbol = str(r.get("token") or "").strip(), str(r.get("symbol") or "").strip().upper()
        if exchange not in EXCHANGES or not token or not symbol:
            continue
        if kind_raw == "AMXIDX":
            kind = "INDEX"
        elif kind_raw.startswith("FUT"):
            kind = "FUT"
        elif kind_raw.startswith("OPT") and symbol[-2:] in ("CE", "PE"):
            kind = symbol[-2:]
        elif not kind_raw and exchange in ("NSE", "BSE"):
            kind = "EQ"
        else:
            continue
        derivative = kind in ("FUT", "CE", "PE")
        yield Contract(
            exchange=exchange, kind=kind, key=f"{exchange}|{token}", symbol=symbol,
            underlying=str(r.get("name") or "").upper() if derivative else "",
            name=str(r.get("name") or ""), expiry=parse_date(r.get("expiry")) if derivative else None,
            # Angel One gives strikes and ticks in paise.
            strike=number(r.get("strike")) / 100 if derivative else 0.0,
            lot_size=int(number(r.get("lotsize"), 1)) or 1, tick_size=(number(r.get("tick_size"), 5) / 100) or 0.05,
            native={"exchange": exchange, "token": token, "symbol": symbol})


class AngelOne(RestBroker):
    name, label = "angelone", "Angel One"
    base_url = "https://apiconnect.angelone.in"
    login_kind = "totp"
    fields = (
        {"key": "api_key", "label": "SmartAPI key", "required": True, "secret": False,
         "hint": "Create an app at smartapi.angelone.in (Trading APIs)."},
        {"key": "client_code", "label": "Client code", "required": True, "secret": False},
        {"key": "pin", "label": "Account PIN", "required": True, "secret": True},
        {"key": "totp_secret", "label": "TOTP secret", "required": True, "secret": True,
         "hint": "The text under the QR code when you enable TOTP at smartapi.angelone.in/enable-totp."},
        {"key": "public_ip", "label": "Static IP registered on the app", "required": False, "secret": False},
    )
    minutes = (1, 3, 5, 10, 15, 30, 60)
    candle_days, day_candle_days = 28, 1900

    def headers(self) -> dict:
        mac = ":".join(f"{(uuid.getnode() >> s) & 0xFF:02x}" for s in range(40, -1, -8))
        out = {**super().headers(), "X-UserType": "USER", "X-SourceID": "WEB", "X-ClientLocalIP": "127.0.0.1",
               "X-ClientPublicIP": self.creds.get("public_ip") or "127.0.0.1", "X-MACAddress": mac,
               "X-PrivateKey": self.creds.get("api_key", "")}
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        return out

    def session_expired(self, status: int, body) -> bool:
        return isinstance(body, dict) and str(body.get("errorcode") or body.get("errorCode") or "") in (
            "AG8001", "AG8002", "AG8003", "AB1010", "AB8050", "AB8051")

    def failed(self, body) -> bool:
        return isinstance(body, dict) and body.get("status") is False

    def login(self) -> dict:
        try:
            code = totp(self.creds["totp_secret"])
        except (ValueError, TypeError) as exc:
            raise KiteError("The Angel One TOTP secret is not valid base32 text.") from exc
        body = self.request("POST", "/rest/auth/angelbroking/user/v1/loginByPassword", auth=False, json_body={
            "clientcode": self.creds["client_code"], "password": self.creds["pin"], "totp": code})
        token = ((body or {}).get("data") or {}).get("jwtToken")
        if not token:
            raise KiteError(f"Angel One refused the sign-in: {self.message(body)}")
        self._save_token(token.removeprefix("Bearer "))
        return {"url": None, "message": f"Signed in to Angel One as {self.creds['client_code']}."}

    def _load_instruments(self):
        return list(contracts(json.loads(self.download(INSTRUMENTS_URL))))

    def _data(self, method: str, path: str, body=None):
        answer = self.request(method, SECURE + path, json_body=body)
        return answer.get("data") if isinstance(answer, dict) else None

    def _names(self, r: dict) -> tuple[str, str]:
        exchange = str(r.get("exchange") or "").upper()
        row = self.by_key(f"{exchange}|{r.get('symboltoken')}")
        return self.kite_names(row, str(r.get("tradingsymbol") or ""), exchange)

    # -- account -------------------------------------------------------------

    def _native_profile(self) -> dict:
        data = self._data("GET", "/user/v1/getProfile") or {}
        return {"user_id": data.get("clientcode"), "user_name": data.get("name"), "email": data.get("email"),
                "broker": "ANGEL ONE"}

    def _native_margins(self) -> dict:
        data = self._data("GET", "/user/v1/getRMS") or {}
        return margins_shape(number(data.get("availablecash") or data.get("net")), number(data.get("utiliseddebits")))

    def _native_holdings(self) -> list:
        out = []
        for r in self._data("GET", "/portfolio/v1/getHolding") or []:
            exchange, symbol = self._names(r)
            t1 = int(number(r.get("t1quantity")))
            out.append({"tradingsymbol": symbol, "exchange": exchange, "isin": r.get("isin"), "product": "CNC",
                        "quantity": max(int(number(r.get("quantity"))) - t1, 0), "t1_quantity": t1,
                        "average_price": number(r.get("averageprice")), "last_price": number(r.get("ltp")),
                        "close_price": number(r.get("close")), "pnl": number(r.get("profitandloss"))})
        return out

    def _native_positions(self) -> list:
        out = []
        for r in self._data("GET", "/order/v1/getPosition") or []:
            exchange, symbol = self._names(r)
            pnl = r.get("pnl")
            out.append({"tradingsymbol": symbol, "exchange": exchange, "quantity": int(number(r.get("netqty"))),
                        "average_price": number(r.get("avgnetprice") or r.get("netprice")),
                        "product": KITE_PRODUCTS.get(str(r.get("producttype")).upper(), r.get("producttype")),
                        "last_price": number(r.get("ltp")), "multiplier": 1.0,
                        "pnl": number(pnl) if pnl not in (None, "") else number(r.get("unrealised")) + number(r.get("realised"))})
        return out

    def _native_orders(self) -> list:
        out = []
        for r in self._data("GET", "/order/v1/getOrderBook") or []:
            exchange, symbol = self._names(r)
            order_type = {v: k for k, v in ORDER_TYPES.items()}.get(str(r.get("ordertype")), r.get("ordertype"))
            out.append({"order_id": str(r.get("orderid")), "tradingsymbol": symbol, "exchange": exchange,
                        "transaction_type": r.get("transactiontype"), "quantity": int(number(r.get("quantity"))),
                        "filled_quantity": int(number(r.get("filledshares"))), "price": number(r.get("price")),
                        "average_price": number(r.get("averageprice")), "trigger_price": number(r.get("triggerprice")),
                        "status": kite_status(r.get("status") or r.get("orderstatus")), "status_message": r.get("text"),
                        "product": KITE_PRODUCTS.get(str(r.get("producttype")).upper(), r.get("producttype")),
                        "order_type": order_type, "tag": r.get("ordertag"), "order_timestamp": r.get("updatetime")})
        return out

    def _native_trades(self) -> list:
        out = []
        for r in self._data("GET", "/order/v1/getTradeBook") or []:
            exchange, symbol = self._names(r)
            out.append({"trade_id": str(r.get("fillid")), "order_id": str(r.get("orderid")), "tradingsymbol": symbol,
                        "exchange": exchange, "transaction_type": r.get("transactiontype"),
                        "quantity": int(number(r.get("fillsize"))), "average_price": number(r.get("fillprice"))})
        return out

    # -- market data ---------------------------------------------------------

    def _native_quotes(self, rows: dict[str, dict]) -> dict[str, dict]:
        by_native = {(row["_native"]["exchange"], row["_native"]["token"]): name for name, row in rows.items()}
        keys, out = list(by_native), {}
        for start in range(0, len(keys), 50):
            tokens: dict[str, list[str]] = {}
            for exchange, token in keys[start:start + 50]:
                tokens.setdefault(exchange, []).append(token)
            data = self._data("POST", "/market/v1/quote/", {"mode": "FULL", "exchangeTokens": tokens}) or {}
            for q in data.get("fetched") or []:
                name = by_native.get((str(q.get("exchange")), str(q.get("symbolToken"))))
                if not name:
                    continue
                depth = q.get("depth") or {}
                out[name] = kite_quote(
                    rows[name], q.get("ltp"), q.get("open"), q.get("high"), q.get("low"), q.get("close"),
                    q.get("tradeVolume"), q.get("opnInterest"), q.get("avgPrice"), q.get("totBuyQuan"),
                    q.get("totSellQuan"), q.get("lowerCircuit"), q.get("upperCircuit"), q.get("exchTradeTime"),
                    [{"price": d.get("price"), "quantity": d.get("quantity")} for d in depth.get("buy") or []],
                    [{"price": d.get("price"), "quantity": d.get("quantity")} for d in depth.get("sell") or []])
        return out

    def _native_candles(self, row: dict, minutes: int | None, start, end, oi: bool) -> list:
        native = row["_native"]
        data = self._data("POST", "/historical/v1/getCandleData", {
            "exchange": native["exchange"], "symboltoken": native["token"],
            "interval": "ONE_DAY" if minutes is None else INTERVALS[minutes],
            "fromdate": f"{start:%Y-%m-%d %H:%M}", "todate": f"{end:%Y-%m-%d %H:%M}"}) or []
        out = []
        for c in data:
            if isinstance(c, list) and len(c) >= 5:
                out.append({"date": ist_stamp(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                            "close": float(c[4]), "volume": float(c[5] if len(c) > 5 and c[5] is not None else 0)})
        return out

    # -- orders --------------------------------------------------------------

    def _native_place(self, row: dict, order: dict) -> dict:
        native = row["_native"]
        order_type = ORDER_TYPES.get(str(order.get("order_type") or "LIMIT"), "LIMIT")
        variety = "AMO" if order.get("variety") == "amo" else ("STOPLOSS" if order_type.startswith("STOPLOSS") else "NORMAL")
        body = {"variety": variety, "tradingsymbol": native["symbol"], "symboltoken": native["token"],
                "transactiontype": order["transaction_type"], "exchange": native["exchange"], "ordertype": order_type,
                "producttype": PRODUCTS.get(str(order.get("product")), "DELIVERY"), "duration": order.get("validity") or "DAY",
                "price": str(order.get("price") or 0), "triggerprice": str(order.get("trigger_price") or 0),
                "quantity": str(int(order["quantity"])), "ordertag": str(order.get("tag") or "")[:20]}
        data = self._data("POST", "/order/v1/placeOrder", body) or {}
        return {"order_id": str(data.get("orderid")) if data.get("orderid") else None, "broker": data}

    def _native_cancel(self, order_id: str) -> dict:
        return {"order_id": order_id, "broker": self._data("POST", "/order/v1/cancelOrder",
                                                          {"variety": "NORMAL", "orderid": order_id})}
