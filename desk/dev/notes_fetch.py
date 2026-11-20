"""Fetch the NSE end-of-day data the trading notes are drawn from.

    python -m desk.dev.notes_fetch FOLDER        # then: python -m desk.dev.notes_figures --data FOLDER

Needs access to finance.yahoo.com and nseindia.com. Writes one CSV per symbol, NSE's lot-size and Nifty 50 list
files, and SOURCES.json with what was fetched and when.
"""
import datetime as dt
import json
import pathlib
import sys
import time

import pandas as pd
import requests
import yfinance as yf


def main() -> None:
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "notes-data")
    out.mkdir(exist_ok=True)
    log = {"fetched_utc": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "yahoo": {}, "nse": {}}
    s = requests.Session()
    s.headers.update({"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
                      "Accept": "*/*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"})
    try:
        s.get("https://www.nseindia.com/", timeout=20)
    except Exception as e:
        log["nse"]["home"] = str(e)
    for name, url in {
        "fo_mktlots.csv": "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv",
        "nifty50list.csv": "https://nsearchives.nseindia.com/content/indices/ind_nifty50list.csv",
    }.items():
        try:
            r = s.get(url, timeout=30)
            log["nse"][name] = {"url": url, "status": r.status_code, "bytes": len(r.content)}
            if r.ok and len(r.content) > 200:
                (out / name).write_bytes(r.content)
        except Exception as e:
            log["nse"][name] = {"url": url, "error": str(e)}
    syms = ["^NSEI", "^NSEBANK", "^INDIAVIX", "^BSESN", "^CNXIT", "^CNXAUTO", "^CNXPHARMA", "^CNXFMCG", "^CNXMETAL",
            "^CNXREALTY", "^CNXPSUBANK"]
    try:
        syms += [f"{x.strip()}.NS" for x in pd.read_csv(out / "nifty50list.csv")["Symbol"]]
    except Exception:
        syms += [f"{x}.NS" for x in ["RELIANCE", "HDFCBANK", "ICICIBANK", "INFY", "TCS", "ITC", "LT", "SBIN", "BHARTIARTL", "KOTAKBANK", "AXISBANK", "HINDUNILVR", "BAJFINANCE", "MARUTI", "SUNPHARMA", "TITAN", "ASIANPAINT", "ULTRACEMCO", "NTPC", "POWERGRID", "TATASTEEL", "JSWSTEEL", "HCLTECH", "WIPRO", "TECHM", "M&M", "ONGC", "COALINDIA", "ADANIENT", "ADANIPORTS", "BAJAJFINSV", "NESTLEIND", "GRASIM", "HINDALCO", "CIPLA", "DRREDDY", "EICHERMOT", "HEROMOTOCO", "BAJAJ-AUTO", "BRITANNIA", "APOLLOHOSP", "TATACONSUM", "SHRIRAMFIN", "TRENT", "BEL", "ETERNAL", "JIOFIN", "INDIGO", "MAXHEALTH", "HDFCLIFE", "SBILIFE"]]
    for sym in syms:
        for _ in range(3):
            try:
                df = yf.Ticker(sym).history(period="max", interval="1d", auto_adjust=False, actions=True)
                if df.empty:
                    raise ValueError("empty")
                df.index = df.index.tz_localize(None).date
                df.index.name = "Date"
                df.round(4).to_csv(out / f"{sym.replace('^', 'IDX_')}.csv")
                log["yahoo"][sym] = {"rows": len(df), "first": str(df.index[0]), "last": str(df.index[-1])}
                break
            except Exception as e:
                log["yahoo"][sym] = {"error": str(e)}
                time.sleep(3)
    (out / "SOURCES.json").write_text(json.dumps(log, indent=1))
    print(json.dumps(log)[:4000])


if __name__ == "__main__":
    main()
