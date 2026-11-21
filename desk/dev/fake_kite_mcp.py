"""A stand-in Kite MCP server for development and tests. Not a broker.

Speaks the same tool names and argument shapes as zerodha/kite-mcp-server so
the desk can be exercised end to end without an account. Run with
``--read-only`` to mimic the hosted server, which registers no order tools.

    python -m desk.dev.fake_kite_mcp [--read-only]
"""

from __future__ import annotations

import json
import sys

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

READ_ONLY = "--read-only" in sys.argv
server = MCPServer("fake-kite", log_level="CRITICAL")
STATE = {"logged_in": False, "orders": [], "gtts": []}
PRICES = {"NSE:RELIANCE": 1400.0, "NSE:TCS": 3500.0, "NSE:INFY": 1500.0, "BSE:RELIANCE": 1400.5,
          "NSE:NIFTY 50": 24500.0, "NSE:NIFTY BANK": 52000.0, "NSE:NIFTY IT": 38000.0,
          "BSE:SENSEX": 81000.0, "BSE:BANKEX": 60000.0, "BSE:SENSEX50": 25800.0, "BSE:BSE500": 36000.0}
# Kite files indices under the INDICES segment; these are the BSE ones the fake lists.
BSE_INDEX_ROWS = [("SENSEX", "SENSEX"), ("BANKEX", "BANKEX"), ("SENSEX50", "SENSEX50"), ("BSE500", "BSE 500")]
HOLDINGS = [{"tradingsymbol": "INFY", "exchange": "NSE", "quantity": 20, "average_price": 1450.0,
             "last_price": 1500.0, "pnl": 1000.0}]


def _need_login():
    if not STATE["logged_in"]:
        raise ToolError("Please log in first using the login tool")


@server.tool()
def login() -> str:
    """Login to Kite."""
    STATE["logged_in"] = True
    return "Please login here: https://kite.zerodha.com/connect/login?api_key=fake&v=3"


@server.tool()
def get_profile() -> str:
    """Get user profile."""
    _need_login()
    return json.dumps({"user_id": "ZZ0000", "user_name": "Test Trader", "broker": "ZERODHA",
                       "exchanges": ["NSE", "BSE"]})


@server.tool()
def get_margins() -> str:
    """Get margins."""
    _need_login()
    return json.dumps({"equity": {"enabled": True, "net": 250000.0, "utilised": {"debits": 0.0},
                                  "available": {"live_balance": 250000.0, "cash": 250000.0, "opening_balance": 250000.0,
                                                "intraday_payin": 0.0, "collateral": 0.0}},
                       "commodity": {"enabled": True, "net": 400000.0, "utilised": {"debits": 0.0},
                                     "available": {"live_balance": 400000.0, "cash": 400000.0, "opening_balance": 400000.0,
                                                   "intraday_payin": 0.0, "collateral": 0.0}}})


@server.tool()
def get_holdings() -> str:
    """Get holdings."""
    _need_login()
    return json.dumps(HOLDINGS)


@server.tool()
def get_positions() -> str:
    """Get positions."""
    _need_login()
    net = {}
    for order in STATE["orders"]:
        if order["exchange"] in ("NFO", "BFO", "MCX") and order["status"] != "CANCELLED":
            sign = 1 if order["transaction_type"] == "BUY" else -1
            row = net.setdefault(order["tradingsymbol"], {"tradingsymbol": order["tradingsymbol"], "exchange": order["exchange"],
                                                          "product": order["product"], "quantity": 0, "pnl": 0.0})
            row["quantity"] += sign * int(order["quantity"])
    return json.dumps({"net": list(net.values()), "day": []})


@server.tool()
def get_orders() -> str:
    """Get orders."""
    _need_login()
    return json.dumps(STATE["orders"])


@server.tool()
def get_trades() -> str:
    """Get today's trades. Every order placed is shown as filled in full at its price."""
    _need_login()
    return json.dumps([{"trade_id": "T" + o["order_id"], "order_id": o["order_id"], "exchange": o["exchange"],
                        "tradingsymbol": o["tradingsymbol"], "transaction_type": o["transaction_type"],
                        "quantity": o["quantity"], "average_price": o["price"] or PRICES.get(
                            f"{o['exchange']}:{o['tradingsymbol']}", 0)}
                       for o in STATE["orders"] if o["status"] != "CANCELLED"])


OPTIONS: dict = {}
METALS: dict = {}                                   # MCX symbol -> name, kind, strike / price
METAL_FUTURE = {"GOLDM": 130000.0, "SILVERM": 152000.0}
TOKENS: dict = {}


def _anchor():
    """The fake's "today": FAKE_KITE_ANCHOR when set, so tests pinned to it do not age; else the real date."""
    import os
    from datetime import date

    return date.fromisoformat(os.environ.get("FAKE_KITE_ANCHOR") or date.today().isoformat())


def _token(instrument: str) -> int:
    token = abs(hash(instrument)) % 10_000_000
    TOKENS[token] = instrument
    return token


def _price(instrument: str):
    """A price for any instrument the fake knows: stocks and indices from PRICES, options by Black-Scholes."""
    if instrument in PRICES:
        return PRICES[instrument]
    symbol = instrument.split(":", 1)[-1]
    if instrument.startswith("MCX:"):
        metal = METALS.get(symbol)
        if metal is None:
            return None
        if metal["kind"] == "FUT":
            return metal["price"]
        future = METAL_FUTURE[metal["name"]]
        intrinsic = max(future - metal["strike"], 0) if metal["kind"] == "CE" else max(metal["strike"] - future, 0)
        return round(intrinsic + max(future * 0.012 - abs(future - metal["strike"]) * 0.3, 20)) + 0.5
    if symbol.endswith("FUT") and instrument.startswith("NFO:"):
        import re

        match = re.match(r"([A-Z&-]+?)(\d{2})([A-Z]{3}|\d{2})FUT$", symbol)
        if match:
            name = match.group(1)
            spot = PRICES.get({"NIFTY": "NSE:NIFTY 50", "BANKNIFTY": "NSE:NIFTY BANK"}.get(name, f"NSE:{name}"))
            if spot:
                months = (int(match.group(3)) - 11) % 12 + 1 if match.group(3).isdigit() else 1
                return round(spot * (1 + 0.005 * months), 2)
        return None
    option = OPTIONS.get(symbol)
    if not option:
        return None
    import math
    from datetime import date

    days = max((date.fromisoformat(option["expiry"]) - _anchor()).days, 0) + 0.3
    spot, strike, vol, t = PRICES["NSE:NIFTY 50"], option["strike"], 0.13, days / 365
    d1 = (math.log(spot / strike) + (0.065 + vol * vol / 2) * t) / (vol * math.sqrt(t))
    d2 = d1 - vol * math.sqrt(t)
    cdf = lambda x: 0.5 * (1 + math.erf(x / math.sqrt(2)))       # noqa: E731
    if option["kind"] == "CE":
        value = spot * cdf(d1) - strike * math.exp(-0.065 * t) * cdf(d2)
    else:
        value = strike * math.exp(-0.065 * t) * cdf(-d2) - spot * cdf(-d1)
    return round(max(value, 0.05) * 20) / 20


@server.tool()
def get_quotes(instruments: list[str]) -> str:
    """Get full quotes."""
    _need_login()
    out = {}
    for name in instruments:
        price = _price(name)
        if price is None:
            continue
        oi = (abs(hash(name)) % 90 + 10) * 1000 if name.startswith("NFO:") else 0
        out[name] = {"instrument_token": _token(name), "last_price": price, "volume": 125000,
                     "average_price": price, "oi": oi, "buy_quantity": 1000, "sell_quantity": 900,
                     "lower_circuit_limit": round(price * 0.9, 2), "upper_circuit_limit": round(price * 1.1, 2),
                     "ohlc": {"open": round(price * 0.99, 2), "high": round(price * 1.01, 2), "low": round(price * 0.98, 2),
                              "close": round(price * 0.995, 2)},
                     "depth": {"buy": [{"price": round(price - 0.05, 2), "quantity": 75}],
                               "sell": [{"price": round(price + 0.05, 2), "quantity": 75}]}}
    return json.dumps(out)


@server.tool()
def get_ltp(instruments: list[str]) -> str:
    """Get last traded prices."""
    _need_login()
    return json.dumps({i: {"instrument_token": _token(i), "last_price": _price(i)}
                       for i in instruments if _price(i) is not None})


@server.tool()
def get_historical_data(instrument_token: float, from_date: str, to_date: str, interval: str,
                        continuous: bool = False, oi: bool = False) -> str:
    """Get historical candles."""
    _need_login()
    import math
    from datetime import datetime, timedelta

    minutes = {"minute": 1, "3minute": 3, "5minute": 5, "10minute": 10, "15minute": 15, "30minute": 30,
               "60minute": 60, "day": 0}[interval]
    start = datetime.strptime(from_date, "%Y-%m-%d %H:%M:%S")
    end = datetime.strptime(to_date, "%Y-%m-%d %H:%M:%S")
    final = _price(TOKENS.get(int(instrument_token), "")) or 1400.0
    rows, day, price, n = [], start.replace(hour=0, minute=0, second=0), 1400.0, 0
    import random

    rng = random.Random(int(instrument_token) * 31 + minutes)
    scale = math.sqrt(minutes / 375) if minutes else 1.0
    while day <= end:
        if day.weekday() < 5:
            slots = [day] if not minutes else [day.replace(hour=9, minute=15) + timedelta(minutes=minutes * k)
                                               for k in range(375 // minutes + (1 if 375 % minutes else 0))]
            for stamp in slots:
                if minutes and not start <= stamp <= end:
                    continue
                n += 1
                # A random walk (seeded per instrument, so repeatable): realistic enough that strategies on it
                # neither win every trade nor lose every trade.
                close = price * math.exp(rng.gauss(0.0003 * scale ** 2, 0.011 * scale))
                rows.append({"date": stamp.strftime("%Y-%m-%dT%H:%M:%S+0530"), "open": round(price, 2),
                             "high": round(max(price, close) * 1.002, 2), "low": round(min(price, close) * 0.998, 2),
                             "close": round(close, 2), "volume": 100000 + (n * 7919) % 50000,
                             **({"oi": 1_000_000 + (n * 104729) % 200000} if oi else {})})
                price = close
        day += timedelta(days=1)
    rows = rows[-40000:]
    if rows:                                   # scale so the last close is the instrument's current price
        factor = final / rows[-1]["close"]
        for row in rows:
            for key in ("open", "high", "low", "close"):
                row[key] = round(row[key] * factor, 2)
    return json.dumps(rows)


@server.tool()
def search_instruments(query: str, filter_on: str = "id") -> str:
    """Search instruments."""
    from datetime import timedelta

    # Dates hang off an anchor day (today unless FAKE_KITE_ANCHOR is set) so they are always in the future.
    anchor = _anchor()
    monthly = [(anchor + timedelta(days=d)).isoformat() for d in (8, 43, 71)]
    weekly = [(anchor + timedelta(days=d)).isoformat() for d in (1, 15)]
    past = (anchor - timedelta(days=20)).isoformat()
    lots = {"NIFTY": 75, "BANKNIFTY": 35, "RELIANCE": 500, "TCS": 175, "INFY": 400, "SBIN": 750}
    rows = []
    if filter_on == "id" and query == "BSE:":
        rows = [{"exchange": "BSE", "segment": "INDICES", "tradingsymbol": symbol, "name": name, "instrument_type": "EQ",
                 "lot_size": 0} for symbol, name in BSE_INDEX_ROWS]
        rows.append({"exchange": "BSE", "segment": "BSE", "tradingsymbol": "RELIANCE", "name": "RELIANCE INDUSTRIES",
                     "instrument_type": "EQ", "lot_size": 1})
    elif filter_on == "tradingsymbol" and query == "FUT":
        for name, lot in lots.items():
            for expiry in monthly:
                rows.append({"exchange": "NFO", "name": name, "tradingsymbol": f"{name}{expiry[2:4]}{expiry[5:7]}FUT",
                             "instrument_type": "FUT", "expiry_date": expiry, "lot_size": lot})
        rows.append({"exchange": "MCX", "name": "GOLD", "tradingsymbol": "GOLD26DECFUT", "instrument_type": "FUT",
                     "expiry_date": monthly[1], "lot_size": 100})
        rows.append({"exchange": "NFO", "name": "OLD", "tradingsymbol": "OLD26OCTFUT", "instrument_type": "FUT",
                     "expiry_date": past, "lot_size": 10})
    elif filter_on == "underlying" and query.split(":")[-1] in METAL_FUTURE and query.startswith("MCX:"):
        name, months = query.split(":")[-1], ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
        option_expiry = monthly[1]
        futures = [(anchor + timedelta(days=d)).isoformat() for d in (48, 100)]    # options expire before their future
        for n, expiry in enumerate(futures):
            symbol = f"{name}{expiry[2:4]}{months[int(expiry[5:7]) - 1]}FUT"
            METALS[symbol] = {"name": name, "kind": "FUT", "price": METAL_FUTURE[name] + 400 * n}
            rows.append({"exchange": "MCX", "name": name, "tradingsymbol": symbol, "instrument_type": "FUT",
                         "expiry_date": expiry, "lot_size": 1, "tick_size": 1.0})
        step = 500 if name == "GOLDM" else 250
        centre = round(METAL_FUTURE[name] / step) * step
        label = f"{name}{futures[0][2:4]}{months[int(futures[0][5:7]) - 1]}"
        for strike in range(centre - 10 * step, centre + 10 * step + 1, step):
            for kind in ("CE", "PE"):
                symbol = f"{label}{strike}{kind}"
                METALS[symbol] = {"name": name, "kind": kind, "strike": strike}
                rows.append({"exchange": "MCX", "name": name, "tradingsymbol": symbol, "instrument_type": kind,
                             "strike": strike, "expiry_date": option_expiry, "lot_size": 1, "tick_size": 0.5})
    elif filter_on == "underlying" and query == "NFO:NIFTY":
        for expiry in sorted(weekly + monthly[:2]):
            for strike in range(23500, 25550, 50):
                for kind in ("CE", "PE"):
                    symbol = f"NIFTY{expiry[2:4]}{expiry[5:7]}{expiry[8:10]}{strike}{kind}"
                    OPTIONS[symbol] = {"strike": strike, "kind": kind, "expiry": expiry}
                    rows.append({"exchange": "NFO", "name": "NIFTY", "tradingsymbol": symbol, "instrument_type": kind,
                                 "strike": strike, "expiry_date": expiry, "lot_size": 75, "freeze_quantity": 1800})
    return json.dumps(rows)


@server.tool()
def get_gtts() -> str:
    """Get GTT orders."""
    _need_login()
    return json.dumps(STATE["gtts"])


if not READ_ONLY:
    @server.tool()
    def place_gtt_order(exchange: str, tradingsymbol: str, last_price: float, transaction_type: str,
                        product: str, trigger_type: str, trigger_value: float = 0, quantity: float = 0,
                        limit_price: float = 0, upper_trigger_value: float = 0, upper_quantity: float = 0,
                        upper_limit_price: float = 0, lower_trigger_value: float = 0,
                        lower_quantity: float = 0, lower_limit_price: float = 0) -> str:
        """Place a GTT order."""
        _need_login()
        trigger_id = 1000 + len(STATE["gtts"]) + 1
        values = [trigger_value] if trigger_type == "single" else [lower_trigger_value, upper_trigger_value]
        STATE["gtts"].append({"id": trigger_id, "type": trigger_type, "status": "active",
                              "condition": {"exchange": exchange, "tradingsymbol": tradingsymbol,
                                            "trigger_values": values, "last_price": last_price},
                              "orders": [{"transaction_type": transaction_type, "product": product,
                                          "quantity": quantity or lower_quantity,
                                          "price": limit_price or lower_limit_price}]})
        return json.dumps({"trigger_id": trigger_id})

    @server.tool()
    def delete_gtt_order(trigger_id: float) -> str:
        """Delete a GTT order."""
        _need_login()
        for gtt in STATE["gtts"]:
            if gtt["id"] == int(trigger_id):
                gtt["status"] = "deleted"
        return json.dumps({"trigger_id": int(trigger_id)})

    @server.tool()
    def cancel_order(variety: str, order_id: str) -> str:
        """Cancel an order."""
        _need_login()
        for order in STATE["orders"]:
            if order["order_id"] == order_id:
                order["status"] = "CANCELLED"
        return json.dumps({"order_id": order_id})

    @server.tool()
    def get_basket_margins(orders: list[dict], consider_positions: bool = False) -> str:
        """Margins for a basket (the desk's kite-mcp-server build). Crude: sold options and futures need 9% SPAN and
        3% exposure of their notional, bought options their premium; a basket with both gets 70% off the sold legs."""
        _need_login()
        if not orders:
            raise ToolError("orders must be a non-empty list")
        rows, bought = [], any(o.get("transaction_type") == "BUY" and not str(o.get("tradingsymbol", "")).endswith("FUT")
                               for o in orders)
        for o in orders:
            symbol, quantity = str(o["tradingsymbol"]).upper(), float(o["quantity"])
            price = _price(f"{o['exchange']}:{symbol}")
            if price is None:
                raise ToolError(f"Failed to calculate margins: Invalid `tradingsymbol` {symbol}")
            future = symbol.endswith("FUT")
            if o["transaction_type"] == "BUY" and not future:
                span = exposure = 0.0
                premium = price * quantity
            else:
                notional = (price if future else PRICES["NSE:NIFTY 50"]) * quantity
                span, exposure, premium = notional * 0.09, notional * 0.03, 0.0
            rows.append({"tradingsymbol": symbol, "exchange": o["exchange"], "span": span, "exposure": exposure,
                         "option_premium": premium, "total": span + exposure + premium})
        sold = sum(r["span"] + r["exposure"] for r in rows)
        premium = sum(r["option_premium"] for r in rows)
        cut = 0.7 if bought and sold else 0.0
        initial = {"span": sum(r["span"] for r in rows), "exposure": sum(r["exposure"] for r in rows),
                   "option_premium": premium, "total": sold + premium}
        final = {"span": initial["span"] * (1 - cut), "exposure": initial["exposure"] * (1 - cut),
                 "option_premium": premium, "total": sold * (1 - cut) + premium}
        return json.dumps({"initial": initial, "final": final, "orders": rows})

    @server.tool()
    def place_order(variety: str, exchange: str, tradingsymbol: str, transaction_type: str,
                    quantity: float, product: str, order_type: str, price: float = 0,
                    trigger_price: float = 0, validity: str = "DAY", tag: str = "") -> str:
        """Place an order."""
        _need_login()
        order_id = f"FAKE{len(STATE['orders']) + 1:06d}"
        STATE["orders"].append({
            "order_id": order_id, "variety": variety, "exchange": exchange,
            "tradingsymbol": tradingsymbol, "transaction_type": transaction_type,
            "quantity": quantity, "product": product, "order_type": order_type, "price": price,
            "status": "OPEN", "tag": tag,
        })
        return json.dumps({"order_id": order_id})


if __name__ == "__main__":
    server.run("stdio")
