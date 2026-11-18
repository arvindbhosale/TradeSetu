"""The brokers TradeSetu can trade through, and the switch that puts one of them behind the desk.

Zerodha Kite (through its MCP server, :mod:`desk.kite`) is the default and the tested path. The others talk
to each broker's own REST API and answer in Kite's shape (see :mod:`desk.brokers.base`), so the rest of the
desk is unchanged whichever broker is chosen.
"""

from __future__ import annotations

from ..kite import KiteError, KiteMCP
from .angelone import AngelOne
from .base import RestBroker
from .dhan import Dhan
from .fyers import Fyers
from .groww import Groww
from .upstox import Upstox

# "supported": tested end to end against a live account. "experimental": written against the broker's
# published API and SDK and covered by offline tests, not yet run against a live account.
BROKERS: dict[str, dict] = {
    "kite": {"label": "Zerodha Kite", "status": "supported", "cls": None,
             "notes": "Through Zerodha's Kite MCP server. Orders need a self-hosted kite-mcp-server with a Kite Connect key."},
    "upstox": {"label": "Upstox", "status": "experimental", "cls": Upstox,
               "notes": "Upstox API app (free). Signs in through Upstox's login page."},
    "dhan": {"label": "Dhan", "status": "experimental", "cls": Dhan,
             "notes": "DhanHQ API. Market data and candles need Dhan's Data API plan."},
    "angelone": {"label": "Angel One", "status": "experimental", "cls": AngelOne,
                 "notes": "SmartAPI (free). Signs in with client code, PIN and TOTP."},
    "fyers": {"label": "Fyers", "status": "experimental", "cls": Fyers,
              "notes": "Fyers API v3 app (free). Signs in through Fyers' login page."},
    "groww": {"label": "Groww", "status": "experimental", "cls": Groww,
              "notes": "Groww Trade API (paid subscription on the Groww account)."},
}


def catalog() -> list[dict]:
    """The brokers and the settings each needs, for the Settings page."""
    out = []
    for name, info in BROKERS.items():
        cls = info["cls"]
        out.append({"name": name, "label": info["label"], "status": info["status"], "notes": info["notes"],
                    "login": cls.login_kind if cls else "mcp", "oauth": bool(cls and cls.login_kind == "oauth"),
                    "fields": [dict(f) for f in cls.fields] if cls else []})
    return out


def secret_fields(name: str) -> set[str]:
    cls = (BROKERS.get(name) or {}).get("cls")
    return {f["key"] for f in cls.fields if f.get("secret")} if cls else set()


def field_names(name: str) -> set[str]:
    cls = (BROKERS.get(name) or {}).get("cls")
    return {f["key"] for f in cls.fields} if cls else set()


class Switch:
    """The desk's one broker object. It forwards to the chosen broker; Kite until another is chosen.

    Every part of the desk holds this same object, so choosing a broker in Settings takes effect everywhere
    at the next connect, with nothing else rebuilt.
    """

    def __init__(self, kite: KiteMCP | None = None):
        self.kite = kite or KiteMCP()
        self.active = self.kite
        self.name = "kite"
        self._adapters: dict[str, RestBroker] = {}

    def use(self, name: str):
        """Point the desk at ``name``; disconnects the broker in use if it changes."""
        if name not in BROKERS:
            raise KiteError(f'Unknown broker "{name}". Choose one of: {", ".join(BROKERS)}.')
        if name == self.name:
            return self.active
        if self.active.connected:
            self.active.disconnect()
        if name == "kite":
            self.active = self.kite
        else:
            if name not in self._adapters:
                self._adapters[name] = BROKERS[name]["cls"]()
            self.active = self._adapters[name]
        self.name = name
        return self.active

    def status(self) -> dict:
        out = dict(self.active.status())
        out.setdefault("broker", self.name)
        out.setdefault("broker_label", BROKERS[self.name]["label"])
        out.setdefault("login_kind", "mcp")
        out["broker_status"] = BROKERS[self.name]["status"]
        return out

    def complete_login(self, params: dict) -> dict:
        if not isinstance(self.active, RestBroker):
            raise KiteError("Zerodha Kite signs in through its MCP server, not here.")
        return self.active.complete_login(params)

    def __getattr__(self, attr):
        # Only reached for names not defined above: connected, tools, candles, place_order, ...
        active = self.__dict__.get("active")
        if active is None:
            raise AttributeError(attr)
        return getattr(active, attr)


__all__ = ["BROKERS", "Switch", "catalog", "secret_fields", "field_names", "RestBroker"]
