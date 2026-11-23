"""What the Charts tab keeps for the user on the server: the watchlist, the drawings on each chart and each
chart view's settings (symbol, candle size, indicators with their settings and colours).

Kept on the server (not in the browser) so the desktop app, a phone and a browser all see the same.
"""
from __future__ import annotations

import json
import math
import re
import threading
from pathlib import Path

VENUES = ("crypto", "india", "imported")
# How many points each drawing keeps. A long or short position keeps entry, target and stoploss.
KINDS = {"trend": 2, "ray": 2, "extended": 2, "hline": 1, "hray": 1, "vline": 1, "rect": 2, "channel": 3,
         "fib": 2, "measure": 2, "long": 3, "short": 3}
VIEWS = ("live", "classic")
MAX_INDICATORS = 30
_COLOUR = re.compile(r"#[0-9a-fA-F]{6}")
_KEY = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,31}")
MAX_WATCH = 50
MAX_DRAWINGS = 100          # per chart
MAX_CHARTS = 500


def _symbol(raw) -> str:
    symbol = str(raw or "").strip().upper()
    if not symbol or len(symbol) > 40:
        raise ValueError("A symbol needs 1 to 40 characters.")
    return symbol


def chart_key(venue: str, symbol: str) -> str:
    if venue not in VENUES:
        raise ValueError(f"venue must be one of {', '.join(VENUES)}")
    return f"{venue}:{_symbol(symbol)}"


class ChartStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._lock = threading.Lock()

    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(state, dict):
                return {"watchlist": state.get("watchlist") or [], "drawings": state.get("drawings") or {},
                        "layouts": state.get("layouts") or {}}
        except (OSError, ValueError):
            pass
        return {"watchlist": [{"venue": "crypto", "symbol": s} for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")],
                "drawings": {}, "layouts": {}}

    def _save(self, state: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(self.path)

    def watchlist(self) -> list[dict]:
        with self._lock:
            return self._load()["watchlist"]

    def set_watchlist(self, items) -> list[dict]:
        if not isinstance(items, list):
            raise ValueError("watchlist must be a list")
        clean, seen = [], set()
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("each watchlist entry needs venue and symbol")
            key = chart_key(str(item.get("venue", "")), item.get("symbol"))
            if key not in seen:
                seen.add(key)
                clean.append({"venue": key.split(":", 1)[0], "symbol": key.split(":", 1)[1]})
        if len(clean) > MAX_WATCH:
            raise ValueError(f"A watchlist holds {MAX_WATCH} symbols at most.")
        with self._lock:
            state = self._load()
            state["watchlist"] = clean
            self._save(state)
        return clean

    def drawings(self, venue: str, symbol: str) -> list[dict]:
        key = chart_key(venue, symbol)
        with self._lock:
            return self._load()["drawings"].get(key, [])

    def set_drawings(self, venue: str, symbol: str, items) -> list[dict]:
        key = chart_key(venue, symbol)
        if not isinstance(items, list) or len(items) > MAX_DRAWINGS:
            raise ValueError(f"drawings must be a list of at most {MAX_DRAWINGS}")
        clean = []
        for item in items:
            kind = item.get("kind") if isinstance(item, dict) else None
            if kind not in KINDS:
                raise ValueError(f"unknown drawing {str(kind)[:20]!r}; one of {', '.join(KINDS)}")
            points = item.get("points")
            if not isinstance(points, list) or len(points) != KINDS[kind]:
                raise ValueError(f"a {kind} drawing needs {KINDS[kind]} point(s)")
            pts = []
            for p in points:
                try:
                    t, price = int(p["time"]), float(p["price"])
                except (KeyError, TypeError, ValueError):
                    raise ValueError("each point needs a time and a price") from None
                if not math.isfinite(price) or price <= 0:
                    raise ValueError("a drawing's price must be above zero")
                pts.append({"time": t, "price": price})
            drawing = {"id": str(item.get("id", ""))[:20] or f"d{len(clean)}", "kind": kind, "points": pts}
            colour = item.get("color")
            if colour is not None:
                if not isinstance(colour, str) or not _COLOUR.fullmatch(colour):
                    raise ValueError("a drawing's colour is #rrggbb")
                drawing["color"] = colour
            if kind in ("long", "short"):
                drawing["qty"] = _quantity(item.get("qty", 1))
            clean.append(drawing)
        with self._lock:
            state = self._load()
            if clean:
                if key not in state["drawings"] and len(state["drawings"]) >= MAX_CHARTS:
                    raise ValueError(f"Drawings are kept for {MAX_CHARTS} charts at most. Clear some first.")
                state["drawings"][key] = clean
            else:
                state["drawings"].pop(key, None)
            self._save(state)
        return clean

    def layout(self, view: str) -> dict | None:
        if view not in VIEWS:
            raise ValueError(f"view must be one of {', '.join(VIEWS)}")
        with self._lock:
            return self._load()["layouts"].get(view)

    def set_layout(self, view: str, layout) -> dict:
        if view not in VIEWS:
            raise ValueError(f"view must be one of {', '.join(VIEWS)}")
        clean = clean_layout(layout)
        with self._lock:
            state = self._load()
            state["layouts"][view] = clean
            self._save(state)
        return clean


def _quantity(raw) -> float:
    try:
        qty = float(raw)
    except (TypeError, ValueError):
        raise ValueError("a position's quantity is a number") from None
    if not math.isfinite(qty) or qty <= 0 or qty > 1e9:
        raise ValueError("a position's quantity is above zero")
    return qty


def _text(raw, name: str, limit: int = 40) -> str:
    if not isinstance(raw, str) or len(raw) > limit:
        raise ValueError(f"{name} must be text of at most {limit} characters")
    return raw


def clean_layout(layout) -> dict:
    """A chart view's settings, checked for shape and size. Which indicators exist and their settings' ranges
    are the browser's (it drops ones it does not know), so the server only keeps the data well formed."""
    if not isinstance(layout, dict):
        raise ValueError("layout must be an object")
    out: dict = {}
    for key in ("venue", "symbol", "tf", "type"):
        if layout.get(key) is not None:
            out[key] = _text(layout[key], key)
    if out.get("venue") is not None and out["venue"] not in VENUES:
        raise ValueError(f"venue must be one of {', '.join(VENUES)}")
    out["volume"] = layout.get("volume") is not False
    items = layout.get("indicators", [])
    if not isinstance(items, list) or len(items) > MAX_INDICATORS:
        raise ValueError(f"a chart holds {MAX_INDICATORS} indicators at most")
    indicators = []
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("kind"), str) or not _KEY.fullmatch(item["kind"]):
            raise ValueError("each indicator needs a kind")
        params = item.get("params") or {}
        if not isinstance(params, dict) or len(params) > 20:
            raise ValueError("an indicator's settings must be an object")
        clean_params = {}
        for k, v in params.items():
            if not isinstance(k, str) or not _KEY.fullmatch(k):
                raise ValueError("an indicator setting's name is a short word")
            if isinstance(v, bool) or not isinstance(v, (int, float, str)):
                raise ValueError(f"setting {k} must be a number or a word")
            if isinstance(v, str):
                clean_params[k] = _text(v, k)
            elif math.isfinite(v):
                clean_params[k] = v
            else:
                raise ValueError(f"setting {k} must be a finite number")
        colours = item.get("colors") or {}
        if not isinstance(colours, dict) or len(colours) > 20:
            raise ValueError("an indicator's colours must be an object")
        for k, v in colours.items():
            if not isinstance(k, str) or not _KEY.fullmatch(k) or not isinstance(v, str) or not _COLOUR.fullmatch(v):
                raise ValueError("colours are #rrggbb")
        width = item.get("width")
        if width is not None and (isinstance(width, bool) or not isinstance(width, int) or not 1 <= width <= 4):
            raise ValueError("line thickness is 1 to 4")
        indicators.append({"kind": item["kind"], "params": clean_params, "colors": dict(colours), "width": width,
                           "hidden": bool(item.get("hidden", False))})
    out["indicators"] = indicators
    return out
