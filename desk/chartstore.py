"""What the Charts tab keeps for the user on the server: the watchlist and the drawings on each chart.

Kept on the server (not in the browser) so the desktop app, a phone and a browser all see the same.
"""
from __future__ import annotations

import json
import math
import threading
from pathlib import Path

VENUES = ("crypto", "india", "imported")
KINDS = {"trend": 2, "hline": 1, "fib": 2}
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
                return {"watchlist": state.get("watchlist") or [], "drawings": state.get("drawings") or {}}
        except (OSError, ValueError):
            pass
        return {"watchlist": [{"venue": "crypto", "symbol": s} for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT")],
                "drawings": {}}

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
                raise ValueError("a drawing is a trend line, a horizontal line or a Fibonacci retracement")
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
            clean.append({"id": str(item.get("id", ""))[:20] or f"d{len(clean)}", "kind": kind, "points": pts})
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
