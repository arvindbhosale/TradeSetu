"""A polite client for NSE's website data (results filings, results comparison).

NSE's ``/api`` answers only a browser-like session: it needs the cookies its
pages set, a browser User-Agent and a Referer. This client opens the home page
once for cookies, paces requests (one a second by default) and, on 401 or 403,
reopens the page for fresh cookies and tries once more.

NSE serves the same site at www.nseindia.com and www.nseindia.in; the client
tries them in turn and keeps the one that answered. Files (XBRL, CSV) come from
nsearchives.nseindia.com.

NSE allows this for personal use from your own connection. It blocks many
cloud and data-centre addresses, so on a server the requests can fail; the
callers keep what they already have and say why.
"""

from __future__ import annotations

import os
import threading
import time

HOSTS = ("https://www.nseindia.com", "https://www.nseindia.in")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-IN,en;q=0.9",
}


def exchange_proxy() -> str | None:
    """``DESK_EXCHANGE_PROXY`` (e.g. ``http://user:pass@host:port``): send NSE and BSE requests through it.

    NSE and BSE refuse many cloud and data-centre addresses; a proxy on an Indian home or office connection gets
    their data to a desk hosted on such a server. Only exchange requests use it, never the broker.
    """
    return (os.environ.get("DESK_EXCHANGE_PROXY") or "").strip() or None


def proxy_kwargs() -> dict:
    proxy = exchange_proxy()
    return {"proxy": proxy} if proxy else {}


class NseError(RuntimeError):
    """NSE did not answer with data."""


class NseClient:
    """``json(path, params)`` for ``<host>/api/<path>`` and ``file(url)`` for archive files.

    ``get_json(path, params)`` and ``download(url)`` replace the network (tests).
    """

    def __init__(self, get_json=None, download=None, gap: float = 1.0, timeout: float = 30.0):
        self._get_json, self._download = get_json, download
        self.gap, self.timeout = gap, timeout
        self._lock = threading.Lock()
        self._last = 0.0
        self._client = None
        self._warm: set[str] = set()
        self._host = HOSTS[0]

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.Client(headers=HEADERS, timeout=self.timeout, follow_redirects=True, **proxy_kwargs())
        return self._client

    def _pace(self) -> None:
        wait = self._last + self.gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()

    def _cookies(self, host: str) -> None:
        self._pace()
        self._http().get(host + "/", headers={"Accept": "text/html,*/*"})
        self._warm.add(host)

    def json(self, path: str, params: dict | None = None):
        if self._get_json is not None:
            return self._get_json(path, params or {})
        failures = []
        with self._lock:
            for host in (self._host, *[h for h in HOSTS if h != self._host]):
                try:
                    return self._json_from(host, path, params or {})
                except Exception as exc:  # noqa: BLE001 - try the other host, then report both
                    failures.append(f"{host.split('//')[1]}: {str(exc)[:120]}")
        raise NseError("NSE did not answer (" + "; ".join(failures) + ")")

    def _json_from(self, host: str, path: str, params: dict):
        for attempt in range(2):
            if host not in self._warm or attempt:
                self._cookies(host)
            self._pace()
            response = self._http().get(f"{host}/api/{path}", params=params,
                                        headers={"Referer": host + "/companies-listing/corporate-filings-financial-results"})
            if response.status_code in (401, 403) and not attempt:
                continue
            response.raise_for_status()
            if not response.content.strip():
                raise NseError("empty answer")
            self._host = host
            return response.json()
        raise NseError("refused after fresh cookies")

    def file(self, url: str) -> bytes:
        if self._download is not None:
            return self._download(url)
        with self._lock:
            for attempt in range(2):
                self._pace()
                response = self._http().get(url, headers={"Referer": self._host + "/", "Accept": "*/*"})
                if response.status_code in (401, 403) and not attempt:
                    self._cookies(self._host)
                    continue
                response.raise_for_status()
                return response.content
        raise NseError(f"NSE refused {url}")
