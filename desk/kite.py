"""Client for a Kite (Zerodha) MCP server.

The server is described by a standard ``mcpServers`` entry: a ``command`` and
``args`` to start a stdio server (``npx mcp-remote <url>``), or a ``url`` for a
streamable-HTTP server. The session lives on a background event loop so the
rest of the desk can call it from ordinary threads.

Which tools exist is read from the server, never assumed. Zerodha's hosted
server (mcp.kite.trade) does not register the order tools; a self-hosted
kite-mcp-server with your own Kite Connect key does. ``can_trade`` says which
one is connected.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import threading
from concurrent.futures import TimeoutError as FutureTimeout

logger = logging.getLogger(__name__)

ORDER_TOOLS = ("place_order", "modify_order", "cancel_order")
_URL = re.compile(r"https?://[^\s)\]>\"']+")
_LOGIN_HINT = re.compile(r"log ?in|session|token|authenticat", re.IGNORECASE)


class KiteError(Exception):
    """A Kite MCP call failed. ``needs_login`` marks an expired or absent session."""

    def __init__(self, message: str, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


def resolve_transport(entry: dict) -> dict:
    """How to reach the server an ``mcpServers`` entry describes.

    A stdio entry whose command is not installed falls back to the URL in its
    arguments, when there is one: ``npx mcp-remote <url>`` only bridges to that
    URL, so the desk can speak to it directly without Node.
    """
    if entry.get("url"):
        return {"kind": "http", "url": entry["url"]}
    command = entry.get("command")
    if not command:
        raise KiteError('the MCP server entry needs a "command" or a "url"')
    args = [str(a) for a in entry.get("args", [])]
    found = shutil.which(command)
    if found:
        if os.path.basename(found).lower().split(".")[0] == "npx" and not {"-y", "--yes"} & set(args):
            args = ["-y", *args]      # no terminal to answer npx's install prompt
        return {"kind": "stdio", "command": found, "args": args, "env": entry.get("env") or None}
    urls = [a for a in args if _URL.fullmatch(a)]
    if urls:
        return {"kind": "http", "url": urls[0], "fallback_from": command}
    raise KiteError(f'"{command}" is not installed and the entry has no URL to connect to directly. '
                    f"Install Node.js (for npx) or give the server's URL.")


def _parse(result) -> object:
    """A tool result as data: JSON when the text is JSON, else the text."""
    texts = [getattr(block, "text", "") for block in (result.content or [])
             if getattr(block, "type", "") == "text"]
    text = "\n".join(t for t in texts if t).strip()
    if getattr(result, "is_error", False) or getattr(result, "isError", False):
        raise KiteError(text or "the Kite server returned an error",
                        needs_login=bool(_LOGIN_HINT.search(text)))
    structured = getattr(result, "structured_content", None)
    if not text and structured is not None:
        return structured
    try:
        return json.loads(text)
    except (ValueError, TypeError):
        return text


class KiteMCP:
    def __init__(self):
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._session = None
        self._stop: asyncio.Event | None = None
        self._lock = threading.Lock()
        self.tools: dict[str, dict] = {}
        self._tokens: dict[str, int] = {}        # instrument tokens, which never change within a session
        self.transport: dict | None = None
        self.error: str | None = None

    # -- lifecycle ---------------------------------------------------------

    @property
    def connected(self) -> bool:
        return self._session is not None

    @property
    def can_trade(self) -> bool:
        return self.connected and "place_order" in self.tools

    def status(self) -> dict:
        transport = self.transport or {}
        return {
            "connected": self.connected,
            "can_trade": self.can_trade,
            "tools": sorted(self.tools),
            "missing_order_tools": [t for t in ORDER_TOOLS if t not in self.tools] if self.connected else [],
            "transport": transport.get("kind"),
            "endpoint": transport.get("url") or " ".join(
                [os.path.basename(transport.get("command", ""))] + transport.get("args", [])).strip(),
            "fallback_from": transport.get("fallback_from"),
            "error": self.error,
        }

    def connect(self, entry: dict, timeout: float = 240.0) -> dict:
        """Start the server session; replaces any existing one.

        The wait is long because mcp-remote may open the browser for a Zerodha
        sign-in (the hosted server uses OAuth) before the session starts.
        """
        with self._lock:
            self._disconnect()
            self.error = None
            try:
                self.transport = resolve_transport(entry)
            except KiteError as exc:
                self.error = str(exc)
                raise
            ready: threading.Event = threading.Event()
            failure: list[BaseException] = []
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._serve, args=(self._loop, self.transport, ready, failure),
                name="kite-mcp", daemon=True)
            self._thread.start()
            if not ready.wait(timeout):
                self._disconnect()
                self.error = f"the Kite MCP server did not answer within {timeout:.0f}s"
                raise KiteError(self.error)
            if failure:
                transport = self.transport
                self._disconnect()
                self.error = f"could not connect to the Kite MCP server: {_describe(failure[0])}"
                if transport["kind"] == "http" and _needs_oauth(transport["url"]):
                    self.error = (
                        f"{transport['url']} requires an OAuth sign-in, which the desk does not do itself. "
                        "Install Node.js so the desk can start it through `npx mcp-remote`, which opens "
                        "the Zerodha sign-in in your browser.")
                raise KiteError(self.error)
            return self.status()

    def disconnect(self) -> None:
        with self._lock:
            self._disconnect()

    def _disconnect(self) -> None:
        loop, thread, stop = self._loop, self._thread, self._stop
        if loop and stop and loop.is_running():
            loop.call_soon_threadsafe(stop.set)
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=10)
        self._session, self._loop, self._thread, self._stop = None, None, None, None
        self.tools = {}

    def _serve(self, loop, transport, ready, failure) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._session_task(transport, ready, failure))
        except BaseException as exc:  # noqa: BLE001 - reported to the caller
            if not ready.is_set():
                failure.append(exc)
            else:
                self.error = f"the Kite MCP connection ended: {_describe(exc)}"
                logger.warning(self.error)
        finally:
            self._session = None
            ready.set()
            loop.close()

    async def _session_task(self, transport, ready, failure) -> None:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client

        self._stop = asyncio.Event()
        if transport["kind"] == "stdio":
            env = {**os.environ, **transport["env"]} if transport.get("env") else None
            streams = stdio_client(StdioServerParameters(
                command=transport["command"], args=transport["args"], env=env))
        else:
            try:
                from mcp.client.streamable_http import streamable_http_client as http_client
            except ImportError:      # mcp 1.x spelling
                from mcp.client.streamable_http import streamablehttp_client as http_client
            streams = http_client(transport["url"])
        async with streams as opened:
            read, write = opened[0], opened[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                listed = await session.list_tools()
                self.tools = {
                    tool.name: {
                        "description": tool.description or "",
                        "schema": getattr(tool, "input_schema", None) or getattr(tool, "inputSchema", None) or {},
                    }
                    for tool in listed.tools
                }
                self._session = session
                ready.set()
                await self._stop.wait()
                self._session = None

    # -- calls -------------------------------------------------------------

    def call(self, tool: str, arguments: dict | None = None, timeout: float = 45.0):
        """Call a tool and return its result as data. Raises KiteError."""
        session, loop = self._session, self._loop
        if session is None or loop is None:
            raise KiteError("not connected to the Kite MCP server")
        if tool not in self.tools:
            raise KiteError(f'the connected Kite server has no "{tool}" tool')
        future = asyncio.run_coroutine_threadsafe(session.call_tool(tool, arguments or {}), loop)
        try:
            result = future.result(timeout)
        except FutureTimeout:
            future.cancel()
            raise KiteError(f"{tool} timed out after {timeout:.0f}s") from None
        except KiteError:
            raise
        except Exception as exc:  # noqa: BLE001 - transport and protocol errors
            raise KiteError(f"{tool} failed: {_describe(exc)}") from exc
        return _parse(result)

    def login(self) -> dict:
        """Ask the server for a Kite login link. The user opens it and signs in."""
        answer = self.call("login")
        text = answer if isinstance(answer, str) else json.dumps(answer)
        match = _URL.search(text)
        return {"url": match.group(0).rstrip(".,") if match else None, "message": text}

    def profile(self):
        return self.call("get_profile")

    def margins(self):
        return self.call("get_margins")

    def holdings(self) -> list:
        return _rows(self.call("get_holdings"))

    def positions(self) -> list:
        data = self.call("get_positions")
        if isinstance(data, dict) and ("net" in data or "day" in data):
            return list(data.get("net") or [])
        return _rows(data)

    def orders(self) -> list:
        return _rows(self.call("get_orders"))

    def ltp(self, instruments: list[str]) -> dict[str, float]:
        """Last traded prices, keyed ``EXCHANGE:SYMBOL``."""
        data = self.call("get_ltp", {"instruments": instruments})
        out = {}
        if isinstance(data, dict):
            for key, value in data.items():
                price = value.get("last_price") if isinstance(value, dict) else value
                if isinstance(price, (int, float)) and price > 0:
                    out[key] = float(price)
        return out

    def gtts(self) -> list:
        return _rows(self.call("get_gtts"))

    def place_gtt(self, args: dict):
        if "place_gtt_order" not in self.tools:
            raise KiteError("This Kite server does not offer GTT orders. Zerodha's hosted server is "
                            "read-only; use a self-hosted kite-mcp-server.")
        return self.call("place_gtt_order", args, timeout=30.0)

    def delete_gtt(self, trigger_id):
        return self.call("delete_gtt_order", {"trigger_id": int(trigger_id)}, timeout=30.0)

    def cancel_order(self, order_id: str, variety: str = "regular"):
        return self.call("cancel_order", {"variety": variety, "order_id": str(order_id)}, timeout=30.0)

    def candles(self, instrument: str, interval: str, start, end, continuous: bool = False, oi: bool = False) -> list:
        """Kite's own candles for ``EXCHANGE:SYMBOL``. Needs the historical-data add-on on the API key."""
        if "get_historical_data" not in self.tools:
            raise KiteError("This Kite server does not offer historical candles.")
        token = self._tokens.get(instrument)
        if not token:
            data = self.call("get_ltp", {"instruments": [instrument]})
            token = (data.get(instrument) or {}).get("instrument_token") if isinstance(data, dict) else None
            if not token:
                raise KiteError(f"Kite does not know the instrument {instrument}")
            self._tokens[instrument] = token
        from . import data

        spec = data.INTERVALS[interval]
        if spec.get("kite_resample"):
            # Kite has no 2 or 4-minute candles: they are built from its minute candles. Whole days are
            # fetched so each day's buckets line up with the session open.
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
        args = {"instrument_token": token, "interval": spec["kite"],
                "from_date": start.strftime("%Y-%m-%d %H:%M:%S"), "to_date": end.strftime("%Y-%m-%d %H:%M:%S")}
        # Continuous: a futures token's history stitched across expired contracts (Kite serves it for day candles only).
        if continuous:
            args["continuous"] = True
        if oi:
            args["oi"] = True
        rows = _rows(self.call("get_historical_data", args, timeout=60.0))
        return _resampled_rows(rows, spec["minutes"]) if spec.get("kite_resample") else rows

    def quotes(self, instruments: list[str]) -> dict[str, dict]:
        """Full quotes (last price, day's open/high/low, previous close, volume, OI), keyed ``EXCHANGE:SYMBOL``."""
        data = self.call("get_quotes", {"instruments": instruments}, timeout=60.0)
        return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}

    def place_order(self, order: dict):
        if "place_order" not in self.tools:
            raise KiteError(
                "This Kite server does not offer order placement. Zerodha's hosted server "
                "(mcp.kite.trade) is read-only; run kite-mcp-server yourself with a Kite "
                "Connect API key and point the MCP settings at it.")
        return self.call("place_order", order, timeout=30.0)


def _needs_oauth(url: str) -> bool:
    """Whether an MCP endpoint refuses an unauthenticated client with a bearer challenge."""
    import httpx

    try:
        response = httpx.post(url, json={"jsonrpc": "2.0", "id": 0, "method": "ping"},
                              headers={"accept": "application/json, text/event-stream"}, timeout=10)
    except httpx.HTTPError:
        return False
    return response.status_code == 401


def _resampled_rows(rows: list, minutes: int) -> list:
    """Kite minute rows merged into ``minutes``-long rows of the same shape (IST timestamps, open interest kept)."""
    import pandas as pd

    from . import data

    frame = data.kite_frame(rows)
    if frame.empty:
        return []
    raw = pd.DataFrame(rows)
    if "oi" in raw:
        stamps = pd.to_datetime(raw["date"])
        if getattr(stamps.dt, "tz", None) is not None:
            stamps = stamps.dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        oi = pd.Series(pd.to_numeric(raw["oi"], errors="coerce").to_numpy(), index=pd.DatetimeIndex(stamps))
        frame["Oi"] = oi[~oi.index.duplicated(keep="last")].reindex(frame.index)
    out = data.resample(frame, minutes)
    return [{"date": stamp.strftime("%Y-%m-%dT%H:%M:%S+0530"), "open": float(r.Open), "high": float(r.High),
             "low": float(r.Low), "close": float(r.Close), "volume": float(r.Volume),
             **({"oi": float(r.Oi)} if "Oi" in out.columns and pd.notna(r.Oi) else {})}
            for stamp, r in zip(out.index, out.itertuples(), strict=True)]


def _rows(data) -> list:
    """The list inside a possibly paginated tool answer."""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("data", "items", "results"):
            if isinstance(data.get(key), list):
                return data[key]
    return []


def _describe(exc: BaseException) -> str:
    inner = getattr(exc, "exceptions", None)      # an ExceptionGroup from the task group
    if inner:
        return "; ".join(_describe(e) for e in inner)
    return str(exc) or type(exc).__name__
