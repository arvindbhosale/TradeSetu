"""Start the desk: the local server, and a native window onto it."""

from __future__ import annotations

import argparse
import contextlib
import logging
import os
import socket
import threading
import time
import webbrowser

import uvicorn

from . import APP_NAME
from .server import create_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="tradesetu", description=f"{APP_NAME}: NSE/BSE trading desk")
    parser.add_argument("--port", type=int, default=0, help="local port (default: a free one)")
    parser.add_argument("--browser", action="store_true", help="open in the web browser, not a window")
    parser.add_argument("--no-window", action="store_true", help="serve only; print the address")
    parser.add_argument("--serve", action="store_true",
                        help="hosted mode for a server: sign-in required, listens on all interfaces")
    parser.add_argument("--host", default=None, help="address to listen on in hosted mode (default 0.0.0.0)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    if args.serve:
        serve_hosted(args.host or "0.0.0.0", args.port or int(os.environ.get("PORT", "8000")))
        return

    port = args.port or _free_port()
    app = create_app()
    app.state.algos.start()
    app.state.sectors.start()
    _start_indices(app)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    url = f"http://127.0.0.1:{port}/"

    if args.no_window:
        print(f"{APP_NAME} is serving at {url}")
        server.run()
        return

    thread = threading.Thread(target=server.run, name="desk-server", daemon=True)
    thread.start()
    while not server.started and thread.is_alive():
        time.sleep(0.05)

    window = None
    if not args.browser:
        try:
            import webview

            window = webview.create_window(APP_NAME, url, width=1360, height=880, min_size=(1000, 680))
            webview.start()      # blocks until the window closes
        except Exception as exc:  # noqa: BLE001 - no GUI toolkit: the browser still works
            logging.getLogger(__name__).warning("No native window (%s); opening the browser instead.", exc)
            window = None
    if window is None:
        webbrowser.open(url)
        print(f"{APP_NAME} is open at {url}  (Ctrl+C to quit)")
        try:
            while thread.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
    app.state.kite.disconnect()
    server.should_exit = True
    thread.join(timeout=5)


def _start_indices(app) -> None:
    """Keep the index board fresh: constituent lists daily, levels and live quotes through the day."""
    indices = app.state.indices

    def loop():
        while True:
            try:
                indices.tick()
                wanted = {r["symbol"] for i in ("nifty50", "niftynext50", "niftybank", "niftyit", "niftyfinance", "niftyauto",
                                                "niftypharma", "niftyfmcg", "niftymetal", "niftyrealty", "niftyenergy")
                          for r in indices.constituents(i)}
                tools = app.state.tools
                with contextlib.suppress(Exception):
                    tools.tick()
                broad = {r["symbol"] for r in indices.constituents("nifty500")} | wanted | set(app.state.sectors.symbols())
                wanted |= set(tools.result_symbols(broad))        # stocks that reported, for Results Drift
                app.state.sectors.extra = sorted(wanted)
            except Exception:  # noqa: BLE001 - try again next round
                logging.getLogger(__name__).exception("index refresh failed")
            time.sleep(60)

    threading.Thread(target=loop, name="desk-indices", daemon=True).start()


def serve_hosted(host: str, port: int) -> None:
    """Run the desk for a server deployment (Docker, Dokploy): sign-in required."""
    from .auth import Auth, AuthNotConfigured
    from .settings import DEFAULT_MCP_CONFIG, Settings

    try:
        auth = Auth.from_env()
    except AuthNotConfigured as exc:
        raise SystemExit(f"Refusing to start: {exc}") from None
    from .auth import TwoFactor
    from .settings import home

    auth.two_factor = TwoFactor(home() / "two_factor.json")

    settings = Settings()
    kite_url = (os.environ.get("DESK_KITE_MCP_URL") or "").strip()
    if kite_url and settings.get("mcp") == DEFAULT_MCP_CONFIG:
        # A server has no browser for mcp-remote's sign-in; use the Kite server
        # deployed beside the desk unless the user has set something else.
        settings.update({"mcp": {"mcpServers": {"kite": {"url": kite_url}}}, "mcp_server": "kite"})
    hosts = [h for h in (os.environ.get("DESK_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    app = create_app(settings=settings, auth=auth, allowed_hosts=hosts,
                     kite_proxy=kite_url.rsplit("/", 1)[0] if kite_url else None)
    app.state.algos.start()
    app.state.sectors.start()
    _start_indices(app)
    if kite_url:
        # Reconnect to the Kite server after a redeploy without waiting for a click.
        # Signing in to Kite itself is still the user's step, every morning.
        def connect_kite():
            time.sleep(5)
            try:
                app.state.kite.connect({"url": kite_url}, timeout=60)
            except Exception as exc:  # noqa: BLE001 - the Trade tab shows the reason and a Connect button
                logging.getLogger(__name__).warning("Kite server not reachable at start: %s", exc)

        threading.Thread(target=connect_kite, name="kite-connect", daemon=True).start()
    logging.getLogger(__name__).info("%s hosted on %s:%s, user %r", APP_NAME, host, port, auth.username)
    # Behind the panel's reverse proxy: trust its X-Forwarded-* for scheme and client address.
    uvicorn.run(app, host=host, port=port, log_level="info", proxy_headers=True, forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
