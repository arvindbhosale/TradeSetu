"""The desk's local HTTP API and the page it serves.

Bound to 127.0.0.1 only. Every /api call must carry the session token the page
was served with, and name a local Host: a web page open in the same browser
cannot read the token, so it cannot place an order through this server, and a
DNS-rebinding page fails the Host check.
"""

from __future__ import annotations

import contextlib
import hmac
import html
import logging
import os
import random
import re
import secrets
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pandas as pd
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles

import tradingagents
from tradingagents.dataflows.config import set_config
from tradingagents.dataflows.date_window import get_current_date
from tradingagents.dataflows.symbols import kite_instrument, normalize_symbol, safe_ticker_component
from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV
from tradingagents.portfolio import PortfolioContext, Position

from . import (
    APP_NAME,
    __version__,
    charts,
    custom as custom_module,
    data,
    indicators,
    jobs as jobs_module,
    margin as zerodha_margin,
    market,
    mcx,
    replay as replay_module,
    results,
    risk,
    scalein,
    scaleout,
)
from .algo import AlgoEngine, AlgoError, arm_phrase
from .auth import COOKIE, SESSION_SECONDS, Auth
from .backtest import BacktestParams, run_strategy
from .broker import Desk, OrderRejected
from .brokers import Switch, catalog as broker_catalog
from .chainhistory import (
    DEFAULT_UNDERLYINGS as HISTORY_DEFAULT,
    INDEX_OPTIONS as HISTORY_UNDERLYINGS,
    ChainHistory,
    Recorder,
)
from .chartorders import ChartOrders
from .chartstore import ChartStore
from .cryptopaper import CryptoPaper
from .custom import OPS as RULE_OPS, TIME_OPS as RULE_TIME_OPS, CustomStrategies
from .financials import Financials, peer_rows
from .fno import BSE_INDICES, Derivatives, exchange_of
from .fnoalgo import FnoAlgoEngine, arm_phrase as fno_arm_phrase
from .fundamentals import LABELS as FUNDAMENTAL_LABELS, Fundamentals
from .indices import Indices
from .kite import KiteError, KiteMCP
from .kiteconnect import kite_entry, request_token_from
from .marketdata import (
    TIMEFRAMES as CHART_TIMEFRAMES,
    MarketDataError,
    broker as md_broker,
    rows_from_frame as md_rows,
    weekly as md_weekly,
)
from .marketdata.crypto import PAIRS as CRYPTO_PAIRS, CryptoFeed, tickers as crypto_tickers
from .marketdata.csvfeed import Imports as ChartImports
from .markettools import MarketTools, finite
from .nse import NseClient, exchange_proxy as nse_proxy
from .openpeaks import OpenPeaks
from .optimiser import OBJECTIVES, optimise
from .passkeys import PasskeyError, Passkeys, relying_party
from .scanner import Scanner
from .sectors import Sectors
from .settings import Settings, home
from .stockinfo import stock_view
from .supervisor import Supervisor
from .ticker import Ticker
from .tradenotes import MISTAKES, TradeNotes

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_GRID_CELLS = 60
XLSX_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
HANDOFF_SECONDS = 300            # a phone download link lasts five minutes
HANDOFF_MAX_BYTES = 25 * 1024 * 1024
HANDOFF_MAX_FILES = 20


def create_app(settings: Settings | None = None, kite: KiteMCP | None = None,
               desk: Desk | None = None, token: str | None = None, fetch=None,
               auth: Auth | None = None, allowed_hosts: list[str] | None = None,
               kite_proxy: str | None = None) -> FastAPI:
    """Build the app. ``fetch`` replaces the Yahoo price download (tests).

    Two ways to run it. On a desktop (``auth`` None) it answers only local
    Hosts and trusts the token baked into the page it serves. Hosted (``auth``
    given) it serves a sign-in page and trusts a session cookie plus a CSRF
    header; ``allowed_hosts`` then limits the Host names it answers to, and
    ``kite_proxy`` is the internal kite-mcp-server whose login pages it relays.
    """
    settings = settings or Settings()
    settings.export_keys()
    set_config({"default_exchange": settings.get("default_exchange")})
    kite = kite or Switch()
    if isinstance(kite, Switch):
        kite.use(settings.get("broker"))
    desk = desk or Desk(settings, kite, price_lookup=lambda symbol: data.last_close(symbol, fetch=fetch))
    token = token or secrets.token_urlsafe(32)
    runner = jobs_module.Jobs()
    scan_runner = jobs_module.Jobs("desk-scan")      # long CPU work of its own, so it never queues an agent run
    notes = TradeNotes(desk.ledger.path.parent / "trade_notes.json", now=desk._now)
    library = CustomStrategies()
    algos = AlgoEngine(desk, fetch=fetch, library=library)
    peaks = OpenPeaks(desk, desk.ledger.path.parent / "open_peaks.json", now=desk._now)
    algos.extra_ticks.append(peaks.tick)
    scanner = Scanner(desk, fetch=fetch)
    algos.extra_ticks.append(scanner.tick)
    sectors = Sectors(kite, fetch=fetch, settings=settings)
    derivatives = Derivatives(kite)
    desk.contract_lookup = derivatives.contract
    fundamentals = Fundamentals(fetch=(lambda symbol: {}) if fetch is not None else None)
    offline_json = (lambda url, params: (_ for _ in ()).throw(OSError("offline"))) if fetch is not None else None
    indices = Indices(sectors, fundamentals, kite, fetch=fetch,
                      download=(lambda url: (_ for _ in ()).throw(OSError("offline"))) if fetch is not None else None,
                      get_json=offline_json)
    financials = Financials(NseClient(get_json=offline_json, download=lambda url: (_ for _ in ()).throw(OSError("offline")))
                            if fetch is not None else None)
    tools = MarketTools(sectors, indices, fundamentals, fetch=fetch, get_json=offline_json,
                        download=(lambda url: (_ for _ in ()).throw(OSError("offline"))) if fetch is not None else None,
                        results=financials.results, nse=financials.client)
    ticker = Ticker(indices, lambda: settings.get("ticker_indices"))
    sectors.extra = []                      # filled from index lists so contributor prices stay warm
    first_oi: dict = {"day": None, "values": {}}
    algos.extra_ticks.append(derivatives.tick)

    def option_settlement_spot(underlying: str, expiry: str):
        import pandas as pd

        from . import options as opt
        with contextlib.suppress(Exception):
            if kite.connected:
                frame = data.kite_bars(kite, opt.SPOT[underlying], "1d", market.now_ist(), raw=True)
                day = frame[frame.index.normalize() == pd.Timestamp(expiry)]
                if len(day):
                    return float(day["Close"].iloc[-1])
        with contextlib.suppress(Exception):
            if underlying in opt.YAHOO_SPOT:
                frame = data.history(opt.YAHOO_SPOT[underlying], fetch=fetch)
                day = frame[frame.index.normalize() == pd.Timestamp(expiry)]
                if len(day):
                    return float(day["Close"].iloc[-1])
        return None

    def settle_paper_options():
        desk.settle_paper_options(option_settlement_spot)

    algos.extra_ticks.append(settle_paper_options)
    fno_algos = FnoAlgoEngine(desk, derivatives, fetch=fetch, library=library)
    algos.extra_ticks.append(fno_algos.tick)
    chain_history = ChainHistory(desk.ledger.path.parent / "option_history.db",
                                 client=NseClient(get_json=offline_json, download=lambda url: (_ for _ in ()).throw(OSError("offline")))
                                 if fetch is not None else None, now=lambda: market.now_ist().replace(tzinfo=None))
    chain_recorder = Recorder(chain_history, kite, derivatives, settings)
    algos.extra_ticks.append(chain_recorder.tick)
    algos.extra_live.append(fno_algos.live_names)
    desk.algo_claims = fno_algos.claimed
    algos.extra_ticks.append(desk.close_paper_mcx)
    chart_orders = ChartOrders(desk, desk.ledger.path.parent / "chart_orders.json")
    algos.extra_ticks.append(chart_orders.tick)
    algos.extra_live.append(lambda: [f"chart order {i['tradingsymbol']}" for i in chart_orders.view()["items"]
                                     if i["mode"] == "live" and i["state"] in ("waiting", "protected")])

    def supervisor_ask(prompt: str) -> str:
        from tradingagents.llm_clients import create_tier_client

        reply = create_tier_client({**settings.agent_config(), "llm_max_retries": 1, "max_tokens": 1000},
                                   "quick").get_llm().invoke(prompt)
        content = reply.content
        if isinstance(content, list):          # providers that answer in blocks
            content = "".join(b.get("text", "") if isinstance(b, dict) else str(b) for b in content)
        return str(content)

    supervisor = Supervisor(desk, algos, fno_algos, ask=supervisor_ask, llm_problem=lambda: llm_ready())
    algos.extra_ticks.append(supervisor.tick)
    hosted = auth is not None
    passkeys = Passkeys(home() / "passkeys.json", auth.username) if hosted else None
    local_hosts = ("127.0.0.1", "localhost", "::1", "testserver")
    allowed = {h.strip().lower() for h in (allowed_hosts or []) if h.strip()}

    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token, app.state.settings, app.state.kite, app.state.desk = token, settings, kite, desk
    app.state.chain_history = chain_history
    app.state.algos, app.state.auth, app.state.scanner = algos, auth, scanner
    app.state.sectors, app.state.indices, app.state.fundamentals = sectors, indices, fundamentals
    app.state.ticker = ticker
    app.state.derivatives = derivatives
    app.state.tools = tools
    app.state.financials = financials
    app.state.fno_algos = fno_algos
    app.state.supervisor = supervisor

    @app.middleware("http")
    async def guard(request: Request, call_next):
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].strip("[]").lower()
        path = request.url.path
        if path == "/healthz":
            return await call_next(request)      # the container health check, by IP
        if (not hosted and host not in local_hosts) or (hosted and allowed and host not in allowed
                                                        and host not in local_hosts):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        if hosted:
            session = auth.session(request.cookies.get(COOKIE))
            request.state.session = session
            # /broker/callback: a broker's sign-in page redirecting back. No session cookie comes with a
            # cross-site redirect; the one-time OAuth state it must carry is the check instead.
            # /dl/<token>: a file handed to the phone's own browser to save (the app's WebView cannot); the
            # long random token, made by a signed-in request a few minutes ago, is the check.
            public = path in ("/login", "/api/login", "/api/passkey/options", "/api/passkey/login", "/sw.js",
                              "/broker/callback") \
                or path.startswith(("/static/", "/kite/", "/dl/"))
            if not session and not public:
                if path.startswith("/api/"):
                    return JSONResponse({"error": "Sign in again.", "signed_out": True}, status_code=401)
                return RedirectResponse("/login", status_code=303)
            if session and path.startswith("/api/") and not public and not hmac.compare_digest(
                    request.headers.get("x-desk-token", ""), session["csrf"]):
                return JSONResponse({"error": "missing or wrong request token"}, status_code=403)
        elif path.startswith("/api/"):
            sent = request.headers.get("x-desk-token", "")
            if not hmac.compare_digest(sent, token):
                return JSONResponse({"error": "missing or wrong session token"}, status_code=401)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        if hosted and request.headers.get("x-forwarded-proto", request.url.scheme) == "https":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        if path.startswith("/kite/"):
            return response                       # Zerodha's own pages carry their own policy
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
            "connect-src 'self' wss://data-stream.binance.vision wss://ws-feed.exchange.coinbase.com; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'")
        return response

    @app.exception_handler(OrderRejected)
    async def rejected(_, exc: OrderRejected):
        return JSONResponse({"error": str(exc), "errors": exc.reasons}, status_code=422)

    @app.exception_handler(KiteError)
    async def kite_failed(_, exc: KiteError):
        return JSONResponse({"error": str(exc), "needs_login": exc.needs_login}, status_code=502)

    @app.exception_handler(MarketDataError)
    async def market_data_failed(_, exc: MarketDataError):
        return JSONResponse({"error": str(exc), "provider": exc.provider, "needs_login": exc.needs_login},
                            status_code=502)

    @app.exception_handler(data.NoData)
    async def no_data(_, exc: data.NoData):
        return JSONResponse({"error": str(exc)}, status_code=404)

    @app.exception_handler(AlgoError)
    async def algo_failed(_, exc: AlgoError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.exception_handler(ValueError)
    async def bad_value(_, exc: ValueError):
        return JSONResponse({"error": str(exc)}, status_code=400)

    def body_of(payload) -> dict:
        if not isinstance(payload, dict):
            raise ValueError("the request body must be a JSON object")
        return payload

    def symbol_of(raw) -> str:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError("Enter a symbol, e.g. RELIANCE, TCS.NS, BSE:TCS or NIFTY")
        try:
            return safe_ticker_component(normalize_symbol(raw))
        except ValueError:
            raise ValueError(f"“{raw.strip()[:40]}” is not a valid symbol. Use an NSE symbol (RELIANCE), "
                             f"a BSE one (BSE:RELIANCE), or an index (NIFTY, SENSEX).") from None

    def date_of(raw, label: str) -> str:
        try:
            if datetime.strptime(str(raw), "%Y-%m-%d").strftime("%Y-%m-%d") != str(raw):
                raise ValueError
        except (TypeError, ValueError):
            raise ValueError(f"{label} must be a date as YYYY-MM-DD") from None
        return str(raw)

    def llm_ready() -> str | None:
        provider = settings.get("llm_provider")
        if provider not in PROVIDER_API_KEY_ENV:
            return f"Unknown LLM provider: {provider}"
        env = PROVIDER_API_KEY_ENV[provider]
        import os
        url = settings.get("backend_url")
        if provider == "openai_compatible" and not url:
            return ("Set the Endpoint URL of your AI service in Settings (its base address, usually ending "
                    "in /v1), and its model name in both model boxes.")
        if url:
            host = url.split("/")[2] if url.startswith(("http://", "https://")) and len(url.split("/")) > 2 else ""
            if not host or "example" in host:
                return f"The Endpoint URL “{url}” is not a real address. Enter your AI service's base URL in Settings."
        if provider == "openai_compatible" and not settings.get("quick_think_llm"):
            return "Enter the model name your AI service expects in both model boxes in Settings."
        if env and provider != "openai_compatible" and not os.environ.get(env):
            return f"Add your {provider} API key ({env}) in Settings before running the agents."
        return None

    # -- page ---------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request):
        page = (STATIC / "index.html").read_text(encoding="utf-8")
        return page.replace("__DESK_TOKEN__", request.state.session["csrf"] if hosted else token)

    @app.get("/manual", response_class=HTMLResponse)
    def manual():
        return _manual_page()

    @app.get("/notes", response_class=HTMLResponse)
    def notes_view():
        return notes_page()

    @app.get("/notes.pdf")
    def notes_pdf():
        path = STATIC / "notes" / NOTES_PDF
        if not path.exists():
            raise HTTPException(404, "The PDF of the notes has not been built.")
        return FileResponse(path, media_type="application/pdf", filename=NOTES_PDF)

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so its scope is the whole app, not just /static/.
        return Response((STATIC / "sw.js").read_text(encoding="utf-8"), media_type="text/javascript",
                        headers={"Service-Worker-Allowed": "/"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    if hosted:
        @app.get("/login", response_class=HTMLResponse)
        def login_page(request: Request):
            if request.state.session:
                return RedirectResponse("/", status_code=303)
            return (STATIC / "login.html").read_text(encoding="utf-8")

        @app.post("/api/login")
        def login(payload: dict, request: Request):
            payload = body_of(payload)
            client = request.client.host if request.client else "unknown"
            try:
                session = auth.login(str(payload.get("username", "")), str(payload.get("password", "")), client,
                                     str(payload.get("code", "")))
            except PermissionError as exc:
                return JSONResponse({"error": str(exc)}, status_code=429)
            except LookupError as exc:
                return JSONResponse({"error": str(exc), "needs_code": True}, status_code=401)
            if not session:
                second = auth.two_factor is not None and auth.two_factor.enabled
                return JSONResponse({"error": "Wrong username, password or code." if second
                                     else "Wrong username or password.", "needs_code": second}, status_code=401)
            response = JSONResponse({"ok": True})
            secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            response.set_cookie(COOKIE, session["id"], max_age=SESSION_SECONDS, httponly=True,
                                samesite="strict", secure=secure, path="/")
            return response

        def _client(request: Request) -> str:
            return request.client.host if request.client else "unknown"

        def _sign_in(request: Request, session: dict) -> JSONResponse:
            response = JSONResponse({"ok": True})
            secure = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
            response.set_cookie(COOKIE, session["id"], max_age=SESSION_SECONDS, httponly=True,
                                samesite="strict", secure=secure, path="/")
            return response

        @app.post("/api/passkey/options")
        def passkey_options(request: Request):
            wait = auth.locked_for(_client(request))
            if wait:
                return JSONResponse({"error": f"Too many failed sign-ins. Try again in {wait // 60 + 1} minutes."}, status_code=429)
            if not passkeys.count():
                return JSONResponse({"error": "No passkey has been added yet. Sign in with your password, then add one in Settings."},
                                    status_code=404)
            rp_id, origin = relying_party(request, os.environ.get("DESK_PUBLIC_URL", ""))
            return passkeys.authentication_options(rp_id, origin)

        @app.post("/api/passkey/login")
        def passkey_login(payload: dict, request: Request):
            payload, client = body_of(payload), _client(request)
            wait = auth.locked_for(client)
            if wait:
                return JSONResponse({"error": f"Too many failed sign-ins. Try again in {wait // 60 + 1} minutes."}, status_code=429)
            try:
                used = passkeys.authenticate(str(payload.get("token", "")), payload.get("credential") or {})
            except PasskeyError as exc:
                auth.failed(client, f"passkey: {exc}")
                return JSONResponse({"error": str(exc)}, status_code=401)
            desk.notifier.notify("Signed in with a passkey", f"{used['name']} from {client}.", "info")
            return _sign_in(request, auth.passkey_session(client))

        @app.post("/api/logout")
        def logout(request: Request):
            auth.logout(request.cookies.get(COOKIE))
            response = JSONResponse({"ok": True})
            response.delete_cookie(COOKIE, path="/")
            return response

    # -- passkeys --------------------------------------------------------------

    def passkey_store():
        if not hosted:
            raise ValueError("Passkeys apply to the hosted desk only.")
        return passkeys

    @app.get("/api/passkeys")
    def passkey_list():
        if not hosted:
            return {"available": False, "passkeys": []}
        return {"available": True, "passkeys": passkeys.listing()}

    @app.post("/api/passkeys/options")
    def passkey_register_options(payload: dict, request: Request):
        store = passkey_store()
        if not auth.check_password(body_of(payload).get("password", "")):
            raise ValueError("Enter your current password to add a passkey.")
        rp_id, origin = relying_party(request, os.environ.get("DESK_PUBLIC_URL", ""))
        return store.registration_options(rp_id, origin)

    @app.post("/api/passkeys")
    def passkey_register(payload: dict):
        payload = body_of(payload)
        added = passkey_store().register(str(payload.get("token", "")), payload.get("credential") or {},
                                         str(payload.get("name", "")))
        desk.notifier.notify("Passkey added", f"{added['name']} can now sign in to the desk.", "warn")
        return {"added": added, "passkeys": passkeys.listing()}

    @app.delete("/api/passkeys/{key_id}")
    def passkey_remove(key_id: str):
        if not passkey_store().remove(key_id):
            raise ValueError("No such passkey.")
        desk.notifier.notify("Passkey removed", "It can no longer sign in.", "warn")
        return {"passkeys": passkeys.listing()}

    # -- two-factor sign-in ---------------------------------------------------

    def second_factor():
        if not hosted or auth.two_factor is None:
            raise ValueError("Two-factor sign-in applies to the hosted desk only.")
        return auth.two_factor

    @app.get("/api/2fa")
    def two_factor_status():
        if not hosted or auth.two_factor is None:
            return {"available": False, "enabled": False}
        return {"available": True, "enabled": auth.two_factor.enabled, "recovery_left": auth.two_factor.recovery_left()}

    @app.post("/api/2fa/setup")
    def two_factor_setup(payload: dict):
        second = second_factor()
        if second.enabled:
            raise ValueError("Two-factor sign-in is already on. Turn it off first to set it up again.")
        if not auth.check_password(body_of(payload).get("password", "")):
            raise ValueError("Enter your current password to set up two-factor sign-in.")
        import segno

        started = second.begin(auth.username)
        import io

        drawing = io.BytesIO()
        # With the SVG namespace, so the browser draws it when the page inserts it as a parsed document.
        segno.make(started["uri"], error="m").save(drawing, kind="svg", xmldecl=False, svgns=True, nl=False,
                                                    scale=5, border=2, dark="#000", light="#fff")
        started["qr_svg"] = drawing.getvalue().decode()
        return started

    @app.post("/api/2fa/enable")
    def two_factor_enable(payload: dict):
        codes = second_factor().confirm(str(body_of(payload).get("code", "")))
        desk.notifier.notify("Two-factor sign-in switched ON", "Sign-in now needs a code from your authenticator app.", "good")
        return {"enabled": True, "recovery_codes": codes}

    @app.post("/api/2fa/disable")
    def two_factor_disable(payload: dict):
        second, payload = second_factor(), body_of(payload)
        if not auth.check_password(payload.get("password", "")) or not second.verify(str(payload.get("code", ""))):
            raise ValueError("Enter your password and a current code (or a recovery code) to turn two-factor off.")
        second.disable()
        desk.notifier.notify("Two-factor sign-in switched OFF", "Sign-in needs only the password again.", "warn")
        return {"enabled": False}

    if kite_proxy:
        import httpx

        @app.get("/kite/{page}")
        def kite_relay(page: str, request: Request):
            """Relay the Kite sign-in pages of the internal kite-mcp-server.

            Only these two paths are reachable from outside; its MCP endpoint,
            which has no authentication of its own, stays on the private network.
            """
            if page not in ("authorize", "callback"):
                raise HTTPException(404, "not found")
            try:
                upstream = httpx.get(f"{kite_proxy.rstrip('/')}/{page}", params=dict(request.query_params),
                                     timeout=30, follow_redirects=False)
            except httpx.HTTPError as exc:
                raise HTTPException(502, f"the Kite server is not answering: {exc}") from exc
            headers = {k: v for k, v in upstream.headers.items() if k.lower() in ("content-type", "location")}
            return Response(upstream.content, status_code=upstream.status_code, headers=headers)

    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    # -- state and settings ---------------------------------------------------

    @app.get("/api/state")
    def state():
        return {
            "app": APP_NAME, "version": __version__, "engine": tradingagents.__version__,
            "today": get_current_date(), "settings": settings.public(),
            "providers": dict(PROVIDER_API_KEY_ENV.items()),
            "kite": kite.status(), "brokers": broker_catalog(), "session": market.session_status(), "llm_problem": llm_ready(),
            "hosted": hosted, "user": auth.username if hosted else None,
            "window": desk.window_status(), "llm_presets": LLM_PRESETS, "llm_models": _llm_models(),
            "trend_methods": __import__("desk.trend", fromlist=["METHODS"]).METHODS,
            "fundamental_labels": FUNDAMENTAL_LABELS, "optimise_objectives": OBJECTIVES,
            "structures": _structures(), "screens": _screens(), "portfolios": _portfolios(),
            "strategies": [s.describe() for s in library.all()],
            "indicators": indicators.describe(), "candle_patterns": _candle_patterns(), "rule_ops": RULE_OPS, "rule_time_ops": RULE_TIME_OPS,
            "chart_overlays": charts.OVERLAYS, "chart_panels": charts.PANELS,
            "intervals": {key: spec["label"] for key, spec in data.INTERVALS.items()},
        }

    @app.put("/api/settings")
    def put_settings(payload: dict):
        updated = settings.update(body_of(payload))
        set_config({"default_exchange": settings.get("default_exchange")})
        if isinstance(kite, Switch):
            kite.use(settings.get("broker"))      # a different broker: the old connection closes
        return {"settings": updated, "llm_problem": llm_ready()}

    @app.post("/api/llm/test")
    def llm_test():
        """Send one tiny request to the configured AI service and report what came back."""
        if problem := llm_ready():
            return {"ok": False, "error": problem}
        config = settings.agent_config()
        where = config.get("backend_url") or f"the default address of {config['llm_provider']}"
        out = {}
        for tier in ("quick", "deep"):
            model = config[f"{tier}_think_llm"]
            try:
                from tradingagents.llm_clients import create_tier_client

                reply = create_tier_client({**config, "llm_max_retries": 0, "max_tokens": 64}, tier).get_llm().invoke(
                    "Reply with the single word OK.")
                text = reply.content if isinstance(reply.content, str) else str(reply.content)
                out[tier] = {"ok": True, "model": model, "reply": text.strip()[:80]}
            except Exception as exc:  # noqa: BLE001 - the point is to show the user this error
                detail = f"{type(exc).__name__}: {exc}"[:400]
                hint = ""
                low = detail.lower()
                if "connection" in low or "timeout" in low:
                    hint = (f"The desk's server could not reach {where}. Check the address; from a server, "
                            f"'localhost' means the server itself, not your computer.")
                elif "401" in low or "auth" in low or "api key" in low:
                    hint = "The service refused the key. Paste the key again and save."
                elif "404" in low or "model" in low:
                    hint = f"The service does not know the model “{model}”. Use the exact model name it lists."
                elif "429" in low or "rate" in low or "quota" in low:
                    hint = "The service is rate-limiting this key. Free tiers often cannot carry a full analysis."
                out[tier] = {"ok": False, "model": model, "error": detail, "hint": hint}
        return {"ok": all(t["ok"] for t in out.values()), "endpoint": where, **out}

    # -- alerts and P&L --------------------------------------------------------

    @app.get("/api/alerts")
    def alerts():
        return {"alerts": desk.notifier.recent(), "last_error": desk.notifier.last_error}

    @app.post("/api/alerts/test")
    def alerts_test():
        return desk.notifier.test()

    @app.get("/api/pnl")
    def pnl(mode: str = "paper"):
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if mode == "live":
            desk.sync_live_fills()
        report = desk.pnl(mode)
        report["loss_limit"] = settings.get("daily_loss_limit")
        return report

    @app.get("/api/funds")
    def funds(mode: str = "paper"):
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        return desk.funds(mode)

    # -- futures and options --------------------------------------------------

    @app.get("/api/fno")
    def fno_view():
        if derivatives.stale() and kite.connected:
            with contextlib.suppress(KiteError):
                derivatives.refresh()
        return derivatives.view()

    @app.post("/api/fno/refresh")
    def fno_refresh():
        return derivatives.refresh()

    @app.get("/api/margin/underlyings")
    def margin_underlyings():
        if derivatives.stale() and kite.connected:
            with contextlib.suppress(KiteError):
                derivatives.refresh()
        return {"underlyings": derivatives.margin_underlyings(), "available": zerodha_margin.why_not(kite) is None,
                "reason": zerodha_margin.why_not(kite)}

    @app.get("/api/margin/contracts")
    def margin_contracts(underlying: str):
        return {"underlying": underlying.strip().upper(), "contracts": derivatives.chain(underlying)}

    @app.post("/api/margin")
    def margin_calculate(payload: dict):
        """Zerodha's margin for legs picked in the calculator: {legs: [{underlying, tradingsymbol, side, lots}],
        product, mode}. Lots become quantities from the contract's lot size."""
        body = body_of(payload)
        mode = body.get("mode", "paper")
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        product = str(body.get("product") or "NRML").upper()
        if product not in ("NRML", "MIS"):
            raise ValueError("product must be NRML or MIS")
        raw = body.get("legs")
        if not isinstance(raw, list) or not raw:
            raise ValueError("Add at least one leg.")
        legs = []
        for item in raw:
            if not isinstance(item, dict):
                raise ValueError("each leg must be an object")
            symbol = str(item.get("tradingsymbol", "")).strip().upper()
            contract = next((c for c in derivatives.chain(str(item.get("underlying", ""))) if c["tradingsymbol"] == symbol), None)
            if contract is None:
                raise ValueError(f"{symbol or 'A leg'} is not a live contract of {item.get('underlying')}.")
            try:
                lots = int(item.get("lots"))
            except (TypeError, ValueError):
                raise ValueError(f"Lots for {symbol} must be a whole number.") from None
            if not 1 <= lots <= 500:
                raise ValueError(f"Lots for {symbol} must be between 1 and 500.")
            side = str(item.get("side", "")).upper()
            if side not in ("BUY", "SELL"):
                raise ValueError(f"Choose Buy or Sell for {symbol}.")
            legs.append({"exchange": contract["exchange"], "tradingsymbol": symbol, "side": side, "lots": lots,
                         "quantity": lots * int(contract.get("lot_size") or 1)})
        try:
            result = zerodha_margin.basket(kite, legs, product=product)
        except zerodha_margin.MarginUnavailable as exc:
            return JSONResponse({"error": str(exc), "needs_login": exc.needs_login}, status_code=409)
        for row, leg in zip(result["legs"], legs, strict=True):
            row["lots"] = leg["lots"]
        segment = "commodity" if all(g["exchange"] == "MCX" for g in legs) else "equity"
        if any(g["exchange"] == "MCX" for g in legs) and segment == "equity":
            funds, funds_note = None, "MCX and NSE/BSE legs use different Kite funds; compare each part separately."
        elif mode == "live":
            funds = desk._live_cash(segment)
            funds_note = None if funds is not None else "Kite did not report your available funds."
        else:
            funds, funds_note = desk.paper.cash, None
        return {**result, "mode": mode, "funds": funds, "funds_note": funds_note,
                "short": funds is not None and result["total"] > funds}

    # -- sectors -------------------------------------------------------------

    @app.get("/api/sectors")
    def sector_cards():
        return sectors.cards()

    @app.get("/api/sectors/{sector_id}")
    def sector_detail(sector_id: str):
        return sectors.detail(sector_id, fundamentals)

    @app.post("/api/sectors")
    def sector_create(payload: dict):
        payload = body_of(payload)
        if payload.get("reset"):
            sectors.reset()
            return sectors.cards()
        return sectors.save_sector(payload.get("name", ""), payload.get("stocks"))

    @app.put("/api/sectors/{sector_id}")
    def sector_update(sector_id: str, payload: dict):
        payload = body_of(payload)
        return sectors.save_sector(payload.get("name", ""), payload.get("stocks"), sector_id)

    @app.delete("/api/sectors/{sector_id}")
    def sector_delete(sector_id: str):
        sectors.delete(sector_id)
        return {"ok": True}

    @app.get("/api/symbol")
    def symbol(q: str):
        canonical = symbol_of(q)
        instrument = kite_instrument(canonical)
        return {"symbol": canonical,
                "kite": {"exchange": instrument[0], "tradingsymbol": instrument[1]} if instrument else None}

    # -- backtest -------------------------------------------------------------

    def trade_type_of(payload: dict, interval: str) -> str:
        """Intraday or positional. Left out, it follows the candle: day candles positional, intraday candles
        intraday, as every run and algo before the choice existed."""
        kind = str(payload.get("trade_type") or ("positional" if interval == "1d" else "intraday")).lower()
        if kind not in ("intraday", "positional"):
            raise ValueError("Trade type must be intraday or positional")
        if kind == "intraday" and interval == "1d":
            raise ValueError("An intraday trade needs minute or hourly candles. Day candles are positional.")
        return kind

    def hold_days_of(payload: dict) -> int:
        value = payload.get("max_hold_days") or 0
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value):
            raise ValueError("Sell after (days held) must be a whole number of days")
        return int(value)

    @app.post("/api/backtest/strategy")
    def backtest_strategy(payload: dict):
        payload = body_of(payload)
        canonical = symbol_of(payload.get("symbol"))
        start = date_of(payload.get("start"), "Start date")
        end = date_of(payload.get("end"), "End date")
        if end <= start:
            raise ValueError("End date must be after the start date")
        cfg = settings.all()
        interval = str(payload.get("interval", "1d"))
        if interval not in data.INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(data.INTERVALS)}")
        # Rules typed on the Backtest screen ("Buy when / Sell when") need not be saved first.
        strategy = (custom_module.inline(payload["rules"]) if payload.get("rules")
                    else library.strategy(str(payload.get("strategy", ""))))
        exits = risk.from_payload(payload.get("exits"), "Stock", legacy={
            "stop_loss": _number(payload, "stop_loss_pct", 0.0), "target": _number(payload, "target_pct", 0.0)})
        params = BacktestParams(
            capital=_number(payload, "capital", cfg["paper_capital"]),
            allocation_pct=_number(payload, "allocation_pct", 100.0),
            exits=exits,
            trailing_stop_pct=_number(payload, "trailing_stop_pct", 0.0),
            slippage_bps=_number(payload, "slippage_bps", cfg["slippage_bps"]),
            risk_free_rate_pct=cfg["risk_free_rate_pct"],
            exchange="BSE" if canonical.endswith(".BO") else "NSE",
            intraday=interval != "1d", positional=trade_type_of(payload, interval) == "positional",
            max_hold_days=hold_days_of(payload), trade_start=str(payload.get("trade_start") or cfg["trade_start"]),
            trade_end=str(payload.get("trade_end") or cfg["trade_end"]),
            square_off=str(payload.get("square_off") or cfg["square_off"]),
        )
        bars, source = stock_bars(canonical, interval, start, end)
        index_symbol = "^BSESN" if canonical.endswith(".BO") else "^NSEI"
        benchmark, benchmark_note = None, None
        if canonical != index_symbol:
            try:
                benchmark = data.history(index_symbol, start, end, fetch=fetch)["Close"]
            except data.NoData as exc:
                benchmark_note = f"Benchmark unavailable: {exc}"
        steps = scaleout.from_payload(payload.get("steps"), "stock")
        adds = scalein.from_payload(payload.get("adds"), "stock")
        result = scalein.run_all(lambda s, a: run_strategy(
            bars, strategy, payload.get("strategy_params"),
            replace(params, steps=s if s.active else None, adds=a if a.active else None), benchmark), steps, adds)
        result["symbol"], result["interval"] = canonical, interval
        result["benchmark_name"] = {"^NSEI": "Nifty 50", "^BSESN": "Sensex"}[index_symbol] if result["benchmark"] else None
        result["benchmark_note"] = benchmark_note
        result["data_note"], result["data_source"] = source["note"], source
        return result

    def stock_bars(canonical: str, interval: str, start: str, end: str):
        """A stock's candles for a backtest: Kite first when it is logged in, the free feed (Yahoo) otherwise, and
        a ``source`` saying which one served them and whether they cover the dates asked for."""
        import pandas as pd

        label = data.INTERVALS[interval]["label"]
        bars, kite_reason, notes = None, None, []
        status = kite.status() if kite.connected else {}
        broker = "Kite" if status.get("broker", "kite") == "kite" else (status.get("broker_label") or "Your broker")
        if kite.connected:
            try:
                bars = data.kite_range(kite, canonical, interval, start, end,
                                       reference=lambda: data.history(canonical, fetch=fetch))
                name = broker
                if bars.attrs.get("from_clipped") is not None:
                    notes.append(f"{name} keeps {label} candles from {data.KITE_INTRADAY_FROM:%d %b %Y}, so the test starts there.")
                for day, ratio in bars.attrs.get("adjusted") or []:
                    notes.append(f"The price opened {(1 - 1 / ratio) * 100:.0f}% lower on {day}, a split or bonus {name} had not "
                                 f"adjusted; earlier prices are divided by {ratio:.3g} so it does not read as a loss.")
            except (KiteError, data.NoData) as exc:
                kite_reason = (f"{broker} needs a login on the Trade tab" if getattr(exc, "needs_login", False)
                               else f"{broker} could not serve them: {str(exc)[:160]}")
        else:
            kite_reason = "your broker is not logged in"
        if bars is None:
            bars = data.bars_for(canonical, interval, start, end, fetch=fetch)
            name = "Yahoo"
            if bars.attrs.get("note"):
                notes.append(bars.attrs["note"])
        first, last = bars.index[0], bars.index[-1]
        # Short: the candles start more than a week after the start date, or stop more than a week before the end.
        short = first.normalize() > pd.Timestamp(start) + pd.Timedelta(days=7) \
            or last.normalize() < min(pd.Timestamp(end), pd.Timestamp.now().normalize()) - pd.Timedelta(days=7)
        head = f"Prices from {name}: {len(bars):,} {label} candles, {first:%d %b %Y} to {last:%d %b %Y}."
        if name == "Yahoo":
            head = (f"Prices from Yahoo ({kite_reason}): {len(bars):,} {label} candles, {first:%d %b %Y} to {last:%d %b %Y}."
                    + (f" Yahoo keeps only {_PERIOD_WORDS.get(data.INTERVALS[interval]['period'], data.INTERVALS[interval]['period'])}"
                       f" of {label} candles." if interval != "1d" else ""))
        if short:
            head += f" You asked for {pd.Timestamp(start):%d %b %Y} to {pd.Timestamp(end):%d %b %Y}: the results cover only the dates above."
        return bars, {"name": name, "first": first.strftime("%Y-%m-%d"), "last": last.strftime("%Y-%m-%d"),
                      "start": start, "end": end, "short": bool(short), "kite_error": kite_reason if kite.connected else None,
                      "note": " ".join([head, *notes])}

    @app.get("/api/strategies")
    def strategy_list():
        return {"strategies": [s.describe() for s in library.all()], "custom": library.list()}

    @app.post("/api/strategies")
    def strategy_create(payload: dict):
        return library.save(body_of(payload))

    @app.put("/api/strategies/{strategy_id}")
    def strategy_update(strategy_id: str, payload: dict):
        return library.save(body_of(payload), strategy_id)

    @app.delete("/api/strategies/{strategy_id}")
    def strategy_delete(strategy_id: str):
        in_use = [a["name"] for a in algos.list() if a["strategy"] == f"custom:{strategy_id}"]
        if in_use:
            raise ValueError(f"Used by algo {', '.join(in_use)}. Delete the algo first.")
        library.delete(strategy_id)
        return {"ok": True}

    @app.post("/api/backtest/agents")
    def backtest_agents(payload: dict):
        payload = body_of(payload)
        if problem := llm_ready():
            raise ValueError(problem)
        raw = payload.get("symbols")
        if isinstance(raw, str):
            raw = [part for part in raw.replace(",", " ").split() if part]
        if not isinstance(raw, list) or not raw:
            raise ValueError("Give at least one symbol")
        symbols = list(dict.fromkeys(symbol_of(s) for s in raw))
        start = date_of(payload.get("start"), "Start date")
        end = date_of(payload.get("end"), "End date")
        every = int(_number(payload, "every_n_days", 7))
        from tradingagents.backtest import iter_grid
        dates = [d for d in iter_grid(start, end, every) if datetime.strptime(d, "%Y-%m-%d").weekday() < 5]
        if not dates:
            raise ValueError("No weekdays fall in that range")
        cells = len(symbols) * len(dates)
        if cells > MAX_GRID_CELLS:
            raise ValueError(f"{cells} agent runs is above the limit of {MAX_GRID_CELLS}. Each run makes "
                             f"dozens of LLM calls; narrow the range, widen the step, or use fewer symbols.")
        config, analysts = settings.agent_config(), settings.get("analysts")
        job = runner.submit("agent_backtest", f"Agent backtest: {', '.join(symbols)} × {len(dates)} dates",
                            lambda job: jobs_module.agent_backtest(job, config, analysts, symbols, dates))
        return {"job": job.view(), "cells": cells}

    # -- charts and algos -------------------------------------------------------

    @app.get("/api/chart")
    def chart(symbol: str = "", interval: str = "1d", overlays: str = "", panels: str = "", start: str = "",
              instrument: str = ""):
        if not settings.get("charts_enabled"):
            raise ValueError("Charts are switched off in Settings.")
        if interval not in data.INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(data.INTERVALS)}")
        if symbol.upper().startswith("IDX:"):
            index_id = symbol[4:].lower()
            entry = indices.entry(index_id)
            if entry is None:
                raise ValueError("no such index")
            instrument = indices.kite_name(index_id) or ""
            if not instrument:
                if entry[5]:
                    symbol = entry[5]
                else:
                    raise ValueError(f"Kite has not named {entry[1]} yet. Connect and log in to Kite, then reopen it.")
        if instrument:
            # An index or a futures and options contract, by its Kite name: Kite only.
            import re
            if not re.fullmatch(r"(NSE|BSE|NFO|BFO|MCX):[A-Z0-9 &\-]{1,40}", instrument.strip().upper()):
                raise ValueError("instrument must be EXCHANGE:NAME, for example NSE:NIFTY 50 or NFO:NIFTY25NOV24500CE")
            instrument = instrument.strip().upper()
            if not kite.connected:
                raise ValueError("This chart comes from Kite. Open Trade, press Connect and log in to Kite.")
            try:
                bars = data.kite_bars(kite, instrument, interval, market.now_ist(), raw=True)
            except (KiteError, data.NoData) as exc:
                reason = "Log in to Kite on the Trade tab." if getattr(exc, "needs_login", False) else str(exc)[:200]
                raise ValueError(f"Kite could not serve this chart. {reason}") from exc
            out = charts.build(bars, interval, [o for o in overlays.split(",") if o],
                               [p for p in panels.split(",") if p], date_of(start, "Start date") if start else None)
            out.update(symbol=instrument, note=None, source="Kite", session=market.session_status(), live_price=None)
            with contextlib.suppress(Exception):
                out["live_price"] = kite.ltp([instrument]).get(instrument)
            return out
        canonical = symbol_of(symbol)
        source, note = "Kite", None
        try:
            if not kite.connected:
                raise KiteError("Kite is not connected. Open Trade, press Connect and log in to Kite.")
            bars = data.kite_bars(kite, canonical, interval, market.now_ist())
        except (KiteError, data.NoData) as exc:
            if settings.get("chart_source") != "kite_then_yahoo":
                reason = "Log in to Kite on the Trade tab." if getattr(exc, "needs_login", False) else str(exc)
                raise ValueError(
                    f"Charts use Kite's data. {reason} Kite serves candles only on the paid Kite Connect plan "
                    f"(the free Personal API has no market data). To fall back to delayed Yahoo data, switch it "
                    f"on in Settings.") from exc
            bars = data.bars_for(canonical, interval, fetch=fetch)
            source, note = "Yahoo (delayed)", f"Kite could not serve this chart ({str(exc)[:120]}); showing delayed Yahoo data."
        out = charts.build(bars, interval, [o for o in overlays.split(",") if o],
                           [p for p in panels.split(",") if p], date_of(start, "Start date") if start else None)
        out["symbol"], out["note"], out["source"] = canonical, note or bars.attrs.get("note"), source
        out["session"] = market.session_status()
        instrument = kite_instrument(canonical)
        out["live_price"] = None
        if instrument and kite.connected:
            price, source = desk.last_price(f"{instrument[0]}:{instrument[1]}")
            if price and source.startswith("Kite"):
                out["live_price"] = price
        return out

    # -- live charts (Charts tab): crypto public data or the connected broker, never Yahoo -------------------
    app.state.crypto = CryptoFeed()

    def chart_broker() -> str:
        status = kite.status() or {}
        return "Kite" if status.get("broker", "kite") == "kite" else (status.get("broker_label") or "Your broker")

    def chart_instrument(symbol: str) -> tuple[str, bool, str]:
        """An NSE/BSE chart symbol as (what the broker is asked for, raw EXCHANGE:NAME?, display name)."""
        symbol = (symbol or "").strip()
        if not symbol:
            raise ValueError("Enter a symbol.")
        if symbol.upper().startswith("IDX:"):
            entry = indices.entry(symbol[4:].lower())
            if entry is None:
                raise ValueError("no such index")
            name = indices.kite_name(entry[0])
            if not name:
                broker = chart_broker()
                raise MarketDataError(f"{broker} has not named {entry[1]} yet. Connect and log in to {broker}, then "
                                      f"reopen it.", broker, needs_login=True)
            return name, True, entry[1]
        if re.fullmatch(r"(NSE|BSE|NFO|BFO|MCX):[A-Z0-9 &\-]{1,40}", symbol.upper()):
            return symbol.upper(), True, symbol.upper()
        canonical = symbol_of(symbol)
        return canonical, False, canonical

    app.state.chartstore = ChartStore(home() / "charts.json")
    app.state.imports = ChartImports(home() / "chart_imports")
    app.state.cpaper = CryptoPaper(home() / "crypto_paper.json", lambda symbols: crypto_tickers(app.state.crypto, symbols))

    @app.get("/api/md/catalog")
    def md_catalog():
        return {"timeframes": {k: v["label"] for k, v in CHART_TIMEFRAMES.items()},
                "crypto": [{"symbol": p[0], "coin": p[1], "name": p[2]} for p in CRYPTO_PAIRS],
                "indices": [{"symbol": "IDX:" + e[0], "name": e[1]} for e in indices.entries()],
                "imports": app.state.imports.list()}

    @app.get("/api/md/candles")
    def md_candles(venue: str = "crypto", symbol: str = "", tf: str = "1h"):
        if not settings.get("charts_enabled"):
            raise ValueError("Charts are switched off in Settings.")
        if venue == "crypto":
            out = app.state.crypto.candles(symbol, tf)
            return {**out, "market": "crypto", "tf": tf, "session": {"open": True, "note": "Crypto trades all day, every day."}}
        if venue == "imported":
            out = app.state.imports.candles(symbol, tf)
            return {**out, "symbol": symbol.strip().upper(), "market": "imported", "tf": tf,
                    "session": {"open": False, "note": "Imported prices are not live."}}
        if venue != "india":
            raise ValueError("venue must be crypto or india")
        instrument, raw, name = chart_instrument(symbol)
        out = md_broker.candles(kite, instrument, tf, market.now_ist(), raw, chart_broker())
        return {**out, "symbol": symbol.strip().upper(), "name": name, "market": "india", "tf": tf,
                "live": {"kind": "poll", "every": 3}, "session": market.session_status()}

    @app.get("/api/md/quote")
    def md_quote(symbol: str = ""):
        """The last traded price of an NSE/BSE chart symbol, from the broker only."""
        instrument, raw, _ = chart_instrument(symbol)
        broker = chart_broker()
        if not raw:
            pair = kite_instrument(instrument)
            if not pair:
                raise MarketDataError(f"{broker} has no instrument for {instrument}.", broker)
            instrument = f"{pair[0]}:{pair[1]}"
        if not kite.connected:
            raise MarketDataError(f"Live prices come from your broker. Open Trade, press Connect and log in to {broker}.",
                                  broker, needs_login=True)
        try:
            price = kite.ltp([instrument]).get(instrument)
        except KiteError as exc:
            raise MarketDataError(f"{broker} could not give the live price. {str(exc)[:200]}", broker,
                                  needs_login=exc.needs_login) from exc
        if not price:
            raise MarketDataError(f"{broker} gave no live price for {instrument}.", broker)
        return {"price": price, "session": market.session_status()}

    # -- price alerts: a share, a future or a coin reaching a price, to the alert log and Telegram ----------
    def _india_prices(instruments: list[str]) -> dict:
        if not kite.connected:
            raise MarketDataError("connect and log in to your broker on the Trade tab", "Broker", needs_login=True)
        return kite.ltp(instruments)

    def _crypto_prices(symbols: list[str]) -> dict:
        return crypto_tickers(app.state.crypto, symbols)

    from .pricealerts import CONDITIONS as ALERT_CONDITIONS, MARKETS as ALERT_MARKETS, PriceAlerts
    app.state.price_alerts = PriceAlerts(home() / "price_alerts.json", desk.notifier.notify, _india_prices, _crypto_prices)

    @app.get("/api/price-alerts")
    def price_alerts_list():
        return {**app.state.price_alerts.list(), "markets": ALERT_MARKETS, "conditions": ALERT_CONDITIONS,
                "telegram": bool(settings.get("alerts_enabled"))}

    @app.post("/api/price-alerts")
    def price_alerts_add(payload: dict):
        from .marketdata.crypto import pair as crypto_pair
        payload = body_of(payload)
        kind, current = str(payload.get("market", "")), None
        raw_symbol = str(payload.get("symbol") or "").strip()
        if kind == "crypto":
            instrument = crypto_pair(raw_symbol)[0]
            label = instrument
            with contextlib.suppress(Exception):
                got = app.state.price_alerts.crypto_prices([instrument]).get(instrument) or {}
                current = float(got["price"]) if got.get("price") else None
        elif kind == "india":
            instrument, raw, label = chart_instrument(raw_symbol)
            if not raw:
                found = kite_instrument(instrument)
                if not found:
                    raise ValueError(f"No broker instrument for {instrument}.")
                instrument = f"{found[0]}:{found[1]}"
                label = found[1] if found[0] == "NSE" else instrument
            if kite.connected:
                with contextlib.suppress(Exception):
                    value = kite.ltp([instrument]).get(instrument)
                    current = float(value) if value else None
        else:
            raise ValueError("Choose Indian market or Crypto")
        alert = app.state.price_alerts.add({**payload, "label": label}, instrument=instrument, current=current)
        return {"alert": alert, **app.state.price_alerts.list()}

    @app.delete("/api/price-alerts/{alert_id}")
    def price_alerts_remove(alert_id: str):
        if not re.fullmatch(r"[0-9a-f]{10}", alert_id) or not app.state.price_alerts.remove(alert_id):
            raise HTTPException(404, "no such alert")
        return app.state.price_alerts.list()

    @app.post("/api/price-alerts/clear")
    def price_alerts_clear():
        app.state.price_alerts.clear_done()
        return app.state.price_alerts.list()

    # -- Charts tab: watchlist, drawings, imported files, crypto paper trading ---------------------------
    @app.get("/api/md/watchlist")
    def md_watchlist():
        return {"items": app.state.chartstore.watchlist()}

    @app.put("/api/md/watchlist")
    def md_watchlist_set(payload: dict):
        return {"items": app.state.chartstore.set_watchlist(body_of(payload).get("items"))}

    @app.get("/api/md/prices")
    def md_prices():
        """The watchlist's last prices: crypto from the exchange, NSE/BSE from the broker, imports' last close."""
        items = app.state.chartstore.watchlist()
        out: dict[str, dict] = {}
        crypto = [i["symbol"] for i in items if i["venue"] == "crypto"]
        if crypto:
            try:
                for symbol, value in crypto_tickers(app.state.crypto, crypto).items():
                    out[f"crypto:{symbol}"] = value
            except ValueError as exc:
                for symbol in crypto:
                    out[f"crypto:{symbol}"] = {"error": str(exc)}
        india = [i["symbol"] for i in items if i["venue"] == "india"]
        if india:
            broker = chart_broker()
            names: dict[str, str] = {}
            for symbol in india:
                try:
                    instrument, raw, _ = chart_instrument(symbol)
                    if not raw:
                        pair = kite_instrument(instrument)
                        if not pair:
                            raise ValueError(f"{broker} has no instrument for {instrument}.")
                        instrument = f"{pair[0]}:{pair[1]}"
                    names[symbol] = instrument
                except (ValueError, MarketDataError) as exc:
                    out[f"india:{symbol}"] = {"error": str(exc)}
            if names and not kite.connected:
                for symbol in names:
                    out[f"india:{symbol}"] = {"error": f"Log in to {broker} for live prices."}
            elif names:
                try:
                    quotes = kite.quotes(sorted(set(names.values())))
                except Exception as exc:  # noqa: BLE001 - shown against each symbol
                    quotes, failure = {}, str(exc)[:160]
                else:
                    failure = f"{broker} gave no price."
                for symbol, instrument in names.items():
                    q = quotes.get(instrument) or {}
                    last = q.get("last_price")
                    close = (q.get("ohlc") or {}).get("close")
                    out[f"india:{symbol}"] = ({"price": float(last), "source": broker,
                                               "change_pct": (last / close - 1) * 100 if close else None}
                                              if last else {"error": failure})
        for item in items:
            if item["venue"] == "imported":
                try:
                    rows = app.state.imports.candles(item["symbol"], "1d")["candles"]
                    prev = rows[-2]["close"] if len(rows) > 1 else None
                    out[f"imported:{item['symbol']}"] = {"price": rows[-1]["close"], "source": "Imported file",
                                                         "change_pct": (rows[-1]["close"] / prev - 1) * 100 if prev else None,
                                                         "stale": True}
                except (ValueError, MarketDataError) as exc:
                    out[f"imported:{item['symbol']}"] = {"error": str(exc)}
        return {"prices": out, "session": market.session_status()}

    @app.get("/api/md/layout")
    def md_layout(view: str = "live"):
        return {"layout": app.state.chartstore.layout(view)}

    @app.put("/api/md/layout")
    def md_layout_set(payload: dict):
        body = body_of(payload)
        return {"layout": app.state.chartstore.set_layout(str(body.get("view", "")), body.get("layout"))}

    @app.get("/api/md/drawings")
    def md_drawings(venue: str = "", symbol: str = ""):
        return {"items": app.state.chartstore.drawings(venue, symbol)}

    @app.put("/api/md/drawings")
    def md_drawings_set(payload: dict):
        body = body_of(payload)
        return {"items": app.state.chartstore.set_drawings(str(body.get("venue", "")), body.get("symbol"), body.get("items"))}

    @app.get("/api/md/imports")
    def md_imports():
        return {"items": app.state.imports.list()}

    @app.post("/api/md/imports")
    def md_import(payload: dict):
        body = body_of(payload)
        return app.state.imports.save(str(body.get("symbol", "")), str(body.get("text", "")))

    @app.delete("/api/md/imports/{symbol}")
    def md_import_delete(symbol: str):
        app.state.imports.delete(symbol)
        return {"items": app.state.imports.list()}

    # -- replay charts: past candles one at a time, practice trades kept apart from paper and live -------------
    def replay_charges(session: dict, trade: dict) -> float:
        """Round-trip charges of a practice trade as the paper desk would book them."""
        from . import sizing
        from .cryptopaper import FEE as CRYPTO_FEE
        qty, entry, exit_price = trade["qty"], trade["entry"], trade["exit"]
        if session["venue"] == "crypto":
            return (entry + exit_price) * qty * CRYPTO_FEE
        side = "BUY" if trade["side"] == "long" else "SELL"
        if session["venue"] == "imported":
            exchange, tradingsymbol = "NSE", session["symbol"]
        else:
            if session["symbol"].startswith("IDX:"):
                return 0.0                      # an index is not traded; practice on it has no charges
            instrument, raw, _ = chart_instrument(session["symbol"])
            if raw:
                exchange, tradingsymbol = instrument.split(":", 1)
            else:
                pair = kite_instrument(instrument)
                if not pair:
                    return 0.0
                exchange, tradingsymbol = pair
        derivative = exchange in ("NFO", "BFO", "MCX")
        product = "NRML" if derivative else ("MIS" if trade["product"] == "intraday" else "CNC")
        return sizing.round_trip_charges(side, max(1, int(round(qty))), entry, exit_price, product, exchange,
                                         tradingsymbol)

    app.state.replays = replay_module.ReplayStore(home() / "replay_sessions.json", replay_charges)

    def replay_rows(venue: str, symbol: str, tf: str, cut: int, warm: int, ahead: int) -> tuple[dict, list[dict]]:
        """(chart info, candles) around ``cut`` for a replay, from the same source as the live chart."""
        seconds = CHART_TIMEFRAMES[tf]["seconds"]
        if venue == "crypto":
            from .marketdata.crypto import pair as crypto_pair
            pair_symbol, coin, name, _ = crypto_pair(symbol)
            first = cut - market_ist_offset() - warm * seconds
            rows = app.state.crypto.binance.history(pair_symbol, tf, first, warm + ahead)
            info = {"symbol": pair_symbol, "name": f"{name} ({coin}/USDT)", "source": "Binance",
                    "session_offset": market_ist_offset(),
                    "anchor": (4 * 86400 + market_ist_offset()) if tf == "1w" else market_ist_offset()}
            return info, rows
        if venue == "imported":
            out = app.state.imports.history(symbol, tf)
            return {k: v for k, v in out.items() if k != "candles"}, out["candles"]
        instrument, raw, name = chart_instrument(symbol)
        broker = chart_broker()
        if not kite.connected:
            raise MarketDataError("Replay of NSE/BSE charts uses your broker's history. Open Trade, press Connect "
                                  f"and log in to {broker}.", broker, needs_login=True)
        interval, merge = md_broker.BASE[tf]
        at = pd.Timestamp(cut, unit="s")
        today = pd.Timestamp(market.now_ist().replace(tzinfo=None)).normalize()
        first = (at - pd.Timedelta(days=replay_module.WARM_DAYS[tf])) if warm else at
        last = min(at + pd.Timedelta(days=replay_module.AHEAD_DAYS[tf]), today)
        continuous = interval == "1d" and (raw and instrument.split(":", 1)[0] in ("NFO", "BFO", "MCX")
                                           and instrument.endswith("FUT"))
        try:
            frame = data.kite_range(kite, instrument, interval, first.strftime("%Y-%m-%d"), last.strftime("%Y-%m-%d"),
                                    raw=raw, continuous=continuous)
        except data.NoData as exc:
            raise MarketDataError(f"{broker} has no candles for this replay: {exc}", broker) from exc
        except KiteError as exc:
            raise MarketDataError(f"{broker} could not serve the replay. {str(exc)[:200]}", broker,
                                  needs_login=exc.needs_login) from exc
        if merge == "week":
            frame = md_weekly(frame)
        elif merge:
            frame = data.resample(frame, merge)
        info = {"symbol": symbol.strip().upper(), "name": name, "source": broker, "session_offset": 0,
                "anchor": md_broker.ANCHOR.get(tf, md_broker.SESSION_OPEN)}
        notes = []
        if frame.attrs.get("adjusted"):
            notes.append("Prices before " + ", ".join(d for d, _ in frame.attrs["adjusted"])
                         + " were adjusted for a split or bonus.")
        info["notes"] = notes
        return info, md_rows(frame, limit=None)

    def market_ist_offset() -> int:
        from .marketdata import IST_OFFSET
        return IST_OFFSET

    @app.get("/api/md/replay")
    def md_replay(venue: str = "india", symbol: str = "", tf: str = "5m", start: str = "", after: int | None = None):
        """Candles for a replay: a warm-up before ``start`` and the candles after it (``start_index`` is how many
        are shown at first). ``start=random`` picks a random date (blind mode). ``after`` (a bar time) loads the
        next candles once a replay reaches the end of what it has."""
        if not settings.get("charts_enabled"):
            raise ValueError("Charts are switched off in Settings.")
        if venue not in ("india", "crypto", "imported"):
            raise ValueError("venue must be india, crypto or imported")
        if tf not in CHART_TIMEFRAMES:
            raise ValueError("unknown candle size")
        if not symbol.strip():
            raise ValueError("Enter a symbol.")
        today = pd.Timestamp(market.now_ist().replace(tzinfo=None))
        seconds = CHART_TIMEFRAMES[tf]["seconds"]
        if after is not None:
            info, rows = replay_rows(venue, symbol, tf, int(after) + 1, 0, replay_module.AHEAD_BARS)
            rows = [r for r in rows if r["time"] > int(after)][:replay_module.AHEAD_BARS]
            return {**info, "candles": rows, "start_index": 0, "seconds": seconds, "tf": tf, "market": venue,
                    "live": {"kind": "none"}, "more": bool(rows) and rows[-1]["time"] + seconds < epoch_now(today)}
        rng = random.Random()
        tries = 4 if start == "random" else 1
        span = None
        if start == "random" and venue == "imported":
            whole = app.state.imports.history(symbol, tf)["candles"]
            if len(whole) < 120:
                raise ValueError("This imported file is too short for a random replay.")
            span = (pd.Timestamp(whole[60]["time"], unit="s"), pd.Timestamp(whole[-60]["time"], unit="s"))
        problem = ""
        for _ in range(tries):
            if start == "random":
                stamp = replay_module.random_start(venue, tf, today, rng, *(span or (None, None)))
            else:
                stamp = replay_module.parse_start(start, tf, venue)
                replay_module.check_start(stamp, venue, tf, today)
            cut = replay_module.epoch(stamp)
            info, rows = replay_rows(venue, symbol, tf, cut, replay_module.WARM_BARS, replay_module.AHEAD_BARS)
            candles, shown = replay_module.window(rows, cut, seconds)
            if shown >= 20 and len(candles) - shown >= 20:
                notes = list(info.pop("notes", []) or [])
                if shown < 100:
                    notes.append(f"Only {shown} candles before the start: long indicators need a while to settle.")
                return {**info, "candles": candles, "start_index": shown, "start": cut, "seconds": seconds,
                        "tf": tf, "market": venue, "notes": notes, "live": {"kind": "none"},
                        "more": candles[-1]["time"] + seconds < epoch_now(today)}
            problem = (f"{shown} candles before {stamp:%d %b %Y %H:%M} and {len(candles) - shown} after it"
                       if candles else f"no candles around {stamp:%d %b %Y}")
        raise ValueError(f"Not enough history to replay from there ({problem}). "
                         + ("Try again for another date, or another stock." if start == "random"
                            else "Pick another date; it may be before this stock listed, or a holiday stretch."))

    def epoch_now(today: pd.Timestamp) -> int:
        return replay_module.epoch(today)

    @app.get("/api/replay/sessions")
    def replay_sessions():
        return app.state.replays.list()

    @app.post("/api/replay/sessions")
    def replay_session_save(payload: dict):
        return {"session": app.state.replays.save(body_of(payload)), **app.state.replays.list()}

    @app.get("/api/replay/sessions/{sid}")
    def replay_session_get(sid: str):
        try:
            found = app.state.replays.get(sid)
        except KeyError:
            raise HTTPException(404, "no such practice session") from None
        if found.get("blind") and not found.get("ended"):
            found = {**found, "symbol": "Hidden", "name": "Hidden until you end it"}
        return {"session": found}

    @app.delete("/api/replay/sessions/{sid}")
    def replay_session_delete(sid: str):
        if not app.state.replays.delete(sid):
            raise HTTPException(404, "no such practice session")
        return app.state.replays.list()

    @app.get("/api/cpaper")
    def cpaper_view():
        return app.state.cpaper.view()

    @app.post("/api/cpaper/order")
    def cpaper_order(payload: dict):
        result = app.state.cpaper.place(body_of(payload))
        return {**result, "book": app.state.cpaper.view()}

    @app.delete("/api/cpaper/order/{order_id}")
    def cpaper_cancel(order_id: str):
        app.state.cpaper.cancel(order_id)
        return app.state.cpaper.view()

    @app.post("/api/cpaper/reset")
    def cpaper_reset(payload: dict):
        return app.state.cpaper.reset(body_of(payload).get("balance", 10_000))

    @app.post("/api/cpaper/protect")
    def cpaper_protect(payload: dict):
        bracket = app.state.cpaper.protect(body_of(payload))
        return {"bracket": bracket, "book": app.state.cpaper.view()}

    @app.put("/api/cpaper/bracket/{bracket_id}")
    def cpaper_bracket_change(bracket_id: str, payload: dict):
        bracket = app.state.cpaper.change_bracket(bracket_id, body_of(payload))
        return {"bracket": bracket, "book": app.state.cpaper.view()}

    @app.delete("/api/cpaper/bracket/{bracket_id}")
    def cpaper_bracket_cancel(bracket_id: str):
        app.state.cpaper.cancel_bracket(bracket_id)
        return app.state.cpaper.view()

    # -- orders from the chart (NSE/BSE): a buy with its stoploss and target, moved by dragging its lines ------
    app.state.chart_orders = chart_orders

    def chart_order_instrument(symbol: str) -> tuple[str, str]:
        symbol = (symbol or "").strip().upper()
        if not symbol:
            raise ValueError("Enter a symbol.")
        if symbol.startswith("IDX:"):
            raise ValueError("An index cannot be bought or sold. Open a share to trade it from the chart.")
        if ":" in symbol:
            exchange, name = symbol.split(":", 1)
            if exchange not in ("NSE", "BSE"):
                raise ValueError("Chart orders are for NSE and BSE shares.")
            return exchange, name
        pair = kite_instrument(symbol_of(symbol))
        if not pair:
            raise ValueError(f"{symbol} is not an NSE/BSE share that can be ordered.")
        return pair[0], pair[1]

    @app.get("/api/chart-orders")
    def chart_orders_list(symbol: str = ""):
        if not symbol:
            return chart_orders.view()
        exchange, name = chart_order_instrument(symbol)
        return {**chart_orders.view(exchange, name), "exchange": exchange, "tradingsymbol": name}

    @app.post("/api/chart-orders/preview")
    def chart_order_preview(payload: dict):
        payload = body_of(payload)
        exchange, name = chart_order_instrument(str(payload.get("symbol", "")))
        order = {"exchange": exchange, "tradingsymbol": name, "side": payload.get("side"),
                 "quantity": payload.get("quantity"), "product": payload.get("product", "MIS"),
                 "order_type": payload.get("order_type", "LIMIT"), "price": payload.get("price") or 0,
                 "short": payload.get("short") is True}
        return chart_orders.preview({"mode": str(payload.get("mode", "paper")), "order": order,
                                     "stop": payload.get("stop"), "target": payload.get("target")})

    @app.post("/api/chart-orders/place")
    def chart_order_place(payload: dict):
        payload = body_of(payload)
        return chart_orders.place(str(payload.get("token", "")), str(payload.get("phrase", "")))

    @app.post("/api/chart-orders/{item_id}/change/preview")
    def chart_order_change_preview(item_id: str, payload: dict):
        return chart_orders.change_preview(item_id, body_of(payload))

    @app.post("/api/chart-orders/{item_id}/change")
    def chart_order_change(item_id: str, payload: dict):
        return chart_orders.change(str(body_of(payload).get("token", "")))

    @app.delete("/api/chart-orders/{item_id}")
    def chart_order_remove(item_id: str):
        return chart_orders.remove(item_id)

    @app.get("/api/algos")
    def algo_list():
        return {"algos": algos.list(), "run_after_ist": "15:50"}

    @app.post("/api/algos")
    def algo_create(payload: dict):
        return algos.create(body_of(payload))

    @app.put("/api/algos/{algo_id}")
    def algo_update(algo_id: str, payload: dict):
        return algos.update(algo_id, body_of(payload))

    def algo_detail(algo: dict, source: str, orders: list[dict]) -> dict:
        """What one algo bought and sold, at what price, its booked and open P&L, and its orders still out."""
        if algo["mode"] == "live":
            with contextlib.suppress(Exception):
                desk.sync_live_fills()
        legs = desk.ledger.legs(source, lambda instrument: desk.last_price(instrument)[0])
        held = [g for g in legs if g["status"] == "Active"]
        priced = [g["unrealized"] for g in held if g["unrealized"] is not None]
        unrealized = round(sum(priced), 2) if len(priced) == len(held) else None     # a held leg without a price
        realized = round(sum(g["realized"] for g in legs), 2)
        return {"algo": algo, "legs": legs, "orders": orders, "can_edit": not (algo["enabled"] and algo["mode"] == "live"),
                "daily": peaks.source(algo["mode"], source),
                "totals": {"realized": realized, "unrealized": unrealized,
                           "total": None if unrealized is None else round(realized + unrealized, 2),
                           "charges": round(sum(g["charges"] for g in legs), 2), "held": len(held),
                           "closed": len(legs) - len(held)}}

    @app.get("/api/algos/{algo_id}/detail")
    def algo_detail_view(algo_id: str):
        algo = next((a for a in algos.list() if a["id"] == algo_id), None)
        if algo is None:
            raise AlgoError("no such algo")
        orders = []
        if algo["pending"]:
            p = algo["pending"]
            orders.append({"symbol": algo["symbol"], "side": p["side"], "quantity": p["quantity"], "price": p["price"],
                           "order_id": p["order_id"], "status": "Sent, waiting to fill", "time_ist": p["placed"]})
        if algo["queued"]:
            q = algo["queued"]
            orders.append({"symbol": algo["symbol"], "side": q["side"], "quantity": None, "price": None, "order_id": None,
                           "status": "Waiting for the trade window", "time_ist": None})
        if algo["protect"]:
            orders.append({"symbol": algo["symbol"], "side": "SELL", "quantity": algo["position"], "price": algo.get("stop_level"),
                           "order_id": algo["protect"], "status": "Target / stoploss order (GTT) active", "time_ist": None})
        return algo_detail(algo, f"algo:{algo['name']}", orders)

    @app.post("/api/algos/{algo_id}/start")
    def algo_start(algo_id: str, payload: dict):
        return algos.set_enabled(algo_id, True, str(body_of(payload).get("phrase", "")))

    @app.post("/api/algos/{algo_id}/stop")
    def algo_stop(algo_id: str):
        return algos.set_enabled(algo_id, False)

    @app.post("/api/algos/{algo_id}/run")
    def algo_run(algo_id: str):
        return algos.evaluate(algo_id, force=True)

    @app.post("/api/algos/{algo_id}/clear")
    def algo_clear(algo_id: str):
        return algos.forget_position(algo_id)

    @app.delete("/api/algos/{algo_id}")
    def algo_delete(algo_id: str):
        algos.delete(algo_id)
        return {"ok": True}

    @app.post("/api/algos/{algo_id}/exit")
    def algo_exit(algo_id: str):
        return algos.request_exit(algo_id)

    @app.post("/api/algo-exits")
    def algo_exit_many(payload: dict):
        """Exit several algos at once: the ids given (``stock`` and ``fno`` lists), or every algo of ``mode``
        (paper, live or all) that holds something. Each is closed as its own Exit button would; one that cannot be
        closed now does not stop the others."""
        body = body_of(payload)
        mode = str(body.get("mode") or "")
        if mode and mode not in ("paper", "live", "all"):
            raise ValueError("mode must be paper, live or all")
        if mode:
            stock = [a["id"] for a in algos.list() if (mode == "all" or a["mode"] == mode)
                     and (a["position"] or a["pending"]) and not a.get("exit_requested")]
            fno = [a["id"] for a in fno_algos.list() if (mode == "all" or a["mode"] == mode)
                   and a["book"] and not a.get("exit_requested")]
        else:
            stock = [str(i) for i in body.get("stock") or []]
            fno = [str(i) for i in body.get("fno") or []]
        if len(stock) + len(fno) > 100:
            raise ValueError("too many algos in one request")
        out = []
        for kind, ids, engine in (("fno", fno, fno_algos), ("stock", stock, algos)):
            for algo_id in ids:
                try:
                    algo = engine.request_exit(algo_id, "Exit pressed (several at once)")
                    out.append({"kind": kind, "id": algo_id, "name": algo["name"], "ok": True, "status": algo["status"]})
                except (AlgoError, ValueError, KiteError, OrderRejected) as exc:
                    out.append({"kind": kind, "id": algo_id, "ok": False, "error": str(exc)})
        return {"results": out}

    @app.post("/api/algos/{algo_id}/release")
    def algo_release(algo_id: str):
        return algos.release_hold(algo_id)

    @app.post("/api/fno-algos/{algo_id}/release")
    def fno_algo_release(algo_id: str):
        return fno_algos.release_hold(algo_id)

    # -- supervisor: watches the algos, may hold entries or close positions, never opens one ----

    @app.get("/api/supervisor")
    def supervisor_status():
        return supervisor.status()

    @app.post("/api/supervisor/review")
    def supervisor_review():
        return supervisor.review(background=True)

    @app.post("/api/supervisor/check")
    def supervisor_check():
        return {"actions": supervisor.check_rules()}

    @app.get("/api/algos/{algo_id}/phrase")
    def algo_phrase(algo_id: str):
        return {"phrase": arm_phrase(algos.get(algo_id)["name"])}

    # -- F&O algos: option strategies and index futures, paper and live ---------------

    @app.get("/api/fno-algos")
    def fno_algo_list():
        rows = fno_algos.list()
        for row in rows:
            if row["book"] and row["book"].get("pnl") is None:
                with contextlib.suppress(Exception):
                    row["book"]["pnl"] = fno_algos.mtm(fno_algos.get(row["id"]))
        return {"algos": rows, "kite_connected": kite.connected, "can_trade": kite.can_trade,
                "naked_live": settings.get("fno_naked_live")}

    @app.post("/api/fno-algos")
    def fno_algo_create(payload: dict):
        return fno_algos.create(body_of(payload))

    @app.get("/api/fno-algos/expiries")
    def fno_algo_expiries(underlying: str = "NIFTY", expiry_kind: str = "weekly", min_days: float = 0):
        return {"expiries": fno_algos.listed_expiries(underlying, expiry_kind, min_days)}

    @app.post("/api/fno-algos/preview")
    def fno_algo_preview(payload: dict):
        return fno_algos.preview(body_of(payload))

    @app.put("/api/fno-algos/{algo_id}")
    def fno_algo_update(algo_id: str, payload: dict):
        return fno_algos.update(algo_id, body_of(payload))

    @app.get("/api/fno-algos/{algo_id}/detail")
    def fno_algo_detail_view(algo_id: str):
        algo = next((a for a in fno_algos.list() if a["id"] == algo_id), None)
        if algo is None:
            raise AlgoError("no such F&O algo")
        orders = []
        if algo["book"]:
            for g in algo["book"]["legs"]:
                for p in g["pending"]:
                    orders.append({"symbol": g["tradingsymbol"], "side": p["side"], "quantity": p["quantity"],
                                   "price": p["price"], "order_id": p["order_id"], "status": "Sent, waiting to fill",
                                   "time_ist": p["placed"]})
        return algo_detail(algo, f"fno:{algo['name']}", orders)

    @app.post("/api/fno-algos/{algo_id}/start")
    def fno_algo_start(algo_id: str, payload: dict):
        return fno_algos.set_enabled(algo_id, True, str(body_of(payload).get("phrase", "")))

    @app.post("/api/fno-algos/{algo_id}/stop")
    def fno_algo_stop(algo_id: str, payload: dict):
        return fno_algos.set_enabled(algo_id, False, force=bool(body_of(payload).get("force")))

    @app.post("/api/fno-algos/{algo_id}/exit")
    def fno_algo_exit(algo_id: str):
        return fno_algos.request_exit(algo_id)

    @app.post("/api/fno-algos/{algo_id}/run")
    def fno_algo_run(algo_id: str):
        algo = fno_algos.get(algo_id)
        if not algo["enabled"]:
            raise AlgoError("Start the algo first.")
        return fno_algos.evaluate(algo_id, force=True)

    @app.post("/api/fno-algos/{algo_id}/clear")
    def fno_algo_clear(algo_id: str):
        return fno_algos.forget_book(algo_id)

    @app.delete("/api/fno-algos/{algo_id}")
    def fno_algo_delete(algo_id: str):
        fno_algos.delete(algo_id)
        return {"ok": True}

    @app.get("/api/fno-algos/{algo_id}/margin")
    def fno_algo_margin(algo_id: str):
        return fno_algos.margin_check(algo_id)

    @app.get("/api/fno-algos/{algo_id}/phrase")
    def fno_algo_phrase(algo_id: str):
        return {"phrase": fno_arm_phrase(fno_algos.get(algo_id)["name"])}

    # -- analysis -------------------------------------------------------------

    @app.post("/api/analyze")
    def analyze(payload: dict):
        payload = body_of(payload)
        if problem := llm_ready():
            raise ValueError(problem)
        canonical = symbol_of(payload.get("symbol"))
        date = date_of(payload.get("date") or get_current_date(), "Date")
        if date > get_current_date():
            raise ValueError("The analysis date cannot be in the future")
        portfolio = _portfolio(desk, kite) if payload.get("use_portfolio") else None
        config, analysts = settings.agent_config(), settings.get("analysts")
        job = runner.submit("analysis", f"{canonical} · {date}",
                            lambda job: jobs_module.analyze(job, config, analysts, canonical, date, portfolio))
        return {"job": job.view()}

    @app.get("/api/jobs")
    def list_jobs():
        return {"jobs": scan_runner.list() + runner.list()}

    def find_job(job_id: str):
        job = runner.get(job_id) or scan_runner.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return job

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        return find_job(job_id).view()

    @app.post("/api/jobs/{job_id}/stop")
    def stop_job(job_id: str):
        job = find_job(job_id)
        if job.status in ("queued", "running"):
            job.stop_requested = True
            job.log("Stopping…")
        return job.view(full=False)

    # -- AI strategy scan --------------------------------------------------------

    def scan_indices() -> list[dict]:
        out = []
        for entry in indices.entries():
            if not entry[3] or entry[3] == "bse":
                continue
            out.append({"id": entry[0], "name": entry[1], "group": entry[2], "stocks": len(indices.constituents(entry[0]))})
        return out

    @app.get("/api/scan/options")
    def scan_options():
        from . import aiscan
        return {"indices": scan_indices(), "depths": {k: v[0] for k, v in aiscan.DEPTHS.items()},
                "strategies": len(aiscan.strategy_keys()) - 1,
                "settings": {k: len(aiscan.candidates(k)) for k in aiscan.DEPTHS}, "max_stocks": aiscan.MAX_STOCKS,
                "workers": _scan_workers()}

    @app.post("/api/scan")
    def scan_start(payload: dict):
        import time as time_module

        from . import aiscan, xlsxexport
        payload = body_of(payload)
        index_id = str(payload.get("index", ""))
        entry = indices.entry(index_id)
        if entry is None or not entry[3] or entry[3] == "bse":
            raise ValueError("Choose an NSE index with a published stock list")
        rows = indices.constituents(index_id)
        if not rows:
            raise ValueError(f"The stock list of {entry[1]} has not loaded yet. Open Indices, wait a moment and try again.")
        start, end = date_of(payload.get("start"), "Start date"), date_of(payload.get("end"), "End date")
        if end <= start:
            raise ValueError("End date must be after the start date")
        depth = str(payload.get("depth", "standard"))
        if depth not in aiscan.DEPTHS:
            raise ValueError(f"depth must be one of {', '.join(aiscan.DEPTHS)}")
        oos_pct = _number(payload, "oos_pct", 30.0)
        cfg = settings.all()
        params = BacktestParams(capital=_number(payload, "capital", max(cfg["paper_capital"], 20_000.0 * len(rows))),
                                slippage_bps=_number(payload, "slippage_bps", cfg["slippage_bps"]),
                                risk_free_rate_pct=cfg["risk_free_rate_pct"])
        params.validate()
        aiscan._cut_date(start, end, oos_pct)          # refuse a short period now, not inside the job
        if params.capital / len(rows) < aiscan.MIN_STOCK_CAPITAL:
            raise ValueError(f"Use at least ₹{aiscan.MIN_STOCK_CAPITAL * len(rows):,.0f} for {len(rows)} stocks "
                             f"(₹{aiscan.MIN_STOCK_CAPITAL:,.0f} a stock).")
        cache = aiscan.PriceCache(home() / "scan_cache")

        def load(symbol: str):
            hit = cache.get(symbol, start, end)
            if hit:
                return hit[0], f"{hit[1]} (saved copy)"
            bars, source = stock_bars(symbol, "1d", start, end)
            cache.put(symbol, start, end, bars, source["name"])
            if source["name"] != "Yahoo":
                time_module.sleep(0.35)               # brokers cap history requests (Kite: three a second)
            return bars, source["name"]

        def work(job):
            job.progress = {"done": 0, "total": len(rows)}

            def progress(done, total):
                job.progress = {"done": done, "total": total}

            result = aiscan.scan(rows, load, params, start=start, end=end, depth=depth, oos_pct=oos_pct,
                                 index_name=entry[1], log=job.log, progress=progress,
                                 stopped=lambda: job.stop_requested, workers=_scan_workers())
            job.log("Writing the Excel workbook")
            folder = home() / "scans"
            folder.mkdir(parents=True, exist_ok=True)
            (folder / f"{job.id}.xlsx").write_bytes(xlsxexport.scan_workbook(result, APP_NAME))
            for old in sorted(folder.glob("*.xlsx"), key=os.path.getmtime)[:-20]:
                old.unlink(missing_ok=True)            # keep the last 20 workbooks
            result.pop("runs")                         # every run is in the workbook; the page shows the summary
            result["workbook"] = f"/api/scan/{job.id}.xlsx"
            job.log("Done")
            return result

        job = scan_runner.submit("scan", f"AI scan: {entry[1]}, {len(rows)} stocks", work)
        return {"job": job.view()}

    @app.get("/api/scan/{job_id}.xlsx")
    def scan_workbook(job_id: str):
        if not re.fullmatch(r"[0-9a-f]{12}", job_id):
            raise HTTPException(404, "no such scan")
        path = home() / "scans" / f"{job_id}.xlsx"
        if not path.exists():
            raise HTTPException(404, "That scan's workbook is no longer kept. Run the scan again.")
        return Response(path.read_bytes(), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                        headers={"Content-Disposition": f'attachment; filename="ai-strategy-scan-{job_id}.xlsx"'})

    # -- broker ---------------------------------------------------------------

    @app.get("/api/brokers")
    def brokers_list():
        return {"brokers": broker_catalog(), "active": settings.get("broker")}

    @app.post("/api/kite/connect")
    def kite_connect(request: Request):
        name = settings.get("broker")
        if isinstance(kite, Switch):
            kite.use(name)
            if name != "kite":
                entry = dict(settings.get("broker_keys").get(name) or {})
                if not entry.get("redirect_uri"):
                    entry["redirect_uri"] = str(request.base_url).rstrip("/") + "/broker/callback"
                return kite.connect(entry)
        return kite.connect(kite_entry(settings, str(request.base_url).rstrip("/") + "/broker/callback"))

    @app.post("/api/kite/session")
    def kite_session(payload: dict):
        """Finish a Kite login by hand: the address Kite redirected to, pasted (when that page did not load)."""
        if not getattr(kite, "is_direct", False):
            raise KiteError("Save your Kite Connect API key and secret in Settings → Broker, then Connect.")
        return kite.direct.exchange(request_token_from(str(body_of(payload).get("url") or "")))

    @app.post("/api/kite/logout")
    def kite_logout():
        if getattr(kite, "is_direct", False):
            kite.direct.logout()
        return kite.status()

    @app.get("/broker/callback")
    def broker_callback(request: Request):
        """Where Upstox and Fyers send the browser back after their sign-in page."""
        import html

        params = dict(request.query_params)
        try:
            if not isinstance(kite, Switch):
                raise KiteError("This desk is not set up for broker sign-in redirects.")
            kite.complete_login(params)
            text, ok = f"Signed in to {kite.status().get('broker_label')}. Return to TradeSetu and press Refresh.", True
        except KiteError as exc:
            text, ok = str(exc), False
        body = (f"<!doctype html><meta charset=utf-8><title>TradeSetu sign-in</title>"
                f"<body style='font-family:system-ui;max-width:36rem;margin:3rem auto;padding:0 1rem'>"
                f"<h1>{'Signed in' if ok else 'Sign-in failed'}</h1><p>{html.escape(text)}</p><p><a href='/'>Back to TradeSetu</a></p>")
        return HTMLResponse(body, status_code=200 if ok else 400)

    @app.post("/api/kite/disconnect")
    def kite_disconnect():
        kite.disconnect()
        return kite.status()

    @app.post("/api/kite/login")
    def kite_login():
        return kite.login()

    @app.get("/api/kite/account")
    def kite_account():
        out = {"status": kite.status(), "market_data": _market_data(kite)}
        for key, call in (("profile", kite.profile), ("margins", kite.margins), ("holdings", kite.holdings),
                          ("positions", kite.positions), ("orders", kite.orders)):
            try:
                out[key] = call()
            except KiteError as exc:
                if exc.needs_login:
                    raise
                out[key], out.setdefault("errors", {})[key] = None, str(exc)
        if out.get("profile"):
            indices.kick()                       # signed in: index levels from Kite now, not at the next tick
        return out

    @app.get("/api/paper")
    def paper():
        return desk.paper.snapshot()

    @app.post("/api/paper/reset")
    def paper_reset():
        desk.paper.reset(settings.get("paper_capital"))
        return desk.paper.snapshot()

    # -- orders ---------------------------------------------------------------

    @app.post("/api/order/plan")
    def order_plan(payload: dict):
        payload = body_of(payload)
        mode = payload.get("mode", "paper")
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        return desk.plan(symbol_of(payload.get("symbol")), str(payload.get("rating", "")), mode)

    @app.post("/api/order/preview")
    def order_preview(payload: dict):
        payload = body_of(payload)
        return desk.preview(body_of(payload.get("order")), str(payload.get("mode", "paper")), stop=payload.get("stop"),
                            manual=True)

    @app.post("/api/order/size")
    def order_size(payload: dict):
        from . import sizing
        payload = body_of(payload)
        cfg = settings.all()
        if payload.get("risk_pct") in (None, ""):
            payload = {**payload, "risk_pct": cfg["risk_per_trade_pct"]}
        capital = payload.get("capital")
        if capital in (None, "", 0):
            capital = cfg["risk_capital"] or cfg["paper_capital"]
        if isinstance(capital, bool) or not isinstance(capital, (int, float)):
            raise ValueError("Capital must be a number")
        return sizing.size(payload, float(capital), cfg["max_order_value"])

    @app.post("/api/order/place")
    def order_place(payload: dict):
        payload = body_of(payload)
        return desk.place(str(payload.get("token", "")), str(payload.get("phrase", "")))

    # -- GTT -----------------------------------------------------------------

    @app.post("/api/gtt/preview")
    def gtt_preview(payload: dict):
        payload = body_of(payload)
        return desk.gtt_preview(body_of(payload.get("gtt")), str(payload.get("mode", "paper")))

    @app.get("/api/gtt")
    def gtt_list(mode: str = "paper"):
        if mode == "paper":
            return {"mode": mode, "gtts": list(reversed(desk.paper.gtts()))}
        rows = []
        for row in kite.gtts():
            condition, order = row.get("condition") or {}, ((row.get("orders") or [{}])[0])
            rows.append({"id": str(row.get("id")), "status": str(row.get("status", "")).lower(),
                         "kind": "oco" if row.get("type") == "two-leg" else "single",
                         "exchange": condition.get("exchange"), "tradingsymbol": condition.get("tradingsymbol"),
                         "side": order.get("transaction_type"), "quantity": order.get("quantity"),
                         "triggers": condition.get("trigger_values") or [], "created": row.get("created_at")})
        return {"mode": mode, "gtts": rows}

    @app.post("/api/gtt/cancel")
    def gtt_cancel(payload: dict):
        payload = body_of(payload)
        mode = str(payload.get("mode", "paper"))
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        desk.cancel_gtt(mode, str(payload.get("id", "")))
        return {"ok": True}

    # -- scanner -------------------------------------------------------------

    @app.get("/api/scanner")
    def scanner_view():
        return scanner.view()

    @app.put("/api/scanner")
    def scanner_configure(payload: dict):
        payload = body_of(payload)
        if payload.get("reset_universe"):
            return scanner.reset_universe()
        return scanner.configure(payload.get("universe"), payload.get("params"))

    @app.post("/api/scanner/scan")
    def scanner_scan():
        job = runner.submit("scan", "Breakout scan", lambda job: scanner.scan(
            lambda done, total, symbol: job.log(f"[{done}/{total}] {symbol}")))
        return {"job": job.view()}

    @app.post("/api/scanner/place")
    def scanner_place(payload: dict):
        payload = body_of(payload)
        mode = str(payload.get("mode", "paper"))
        if mode == "live" and " ".join(str(payload.get("phrase", "")).upper().split()) != "PLACE LIVE GTT":
            raise ValueError('Type "PLACE LIVE GTT" to place these orders with real money.')
        keys = payload.get("keys")
        if keys is not None and not (isinstance(keys, list) and all(isinstance(k, str) for k in keys)):
            raise ValueError("keys must be a list")
        return scanner.place(mode, keys)

    @app.post("/api/scanner/live")
    def scanner_live(payload: dict):
        return scanner.set_live(bool(body_of(payload).get("live")))

    @app.post("/api/scanner/cancel")
    def scanner_cancel(payload: dict):
        return scanner.cancel(str(body_of(payload).get("key", "")))

    @app.post("/api/scanner/forget")
    def scanner_forget(payload: dict):
        return scanner.forget(str(body_of(payload).get("key", "")))

    # -- indices, stock details, fundamentals ------------------------------------

    @app.get("/api/ticker")
    def ticker_strip():
        """The index strip along the top of every page."""
        if not settings.get("ticker_show"):
            return {"state": "hidden", "items": [], "every": 300}
        return {**ticker.snapshot(), "broker": chart_broker()}

    @app.get("/api/ticker/choices")
    def ticker_choices():
        return {"choices": ticker.choices(), "chosen": settings.get("ticker_indices")}

    @app.get("/api/indices")
    def index_cards():
        return indices.cards(settings.get("trend_rule"))

    @app.get("/api/tools/pulse")
    def tool_pulse():
        return finite(tools.pulse())

    @app.get("/api/tools/deals")
    def tool_deals(days: int = 5, min_value: float = 0.0):
        return finite(tools.deals(max(1, min(days, 30)), max(0.0, min_value)))

    @app.get("/api/tools/drift")
    def tool_drift(min_move: float = 4.0):
        return finite(tools.drift(max(1.0, min(min_move, 30.0))))

    @app.get("/api/tools/rotation")
    def tool_rotation(period: str = "weekly"):
        return finite(tools.rotation(period != "daily"))

    @app.get("/api/tools/radar")
    def tool_radar():
        return finite(tools.radar())

    def last_price(symbol: str) -> float | None:
        frame = sectors.bars(symbol)
        if frame is not None and len(frame):
            return round(float(frame["Close"].iloc[-1]), 2)
        return None

    @app.get("/api/financials")
    def stock_financials(symbol: str, basis: str = ""):
        """Quarterly results, profit and loss, balance sheet, cash flow, ratios (NSE filings) and peers."""
        canonical = symbol_of(symbol)
        if not kite_instrument(canonical):
            raise ValueError(f"{canonical} is not an NSE/BSE stock")
        price = last_price(canonical)
        if price is None:
            with contextlib.suppress(Exception):
                price = data.last_close(canonical, fetch=fetch)
        out = financials.view(canonical, basis or None, price)
        peers = peer_rows(canonical, indices.industry_of(), tools._names(), fundamentals.get, last_price)
        if peers["missing"]:
            fundamentals.queue(peers["missing"])
        out["peers"] = peers
        return finite(out)

    @app.post("/api/financials/refresh")
    def refresh_financials(payload: dict):
        body = body_of(payload)
        canonical = symbol_of(str(body.get("symbol", "")))
        if not kite_instrument(canonical):
            raise ValueError(f"{canonical} is not an NSE/BSE stock")
        financials.forget_listing(canonical)
        financials.request(canonical, str(body.get("basis") or "") or None)
        return {"queued": True}

    @app.get("/api/indices/{index_id}")
    def index_detail(index_id: str):
        return indices.detail(index_id, settings.get("trend_rule"))

    @app.get("/api/stock")
    def stock_detail(symbol: str):
        canonical = symbol_of(symbol)
        if not kite_instrument(canonical):
            raise ValueError(f"{canonical} is not an NSE/BSE stock")
        out = stock_view(canonical, kite, fundamentals, indices.industry_of(), settings.get("trend_rule"), fetch=fetch)
        out["sectors"] = sectors.sector_of().get(canonical, [])
        return out

    @app.put("/api/trend-rule")
    def set_trend_rule(payload: dict):
        settings.update({"trend_rule": body_of(payload)})
        return {"rule": settings.get("trend_rule"), "text": __import__("desk.trend", fromlist=["describe"]).describe(settings.get("trend_rule"))}

    # -- optimiser -------------------------------------------------------------

    @app.post("/api/optimise")
    def optimise_start(payload: dict):
        payload = body_of(payload)
        canonical = symbol_of(payload.get("symbol"))
        start, end = date_of(payload.get("start"), "Start date"), date_of(payload.get("end"), "End date")
        interval = str(payload.get("interval", "1d"))
        if interval not in data.INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(data.INTERVALS)}")
        strategy = library.strategy(str(payload.get("strategy", "")))
        ranges = payload.get("ranges") or {}
        if not isinstance(ranges, dict):
            raise ValueError("ranges must be an object")
        cfg = settings.all()
        params = BacktestParams(capital=_number(payload, "capital", cfg["paper_capital"]),
                                stop_loss_pct=_number(payload, "stop_loss_pct", 0.0),
                                slippage_bps=cfg["slippage_bps"], risk_free_rate_pct=cfg["risk_free_rate_pct"],
                                exchange="BSE" if canonical.endswith(".BO") else "NSE", intraday=interval != "1d",
                                positional=trade_type_of(payload, interval) == "positional",
                                max_hold_days=hold_days_of(payload),
                                trade_start=cfg["trade_start"], trade_end=cfg["trade_end"], square_off=cfg["square_off"])
        params.validate()
        from .optimiser import grid
        grid(strategy, ranges)                       # refuse a bad grid now, not inside the job
        objective = str(payload.get("objective", "sharpe"))
        folds, train_pct = int(_number(payload, "folds", 4)), _number(payload, "train_pct", 70)
        min_trades = int(_number(payload, "min_trades", 3))

        def work(job):
            bars, source = stock_bars(canonical, interval, start, end)
            job.log(source["note"])
            result = optimise(bars, strategy, ranges, params, objective, folds, train_pct, min_trades, progress=job.log)
            result.update(symbol=canonical, strategy=strategy.name, interval=interval)
            return result

        job = runner.submit("optimise", f"Optimise {strategy.name} on {canonical}", work)
        return {"job": job.view()}

    # -- pair trading and the signal test (Backtest > More tools) ----------------------------------------

    @app.get("/api/pairs/options")
    def pairs_options():
        from . import factortest, pairs
        return {"examples": [{"a": a, "b": b, "group": g} for a, b, g in pairs.EXAMPLES],
                "signals": {k: {"name": v[0], "text": v[1]} for k, v in factortest.SIGNALS.items()},
                "indices": scan_indices()}

    @app.post("/api/pairs/backtest")
    def pairs_backtest(payload: dict):
        from . import pairs
        payload = body_of(payload)
        first, second = symbol_of(payload.get("a")), symbol_of(payload.get("b"))
        if first == second:
            raise ValueError("Choose two different stocks")
        start, end = date_of(payload.get("start"), "Start date"), date_of(payload.get("end"), "End date")
        if end <= start:
            raise ValueError("End date must be after the start date")
        lookback = int(_number(payload, "lookback", 60))
        # Fetch the look-back before the start too, so the first day of the test already has a usable gap.
        from datetime import date as _date, timedelta as _td
        early = (_date.fromisoformat(start) - _td(days=int(lookback * 1.6) + 10)).isoformat()
        bars_a, source_a = stock_bars(first, "1d", early, end)
        bars_b, _ = stock_bars(second, "1d", early, end)
        result = pairs.backtest(bars_a, bars_b, lookback=lookback, entry_z=_number(payload, "entry_z", 2.0),
                                exit_z=_number(payload, "exit_z", 0.5), stop_z=_number(payload, "stop_z", 4.0),
                                max_days=int(_number(payload, "max_days", 0)), capital=_number(payload, "capital", 1_000_000),
                                cost_pct=_number(payload, "cost_pct", 0.05),
                                names=(first.removesuffix(".NS"), second.removesuffix(".NS")), start=start)
        result["data_note"] = f"Daily prices from {source_a['name']}."
        return result

    @app.post("/api/signal-test")
    def signal_test_start(payload: dict):
        import time as time_module

        from . import aiscan, factortest
        payload = body_of(payload)
        index_id = str(payload.get("index", "nifty500"))
        entry = indices.entry(index_id)
        if entry is None or not entry[3] or entry[3] == "bse":
            raise ValueError("Choose an NSE index with a published stock list")
        rows = indices.constituents(index_id)
        if not rows:
            raise ValueError(f"The stock list of {entry[1]} has not loaded yet. Open Indices, wait a moment and try again.")
        signal = str(payload.get("signal", ""))
        if signal not in factortest.SIGNALS:
            raise ValueError("Choose a signal")
        start, end = date_of(payload.get("start"), "Start date"), date_of(payload.get("end"), "End date")
        if end <= start:
            raise ValueError("End date must be after the start date")
        hold, groups = int(_number(payload, "hold", 5)), int(_number(payload, "groups", 5))
        cost_pct = _number(payload, "cost_pct", 0.2)
        from datetime import date as _date, timedelta as _td
        early = (_date.fromisoformat(start) - _td(days=420)).isoformat()      # a year of warm-up for 12-month signals
        cache = aiscan.PriceCache(home() / "scan_cache")

        def load(symbol: str):
            hit = cache.get(symbol, early, end)
            if hit:
                return hit[0]
            bars, source = stock_bars(symbol, "1d", early, end)
            cache.put(symbol, early, end, bars, source["name"])
            if source["name"] != "Yahoo":
                time_module.sleep(0.35)               # brokers cap history requests (Kite: three a second)
            return bars

        def work(job):
            closes, names, skipped = {}, {}, []
            job.progress = {"done": 0, "total": len(rows)}
            for i, row in enumerate(rows, 1):
                if job.stop_requested:
                    job.log(f"Stopped after {i - 1} stocks; testing on those.")
                    break
                symbol = row["symbol"]
                try:
                    closes[symbol] = load(symbol)["Close"]
                    names[symbol] = row.get("name") or symbol
                except Exception as exc:  # noqa: BLE001 - one missing stock does not stop the test
                    skipped.append(symbol)
                    job.log(f"{symbol}: skipped ({str(exc)[:80]})")
                job.progress = {"done": i, "total": len(rows)}
                if i % 50 == 0:
                    job.log(f"Prices loaded for {i} of {len(rows)} stocks")
            job.log(f"Ranking {len(closes)} stocks every {hold} trading days")
            result = factortest.run(closes, signal, start=start, end=end, hold=hold, groups=groups, cost_pct=cost_pct,
                                    names=names)
            result.update(index_name=entry[1], skipped=skipped)
            job.log("Done")
            return result

        job = scan_runner.submit("signal", f"Signal test: {factortest.SIGNALS[signal][0]} on {entry[1]}", work)
        return {"job": job.view()}

    @app.post("/api/patterns/pooled")
    def patterns_pooled(payload: dict):
        """Candle pattern odds with the cases of every stock in an NSE index added together, on day candles
        up to ``end`` (a replay passes its cut, so nothing after it is read)."""
        import time as time_module

        from . import aiscan, candles
        payload = body_of(payload)
        index_id = str(payload.get("index", "nifty50"))
        entry = indices.entry(index_id)
        if entry is None or not entry[3] or entry[3] == "bse":
            raise ValueError("Choose an NSE index with a published stock list")
        rows = indices.constituents(index_id)
        if not rows:
            raise ValueError(f"The stock list of {entry[1]} has not loaded yet. Open Indices, wait a moment and try again.")
        filters = candles.clean_filters(payload.get("filters"))
        min_cases = int(_number(payload, "min_cases", candles.MIN_CASES))
        if not 5 <= min_cases <= 500:
            raise ValueError("Fewest cases to trust must be from 5 to 500")
        end = date_of(payload.get("end") or market.now_ist().strftime("%Y-%m-%d"), "End date")
        years = int(_number(payload, "years", 6))
        if not 1 <= years <= 15:
            raise ValueError("Years of history must be from 1 to 15")
        from datetime import date as _date, timedelta as _td
        start = (_date.fromisoformat(end) - _td(days=365 * years)).isoformat()
        cache = aiscan.PriceCache(home() / "scan_cache")

        def load(symbol: str):
            hit = cache.get(symbol, start, end)
            if hit:
                return hit[0]
            bars, source = stock_bars(symbol, "1d", start, end)
            cache.put(symbol, start, end, bars, source["name"])
            if source["name"] != "Yahoo":
                time_module.sleep(0.35)               # brokers cap history requests (Kite: three a second)
            return bars

        def work(job):
            frames, skipped = {}, []
            job.progress = {"done": 0, "total": len(rows)}
            for i, row in enumerate(rows, 1):
                if job.stop_requested:
                    job.log(f"Stopped after {i - 1} stocks; counting those.")
                    break
                try:
                    bars = load(row["symbol"])
                    frames[row["symbol"]] = bars[bars.index <= pd_ts(end)]
                except Exception as exc:  # noqa: BLE001 - one missing stock does not stop the count
                    skipped.append(row["symbol"])
                    job.log(f"{row['symbol']}: skipped ({str(exc)[:80]})")
                job.progress = {"done": i, "total": len(rows)}
            result = candles.pooled(frames, filters=filters, min_cases=min_cases)
            result.update(index_name=entry[1], skipped=skipped, start=start, end=end,
                          note=(f"Day candles from {start} to {end}. The stock list is today's {entry[1]}, so stocks "
                                "that dropped out of it are missing; that flatters patterns a little."))
            job.log("Done")
            return result

        job = scan_runner.submit("patterns", f"Candle patterns across {entry[1]}", work)
        return {"job": job.view()}

    # -- options ----------------------------------------------------------------

    @app.get("/api/futures")
    def index_futures():
        """Index futures with Kite's live quote, the index level and the basis (future minus spot)."""
        from . import options as opt

        if derivatives.stale() and kite.connected:
            with contextlib.suppress(KiteError):
                derivatives.refresh()
        rows = derivatives.futures()
        if not rows:
            return {"futures": [], "connected": kite.connected,
                    "note": "Connect Kite on the Trade tab to load index futures." if not kite.connected else
                    "Kite returned no index futures yet. Press Refresh from Kite below."}
        names = [f"{r['exchange']}:{r['tradingsymbol']}" for r in rows]
        spots = sorted({opt.SPOT[r["underlying"]] for r in rows if r["underlying"] in opt.SPOT})
        quotes = {}
        with contextlib.suppress(KiteError):
            quotes = kite.quotes(names + spots)
        today = pd_ts(market.now_ist().strftime("%Y-%m-%d"))
        out = []
        for r, name in zip(rows, names, strict=True):
            q = quotes.get(name) or {}
            spot = (quotes.get(opt.SPOT.get(r["underlying"], "")) or {}).get("last_price")
            last, close = q.get("last_price"), (q.get("ohlc") or {}).get("close")
            days = max(int((pd_ts(r["expiry"]) - today).days), 0)
            basis = round(last - spot, 2) if last and spot else None
            out.append({**r, "instrument": name, "ltp": last, "change_pct": round((last / close - 1) * 100, 2) if last and close else None,
                        "oi": q.get("oi"), "volume": q.get("volume"), "spot": spot, "basis": basis, "days": days,
                        "basis_annual_pct": round(basis / spot * 365 / days * 100, 2) if basis is not None and days else None})
        return {"futures": out, "connected": kite.connected, "note": None}

    @app.get("/api/options/underlyings")
    def option_underlyings():
        if derivatives.stale() and kite.connected:
            with contextlib.suppress(KiteError):
                derivatives.refresh()
        return {"underlyings": derivatives.underlyings(), "connected": kite.connected}

    @app.get("/api/options/chain")
    def option_chain(underlying: str, expiry: str = "", width: int = 12):
        from . import options as opt
        underlying = underlying.strip().upper()
        if underlying not in opt.SPOT:
            raise ValueError(f"underlying must be one of {', '.join(opt.SPOT)}")
        if not kite.connected or "get_quotes" not in kite.tools:
            raise ValueError("The option chain is read live from Kite. Open Trade, press Connect and log in to Kite.")
        contracts = derivatives.contracts(underlying)
        if not contracts:
            derivatives.refresh()
            contracts = derivatives.contracts(underlying)
        if not contracts:
            raise ValueError(f"Kite listed no {underlying} options. Refresh the contract list from Kite.")
        expiries = sorted({c["expiry"] for c in contracts if c["expiry"] >= market.now_ist().strftime("%Y-%m-%d")})
        if not expiries:
            raise ValueError(f"Every {underlying} expiry in the contract list has passed. Refresh the contract list from Kite.")
        expiry = expiry or expiries[0]
        if expiry not in expiries:
            raise ValueError("That expiry is not listed.")
        spot_name = opt.SPOT[underlying]
        spot = kite.ltp([spot_name]).get(spot_name)
        if not spot:
            raise ValueError(f"Kite gave no price for {spot_name}.")
        width = max(3, min(int(width), 30))
        near = sorted({c["strike"] for c in contracts if c["expiry"] == expiry}, key=lambda k: abs(k - spot))[: 2 * width + 1]
        wanted = [f"{c['exchange']}:{c['tradingsymbol']}" for c in contracts if c["expiry"] == expiry and c["strike"] in near]
        quotes = {}
        for begin in range(0, len(wanted), 500):
            quotes.update(kite.quotes(wanted[begin:begin + 500]))
        today = market.now_ist().strftime("%Y-%m-%d")
        if first_oi["day"] != today:
            first_oi.update(day=today, values={})
        for name, quote in quotes.items():
            if quote.get("oi") is not None:
                first_oi["values"].setdefault(name, quote["oi"])
        chain = opt.build_chain(contracts, quotes, spot, expiry, market.now_ist(), width, first_oi["values"])
        chain.update(underlying=underlying, spot_instrument=spot_name, expiries=expiries,
                     lot_size=contracts[0]["lot_size"], session=market.session_status())
        return chain

    # -- MCX gold and silver --------------------------------------------------

    def _mcx_contracts():
        if derivatives.stale() and kite.connected:
            with contextlib.suppress(KiteError):
                derivatives.refresh()

    def _mcx_future_row(c: dict, quote: dict, cfg: dict, now) -> dict:
        last, close = quote.get("last_price"), (quote.get("ohlc") or {}).get("close")
        days = mcx.days_to_expiry(c["expiry"], now)
        value = last * c["multiplier"] if last else None
        return {**c, "instrument": f"{c['exchange']}:{c['tradingsymbol']}", "ltp": last,
                "change_pct": round((last / close - 1) * 100, 2) if last and close else None,
                "oi": quote.get("oi"), "volume": quote.get("volume"), "days": days,
                "cutoff": mcx.in_delivery_window(c["expiry"], now, cfg["mcx_exit_days"]),
                "value_per_lot": round(value, 0) if value else None,
                "margin_per_lot": round(value * cfg["mcx_margin_pct"] / 100, 0) if value else None}

    @app.get("/api/mcx")
    def mcx_view():
        """MCX gold and silver: specs, futures with Kite quotes, option expiries, the session and the cut-off."""
        _mcx_contracts()
        view, cfg, now = derivatives.metals(), settings.all(), market.now_ist()
        names = [f"{c['exchange']}:{c['tradingsymbol']}" for m in view["metals"] for c in m["futures"]]
        quotes = {}
        if names and kite.connected and "get_quotes" in kite.tools:
            with contextlib.suppress(KiteError):
                quotes = kite.quotes(names)
        for metal in view["metals"]:
            metal["futures"] = [_mcx_future_row(c, quotes.get(f"{c['exchange']}:{c['tradingsymbol']}") or {}, cfg, now)
                                for c in metal["futures"]]
        view.update(session=mcx.session_status(now), exit_days=cfg["mcx_exit_days"], margin_pct=cfg["mcx_margin_pct"],
                    trade_window=[cfg["mcx_trade_start"], cfg["mcx_trade_end"]])
        return view

    @app.get("/api/mcx/chain")
    def mcx_chain(name: str, expiry: str = "", width: int = 10):
        """An MCX metal's option chain, around the future the options are written on."""
        from . import options as opt
        name = name.strip().upper()
        if name not in mcx.METALS:
            raise ValueError(f"name must be one of {', '.join(mcx.METALS)}")
        if not kite.connected or "get_quotes" not in kite.tools:
            raise ValueError("The MCX option chain is read live from Kite. Open Trade, press Connect and log in to Kite.")
        _mcx_contracts()
        options = [c for c in derivatives.metal_contracts(name) if c["type"] != "FUT"]
        if not options:
            raise ValueError(f"Kite lists no {name} options. MCX lists options on GOLD, GOLDM, SILVER, SILVERM and "
                             f"some smaller contracts only; refresh the contract list or pick another metal.")
        expiries = sorted({c["expiry"] for c in options})
        expiry = expiry or expiries[0]
        if expiry not in expiries:
            raise ValueError("That expiry is not listed.")
        future = next((c for c in derivatives.metal_futures(name) if c["expiry"] >= expiry), None)
        if future is None:
            raise ValueError(f"No {name} future expiring on or after {expiry} is loaded: the option's underlying is missing.")
        spot_name = f"{future['exchange']}:{future['tradingsymbol']}"
        spot = kite.ltp([spot_name]).get(spot_name)
        if not spot:
            raise ValueError(f"Kite gave no price for {spot_name}.")
        width = max(3, min(int(width), 30))
        near = sorted({c["strike"] for c in options if c["expiry"] == expiry}, key=lambda k: abs(k - spot))[: 2 * width + 1]
        wanted = [f"{c['exchange']}:{c['tradingsymbol']}" for c in options if c["expiry"] == expiry and c["strike"] in near]
        quotes = {}
        for begin in range(0, len(wanted), 500):
            quotes.update(kite.quotes(wanted[begin:begin + 500]))
        now = market.now_ist()
        chain = opt.build_chain(options, quotes, spot, expiry, now, width, None, forward=True,
                                close=mcx.close_time(datetime_on(expiry)))
        cfg = settings.all()
        chain.update(underlying=name, spot_instrument=spot_name, future=future, expiries=expiries, lot_size=1,
                     multiplier=mcx.METALS[name]["units_per_lot"], spec=mcx.METALS[name], session=mcx.session_status(now),
                     cutoff=mcx.in_delivery_window(expiry, now, cfg["mcx_exit_days"]), exit_days=cfg["mcx_exit_days"])
        return chain

    @app.get("/api/mcx/positions")
    def mcx_positions(mode: str = "paper"):
        cfg, now = settings.all(), market.now_ist()

        def row(exchange, symbol, quantity, average, last, product=None):
            contract = derivatives.contract(exchange, symbol)
            factor = mcx.multiplier(exchange, symbol)
            return {"instrument": f"{exchange}:{symbol}", "tradingsymbol": symbol, "exchange": exchange,
                    "quantity": quantity, "average": None if average is None else round(float(average), 2), "last": last,
                    "multiplier": factor, "product": product,
                    "pnl": round((last - float(average)) * quantity * factor, 2) if last and average is not None else None,
                    "contract": contract,
                    "cutoff": bool(contract and mcx.in_delivery_window(contract["expiry"], now, cfg["mcx_exit_days"]))}

        rows = []
        if mode == "paper":
            for instrument, held in desk.paper.snapshot()["positions"].items():
                exchange, symbol = instrument.split(":", 1)
                if exchange == mcx.EXCHANGE and held["quantity"] and mcx.name_of(symbol):
                    rows.append(row(exchange, symbol, int(held["quantity"]), held["average_price"], desk.last_price(instrument)[0]))
            return {"positions": rows}
        if mode != "live":
            raise ValueError("mode must be paper or live")
        for p in kite.positions() if kite.connected else []:
            symbol = str(p.get("tradingsymbol", "")).upper()
            if str(p.get("exchange")) == mcx.EXCHANGE and int(p.get("quantity") or 0) and mcx.name_of(symbol):
                r = row(mcx.EXCHANGE, symbol, int(p["quantity"]), p.get("average_price"), p.get("last_price"), p.get("product"))
                kite_multiplier = p.get("multiplier")
                if kite_multiplier and float(kite_multiplier) != r["multiplier"]:
                    r["warning"] = (f"Kite reports a multiplier of {kite_multiplier} for {symbol}; the desk uses "
                                    f"{r['multiplier']}. Trust Kite's P&L and report this.")
                r["pnl"] = p.get("pnl", r["pnl"])                   # Kite's own figure is the authority live
                rows.append(r)
        return {"positions": rows}

    @app.get("/api/options/positions")
    def option_positions(mode: str = "paper"):
        if mode == "paper":
            rows = []
            for instrument, held in desk.paper.snapshot()["positions"].items():
                exchange, symbol = instrument.split(":", 1)
                if exchange in ("NFO", "BFO") and held["quantity"]:
                    last = desk.last_price(instrument)[0]
                    rows.append({"instrument": instrument, "tradingsymbol": symbol, "exchange": exchange, "quantity": held["quantity"],
                                 "average": round(held["average_price"], 2), "last": last,
                                 "pnl": round((last - held["average_price"]) * held["quantity"], 2) if last else None,
                                 "contract": derivatives.contract(exchange, symbol)})
            return {"positions": rows}
        if mode != "live":
            raise ValueError("mode must be paper or live")
        rows = []
        for row in kite.positions() if kite.connected else []:
            if str(row.get("exchange")) in ("NFO", "BFO") and int(row.get("quantity") or 0):
                rows.append({"instrument": f"{row['exchange']}:{row['tradingsymbol']}", "tradingsymbol": row["tradingsymbol"],
                             "exchange": row["exchange"], "quantity": int(row["quantity"]), "average": row.get("average_price"),
                             "last": row.get("last_price"), "pnl": row.get("pnl"), "product": row.get("product"),
                             "contract": derivatives.contract(row["exchange"], row["tradingsymbol"])})
        return {"positions": rows}

    @app.post("/api/backtest/options")
    def backtest_options(payload: dict):
        from . import options as opt
        payload = body_of(payload)
        underlying = str(payload.get("underlying", "NIFTY")).upper()
        if underlying not in opt.SPOT:
            raise ValueError(f"underlying must be one of {', '.join(opt.SPOT)}")
        start, end = date_of(payload.get("start"), "Start date"), date_of(payload.get("end"), "End date")
        interval = str(payload.get("interval", "1d"))
        if interval not in data.INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(data.INTERVALS)}")
        strategy = library.strategy(str(payload.get("strategy", "")))
        bars, source = None, ""
        if kite.connected:
            with contextlib.suppress(KiteError, data.NoData):
                if interval == "1d":
                    whole = data.kite_bars(kite, opt.SPOT[underlying], "1d", market.now_ist(), raw=True)
                    bars = whole[(whole.index >= pd_ts(start)) & (whole.index <= pd_ts(end))]
                else:
                    instrument = opt.SPOT[underlying]
                    bars = _kite_range_raw(kite, instrument, interval, start, end)
                source = "Kite"
        if (bars is None or bars.empty) and underlying in opt.YAHOO_SPOT:
            bars, source = data.bars_for(opt.YAHOO_SPOT[underlying], interval, start, end, fetch=fetch), "Yahoo"
        if bars is None or bars.empty:
            raise ValueError("No candles for this index. Log in to Kite (it has every index).")
        vix = data.history("^INDIAVIX", fetch=fetch)["Close"]
        positions = strategy.positions(bars, strategy.resolve(payload.get("strategy_params")))
        cfg = settings.all()
        contracts = derivatives.contracts(underlying)
        lot = int(_number(payload, "lot_size", contracts[0]["lot_size"] if contracts else opt.LOT_DEFAULT.get(underlying, 50)))
        lots = int(_number(payload, "lots", 1))
        result = scalein.run_all(lambda s, a: opt.run_options(
            underlying, bars, positions, vix, capital=_number(payload, "capital", cfg["paper_capital"]),
            lots=lots, lot_size=lot, side=str(payload.get("side", "calls")),
            moneyness=int(_number(payload, "moneyness", 0)), min_days=_number(payload, "min_days", 2),
            expiry_weekday=int(_number(payload, "expiry_weekday", 3 if underlying in BSE_INDICES else 1)),
            stop_pct=_number(payload, "stop_pct", 0), target_pct=_number(payload, "target_pct", 0),
            stop_rs=_number(payload, "stop_rs", 0), target_rs=_number(payload, "target_rs", 0),
            iv_scale=_number(payload, "iv_scale", 1.0), slippage_pct=_number(payload, "slippage_pct", 1.0),
            intraday=interval != "1d", square_off=cfg["square_off"], strategy_name=strategy.name, steps=s, adds=a),
            scaleout.from_payload(payload.get("steps"), "option", lots), scalein.from_payload(payload.get("adds"), "option", lots))
        skipped = result.get("skipped_entries") or 0
        result.update(interval=interval, data_note=f"Underlying candles from {source}. Option premiums are simulated with "
                      f"Black-Scholes and India VIX × {_number(payload, 'iv_scale', 1.0):g}: Kite keeps no candles for expired contracts."
                      + (f" {skipped} signals were not taken: the capital did not cover the premium." if skipped else ""),
                      benchmark_name=None, benchmark_note=None)
        return result

    @app.post("/api/backtest/futures")
    def backtest_futures(payload: dict):
        import pandas as pd

        from . import futures as fut, options as opt
        payload = body_of(payload)
        underlying = str(payload.get("underlying", "NIFTY")).upper()
        if underlying not in opt.SPOT:
            raise ValueError(f"underlying must be one of {', '.join(opt.SPOT)}")
        start, end = date_of(payload.get("start"), "Start date"), date_of(payload.get("end"), "End date")
        interval = str(payload.get("interval", "1d"))
        if interval not in data.INTERVALS:
            raise ValueError(f"interval must be one of {', '.join(data.INTERVALS)}")
        strategy = library.strategy(str(payload.get("strategy", "")))
        exchange = exchange_of(underlying)
        front = next((c for c in derivatives.futures() if c["underlying"] == underlying), None)
        bars, source, continuous = None, "", False
        if kite.connected and front:
            instrument = f"{front['exchange']}:{front['tradingsymbol']}"
            with contextlib.suppress(KiteError, data.NoData):
                if interval == "1d":
                    bars = _kite_range_raw(kite, instrument, "1d", start, end, continuous=True)
                    source, continuous = "Kite continuous futures (day candles)", True
                else:
                    own = _kite_range_raw(kite, instrument, interval, start, end)
                    if not own.empty and own.index[0] <= pd_ts(start) + pd.Timedelta(days=5):
                        bars, source = own, f"Kite, the {front['tradingsymbol']} contract's own candles"
        if (bars is None or bars.empty) and kite.connected:
            with contextlib.suppress(KiteError, data.NoData):
                bars = _kite_range_raw(kite, opt.SPOT[underlying], interval, start, end)
                source = ("Kite index candles as a proxy (Kite keeps intraday candles only for the live contract; "
                          "the basis is ignored)" if interval != "1d" else "Kite index candles as a proxy (basis and rolls ignored)")
        if (bars is None or bars.empty) and underlying in opt.YAHOO_SPOT:
            bars = data.bars_for(opt.YAHOO_SPOT[underlying], interval, start, end, fetch=fetch)
            source = "Yahoo index candles as a proxy (basis and rolls ignored). Log in to Kite for real futures prices"
        if bars is None or bars.empty:
            raise ValueError("No candles for this index. Log in to Kite (it has every index and its futures).")
        bars = bars[(bars.index >= pd_ts(start) - pd.Timedelta(days=1)) & (bars.index <= pd_ts(end))]
        signal = strategy.positions(bars, strategy.resolve(payload.get("strategy_params")))
        cfg = settings.all()
        lot = int(_number(payload, "lot_size", (front or {}).get("lot_size") or opt.LOT_DEFAULT.get(underlying, 50)))
        lots = int(_number(payload, "lots", 1))
        result = scalein.run_all(lambda s, a: fut.run_futures(
            bars, signal, underlying=underlying, lot_size=lot, lots=lots, steps=s, adds=a,
            capital=_number(payload, "capital", cfg["paper_capital"]), direction=str(payload.get("direction", "long")),
            margin_pct=_number(payload, "margin_pct", 12.0), stop_pct=_number(payload, "stop_pct", 0),
            target_pct=_number(payload, "target_pct", 0), slippage_pct=_number(payload, "slippage_pct", 0.02),
            stop_rs=_number(payload, "stop_rs", 0), target_rs=_number(payload, "target_rs", 0),
            intraday=interval != "1d", trade_start=cfg["trade_start"], trade_end=cfg["trade_end"], square_off=cfg["square_off"],
            continuous=continuous, carry_pct=_number(payload, "carry_pct", 7.0), holidays=frozenset(market.HOLIDAYS),
            exchange=exchange, strategy_name=strategy.name), scaleout.from_payload(payload.get("steps"), "futures", lots),
            scalein.from_payload(payload.get("adds"), "futures", lots))
        m = result["metrics"]
        notes = [f"Prices: {source}."]
        if continuous:
            notes.append(f"{m['rolls']} monthly rolls, each charged a round trip with the basis taken out at {_number(payload, 'carry_pct', 7.0):g}% a year.")
        if m["skipped_entries"]:
            notes.append(f"{m['skipped_entries']} entries were skipped: equity did not cover the margin.")
        notes.append(f"Margin assumed {_number(payload, 'margin_pct', 12.0):g}% of contract value (about ₹{m['margin_per_lot']:,.0f} a lot now). "
                     "Buy & hold is the index itself without leverage.")
        result.update(interval=interval, data_note=" ".join(notes), benchmark_name=None, benchmark_note=None)
        return result

    @app.post("/api/backtest/structure")
    def backtest_structure(payload: dict):
        import pandas as pd

        from . import options as opt, optstrat
        payload = body_of(payload)
        underlying = str(payload.get("underlying", "NIFTY")).upper()
        if underlying not in opt.SPOT:
            raise ValueError(f"underlying must be one of {', '.join(opt.SPOT)}")
        start, end = date_of(payload.get("start"), "Start date"), date_of(payload.get("end"), "End date")
        mode = str(payload.get("mode", "positional"))
        interval = str(payload.get("interval", "5m" if mode == "intraday" else "1d"))
        if mode == "intraday" and interval not in ("1m", "2m", "3m", "4m", "5m", "15m", "30m"):
            raise ValueError("Intraday structures use 1, 2, 3, 4, 5, 15 or 30-minute candles.")
        if mode != "intraday":
            interval = "1d"
        bars, source = None, ""
        if kite.connected:
            with contextlib.suppress(KiteError, data.NoData):
                bars = _kite_range_raw(kite, opt.SPOT[underlying], interval, start, end)
                source = "Kite"
        if (bars is None or bars.empty) and underlying in opt.YAHOO_SPOT:
            bars, source = data.bars_for(opt.YAHOO_SPOT[underlying], interval, start, end, fetch=fetch), "Yahoo"
        if bars is None or bars.empty:
            raise ValueError("No candles for this index. Log in to Kite (it has every index, intraday for years).")
        bars = bars[(bars.index >= pd_ts(start) - pd.Timedelta(days=1)) & (bars.index <= pd_ts(end))]
        vix = data.history("^INDIAVIX", fetch=fetch)["Close"]
        signal = None
        if mode == "signal":
            strategy = library.strategy(str(payload.get("strategy", "")))
            signal = strategy.positions(bars, strategy.resolve(payload.get("strategy_params")))
        weekdays = payload.get("weekdays") or [0, 1, 2, 3, 4]
        if not isinstance(weekdays, list) or not all(isinstance(d, int) and 0 <= d <= 4 for d in weekdays):
            raise ValueError("weekdays must be a list of 0 (Mon) to 4 (Fri)")
        contracts = derivatives.contracts(underlying)
        lot = int(_number(payload, "lot_size", contracts[0]["lot_size"] if contracts else opt.LOT_DEFAULT.get(underlying, 50)))
        cfg = settings.all()
        times = [str(payload.get("entry_time", "09:20")), str(payload.get("exit_time", "15:15"))]
        for t in times:
            if not re.fullmatch(r"(0?9|1[0-5]):[0-5]\d", t):
                raise ValueError("Entry and exit times must be between 09:15 and 15:30, like 09:20")
        premiums = str(payload.get("premiums") or "real")
        if premiums not in ("real", "model"):
            raise ValueError("premiums must be real or model")
        stored_days = chain_history.covers(underlying, bars.index[0].date(), bars.index[-1].date()) if premiums == "real" else 0
        lots = int(_number(payload, "lots", 1))
        result = scalein.run_all(lambda s, a: optstrat.run_structure(
            underlying, bars, vix, structure=str(payload.get("structure", "")), mode=mode, lot_size=lot,
            history=chain_history if stored_days else None, steps=s, adds=a,
            lots=lots, capital=_number(payload, "capital", cfg["paper_capital"]),
            k=int(_number(payload, "k", 2)), w=int(_number(payload, "w", 4)), expiry_kind=str(payload.get("expiry_kind", "weekly")),
            min_days=_number(payload, "min_days", 0 if mode == "intraday" else 1), weekdays=tuple(weekdays),
            entry_time=times[0], exit_time=times[1], exit_days_before=int(_number(payload, "exit_days_before", 0)),
            leg_sl_pct=_number(payload, "leg_sl_pct", 0), stop_pct=_number(payload, "stop_pct", 0),
            target_pct=_number(payload, "target_pct", 0), signal=signal, margin_pct=_number(payload, "margin_pct", 12),
            iv_scale=_number(payload, "iv_scale", 1.0), skew=_number(payload, "skew", 0.0),
            slippage_pct=_number(payload, "slippage_pct", 1.0), holidays=frozenset(market.HOLIDAYS),
            leg_exits=risk.from_payload(payload.get("leg_exits"), "Each leg",
                                        legacy={"stop_loss": _number(payload, "leg_sl_pct", 0)}),
            leg_scope=str(payload.get("leg_scope") or "sold"), legs=payload.get("legs") or None,
            stop_rs=_number(payload, "stop_rs", 0), target_rs=_number(payload, "target_rs", 0),
            expiry_ahead=_number(payload, "expiry_ahead", 0)),
            scaleout.from_payload(payload.get("steps"), "structure", lots), scalein.from_payload(payload.get("adds"), "structure", lots))
        m = result["metrics"]
        notes = [f"Index candles from {source}.", _premium_note(result.get("pricing"), premiums, mode, underlying,
                                                              _number(payload, "iv_scale", 1.0))]
        if m["skipped_entries"]:
            notes.append(f"{m['skipped_entries']} entries skipped: equity did not cover the margin.")
        if m.get("legs_stopped"):
            notes.append(f"{m['legs_stopped']} legs hit their own stoploss or target.")
        if m.get("reentries"):
            notes.append(f"{m['reentries']} re-entries after a stoploss.")
        if m.get("avg_margin"):
            notes.append(f"Average margin blocked ₹{m['avg_margin']:,.0f}; trade returns are on that margin.")
        result.update(interval=interval, data_note=" ".join(notes), benchmark_name=None, benchmark_note=None)
        return result

    def _premium_note(used: dict | None, premiums: str, mode: str, underlying: str, iv_scale: float) -> str:
        model = f"Black-Scholes on India VIX × {iv_scale:g}"
        if premiums == "model":
            return f"Every premium is modelled ({model}), as you chose."
        if not used:
            where = ("no live chain recorded for these days" if mode == "intraday"
                     else "no NSE history imported for this period")
            return (f"Every premium is modelled ({model}): {where} for {underlying}. "
                    "Import NSE history under Real option prices to use traded prices.")
        fills = used["traded"] + used["settle"] + used["recorded"] + used["model"]
        parts = []
        if used["traded"]:
            parts.append(f"{used['traded']} from NSE traded prices")
        if used["settle"]:
            parts.append(f"{used['settle']} from NSE settlement prices (strike did not trade that day)")
        if used["recorded"]:
            parts.append(f"{used['recorded']} from recorded live prices")
        if used["model"]:
            parts.append(f"{used['model']} modelled ({model}) where no real price was stored")
        text = f"Premiums of {fills} fills: " + "; ".join(parts) + "."
        if used["expiry"]:
            text += f" {used['expiry']} legs settled at expiry against the index close."
        if mode == "intraday" and used["recorded"] == 0:
            text += " The NSE bhavcopy has one price a day, so intraday entries need the live chain recorder."
        return text

    @app.get("/api/options/history")
    def option_history_status():
        cfg = settings.all()
        return {**chain_history.status(), "recorder": {"underlyings": cfg["chain_recorder"],
                                                       "minutes": cfg["chain_recorder_minutes"],
                                                       "last": chain_recorder.last},
                "choices": list(HISTORY_UNDERLYINGS), "defaults": list(HISTORY_DEFAULT), "proxy": bool(nse_proxy())}

    @app.post("/api/options/history/import")
    def option_history_import(payload: dict):
        payload = body_of(payload)
        start = datetime.strptime(date_of(payload.get("start"), "From"), "%Y-%m-%d").date()
        end = datetime.strptime(date_of(payload.get("end"), "To"), "%Y-%m-%d").date()
        today = market.now_ist().date()
        if start > end or end > today:
            raise ValueError("From must be before To, and To no later than today.")
        if start.year < 2010:
            raise ValueError("NSE's archive of these files starts around 2000, but weekly options began in 2016-2019; "
                             "start from 2010 or later.")
        underlyings = payload.get("underlyings") or list(HISTORY_DEFAULT)
        if not isinstance(underlyings, list) or not underlyings or not set(underlyings) <= set(HISTORY_UNDERLYINGS):
            raise ValueError(f"underlyings must be some of {', '.join(HISTORY_UNDERLYINGS)}")
        return chain_history.start_import(start, end, underlyings)

    @app.post("/api/options/history/stop")
    def option_history_stop():
        chain_history.stop()
        return chain_history.job

    def _index_spot(underlying: str) -> tuple[float, str]:
        """The index's latest price and where it came from: Kite's last price, else Yahoo's last close."""
        from . import options as opt
        if kite.connected:
            with contextlib.suppress(KiteError):
                spot = float(kite.ltp([opt.SPOT[underlying]]).get(opt.SPOT[underlying]) or 0)
                if spot > 0:
                    return spot, "index price from Kite"
        if underlying in opt.YAHOO_SPOT:
            with contextlib.suppress(data.NoData, KeyError, IndexError):
                return float(data.history(opt.YAHOO_SPOT[underlying], fetch=fetch)["Close"].iloc[-1]), "index price: last close from Yahoo"
        return 0.0, ""

    @app.get("/api/options/spot")
    def option_spot(underlying: str = "NIFTY"):
        """The index's price and at-the-money strike now, so the leg editor can show each strike's price today."""
        from . import options as opt
        underlying = underlying.upper()
        if underlying not in opt.SPOT:
            raise ValueError(f"underlying must be one of {', '.join(opt.SPOT)}")
        spot, source = _index_spot(underlying)
        step = opt.STRIKE_STEP.get(underlying, 50)
        return {"underlying": underlying, "spot": round(spot, 2) if spot else None, "step": step,
                "atm": round(spot / step) * step if spot else None, "source": source}

    @app.post("/api/options/payoff")
    def option_payoff(payload: dict):
        """One option structure worked out now: legs, payoff, breakevens, maximum profit and loss, Greeks, margin."""
        import pandas as pd

        from . import options as opt, optstrat
        payload = body_of(payload)
        underlying = str(payload.get("underlying", "NIFTY")).upper()
        if underlying not in opt.SPOT:
            raise ValueError(f"underlying must be one of {', '.join(opt.SPOT)}")
        structure = str(payload.get("structure", ""))
        own, ahead = optstrat.expiry_shift(payload.get("legs") or None, _number(payload, "expiry_ahead", 0))
        specs = optstrat.legs_for(structure, int(_number(payload, "k", 2)), int(_number(payload, "w", 4)), own)
        sources = []
        spot = _number(payload, "spot", 0)
        if spot <= 0:
            spot, said = _index_spot(underlying)
            if said:
                sources.append(said)
        if spot <= 0:
            raise ValueError(f"No price for {underlying}. Type the index level in Spot, or log in to Kite.")
        iv_pct = _number(payload, "iv_pct", 0)
        if iv_pct <= 0:
            try:
                iv_pct = float(data.history("^INDIAVIX", fetch=fetch)["Close"].iloc[-1]) * _number(payload, "iv_scale", 1.0)
            except (data.NoData, KeyError, IndexError):
                raise ValueError("No India VIX to use as the volatility. Type the volatility (IV %).") from None
            sources.append(f"volatility: India VIX × {_number(payload, 'iv_scale', 1.0):g}")
        expiry_kind = str(payload.get("expiry_kind", "weekly"))
        if expiry_kind not in ("weekly", "monthly"):
            raise ValueError("expiry_kind must be weekly or monthly")
        if expiry_kind == "weekly" and underlying not in ("NIFTY", "SENSEX"):
            raise ValueError(f"{underlying} has had only monthly options since November 2024. Choose monthly expiry.")
        days = payload.get("days")
        far_days = payload.get("far_days")
        moment = pd.Timestamp(market.now_ist().replace(tzinfo=None))
        holidays = frozenset(market.HOLIDAYS)
        near = optstrat.expiry_ahead_of(underlying, moment, expiry_kind, 0.0, ahead, holidays)
        if days in (None, ""):
            days = max((near - moment).total_seconds() / 86400, 0.0)
            sources.append(f"{'near ' if any(x['series'] == 'far' for x in specs) else ''}expiry {near:%a %d %b}")
        if far_days in (None, "") and any(x["series"] == "far" for x in specs):
            base = moment + pd.Timedelta(days=float(_number(payload, "days", days)))
            far = optstrat.expiry_after(underlying, max(base, near) + pd.Timedelta(minutes=1), expiry_kind, 0.0, holidays)
            far_days = (far - moment).total_seconds() / 86400
            sources.append(f"far expiry {far:%a %d %b}")
        contracts = derivatives.contracts(underlying)
        if not contracts and kite.connected:
            with contextlib.suppress(KiteError):
                derivatives.refresh()
                contracts = derivatives.contracts(underlying)
        lot = int(_number(payload, "lot_size", contracts[0]["lot_size"] if contracts else opt.LOT_DEFAULT.get(underlying, 50)))
        premiums = payload.get("premiums") or None
        labels = payload.get("labels") if premiums else None
        args = {"k": int(_number(payload, "k", 2)), "w": int(_number(payload, "w", 4)), "lots": int(_number(payload, "lots", 1)),
                "lot_size": lot, "margin_pct": _number(payload, "margin_pct", 12)}

        def work(iv_now, near_days, far_now, given, given_labels):
            return optstrat.analyse(
                underlying, structure, spot=spot, iv=iv_now / 100, days=float(_number({"days": near_days}, "days", 0)),
                far_days=None if far_now in (None, "") else float(_number({"far_days": far_now}, "far_days", 0)),
                premiums=given, labels=given_labels, legs=own, **args)

        result = work(iv_pct, days, far_days, premiums, labels)
        notes = []
        if premiums is None:
            typed = [name for name in ("spot", "days", "far_days") if payload.get(name) not in (None, "")]
            if typed:
                notes.append("Premiums are modelled because you typed " + " and ".join(
                    {"spot": "Spot", "days": "Days to expiry", "far_days": "Days to far expiry"}[n] for n in typed)
                    + ". Clear those boxes to fill premiums from Kite.")
            else:
                live = _kite_premiums(kite, derivatives, underlying, result["legs"], contracts, near,
                                      None if far_days in (None, "") else moment + pd.Timedelta(days=float(far_days)))
                notes += live["notes"]
                if live["premiums"]:
                    days = live["days"] if live["days"] is not None else days
                    far_days = live["far_days"] if live["far_days"] is not None else far_days
                    result = work(iv_pct, days, far_days, live["premiums"], live["labels"])
                    implied = [g["iv_pct"] for g in result["legs"] if g.get("iv_implied")]
                    if implied and payload.get("iv_pct") in (None, ""):
                        iv_pct = sum(implied) / len(implied)
                        sources = [x for x in sources if not x.startswith("volatility:")]
                        sources.append("volatility: average implied volatility of the legs from Kite")
                        result = work(iv_pct, days, far_days, live["premiums"], live["labels"])
                    sources.append(live["source"])
        result["sources"] = sources
        result["notes"] = notes + result["notes"]
        return result

    @app.post("/api/screens/run")
    def run_screen(payload: dict):
        from . import screens as screens_module
        payload = body_of(payload)
        key = str(payload.get("screen", ""))
        if key not in screens_module.SCREENS:
            raise ValueError(f"screen must be one of {', '.join(screens_module.SCREENS)}")
        universe = str(payload.get("universe") or screens_module.SCREENS[key]["universe"])
        entry = indices.entry(universe)
        if entry is None:
            raise ValueError("no such index")
        if not entry[3]:
            raise ValueError(f"{entry[1]} has no published stock list to screen. Pick a Nifty index.")
        rows = indices.constituents(universe)
        if not rows:
            raise ValueError(f"The stock list of {entry[1]} has not loaded yet. Open Indices, wait a moment and try again.")
        missing = [r["symbol"] for r in rows if not fundamentals.get(r["symbol"])]
        if missing:
            fundamentals.queue(missing)
        out = screens_module.run(key, rows, fundamentals.get)
        out.update(universe=universe, universe_name=entry[1],
                   note=(f"Figures for {out['missing']} of {out['total']} stocks are still loading (about 2 a second); run it "
                         "again in a minute for the full list." if out["missing"] else None))
        return out

    @app.post("/api/backtest/portfolio")
    def backtest_portfolio(payload: dict):
        from . import portfolio as pf
        payload = body_of(payload)
        key = str(payload.get("portfolio", "") or "")
        weights = payload.get("weights") or (pf.PORTFOLIOS[key]["weights"] if key in pf.PORTFOLIOS else None)
        if not isinstance(weights, dict) or not weights:
            raise ValueError("Choose a portfolio or give weights")
        clean = {}
        for symbol, weight in weights.items():
            name = str(symbol).upper().strip()
            if name != pf.CASH:
                name = safe_ticker_component(name if name.endswith((".NS", ".BO")) else name + ".NS")
            if isinstance(weight, bool) or not isinstance(weight, (int, float)):
                raise ValueError("Weights must be numbers")
            if weight:
                clean[name] = float(weight)
        prices = {}
        for symbol in [s for s in clean if s != pf.CASH] + ["NIFTYBEES.NS"]:
            try:
                prices[symbol] = data.history(symbol, fetch=fetch)["Close"]
            except data.NoData as exc:
                if symbol in clean:
                    raise ValueError(f"No price history for {symbol}") from exc
        start = payload.get("start") and date_of(payload.get("start"), "Start date")
        end = payload.get("end") and date_of(payload.get("end"), "End date")
        result = pf.run_portfolio(prices, clean, capital=_number(payload, "capital", 1_000_000),
                                  rebalance=str(payload.get("rebalance", "Q")), cash_rate_pct=_number(payload, "cash_rate_pct", 6.5),
                                  cost_pct=_number(payload, "cost_pct", 0.1), start=start, end=end)
        result["strategy"]["name"] = pf.PORTFOLIOS[key]["name"] if key in pf.PORTFOLIOS and not payload.get("weights") else "Custom portfolio"
        result.update(interval="1d", symbol="Portfolio", benchmark_name=None, benchmark_note=None, buy_hold_name="Nifty 50 (NIFTYBEES)",
                      data_note=f"Daily ETF prices from Yahoo. Rebalanced {pf.FREQUENCIES[result['params']['rebalance']]}; "
                                f"cash earns {_number(payload, 'cash_rate_pct', 6.5):g}% a year. Buy & hold is NIFTYBEES alone."
                                + (f" Price errors corrected: {', '.join(result['splits'])}." if result.get("splits") else ""))
        return result

    # -- journal and tax export ---------------------------------------------------

    def _export_rows(kind: str, mode: str, fy: str):
        from . import taxreport as tax
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if kind == "journal":
            return [r for r in reversed(desk.journal_rows(100000)) if r.get("mode") == mode], tax.JOURNAL_FIELDS
        if kind == "fills":
            return [f for f in desk.ledger.fills() if f.get("mode") == mode], tax.FILL_FIELDS
        if kind == "pnl":
            return desk.ledger.closed_trades(mode), tax.PNL_FIELDS
        if kind == "tax-lots":
            return [lot for lot in tax.tax_lots(desk.ledger.fills(), mode) if not fy or lot["fy"] == fy], tax.LOT_FIELDS
        raise HTTPException(404, "unknown export")

    def _export_name(kind: str, mode: str, fy: str, ext: str) -> str:
        return f"tradesetu-{kind}-{mode}{'-FY' + fy if fy else ''}.{ext}"

    @app.get("/api/export/{kind}.csv")
    def export_csv(kind: str, mode: str = "live", fy: str = ""):
        from . import taxreport as tax
        rows, fields = _export_rows(kind, mode, fy)
        return Response(tax.to_csv(rows, fields), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="{_export_name(kind, mode, fy, "csv")}"'})

    @app.get("/api/export/{kind}.xlsx")
    def export_xlsx(kind: str, mode: str = "live", fy: str = ""):
        rows, _ = _export_rows(kind, mode, fy)
        from . import xlsxexport
        data = xlsxexport.workbook(kind, rows, mode, fy=fy)
        return Response(data, media_type=XLSX_TYPE,
                        headers={"Content-Disposition": f'attachment; filename="{_export_name(kind, mode, fy, "xlsx")}"'})

    # -- backtest workbook and file hand-off -------------------------------------------------------------

    @app.post("/api/backtest/export.xlsx")
    def backtest_export(payload: dict):
        """The result on the Backtest page as an Excel workbook, with charts. The page sends back the result it
        shows (and its "Can you trust this result?" lines), so the file matches the screen exactly."""
        from . import btexport
        payload = body_of(payload)
        result = payload.get("result")
        checks = payload.get("checks") if isinstance(payload.get("checks"), list) else []
        notes = payload.get("notes") if isinstance(payload.get("notes"), list) else []
        data_ = btexport.workbook(result, checks=checks, notes=notes, app_name=APP_NAME)
        return Response(data_, media_type=XLSX_TYPE,
                        headers={"Content-Disposition": f'attachment; filename="{btexport.filename(result, APP_NAME)}"'})

    handoffs: dict[str, tuple[float, str, str, bytes]] = {}

    @app.post("/api/download/handoff")
    async def download_handoff(request: Request):
        """A file the phone app has fetched, kept for a few minutes behind a one-off link. The app's WebView cannot
        save files, so the app opens that link in the phone's browser, which saves it to Downloads."""
        body = await request.body()
        if not body or len(body) > HANDOFF_MAX_BYTES:
            raise ValueError("Nothing to save, or the file is too large (25 MB at most).")
        name = re.sub(r"[^A-Za-z0-9._ -]+", "-", request.headers.get("x-file-name") or "download").strip(" .-")[:150]
        ctype = (request.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
        if not re.fullmatch(r"[A-Za-z0-9.+-]+/[A-Za-z0-9.+-]+", ctype):
            ctype = "application/octet-stream"
        now = datetime.now().timestamp()
        for key in [k for k, v in handoffs.items() if v[0] < now]:
            handoffs.pop(key, None)
        while len(handoffs) >= HANDOFF_MAX_FILES:
            handoffs.pop(next(iter(handoffs)))
        token = secrets.token_urlsafe(32)
        handoffs[token] = (now + HANDOFF_SECONDS, name or "download", ctype, body)
        return {"url": f"/dl/{token}", "expires_in": HANDOFF_SECONDS}

    @app.get("/dl/{token}")
    def download_handed(token: str):
        item = handoffs.get(token)
        if not item or item[0] < datetime.now().timestamp():
            handoffs.pop(token, None)
            return HTMLResponse("<p>This download link has expired. Press the download button in the app again.</p>",
                                status_code=404)
        _, name, ctype, body = item
        return Response(body, media_type=ctype, headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/api/tax")
    def tax_summary(mode: str = "live"):
        from . import taxreport as tax
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        lots = tax.tax_lots(desk.ledger.fills(), mode)
        return {"mode": mode, "years": tax.summary(lots), "lots": lots[-300:][::-1],
                "rates": {"stcg": tax.STCG_RATE, "ltcg": tax.LTCG_RATE, "ltcg_exemption": tax.LTCG_EXEMPTION}}

    @app.get("/api/order/journal")
    def order_journal():
        return {"orders": desk.journal_rows()}

    # -- trade journal and your results ------------------------------------------

    def journal_trades(mode: str) -> list[dict]:
        report = desk.pnl(mode)
        closed, open_rows = desk.ledger.closed_trades(mode), report["open"]
        linked = notes.attach([{"key": r["key"], "mode": mode, "source": r["source"], "exchange": r["exchange"],
                                "symbol": r["symbol"], "opened_at": r["opened_at"]} for r in [*closed, *open_rows]])
        return peaks.attach(results.positions(closed, open_rows, linked))

    @app.get("/api/journal")
    def journal(mode: str = "paper"):
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if mode == "live":
            desk.sync_live_fills()
        trades = journal_trades(mode)
        other = "live" if mode == "paper" else "paper"
        return {"mode": mode, "trades": trades[:500], "pending": notes.pending(mode)[-50:],
                "results": results.report(trades),
                "other": {"mode": other, **results.summarize([t for t in journal_trades(other) if t["status"] == "Closed"])},
                "setups": notes.setups(), "mistakes": list(MISTAKES)}

    @app.post("/api/journal")
    def journal_add(payload: dict):
        return notes.add(body_of(payload))

    @app.put("/api/journal/{note_id}")
    def journal_update(note_id: str, payload: dict):
        try:
            return notes.update(note_id, body_of(payload))
        except KeyError:
            raise HTTPException(404, "no such note") from None

    @app.delete("/api/journal/{note_id}")
    def journal_delete(note_id: str):
        try:
            notes.delete(note_id)
        except KeyError:
            raise HTTPException(404, "no such note") from None
        return {"ok": True}

    @app.get("/api/quote")
    def quote(symbol: str):
        canonical = symbol_of(symbol)
        instrument = kite_instrument(canonical)
        if not instrument:
            raise ValueError(f"{canonical} is not an NSE/BSE equity")
        price, source = desk.last_price(f"{instrument[0]}:{instrument[1]}")
        return {"symbol": canonical, "exchange": instrument[0], "tradingsymbol": instrument[1],
                "last_price": price, "source": source}

    return app


_market_data_seen: dict = {}


def _market_data(kite: KiteMCP) -> dict:
    """Whether this Kite key serves candles (the paid Connect plan does; the free Personal API does not)."""
    import time as clock

    seen = _market_data_seen.get(id(kite))
    if seen and clock.time() - seen[0] < 300 and seen[1]["ok"]:
        return seen[1]
    try:
        now = market.now_ist().replace(tzinfo=None)
        from datetime import timedelta
        rows = kite.candles("NSE:RELIANCE", "1d", now - timedelta(days=10), now)
        status = kite.status()
        label = "Kite" if status.get("broker", "kite") == "kite" else status.get("broker_label")
        result = {"ok": bool(rows), "detail": f"{label} market data is working: charts, live prices and long intraday "
                                              "backtests use it." if rows else f"{label} returned no candles."}
    except KiteError as exc:
        if exc.needs_login:
            raise
        status = kite.status()
        label = "Kite" if status.get("broker", "kite") == "kite" else status.get("broker_label")
        plan = ("the paid Kite Connect plan on the API key this desk was installed with" if label == "Kite"
                else f"market data enabled on your {label} API plan")
        result = {"ok": False, "detail": f"{label} did not serve candles: {str(exc)[:200]}. Market data needs {plan}."}
    _market_data_seen[id(kite)] = (clock.time(), result)
    return result


NOTES_PDF = "trading-notes.pdf"


def _render_markdown(source: str) -> str:
    """Markdown to HTML with an id on every heading, the way the manual's links expect."""
    from markdown_it import MarkdownIt

    body = MarkdownIt("commonmark").enable("table").render(source)

    def anchor(match):
        text = html.unescape(re.sub(r"<[^>]+>", "", match.group(2)))       # "F&amp;O" slugs as "fo", as links expect
        slug = re.sub(r"[^a-z0-9 -]", "", text.lower()).strip().replace(" ", "-")
        return f'<h{match.group(1)} id="{slug}">{match.group(2)}</h{match.group(1)}>'

    return re.sub(r"<h([1-4])>(.*?)</h\1>", anchor, body)


def _doc_page(title: str, bar: str, body: str, static: str = "/static", theme: str = "") -> str:
    head = (f'<script src="{static}/theme.js"></script>' if not theme else "")
    return ('<!doctype html><html lang="en"' + (f' data-theme="{theme}"' if theme else "") + '><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{title}</title>{head}'
            f'<link rel="stylesheet" href="{static}/app.css"></head><body class="manual-page">'
            f'<div class="manual-bar">{bar}</div>'
            f'<main class="manual">{body}</main></body></html>')


def _manual_page() -> str:
    """The user manual as a page of the app."""
    source = (STATIC / "manual" / "USER_MANUAL.md").read_text(encoding="utf-8")
    body = _render_markdown(source).replace('src="img/', 'src="/static/manual/img/')
    return _doc_page("User manual · TradeSetu",
                     '<a href="/">← Back to the desk</a><span>TradeSetu user manual</span>', body)


def notes_page(for_print: bool = False) -> str:
    """The trading notes as a page of the app, or (``for_print``) as a standalone page for the PDF."""
    source = (STATIC / "notes" / "LEARNING_NOTES.md").read_text(encoding="utf-8")
    body = _render_markdown(source)
    static = STATIC.as_uri() if for_print else "/static"
    body = body.replace('src="img/', f'src="{static}/notes/img/').replace('src="manual/', f'src="{static}/manual/img/')
    if for_print:
        # Links into the app's manual cannot open from a PDF: keep the words, drop the link.
        body = re.sub(r'<a href="/manual[^"]*">(.*?)</a>', r"\1 (in the User manual)", body)
        return _doc_page("Trading Notes · TradeSetu", "<span>TradeSetu trading notes</span>", body, static,
                         theme="light")
    return _doc_page("Trading notes · TradeSetu",
                     '<a href="/">← Back to the desk</a><span>TradeSetu trading notes</span>'
                     '<a class="manual-pdf" href="/notes.pdf" download>Download PDF</a>', body)


# Free or low-cost OpenAI-compatible LLM services, as starting points for Settings.
# Free tiers have rate limits; one analysis makes dozens of calls.
LLM_PRESETS = [
    # Paid providers: models from the catalogue; pick another in the model boxes.
    {"name": "OpenAI (paid)", "provider": "openai", "backend_url": "", "deep": "gpt-6-sol", "quick": "gpt-6-luna",
     "key_from": "platform.openai.com/api-keys", "paid": True},
    {"name": "Anthropic Claude (paid)", "provider": "anthropic", "backend_url": "", "deep": "claude-opus-5-5",
     "quick": "claude-sonnet-5", "key_from": "console.anthropic.com/settings/keys", "paid": True},
    {"name": "Google Gemini (paid)", "provider": "google", "backend_url": "", "deep": "gemini-3.1-pro-preview",
     "quick": "gemini-3.8-flash", "key_from": "aistudio.google.com/apikey", "paid": True},
    {"name": "xAI Grok (paid)", "provider": "xai", "backend_url": "", "deep": "grok-4.6", "quick": "grok-4.6",
     "key_from": "console.x.ai", "paid": True},
    {"name": "DeepSeek (paid)", "provider": "deepseek", "backend_url": "", "deep": "deepseek-v4-pro", "quick": "deepseek-flash",
     "key_from": "platform.deepseek.com/api_keys", "paid": True},
    {"name": "Mistral (paid)", "provider": "mistral", "backend_url": "", "deep": "", "quick": "",
     "key_from": "console.mistral.ai/api-keys; enter the model names it lists", "paid": True},
    {"name": "OpenRouter (paid, any model)", "provider": "openrouter", "backend_url": "", "deep": "", "quick": "",
     "key_from": "openrouter.ai/keys; enter model ids such as anthropic/claude-opus-5-5", "paid": True},
    # Free tiers
    {"name": "Groq (free tier)", "provider": "groq", "backend_url": "", "deep": "llama-3.3-70b-versatile",
     "quick": "llama-3.1-8b-instant", "key_from": "console.groq.com/keys"},
    {"name": "Google Gemini (free tier)", "provider": "google", "backend_url": "", "deep": "gemini-3.8-flash",
     "quick": "gemini-3.5-flash-lite", "key_from": "aistudio.google.com/apikey"},
    {"name": "OpenRouter (free models)", "provider": "openrouter", "backend_url": "",
     "deep": "meta-llama/llama-3.3-70b-instruct:free", "quick": "meta-llama/llama-3.3-70b-instruct:free",
     "key_from": "openrouter.ai/keys"},
    {"name": "Any OpenAI-compatible service (FreeLLM, LM Studio, vLLM, a relay…)", "provider": "openai_compatible",
     "backend_url": "", "deep": "", "quick": "",
     "key_from": "the service's dashboard; set its base URL as the Endpoint and its model name in both model boxes"},
    {"name": "Ollama on this server (no key)", "provider": "ollama", "backend_url": "", "deep": "llama3.1",
     "quick": "llama3.1", "key_from": "none needed"},
]


def datetime_on(day: str):
    """Noon IST on a YYYY-MM-DD day."""
    from datetime import datetime as _dt
    return _dt.strptime(day, "%Y-%m-%d").replace(hour=12, tzinfo=market.IST)


def pd_ts(day: str):
    import pandas as pd
    return pd.Timestamp(day) + (pd.Timedelta(hours=23, minutes=59) if len(day) == 10 else pd.Timedelta(0))


_PERIOD_WORDS = {"7d": "the last 7 days", "60d": "the last 60 days", "730d": "the last 2 years"}


def _kite_range_raw(kite, instrument: str, interval: str, start: str, end: str, continuous: bool = False):
    """Kite candles for an index or contract by its Kite name, over a date range."""
    return data.kite_range(kite, instrument, interval, start, end, raw=True, continuous=continuous)


def _llm_models() -> dict:
    """The model picker for every provider: the catalogue's quick and deep models."""
    from tradingagents.llm_clients.model_catalog import MODEL_OPTIONS

    out = {}
    for provider in PROVIDER_API_KEY_ENV:
        modes = MODEL_OPTIONS.get(provider)
        if not modes:
            continue
        out[provider] = {mode: [{"label": label, "id": model} for label, model in modes.get(mode, []) if model != "custom"]
                         for mode in ("quick", "deep")}
    return out


def _candle_patterns() -> list:
    from .candles import PATTERNS
    return [{"key": k, "name": v[0], "signal": v[1], "candles": v[2], "about": v[3]} for k, v in PATTERNS.items()]


def _structures() -> list:
    from .optstrat import STRUCTURES
    return [{"key": k, "name": v["name"], "about": v["about"], "markets": list(v["markets"]), "bias": v.get("bias"),
             "w": v.get("w", 4), "min_k": v.get("min_k", 0), "legs": [list(leg) for leg in v["legs"]],
             "uses_k": any("k" in leg[2] for leg in v["legs"]), "uses_w": any("w" in leg[2] for leg in v["legs"]),
             "calendar": any(len(leg) > 4 and leg[4] == "far" for leg in v["legs"])} for k, v in STRUCTURES.items()] + [
        {"key": "custom", "name": "Your own strategy (build the legs)", "custom": True, "markets": [], "bias": None, "w": 4,
         "min_k": 0, "legs": [], "uses_k": False, "uses_w": False, "calendar": False,
         "about": "Pick every leg yourself: Buy or Sell, Call, Put or Futures, the strike counted from the money, lots "
                  "and expiry."}]


def _screens() -> list:
    from .screens import SCREENS
    return [{"key": k, "name": v["name"], "about": v["about"], "legend": v["legend"], "universe": v["universe"]}
            for k, v in SCREENS.items()]


def _portfolios() -> list:
    from .portfolio import PORTFOLIOS
    return [{"key": k, "name": v["name"], "about": v["about"], "legend": v["legend"], "markets": list(v["markets"]),
             "weights": v["weights"]} for k, v in PORTFOLIOS.items()]


def _kite_premiums(kite: KiteMCP, derivatives, underlying: str, legs: list, contracts: list,
                   near, far) -> dict:
    """Each leg's premium from Kite for the listed contract nearest the near (and far) expiry.

    Returns premiums (one per leg, ``None`` where Kite had no price), labels, the days to the listed expiries and
    plain notes. During the session the price is the last trade; after it, the last trade of the latest session,
    or the previous close when there was none."""
    import pandas as pd

    from . import options as opt
    out = {"premiums": None, "labels": None, "days": None, "far_days": None, "notes": [], "source": ""}
    if not kite.connected or "get_quotes" not in kite.tools:
        out["notes"].append("Premiums are modelled. Log in to Kite (Trade, Connect) to fill them with real prices.")
        return out
    moment = market.now_ist()
    try:
        today = moment.strftime("%Y-%m-%d")
        listed = sorted({c["expiry"] for c in contracts if c["expiry"] >= today})

        def nearest(target):
            if target is None or not listed:
                return None
            day = pd.Timestamp(target).normalize()
            best = min(listed, key=lambda d: abs(pd.Timestamp(d) - day))
            return best if abs(pd.Timestamp(best) - day) <= pd.Timedelta(days=3) else None

        dates = {"near": nearest(near), "far": nearest(far)}
        by_key = {(c["expiry"], float(c["strike"]), c["type"]): c for c in contracts}
        future = next((c for c in derivatives.futures() if c["underlying"] == underlying), None)
        names = []
        for g in legs:
            if g["type"] == "FUT":
                c = future
            else:
                expiry = dates["far" if g["expiry"] == "far" else "near"]
                c = by_key.get((expiry, float(g["strike"] or 0), g["type"])) if expiry else None
            names.append(f"{c['exchange']}:{c['tradingsymbol']}" if c else None)
        wanted = sorted({n for n in names if n})
        quotes = kite.quotes(wanted) if wanted else {}
    except KiteError as exc:
        out["notes"].append(f"Premiums are modelled. Kite did not answer ({exc}). Log in to Kite again to fill real prices.")
        return out
    session = market.session_status(moment)
    premiums, labels, missing, closes = [], [], 0, 0
    for name in names:
        quote = (quotes.get(name) or {}) if name else {}
        last = quote.get("last_price")
        previous = (quote.get("ohlc") or {}).get("close")
        if isinstance(last, (int, float)) and last > 0:
            premiums.append(round(float(last), 2))
            labels.append("Kite live" if session["open"] else "Kite last price")
        elif isinstance(previous, (int, float)) and previous > 0:
            premiums.append(round(float(previous), 2))
            labels.append("Kite previous close")
            closes += 1
        else:
            premiums.append(None)
            labels.append(None)
            missing += 1
    if all(p is None for p in premiums):
        out["notes"].append("Premiums are modelled. Kite had no price for these strikes"
                            + ("" if dates["near"] else " (no listed expiry near the one asked for)") + ".")
        return out
    out.update(premiums=premiums, labels=labels)
    for key, which in (("days", "near"), ("far_days", "far")):
        if dates[which]:
            out[key] = opt.years_to(dates[which], moment) * 365
    if not session["open"]:
        out["notes"].append("Market closed: premiums are Kite's last traded prices, from the latest session.")
    if closes:
        out["notes"].append(f"{closes} leg{'s' if closes > 1 else ''} had no trade today; the previous close is used.")
    if missing:
        out["notes"].append(f"Kite had no price for {missing} leg{'s' if missing > 1 else ''}; "
                            f"{'they are' if missing > 1 else 'it is'} modelled.")
    out["source"] = "premiums from Kite for the " + " and ".join(
        pd.Timestamp(d).strftime("%a %d %b") for d in dict.fromkeys(v for v in dates.values() if v)) + " expiry"
    return out


def _scan_workers() -> int:
    """Worker processes for the AI scan: DESK_SCAN_WORKERS, else all cores but one (at most 4), so the app
    itself stays responsive while it runs."""
    raw = os.environ.get("DESK_SCAN_WORKERS", "")
    if raw.isdigit() and int(raw) > 0:
        return min(int(raw), 16)
    return max(1, min(4, (os.cpu_count() or 2) - 1))


def _number(payload: dict, key: str, default: float) -> float:
    value = payload.get(key, default)
    if value is None or value == "":
        return float(default)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be a number")
    return float(value)


def _portfolio(desk: Desk, kite: KiteMCP) -> PortfolioContext | None:
    """The book the agents should reason about: Kite's when connected, else paper."""
    from tradingagents.dataflows.symbols import from_kite

    if kite.connected:
        try:
            positions = [Position(ticker=from_kite(row.get("exchange", "NSE"), row["tradingsymbol"]),
                                  quantity=float(row.get("quantity", 0)),
                                  average_price=row.get("average_price"))
                         for row in kite.holdings() if row.get("tradingsymbol") and row.get("quantity")]
            return PortfolioContext(cash=desk._live_cash(), currency="INR", positions=positions)
        except (KiteError, ValueError, TypeError):
            logger.warning("could not read the Kite book; falling back to the paper book")
    book = desk.paper.snapshot()
    positions = []
    for instrument, held in book["positions"].items():
        exchange, symbol = instrument.split(":", 1)
        positions.append(Position(ticker=from_kite(exchange, symbol), quantity=held["quantity"],
                                  average_price=held["average_price"]))
    return PortfolioContext(cash=book["cash"], currency="INR", positions=positions)


