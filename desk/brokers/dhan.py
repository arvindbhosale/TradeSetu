"""Dhan, over DhanHQ API v2 (https://dhanhq.co/docs/v2/).

Dhan signs in with an access token the user generates on web.dhan.co (My Profile → DhanHQ Trading APIs);
it is pasted into Settings with the Dhan client id. Market data needs Dhan's Data API plan.
"""

from __future__ import annotations

import csv
import io

from ..kite import KiteError
from .base import RestBroker, ist_stamp, kite_quote, kite_status, margins_shape
from .instruments import Contract, number, parse_date

INSTRUMENTS_URL = "https://images.dhan.co/api-data/api-scrip-master.csv"
PRODUCTS = {"CNC": "CNC", "MIS": "INTRADAY", "NRML": "MARGIN"}
KITE_PRODUCTS = {"CNC": "CNC", "INTRADAY": "MIS", "MARGIN": "NRML", "MTF": "MTF", "CO": "CO", "BO": "BO"}
ORDER_TYPES = {"LIMIT": "LIMIT", "MARKET": "MARKET", "SL": "STOP_LOSS", "SL-M": "STOP_LOSS_MARKET"}
STATUS = {"TRADED": "COMPLETE", "PENDING": "OPEN", "TRANSIT": "OPEN", "PART_TRADED": "OPEN",
          "TRIGGERED": "OPEN", "REJECTED": "REJECTED", "CANCELLED": "CANCELLED", "EXPIRED": "CANCELLED"}


def _segment(exchange: str, code: str) -> tuple[str, str] | None:
    """(Kite exchange, Dhan exchangeSegment) for the master's exchange and segment letter."""
    return {("NSE", "E"): ("NSE", "NSE_EQ"), ("BSE", "E"): ("BSE", "BSE_EQ"), ("NSE", "D"): ("NFO", "NSE_FNO"),
            ("BSE", "D"): ("BFO", "BSE_FNO"), ("MCX", "M"): ("MCX", "MCX_COMM"), ("NSE", "I"): ("NSE", "IDX_I"),
            ("BSE", "I"): ("BSE", "IDX_I")}.get((exchange, code))


def _tick(value, exchange: str) -> float:
    tick = number(value, 0.05)
    # The scrip master has carried ticks in paise in some versions: 5.0 for 0.05.
    if (exchange != "MCX" and tick >= 1) or (exchange == "MCX" and tick >= 50):
        tick /= 100
    return tick or 0.05


def contracts(text: str):
    """Dhan's compact scrip master (CSV with SEM_* columns) as contracts."""
    for r in csv.DictReader(io.StringIO(text.lstrip("﻿"))):
        found = _segment(str(r.get("SEM_EXM_EXCH_ID") or "").strip(), str(r.get("SEM_SEGMENT") or "").strip())
        if not found:
            continue
        exchange, segment = found
        instrument = str(r.get("SEM_INSTRUMENT_NAME") or "").strip().upper()
        trading = str(r.get("SEM_TRADING_SYMBOL") or "").strip().upper()
        security = str(r.get("SEM_SMST_SECURITY_ID") or "").strip()
        if not security or not trading:
            continue
        option = str(r.get("SEM_OPTION_TYPE") or "").strip().upper()
        if segment == "IDX_I":
            kind = "INDEX"
        elif instrument == "EQUITY":
            kind = "EQ"
            series = str(r.get("SEM_SERIES") or "").strip().upper()
            if exchange == "NSE" and series and series != "EQ":
                trading = f"{trading}-{series}"
        elif instrument.startswith("FUT"):
            kind = "FUT"
        elif instrument.startswith("OPT") and option in ("CE", "PE"):
            kind = option
        else:
            continue
        yield Contract(
            exchange=exchange, kind=kind, key=f"{segment}|{security}", symbol=trading,
            underlying=trading.split("-")[0] if kind in ("FUT", "CE", "PE") else "",
            name=str(r.get("SM_SYMBOL_NAME") or r.get("SEM_CUSTOM_SYMBOL") or "").strip(),
            expiry=parse_date(r.get("SEM_EXPIRY_DATE")) if kind in ("FUT", "CE", "PE") else None,
            strike=number(r.get("SEM_STRIKE_PRICE")), lot_size=int(number(r.get("SEM_LOT_UNITS"), 1)) or 1,
            tick_size=_tick(r.get("SEM_TICK_SIZE"), exchange),
            native={"segment": segment, "security_id": security, "instrument": instrument})


class Dhan(RestBroker):
    name, label = "dhan", "Dhan"
    base_url = "https://api.dhan.co/v2"
    login_kind = "token"
    fields = (
        {"key": "client_id", "label": "Dhan client id", "required": True, "secret": False},
        {"key": "access_token", "label": "Access token", "required": True, "secret": True,
         "hint": "Generate it on web.dhan.co → My Profile → DhanHQ Trading APIs. Paste a new one when it expires."},
    )
    minutes = (1, 5, 15, 25, 60)
    candle_days, day_candle_days = 85, 3000

    def headers(self) -> dict:
        return {**super().headers(), "access-token": self.token or "", "client-id": self.creds.get("client_id", "")}

    def session_expired(self, status: int, body) -> bool:
        return isinstance(body, dict) and str(body.get("errorCode", "")) in ("DH-901", "807")

    def failed(self, body) -> bool:
        return isinstance(body, dict) and bool(body.get("errorCode")) and not body.get("data")

    def login(self) -> dict:
        profile = self._native_profile()
        return {"url": None, "message": f"Signed in to Dhan as {profile.get('user_id')}. The token is valid until "
                                        f"{profile.get('token_validity') or 'its expiry on web.dhan.co'}."}

    def _load_instruments(self):
        return list(contracts(self.download(INSTRUMENTS_URL).decode("utf-8", "replace")))

    def by_native(self, segment, security_id) -> dict | None:
        return self.by_key(f"{segment}|{security_id}")

    def _names(self, r: dict) -> tuple[str, str]:
        segment, sid = str(r.get("exchangeSegment") or ""), r.get("securityId")
        row = self.by_native(segment, sid) if segment else None
        if row is None and not segment:
            row = self.by_native("NSE_EQ", sid) or self.by_native("BSE_EQ", sid)
        exchange = {"NSE_EQ": "NSE", "BSE_EQ": "BSE", "NSE_FNO": "NFO", "BSE_FNO": "BFO", "MCX_COMM": "MCX"}.get(
            segment, str(r.get("exchange") or ""))
        return self.kite_names(row, str(r.get("tradingSymbol") or ""), exchange)

    # -- account -------------------------------------------------------------

    def _native_profile(self) -> dict:
        data = self.request("GET", "/profile") or {}
        return {"user_id": data.get("dhanClientId"), "user_name": data.get("dhanClientName") or data.get("dhanClientId"),
                "broker": "DHAN", "token_validity": data.get("tokenValidity")}

    def _native_margins(self) -> dict:
        data = self.request("GET", "/fundlimit") or {}
        available = data.get("availabelBalance", data.get("availableBalance"))
        return margins_shape(available, data.get("utilizedAmount"))

    def _list(self, path: str) -> list:
        data = self.request("GET", path)
        return data if isinstance(data, list) else (data.get("data") or [] if isinstance(data, dict) else [])

    def _native_holdings(self) -> list:
        out = []
        for r in self._list("/holdings"):
            exchange, symbol = self._names(r)
            out.append({"tradingsymbol": symbol, "exchange": exchange, "isin": r.get("isin"), "product": "CNC",
                        "quantity": int(number(r.get("totalQty"))) - int(number(r.get("t1Qty"))),
                        "t1_quantity": int(number(r.get("t1Qty"))), "average_price": number(r.get("avgCostPrice")),
                        "last_price": number(r.get("lastTradedPrice")), "pnl": None})
        return out

    def _native_positions(self) -> list:
        out = []
        for r in self._list("/positions"):
            exchange, symbol = self._names(r)
            quantity = int(number(r.get("netQty")))
            average = number(r.get("costPrice")) or number(r.get("buyAvg") if quantity >= 0 else r.get("sellAvg"))
            out.append({"tradingsymbol": symbol, "exchange": exchange, "quantity": quantity, "average_price": average,
                        "product": KITE_PRODUCTS.get(str(r.get("productType")), r.get("productType")),
                        "last_price": None, "multiplier": number(r.get("multiplier"), 1.0),
                        "pnl": number(r.get("realizedProfit")) + number(r.get("unrealizedProfit"))})
        return out

    def _native_orders(self) -> list:
        out = []
        for r in self._list("/orders"):
            exchange, symbol = self._names(r)
            order_type = {v: k for k, v in ORDER_TYPES.items()}.get(str(r.get("orderType")), r.get("orderType"))
            out.append({"order_id": str(r.get("orderId")), "tradingsymbol": symbol, "exchange": exchange,
                        "transaction_type": r.get("transactionType"), "quantity": int(number(r.get("quantity"))),
                        "filled_quantity": int(number(r.get("filledQty"))), "price": number(r.get("price")),
                        "average_price": number(r.get("averageTradedPrice")), "trigger_price": number(r.get("triggerPrice")),
                        "status": STATUS.get(str(r.get("orderStatus")).upper(), kite_status(r.get("orderStatus"))),
                        "status_message": r.get("omsErrorDescription"), "order_type": order_type,
                        "product": KITE_PRODUCTS.get(str(r.get("productType")), r.get("productType")),
                        "tag": r.get("correlationId"), "order_timestamp": r.get("createTime")})
        return out

    def _native_trades(self) -> list:
        out = []
        for r in self._list("/trades"):
            exchange, symbol = self._names(r)
            out.append({"trade_id": str(r.get("exchangeTradeId") or r.get("tradeId") or ""), "order_id": str(r.get("orderId")),
                        "tradingsymbol": symbol, "exchange": exchange, "transaction_type": r.get("transactionType"),
                        "quantity": int(number(r.get("tradedQuantity"))), "average_price": number(r.get("tradedPrice"))})
        return out

    # -- market data ---------------------------------------------------------

    def _native_quotes(self, rows: dict[str, dict]) -> dict[str, dict]:
        wanted: dict[str, list[int]] = {}
        by_native = {}
        for name, row in rows.items():
            native = row["_native"]
            wanted.setdefault(native["segment"], []).append(int(native["security_id"]))
            by_native[(native["segment"], str(native["security_id"]))] = name
        body = self.request("POST", "/marketfeed/quote", json_body=wanted) or {}
        out = {}
        for segment, items in ((body.get("data") or {}) if isinstance(body, dict) else {}).items():
            for sid, q in (items or {}).items():
                name = by_native.get((segment, str(sid)))
                if not name or not isinstance(q, dict):
                    continue
                ohlc, depth = q.get("ohlc") or {}, q.get("depth") or {}
                last, change = q.get("last_price"), q.get("net_change")
                previous = ohlc.get("close")
                if isinstance(last, (int, float)) and isinstance(change, (int, float)) and change:
                    previous = last - change
                out[name] = kite_quote(
                    rows[name], last, ohlc.get("open"), ohlc.get("high"), ohlc.get("low"), previous, q.get("volume"),
                    q.get("oi"), q.get("average_price"), q.get("buy_quantity"), q.get("sell_quantity"),
                    q.get("lower_circuit_limit"), q.get("upper_circuit_limit"), q.get("last_trade_time"),
                    [{"price": d.get("price"), "quantity": d.get("quantity")} for d in depth.get("buy") or []],
                    [{"price": d.get("price"), "quantity": d.get("quantity")} for d in depth.get("sell") or []])
        return out

    def _native_candles(self, row: dict, minutes: int | None, start, end, oi: bool) -> list:
        native = row["_native"]
        instrument = "INDEX" if native["segment"] == "IDX_I" else native["instrument"]
        payload = {"securityId": native["security_id"], "exchangeSegment": native["segment"], "instrument": instrument,
                   "oi": bool(oi)}
        if minutes is None:
            from datetime import timedelta

            payload.update(expiryCode=0, fromDate=f"{start:%Y-%m-%d}", toDate=f"{end + timedelta(days=1):%Y-%m-%d}")
            body = self.request("POST", "/charts/historical", json_body=payload)
        else:
            payload.update(interval=str(minutes), fromDate=f"{start:%Y-%m-%d %H:%M:%S}", toDate=f"{end:%Y-%m-%d %H:%M:%S}")
            body = self.request("POST", "/charts/intraday", json_body=payload)
        body = body.get("data", body) if isinstance(body, dict) else {}
        stamps = body.get("timestamp") or []
        out = []
        for i, stamp in enumerate(stamps):
            try:
                item = {"date": ist_stamp(stamp), "open": float(body["open"][i]), "high": float(body["high"][i]),
                        "low": float(body["low"][i]), "close": float(body["close"][i]),
                        "volume": float((body.get("volume") or [0] * len(stamps))[i] or 0)}
            except (KeyError, IndexError, TypeError, ValueError):
                continue
            if oi and body.get("open_interest"):
                item["oi"] = float(body["open_interest"][i] or 0)
            if minutes is None:
                item["date"] = item["date"][:10] + "T00:00:00+0530"
            out.append(item)
        return out

    # -- orders --------------------------------------------------------------

    def _native_place(self, row: dict, order: dict) -> dict:
        native = row["_native"]
        if native["segment"] == "IDX_I":
            raise KiteError("An index cannot be traded.")
        body = {"dhanClientId": self.creds.get("client_id"), "correlationId": str(order.get("tag") or "")[:25],
                "transactionType": order["transaction_type"], "exchangeSegment": native["segment"],
                "productType": PRODUCTS.get(str(order.get("product")), "CNC"),
                "orderType": ORDER_TYPES.get(str(order.get("order_type") or "LIMIT"), "LIMIT"),
                "validity": order.get("validity") or "DAY", "securityId": str(native["security_id"]),
                "quantity": int(order["quantity"]), "disclosedQuantity": 0, "price": float(order.get("price") or 0),
                "triggerPrice": float(order.get("trigger_price") or 0), "afterMarketOrder": order.get("variety") == "amo"}
        if body["afterMarketOrder"]:
            body["amoTime"] = "OPEN"
        answer = self.request("POST", "/orders", json_body=body) or {}
        if str(answer.get("orderStatus", "")).upper() == "REJECTED":
            raise KiteError(f"Dhan rejected the order: {answer.get('omsErrorDescription') or 'no reason given'}")
        return {"order_id": str(answer.get("orderId")) if answer.get("orderId") else None, "broker": answer}

    def _native_cancel(self, order_id: str) -> dict:
        return {"order_id": order_id, "broker": self.request("DELETE", f"/orders/{order_id}")}
