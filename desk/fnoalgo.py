"""F&O algos: an option strategy or an index futures signal, run by itself on Kite, paper or live.

An F&O algo trades one index (NIFTY, BANKNIFTY, SENSEX...). It is one of two kinds:

- **Option strategy.** Any of the structures in ``optstrat.STRUCTURES`` (straddles, strangles, condors, spreads,
  butterflies, calendars, ratios, futures hedges). It opens the legs on a schedule and closes them on a schedule,
  on a stop or target, or by hand:
  ``intraday`` opens at the entry time on the chosen weekdays and closes at the exit time the same day;
  ``positional`` opens at the entry time on the chosen weekdays and holds until a set number of days before the
  near expiry; ``signal`` (directional structures only) holds the structure while a strategy on the index's day
  candles says so, the way the backtest's signal mode does.
- **Index futures.** A strategy on the index's day candles decides long, short, or long-and-short; the algo holds
  the near-month future accordingly.

Paper and live take one path. Every leg is a LIMIT order through ``Desk.auto_place``, with every check a manual
order gets (kill switch, trade window, order value, orders per day, lot size, freeze quantity, expiry). The only
difference is the fill: the paper book fills at once at the last price; Kite fills when it fills, and the algo
waits for it.

Rules that matter with real money:

- **Hedge first.** Entering, the bought legs go first; the sold legs are sent only once every bought leg has
  filled, so the margin benefit is there when the sells arrive. Exiting, the sold legs are bought back first and
  the hedges sold last, so a short is never left without its wing.
- **Risk.** A live algo needs a maximum loss in rupees: when the position's profit and loss (with charges) falls to
  it, every leg is closed and the algo takes no new entry that day. A target, a stop on each sold leg, and the
  desk's daily loss limit and kill switch apply as well. "Exit" closes everything now.
- **Naked shorts.** A structure with a sold option not covered by a bought one (short straddle, strangle, guts, ratio
  spread, jade lizard, synthetics) runs live only when Settings allows it.
- **Margin.** Before an entry the algo asks Zerodha's basket margin calculator for the legs (``desk.margin``: SPAN,
  exposure and premium, with the hedge benefit) and compares it with the funds Kite reports, or with the paper cash
  not already used by other paper F&O algos. A Kite server without that tool gets the desk's estimate instead
  (``optstrat.estimate_margin``, plus 2% on a short leg on its expiry day), and says so. Zerodha's own check at
  order time is the authority either way, and a rejected leg unwinds the entry.
- **Prices.** Option premiums come from Kite, in paper mode too: there is no other source of live option prices.
- **Expiry.** Every leg is closed at the exit time on its expiry day rather than left to settle (an in-the-money
  option left to expire pays STT on its full value).
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import threading
import uuid
from datetime import datetime, timedelta

import pandas as pd

from . import data, market, mcx, optstrat, risk
from .algo import AlgoError
from .broker import Desk, OrderRejected, fees
from .fno import INDICES
from .kite import KiteError
from .margin import MarginUnavailable, basket as zerodha_basket, why_not as margin_why_not
from .options import SPOT, STRIKE_STEP, YAHOO_SPOT
from .settings import home

logger = logging.getLogger(__name__)

MAX_ALGOS = 20
UNFILLED_AFTER = timedelta(minutes=2)    # a live leg still open after this is cancelled and sent again at a new price
MAX_SLICES = 10                          # SEBI's algo rules: an order split at the freeze quantity, at most 10 slices
ENTRY_TRIES = 3                          # unfilled attempts on one entry leg before the entry is given up and unwound
SCHEDULES = ("intraday", "positional", "signal")
DIRECTIONS = ("long", "short", "both")
WEEKLY = ("NIFTY", "SENSEX")             # the only index weeklies left after SEBI's November 2024 rules
_TIME = re.compile(r"([01]?\d|2[0-3]):[0-5]\d")
MCX_LAST = "23:25"                       # MCX closes at 23:30 in the US summer (23:55 in winter): the latest exit time


def arm_phrase(name: str) -> str:
    return f"ARM LIVE FNO {' '.join(name.upper().split())}"


def naked(structure: str) -> bool:
    """Whether a structure sells more options of a type than it buys (a call sold against long futures is covered)."""
    count = {"CE": 0, "PE": 0}
    for leg in optstrat.leg_specs(structure):
        if leg["type"] == "FUT":
            count["CE"] += leg["action"] * leg["ratio"]
        else:
            count[leg["type"]] += leg["action"] * leg["ratio"]
    return any(v < 0 for v in count.values())


def _copy(value):
    return json.loads(json.dumps(value))


class FnoAlgoEngine:
    def __init__(self, desk: Desk, derivatives, path=None, fetch=None, now=market.now_ist, library=None):
        from .custom import CustomStrategies

        self.desk, self.derivatives, self.fetch, self._now = desk, derivatives, fetch, now
        self.library = library or CustomStrategies()
        self.path = path or home() / "fno_algos.json"
        self._lock = threading.RLock()
        self._algos: dict[str, dict] = {}
        self._price_error: str | None = None
        self._retry_at: dict[str, datetime] = {}
        with contextlib.suppress(OSError, json.JSONDecodeError):
            stored = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(stored, list):
                for algo in stored:
                    if isinstance(algo, dict) and "id" in algo:
                        self._algos[algo["id"]] = {**_copy(_BLANK), **algo}

    # -- storage --------------------------------------------------------------------------------------------------

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(list(self._algos.values()), indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def list(self) -> list[dict]:
        with self._lock:
            out = _copy(sorted(self._algos.values(), key=lambda a: a["created"]))
        for algo in out:
            rules = self.leg_rules(algo)
            algo["leg_exits_text"] = rules.text() if algo["kind"] == "structure" and (rules.active or rules.trail_every) else ""
        return out

    def get(self, algo_id: str) -> dict:
        with self._lock:
            if algo_id not in self._algos:
                raise AlgoError("no such F&O algo")
            return self._algos[algo_id]

    def _log(self, algo: dict, message: str) -> None:
        if algo["log"] and algo["log"][-1]["message"] == message:
            return
        algo["log"].append({"time_ist": self._now().strftime("%Y-%m-%d %H:%M"), "message": message})
        del algo["log"][:-120]
        logger.info("fno algo %s: %s", algo["name"], message)

    # -- create, change, remove -----------------------------------------------------------------------------------

    def validate(self, raw: dict) -> dict:
        """The algo ``raw`` describes, checked; raises AlgoError. Nothing is stored."""
        def number(key, default, low, high, label, integer=False):
            value = raw.get(key, default)
            if value in (None, ""):
                value = default
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high:
                raise AlgoError(f"{label} must be between {low:g} and {high:g}.")
            if integer and int(value) != value:
                raise AlgoError(f"{label} must be a whole number.")
            return int(value) if integer else float(value)

        name = " ".join(str(raw.get("name", "")).split())
        if not 1 <= len(name) <= 40 or not all(c.isalnum() or c in " -_&." for c in name):
            raise AlgoError("Give the algo a name of up to 40 letters, digits or spaces.")
        kind = str(raw.get("kind", "structure"))
        if kind not in ("structure", "futures"):
            raise AlgoError("Kind must be structure (an option strategy) or futures.")
        underlying = str(raw.get("underlying", "NIFTY")).upper()
        metal = underlying in mcx.METALS
        if underlying not in INDICES and not metal:
            raise AlgoError(f"Index must be one of {', '.join(INDICES)}, or an MCX metal: {', '.join(mcx.METALS)}.")
        expiry_kind = str(raw.get("expiry_kind", "monthly" if metal else "weekly"))
        if expiry_kind not in ("weekly", "monthly"):
            raise AlgoError("Expiry must be weekly or monthly.")
        if metal:
            expiry_kind = "monthly"                     # MCX metal options and futures have one expiry a month
        elif expiry_kind == "weekly" and underlying not in WEEKLY:
            raise AlgoError(f"{underlying} has had only monthly options since November 2024. Choose monthly expiry.")
        mode = str(raw.get("mode", "paper"))
        if mode not in ("paper", "live"):
            raise AlgoError("Mode must be paper or live.")
        out = {"name": name, "kind": kind, "underlying": underlying,
               "exchange": mcx.EXCHANGE if metal else INDICES[underlying],
               "expiry_kind": expiry_kind, "mode": mode,
               "lots": number("lots", 1, 1, 50, "Lots", True),
               "max_loss": number("max_loss", 0, 0, 1e8, "Maximum loss (₹)"),
               "target": number("target", 0, 0, 1e8, "Target (₹)"),
               "limit_buffer_pct": number("limit_buffer_pct", 2.0, 0.1, 10, "Limit buffer %"),
               "margin_pct": number("margin_pct", 12.0, 5, 50, "Margin %")}
        if mode == "live" and not out["max_loss"]:
            raise AlgoError("A live F&O algo needs a maximum loss in rupees: it is what closes the position on a bad day.")
        times = [str(raw.get("entry_time", "09:20")).strip(), str(raw.get("exit_time", "15:15")).strip()]
        first, last = ("09:00", MCX_LAST) if metal else ("09:15", "15:29")
        for t in times:
            if not _TIME.fullmatch(t):
                raise AlgoError(f"Entry and exit times must be between {first} and {last}, like 09:20.")
        times = [t.zfill(5) for t in times]
        if not first <= times[0] < times[1] <= last:
            raise AlgoError(f"The entry time must be after {first} and before the exit time, which is at most {last}"
                            + (" (MCX closes at 23:30 while the US is on summer time)." if metal else "."))
        out["entry_time"], out["exit_time"] = times
        if kind == "structure":
            structure = str(raw.get("structure", ""))
            if structure not in optstrat.STRUCTURES:
                raise AlgoError("Unknown option strategy.")
            spec = optstrat.STRUCTURES[structure]
            schedule = str(raw.get("schedule", "intraday"))
            if schedule not in SCHEDULES:
                raise AlgoError(f"Schedule must be one of {', '.join(SCHEDULES)}.")
            if schedule == "signal" and not spec.get("bias"):
                raise AlgoError("Signal mode is for directional structures (spreads, protective put, covered call). "
                                "Run straddles, strangles, condors and flies on a schedule (intraday or positional).")
            out.update(structure=structure, structure_name=spec["name"], schedule=schedule, direction=None,
                       k=number("k", 2, 0, 40, "Short distance (k)", True), w=number("w", spec.get("w", 4), 1, 60, "Wing width (w)", True),
                       leg_sl_pct=number("leg_sl_pct", 0, 0, 1000, "Stoploss on each sold leg %"), naked=naked(structure))
            try:
                exits = risk.from_payload(raw.get("leg_exits"), "Each leg", legacy={"stop_loss": out["leg_sl_pct"]})
            except ValueError as exc:
                raise AlgoError(f"{str(exc)[:1].upper()}{str(exc)[1:]}.") from None
            scope = str(raw.get("leg_scope") or "sold")
            if scope not in optstrat.LEG_SCOPES:
                raise AlgoError("The leg exit rules apply to sold legs, bought legs or all legs.")
            out.update(leg_exits=exits.as_dict(), leg_scope=scope, leg_sl_pct=0.0)
            if out["k"] < spec.get("min_k", 0):
                raise AlgoError(f"{spec['name']} needs its legs at least {spec['min_k']} strike from the money (k).")
        else:
            direction = str(raw.get("direction", "long"))
            if direction not in DIRECTIONS:
                raise AlgoError("Direction must be long, short or both.")
            out.update(structure=None, structure_name=f"{underlying} futures, {direction}", schedule="signal",
                       direction=direction, k=0, w=0, leg_sl_pct=0.0, naked=False,
                       leg_exits=risk.NONE.as_dict(), leg_scope="sold")
        weekdays = raw.get("weekdays", [0, 1, 2, 3, 4])
        if (not isinstance(weekdays, list) or not weekdays
                or not all(isinstance(d, int) and not isinstance(d, bool) and 0 <= d <= 4 for d in weekdays)):
            raise AlgoError("Weekdays must be a list of 0 (Monday) to 4 (Friday).")
        out["weekdays"] = sorted(set(weekdays))
        out["min_days"] = number("min_days", 0, 0, 30, "Minimum days to expiry")
        out["exit_days_before"] = number("exit_days_before", 0, 0, 10, "Exit days before expiry", True)
        if out["schedule"] == "signal":
            try:
                strategy = self.library.strategy(str(raw.get("strategy", "")))
            except ValueError:
                raise AlgoError("Unknown strategy.") from None
            if strategy.key == "buy_hold":
                raise AlgoError("Buy and hold never changes position; there is nothing to automate.")
            if strategy.intraday_only:
                raise AlgoError(f"{strategy.name} needs intraday candles; F&O signal algos read day candles.")
            out.update(strategy=strategy.key, strategy_name=strategy.name,
                       params=strategy.resolve(raw.get("strategy_params")))
        else:
            out.update(strategy=None, strategy_name=None, params={})
        return out

    def create(self, raw: dict) -> dict:
        config = self.validate(raw)
        with self._lock:
            if len(self._algos) >= MAX_ALGOS:
                raise AlgoError(f"The desk runs at most {MAX_ALGOS} F&O algos.")
            if any(a["name"].lower() == config["name"].lower() for a in self._algos.values()):
                raise AlgoError("An F&O algo with that name already exists.")
            algo = {**_copy(_BLANK), **config, "id": uuid.uuid4().hex[:10],
                    "created": self._now().strftime("%Y-%m-%d %H:%M:%S")}
            self._log(algo, f"Created: {self._describe(algo)}, {algo['lots']} lot(s), {algo['mode']}.")
            self._algos[algo["id"]] = algo
            self._save()
            return _copy(algo)

    # While legs are open, only these can change: they are read afresh on every check and touch no leg's state.
    OPEN_EDITABLE = ("max_loss", "target", "limit_buffer_pct", "leg_exits", "leg_scope")
    _DERIVED = ("exchange", "structure_name", "naked", "leg_sl_pct", "strategy_name")

    def update(self, algo_id: str, raw: dict) -> dict:
        """Change an algo that is not trading live. The name is fixed (the P&L ledger files every fill under it).
        With legs open only the whole-position and per-leg exit rules and the limit buffer can change; a change of
        mode stops the algo, so it goes live only through Start and its typed phrase."""
        with self._lock:
            algo = self.get(algo_id)
            if algo["enabled"] and algo["mode"] == "live":
                raise AlgoError("This algo is trading live. Stop it before editing.")
            if algo["exit_requested"]:
                raise AlgoError("This algo is closing its legs. Edit it once it is flat.")
            config = self.validate({**raw, "name": algo["name"]})
            changed = [k for k in config if k not in self._DERIVED and config[k] != algo.get(k)]
            if algo.get("leg_exits") is None and "leg_exits" in changed \
                    and config["leg_exits"] == self.leg_rules(algo).as_dict():
                changed.remove("leg_exits")                  # saved before leg exit rules, rules unchanged
            if not changed:
                return _copy(algo)
            if algo["book"]:
                locked = [k for k in changed if k not in self.OPEN_EDITABLE]
                if locked:
                    raise AlgoError("This algo has open legs. While it does, only the maximum loss, the target, the "
                                    "exit rules for each leg and the limit buffer can change. Exit, or clear the "
                                    "legs, to change the rest.")
            was_enabled = algo["enabled"]
            algo.update(config)
            core = [k for k in changed if k not in self.OPEN_EDITABLE]
            if any(k in core for k in ("kind", "underlying", "structure", "direction", "schedule", "strategy", "params")):
                algo.update(last_signal=None, ready=False)  # a signal algo waits for a fresh signal, as a new one does
            if "mode" in changed and was_enabled:
                algo.update(enabled=False, status="Stopped")
            self._retry_at.pop(algo_id, None)
            self._log(algo, f"Edited ({', '.join(changed)}): {self._describe(algo)}, {algo['lots']} lot(s), "
                            f"{algo['mode']}." + (" Stopped: the mode changed." if "mode" in changed and was_enabled else ""))
            self._save()
            return _copy(algo)

    def _describe(self, algo: dict) -> str:
        what = algo["structure_name"] if algo["kind"] == "structure" else f"{algo['underlying']} futures {algo['direction']}"
        how = {"intraday": f"intraday {algo['entry_time']}-{algo['exit_time']}",
               "positional": f"positional, out {algo['exit_days_before']} day(s) before expiry",
               "signal": f"on {algo.get('strategy_name')} signals"}[algo["schedule"]]
        return f"{what} on {algo['underlying']} ({algo['expiry_kind']}), {how}"

    def set_enabled(self, algo_id: str, enabled: bool, phrase: str = "", force: bool = False) -> dict:
        with self._lock:
            algo = self.get(algo_id)
            if enabled:
                if algo["mode"] == "live":
                    if " ".join(str(phrase).upper().split()) != arm_phrase(algo["name"]):
                        raise AlgoError(f'Type "{arm_phrase(algo["name"])}" to let this algo place live F&O orders.')
                    cfg = self.desk.settings.all()
                    if not cfg["live_enabled"]:
                        raise AlgoError("Live orders are switched off in Settings.")
                    if not algo["max_loss"]:
                        raise AlgoError("Set a maximum loss before running this algo live.")
                    if algo["naked"] and not cfg["fno_naked_live"]:
                        raise AlgoError(f"{algo['structure_name']} sells options with no bought option to cover them: its loss "
                                        f"is unlimited or very large. Allow naked F&O shorts in Settings to run it live.")
                    if not self.desk.kite.can_trade:
                        raise AlgoError("The connected Kite server cannot place orders. Connect a self-hosted "
                                        "kite-mcp-server with your Kite Connect key.")
                if algo["schedule"] == "signal":
                    try:
                        self.library.strategy(algo["strategy"])
                    except ValueError:
                        raise AlgoError(f"The strategy “{algo['strategy_name']}” no longer exists.") from None
                algo.update(enabled=True, exit_requested=False, status="Running")
                self._log(algo, "Started.")
            else:
                if algo["book"] and not force:
                    raise AlgoError("This algo holds F&O positions. Press Exit to close them first, or stop it anyway "
                                    "and manage the positions yourself in Kite.")
                algo.update(enabled=False, exit_requested=False, status="Stopped")
                self._log(algo, "Stopped with positions open; they are no longer watched." if algo["book"] else "Stopped.")
            self._retry_at.pop(algo_id, None)
            self._save()
            return _copy(algo)

    def request_exit(self, algo_id: str) -> dict:
        """The algo's own kill switch: close every leg now, then stop."""
        with self._lock:
            algo = self.get(algo_id)
            if not algo["book"]:
                algo.update(enabled=False, exit_requested=False, status="Stopped")
                self._log(algo, "Exit pressed with nothing open: stopped.")
            else:
                algo["exit_requested"] = True
                self._log(algo, "Exit pressed: closing every leg, then stopping.")
                self._save()
                return self.evaluate(algo_id)["algo"]
            self._save()
            return _copy(algo)

    def delete(self, algo_id: str) -> None:
        with self._lock:
            algo = self.get(algo_id)
            if algo["enabled"]:
                raise AlgoError("Stop the algo before deleting it.")
            if algo["book"]:
                raise AlgoError("This algo still records open legs. Close them (Exit), or clear them after closing "
                                "them yourself, then delete.")
            del self._algos[algo_id]
            self._save()

    def forget_book(self, algo_id: str) -> dict:
        """Mark a stopped algo flat, after its legs were dealt with by hand."""
        with self._lock:
            algo = self.get(algo_id)
            if algo["enabled"]:
                raise AlgoError("Stop the algo first.")
            if algo["book"]:
                legs = ", ".join(f"{g['tradingsymbol']} {g['open_qty']}" for g in algo["book"]["legs"] if g["open_qty"])
                self._log(algo, f"Open legs cleared by hand ({legs or 'none'}); no order was sent.")
            algo["book"] = None
            self._save()
            return _copy(algo)

    # -- prices and contracts -------------------------------------------------------------------------------------

    def _prices(self, instruments: list[str]) -> dict[str, float]:
        kite, out = self.desk.kite, {}
        self._price_error = None if kite.connected else "Kite is not connected. Log in to Kite"
        if kite.connected and "get_ltp" not in kite.tools:
            self._price_error = "The connected Kite server has no price (get_ltp) tool"
        elif kite.connected:
            try:
                out = kite.ltp(instruments)
            except KiteError as exc:
                self._price_error = ("Kite needs a login. Log in to Kite" if exc.needs_login else
                                     f"Kite did not give prices ({str(exc).rstrip('.')}); trying again")
        for name in instruments:
            if not out.get(name):
                price, source = self.desk.last_price(name)
                if price and "yahoo" not in source.lower():        # a delayed close would pick the wrong strikes
                    out[name] = price
        return out

    @staticmethod
    def _metal(algo: dict) -> bool:
        return algo["underlying"] in mcx.METALS

    def _options(self, algo: dict) -> list[dict]:
        if self._metal(algo):
            return [c for c in self.derivatives.metal_contracts(algo["underlying"]) if c["type"] != "FUT"]
        return self.derivatives.contracts(algo["underlying"])

    def _session_open(self, algo: dict) -> bool:
        if self._metal(algo):
            return mcx.session_status(self._now())["open"]
        return self.desk.window_status()["market"]

    def _window(self, algo: dict) -> dict:
        return self.desk.mcx_window_status() if self._metal(algo) else self.desk.window_status()

    def _expiries(self, algo: dict) -> list[str]:
        today = self._now().strftime("%Y-%m-%d")
        dates = sorted({c["expiry"] for c in self._options(algo) if c["expiry"] >= today})
        if algo["expiry_kind"] == "monthly":
            last: dict[str, str] = {}
            for d in dates:
                last[d[:7]] = d
            dates = sorted(last.values())
        return dates

    def _exit_due(self, algo: dict, expiry: str) -> bool:
        """Whether a leg on ``expiry`` must be closed now: on its expiry day, or ``exit_days_before`` days before it
        when the algo is positional, from the exit time. An MCX metal contract is closed, whatever the schedule, as
        soon as it is within the delivery cut-off (``mcx_exit_days``): it is physically delivered."""
        now = self._now()
        days = (datetime.strptime(expiry, "%Y-%m-%d").date() - now.date()).days
        if self._metal(algo) and mcx.in_delivery_window(expiry, now, self.desk.settings.get("mcx_exit_days")):
            return True
        before = algo["exit_days_before"] if algo["schedule"] == "positional" else 0
        return days < before or (days <= before and now.strftime("%H:%M") >= algo["exit_time"])

    def _days_left(self, expiry: str, metal: bool = False) -> float:
        end = datetime.strptime(expiry, "%Y-%m-%d").replace(hour=15, minute=30, tzinfo=market.IST)
        if metal:
            end = end.replace(hour=23, minute=30)
        return (end - self._now()).total_seconds() / 86400

    def _future(self, algo: dict, after: str | None = None) -> dict | None:
        """The future an algo trades: the nearest one outside the exit rule. For an MCX option, the future it is
        written on is the first one expiring on or after the option (``after``)."""
        today = self._now().strftime("%Y-%m-%d")
        if self._metal(algo):
            for c in self.derivatives.metal_futures(algo["underlying"]):
                if c["expiry"] >= (after or today) and not self._exit_due(algo, c["expiry"]):
                    return c
            return None
        for c in self.derivatives.futures():
            if c["underlying"] == algo["underlying"] and c["expiry"] >= today and not self._exit_due(algo, c["expiry"]):
                return c
        return None

    def plan(self, algo: dict, direction: int = 1, exact: bool = False) -> dict:
        """The legs an entry would open now, with their contracts, last prices and the margin: Zerodha's figure when
        ``exact`` and the Kite server has the margin calculator, else the desk's estimate (``margin_source`` says
        which, ``margin_note`` why). Raises AlgoError with the reason when it cannot be worked out."""
        u = algo["underlying"]
        if algo["kind"] == "futures":
            specs = [{"action": direction, "type": "FUT", "off": "0", "ratio": 1, "series": "near"}]
        else:
            specs = optstrat.leg_specs(algo["structure"])
        metal = self._metal(algo)
        if not metal:
            spot = self._prices([SPOT[u]]).get(SPOT[u])
            if not spot:
                raise AlgoError(f"No live price for {u}. Log in to Kite: F&O algos take index and option prices from "
                                f"Kite, in paper mode too.")
        near = far = None
        if any(s["type"] != "FUT" for s in specs):
            expiries = self._expiries(algo)
            if not expiries:
                raise AlgoError(f"No {u} option contracts are loaded. Open F&O and refresh the contract list from Kite.")
            usable = [d for d in expiries if self._days_left(d, metal) >= algo["min_days"] and not self._exit_due(algo, d)]
            if not usable:
                raise AlgoError(f"No {algo['expiry_kind']} {u} expiry is at least {algo['min_days']:g} days away and "
                                f"outside the exit rule.")
            near = usable[0]
            if any(s["series"] == "far" for s in specs):
                later = [d for d in expiries if d > near]
                if not later:
                    raise AlgoError("No later expiry is loaded for the far leg of this calendar or diagonal.")
                far = later[0]
        if metal:
            # MCX options are written on a futures contract, not on spot: that future's price is the "spot" here.
            underlying_future = self._future(algo, after=near)
            if underlying_future is None:
                raise AlgoError(f"No {u} futures contract outside the {self.desk.settings.get('mcx_exit_days')}-day "
                                f"delivery cut-off is loaded. Refresh the contract list from Kite.")
            name = f"{mcx.EXCHANGE}:{underlying_future['tradingsymbol']}"
            spot = self._prices([name]).get(name)
            strikes = sorted({c["strike"] for c in self._options(algo) if c["expiry"] == near}) if near else []
            gaps = [b - a for a, b in zip(strikes, strikes[1:], strict=False) if b > a]
            step = min(gaps) if gaps else 1.0
            if not spot:
                raise AlgoError(f"No live price for {name}. Log in to Kite: MCX algos take futures and option prices "
                                f"from Kite, in paper mode too.")
        else:
            step = STRIKE_STEP.get(u)
            if not step:                                 # an index with no fixed step here: the gap Kite's strikes use
                strikes = sorted({c["strike"] for c in self._options(algo) if c["expiry"] == near}) if near else []
                gaps = [b - a for a, b in zip(strikes, strikes[1:], strict=False) if b > a]
                step = min(gaps) if gaps else 50
        atm = round(spot / step) * step
        if metal and strikes:
            atm = min(strikes, key=lambda k: abs(k - spot))      # MCX strikes need not sit on multiples of the step
        by_key = {(c["strike"], c["type"], c["expiry"]): c for c in self._options(algo)}
        future = self._future(algo, after=near if metal else None) if any(s["type"] == "FUT" for s in specs) else None
        legs = []
        for index, s in enumerate(specs):
            if s["type"] == "FUT":
                if future is None:
                    raise AlgoError(f"No {u} futures contract is loaded. Refresh the F&O contract list from Kite.")
                contract, strike = future, None
            else:
                strike = float(atm + optstrat._offset(s["off"], algo["k"], algo["w"]) * step)
                expiry = far if s["series"] == "far" else near
                contract = by_key.get((strike, s["type"], expiry))
                if contract is None:
                    raise AlgoError(f"Kite lists no {u} {strike:g} {s['type']} for {expiry}. Refresh the contract list, "
                                    f"or choose other strikes.")
            lot = int(contract.get("lot_size") or 1)
            factor = mcx.multiplier(contract["exchange"], contract["tradingsymbol"], lot)
            legs.append({"mult": factor,"exchange": contract["exchange"], "tradingsymbol": contract["tradingsymbol"],
                         "instrument": f"{contract['exchange']}:{contract['tradingsymbol']}", "type": s["type"],
                         "strike": strike, "expiry": contract["expiry"], "series": s["series"], "action": s["action"],
                         "ratio": s["ratio"], "lot_size": lot, "quantity": algo["lots"] * s["ratio"] * lot,
                         "freeze": int(contract.get("freeze_quantity") or 0),
                         "tick": float(contract.get("tick_size") or (1.0 if metal else 0.05)),
                         "open_qty": 0, "avg": 0.0, "realized": 0.0, "charges": 0.0, "pending": [], "tries": 0,
                         "closing": False, "stopped": False, "index": index})
        prices = self._prices([g["instrument"] for g in legs])
        missing = [g["tradingsymbol"] for g in legs if not prices.get(g["instrument"])]
        if missing:
            raise AlgoError(f"No live price for {', '.join(missing)}. "
                            f"{self._price_error or 'Kite did not return these contracts; trying again'}.")
        for g in legs:
            g["ltp"] = prices[g["instrument"]]
        lot, factor = legs[0]["lot_size"], legs[0]["mult"]
        today = self._now().strftime("%Y-%m-%d")
        expiry_day = not metal and any(g["action"] < 0 and g["type"] != "FUT" and g["expiry"] == today for g in legs)
        margin_pct = algo["margin_pct"] + (2 if expiry_day else 0)          # SEBI's extra 2% on expiry-day shorts
        margin = optstrat.estimate_margin([{**g, "entry": g["ltp"]} for g in legs], spot, algo["lots"] * lot,
                                          margin_pct) * factor
        net = sum(-g["action"] * g["ratio"] * g["ltp"] for g in legs if g["type"] != "FUT") * algo["lots"] * lot * factor
        out = {"spot": spot, "atm": atm, "near": near, "far": far, "legs": legs, "margin": round(margin, 0),
               "net_premium": round(net, 2), "expiry_day_margin": expiry_day, "margin_source": "estimate",
               "margin_estimate": round(margin, 0), "hedge_benefit": None, "margin_note": None}
        if exact:
            out.update(self._zerodha_margin(algo, legs))
        return out

    def _zerodha_margin(self, algo: dict, legs: list[dict]) -> dict:
        """Zerodha's margin for an entry's legs, or the reason it is not available (the estimate then stands)."""
        kite = self.desk.kite
        reason = margin_why_not(kite)
        if reason:
            return {"margin_note": reason}
        product = "MIS" if algo["schedule"] == "intraday" else "NRML"
        try:
            got = zerodha_basket(kite, [{"exchange": g["exchange"], "tradingsymbol": g["tradingsymbol"],
                                         "side": "BUY" if g["action"] > 0 else "SELL", "quantity": g["quantity"],
                                         "price": g["ltp"]} for g in legs], product=product)
        except MarginUnavailable as exc:
            return {"margin_note": f"{exc} The desk's estimate is used."}
        return {"margin": round(got["total"], 0), "margin_source": "kite", "hedge_benefit": round(got["hedge_benefit"], 0)}

    def _funds(self, algo: dict) -> float | None:
        if algo["mode"] == "live":
            return self.desk._live_cash("commodity" if self._metal(algo) else "equity")
        used = sum((a["book"] or {}).get("margin", 0) for a in self._algos.values()
                   if a["id"] != algo["id"] and a["mode"] == "paper" and a["book"])
        return self.desk.paper.cash - used

    def preview(self, raw: dict) -> dict:
        """What an algo with these settings would open right now. Nothing is placed or stored."""
        algo = {**_copy(_BLANK), **self.validate(raw), "id": "preview"}
        direction = -1 if algo["kind"] == "futures" and algo["direction"] == "short" else 1
        with self._lock:
            plan = self.plan(algo, direction, exact=True)
            funds = self._funds(algo)
        order = sorted(plan["legs"], key=lambda g: g["action"] < 0)        # bought legs first
        return {"spot": plan["spot"], "atm": plan["atm"], "near_expiry": plan["near"], "far_expiry": plan["far"],
                "margin": plan["margin"], "funds": funds, "net_premium": plan["net_premium"],
                "naked": algo["naked"], "expiry_day_margin": plan["expiry_day_margin"],
                "margin_source": plan["margin_source"], "margin_estimate": plan["margin_estimate"],
                "hedge_benefit": plan["hedge_benefit"], "margin_note": plan["margin_note"],
                "legs": [{"order": n + 1, "side": "BUY" if g["action"] > 0 else "SELL", "tradingsymbol": g["tradingsymbol"],
                          "type": g["type"], "strike": g["strike"], "expiry": g["expiry"], "quantity": g["quantity"],
                          "lots": g["quantity"] // g["lot_size"], "ltp": g["ltp"]} for n, g in enumerate(order)],
                "notes": ["Bought legs are sent first; sold legs only after every bought leg has filled.",
                          ("Margin is Zerodha's figure for these legs now, with the hedge benefit. "
                           if plan["margin_source"] == "kite" else "Margin is the desk's estimate. ")
                          + "Zerodha's check at order time decides; a rejected leg unwinds the entry."]}

    def margin_check(self, algo_id: str) -> dict:
        """The margin an algo's entry would need now and the funds it would be checked against, shown before it
        starts. Never raises for a plan that cannot be worked out: ``error`` says why."""
        with self._lock:
            algo = self.get(algo_id)
            out = {"id": algo_id, "mode": algo["mode"], "margin": None, "funds": None, "margin_source": None,
                   "hedge_benefit": None, "margin_note": None, "short": False, "error": None}
            direction = -1 if algo["kind"] == "futures" and algo["direction"] == "short" else 1
            try:
                plan = self.plan(algo, direction, exact=True)
            except AlgoError as exc:
                out["error"] = str(exc)
                return out
            funds = self._funds(algo)
            out.update(margin=plan["margin"], funds=funds, margin_source=plan["margin_source"],
                       hedge_benefit=plan["hedge_benefit"], margin_note=plan["margin_note"],
                       short=funds is not None and plan["margin"] > funds)
            return out

    # -- one step -------------------------------------------------------------------------------------------------

    def evaluate(self, algo_id: str, force: bool = False) -> dict:
        """Run one algo now. Returns what it did."""
        with self._lock:
            algo = self.get(algo_id)
            tail, reason = (algo["log"][-1] if algo["log"] else None), ""
            try:
                status, done = self._step(algo, force)
            except AlgoError as exc:
                reason = str(exc)
                self._log(algo, reason)
                status, done = f"Blocked: {reason.split('.')[0]}", False
            before = algo["status"]
            algo["status"] = status
            if status.startswith("Blocked") and status != before:
                # The reason logged by this run, said once; the status line alone when nothing new was logged.
                if not reason and algo["log"] and algo["log"][-1] is not tail:
                    reason = algo["log"][-1]["message"]
                self.desk.notifier.notify(f"F&O algo “{algo['name']}” is blocked", reason or status, "bad",
                                          key=f"fno-blocked:{algo['id']}:{status}")
            self._save()
            return {"status": status, "done": done, "algo": _copy(algo)}

    def _step(self, algo: dict, force: bool) -> tuple[str, bool]:
        now = self._now()
        today = now.strftime("%Y-%m-%d")
        live = algo["mode"] == "live"
        if live and not self.desk.kite.connected:
            return "Blocked: Kite not connected", False
        session = self._session_open(algo)
        book = algo["book"]
        if book and any(g["pending"] for g in book["legs"]):
            try:
                self._reconcile(algo)
            except KiteError as exc:
                why = "Kite needs a login" if exc.needs_login else str(exc)
                self._log(algo, f"Could not check the pending orders: {why}.")
                return f"Blocked: {why}", False
            if any(g["pending"] for g in book["legs"]):
                return "Running: waiting for fills", True
        if not session:
            if book and algo["schedule"] == "intraday" and book["phase"] != "closing":
                self._log(algo, "Legs still open after the close. An intraday algo carries nothing overnight; it closes "
                                "them at the next open. Check Kite.")
                return "Blocked: legs left open", False
            return "Running: market closed", True
        if book:
            stale = [g["tradingsymbol"] for g in book["legs"] if g["open_qty"] and g["expiry"] < today]
            if stale:
                raise AlgoError(f"{', '.join(stale)} expired while the desk was not running and were settled by the "
                                f"exchange. Stop the algo, check Kite, and clear its legs")
            if book["phase"] == "opening":
                return self._work_open(algo)
            if book["phase"] == "closing":
                return self._work_close(algo)
            return self._watch(algo)
        # Flat.
        if algo["exit_requested"]:
            algo.update(enabled=False, exit_requested=False)
            self._log(algo, "Flat after Exit: stopped.")
            return "Stopped", True
        if self.desk.flatten_day == today or algo["blocked_day"] == today:
            return "Running: done for today", True
        if self.desk.settings.get("kill_switch"):
            return "Blocked: kill switch is on", False
        direction = self._wanted(algo, force)
        if not direction:
            return "Running: flat", True
        return self._enter(algo, direction)

    def _wanted(self, algo: dict, force: bool) -> int:
        """+1 (or -1 for a short future) when the algo should open now, else 0."""
        now = self._now()
        today, clock = now.strftime("%Y-%m-%d"), now.strftime("%H:%M")
        if not algo["entry_time"] <= clock < algo["exit_time"] and not force:
            return 0
        if algo["schedule"] in ("intraday", "positional"):
            if algo["last_entry_day"] == today or now.weekday() not in algo["weekdays"]:
                return 0
            return 1
        signal = self._signal(algo)
        if signal is None or not algo["ready"]:
            return 0
        return signal

    def _signal(self, algo: dict) -> int | None:
        """The day's signal, read once a day from the index's finished day candles: +1, -1 or 0."""
        today = self._now().strftime("%Y-%m-%d")
        if algo["last_signal"] and algo["last_signal"]["day"] == today:
            return algo["last_signal"]["want"]
        bars = self._day_bars(algo["underlying"])
        bars = bars[bars.index.normalize() < pd.Timestamp(today)]           # finished candles only
        if len(bars) < 30:
            raise AlgoError(f"Not enough {algo['underlying']} day candles for the strategy")
        strategy = self.library.strategy(algo["strategy"])
        try:
            positions = strategy.positions(bars, algo["params"]).fillna(0).astype(int)
        except ValueError as exc:
            raise AlgoError(f"Strategy error: {exc}") from None
        long, started = int(positions.iloc[-1]) > 0, bool((positions > 0).any())
        bias = (optstrat.STRUCTURES[algo["structure"]].get("bias") if algo["kind"] == "structure" else None)
        if algo["kind"] == "futures":
            want = {"long": 1 if long else 0, "short": -1 if not long and started else 0,
                    "both": 1 if long else (-1 if started else 0)}[algo["direction"]]
        elif bias == "bear":
            want = 1 if not long and started else 0          # a bearish structure is on while the strategy is flat
        else:
            want = 1 if long else 0
        previous = algo["last_signal"]["want"] if algo["last_signal"] else None
        if previous is not None and want != previous:
            algo["ready"] = True                             # a fresh signal: a new algo does not join a trend midway
        close = round(float(bars["Close"].iloc[-1]), 2)
        algo["last_signal"] = {"day": today, "bar": bars.index[-1].strftime("%Y-%m-%d"), "want": want, "close": close}
        self._log(algo, f"{algo['last_signal']['bar']} close {close:,.2f}: signal "
                        f"{ {1: 'on' if algo['kind'] == 'structure' else 'buy', -1: 'sell', 0: 'off'}[want] }"
                        + ("" if algo["ready"] or not want else "; waiting for a fresh signal rather than joining midway") + ".")
        return want

    def _day_bars(self, underlying: str) -> pd.DataFrame:
        kite = self.desk.kite
        if underlying in mcx.METALS:
            # Kite's continuous series for the near future: MCX gold in rupees. No other source fits (COMEX prices are
            # in dollars per ounce and leave out the rupee and the import duty).
            futures = self.derivatives.metal_futures(underlying)
            if not futures:
                raise AlgoError(f"No {underlying} futures contract is loaded. Refresh the contract list from Kite")
            if not (kite.connected and "get_historical_data" in kite.tools):
                raise AlgoError(f"{underlying} day candles come from Kite's historical data, which is not available")
            try:
                frame = data.kite_bars(kite, f"{mcx.EXCHANGE}:{futures[0]['tradingsymbol']}", "1d", self._now(), raw=True)
            except (KiteError, data.NoData, KeyError, ValueError) as exc:
                raise AlgoError(f"No {underlying} day candles from Kite: {exc}") from None
            return frame
        if self.fetch is None and kite.connected and "get_historical_data" in kite.tools:
            with contextlib.suppress(KiteError, data.NoData, KeyError, ValueError):
                frame = data.kite_bars(kite, SPOT[underlying], "1d", self._now(), raw=True)
                if len(frame) >= 30:
                    return frame
        if underlying not in YAHOO_SPOT:
            raise AlgoError(f"{underlying} day candles come from Kite's historical data, which is not available")
        try:
            return data.history(YAHOO_SPOT[underlying], fetch=self.fetch)
        except data.NoData as exc:
            raise AlgoError(f"No day candles for {underlying}: {exc}") from None

    # -- entering -------------------------------------------------------------------------------------------------

    def _enter(self, algo: dict, direction: int) -> tuple[str, bool]:
        cfg, window = self.desk.settings.all(), self._window(algo)
        if cfg["enforce_trade_window"] and not window["entries"]:
            return f"Running: waiting for the trade window ({window['trade_start']}-{window['trade_end']})", True
        plan = self.plan(algo, direction, exact=True)
        funds = self._funds(algo)
        if funds is None:
            raise AlgoError("Could not read your available funds from Kite")
        if plan["margin"] > funds:
            algo["last_entry_day"] = self._now().strftime("%Y-%m-%d")
            what = "Zerodha's margin of" if plan["margin_source"] == "kite" else "Margin of about"
            raise AlgoError(f"{what} ₹{plan['margin']:,.0f} is more than the ₹{funds:,.0f} available. Entry skipped today")
        today = self._now().strftime("%Y-%m-%d")
        algo["last_entry_day"] = today
        algo["book"] = {"phase": "opening", "direction": direction, "opened": self._now().strftime("%Y-%m-%d %H:%M"),
                        "day": today, "spot": round(plan["spot"], 2), "near": plan["near"], "far": plan["far"],
                        "margin": plan["margin"], "legs": plan["legs"], "reason": None, "peak": 0.0}
        legs = ", ".join(f"{'+' if g['action'] > 0 else '−'}{g['quantity']} {g['tradingsymbol']} @ {g['ltp']:,.2f}" for g in plan["legs"])
        margin = (f"Zerodha's margin ₹{plan['margin']:,.0f}" if plan["margin_source"] == "kite"
                  else f"Margin about ₹{plan['margin']:,.0f} (estimate)")
        self._log(algo, f"Entering at {algo['underlying']} {plan['spot']:,.2f}: {legs}. {margin}.")
        return self._work_open(algo)

    def _work_open(self, algo: dict) -> tuple[str, bool]:
        book = algo["book"]
        if self._now().strftime("%H:%M") >= algo["exit_time"]:
            return self._begin_close(algo, "entry not complete by the exit time", block=True)
        for group in ([g for g in book["legs"] if g["action"] > 0], [g for g in book["legs"] if g["action"] < 0]):
            todo = [g for g in group if g["open_qty"] < g["quantity"]]
            if not todo:
                continue
            for leg in todo:
                if leg["pending"]:
                    continue
                if leg["tries"] >= ENTRY_TRIES:
                    return self._begin_close(algo, f"{leg['tradingsymbol']} did not fill after {ENTRY_TRIES} tries; "
                                                   f"entry given up", block=True)
                try:
                    self._send(algo, leg, "BUY" if leg["action"] > 0 else "SELL", leg["quantity"] - leg["open_qty"], "open")
                except OrderRejected as exc:
                    return self._begin_close(algo, f"{leg['tradingsymbol']} refused ({exc}); entry given up", block=True)
            if any(g["pending"] or g["open_qty"] < g["quantity"] for g in group):
                return "Running: entering, waiting for fills", True        # the sells wait for every hedge
        book["phase"] = "open"
        self._log(algo, f"Entered: every leg filled. Premium {'received' if self._net_entry(book) > 0 else 'paid'} "
                        f"₹{abs(self._net_entry(book)):,.0f}.")
        self.desk.notifier.notify(f"F&O algo “{algo['name']}” entered", f"{self._describe(algo)} · {algo['mode']}", "good")
        return self._watch(algo)

    @staticmethod
    def _net_entry(book: dict) -> float:
        return sum(-g["action"] * g["avg"] * g["quantity"] * g.get("mult", 1) for g in book["legs"] if g["type"] != "FUT")

    # -- holding --------------------------------------------------------------------------------------------------

    def mtm(self, algo: dict, prices: dict | None = None) -> float | None:
        """The open position's profit and loss in rupees, with charges paid so far. None without prices."""
        book = algo["book"]
        if not book:
            return None
        prices = prices if prices is not None else self._prices([g["instrument"] for g in book["legs"] if g["open_qty"]])
        total = 0.0
        for g in book["legs"]:
            total += g["realized"] - g["charges"]
            if g["open_qty"]:
                last = prices.get(g["instrument"])
                if not last:
                    return None
                total += (last - g["avg"]) * g["open_qty"] * g.get("mult", 1) * (1 if g["action"] > 0 else -1)
        return total

    def _watch(self, algo: dict) -> tuple[str, bool]:
        book, now = algo["book"], self._now()
        today = now.strftime("%Y-%m-%d")
        if algo["exit_requested"]:
            return self._begin_close(algo, "Exit pressed", block=True)
        if self.desk.flatten_day == today:
            return self._begin_close(algo, "daily loss limit", block=True)
        if algo["schedule"] == "intraday" and book["day"] != today:
            return self._begin_close(algo, "carried overnight (the desk was not running at the exit time)", block=False)
        open_legs = [g for g in book["legs"] if g["open_qty"]]
        prices = self._prices([g["instrument"] for g in open_legs])
        pnl = self.mtm(algo, prices)
        if pnl is None:
            missing = [g["tradingsymbol"] for g in open_legs if not prices.get(g["instrument"])]
            why = self._price_error or "Kite did not return these contracts; trying again"
            raise AlgoError(f"No live price for {', '.join(missing)}; the maximum loss cannot be watched. {why}")
        book["pnl"], book["peak"] = round(pnl, 2), max(book.get("peak", 0.0), pnl)
        if algo["max_loss"] and pnl <= -algo["max_loss"]:
            return self._begin_close(algo, f"maximum loss: ₹{pnl:,.0f} against ₹{algo['max_loss']:,.0f}", block=True)
        if algo["target"] and pnl >= algo["target"]:
            return self._begin_close(algo, f"target: ₹{pnl:,.0f}", block=True)
        if algo["schedule"] == "intraday" and now.strftime("%H:%M") >= algo["exit_time"]:
            return self._begin_close(algo, f"exit time {algo['exit_time']}", block=False)
        due = sorted({g["expiry"] for g in open_legs if self._exit_due(algo, g["expiry"])})
        if due:
            roll = algo["schedule"] == "signal"
            why = f"expiry {due[0]}" + (": rolls to the next expiry" if roll else "")
            return self._begin_close(algo, why, block=False, ready=roll)
        if algo["schedule"] == "signal":
            want = self._signal(algo)
            if want != book["direction"]:
                return self._begin_close(algo, "signal changed", block=False, ready=True)
        rules = self.leg_rules(algo)
        if rules.active:
            hedged = any(g["action"] < 0 and g["type"] != "FUT" for g in book["legs"])
            for g in open_legs:
                if g["closing"] or g["pending"] or not self._in_scope(algo, g):
                    continue
                last, side = prices[g["instrument"]], 1 if g["action"] > 0 else -1
                g["best"] = (max if side > 0 else min)(g.get("best") or g["avg"], last)
                stop, kind, goal = rules.levels(g["avg"], g["best"], side)
                hit = ("stop" if stop is not None and (last - stop) * side <= 0 else
                       "target" if goal is not None and (last - goal) * side >= 0 else None)
                if not hit:
                    continue
                word = kind if hit == "stop" else "target"
                level = stop if hit == "stop" else goal
                what = (f"{word} on {'bought' if side > 0 else 'sold'} leg {g['tradingsymbol']}: {last:,.2f} against "
                        f"{level:,.2f} (entry {g['avg']:,.2f})")
                if side > 0 and hedged and any(x["action"] < 0 and x["open_qty"] for x in book["legs"] if x is not g):
                    # A bought leg covers a sold one: it is never closed alone, or the sold leg would be left uncovered.
                    return self._begin_close(algo, f"{what}; it covers a sold leg, so every leg is closed", block=True)
                g["closing"], g["stopped"] = True, hit == "stop"
                left = g.get("reentries_left", rules.reentry)
                if hit == "stop" and kind == "stoploss" and left > 0:
                    g["rearm"] = {"price": g["avg"], "left": left}
                self._log(algo, f"{what[:1].upper()}{what[1:]}. {'Buying it back' if side < 0 else 'Selling it'}; "
                                f"the other legs stay." + (" It can re-enter." if g.get("rearm") else ""))
        if any(g["closing"] and g["open_qty"] for g in book["legs"]):
            return self._work_close(algo)
        waiting = self._reentries(algo, prices, now)
        if waiting:
            return waiting
        if not any(g["open_qty"] or g["pending"] or g.get("rearm") or (g.get("reentry") and g["open_qty"] < g["quantity"])
                   for g in book["legs"]):
            return self._begin_close(algo, "every leg has exited on its own rules", block=True)
        return f"Running: open, P&L {'−' if pnl < 0 else ''}₹{abs(pnl):,.0f}", True

    @staticmethod
    def leg_rules(algo: dict) -> risk.ExitRules:
        """Each leg's own exit rules. An algo saved before them has a % stoploss on each sold leg."""
        if algo.get("leg_exits"):
            return risk.ExitRules(**algo["leg_exits"])
        return risk.ExitRules(unit="pct", stop_loss=algo.get("leg_sl_pct") or 0.0)

    @staticmethod
    def _in_scope(algo: dict, g: dict) -> bool:
        scope = algo.get("leg_scope") or "sold"
        return g["type"] != "FUT" and (scope == "all" or (g["action"] < 0) == (scope == "sold"))

    def _reentries(self, algo: dict, prices: dict, now) -> tuple[str, bool] | None:
        """Open again a leg that hit its stoploss, when its rule says so; and keep working a re-entry order."""
        book = algo["book"]
        rules = self.leg_rules(algo)
        if algo["schedule"] == "intraday" and now.strftime("%H:%M") >= algo["exit_time"]:
            return None
        for g in [x for x in book["legs"] if x.get("reentry") and x["open_qty"] < x["quantity"] and not x["pending"]]:
            if g["tries"] >= ENTRY_TRIES:
                g["reentry"] = False
                self._log(algo, f"Re-entry on {g['tradingsymbol']} did not fill after {ENTRY_TRIES} tries; given up.")
                continue
            try:
                self._send(algo, g, "BUY" if g["action"] > 0 else "SELL", g["quantity"] - g["open_qty"], "open")
            except OrderRejected as exc:
                g["reentry"] = False
                self._log(algo, f"Re-entry on {g['tradingsymbol']} refused ({exc}).")
        for g in [x for x in book["legs"] if x.get("rearm") and not x["open_qty"] and not x["pending"]]:
            rearm = g["rearm"]
            if rules.reentry_mode == "at_once":
                try:
                    fresh = dict(self.plan(algo, book["direction"])["legs"][g["index"]])
                except (AlgoError, IndexError, KeyError) as exc:
                    self._log(algo, f"Re-entry on {g['tradingsymbol']} not possible: {exc}")
                    g["rearm"] = None
                    continue
                fresh.pop("ltp", None)
            else:
                last = prices.get(g["instrument"]) or self._prices([g["instrument"]]).get(g["instrument"])
                side = 1 if g["action"] > 0 else -1
                if not last or (last - rearm["price"]) * side < 0:
                    continue                                    # not back at the earlier entry price yet
                fresh = {k: g[k] for k in ("mult", "exchange", "tradingsymbol", "instrument", "type", "strike", "expiry",
                                           "series", "action", "ratio", "lot_size", "quantity", "freeze", "tick", "index")
                         if k in g}
            g["rearm"] = None
            fresh.update(open_qty=0, avg=0.0, realized=0.0, charges=0.0, pending=[], tries=0, closing=False,
                         stopped=False, reentry=True, reentries_left=rearm["left"] - 1, best=None)
            book["legs"].append(fresh)
            self._log(algo, f"Re-entry {rules.reentry - rearm['left'] + 1} of {rules.reentry}: "
                            f"{'buying' if fresh['action'] > 0 else 'selling'} {fresh['tradingsymbol']} again.")
            try:
                self._send(algo, fresh, "BUY" if fresh["action"] > 0 else "SELL", fresh["quantity"], "open")
            except OrderRejected as exc:
                fresh["reentry"] = False
                self._log(algo, f"Re-entry on {fresh['tradingsymbol']} refused ({exc}).")
        if any(x.get("reentry") and x["pending"] for x in book["legs"]):
            return "Running: re-entering a leg, waiting for the fill", True
        return None

    # -- exiting --------------------------------------------------------------------------------------------------

    def _begin_close(self, algo: dict, reason: str, block: bool, ready: bool = False) -> tuple[str, bool]:
        book = algo["book"]
        book["phase"], book["reason"] = "closing", reason
        for g in book["legs"]:
            g["closing"], g["tries"] = True, 0
        if block:
            algo["blocked_day"] = self._now().strftime("%Y-%m-%d")
        algo["ready"] = ready if algo["schedule"] == "signal" else algo["ready"]
        self._log(algo, f"Closing every leg: {reason}.")
        return self._work_close(algo)

    def _work_close(self, algo: dict) -> tuple[str, bool]:
        """Buy back the sold legs first; sell the bought legs (the hedges) only once no sold leg is left."""
        book = algo["book"]
        shorts = [g for g in book["legs"] if g["action"] < 0 and g["closing"] and (g["open_qty"] or g["pending"])]
        longs = [g for g in book["legs"] if g["action"] > 0 and g["closing"] and (g["open_qty"] or g["pending"])]
        # A single sold leg stopped out goes alone; a full exit closes every sold leg before any hedge.
        sold_left = any(g["action"] < 0 and g["open_qty"] for g in book["legs"])
        group = shorts if shorts else ([] if sold_left and book["phase"] == "closing" else longs)
        refused = None
        for leg in group:
            if leg["pending"] or not leg["open_qty"]:
                continue
            try:
                self._send(algo, leg, "SELL" if leg["action"] > 0 else "BUY", leg["open_qty"], "close")
            except OrderRejected as exc:
                refused = f"{leg['tradingsymbol']}: {exc}"
        if refused:
            self._log(algo, f"Closing order refused ({refused}). Trying again.")
            return "Blocked: closing order refused", False
        if any(g["pending"] for g in book["legs"]):
            return "Running: closing, waiting for fills", True
        if any(g["open_qty"] for g in book["legs"] if g["closing"]):
            return self._work_close(algo)
        if book["phase"] != "closing":
            for g in book["legs"]:
                g["closing"] = False
            return self._watch(algo)
        return self._finish(algo)

    def _finish(self, algo: dict) -> tuple[str, bool]:
        book = algo["book"]
        pnl = sum(g["realized"] - g["charges"] for g in book["legs"])
        algo["realized"] = round(algo["realized"] + pnl, 2)
        algo["trades"] += 1
        algo["history"] = (algo["history"] + [{"opened": book["opened"], "closed": self._now().strftime("%Y-%m-%d %H:%M"),
                                               "reason": book["reason"], "pnl": round(pnl, 2),
                                               "legs": [g["tradingsymbol"] for g in book["legs"]]}])[-50:]
        self._log(algo, f"Flat: {book['reason']}. Trade P&L ₹{pnl:,.2f} after charges.")
        self.desk.notifier.notify(f"F&O algo “{algo['name']}” closed", f"{book['reason']} · ₹{pnl:,.0f} · {algo['mode']}",
                                  "good" if pnl >= 0 else "warn")
        algo["book"] = None
        if algo["exit_requested"]:
            algo.update(enabled=False, exit_requested=False)
            self._log(algo, "Stopped after Exit.")
            return "Stopped", True
        return "Running: flat", True

    # -- orders ---------------------------------------------------------------------------------------------------

    def _send(self, algo: dict, leg: dict, side: str, quantity: int, intent: str) -> None:
        """Send ``quantity`` of a leg as LIMIT orders near the last price, split at the freeze quantity.
        Paper fills at once; a live order is recorded as pending. Raises OrderRejected."""
        last = self._prices([leg["instrument"]]).get(leg["instrument"])
        if not last:
            raise OrderRejected([f"no live price for {leg['tradingsymbol']}"])
        buffer, tick = algo["limit_buffer_pct"] / 100, leg.get("tick") or 0.05
        price = market.round_to_tick(last * (1 + buffer) if side == "BUY" else last * (1 - buffer), side, tick)
        price = max(price, tick)
        lot, freeze = leg["lot_size"], leg.get("freeze") or 0
        biggest = (freeze // lot) * lot if freeze >= lot else quantity
        if -(-quantity // biggest) > MAX_SLICES:
            raise OrderRejected([f"{quantity} of {leg['tradingsymbol']} needs more than {MAX_SLICES} orders at the "
                                 f"exchange's freeze limit of {freeze}; use fewer lots"])
        product = "MIS" if algo["schedule"] == "intraday" else "NRML"
        source = f"fno:{algo['name']}"
        while quantity > 0:
            size = min(quantity, biggest)
            order = {"exchange": leg["exchange"], "tradingsymbol": leg["tradingsymbol"], "side": side, "quantity": size,
                     "product": product, "order_type": "LIMIT", "price": price, "variety": "regular", "intent": intent}
            placed = self.desk.auto_place(order, algo["mode"], source, allow_short=intent == "open" and side == "SELL")
            if algo["mode"] == "live":
                leg["pending"].append({"order_id": str(placed.get("order_id")), "side": side, "quantity": size, "price": price,
                                       "intent": intent, "product": product,
                                       "placed": self._now().strftime("%Y-%m-%d %H:%M:%S")})
                self._log(algo, f"Sent {side} {size} {leg['tradingsymbol']} at limit {price:,.2f} ({intent}), "
                                f"order {placed.get('order_id')}.")
            else:
                self._fill(algo, leg, side, size, float(placed["fill_price"]), product)
            quantity -= size

    def _fill(self, algo: dict, leg: dict, side: str, quantity: int, price: float, product: str) -> None:
        opening = (side == "BUY") == (leg["action"] > 0)
        if opening:
            leg["avg"] = (leg["avg"] * leg["open_qty"] + price * quantity) / (leg["open_qty"] + quantity)
            leg["open_qty"] += quantity
        else:
            quantity = min(quantity, leg["open_qty"])
            leg["realized"] += (price - leg["avg"]) * quantity * leg.get("mult", 1) * (1 if leg["action"] > 0 else -1)
            leg["open_qty"] -= quantity
        with contextlib.suppress(ValueError):
            leg["charges"] += fees(side, quantity, price, product, leg["exchange"], leg["tradingsymbol"]).total
        self._log(algo, f"{algo['mode'].capitalize()} {side} {quantity} {leg['tradingsymbol']} filled at {price:,.2f}.")

    def _reconcile(self, algo: dict) -> None:
        """Settle live orders sent earlier: fills, cancels, and orders left unfilled too long (cancelled here)."""
        rows = {str(o.get("order_id")): o for o in self.desk.kite.orders()}
        now = self._now()
        for leg in algo["book"]["legs"]:
            keep = []
            for pending in leg["pending"]:
                row = rows.get(pending["order_id"])
                placed = datetime.strptime(pending["placed"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=market.IST)
                if row is None:
                    if placed.date() == now.date():
                        keep.append(pending)
                    else:
                        self._log(algo, f"Order {pending['order_id']} is no longer listed by Kite; assuming it did not fill. "
                                        f"Check your positions.")
                    continue
                status = str(row.get("status", "")).upper()
                filled = int(row.get("filled_quantity") or 0)
                if status == "COMPLETE" or status in ("CANCELLED", "REJECTED"):
                    if filled or status == "COMPLETE":
                        self._fill(algo, leg, pending["side"], filled or pending["quantity"],
                                   float(row.get("average_price") or pending["price"]), pending["product"])
                    if status != "COMPLETE" and (filled or 0) < pending["quantity"]:
                        leg["tries"] += 1
                        self._log(algo, f"Order {pending['order_id']} {status.lower()} with {filled} of "
                                        f"{pending['quantity']} filled ({row.get('status_message') or 'no reason given'}).")
                    continue
                if now - placed > UNFILLED_AFTER:
                    with contextlib.suppress(KiteError):
                        self.desk.kite.cancel_order(pending["order_id"], "regular")
                        self._log(algo, f"Order {pending['order_id']} unfilled after {UNFILLED_AFTER.seconds // 60} "
                                        f"minutes; cancel sent. It is sent again at a new price.")
                keep.append(pending)
            leg["pending"] = keep

    # -- scheduler ------------------------------------------------------------------------------------------------

    def due(self) -> list[str]:
        now = self._now()
        clock = now.time()
        nse = market.is_trading_day(now) and market.OPEN <= clock < market.CLOSE
        metals = mcx.session_status(now)["open"]
        with self._lock:
            return [a["id"] for a in self._algos.values()
                    if (a["enabled"] or a["exit_requested"]) and self._retry_at.get(a["id"], now) <= now
                    and (metals if self._metal(a) else nse)]

    def claimed(self, instrument: str, source: str, mode: str) -> int:
        """The signed quantity of ``instrument`` that the ``mode`` (paper or live) F&O algos other than ``source``
        (``fno:<name>``; "" for none) hold: below zero for a short. An account's position nets every algo's legs;
        this is the part that is not ``source``'s."""
        with self._lock:
            return sum(g["open_qty"] * (1 if g["action"] > 0 else -1)
                       for a in self._algos.values()
                       if a["book"] and a["mode"] == mode and f"fno:{a['name']}" != source
                       for g in a["book"]["legs"] if g["instrument"] == instrument and g["open_qty"])

    def live_names(self) -> list[str]:
        with self._lock:
            return [a["name"] for a in self._algos.values() if a["enabled"] and a["mode"] == "live"]

    def tick(self) -> None:
        for algo_id in self.due():
            try:
                outcome = self.evaluate(algo_id)
            except Exception as exc:  # noqa: BLE001 - one algo's failure must not stop the others
                logger.exception("fno algo %s failed", algo_id)
                name = self._algos.get(algo_id, {}).get("name", algo_id)
                self.desk.notifier.notify(f"F&O algo “{name}” hit an error", f"{type(exc).__name__}: {exc}. It is tried "
                                          f"again; check the algo and its legs.", "bad",
                                          key=f"fno-error:{algo_id}:{self._now():%Y-%m-%d %H}")
                outcome = {"done": False}
            if not outcome["done"]:
                self._retry_at[algo_id] = self._now() + timedelta(seconds=40)
            else:
                self._retry_at.pop(algo_id, None)


_BLANK = {
    "enabled": False, "exit_requested": False, "status": "Stopped", "log": [], "book": None,
    "last_entry_day": None, "blocked_day": None, "last_signal": None, "ready": False,
    "realized": 0.0, "trades": 0, "history": [],
}
