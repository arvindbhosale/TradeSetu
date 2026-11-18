"""Groww, over the Groww Trade API (https://groww.in/trade-api/docs).

The Trade API is a paid subscription on the Groww account. The desk signs in with either a daily access
token pasted from Groww's Trade API page, or an API key with its secret (approve the key once a day on
Groww) or with a TOTP secret.
"""

from __future__ import annotations

import csv
import hashlib
import io
import time
import uuid

from ..kite import KiteError
from .base import RestBroker, ist_stamp, kite_quote, kite_status, margins_shape
from .instruments import Contract, number, parse_date
from .totp import totp

INSTRUMENTS_URL = "https://growwapi-assets.groww.in/instruments/instrument.csv"
SEGMENT_OF = {"NSE": "CASH", "BSE": "CASH", "NFO": "FNO", "BFO": "FNO", "MCX": "COMMODITY"}
ORDER_TYPES = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL": "SL", "SL-M": "SL_M"}
STATUS = {"EXECUTED": "COMPLETE", "COMPLETED": "COMPLETE", "DELIVERY_AWAITED": "COMPLETE", "REJECTED": "REJECTED",
          "FAILED": "REJECTED", "CANCELLED": "CANCELLED", "TRIGGER_PENDING": "TRIGGER PENDING"}


def contracts(text: str):
    """Groww's instrument CSV as contracts."""
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        exchange, segment = str(r.get("exchange") or "").upper(), str(r.get("segment") or "").upper()
        kind_raw, trading = str(r.get("instrument_type") or "").upper(), str(r.get("trading_symbol") or "").strip()
        if not trading or exchange not in ("NSE", "BSE", "MCX"):
            continue
        if kind_raw in ("IDX", "INDEX"):
            kind, kite_exchange = "INDEX", exchange
        elif segment == "CASH" and kind_raw in ("EQ", ""):
            kind, kite_exchange = "EQ", exchange
        elif segment in ("FNO", "COMMODITY") and kind_raw in ("FUT", "CE", "PE"):
            kind, kite_exchange = kind_raw, {"NSE": "NFO", "BSE": "BFO", "MCX": "MCX"}[exchange]
        else:
            continue
        derivative = kind in ("FUT", "CE", "PE")
        yield Contract(
            exchange=kite_exchange, kind=kind, key=f"{exchange}|{segment}|{trading}", symbol=trading,
            underlying=str(r.get("underlying_symbol") or "").upper() if derivative else "",
            name=str(r.get("name") or ""), expiry=parse_date(r.get("expiry_date")) if derivative else None,
            strike=number(r.get("strike_price")) if derivative else 0.0, lot_size=int(number(r.get("lot_size"), 1)) or 1,
            tick_size=number(r.get("tick_size"), 0.05) or 0.05, isin=str(r.get("isin") or ""),
            freeze_quantity=int(number(r.get("freeze_quantity"))),
            native={"exchange": exchange, "segment": segment, "trading_symbol": trading,
                    "groww_symbol": str(r.get("groww_symbol") or "")})


class Groww(RestBroker):
    name, label = "groww", "Groww"
    base_url = "https://api.groww.in/v1"
    login_kind = "totp"
    fields = (
        {"key": "access_token", "label": "Access token", "required": False, "secret": True,
         "hint": "From groww.in/trade-api → API keys. Or leave empty and give the API key below."},
        {"key": "api_key", "label": "API key", "required": False, "secret": True},
        {"key": "api_secret", "label": "API secret (approval keys)", "required": False, "secret": True,
         "hint": "Approve the key on Groww once a day before signing in."},
        {"key": "totp_secret", "label": "TOTP secret (TOTP keys)", "required": False, "secret": True},
    )
    minutes = (1, 2, 3, 5, 10, 15, 30, 60, 240)
    candle_days, day_candle_days = 28, 1000

    def headers(self) -> dict:
        out = {**super().headers(), "x-api-version": "1.0", "x-request-id": str(uuid.uuid4())}
        if self.token:
            out["Authorization"] = f"Bearer {self.token}"
        return out

    def connect(self, entry: dict, timeout: float = 60.0) -> dict:
        entry = entry or {}
        if not entry.get("access_token") and not (entry.get("api_key") and (entry.get("api_secret") or entry.get("totp_secret"))):
            self.error = "Groww needs an access token, or an API key with its secret or TOTP secret, in Settings → Broker."
            raise KiteError(self.error)
        return super().connect(entry, timeout)

    def request(self, method, path, **kw):
        body = super().request(method, path, **kw)
        return body.get("payload", body) if isinstance(body, dict) else body

    def login(self) -> dict:
        key = self.creds.get("api_key")
        if not key:
            self.token = self.creds.get("access_token")
            self._native_profile()
            return {"url": None, "message": "Using the Groww access token from Settings."}
        if self.creds.get("totp_secret"):
            body = {"key_type": "totp", "totp": totp(self.creds["totp_secret"])}
        else:
            stamp = int(time.time())
            checksum = hashlib.sha256(f"{self.creds['api_secret']}{stamp}".encode()).hexdigest()
            body = {"key_type": "approval", "checksum": checksum, "timestamp": stamp}
        answer = self.request("POST", "/token/api/access", auth=False, json_body=body,
                              headers={"Authorization": f"Bearer {key}"})
        token = answer.get("token") if isinstance(answer, dict) else None
        if not token:
            raise KiteError(f"Groww did not return an access token: {self.message(answer)}")
        self._save_token(token)
        return {"url": None, "message": "Signed in to Groww."}

    def _load_instruments(self):
        return list(contracts(self.download(INSTRUMENTS_URL).decode("utf-8", "replace")))

    def _names(self, r: dict) -> tuple[str, str]:
        exchange, segment = str(r.get("exchange") or "NSE").upper(), str(r.get("segment") or "CASH").upper()
        symbol = str(r.get("trading_symbol") or "")
        row = self.by_key(f"{exchange}|{segment}|{symbol}")
        kite_exchange = exchange if segment == "CASH" else {"NSE": "NFO", "BSE": "BFO", "MCX": "MCX"}.get(exchange, exchange)
        return self.kite_names(row, symbol, kite_exchange)

    # -- account -------------------------------------------------------------

    def _native_profile(self) -> dict:
        data = self.request("GET", "/user/detail") or {}
        return {"user_id": data.get("ucc") or data.get("vendor_user_id"), "user_name": data.get("ucc"), "broker": "GROWW"}

    def _native_margins(self) -> dict:
        data = self.request("GET", "/margins/detail/user") or {}
        equity = data.get("equity_margin_details") or {}
        commodity = data.get("commodity_margin_details") or {}
        available = equity.get("cnc_balance_available", data.get("clear_cash"))
        return margins_shape(number(available), number(data.get("net_margin_used")),
                             number(commodity.get("balance_available")) if commodity else None)

    def _native_holdings(self) -> list:
        out = []
        for r in (self.request("GET", "/holdings/user") or {}).get("holdings") or []:
            exchange, symbol = self._names({**r, "exchange": "NSE", "segment": "CASH"})
            if not self.by_key(f"NSE|CASH|{r.get('trading_symbol')}"):
                exchange, symbol = self._names({**r, "exchange": "BSE", "segment": "CASH"})
            out.append({"tradingsymbol": symbol, "exchange": exchange, "isin": r.get("isin"), "product": "CNC",
                        "quantity": int(number(r.get("quantity"))) - int(number(r.get("t1_quantity"))),
                        "t1_quantity": int(number(r.get("t1_quantity"))), "average_price": number(r.get("average_price")),
                        "last_price": None, "pnl": None})
        return out

    def _native_positions(self) -> list:
        out = []
        for r in (self.request("GET", "/positions/user") or {}).get("positions") or []:
            exchange, symbol = self._names(r)
            out.append({"tradingsymbol": symbol, "exchange": exchange, "quantity": int(number(r.get("quantity"))),
                        "average_price": number(r.get("net_price") or r.get("net_carry_forward_price")),
                        "product": r.get("product"), "last_price": None, "multiplier": 1.0,
                        "pnl": number(r.get("realised_pnl")) if r.get("realised_pnl") is not None else None})
        return out

    def _order_rows(self) -> list:
        rows = []
        for segment in ("CASH", "FNO", "COMMODITY"):
            try:
                rows.extend((self.request("GET", "/order/list", params={"segment": segment, "page": 0,
                                                                          "page_size": 100}) or {}).get("order_list") or [])
            except KiteError as exc:
                if exc.needs_login:
                    raise
        return rows

    def _native_orders(self) -> list:
        out = []
        for r in self._order_rows():
            exchange, symbol = self._names(r)
            order_type = {v: k for k, v in ORDER_TYPES.items()}.get(str(r.get("order_type")), r.get("order_type"))
            raw = str(r.get("order_status") or "").upper()
            out.append({"order_id": str(r.get("groww_order_id")), "tradingsymbol": symbol, "exchange": exchange,
                        "transaction_type": r.get("transaction_type"), "quantity": int(number(r.get("quantity"))),
                        "filled_quantity": int(number(r.get("filled_quantity"))), "price": number(r.get("price")),
                        "average_price": number(r.get("average_fill_price")), "trigger_price": number(r.get("trigger_price")),
                        "status": STATUS.get(raw, kite_status(raw)), "status_message": r.get("remark"),
                        "product": r.get("product"), "order_type": order_type, "segment": r.get("segment"),
                        "tag": r.get("order_reference_id"), "order_timestamp": r.get("created_at")})
        return out

    def _native_trades(self) -> list:
        out = []
        for order in self._native_orders():
            if not order["filled_quantity"]:
                continue
            answer = self.request("GET", f"/order/trades/{order['order_id']}",
                                  params={"segment": order.get("segment") or "CASH", "page": 0, "page_size": 50}) or {}
            for t in answer.get("trade_list") or []:
                out.append({"trade_id": str(t.get("groww_trade_id") or t.get("exchange_trade_id")),
                            "order_id": order["order_id"], "tradingsymbol": order["tradingsymbol"],
                            "exchange": order["exchange"], "transaction_type": order["transaction_type"],
                            "quantity": int(number(t.get("quantity"))), "average_price": number(t.get("price"))})
        return out

    # -- market data ---------------------------------------------------------

    def _native_quotes(self, rows: dict[str, dict]) -> dict[str, dict]:
        out = {}
        if len(rows) <= 5:
            for name, row in rows.items():
                native = row["_native"]
                q = self.request("GET", "/live-data/quote", params={
                    "exchange": native["exchange"], "segment": native["segment"], "trading_symbol": native["trading_symbol"]}) or {}
                ohlc, depth = q.get("ohlc") if isinstance(q.get("ohlc"), dict) else {}, q.get("depth") or {}
                last, change = q.get("last_price"), q.get("day_change")
                previous = (last - change) if isinstance(last, (int, float)) and isinstance(change, (int, float)) else ohlc.get("close")
                out[name] = kite_quote(
                    row, last, ohlc.get("open"), ohlc.get("high"), ohlc.get("low"), previous, q.get("volume"),
                    q.get("open_interest"), q.get("average_price"), q.get("total_buy_quantity"), q.get("total_sell_quantity"),
                    q.get("lower_circuit_limit"), q.get("upper_circuit_limit"), q.get("last_trade_time"),
                    [{"price": d.get("price"), "quantity": d.get("quantity")} for d in depth.get("buy") or []],
                    [{"price": d.get("price"), "quantity": d.get("quantity")} for d in depth.get("sell") or []])
            return out
        # Many at once: last prices and day ranges in batches of 50 per segment.
        groups: dict[str, dict[str, str]] = {}
        for name, row in rows.items():
            native = row["_native"]
            groups.setdefault(native["segment"], {})[f"{native['exchange']}_{native['trading_symbol']}"] = name
        for segment, names in groups.items():
            keys = list(names)
            for start in range(0, len(keys), 50):
                params = {"segment": segment, "exchange_symbols": ",".join(keys[start:start + 50])}
                last = self.request("GET", "/live-data/ltp", params=params) or {}
                ohlc = self.request("GET", "/live-data/ohlc", params=params) or {}
                for key in keys[start:start + 50]:
                    if key not in last:
                        continue
                    day = ohlc.get(key) if isinstance(ohlc.get(key), dict) else {}
                    name = names[key]
                    out[name] = kite_quote(rows[name], last.get(key), day.get("open"), day.get("high"), day.get("low"),
                                           day.get("close"))
        return out

    def _native_candles(self, row: dict, minutes: int | None, start, end, oi: bool) -> list:
        native = row["_native"]
        interval = "1day" if minutes is None else (f"{minutes}minute" if minutes < 60 else f"{minutes // 60}hour")
        body = self.request("GET", "/historical/candles", params={
            "exchange": native["exchange"], "segment": native["segment"],
            "groww_symbol": native["groww_symbol"] or native["trading_symbol"],
            "start_time": f"{start:%Y-%m-%d %H:%M:%S}", "end_time": f"{end:%Y-%m-%d %H:%M:%S}",
            "candle_interval": interval}) or {}
        out = []
        for c in body.get("candles") or []:
            if isinstance(c, list) and len(c) >= 5 and c[1] is not None:
                item = {"date": ist_stamp(c[0]), "open": float(c[1]), "high": float(c[2]), "low": float(c[3]),
                        "close": float(c[4]), "volume": float(c[5] if len(c) > 5 and c[5] is not None else 0)}
                if oi and len(c) > 6 and c[6] is not None:
                    item["oi"] = float(c[6])
                out.append(item)
        return out

    # -- orders --------------------------------------------------------------

    def _native_place(self, row: dict, order: dict) -> dict:
        native = row["_native"]
        if order.get("variety") == "amo":
            raise KiteError("Groww's API takes no after-market orders.")
        body = {"trading_symbol": native["trading_symbol"], "quantity": int(order["quantity"]),
                "price": float(order.get("price") or 0),
                "trigger_price": float(order["trigger_price"]) if order.get("trigger_price") else None,
                "validity": order.get("validity") or "DAY", "exchange": native["exchange"], "segment": native["segment"],
                "product": order.get("product") or "CNC",
                "order_type": ORDER_TYPES.get(str(order.get("order_type") or "LIMIT"), "LIMIT"),
                "transaction_type": order["transaction_type"],
                "order_reference_id": ("TS" + uuid.uuid4().hex)[:20]}
        answer = self.request("POST", "/order/create", json_body=body) or {}
        return {"order_id": str(answer.get("groww_order_id")) if answer.get("groww_order_id") else None, "broker": answer}

    def _native_cancel(self, order_id: str) -> dict:
        segment = next((o.get("segment") for o in self._native_orders() if o["order_id"] == order_id), None) or "CASH"
        return {"order_id": order_id, "broker": self.request("POST", "/order/cancel",
                                                            json_body={"segment": segment, "groww_order_id": order_id})}
