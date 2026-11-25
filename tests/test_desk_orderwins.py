"""Order wins: reading NSE and BSE order filings, sizing them, and the evening and morning Telegram reports."""

import copy
from datetime import datetime

import pytest

import tradingagents.dataflows.config as config_module
from desk import market, orderwins
from desk.notify import Notifier
from desk.orderwins import (
    OrderWins,
    counterparty,
    is_order_win,
    merge,
    order_value,
    parse_ai,
    parse_bse,
    parse_market_cap,
    parse_nse,
    session_of,
    size_band,
)
from desk.settings import Settings

pytestmark = pytest.mark.unit
IST = market.IST


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("BHARAT_DESK_HOME", str(tmp_path / "desk"))
    monkeypatch.delenv("DESK_EXCHANGE_PROXY", raising=False)
    saved = copy.deepcopy(config_module._config)
    config_module.set_config({"default_exchange": "NSE"})
    market.set_holidays([])
    yield
    config_module._config = saved
    market.set_holidays([])


# -- reading one filing ------------------------------------------------------------------

@pytest.mark.parametrize("text, value, customer", [
    ("Atlanta Electricals Limited received a Letter of Intent from Transmission Corporation of Andhra Pradesh "
     "Limited for an order worth Rs 523.21 crore net of GST.", 523.21,
     "Transmission Corporation of Andhra Pradesh Limited"),
    ("Diamond Power Infrastructure Limited received two purchase orders from Adani Energy Solutions Limited. The "
     "aggregate value is Rs. 488.18 crore including GST, or Rs. 413.71 crore excluding GST. This follows an earlier "
     "order of Rs. 707.80 crore, bringing the total orders to approximately Rs. 1,196 crore.", 413.71,
     "Adani Energy Solutions Limited"),
    ("Intimation for receipt of Order aggregating to Rs. 523.21 Crore", 523.21, None),
    ("Company bags order worth USD 12.5 million from Siemens AG", 110.0, "Siemens AG"),
    ("Receipt of order of Rs 45 lakhs from Indian Railways", 0.45, "Indian Railways"),
    ("Order worth &#8377;1,250 Cr from NHAI for a highway", 1250.0, "NHAI"),
    ("LoA from South Central Railway for Rs 120 crore", 120.0, "South Central Railway"),
    ("Order of Rs. 5,40,00,000 received from Ministry of Defence", 5.4, "Ministry of Defence"),
    ("Received an order. Our order book now stands at Rs 12,000 crore.", None, None),
])
def test_the_value_and_the_customer_come_from_the_text(text, value, customer):
    assert order_value(text) == value
    assert counterparty(text) == customer


@pytest.mark.parametrize("text, win", [
    ("Intimation for receipt of Order aggregating to Rs. 523.21 Crore", True),
    ("Received LoA from NHAI", True),
    ("Executed a Power Purchase Agreement with NTPC Limited", True),
    ("Disclosure under Regulation 30 for Order issued by CESTAT, Kolkata", False),
    ("Received an order from the Deputy Commissioner for Rs. 2,97,67,691 under Section 73 of the CGST Act", False),
    ("Termination of the contract with the Railway Board", False),
    ("The BRONZE MEDAL has been awarded to our Premium Gin", False),
])
def test_tax_court_and_cancelled_orders_are_not_order_wins(text, win):
    assert is_order_win(text) is win


def test_when_a_filing_reaches_the_market():
    market.set_holidays(["2026-11-24"])
    during = session_of(datetime(2026, 11, 25, 11, 0))
    assert during == {"timing": "During market hours", "early": False, "reacts_on": "2026-11-25"}
    assert session_of(datetime(2026, 11, 25, 8, 40)) == {"timing": "Before the open", "early": True,
                                                         "reacts_on": "2026-11-25"}
    assert session_of(datetime(2026, 11, 25, 15, 30))["timing"] == "After the close"
    friday = session_of(datetime(2026, 11, 27, 19, 45))
    assert friday == {"timing": "After the close", "early": True, "reacts_on": "2026-11-30"}   # Monday
    holiday = session_of(datetime(2026, 11, 24, 12, 0))
    assert holiday == {"timing": "On a market holiday", "early": True, "reacts_on": "2026-11-25"}


NSE_ROWS = [
    {"symbol": "ATLANTAELE", "seq_id": "106870852", "sm_name": "Atlanta Electricals Limited", "sm_isin": "INE0Z4F01028",
     "sort_date": "2026-11-25 19:30:45", "desc": "Bagging/Receiving of orders/contracts",
     "attchmntText": "Atlanta Electricals Limited has informed the Exchange about Bagging/Receiving of orders/contracts",
     "voiceSummary": "Atlanta Electricals Limited received a Letter of Intent from Transmission Corporation of Andhra "
                     "Pradesh Limited for an order worth Rs 523.21 crore net of GST.",
     "attchmntFile": "https://nsearchives.nseindia.com/corporate/ATL.pdf"},
    {"symbol": "TINYCO", "seq_id": "2", "sm_name": "Tiny Co Limited", "sort_date": "2026-11-25 11:05:00",
     "desc": "Bagging/Receiving of orders/contracts", "attchmntText": "Tiny Co bags order worth Rs 2 crore from NHAI"},
    {"symbol": "TAXCO", "seq_id": "3", "sm_name": "Tax Co Limited", "sort_date": "2026-11-25 17:00:00",
     "desc": "Bagging/Receiving of orders/contracts",
     "attchmntText": "Tax Co has received an order from CESTAT setting aside a demand of Rs 30 crore"},
]
BSE_ROWS = {"Table": [
    {"SCRIP_CD": 544527, "NEWSID": "9305", "SLONGNAME": "Atlanta Electricals Ltd", "DT_TM": "2026-11-25T19:28:10.29",
     "HEADLINE": "Intimation for receipt of Order aggregating to Rs. 523.21 Crore", "MORE": "",
     "NSURL": "https://www.bseindia.com/stock-share-price/atlanta-electricals-ltd/atlantaele/544527/",
     "ATTACHMENTNAME": "ecfc.pdf", "SUBCATNAME": "Award of Order / Receipt of Order"},
    {"SCRIP_CD": 543210, "NEWSID": "7777", "SLONGNAME": "Mystery Works Ltd", "DT_TM": "2026-11-25T20:10:00",
     "HEADLINE": "Receipt of work order", "MORE": "",
     "NSURL": "https://www.bseindia.com/stock-share-price/mystery-works-ltd/mystery/543210/", "ATTACHMENTNAME": "m.pdf"},
], "Table1": [{"ROWCNT": 2}]}


def test_nse_and_bse_lists_are_read_and_the_same_order_is_kept_once():
    nse, bse = parse_nse(NSE_ROWS), parse_bse(BSE_ROWS)
    assert nse[0]["symbol"] == "ATLANTAELE.NS" and "523.21" in nse[0]["text"]
    assert bse[0]["symbol"] == "ATLANTAELE.NS" and bse[0]["pdf"].endswith("/AttachLive/ecfc.pdf")
    merged = merge(nse + bse)
    atlanta = [m for m in merged if m["company"].startswith("Atlanta")]
    assert len(atlanta) == 1 and atlanta[0]["exchange"] == "NSE+BSE"
    assert atlanta[0]["filed_at"] == "2026-11-25 19:28:10"            # the earlier of the two filings
    assert len(merged) == 4


def test_size_against_the_company():
    answer = {"equityResponse": [{"tradeInfo": {"totalMarketCap": 106755584487.5, "issuedSize": 76896625},
                                  "orderBook": {"lastPrice": 1390.2}}]}
    assert parse_market_cap(answer) == 10675.56
    assert parse_market_cap({"equityResponse": [{"tradeInfo": {"issuedSize": 1e7}, "orderBook": {"lastPrice": 100}}]}) == 100.0
    assert parse_market_cap({}) is None
    assert size_band(30, 0.1) == "Very big"            # revenue decides when it is known
    assert size_band(None, 4.9) == "Big" and size_band(None, 0.2) == "Small" and size_band(None, None) == "Unknown"


def test_the_ai_answer_is_read_defensively():
    reply = 'Sure: {"order_win": true, "value_crore": "155", "customer": "NTPC", "summary": "A 25-year PPA."}'
    assert parse_ai(reply) == {"order_win": True, "value_cr": 155.0, "customer": "NTPC", "summary": "A 25-year PPA."}
    assert parse_ai("no json here") is None
    assert parse_ai('{"order_win": false, "value_crore": null}')["order_win"] is False


# -- the scanner -------------------------------------------------------------------------

class FakeNse:
    def __init__(self):
        self.down = False
        self.calls = []

    def json(self, path, params):
        self.calls.append(path)
        if self.down:
            raise RuntimeError("NSE did not answer (connection refused)")
        if path == "corporate-announcements":
            assert params["subject"] == orderwins.NSE_SUBJECT
            return NSE_ROWS
        if path == orderwins.NSE_QUOTE:
            caps = {"ATLANTAELE": 10675.56e7, "TINYCO": 2000e7}
            if params["symbol"] not in caps:
                raise RuntimeError("no such symbol")
            return {"equityResponse": [{"tradeInfo": {"totalMarketCap": caps[params["symbol"]]}}]}
        raise AssertionError(path)

    def file(self, url):
        return b"%PDF"


def scanner(tmp_path, ask=None, chat="123456789"):
    settings = Settings(tmp_path / "settings.json")
    settings.update({"api_keys": {"TELEGRAM_BOT_TOKEN": "123:abc"}, "telegram_chat_id": chat})
    messages = []
    moment = {"now": datetime(2026, 11, 25, 18, 59, tzinfo=IST), "wall": 1_800_000_000.0}
    notifier = Notifier(settings, path=tmp_path / "alerts.jsonl", send=lambda t, c, text: messages.append(text),
                        now=lambda: moment["now"])
    nse = FakeNse()
    bse = {"down": False}

    def bse_json(url, params):
        if bse["down"]:
            raise RuntimeError("BSE answered 403")
        return BSE_ROWS

    wins = OrderWins(settings, notifier, nse=nse, bse_json=bse_json, download=lambda url: b"%PDF", ask=ask,
                     now=lambda: moment["now"], wall=lambda: moment["wall"], path=tmp_path / "wins.json",
                     background=False)
    return settings, wins, messages, moment, nse, bse


def test_the_evening_report_lists_after_close_orders_biggest_first(tmp_path, monkeypatch):
    monkeypatch.setattr(orderwins, "pdf_text", lambda data: "Mystery Works received a work order worth Rs 80 crore "
                                                            "from Bharat Heavy Electricals Limited")
    settings, wins, messages, moment, nse, _ = scanner(tmp_path)
    assert settings.get("order_scan") and settings.get("order_scan_evening") == "19:00"
    moment["now"] = moment["now"].replace(hour=10)
    wins.tick()                                          # the half-hourly screen refresh: no Telegram
    assert messages == [] and wins.view()["rows"]
    moment["now"], moment["wall"] = datetime(2026, 11, 25, 19, 0, tzinfo=IST), moment["wall"] + 9 * 3600
    wins.tick()
    assert len(messages) == 1
    text = messages[0]
    assert "for Thu 26 Nov's session" in text
    assert "ATLANTAELE ₹523 cr from Transmission Corporation of Andhra Pradesh Limited" in text
    assert "4.9% of market value" in text and "Big" in text
    assert "Filed during market hours" not in text       # Tiny Co's order is 0.1% of its value: left out
    assert "TAXCO" not in text                           # a tax order, not an order win
    assert "MYSTERY" in text and "₹80 cr" in text         # value read from the PDF
    view = wins.view()
    mystery = next(r for r in view["rows"] if r["company"].startswith("Mystery"))
    assert mystery["read_by"] == "PDF" and mystery["customer"] == "Bharat Heavy Electricals Limited"
    assert view["hidden"] == 1                           # the tax order, shown only with all=True
    assert len(wins.view(everything=True)["rows"]) == 4
    moment["wall"] += 3600
    wins.tick()
    assert len(messages) == 1                            # once a day
    # The next morning: nothing new overnight, nothing sent.
    moment["now"], moment["wall"] = datetime(2026, 11, 26, 8, 30, tzinfo=IST), moment["wall"] + 13 * 3600
    wins.tick()
    assert len(messages) == 1 and wins.view()["last_report"]["error"] == "no new order wins"


def test_the_smallest_order_to_send_can_be_zero(tmp_path):
    settings, wins, messages, moment, _, _ = scanner(tmp_path)
    settings.update({"order_scan_min_pct": 0})
    moment["now"] = datetime(2026, 11, 25, 19, 5, tzinfo=IST)
    wins.tick()
    assert "Filed during market hours" in messages[0] and "TINYCO ₹2.00 cr from NHAI" in messages[0]


def test_laptop_proxy_off_warns_once_and_retries(tmp_path):
    settings, wins, messages, moment, nse, bse = scanner(tmp_path)
    nse.down, bse["down"] = True, True
    moment["now"] = datetime(2026, 11, 25, 19, 0, tzinfo=IST)
    wins.tick()
    assert len(messages) == 1 and "could not reach NSE or BSE" in messages[0] and "until 23:30" in messages[0]
    moment["wall"] += 60
    wins.tick()                                           # waits RETRY_MINUTES before trying again
    assert len(nse.calls) == 1
    moment["wall"] += orderwins.RETRY_MINUTES * 60
    wins.tick()
    assert len(messages) == 1                             # still down: no second warning
    nse.down, bse["down"] = False, False
    moment["wall"] += orderwins.RETRY_MINUTES * 60
    moment["now"] = moment["now"].replace(hour=19, minute=31)
    wins.tick()
    assert len(messages) == 2 and "ATLANTAELE" in messages[1]
    assert wins.view()["note"] is None


def test_one_exchange_down_still_reports_the_other(tmp_path):
    settings, wins, messages, moment, nse, bse = scanner(tmp_path)
    bse["down"] = True
    moment["now"] = datetime(2026, 11, 25, 19, 0, tzinfo=IST)
    wins.tick()
    assert len(messages) == 1 and "ATLANTAELE" in messages[0]
    assert "BSE: BSE answered 403" in wins.view()["note"]


def test_the_ai_reads_only_unclear_filings_and_can_be_off(tmp_path, monkeypatch):
    monkeypatch.setattr(orderwins, "pdf_text", lambda data: "")       # a scanned PDF
    asked = []

    def ask(prompt):
        asked.append(prompt)
        return '{"order_win": true, "value_crore": 75, "customer": "Coal India", "summary": "Conveyor work."}'

    settings, wins, messages, moment, _, _ = scanner(tmp_path, ask=ask)
    moment["now"] = datetime(2026, 11, 25, 19, 0, tzinfo=IST)
    wins.tick()
    assert len(asked) == 1 and "Mystery Works" in asked[0]           # the only filing with no value in its text
    mystery = next(r for r in wins.view()["rows"] if r["company"].startswith("Mystery"))
    assert mystery["value_cr"] == 75 and mystery["read_by"] == "AI" and mystery["summary"] == "Conveyor work."

    tmp = tmp_path / "off"
    tmp.mkdir()
    asked.clear()
    _, off, _, moment2, _, _ = scanner(tmp, ask=ask)
    off._llm_problem = lambda: "AI reading is off"
    moment2["now"] = datetime(2026, 11, 25, 19, 0, tzinfo=IST)
    off.tick()
    assert asked == []


def test_no_telegram_chat_still_keeps_the_screen(tmp_path):
    settings, wins, messages, moment, _, _ = scanner(tmp_path, chat="")
    moment["now"] = datetime(2026, 11, 25, 19, 0, tzinfo=IST)
    wins.tick()
    assert messages == [] and wins.view()["last_report"]["error"] == "no Telegram chat linked"
    assert any(r["symbol"] == "ATLANTAELE.NS" for r in wins.view()["rows"])


def test_switched_off_does_nothing(tmp_path):
    settings, wins, messages, moment, nse, _ = scanner(tmp_path)
    settings.update({"order_scan": False})
    moment["now"] = datetime(2026, 11, 25, 19, 0, tzinfo=IST)
    wins.tick()
    assert messages == [] and nse.calls == []


def test_report_times_are_validated(tmp_path):
    settings = Settings(tmp_path / "settings.json")
    with pytest.raises(ValueError, match="15:30 and 23:00"):
        settings.update({"order_scan_evening": "14:00"})
    with pytest.raises(ValueError, match="before the open"):
        settings.update({"order_scan_morning": "09:30"})
    with pytest.raises(ValueError, match="between 0% and 100%"):
        settings.update({"order_scan_min_pct": -1})
    settings.update({"order_scan_evening": "20:15", "order_scan_morning": "07:45", "order_scan_min_pct": 2})
    assert settings.get("order_scan_evening") == "20:15" and settings.get("order_scan_min_pct") == 2.0


def test_the_order_wins_api(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from desk.server import create_app

    app = create_app(settings=Settings(tmp_path / "s.json"), fetch=lambda s, interval="1d": None)
    client = TestClient(app)
    headers = {"X-Desk-Token": app.state.token}
    view = client.get("/api/order-wins", headers=headers).json()
    assert view["rows"] == [] and view["enabled"] and view["evening"] == "19:00"
    started = client.post("/api/order-wins/scan", headers=headers).json()
    assert "started" in started
