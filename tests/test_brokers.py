"""The broker layer: Kite symbols from every broker's instrument file, and the Kite-shaped surface over REST.

Nothing here reaches a broker. The instrument rows are written in each broker's published file format and
the HTTP answers in each API's documented shape.
"""

from __future__ import annotations

import json
from datetime import date, datetime

import pytest

from desk.brokers import BROKERS, Switch, angelone, catalog, dhan, fyers, groww, upstox
from desk.brokers.instruments import Master, MasterCache, index_symbol, kite_symbol, parse_date
from desk.brokers.totp import totp
from desk.kite import KiteError

# Tuesday expiries in November 2025: weeklies on the 4th, 11th and 18th, the monthly on the 25th.
WEEKLY, MONTHLY = date(2025, 11, 18), date(2025, 11, 25)
EXPECTED = {"NSE:RELIANCE", "NSE:NIFTY 50", "NSE:NIFTY BANK", "NFO:NIFTY25NOVFUT", "NFO:NIFTY25NOV24000CE",
            "NFO:NIFTY25N1824000PE", "NFO:RELIANCE25NOV1500CE", "MCX:CRUDEOIL25DECFUT", "BFO:SENSEX25N2084000CE"}


def ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, 15, 30).timestamp() * 1000)


# -- symbols ------------------------------------------------------------------

def test_kite_symbols_for_monthly_and_weekly_contracts():
    assert kite_symbol("NIFTY", "FUT", MONTHLY) == "NIFTY25NOVFUT"
    assert kite_symbol("NIFTY", "CE", MONTHLY, 24000, monthly=True) == "NIFTY25NOV24000CE"
    assert kite_symbol("NIFTY", "PE", WEEKLY, 24000, monthly=False) == "NIFTY25N1824000PE"
    assert kite_symbol("NIFTY", "CE", date(2025, 10, 7), 25050, monthly=False) == "NIFTY25O0725050CE"
    assert kite_symbol("NIFTY", "CE", date(2026, 1, 6), 26000, monthly=False) == "NIFTY2610626000CE"
    assert kite_symbol("IDEA", "CE", MONTHLY, 7.5) == "IDEA25NOV7.5CE"


def test_index_names_from_any_broker_spelling():
    for spelling in ("Nifty 50", "NIFTY", "NIFTY50-INDEX", "nifty50"):
        assert index_symbol(spelling) == "NIFTY 50"
    assert index_symbol("Nifty Bank") == index_symbol("BANKNIFTY") == "NIFTY BANK"
    assert index_symbol("MIDCPNIFTY") == "NIFTY MID SELECT"
    assert index_symbol("Nifty Smallcap 100") == "NIFTY SMALLCAP 100"


def test_parse_date_reads_every_broker_format():
    for raw in ("2025-11-25", "2025-11-25 14:30:00", "25NOV2025", "25-Nov-2025", str(ms(MONTHLY)), "1764064800"):
        assert parse_date(raw) == MONTHLY, raw
    assert parse_date("") is None and parse_date("nan") is None


# -- each broker's instrument file gives the same Kite symbols ------------------

def upstox_rows():
    def opt(kind, strike, day, weekly, under="NIFTY", seg="NSE_FO", key="1"):
        return {"segment": seg, "instrument_type": kind, "instrument_key": f"{seg}|{key}", "trading_symbol": "x",
                "underlying_symbol": under, "expiry": ms(day), "strike_price": strike, "lot_size": 75,
                "tick_size": 5.0, "weekly": weekly}
    return [
        {"segment": "NSE_EQ", "instrument_type": "EQ", "instrument_key": "NSE_EQ|INE002A01018",
         "trading_symbol": "RELIANCE", "name": "RELIANCE INDUSTRIES LTD", "lot_size": 1, "tick_size": 10.0},
        {"segment": "NSE_INDEX", "instrument_type": "INDEX", "instrument_key": "NSE_INDEX|Nifty 50",
         "trading_symbol": "NIFTY", "name": "Nifty 50"},
        {"segment": "NSE_INDEX", "instrument_type": "INDEX", "instrument_key": "NSE_INDEX|Nifty Bank",
         "trading_symbol": "BANKNIFTY", "name": "Nifty Bank"},
        opt("FUT", 0, MONTHLY, False, key="2"), opt("CE", 24000, MONTHLY, False, key="3"),
        opt("PE", 24000, WEEKLY, True, key="4"), opt("CE", 1500, MONTHLY, False, "RELIANCE", key="5"),
        opt("FUT", 0, date(2025, 12, 18), False, "CRUDEOIL", "MCX_FO", "6"),
        opt("CE", 84000, date(2025, 11, 20), True, "SENSEX", "BSE_FO", "7"),
    ]


def dhan_csv():
    head = ("SEM_EXM_EXCH_ID,SEM_SEGMENT,SEM_SMST_SECURITY_ID,SEM_INSTRUMENT_NAME,SEM_EXPIRY_CODE,SEM_TRADING_SYMBOL,"
            "SEM_LOT_UNITS,SEM_CUSTOM_SYMBOL,SEM_EXPIRY_DATE,SEM_STRIKE_PRICE,SEM_OPTION_TYPE,SEM_TICK_SIZE,"
            "SEM_EXPIRY_FLAG,SEM_EXCH_INSTRUMENT_TYPE,SEM_SERIES,SM_SYMBOL_NAME")
    rows = [
        "NSE,E,2885,EQUITY,,RELIANCE,1,Reliance Industries,,,XX,10,NA,ES,EQ,RELIANCE INDUSTRIES LTD",
        "NSE,I,13,INDEX,,NIFTY,1,Nifty 50,,,XX,5,NA,INDEX,,NIFTY 50",
        "NSE,I,25,INDEX,,BANKNIFTY,1,Nifty Bank,,,XX,5,NA,INDEX,,NIFTY BANK",
        "NSE,D,52175,FUTIDX,0,NIFTY-Nov2025-FUT,75,NIFTY NOV FUT,2025-11-25 14:30:00,-0.01,XX,10,M,FUTIDX,,",
        "NSE,D,52176,OPTIDX,0,NIFTY-Nov2025-24000-CE,75,NIFTY 25 NOV 24000 CALL,2025-11-25 14:30:00,24000,CE,5,M,OPTIDX,,",
        "NSE,D,52177,OPTIDX,0,NIFTY-Nov2025-24000-PE,75,NIFTY 18 NOV 24000 PUT,2025-11-18 14:30:00,24000,PE,5,W,OPTIDX,,",
        "NSE,D,52178,OPTSTK,0,RELIANCE-Nov2025-1500-CE,500,RELIANCE 25 NOV 1500 CALL,2025-11-25 14:30:00,1500,CE,5,M,OPTSTK,,",
        "MCX,M,451669,FUTCOM,0,CRUDEOIL-Dec2025-FUT,100,CRUDEOIL DEC FUT,2025-12-18 23:30:00,-0.01,XX,100,M,FUTCOM,,",
        "BSE,D,1151,OPTIDX,0,SENSEX-Nov2025-84000-CE,20,SENSEX 20 NOV 84000 CALL,2025-11-20 14:30:00,84000,CE,5,W,OPTIDX,,",
        # Monthly SENSEX expiry, so the 20th reads as a weekly from the calendar alone.
        "BSE,D,1152,FUTIDX,0,SENSEX-Nov2025-FUT,20,SENSEX NOV FUT,2025-11-27 14:30:00,-0.01,XX,5,M,FUTIDX,,",
        "NSE,C,999,FUTCUR,0,USDINR-Nov2025-FUT,1,USDINR,2025-11-26 12:30:00,,XX,0.25,M,FUTCUR,,",
    ]
    return "\n".join([head, *rows])


def angel_rows():
    def r(token, symbol, name, kind, seg, expiry="", strike=-1, lot=1):
        return {"token": token, "symbol": symbol, "name": name, "expiry": expiry, "strike": f"{strike * 100:.6f}",
                "lotsize": str(lot), "instrumenttype": kind, "exch_seg": seg, "tick_size": "5.000000"}
    return [
        r("2885", "RELIANCE-EQ", "RELIANCE", "", "NSE"),
        r("99926000", "Nifty 50", "NIFTY", "AMXIDX", "NSE"),
        r("99926009", "Nifty Bank", "BANKNIFTY", "AMXIDX", "NSE"),
        r("52175", "NIFTY25NOV25FUT", "NIFTY", "FUTIDX", "NFO", "25NOV2025", -0.01, 75),
        r("52176", "NIFTY25NOV2524000CE", "NIFTY", "OPTIDX", "NFO", "25NOV2025", 24000, 75),
        r("52177", "NIFTY18NOV2524000PE", "NIFTY", "OPTIDX", "NFO", "18NOV2025", 24000, 75),
        r("52178", "RELIANCE25NOV251500CE", "RELIANCE", "OPTSTK", "NFO", "25NOV2025", 1500, 500),
        r("451669", "CRUDEOIL18DEC25FUT", "CRUDEOIL", "FUTCOM", "MCX", "18DEC2025", -0.01, 100),
        r("1151", "SENSEX20NOV2584000CE", "SENSEX", "OPTIDX", "BFO", "20NOV2025", 84000, 20),
        r("1152", "SENSEX27NOV25FUT", "SENSEX", "FUTIDX", "BFO", "27NOV2025", -0.01, 20),
    ]


def fyers_files():
    def line(token, details, kind, lot, tick, ticker, under, strike, opt, expiry=""):
        return ",".join([token, details, kind, str(lot), str(tick), "", "0915-1530|1815-1915:", "2025-11-14",
                         str(expiry), ticker, "10", "10", "0", under, "0", str(strike), opt, "0"])
    secs = lambda d: str(ms(d) // 1000)  # noqa: E731
    return {
        "NSE_CM": "\n".join([line("101000000002885", "RELIANCE INDUSTRIES LTD", "0", 1, 0.1, "NSE:RELIANCE-EQ", "RELIANCE", -1, "XX"),
                             line("101000000026000", "NIFTY 50", "10", 1, 0.05, "NSE:NIFTY50-INDEX", "NIFTY50", -1, "XX"),
                             line("101000000026009", "NIFTY BANK", "10", 1, 0.05, "NSE:NIFTYBANK-INDEX", "NIFTYBANK", -1, "XX")]),
        "BSE_CM": "",
        "NSE_FO": "\n".join([line("1", "NIFTY 25 Nov 25 FUT", "11", 75, 0.1, "NSE:NIFTY25NOVFUT", "NIFTY", -1, "XX", secs(MONTHLY)),
                             line("2", "NIFTY 25 Nov 25 24000 CE", "14", 75, 0.05, "NSE:NIFTY25NOV24000CE", "NIFTY", 24000, "CE", secs(MONTHLY)),
                             line("3", "NIFTY 25 Nov 18 24000 PE", "14", 75, 0.05, "NSE:NIFTY25N1824000PE", "NIFTY", 24000, "PE", secs(WEEKLY)),
                             line("4", "RELIANCE 25 Nov 25 1500 CE", "15", 500, 0.05, "NSE:RELIANCE25NOV1500CE", "RELIANCE", 1500, "CE", secs(MONTHLY))]),
        "BSE_FO": "\n".join([line("5", "SENSEX 25 Nov 20 84000 CE", "14", 20, 0.05, "BSE:SENSEX25N2084000CE", "SENSEX", 84000, "CE", secs(date(2025, 11, 20))),
                             line("6", "SENSEX 25 Nov FUT", "11", 20, 0.05, "BSE:SENSEX25NOVFUT", "SENSEX", -1, "XX", secs(date(2025, 11, 27)))]),
        "MCX_COM": line("7", "CRUDEOIL 25 Dec 18 FUT", "30", 100, 1, "MCX:CRUDEOIL25DECFUT", "CRUDEOIL", -1, "XX", secs(date(2025, 12, 18))),
    }


def groww_csv():
    head = ("exchange,exchange_token,trading_symbol,groww_symbol,name,instrument_type,segment,series,isin,"
            "underlying_symbol,underlying_exchange_token,expiry_date,strike_price,lot_size,tick_size,freeze_quantity")
    rows = [
        "NSE,2885,RELIANCE,NSE-RELIANCE,Reliance Industries,EQ,CASH,EQ,INE002A01018,,,,,1,0.1,",
        "NSE,NIFTY,NIFTY,NSE-NIFTY,Nifty 50,IDX,CASH,,,,,,,1,0.05,",
        "NSE,BANKNIFTY,BANKNIFTY,NSE-BANKNIFTY,Nifty Bank,IDX,CASH,,,,,,,1,0.05,",
        "NSE,52175,NIFTY25NOVFUT,NSE-NIFTY-25Nov25-FUT,NIFTY,FUT,FNO,,,NIFTY,26000,2025-11-25,,75,0.1,1800",
        "NSE,52176,NIFTY25NOV24000CE,NSE-NIFTY-25Nov25-24000-CE,NIFTY,CE,FNO,,,NIFTY,26000,2025-11-25,24000,75,0.05,1800",
        "NSE,52177,NIFTY25N1824000PE,NSE-NIFTY-18Nov25-24000-PE,NIFTY,PE,FNO,,,NIFTY,26000,2025-11-18,24000,75,0.05,1800",
        "NSE,52178,RELIANCE25NOV1500CE,NSE-RELIANCE-25Nov25-1500-CE,RELIANCE,CE,FNO,,,RELIANCE,2885,2025-11-25,1500,500,0.05,",
        "MCX,451669,CRUDEOIL25DECFUT,MCX-CRUDEOIL-18Dec25-FUT,CRUDEOIL,FUT,COMMODITY,,,CRUDEOIL,,2025-12-18,,100,1,",
        "BSE,1151,SENSEX25N2084000CE,BSE-SENSEX-20Nov25-84000-CE,SENSEX,CE,FNO,,,SENSEX,1,2025-11-20,84000,20,0.05,",
        "BSE,1152,SENSEX25NOVFUT,BSE-SENSEX-27Nov25-FUT,SENSEX,FUT,FNO,,,SENSEX,1,2025-11-27,,20,0.05,",
    ]
    return "\n".join([head, *rows])


def masters():
    files = fyers_files()
    return {
        "upstox": Master(upstox.contracts(upstox_rows())),
        "dhan": Master(dhan.contracts(dhan_csv())),
        "angelone": Master(angelone.contracts(angel_rows())),
        "fyers": Master(c for f in fyers.FILES for c in fyers.contracts(files[f], f)),
        "groww": Master(groww.contracts(groww_csv())),
    }


@pytest.mark.parametrize("broker", ["upstox", "dhan", "angelone", "fyers", "groww"])
def test_every_broker_file_maps_to_the_same_kite_symbols(broker):
    master = masters()[broker]
    assert set(master.by_symbol) >= EXPECTED, sorted(EXPECTED - set(master.by_symbol))
    option = master.get("NFO:NIFTY25NOV24000CE")
    assert (option["name"], option["instrument_type"], option["strike"], option["expiry_date"], option["lot_size"]) == \
        ("NIFTY", "CE", 24000.0, "2025-11-25", 75)
    assert master.get("NSE:RELIANCE")["instrument_type"] == "EQ"
    assert master.get("NSE:NIFTY 50")["segment"] == "INDICES"
    assert 0 < master.get("NSE:RELIANCE")["tick_size"] < 1          # paise turned into rupees where a broker uses them
    # The desk's F&O list asks for an index's options by underlying, and for futures by symbol.
    assert {r["tradingsymbol"] for r in master.search("NFO:NIFTY", "underlying")} == \
        {"NIFTY25NOVFUT", "NIFTY25NOV24000CE", "NIFTY25N1824000PE"}
    assert any(r["tradingsymbol"] == "NIFTY25NOVFUT" for r in master.search("FUT", "tradingsymbol"))
    assert all("_native" not in r for r in master.search("NIFTY", "id"))


def test_dhan_skips_segments_the_desk_does_not_trade():
    master = masters()["dhan"]
    assert not any(r["tradingsymbol"].startswith("USDINR") for r in master.rows)


def test_master_cache_loads_once_a_day():
    calls = []
    cache = MasterCache(lambda: calls.append(1) or [])
    cache.get("2025-11-14"), cache.get("2025-11-14"), cache.get("2025-11-15")
    assert len(calls) == 2


# -- the REST surface ---------------------------------------------------------

class FakeHTTP:
    """Answers by (method, url suffix); records what was sent."""

    def __init__(self, answers: dict):
        self.answers, self.sent = answers, []

    def __call__(self, method, url, **kw):
        self.sent.append((method, url, kw))
        for (m, suffix), answer in self.answers.items():
            if m == method and url.split("?")[0].endswith(suffix):
                return answer
        return 404, {"status": "error", "message": f"no fake for {method} {url}"}


def adapter(cls, contracts, answers, creds, tmp_path, token="tok"):
    http = FakeHTTP(answers)
    broker = cls(session_path=tmp_path / "sessions.json", http=http)
    broker.masters = MasterCache(lambda: contracts)
    broker.connect(creds)
    broker.token = token
    return broker, http


def test_upstox_order_and_positions_speak_kite(tmp_path):
    answers = {
        ("POST", "/v3/order/place"): (200, {"status": "success", "data": {"order_ids": ["250101000123"]}}),
        ("GET", "/v2/portfolio/short-term-positions"): (200, {"status": "success", "data": [
            {"instrument_token": "NSE_FO|3", "trading_symbol": "NIFTY2511250000CE", "exchange": "NFO", "quantity": -75,
             "average_price": 120.5, "last_price": 100.0, "pnl": 1500.0, "product": "D", "multiplier": 1}]}),
        ("GET", "/v2/market-quote/quotes"): (200, {"status": "success", "data": {
            "NSE_EQ:RELIANCE": {"instrument_token": "NSE_EQ|INE002A01018", "last_price": 1510.0, "net_change": 10.0,
                                "ohlc": {"open": 1500, "high": 1520, "low": 1495, "close": 1510}, "volume": 100,
                                "depth": {"buy": [{"price": 1509.9, "quantity": 5}], "sell": []}}}}),
    }
    broker, http = adapter(upstox.Upstox, list(upstox.contracts(upstox_rows())), answers,
                           {"api_key": "k", "api_secret": "s", "redirect_uri": "http://127.0.0.1/broker/callback"}, tmp_path)
    answer = broker.place_order({"variety": "regular", "exchange": "NFO", "tradingsymbol": "NIFTY25NOV24000CE",
                                 "transaction_type": "SELL", "quantity": 75, "product": "NRML", "order_type": "LIMIT",
                                 "price": 120.5, "validity": "DAY", "tag": "tradesetu"})
    assert answer["order_id"] == "250101000123"
    method, url, sent = http.sent[-1]
    assert url.startswith(upstox.ORDER_HOST)
    assert sent["json"]["instrument_token"] == "NSE_FO|3" and sent["json"]["product"] == "D"
    assert sent["headers"]["Authorization"] == "Bearer tok"
    [position] = broker.positions()
    assert (position["exchange"], position["tradingsymbol"], position["quantity"]) == ("NFO", "NIFTY25NOV24000CE", -75)
    quote = broker.quotes(["NSE:RELIANCE", "NSE:NOSUCH"])
    assert set(quote) == {"NSE:RELIANCE"}
    assert quote["NSE:RELIANCE"]["ohlc"]["close"] == 1500.0          # previous close from the day's change
    assert quote["NSE:RELIANCE"]["depth"]["buy"][0]["price"] == 1509.9
    assert broker.ltp(["NSE:RELIANCE"]) == {"NSE:RELIANCE": 1510.0}


def test_upstox_sign_in_needs_the_state_it_sent(tmp_path):
    answers = {("POST", "/v2/login/authorization/token"): (200, {"access_token": "new-token", "user_id": "AB1234"})}
    broker, http = adapter(upstox.Upstox, [], answers, {"api_key": "k", "api_secret": "s", "redirect_uri": "u"},
                           tmp_path, token=None)
    url = broker.login()["url"]
    state = url.split("state=")[1]
    with pytest.raises(KiteError, match="stale"):
        broker.complete_login({"code": "c", "state": "forged"})
    with pytest.raises(KiteError, match="stale"):
        broker.complete_login({"code": "c", "state": state})             # one use only: the forged try spent it
    state = broker.login()["url"].split("state=")[1]
    broker.complete_login({"code": "c", "state": state})
    assert broker.token == "new-token"
    assert http.sent[-1][2]["data"]["client_secret"] == "s"
    saved = json.loads((tmp_path / "sessions.json").read_text())
    assert saved["upstox"]["token"] == "new-token"


def test_missing_session_asks_for_login(tmp_path):
    broker, _ = adapter(dhan.Dhan, [], {}, {"client_id": "1", "access_token": "x"}, tmp_path, token=None)
    with pytest.raises(KiteError) as caught:
        broker.holdings()
    assert caught.value.needs_login


def test_expired_token_is_reported_as_needs_login(tmp_path):
    answers = {("GET", "/holdings"): (401, {"errorType": "Invalid_Authentication", "errorCode": "DH-901",
                                            "errorMessage": "Client ID or user generated access token is invalid or expired."})}
    broker, _ = adapter(dhan.Dhan, [], answers, {"client_id": "1", "access_token": "x"}, tmp_path)
    with pytest.raises(KiteError) as caught:
        broker.holdings()
    assert caught.value.needs_login and broker.token is None


def test_dhan_quotes_and_orders(tmp_path):
    answers = {
        ("POST", "/marketfeed/quote"): (200, {"status": "success", "data": {"NSE_FNO": {"52176": {
            "last_price": 150.0, "ohlc": {"open": 140, "high": 155, "low": 138, "close": 145}, "volume": 1000, "oi": 5000}}}}),
        ("POST", "/orders"): (200, {"orderId": "112111182198", "orderStatus": "PENDING"}),
        ("GET", "/orders"): (200, [{"orderId": "112111182198", "orderStatus": "TRADED", "exchangeSegment": "NSE_FNO",
                                    "securityId": "52176", "tradingSymbol": "NIFTY-Nov2025-24000-CE", "transactionType": "BUY",
                                    "quantity": 75, "filledQty": 75, "averageTradedPrice": 150.0, "productType": "MARGIN",
                                    "orderType": "LIMIT", "price": 150.0}]),
    }
    broker, http = adapter(dhan.Dhan, list(dhan.contracts(dhan_csv())), answers,
                           {"client_id": "1100", "access_token": "x"}, tmp_path, token="x")
    quote = broker.quotes(["NFO:NIFTY25NOV24000CE"])["NFO:NIFTY25NOV24000CE"]
    assert quote["last_price"] == 150.0 and quote["oi"] == 5000.0
    assert http.sent[-1][2]["json"] == {"NSE_FNO": [52176]}
    placed = broker.place_order({"exchange": "NFO", "tradingsymbol": "NIFTY25NOV24000CE", "transaction_type": "BUY",
                                 "quantity": 75, "product": "NRML", "order_type": "LIMIT", "price": 150.0, "tag": "tradesetu"})
    body = http.sent[-1][2]["json"]
    assert placed["order_id"] == "112111182198"
    assert (body["securityId"], body["exchangeSegment"], body["productType"]) == ("52176", "NSE_FNO", "MARGIN")
    [order] = broker.orders()
    assert (order["status"], order["tradingsymbol"], order["product"]) == ("COMPLETE", "NIFTY25NOV24000CE", "NRML")
    with pytest.raises(KiteError, match="index cannot"):
        broker.place_order({"exchange": "NSE", "tradingsymbol": "NIFTY 50", "transaction_type": "BUY", "quantity": 1})


def test_angel_one_signs_in_with_a_totp(tmp_path):
    answers = {("POST", "/loginByPassword"): (200, {"status": True, "message": "SUCCESS",
                                                    "data": {"jwtToken": "Bearer jwt", "refreshToken": "r", "feedToken": "f"}}),
               ("POST", "/order/v1/placeOrder"): (200, {"status": True, "data": {"orderid": "201020000000080"}})}
    broker, http = adapter(angelone.AngelOne, list(angelone.contracts(angel_rows())), answers,
                           {"api_key": "key", "client_code": "A123", "pin": "1111", "totp_secret": "JBSWY3DPEHPK3PXP"},
                           tmp_path, token=None)
    broker.login()
    sent = http.sent[-1][2]
    assert sent["json"]["clientcode"] == "A123" and len(sent["json"]["totp"]) == 6
    assert sent["headers"]["X-PrivateKey"] == "key" and "Authorization" not in sent["headers"]
    assert broker.token == "jwt"
    broker.place_order({"exchange": "NFO", "tradingsymbol": "NIFTY25N1824000PE", "transaction_type": "BUY",
                        "quantity": 75, "product": "MIS", "order_type": "LIMIT", "price": 80.0})
    body = http.sent[-1][2]["json"]
    assert (body["tradingsymbol"], body["symboltoken"], body["producttype"], body["quantity"]) == \
        ("NIFTY18NOV2524000PE", "52177", "INTRADAY", "75")


def test_fyers_candles_resample_to_sizes_it_lacks(tmp_path):
    from desk.brokers.instruments import _IST

    base = int(datetime(2025, 11, 14, 9, 15, tzinfo=_IST).timestamp())
    candles = [[base + 120 * i, 100 + i, 101 + i, 99 + i, 100.5 + i, 10] for i in range(4)]
    answers = {("GET", "/history"): (200, {"s": "ok", "candles": candles})}
    files = fyers_files()
    broker, http = adapter(fyers.Fyers, [c for f in fyers.FILES for c in fyers.contracts(files[f], f)], answers,
                           {"app_id": "APP-100", "secret_key": "s"}, tmp_path)
    rows = broker.candles("NSE:RELIANCE", "4m", datetime(2025, 11, 14, 9, 15), datetime(2025, 11, 14, 15, 30))
    assert http.sent[-1][2]["params"]["resolution"] == "2"           # the largest size Fyers has that makes 4
    assert http.sent[-1][2]["headers"]["Authorization"] == "APP-100:tok"
    assert [r["date"][11:16] for r in rows] == ["09:15", "09:19"]
    assert rows[0]["open"] == 100 and rows[0]["close"] == 101.5 and rows[0]["volume"] == 20


def test_groww_unwraps_payload_and_lists_orders(tmp_path):
    answers = {("GET", "/order/list"): (200, {"status": "SUCCESS", "payload": {"order_list": [
        {"groww_order_id": "GMK39038RDT490CCVRO", "trading_symbol": "RELIANCE", "exchange": "NSE", "segment": "CASH",
         "order_status": "EXECUTED", "quantity": 2, "filled_quantity": 2, "average_fill_price": 1500.0,
         "transaction_type": "BUY", "order_type": "LIMIT", "product": "CNC"}]}}),
               ("POST", "/order/create"): (200, {"status": "FAILURE", "error": {"code": "GA001", "message": "Insufficient funds"}})}
    broker, _ = adapter(groww.Groww, list(groww.contracts(groww_csv())), answers, {"access_token": "t"}, tmp_path)
    orders = broker.orders()
    assert {(o["tradingsymbol"], o["status"]) for o in orders} == {("RELIANCE", "COMPLETE")}
    with pytest.raises(KiteError, match="Insufficient funds"):
        broker.place_order({"exchange": "NSE", "tradingsymbol": "RELIANCE", "transaction_type": "BUY", "quantity": 1,
                            "price": 1500, "order_type": "LIMIT", "product": "CNC"})


def test_groww_needs_a_token_or_a_key():
    with pytest.raises(KiteError, match="access token"):
        groww.Groww().connect({})


def test_call_answers_the_kite_tools_the_desk_uses(tmp_path):
    broker, _ = adapter(groww.Groww, list(groww.contracts(groww_csv())), {}, {"access_token": "t"}, tmp_path)
    rows = broker.call("search_instruments", {"query": "NFO:NIFTY", "filter_on": "underlying"})
    assert {r["tradingsymbol"] for r in rows} == {"NIFTY25NOVFUT", "NIFTY25NOV24000CE", "NIFTY25N1824000PE"}
    assert "place_gtt_order" not in broker.tools and broker.can_trade
    with pytest.raises(KiteError):
        broker.call("place_gtt_order", {})


def test_totp_matches_rfc_6238():
    # RFC 6238 appendix B, SHA-1 key "12345678901234567890".
    assert totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", at=59, digits=8) == "94287082"
    assert totp("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", at=1111111109, digits=8) == "07081804"


# -- the switch, settings and the app -----------------------------------------

def test_switch_forwards_to_the_chosen_broker():
    switch = Switch()
    assert switch.name == "kite" and switch.status()["broker_status"] == "supported"
    switch.use("upstox")
    assert switch.status()["broker"] == "upstox" and switch.status()["broker_status"] == "experimental"
    assert not switch.connected
    with pytest.raises(KiteError):
        switch.use("nosuch")
    assert [b["name"] for b in catalog()] == list(BROKERS)


def test_settings_keep_broker_secrets_masked(tmp_path):
    from desk.settings import Settings

    settings = Settings(tmp_path / "settings.json")
    settings.update({"broker": "upstox", "broker_keys": {"upstox": {"api_key": "abc", "api_secret": "supersecret1234"}}})
    public = settings.public()["broker_keys"]["upstox"]
    assert public["api_key"] == "abc" and public["api_secret"].endswith("1234") and "supersecret" not in public["api_secret"]
    settings.update({"broker_keys": {"upstox": {"api_secret": public["api_secret"], "api_key": "abc"}}})
    assert settings.get("broker_keys")["upstox"]["api_secret"] == "supersecret1234"   # masked value sent back: kept
    with pytest.raises(ValueError):
        settings.update({"broker": "nosuch"})
    with pytest.raises(ValueError):
        settings.update({"broker_keys": {"upstox": {"password": "x"}}})


def test_app_connects_the_chosen_broker_and_rejects_forged_callbacks(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from desk.server import create_app
    from desk.settings import Settings

    monkeypatch.setenv("TRADESETU_HOME", str(tmp_path))
    settings = Settings(tmp_path / "settings.json")
    settings.update({"broker": "fyers"})
    app = create_app(settings=settings, fetch=lambda *a, **k: None, token="t")
    client = TestClient(app)
    headers = {"x-desk-token": "t"}
    assert client.get("/api/state", headers=headers).json()["kite"]["broker"] == "fyers"
    refused = client.post("/api/kite/connect", headers=headers)
    assert refused.status_code == 502 and "App ID" in refused.json()["error"]
    settings.update({"broker_keys": {"fyers": {"app_id": "APP-100", "secret_key": "s"}}})
    status = client.post("/api/kite/connect", headers=headers).json()
    assert status["connected"] and status["broker"] == "fyers" and not status["signed_in"]
    assert app.state.kite.active.creds["redirect_uri"] == "http://testserver/broker/callback"
    login = client.post("/api/kite/login", headers=headers).json()
    assert "generate-authcode" in login["url"] and "broker%2Fcallback" in login["url"]
    forged = client.get("/broker/callback?s=ok&auth_code=x&state=fyers-forged")
    assert forged.status_code == 400 and "stale" in forged.text
    assert client.get("/api/brokers", headers=headers).json()["active"] == "fyers"
