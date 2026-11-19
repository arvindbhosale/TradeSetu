"""The desk's local HTTP API and the page it serves.

Bound to 127.0.0.1 only. Every /api call must carry the session token the page
was served with, and name a local Host: a web page open in the same browser
cannot read the token, so it cannot place an order through this server, and a
DNS-rebinding page fails the Host check.
"""

from __future__ import annotations

import contextlib
import hmac
import logging
import os
import re
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
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
    market,
    mcx,
    risk,
)
from .algo import AlgoEngine, AlgoError, arm_phrase
from .auth import COOKIE, SESSION_SECONDS, Auth
from .backtest import BacktestParams, run_strategy
from .broker import Desk, OrderRejected
from .brokers import Switch, catalog as broker_catalog
from .custom import OPS as RULE_OPS, TIME_OPS as RULE_TIME_OPS, CustomStrategies
from .fno import BSE_INDICES, Derivatives, exchange_of
from .fnoalgo import FnoAlgoEngine, arm_phrase as fno_arm_phrase
from .fundamentals import LABELS as FUNDAMENTAL_LABELS, Fundamentals
from .indices import Indices
from .kite import KiteError, KiteMCP
from .markettools import MarketTools, finite
from .optimiser import OBJECTIVES, optimise
from .passkeys import PasskeyError, Passkeys, relying_party
from .scanner import Scanner
from .sectors import Sectors
from .settings import Settings, home
from .stockinfo import stock_view

logger = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_GRID_CELLS = 60


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
    library = CustomStrategies()
    algos = AlgoEngine(desk, fetch=fetch, library=library)
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
    tools = MarketTools(sectors, indices, fundamentals, fetch=fetch, get_json=offline_json,
                        download=(lambda url: (_ for _ in ()).throw(OSError("offline"))) if fetch is not None else None)
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

    algos.extra_ticks.append(lambda: desk.settle_paper_options(option_settlement_spot))
    fno_algos = FnoAlgoEngine(desk, derivatives, fetch=fetch, library=library)
    algos.extra_ticks.append(fno_algos.tick)
    algos.extra_live.append(fno_algos.live_names)
    desk.algo_instruments = fno_algos.held_instruments
    algos.extra_ticks.append(desk.close_paper_mcx)
    hosted = auth is not None
    passkeys = Passkeys(home() / "passkeys.json", auth.username) if hosted else None
    local_hosts = ("127.0.0.1", "localhost", "::1", "testserver")
    allowed = {h.strip().lower() for h in (allowed_hosts or []) if h.strip()}

    app = FastAPI(title=APP_NAME, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.token, app.state.settings, app.state.kite, app.state.desk = token, settings, kite, desk
    app.state.algos, app.state.auth, app.state.scanner = algos, auth, scanner
    app.state.sectors, app.state.indices, app.state.fundamentals = sectors, indices, fundamentals
    app.state.derivatives = derivatives
    app.state.tools = tools
    app.state.fno_algos = fno_algos

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
            public = path in ("/login", "/api/login", "/api/passkey/options", "/api/passkey/login", "/sw.js",
                              "/broker/callback") \
                or path.startswith(("/static/", "/kite/"))
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
            "connect-src 'self'; manifest-src 'self'; worker-src 'self'; frame-ancestors 'none'; "
            "base-uri 'none'; form-action 'none'")
        return response

    @app.exception_handler(OrderRejected)
    async def rejected(_, exc: OrderRejected):
        return JSONResponse({"error": str(exc), "errors": exc.reasons}, status_code=422)

    @app.exception_handler(KiteError)
    async def kite_failed(_, exc: KiteError):
        return JSONResponse({"error": str(exc), "needs_login": exc.needs_login}, status_code=502)

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
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        return html.replace("__DESK_TOKEN__", request.state.session["csrf"] if hosted else token)

    @app.get("/manual", response_class=HTMLResponse)
    def manual():
        return _manual_page()

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
            "indicators": indicators.describe(), "rule_ops": RULE_OPS, "rule_time_ops": RULE_TIME_OPS,
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
            intraday=interval != "1d", trade_start=str(payload.get("trade_start") or cfg["trade_start"]),
            trade_end=str(payload.get("trade_end") or cfg["trade_end"]),
            square_off=str(payload.get("square_off") or cfg["square_off"]),
        )
        source_note = None
        bars = None
        if interval != "1d" and kite.connected:
            # Intraday candles from Kite reach back years; the free feed stops at days or weeks.
            try:
                bars = data.kite_range(kite, canonical, interval, start, end)
                source_note = (f"Candles from Kite: {len(bars):,} {data.INTERVALS[interval]['label']} candles, "
                               f"{bars.index[0]:%d %b %Y} to {bars.index[-1]:%d %b %Y}.")
            except (KiteError, data.NoData) as exc:
                reason = "Kite needs a login" if getattr(exc, "needs_login", False) else str(exc)[:160]
                source_note = f"Kite could not serve these candles ({reason}); using the free feed's shorter history."
        if bars is None:
            bars = data.bars_for(canonical, interval, start, end, fetch=fetch)
            if interval != "1d" and source_note is None:
                source_note = ("Candles from the free feed, which keeps only a short intraday history. Log in to "
                               "Kite on the Trade tab to test over months or years.")
        index_symbol = "^BSESN" if canonical.endswith(".BO") else "^NSEI"
        benchmark, benchmark_note = None, None
        if canonical != index_symbol:
            try:
                benchmark = data.history(index_symbol, start, end, fetch=fetch)["Close"]
            except data.NoData as exc:
                benchmark_note = f"Benchmark unavailable: {exc}"
        result = run_strategy(bars, strategy, payload.get("strategy_params"), params, benchmark)
        result["symbol"], result["interval"] = canonical, interval
        result["benchmark_name"] = {"^NSEI": "Nifty 50", "^BSESN": "Sensex"}[index_symbol] if result["benchmark"] else None
        result["benchmark_note"] = benchmark_note
        result["data_note"] = bars.attrs.get("note") or source_note
        return result

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
        return {"jobs": runner.list()}

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str):
        job = runner.get(job_id)
        if not job:
            raise HTTPException(404, "no such job")
        return job.view()

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
        servers = settings.get("mcp")["mcpServers"]
        name = settings.get("mcp_server")
        if name not in servers:
            raise ValueError(f'No MCP server named "{name}" in Settings. Found: {", ".join(servers) or "none"}')
        return kite.connect(servers[name])

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
        return desk.preview(body_of(payload.get("order")), str(payload.get("mode", "paper")))

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
                                trade_start=cfg["trade_start"], trade_end=cfg["trade_end"], square_off=cfg["square_off"])
        params.validate()
        from .optimiser import grid
        grid(strategy, ranges)                       # refuse a bad grid now, not inside the job
        objective = str(payload.get("objective", "sharpe"))
        folds, train_pct = int(_number(payload, "folds", 4)), _number(payload, "train_pct", 70)
        min_trades = int(_number(payload, "min_trades", 3))

        def work(job):
            bars = None
            if interval != "1d" and kite.connected:
                with contextlib.suppress(KiteError, data.NoData):
                    bars = data.kite_range(kite, canonical, interval, start, end)
            if bars is None:
                bars = data.bars_for(canonical, interval, start, end, fetch=fetch)
            job.log(f"{len(bars):,} candles, {bars.index[0]:%d %b %Y} to {bars.index[-1]:%d %b %Y}")
            result = optimise(bars, strategy, ranges, params, objective, folds, train_pct, min_trades, progress=job.log)
            result.update(symbol=canonical, strategy=strategy.name, interval=interval)
            return result

        job = runner.submit("optimise", f"Optimise {strategy.name} on {canonical}", work)
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
        result = opt.run_options(
            underlying, bars, positions, vix, capital=_number(payload, "capital", cfg["paper_capital"]),
            lots=int(_number(payload, "lots", 1)), lot_size=lot, side=str(payload.get("side", "calls")),
            moneyness=int(_number(payload, "moneyness", 0)), min_days=_number(payload, "min_days", 2),
            expiry_weekday=int(_number(payload, "expiry_weekday", 3 if underlying in BSE_INDICES else 1)),
            stop_pct=_number(payload, "stop_pct", 0), target_pct=_number(payload, "target_pct", 0),
            iv_scale=_number(payload, "iv_scale", 1.0), slippage_pct=_number(payload, "slippage_pct", 1.0),
            intraday=interval != "1d", square_off=cfg["square_off"], strategy_name=strategy.name)
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
        result = fut.run_futures(
            bars, signal, underlying=underlying, lot_size=lot, lots=int(_number(payload, "lots", 1)),
            capital=_number(payload, "capital", cfg["paper_capital"]), direction=str(payload.get("direction", "long")),
            margin_pct=_number(payload, "margin_pct", 12.0), stop_pct=_number(payload, "stop_pct", 0),
            target_pct=_number(payload, "target_pct", 0), slippage_pct=_number(payload, "slippage_pct", 0.02),
            intraday=interval != "1d", trade_start=cfg["trade_start"], trade_end=cfg["trade_end"], square_off=cfg["square_off"],
            continuous=continuous, carry_pct=_number(payload, "carry_pct", 7.0), holidays=frozenset(market.HOLIDAYS),
            exchange=exchange, strategy_name=strategy.name)
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
        result = optstrat.run_structure(
            underlying, bars, vix, structure=str(payload.get("structure", "")), mode=mode, lot_size=lot,
            lots=int(_number(payload, "lots", 1)), capital=_number(payload, "capital", cfg["paper_capital"]),
            k=int(_number(payload, "k", 2)), w=int(_number(payload, "w", 4)), expiry_kind=str(payload.get("expiry_kind", "weekly")),
            min_days=_number(payload, "min_days", 0 if mode == "intraday" else 1), weekdays=tuple(weekdays),
            entry_time=times[0], exit_time=times[1], exit_days_before=int(_number(payload, "exit_days_before", 0)),
            leg_sl_pct=_number(payload, "leg_sl_pct", 0), stop_pct=_number(payload, "stop_pct", 0),
            target_pct=_number(payload, "target_pct", 0), signal=signal, margin_pct=_number(payload, "margin_pct", 12),
            iv_scale=_number(payload, "iv_scale", 1.0), skew=_number(payload, "skew", 0.0),
            slippage_pct=_number(payload, "slippage_pct", 1.0), holidays=frozenset(market.HOLIDAYS),
            leg_exits=risk.from_payload(payload.get("leg_exits"), "Each leg",
                                        legacy={"stop_loss": _number(payload, "leg_sl_pct", 0)}),
            leg_scope=str(payload.get("leg_scope") or "sold"))
        m = result["metrics"]
        notes = [f"Index candles from {source}. Every premium is simulated (Black-Scholes on India VIX × "
                 f"{_number(payload, 'iv_scale', 1.0):g}); Kite keeps no candles for expired contracts."]
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
        if structure not in optstrat.STRUCTURES:
            raise ValueError(f"structure must be one of {', '.join(optstrat.STRUCTURES)}")
        sources = []
        spot = _number(payload, "spot", 0)
        if spot <= 0 and kite.connected:
            with contextlib.suppress(KiteError):
                spot = float(kite.ltp([opt.SPOT[underlying]]).get(opt.SPOT[underlying]) or 0)
                if spot > 0:
                    sources.append("index price from Kite")
        if spot <= 0 and underlying in opt.YAHOO_SPOT:
            with contextlib.suppress(data.NoData, KeyError, IndexError):
                spot = float(data.history(opt.YAHOO_SPOT[underlying], fetch=fetch)["Close"].iloc[-1])
                sources.append("index price: last close from Yahoo")
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
        near = optstrat.expiry_after(underlying, moment, expiry_kind, 0.0, holidays)
        if days in (None, ""):
            days = max((near - moment).total_seconds() / 86400, 0.0)
            sources.append(f"near expiry {near:%a %d %b}")
        if far_days in (None, "") and any(x["series"] == "far" for x in optstrat.leg_specs(structure)):
            base = moment + pd.Timedelta(days=float(_number(payload, "days", days)))
            far = optstrat.expiry_after(underlying, max(base, near) + pd.Timedelta(minutes=1), expiry_kind, 0.0, holidays)
            far_days = (far - moment).total_seconds() / 86400
            sources.append(f"far expiry {far:%a %d %b}")
        contracts = derivatives.contracts(underlying)
        lot = int(_number(payload, "lot_size", contracts[0]["lot_size"] if contracts else opt.LOT_DEFAULT.get(underlying, 50)))
        premiums = payload.get("premiums")
        result = optstrat.analyse(
            underlying, structure, spot=spot, iv=iv_pct / 100, days=float(_number(payload, "days", days)),
            far_days=None if far_days in (None, "") else float(_number({"far_days": far_days}, "far_days", 0)),
            k=int(_number(payload, "k", 2)), w=int(_number(payload, "w", 4)), lots=int(_number(payload, "lots", 1)),
            lot_size=lot, margin_pct=_number(payload, "margin_pct", 12), premiums=premiums if premiums else None)
        result["sources"] = sources
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

    @app.get("/api/export/{kind}.csv")
    def export_csv(kind: str, mode: str = "live", fy: str = ""):
        from . import taxreport as tax
        if mode not in ("paper", "live"):
            raise ValueError("mode must be paper or live")
        if kind == "journal":
            rows = [r for r in reversed(desk.journal_rows(100000)) if r.get("mode") == mode]
            text = tax.to_csv(rows, tax.JOURNAL_FIELDS)
        elif kind == "fills":
            text = tax.to_csv([f for f in desk.ledger.fills() if f.get("mode") == mode], tax.FILL_FIELDS)
        elif kind == "pnl":
            text = tax.to_csv(desk.ledger.closed_trades(mode), tax.PNL_FIELDS)
        elif kind == "tax-lots":
            lots = [lot for lot in tax.tax_lots(desk.ledger.fills(), mode) if not fy or lot["fy"] == fy]
            text = tax.to_csv(lots, tax.LOT_FIELDS)
        else:
            raise HTTPException(404, "unknown export")
        name = f"tradesetu-{kind}-{mode}{'-FY' + fy if fy else ''}.csv"
        return Response(text, media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{name}"'})

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


def _manual_page() -> str:
    """The user manual as a page of the app."""
    import re

    from markdown_it import MarkdownIt

    source = (STATIC / "manual" / "USER_MANUAL.md").read_text(encoding="utf-8")
    body = MarkdownIt("commonmark").enable("table").render(source)
    body = body.replace('src="img/', 'src="/static/manual/img/')

    def anchor(match):
        text = re.sub(r"<[^>]+>", "", match.group(2))
        slug = re.sub(r"[^a-z0-9 -]", "", text.lower()).strip().replace(" ", "-")
        return f'<h{match.group(1)} id="{slug}">{match.group(2)}</h{match.group(1)}>'

    body = re.sub(r"<h([1-4])>(.*?)</h\1>", anchor, body)
    return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width, initial-scale=1">'
            '<title>User manual · TradeSetu</title><script src="/static/theme.js"></script>'
            '<link rel="stylesheet" href="/static/app.css"></head><body class="manual-page">'
            '<div class="manual-bar"><a href="/">← Back to the desk</a><span>TradeSetu user manual</span></div>'
            f'<main class="manual">{body}</main></body></html>')


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


def _kite_range_raw(kite, instrument: str, interval: str, start: str, end: str, continuous: bool = False):
    """Kite candles for an index or contract by its Kite name, over a date range."""
    import pandas as pd
    first, last = pd.Timestamp(start), pd.Timestamp(end) + pd.Timedelta(hours=23, minutes=59)
    if (last - first).days > data.KITE_MAX_DAYS[interval]:
        raise data.NoData(f"choose a range of at most {data.KITE_MAX_DAYS[interval]} days")
    frames, cursor = [], first
    while cursor <= last:
        stop = min(cursor + pd.Timedelta(days=data.KITE_CHUNK[interval]) - pd.Timedelta(seconds=1), last)
        rows = (kite.candles(instrument, interval, cursor.to_pydatetime(), stop.to_pydatetime(), continuous=True) if continuous
                else kite.candles(instrument, interval, cursor.to_pydatetime(), stop.to_pydatetime()))
        frames.append(data.kite_frame(rows))
        cursor = stop + pd.Timedelta(seconds=1)
    frame = pd.concat([f for f in frames if not f.empty]) if any(not f.empty for f in frames) else pd.DataFrame(columns=data.COLUMNS)
    return frame[~frame.index.duplicated(keep="last")].sort_index()


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


def _structures() -> list:
    from .optstrat import STRUCTURES
    return [{"key": k, "name": v["name"], "about": v["about"], "markets": list(v["markets"]), "bias": v.get("bias"),
             "w": v.get("w", 4), "min_k": v.get("min_k", 0), "legs": [list(leg) for leg in v["legs"]],
             "uses_k": any("k" in leg[2] for leg in v["legs"]), "uses_w": any("w" in leg[2] for leg in v["legs"]),
             "calendar": any(len(leg) > 4 and leg[4] == "far" for leg in v["legs"])} for k, v in STRUCTURES.items()]


def _screens() -> list:
    from .screens import SCREENS
    return [{"key": k, "name": v["name"], "about": v["about"], "legend": v["legend"], "universe": v["universe"]}
            for k, v in SCREENS.items()]


def _portfolios() -> list:
    from .portfolio import PORTFOLIOS
    return [{"key": k, "name": v["name"], "about": v["about"], "legend": v["legend"], "markets": list(v["markets"]),
             "weights": v["weights"]} for k, v in PORTFOLIOS.items()]


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


