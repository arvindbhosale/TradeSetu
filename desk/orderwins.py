"""Order wins: the work orders and contracts companies tell NSE and BSE they have received.

A listed company must tell the exchanges when it wins a sizeable order (SEBI LODR Regulation 30). NSE files
these under "Bagging/Receiving of orders/contracts" and BSE under "Award of Order / Receipt of Order". The
desk reads both lists, keeps the real order wins (not tax orders, court orders or cancellations, which the
companies file under the same heading), reads the order value and the customer from the filing, and ranks
each order against the size of the company.

When it was filed matters more than anything else. A filing during market hours is in the price within
minutes; one filed after 15:30, before 09:15 or on a holiday is news at the next open. So every filing
carries the session it first reaches, and the two Telegram reports go out before that session:

- **Evening** (19:00 IST by default, every day, holidays too): what was filed since the last report.
- **Morning** (08:30 IST on trading days): what was filed overnight, before the open.

NSE and BSE refuse many cloud servers. Requests go through ``DESK_EXCHANGE_PROXY`` when it is set (a proxy
on an Indian home connection); when that computer is off the scan fails, says so once on Telegram and tries
again every 15 minutes until the report window closes. BSE is read with a browser's TLS fingerprint
(``curl_cffi``) when that package is installed, since BSE turns away plain Python clients.

The value comes from the filing's text first: NSE's own summary of the PDF, BSE's headline. When neither
names a value, the PDF is read; when the text still leaves the value or the customer unclear and an AI
model is set in Settings, the model reads it (a few filings per scan, so a scan stays cheap).
"""

from __future__ import annotations

import contextlib
import html
import io
import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from tradingagents.dataflows.symbols import normalize_symbol

from . import market
from .settings import home

logger = logging.getLogger(__name__)

NSE_SUBJECT = "Bagging/Receiving of orders/contracts"
BSE_API = "https://api.bseindia.com/BseIndiaAPI/api/AnnSubCategoryGetData/w"
BSE_SUBCATEGORY = "Award of Order / Receipt of Order"
BSE_PDF = "https://www.bseindia.com/xml-data/corpfiling/AttachLive/"
BSE_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                             "Chrome/131.0 Safari/537.36",
               "Accept": "application/json, text/plain, */*", "Origin": "https://www.bseindia.com",
               "Referer": "https://www.bseindia.com/"}
NSE_QUOTE = "NextApi/apiClient/GetQuoteApi"

OPEN, CLOSE = (9, 15), (15, 30)
KEEP_DAYS = 45                    # filings kept for the screen
LOOKBACK_DAYS = 3                 # each scan reads this many calendar days back (a long weekend)
RETRY_MINUTES = 15
REFRESH_MINUTES = 30              # the screen's own refresh between the two reports
EVENING_UNTIL = (23, 30)          # an evening report not sent by then is skipped
MORNING_UNTIL = (9, 10)
MAX_PDFS = 25                     # PDFs read per scan
MAX_AI = 20                       # filings sent to the AI model per scan
CAP_SECONDS = 20 * 3600           # market capitalisation is fetched again after this long
TELEGRAM_CHUNK = 3500
MAX_LISTED = 15                   # orders listed in one Telegram report
REPORT_HOURS = 30                 # a report covers filings up to this old that were not reported yet

# Rough rupee rates for orders stated in other currencies; good enough to size an order.
FX = {"USD": 88.0, "EUR": 102.0, "GBP": 117.0, "AED": 24.0, "SAR": 23.5, "JPY": 0.58, "AUD": 57.0,
      "CAD": 63.0, "SGD": 68.0, "CHF": 110.0, "QAR": 24.0, "OMR": 228.0, "KWD": 287.0}

# Filed under "order" but not an order win.
NOT_A_WIN = re.compile(
    r"\b(cestat|appellate|tribunal|nclt|nclat|show[- ]cause|penalt\w*|court|arbitra\w*|order[- ]in[- ]original|"
    r"assessment order|demand notice|tax demand|demand of|section\s+7[34]\b|cgst act|income[- ]tax|customs|"
    r"terminat\w*|cancel\w*|rescind\w*|rescission|short[- ]clos\w*|withdraw\w*|foreclos\w*|"
    r"sebi order|adjudicat\w*|recovery order|attachment|summons)", re.I)
WIN = re.compile(r"\b(orders?|contracts?|lo[ai]s?|letters? of (award|acceptance|intent)|work orders?|purchase orders?|"
                 r"tenders?|l-?1|bagg\w*|bags|(power|supply|service|offtake) (purchase )?agreements?|ppa|"
                 r"empanel\w*|mandate)\b", re.I)

_NUM = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_UNIT = r"(crores?|cr\b\.?|lakhs?|lacs?|lkh|million|mn\b|mio\b|billion|bn\b|thousand|k\b)?"
INR_AMOUNT = re.compile(r"(?:rs\.?|inr|₹|rupees)\s*" + _NUM + r"\s*" + _UNIT, re.I)
BARE_CRORE = re.compile(_NUM + r"\s*(crores?|cr\b\.?)", re.I)
FX_AMOUNT = re.compile(r"(usd|us\s?\$|\$|eur|€|euro|gbp|£|aed|sar|jpy|aud|cad|sgd|chf|qar|omr|kwd)\s*" + _NUM
                       + r"\s*" + _UNIT, re.I)
FX_NAMES = {"US$": "USD", "US $": "USD", "$": "USD", "€": "EUR", "EURO": "EUR", "£": "GBP"}
NOT_THIS_ORDER = re.compile(r"(order\s*book|order\s*inflow|earlier|previous|total|cumulative|aggregate order|"
                            r"so far|till date|to date|year[- ]to[- ]date|ytd|revenue|turnover|market cap|"
                            r"bringing|taking)[^.;]{0,40}$", re.I)
EX_TAX = re.compile(r"^[^.;]{0,30}(excluding|exclusive of|excl\.?|net of|without|plus|\+)\s*(gst|taxes|tax)", re.I)
WITH_TAX = re.compile(r"^[^.;]{0,30}(including|inclusive of|incl\.?|with)\s*(gst|taxes|tax)", re.I)
_SUFFIX = (r"(?:Limited|Ltd\.?|Corporation|Corp\.?|Authority|Board|Company|Railways?|Ministry|Government|Department|"
           r"Inc\.?|LLC|GmbH|PLC|plc|S\.A\.|AG|Pvt\.? Ltd\.?|Private Limited|Nigam|Mandal|Council|Commission|Agency|"
           r"Organisation|Organization|Navy|Army|Air Force|Undertaking|Bank|Trust|University|Institute)")
PARTY = re.compile(
    r"\b(?:from|by|with)\s+(?:m/s\.?\s*|the\s+)?((?:(?:[A-Z][\w&.'()/-]*|of|and|&|the)\s+){0,10}"
    + _SUFFIX + r"(?:\s+of\s+(?:[A-Z][\w&.'-]*\s?){1,4})?(?:\s+(?:Limited|Ltd\.?))?)", re.U)
PARTY_SHORT = re.compile(r"\b(?:from|by)\s+(?:the\s+)?([A-Z][A-Z&]{2,9}(?:\s+[A-Z][A-Za-z]+){0,2})\b")


# -- reading one filing ---------------------------------------------------------------------------------------

def clean_text(text) -> str:
    return " ".join(html.unescape(str(text or "")).replace("₹", "₹").split())


def is_order_win(text: str) -> bool:
    """True for an order or contract won; False for tax, court or penalty orders and cancellations."""
    text = clean_text(text)
    return bool(WIN.search(text)) and not NOT_A_WIN.search(text)


def _number(raw: str) -> float:
    return float(raw.replace(",", ""))


def _crore(value: float, unit: str | None, inr: bool = True) -> float:
    unit = (unit or "").lower().rstrip(".")
    if unit.startswith("cr"):
        return value
    if unit.startswith(("lakh", "lac", "lkh")):
        return value / 100
    if unit in ("million", "mn", "mio"):
        return value / 10
    if unit in ("billion", "bn"):
        return value * 100
    if unit in ("thousand", "k"):
        return value / 1e4
    return value / 1e7 if inr else value / 1e7          # plain rupees (or plain units of a currency)


def amounts(text: str) -> list[dict]:
    """Every amount in the text, as {"crore", "start", "end", "currency", "context"}, in order."""
    text = clean_text(text)
    out, taken = [], []
    for pattern, inr in ((INR_AMOUNT, True), (FX_AMOUNT, False), (BARE_CRORE, True)):
        for m in pattern.finditer(text):
            if any(a <= m.start() < b for a, b in taken):
                continue
            if inr:
                number, unit, currency = m.group(1), m.group(2), "INR"
            else:
                code = m.group(1).upper().replace(" ", "")
                currency = FX_NAMES.get(code, FX_NAMES.get(m.group(1).upper(), code))
                currency = "USD" if currency.startswith("US") else currency
                number, unit = m.group(2), m.group(3)
            try:
                value = _number(number)
            except ValueError:
                continue
            if not unit and inr and pattern is INR_AMOUNT and value < 1e5:
                continue                     # "Rs. 10" (face value, a share price) is not an order
            crore = _crore(value, unit) * (FX.get(currency, 0) if not inr else 1)
            if crore <= 0:
                continue
            taken.append((m.start(), m.end()))
            out.append({"crore": round(crore, 2), "start": m.start(), "end": m.end(), "currency": currency,
                        "before": text[max(0, m.start() - 60):m.start()], "after": text[m.end():m.end() + 60]})
    out.sort(key=lambda a: a["start"])
    return out


def order_value(text: str) -> float | None:
    """The value of this order in ₹ crore, or None when the text does not say.

    Skips amounts that describe something else (the order book, an earlier order, a running total, revenue);
    when the same order is given with and without GST, takes the value without it.
    """
    found = [a for a in amounts(text) if not NOT_THIS_ORDER.search(a["before"])]
    if not found:
        return None
    for a in found:
        if EX_TAX.search(a["after"]):
            return a["crore"]
    first = found[0]
    if WITH_TAX.search(first["after"]) and len(found) > 1 and found[1]["crore"] < first["crore"] \
            and found[1]["start"] - first["end"] < 80:
        return found[1]["crore"]             # "Rs 488 crore including GST (Rs 414 crore excluding GST)"
    return first["crore"]


def counterparty(text: str) -> str | None:
    text = clean_text(text)
    for m in PARTY.finditer(text):
        name = m.group(1).strip(" ,.")
        words = name.split()
        if 1 < len(words) <= 14 and not re.match(r"(?i)(the )?(company|exchange|board of directors)\b", name):
            return name[:120]
    for m in PARTY_SHORT.finditer(text):
        name = m.group(1).strip()
        if name.split()[0] not in ("NSE", "BSE", "SEBI", "LODR", "GST", "INR", "USD", "EUR"):
            return name[:120]
    return None


def company_key(name: str) -> str:
    """One spelling of a company name for matching NSE and BSE filings."""
    text = re.sub(r"[^a-z0-9]", "", clean_text(name).lower().replace("&", "and"))
    for tail in ("limited", "ltd", "india", "pvt", "private"):
        text = text.removesuffix(tail)
    return text


# -- timing --------------------------------------------------------------------------------------------------

def session_of(filed: datetime, is_trading_day=market.is_trading_day) -> dict:
    """When a filing reaches the market: {"timing", "early", "reacts_on"}.

    early is True when the filing came out while the market was shut (before 09:15, after 15:30, or on a
    weekend or holiday), so the next open is the first chance to trade on it.
    """
    day = filed.replace(hour=0, minute=0, second=0, microsecond=0)
    hm = (filed.hour, filed.minute)
    if is_trading_day(filed) and hm < OPEN:
        return {"timing": "Before the open", "early": True, "reacts_on": day.strftime("%Y-%m-%d")}
    if is_trading_day(filed) and hm < CLOSE:
        return {"timing": "During market hours", "early": False, "reacts_on": day.strftime("%Y-%m-%d")}
    nxt = day + timedelta(days=1)
    for _ in range(15):
        if is_trading_day(nxt):
            break
        nxt += timedelta(days=1)
    timing = "After the close" if is_trading_day(filed) else "On a market holiday"
    return {"timing": timing, "early": True, "reacts_on": nxt.strftime("%Y-%m-%d")}


# -- the two exchanges' lists ---------------------------------------------------------------------------------

def _when(text: str, *formats: str) -> datetime | None:
    text = str(text or "").strip()
    for fmt in formats:
        with contextlib.suppress(ValueError):
            return datetime.strptime(text[:len(datetime(2000, 1, 1).strftime(fmt))], fmt)
    return None


def parse_nse(rows) -> list[dict]:
    rows = rows.get("data") if isinstance(rows, dict) else rows
    out = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or not row.get("symbol"):
            continue
        filed = _when(row.get("sort_date"), "%Y-%m-%d %H:%M:%S") or _when(row.get("an_dt"), "%d-%b-%Y %H:%M:%S")
        if filed is None:
            continue
        summary = clean_text(row.get("voiceSummary"))
        brief = clean_text(row.get("attchmntText"))
        out.append({"id": f"NSE:{row.get('seq_id') or row['symbol'] + filed.isoformat()}", "exchange": "NSE",
                    "symbol": normalize_symbol(f"NSE:{str(row['symbol']).strip().upper()}"),
                    "company": clean_text(row.get("sm_name")), "isin": str(row.get("sm_isin") or ""),
                    "filed_at": filed.strftime("%Y-%m-%d %H:%M:%S"), "subject": clean_text(row.get("desc")),
                    "headline": brief, "text": summary or brief, "pdf": str(row.get("attchmntFile") or "")})
    return out


def parse_bse(answer) -> list[dict]:
    rows = answer.get("Table") if isinstance(answer, dict) else None
    out = []
    for row in rows or []:
        if not isinstance(row, dict) or not row.get("SCRIP_CD"):
            continue
        filed = _when(row.get("DT_TM") or row.get("NEWS_DT"), "%Y-%m-%dT%H:%M:%S")
        if filed is None:
            continue
        slug = [p for p in str(row.get("NSURL") or "").split("/") if p]
        scrip_id = slug[-2].upper() if len(slug) >= 2 and not slug[-2].isdigit() else ""
        headline = clean_text(row.get("HEADLINE"))
        more = clean_text(row.get("MORE"))
        attachment = str(row.get("ATTACHMENTNAME") or "").strip()
        out.append({"id": f"BSE:{row.get('NEWSID') or str(row['SCRIP_CD']) + filed.isoformat()}", "exchange": "BSE",
                    "symbol": normalize_symbol(f"NSE:{scrip_id}") if scrip_id else "",
                    "bse_code": str(row["SCRIP_CD"]), "company": clean_text(row.get("SLONGNAME")), "isin": "",
                    "filed_at": filed.strftime("%Y-%m-%d %H:%M:%S"),
                    "subject": clean_text(row.get("SUBCATNAME") or BSE_SUBCATEGORY),
                    "headline": headline, "text": " ".join(x for x in (headline, more) if x),
                    "pdf": BSE_PDF + attachment if attachment else ""})
    return out


def bse_get_json(url: str, params: dict):
    """GET a BSE API answer, with a browser's TLS fingerprint when curl_cffi is installed (BSE turns away plain
    Python clients), through DESK_EXCHANGE_PROXY when it is set."""
    from .nse import exchange_proxy

    proxy = exchange_proxy()
    try:
        from curl_cffi import requests as browserlike
    except ImportError:
        browserlike = None
    if browserlike is not None:
        kwargs = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
        with browserlike.Session(impersonate="chrome", headers=BSE_HEADERS, timeout=30, **kwargs) as session:
            response = session.get(url, params=params)
            if response.status_code in (401, 403):
                session.get("https://www.bseindia.com/", headers={"Accept": "text/html,*/*"})
                response = session.get(url, params=params)
            if response.status_code >= 400:
                raise RuntimeError(f"BSE answered {response.status_code}")
            return json.loads(response.text)
    from .bselists import http_get_json
    return http_get_json(url, params)


def bse_download(url: str) -> bytes:
    from .nse import exchange_proxy

    proxy = exchange_proxy()
    try:
        from curl_cffi import requests as browserlike
    except ImportError:
        browserlike = None
    if browserlike is not None:
        kwargs = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
        response = browserlike.get(url, impersonate="chrome", headers=BSE_HEADERS, timeout=45, **kwargs)
    else:
        import httpx

        from .nse import proxy_kwargs
        response = httpx.get(url, headers=BSE_HEADERS, timeout=45, follow_redirects=True, **proxy_kwargs())
    if response.status_code >= 400:
        raise RuntimeError(f"BSE answered {response.status_code}")
    return response.content


def pdf_text(data: bytes, pages: int = 3) -> str:
    """The text of the first pages of a PDF ("" when it cannot be read, e.g. a scanned image)."""
    try:
        from pypdf import PdfReader
    except ImportError:
        return ""
    try:
        reader = PdfReader(io.BytesIO(data))
        return clean_text(" ".join((page.extract_text() or "") for page in reader.pages[:pages]))[:8000]
    except Exception:  # noqa: BLE001 - a broken PDF is just unreadable
        return ""


def merge(filings: list[dict]) -> list[dict]:
    """One entry per order: the same filing on NSE and BSE (same company, within six hours) becomes one, with
    NSE's symbol, both exchanges' texts and the earlier time."""
    out: list[dict] = []
    by_company: dict[str, list[dict]] = {}
    for f in sorted(filings, key=lambda x: x["filed_at"]):
        key = company_key(f["company"]) or f["symbol"]
        twin = None
        for other in by_company.get(key, []):
            if other["exchange"] != f["exchange"] and "+" not in other["exchange"] and \
                    abs((_when(other["filed_at"], "%Y-%m-%d %H:%M:%S")
                         - _when(f["filed_at"], "%Y-%m-%d %H:%M:%S")).total_seconds()) < 6 * 3600:
                twin = other
                break
        if twin is None:
            entry = dict(f)
            entry["ids"] = [f["id"]]
            out.append(entry)
            by_company.setdefault(key, []).append(entry)
            continue
        nse, bse = (twin, f) if twin["exchange"] == "NSE" else (f, twin)
        twin.update({"exchange": "NSE+BSE", "symbol": nse["symbol"] or bse["symbol"], "isin": nse.get("isin", ""),
                     "bse_code": bse.get("bse_code", ""), "company": nse["company"] or bse["company"],
                     "filed_at": min(twin["filed_at"], f["filed_at"]),
                     "headline": bse["headline"] or nse["headline"],
                     "text": " ".join(x for x in (nse["text"], bse["text"]) if x),
                     "pdf": nse["pdf"] or bse["pdf"], "pdf_bse": bse["pdf"]})
        twin["ids"] = [*twin.get("ids", []), f["id"]]
    return out


# -- size -----------------------------------------------------------------------------------------------------

def size_band(pct_revenue: float | None, pct_cap: float | None) -> str:
    """How big the order is for this company. By yearly revenue when it is known, else by market value."""
    if pct_revenue is not None:
        return ("Very big" if pct_revenue >= 25 else "Big" if pct_revenue >= 10 else
                "Medium" if pct_revenue >= 3 else "Small")
    if pct_cap is not None:
        return ("Very big" if pct_cap >= 5 else "Big" if pct_cap >= 2 else "Medium" if pct_cap >= 0.5 else "Small")
    return "Unknown"


BAND_ORDER = {"Very big": 0, "Big": 1, "Medium": 2, "Small": 3, "Unknown": 4}


def parse_market_cap(answer) -> float | None:
    """NSE quote → market capitalisation in ₹ crore."""
    with contextlib.suppress(AttributeError, IndexError, KeyError, TypeError, ValueError):
        trade = answer["equityResponse"][0]["tradeInfo"]
        value = float(trade.get("totalMarketCap") or 0)
        if value > 0:
            return round(value / 1e7, 2)
        issued, price = float(trade.get("issuedSize") or 0), float(answer["equityResponse"][0]["orderBook"]["lastPrice"])
        return round(issued * price / 1e7, 2) if issued and price else None
    return None


# -- AI reading -----------------------------------------------------------------------------------------------

AI_PROMPT = """You read Indian stock exchange filings. Below is one filing a listed company made under "order received".
Answer in JSON only, with these keys:
"order_win": true if the company itself won or received a business order, contract, letter of award/intent or similar; false for tax, court, penalty or regulatory orders, cancellations, or orders the company gave to others.
"value_crore": the value of this order in Indian rupees crore (convert lakh, million, other currencies at today's rough rates; use the value excluding GST when both are given; null when no value is stated). Not the order book or a running total.
"customer": who gave the order (short name), or null.
"summary": one plain sentence, under 25 words, saying what the order is for.

Company: {company}
Filed: {filed}
Filing:
{text}
"""


def parse_ai(reply: str) -> dict | None:
    match = re.search(r"\{.*\}", str(reply or ""), re.S)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    value = data.get("value_crore")
    try:
        value = round(float(value), 2) if value not in (None, "", "null") else None
    except (TypeError, ValueError):
        value = None
    win = data.get("order_win")
    return {"order_win": None if win is None else bool(win), "value_cr": value if value and value > 0 else None,
            "customer": clean_text(data.get("customer"))[:120] or None,
            "summary": clean_text(data.get("summary"))[:240] or None}


# -- the scanner ----------------------------------------------------------------------------------------------

class OrderWins:
    """Reads, keeps and reports order-win filings.

    ``nse`` is an :class:`desk.nse.NseClient`; ``bse_json(url, params)`` and ``download(url)`` replace the
    BSE and PDF downloads (tests). ``ask(prompt)`` is the AI model (None: no AI). ``llm_problem()`` says why
    the AI cannot run (None when it can). ``revenue(symbol)`` gives yearly revenue in ₹ crore when known.
    ``is_trading_day(datetime)`` knows NSE's holidays. ``send(text)`` sends to Telegram and returns an error
    or None; None means no Telegram chat is linked.
    """

    def __init__(self, settings, notifier, nse=None, bse_json=None, download=None, ask=None, llm_problem=None,
                 revenue=None, is_trading_day=market.is_trading_day, now=market.now_ist, path: Path | None = None,
                 background: bool = True, wall=time.time):
        self.settings, self.notifier, self.nse = settings, notifier, nse
        self._bse_json = bse_json or bse_get_json
        self._download = download
        self._ask, self._llm_problem = ask, llm_problem or (lambda: None)
        self._revenue = revenue or (lambda symbol: None)
        self._trading = is_trading_day
        self._now, self._wall = now, wall
        self._background = background
        self.path = path or home() / "order-wins.json"
        self._lock = threading.Lock()
        self._running: threading.Thread | None = None
        self._state: dict = {"filings": {}, "caps": {}, "scanned_at": 0, "ok_at": 0, "note": None,
                             "evening_day": None, "morning_day": None, "sent": [],
                             "retry_at": 0, "warned_day": None, "last_report": None}
        with contextlib.suppress(OSError, ValueError, TypeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                self._state.update({k: stored[k] for k in self._state if k in stored})

    # -- storage ----------------------------------------------------------

    def _save(self) -> None:
        with contextlib.suppress(OSError):
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._state), encoding="utf-8")
            os.replace(tmp, self.path)

    def _cfg(self, key: str):
        return self.settings.get(key)

    # -- reading the exchanges --------------------------------------------

    def _fetch(self, start: datetime, end: datetime) -> tuple[list[dict], list[str]]:
        found, failed = [], []
        if self.nse is not None:
            try:
                answer = self.nse.json("corporate-announcements", {
                    "index": "equities", "from_date": start.strftime("%d-%m-%Y"),
                    "to_date": end.strftime("%d-%m-%Y"), "subject": NSE_SUBJECT})
                found += parse_nse(answer)
            except Exception as exc:  # noqa: BLE001 - BSE may still answer
                failed.append(f"NSE: {str(exc)[:140]}")
        try:
            for page in range(1, 11):
                answer = self._bse_json(BSE_API, {
                    "pageno": page, "strCat": "Company Update", "strPrevDate": start.strftime("%Y%m%d"),
                    "strScrip": "", "strSearch": "P", "strToDate": end.strftime("%Y%m%d"), "strType": "C",
                    "subcategory": BSE_SUBCATEGORY})
                rows = parse_bse(answer)
                found += rows
                if len((answer or {}).get("Table") or []) < 50:
                    break
        except Exception as exc:  # noqa: BLE001 - NSE may have answered
            failed.append(f"BSE: {str(exc)[:140]}")
        return found, failed

    def _pdf(self, entry: dict) -> str:
        for url in (entry.get("pdf"), entry.get("pdf_bse")):
            if not url:
                continue
            try:
                if self._download is not None:
                    data = self._download(url)
                elif "bseindia" in url:
                    data = bse_download(url)
                elif self.nse is not None:
                    data = self.nse.file(url)
                else:
                    continue
            except Exception as exc:  # noqa: BLE001 - the other exchange's copy may open
                logger.info("order filing PDF %s not read: %s", url, exc)
                continue
            text = pdf_text(data)
            if text:
                return text
        return ""

    def _market_cap(self, symbol: str) -> float | None:
        if not symbol or self.nse is None:
            return None
        cached = self._state["caps"].get(symbol)
        if cached and self._wall() - cached[1] < CAP_SECONDS:
            return cached[0]
        code = symbol.removesuffix(".NS").removesuffix(".BO")
        try:
            cap = parse_market_cap(self.nse.json(NSE_QUOTE, {"functionName": "getSymbolData", "marketType": "N",
                                                             "series": "EQ", "symbol": code}))
        except Exception as exc:  # noqa: BLE001 - the order is still listed, unsized
            logger.info("market cap of %s not read: %s", symbol, exc)
            cap = None
        if cap is None:
            with contextlib.suppress(Exception):     # SME stocks trade in other series
                cap = parse_market_cap(self.nse.json(NSE_QUOTE, {"functionName": "getSymbolData",
                                                                 "marketType": "N", "series": "SM", "symbol": code}))
        if cap is not None or not cached:
            self._state["caps"][symbol] = [cap, self._wall()]
        return cap if cap is not None else (cached[0] if cached else None)

    def _read(self, entry: dict, budget: dict) -> None:
        """Fill in value, customer and the order-win check, from the text, then the PDF, then the AI model."""
        text = entry.get("text") or ""
        entry["value_cr"] = order_value(text)
        entry["customer"] = counterparty(text)
        entry["win"] = is_order_win(" ".join((entry.get("subject") or "", text)))
        entry.setdefault("read_by", "text")
        if entry["value_cr"] is None and entry["win"] and budget["pdf"] > 0 and (entry.get("pdf") or entry.get("pdf_bse")):
            budget["pdf"] -= 1
            body = self._pdf(entry)
            entry["pdf_read"] = True
            if body:
                entry["pdf_text"] = body[:4000]
                if not is_order_win(body[:2500]) and NOT_A_WIN.search(body[:2500]):
                    entry["win"] = False
                entry["value_cr"] = order_value(body)
                entry["customer"] = entry["customer"] or counterparty(body)
                entry["read_by"] = "PDF"
        unclear = entry["win"] and (entry["value_cr"] is None or entry["customer"] is None)
        if unclear and self._ask is not None and budget["ai"] > 0 and self._llm_problem() is None:
            budget["ai"] -= 1
            source = " ".join(x for x in (text, entry.get("pdf_text", "")) if x)[:6000]
            try:
                found = parse_ai(self._ask(AI_PROMPT.format(company=entry["company"], filed=entry["filed_at"],
                                                            text=source)))
            except Exception as exc:  # noqa: BLE001 - the text reading stands
                logger.info("AI reading of %s failed: %s", entry["id"], exc)
                found = None
            entry["ai_tried"] = True
            if found:
                if found["order_win"] is False:
                    entry["win"] = False
                entry["value_cr"] = entry["value_cr"] or found["value_cr"]
                entry["customer"] = entry["customer"] or found["customer"]
                entry["summary"] = found["summary"]
                entry["read_by"] = "AI"

    def _size(self, entry: dict) -> None:
        cap = self._market_cap(entry.get("symbol") or "")
        revenue = None
        with contextlib.suppress(Exception):
            revenue = self._revenue(entry.get("symbol") or "")
        value = entry.get("value_cr")
        entry["market_cap_cr"] = cap
        entry["revenue_cr"] = revenue
        entry["pct_cap"] = round(value / cap * 100, 2) if value and cap else None
        entry["pct_revenue"] = round(value / revenue * 100, 1) if value and revenue else None
        entry["size"] = size_band(entry["pct_revenue"], entry["pct_cap"])

    def scan(self) -> dict:
        """Read the last few days' filings now and keep the new ones. Returns {"new", "failed"}."""
        now = self._now()
        today = now.replace(tzinfo=None)
        found, failed = self._fetch(today - timedelta(days=LOOKBACK_DAYS), today)
        with self._lock:
            known = self._state["filings"]
            seen = {i for f in known.values() for i in f.get("ids", [f["id"]])}
        fresh = [f for f in found if f["id"] not in seen]
        budget = {"pdf": MAX_PDFS, "ai": MAX_AI}
        new = []
        for entry in merge(fresh):
            # A twin of an order already kept (the other exchange filed later) joins it instead.
            key = company_key(entry["company"])
            twin = next((f for f in known.values() if company_key(f["company"]) == key and f["exchange"] != entry["exchange"]
                         and "+" not in f["exchange"]
                         and abs((_when(f["filed_at"], "%Y-%m-%d %H:%M:%S") - _when(entry["filed_at"], "%Y-%m-%d %H:%M:%S"))
                                 .total_seconds()) < 6 * 3600), None)
            if twin is not None:
                with self._lock:
                    twin["ids"] = [*twin.get("ids", [twin["id"]]), *entry["ids"]]
                    twin["exchange"] = "NSE+BSE"
                    if entry["exchange"] == "NSE":
                        twin["symbol"] = entry["symbol"]
                    if twin.get("value_cr") is None:
                        twin["value_cr"] = order_value(entry["text"])
                        if twin["value_cr"] is not None:
                            self._size(twin)
                continue
            self._read(entry, budget)
            entry.update(session_of(_when(entry["filed_at"], "%Y-%m-%d %H:%M:%S"), self._trading))
            if entry["win"]:
                self._size(entry)
            entry.pop("pdf_text", None)
            entry["found_at"] = now.strftime("%Y-%m-%d %H:%M:%S")
            new.append(entry)
        cutoff = (today - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
        with self._lock:
            for entry in new:
                known[entry["id"]] = entry
            self._state["filings"] = {k: v for k, v in known.items() if v["filed_at"] >= cutoff}
            self._state["caps"] = {k: v for k, v in self._state["caps"].items() if self._wall() - v[1] < 7 * 86400}
            self._state["scanned_at"] = self._wall()
            nse_ok = self.nse is not None and not any(f.startswith("NSE") for f in failed)
            bse_ok = not any(f.startswith("BSE") for f in failed)
            if nse_ok or bse_ok:
                self._state["ok_at"] = self._wall()
            self._state["note"] = self._why(failed) if failed else None
            self._save()
        return {"new": [e for e in new if e["win"]], "failed": failed, "ok": not failed or nse_ok or bse_ok}

    @staticmethod
    def _why(failed: list[str]) -> str:
        blocked = any(re.search(r"\b40[13]\b|refused|did not answer|proxy|connect|timed out", f, re.I) for f in failed)
        return ("Order filings could not be read (" + "; ".join(failed) + ")."
                + (" NSE and BSE refuse many cloud servers. If the desk uses your home computer as its exchange "
                   "proxy (DESK_EXCHANGE_PROXY), check that the computer and the proxy are on." if blocked else ""))

    # -- reports ----------------------------------------------------------

    def _report_text(self, rows: list[dict], kind: str, now: datetime) -> list[str]:
        early = [r for r in rows if r.get("early")]
        during = [r for r in rows if not r.get("early")]
        next_day = min((r["reacts_on"] for r in early), default=None)
        when = datetime.strptime(next_day, "%Y-%m-%d").strftime("%a %d %b") if next_day else None
        head = ("🌙 Order wins filed since the last report" if kind == "evening"
                else "🌅 Order wins filed overnight") + (f", for {when}'s session" if when else "")

        def line(i: int, r: dict) -> str:
            code = (r.get("symbol") or r.get("bse_code") or "").removesuffix(".NS")
            value = f"₹{r['value_cr']:,.0f} cr" if r.get("value_cr") and r["value_cr"] >= 10 else (
                f"₹{r['value_cr']:,.2f} cr" if r.get("value_cr") else "value not stated")
            size = []
            if r.get("pct_cap") is not None:
                size.append(f"{r['pct_cap']:.1f}% of market value")
            if r.get("pct_revenue") is not None:
                size.append(f"{r['pct_revenue']:.0f}% of yearly sales")
            filed = r["filed_at"][11:16]
            parts = [f"{i}. {code} {value}"]
            if r.get("customer"):
                parts.append(f"from {r['customer']}")
            text = " ".join(parts)
            extra = " · ".join([*size, f"{r['size']}" if r.get("size") not in (None, "Unknown") else "", f"filed {filed}"])
            return text + "\n   " + " · ".join(x for x in extra.split(" · ") if x)

        lines = [head, ""]
        if early:
            lines += [line(i, r) for i, r in enumerate(early[:MAX_LISTED], 1)]
            if len(early) > MAX_LISTED:
                lines.append(f"…and {len(early) - MAX_LISTED} more in the desk (Tools → Order Wins).")
        if during:
            lines += ["", "Filed during market hours (the price has already reacted):"]
            lines += [line(i, r) for i, r in enumerate(during[:5], 1)]
        lines += ["", "Size is the order against the company's market value or yearly sales. Past order wins filed "
                      "after the close opened about 1.7% higher and then gave back about 0.4% by the close: the news "
                      "is usually in the opening price. Check the filing before trading."]
        text = "\n".join(lines)
        chunks, current = [], ""
        for part in text.split("\n"):
            if len(current) + len(part) + 1 > TELEGRAM_CHUNK:
                chunks.append(current)
                current = ""
            current += ("\n" if current else "") + part
        return [*chunks, current] if current else chunks

    def _due(self, rows: list[dict]) -> list[dict]:
        """Order wins not reported yet, biggest first. Orders smaller than the share of market value set in
        Settings are left out; an order that could not be sized (no value stated) is kept, to be checked."""
        min_pct = float(self._cfg("order_scan_min_pct") or 0)
        sent = set(self._state.get("sent") or [])
        since = (self._now() - timedelta(hours=REPORT_HOURS)).strftime("%Y-%m-%d %H:%M:%S")
        out = []
        for r in rows:
            if not r.get("win") or r["id"] in sent or r["filed_at"] < since:
                continue
            if min_pct and r.get("pct_cap") is not None and r["pct_cap"] < min_pct:
                continue
            out.append(r)
        out.sort(key=lambda r: (BAND_ORDER.get(r.get("size"), 4), -(r.get("pct_cap") or 0), r["filed_at"]))
        return out

    def report(self, kind: str) -> dict:
        """Send the evening or morning report now. {"sent", "orders", "error"}."""
        now = self._now()
        with self._lock:
            rows = list(self._state["filings"].values())
        due = self._due(rows)
        target = self.notifier.telegram_target(require_alerts=False)
        outcome = {"sent": False, "orders": len(due), "error": None}
        if not due:
            outcome["error"] = "no new order wins"
        elif not target:
            outcome["error"] = "no Telegram chat linked"
        else:
            for text in self._report_text(due, kind, now):
                self.notifier.deliver(target, text)
                if self.notifier.last_error:
                    outcome["error"] = self.notifier.last_error
                    break
            outcome["sent"] = outcome["error"] is None
        if outcome["sent"] or outcome["error"] == "no Telegram chat linked":
            with self._lock:
                kept = set(self._state["filings"])
                self._state["sent"] = [i for i in self._state.get("sent") or [] if i in kept] + [r["id"] for r in due]
        with self._lock:
            self._state["last_report"] = {"kind": kind, "at": now.strftime("%Y-%m-%d %H:%M"), **outcome}
            self._save()
        if outcome["sent"]:
            self.notifier.record(f"Order wins sent ({kind})", f"{len(due)} order wins sent to Telegram.", "info")
        return outcome

    # -- the clock --------------------------------------------------------

    def _run(self, kind: str | None) -> None:
        try:
            result = self.scan()
        except Exception as exc:  # noqa: BLE001 - try again on a later tick
            logger.exception("order scan failed")
            result = {"new": [], "failed": [str(exc)[:200]], "ok": False}
        now = self._now()
        day = now.strftime("%Y-%m-%d")
        if kind is None:
            return
        if not result["ok"]:
            with self._lock:
                self._state["retry_at"] = self._wall() + RETRY_MINUTES * 60
                warn = self._state.get("warned_day") != f"{day}:{kind}"
                self._state["warned_day"] = f"{day}:{kind}"
                self._save()
            target = self.notifier.telegram_target(require_alerts=False)
            if warn and target:
                hour, minute = EVENING_UNTIL if kind == "evening" else MORNING_UNTIL
                until = f"{hour:02d}:{minute:02d}"
                self.notifier.deliver(target, "⚠️ Order wins scan could not reach NSE or BSE\n"
                                      + (self._state.get("note") or "")
                                      + f"\nTrying again every {RETRY_MINUTES} minutes until {until}.")
            self.notifier.record("Order wins scan failed", self._state.get("note") or "", "warn")
            return
        self.report(kind)
        with self._lock:
            self._state[f"{kind}_day"] = day
            self._state["retry_at"] = 0
            self._save()

    def _start(self, kind: str | None) -> bool:
        if self._running is not None and self._running.is_alive():
            return False
        if not self._background:
            self._run(kind)
            return True
        self._running = threading.Thread(target=self._run, args=(kind,), name="desk-order-wins", daemon=True)
        self._running.start()
        return True

    def busy(self) -> bool:
        return self._running is not None and self._running.is_alive()

    def scan_now(self) -> bool:
        """Read the filings now in the background (no Telegram report)."""
        return self._start(None)

    def _slot(self, now: datetime, key: str, until: tuple[int, int]) -> str | None:
        """'due' when this report should run now, 'missed' when its window passed, None otherwise."""
        hour, minute = (int(x) for x in str(self._cfg(key)).split(":"))
        start = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        end = now.replace(hour=until[0], minute=until[1], second=0, microsecond=0)
        if now < start:
            return None
        return "due" if now < end else "missed"

    def tick(self) -> None:
        if not self._cfg("order_scan"):
            return
        now = self._now()
        day = now.strftime("%Y-%m-%d")
        if self._wall() < float(self._state.get("retry_at") or 0) or self.busy():
            return
        if self._state.get("morning_day") != day and self._trading(now):
            slot = self._slot(now, "order_scan_morning", MORNING_UNTIL)
            if slot == "due":
                self._start("morning")
                return
            if slot == "missed":
                with self._lock:
                    self._state["morning_day"] = day
                    self._save()
        if self._state.get("evening_day") != day:
            slot = self._slot(now, "order_scan_evening", EVENING_UNTIL)
            if slot == "due":
                self._start("evening")
                return
            if slot == "missed":
                with self._lock:
                    self._state["evening_day"] = day
                    self._save()
        # Keep the screen fresh between the reports: every half hour from 08:00 to 23:30.
        if (8, 0) <= (now.hour, now.minute) < EVENING_UNTIL and \
                self._wall() - float(self._state.get("scanned_at") or 0) >= REFRESH_MINUTES * 60:
            self._start(None)

    # -- the screen -------------------------------------------------------

    def view(self, days: int = 7, everything: bool = False) -> dict:
        cutoff = (self._now() - timedelta(days=days)).strftime("%Y-%m-%d")
        with self._lock:
            rows = [dict(r) for r in self._state["filings"].values() if r["filed_at"] >= cutoff]
            state = {k: self._state.get(k) for k in ("scanned_at", "ok_at", "note", "last_report",
                                                     "evening_day", "morning_day")}
        shown = rows if everything else [r for r in rows if r.get("win")]
        shown.sort(key=lambda r: r["filed_at"], reverse=True)
        for r in shown:
            r.pop("ids", None)
        return {"rows": shown, "hidden": len(rows) - len(shown), **state, "busy": self.busy(),
                "enabled": bool(self._cfg("order_scan")), "evening": self._cfg("order_scan_evening"),
                "morning": self._cfg("order_scan_morning"), "min_pct": self._cfg("order_scan_min_pct"),
                "proxy": bool((os.environ.get("DESK_EXCHANGE_PROXY") or "").strip()),
                "ai": self._ask is not None and self._llm_problem() is None}
