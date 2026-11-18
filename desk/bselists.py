"""Which stocks are in each BSE index.

BSE publishes no constituent file. Its market-movers feed (the gainers and
losers lists behind bseindia.com) carries, for every traded stock, the BSE
indices it belongs to (``index_code``, ``|BSE 500|,|BSE SmallCap|...``). Reading
the whole market's gainers and losers once a day gives every index's members.
A stock that closed exactly unchanged is absent from both lists that day, so a
member is kept until it has not been seen for ``KEEP_DAYS`` days.
"""

from __future__ import annotations

import json
import re
import time

from tradingagents.dataflows.symbols import normalize_symbol

API = "https://api.bseindia.com/BseIndiaAPI/api/MktRGainerLoserData/w"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
                         "Chrome/131.0 Safari/537.36",
           "Accept": "application/json, text/plain, */*", "Origin": "https://www.bseindia.com",
           "Referer": "https://www.bseindia.com/"}
KEEP_DAYS = 10

# Kite's short BSE index symbols, and other spellings, mapped to the words BSE uses for the index.
ALIASES = {"SMLCAP": "SMALLCAP", "MIDCAP": "MIDCAP", "LRGCAP": "LARGECAP", "FMC": "FASTMOVINGCONSUMERGOODS",
           "FMCG": "FASTMOVINGCONSUMERGOODS", "IT": "INFORMATIONTECHNOLOGY", "HC": "HEALTHCARE", "CG": "CAPITALGOODS",
           "CD": "CONSUMERDURABLES", "SNSX50": "SENSEX50", "SNXT50": "SENSEXNEXT50", "MIDSEL": "MIDCAPSELECT",
           "SMLSEL": "SMALLCAPSELECT", "LMI250": "250LARGEMIDCAP", "MSL400": "400MIDSMALLCAP",
           "TELCOM": "TELECOMMUNICATION", "TELECOM": "TELECOMMUNICATION", "UTILS": "UTILITIES",
           "FINSER": "FINANCIALSERVICES", "INDSTR": "INDUSTRIALS", "COMDTY": "COMMODITIES", "CDGS": "CONSUMERDISCRETIONARY",
           "BHRT22": "BHARAT22", "DOL30": "DOLLEX30", "DOL100": "DOLLEX100", "DOL200": "DOLLEX200", "CARBON": "CARBONEX",
           "GREENX": "GREENEX", "INFRA": "INDIAINFRASTRUCTURE", "MFG": "INDIAMANUFACTURING", "OILGAS": "OILGAS",
           "PVTBNK": "PRIVATEBANKS", "PRIVATEBANK": "PRIVATEBANKS"}


def key(name: str) -> str:
    """One spelling for a BSE index name: ``S&P BSE 500``, ``BSE 500`` and Kite's ``BSE500`` all give ``500``."""
    text = re.sub(r"[^A-Z0-9]", "", str(name).upper().replace("S&P", "").replace("INDEX", ""))
    if text in ALIASES:
        return ALIASES[text]
    if text.startswith("BSE") and len(text) > 3:
        text = text[3:]
    return ALIASES.get(text, text)


def parse(rows) -> dict[str, dict]:
    """Index key -> {"name": BSE's name, "stocks": {symbol: company}} from market-movers rows."""
    out: dict[str, dict] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        scrip = str(row.get("scripname") or "").strip().upper()
        if not re.fullmatch(r"[A-Z0-9&\-]{1,20}", scrip):
            continue
        symbol = normalize_symbol(f"NSE:{scrip}")
        company = " ".join(str(row.get("LONG_NAME") or "").split())
        for name in re.findall(r"\|([^|]+)\|", str(row.get("index_code") or "")):
            name = " ".join(name.split())
            k = key(name)
            if k:
                entry = out.setdefault(k, {"name": name, "stocks": {}})
                entry["stocks"][symbol] = company
    return out


def fetch(get_json) -> dict[str, dict]:
    """Every BSE index's members today. ``get_json(url, params)`` returns the decoded answer."""
    rows = []
    for kind in ("gainer", "loser"):
        answer = get_json(API, {"GLtype": kind, "IndxGrp": "all", "IndxGrpval": "", "orderby": "all"})
        if isinstance(answer, dict):
            answer = answer.get("Table") or []
        rows.extend(answer if isinstance(answer, list) else [])
    return parse(rows)


def merge(stored: dict, fresh: dict[str, dict], now: float | None = None) -> dict:
    """Fold today's members into the stored lists; drop a member unseen for KEEP_DAYS."""
    now = time.time() if now is None else now
    out = {k: {"name": v.get("name", k), "stocks": dict(v.get("stocks") or {})} for k, v in (stored or {}).items()}
    for k, entry in fresh.items():
        target = out.setdefault(k, {"name": entry["name"], "stocks": {}})
        target["name"] = entry["name"]
        for symbol, company in entry["stocks"].items():
            target["stocks"][symbol] = [company, now]
    cutoff = now - KEEP_DAYS * 86400
    for k in list(out):
        out[k]["stocks"] = {s: v for s, v in out[k]["stocks"].items() if isinstance(v, list) and v[1] >= cutoff}
        if not out[k]["stocks"]:
            del out[k]
    return out


def http_get_json(url: str, params: dict):
    import httpx

    response = httpx.get(url, params=params, headers=HEADERS, timeout=30, follow_redirects=True)
    response.raise_for_status()
    return json.loads(response.text)
