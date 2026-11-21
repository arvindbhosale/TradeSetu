"""AI strategy scan: every stock of an index against a library of strategies and settings.

"AI" here is a systematic search, not a language model: it tries each built-in daily strategy over a small
grid of settings on every stock, chooses settings on one part of the history and judges them on a later part
it never saw. Testing hundreds of combinations always finds one that looks good by luck, so the scan:

- Splits the dates once for every stock: settings are chosen on the earlier part (in-sample) and the
  strategies are ranked only on the later part (out-of-sample, "unseen").
- Treats each strategy as one portfolio: the same settings on every stock, the capital split equally. One
  strategy that works across many stocks is worth more than the best fit for each stock.
- Charges brokerage, STT, stamp duty, exchange and SEBI fees, GST and DP charges on every fill (delivery),
  plus slippage, through the same engine as the manual backtest (``backtest.run_strategy``).
- Keeps the grid bounded (3 values for at most 2 or 3 settings), counts every trial, and corrects the
  winner's Sharpe ratio for the number of strategies compared (deflated Sharpe ratio, Bailey and
  Lopez de Prado 2014).
- Marks a result fragile when the neighbouring settings did not also make money on unseen data, and too
  thin when it made fewer than ``MIN_TRADES`` trades there.
- Tries stoploss and target levels only on the three best strategies, chosen in-sample as well.

It cannot fix survivorship: today's members of an index are the stocks that did well enough to be in it,
so every past result is flattered. The summary says so.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool
from pathlib import Path
from statistics import NormalDist

import numpy as np
import pandas as pd

from .backtest import MIN_TRADES, TRADING_DAYS, BacktestParams, run_strategy
from .risk import ExitRules
from .strategies import STRATEGIES

logger = logging.getLogger(__name__)

DEPTHS = {"quick": ("Quick: each strategy's usual settings", 0),
          "standard": ("Standard: 3 values of the 2 main settings", 2),
          "deep": ("Deep: 3 values of the 3 main settings", 3)}
MAX_STOCKS = 500
MIN_STOCK_CAPITAL = 10_000.0
DSR_PASS = 0.95                     # probability the Sharpe ratio is above what luck alone gives
ROBUST_PASS = 0.5                   # share of neighbouring settings that must also make money unseen
EXIT_TRIALS = ((5.0, 0.0), (10.0, 0.0), (0.0, 20.0), (5.0, 20.0), (10.0, 20.0))   # (stoploss %, target %)
REFINE_TOP = 3
CACHE_HOURS = 12
BASELINE = "buy_hold"
_EULER = 0.5772156649
_N = NormalDist()

VERDICTS = {   # verdict: (rank order, plain meaning)
    "Beats buy and hold": (0, "Made money on unseen data with a better Sharpe ratio than holding the same stocks, and not by luck."),
    "Works": (1, "Made money on unseen data and passed the luck test, but holding the same stocks did better."),
    "Could be luck": (2, "Made money on unseen data, but after counting how many strategies were tried it may be luck."),
    "Fragile": (3, "Made money, but most neighbouring settings did not: the chosen settings may be a lucky fit."),
    "Below a fixed deposit": (4, "Made money on unseen data, but less than a safe deposit at the risk-free rate."),
    "Too few trades": (5, f"Fewer than {MIN_TRADES} trades on unseen data across all stocks: too little to judge."),
    "Loses money": (6, "Lost money on unseen data after charges."),
}


# -- the search space -------------------------------------------------------------------------------------

def strategy_keys() -> list[str]:
    """Built-in strategies that run on day candles, the baseline last."""
    keys = [k for k, s in STRATEGIES.items() if not s.intraday_only and not k.startswith("custom:") and k != BASELINE]
    return [*keys, BASELINE]


def _values(param) -> list:
    d = float(param.default)
    raw = [d * 0.6, d, d * 1.5] if d else [d, param.minimum + (param.maximum - param.minimum) * 0.1]
    out = []
    for v in raw:
        v = min(max(v, param.minimum), param.maximum)
        v = int(round(v)) if param.integer else round(v, 3)
        if v not in out:
            out.append(v)
    return sorted(out)


def candidates(depth: str = "standard", keys: list[str] | None = None) -> list[dict]:
    """Every (strategy, settings) pair the scan tries. ``axes`` is each varied setting's position in its
    3-value list, so two candidates one step apart on one axis are neighbours."""
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {', '.join(DEPTHS)}")
    varied = DEPTHS[depth][1]
    out = []
    for key in keys or strategy_keys():
        strategy = STRATEGIES[key]
        axes = {p.name: _values(p) for p in strategy.params[:varied]}
        combos = [{}]
        for name, values in axes.items():
            combos = [{**c, name: (i, v)} for c in combos for i, v in enumerate(values)]
        for combo in combos:
            params = {p.name: p.default for p in strategy.params}
            params.update({name: v for name, (_, v) in combo.items()})
            out.append({"id": len(out), "strategy": key, "params": params,
                        "axes": {name: i for name, (i, _) in combo.items()}, "exits": (0.0, 0.0)})
    return out


def describe_params(strategy_key: str, params: dict) -> str:
    strategy = STRATEGIES[strategy_key]
    if not strategy.params:
        return "No settings"
    return ", ".join(f"{p.label} {params.get(p.name, p.default):g}" for p in strategy.params)


def describe_exits(exits) -> str:
    stop, target = exits
    if not stop and not target:
        return "Strategy's own exit"
    parts = ([f"Stoploss {stop:g}%"] if stop else []) + ([f"Target {target:g}%"] if target else [])
    return ", ".join(parts)


# -- statistics -----------------------------------------------------------------------------------------

def segment_stats(values: np.ndarray, dates: pd.DatetimeIndex, rf_pct: float) -> dict:
    """Return, yearly return, Sharpe ratio and worst fall of an equity curve segment."""
    values = np.asarray(values, float)
    if len(values) < 2 or values[0] <= 0:
        return {"return_pct": None, "cagr_pct": None, "sharpe": None, "max_drawdown_pct": None}
    total = values[-1] / values[0] - 1
    years = max((dates[-1] - dates[0]).days, 1) / 365.25
    cagr = (values[-1] / values[0]) ** (1 / years) - 1 if values[-1] > 0 else -1.0
    returns = np.diff(values) / values[:-1]
    excess = returns - rf_pct / 100 / TRADING_DAYS
    sd = returns.std(ddof=1) if len(returns) > 1 else 0.0
    peak = np.maximum.accumulate(values)
    return {"return_pct": round(float(total) * 100, 2), "cagr_pct": round(float(cagr) * 100, 2),
            "sharpe": round(float(excess.mean() / sd * math.sqrt(TRADING_DAYS)), 3) if sd > 0 else None,
            "max_drawdown_pct": round(float((values / peak - 1).min()) * 100, 2)}


def deflated_sharpe(returns: np.ndarray, rf_pct: float, trials: int) -> float | None:
    """Probability that ``returns`` (daily) beat the risk-free rate by skill rather than luck, when it was the
    pick of ``trials`` strategies (the deflated Sharpe ratio of Bailey and Lopez de Prado, 2014).

    The bar is the Sharpe ratio the best of ``trials`` strategies with no skill at all would show by chance
    over the same number of days: the expected maximum of ``trials`` draws from the spread a zero-skill Sharpe
    ratio has (1 / (days - 1)). Skew and fat tails widen it. Similar strategies are not independent, so
    counting each as a full trial makes the test stricter, not looser. With one trial it is the
    probabilistic Sharpe ratio."""
    r = np.asarray(returns, float) - rf_pct / 100 / TRADING_DAYS
    t = len(r)
    sd = r.std(ddof=1) if t > 2 else 0.0
    if sd <= 0:
        return None
    sr = r.mean() / sd
    z = (r - r.mean()) / sd
    skew, kurt = float((z ** 3).mean()), float((z ** 4).mean())
    sr0 = 0.0
    if trials > 1:
        expected_max = (1 - _EULER) * _N.inv_cdf(1 - 1 / trials) + _EULER * _N.inv_cdf(1 - 1 / (trials * math.e))
        sr0 = math.sqrt(1 / (t - 1)) * expected_max
    denom = 1 - skew * sr + (kurt - 1) / 4 * sr ** 2
    if denom <= 0:
        return None
    return round(_N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom)), 4)


# -- one stock: every candidate (runs in a worker process) ----------------------------------------------------

def run_stock(symbol: str, bars: pd.DataFrame, cands: list[dict], params: dict, cut: str) -> dict:
    """Every candidate on one stock. Returns a row per candidate and each one's daily equity values."""
    base = BacktestParams(**params)
    cut_at = pd.Timestamp(cut)
    rows, curves = [], {}
    for cand in cands:
        stop, target = cand["exits"]
        run_params = BacktestParams(**{**params, "exits": ExitRules(unit="pct", stop_loss=stop, target=target)}) \
            if stop or target else base
        try:
            out = run_strategy(bars, cand["strategy"], cand["params"], run_params)
        except ValueError as exc:                    # e.g. a fast average not faster than the slow one
            rows.append({"id": cand["id"], "symbol": symbol, "error": str(exc)})
            continue
        dates = pd.DatetimeIndex([d for d, _ in out["equity"]])
        values = np.array([v for _, v in out["equity"]], float)
        split = int(np.searchsorted(dates, cut_at))
        m = out["metrics"]
        trades = out["trades"]
        closed = [t for t in trades if not t["open"]]
        row = {"id": cand["id"], "symbol": symbol, "error": None,
               "return_pct": m["total_return_pct"], "cagr_pct": m["cagr_pct"], "sharpe": m["sharpe"],
               "max_drawdown_pct": m["max_drawdown_pct"], "trades": m["trades"], "win_rate_pct": m["win_rate_pct"],
               "charges": m["total_charges"],
               "is_trades": sum(1 for t in closed if pd.Timestamp(t["entry_date"]) < cut_at),
               "oos_trades": sum(1 for t in closed if pd.Timestamp(t["entry_date"]) >= cut_at),
               "unfunded": m["trades"] == 0 and m["open_trades"] == 0 and cand["strategy"] == BASELINE}
        if 2 <= split <= len(values) - 2:
            ins = segment_stats(values[:split + 1], dates[:split + 1], base.risk_free_rate_pct)
            oos = segment_stats(values[split:], dates[split:], base.risk_free_rate_pct)
        else:
            ins = oos = segment_stats(np.array([]), dates, 0)
        row.update({f"is_{k}": v for k, v in ins.items()})
        row.update({f"oos_{k}": v for k, v in oos.items()})
        rows.append(row)
        curves[cand["id"]] = (dates, values)
    return {"symbol": symbol, "rows": rows, "curves": curves}


# -- the price cache ------------------------------------------------------------------------------------------

class PriceCache:
    """Day candles per stock and date range on disk, so a second scan (or the exits stage) does not ask Kite
    again. A range that ends before today is kept a week, one that reaches today ``CACHE_HOURS``."""

    def __init__(self, folder: Path):
        self.folder = folder

    def _path(self, symbol: str, start: str, end: str) -> Path:
        safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in symbol)
        return self.folder / f"{safe}__{start}__{end}.csv"

    def get(self, symbol: str, start: str, end: str) -> tuple[pd.DataFrame, str] | None:
        path = self._path(symbol, start, end)
        meta = path.with_suffix(".json")
        if not path.exists() or not meta.exists():
            return None
        ttl = 7 * 24 if pd.Timestamp(end) < pd.Timestamp.now().normalize() else CACHE_HOURS
        if time.time() - path.stat().st_mtime > ttl * 3600:
            return None
        try:
            frame = pd.read_csv(path, index_col=0, parse_dates=True)
            source = json.loads(meta.read_text()).get("source", "cache")
        except (OSError, ValueError):
            return None
        return (frame, source) if len(frame) else None

    def put(self, symbol: str, start: str, end: str, frame: pd.DataFrame, source: str) -> None:
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            path = self._path(symbol, start, end)
            frame[["Open", "High", "Low", "Close", "Volume"]].to_csv(path)
            path.with_suffix(".json").write_text(json.dumps({"source": source}))
        except OSError as exc:            # a full disk only costs the cache
            logger.warning("scan cache not written for %s: %s", symbol, exc)


# -- the scan -----------------------------------------------------------------------------------------------

def _cut_date(start: str, end: str, oos_pct: float) -> pd.Timestamp:
    days = pd.bdate_range(start, min(pd.Timestamp(end), pd.Timestamp.now().normalize()))
    if len(days) < 250:
        raise ValueError("Use at least a year of history: the scan needs room for an in-sample and an unseen part")
    return days[int(len(days) * (1 - oos_pct / 100))]


def _sharpe_key(stats: dict) -> float:
    return stats["sharpe"] if stats["sharpe"] is not None else -9.0


class _Scan:
    """The state of one scan: candidates, every run's row, and each candidate's portfolio equity (the sum of
    its per-stock equity curves on one calendar)."""

    def __init__(self, stocks, params: BacktestParams, start, end, depth, oos_pct, log, stopped, workers):
        self.stocks, self.start, self.end, self.depth, self.oos_pct = stocks, start, end, depth, oos_pct
        self.log, self.stopped, self.workers = log, stopped, workers
        self.params = params
        self.per_stock = params.capital / len(stocks)
        self.cut = _cut_date(start, end, oos_pct)
        self.cands = candidates(depth)
        self.run_params = {**params.__dict__, "capital": self.per_stock, "exits": None}
        self.calendar = pd.bdate_range(start, end)
        self.totals: dict[int, np.ndarray] = {}
        self.traded = np.zeros(len(self.calendar), bool)
        self.rows: list[dict] = []
        self.bars: dict[str, pd.DataFrame] = {}
        self.tested: set[str] = set()
        self.skipped: list[dict] = []
        self.sources: dict[str, int] = {}
        self.stopped_early = False
        self.pool = None

    # running ----------------------------------------------------------------------------------------------

    def _add(self, result: dict) -> None:
        base = next((r for r in result["rows"] if self.cands[r["id"]]["strategy"] == BASELINE), None)
        if base and base.get("unfunded"):
            self.skipped.append({"symbol": result["symbol"],
                                 "reason": f"One share costs more than ₹{self.per_stock:,.0f}, the money per stock"})
            self.bars.pop(result["symbol"], None)
            return
        self.tested.add(result["symbol"])
        self.rows.extend(result["rows"])
        for cid, (dates, values) in result["curves"].items():
            series = pd.Series(values, index=dates).reindex(self.calendar).ffill().fillna(self.per_stock)
            if cid not in self.totals:
                self.totals[cid] = np.zeros(len(self.calendar))
            self.totals[cid] += series.to_numpy()
        first = next(iter(result["curves"].values()), None)
        if first is not None:
            self.traded |= self.calendar.isin(first[0])

    def _run(self, jobs: list[tuple[str, list[dict]]], on_result) -> bool:
        """``(symbol, candidates)`` jobs, in worker processes when there are any; False when stopped."""
        cut = str(self.cut.date())
        if not self.pool:
            for symbol, todo in jobs:
                if self.stopped():
                    return False
                on_result(run_stock(symbol, self.bars[symbol], todo, self.run_params, cut))
            return True
        futures = [self.pool.submit(run_stock, symbol, self.bars[symbol], todo, self.run_params, cut) for symbol, todo in jobs]
        for i, f in enumerate(futures):
            if self.stopped():
                for g in futures:
                    g.cancel()
                return False
            try:
                result = f.result()
            except BrokenProcessPool:
                # A worker died (out of memory, or the server cannot start processes): carry on in this one.
                logger.warning("scan worker processes failed; continuing in one process", exc_info=True)
                self.log("Worker processes failed; continuing in one process (slower).")
                self.pool.shutdown(wait=False, cancel_futures=True)
                self.pool = None
                return self._run(jobs[i:], on_result)
            on_result(result)
        return True

    def run(self, load, progress) -> None:
        if self.workers > 1:
            import multiprocessing
            self.pool = ProcessPoolExecutor(max_workers=self.workers, mp_context=multiprocessing.get_context("spawn"))
        done = 0

        def first_pass(result):
            nonlocal done
            self._add(result)
            done += 1
            progress(done, len(self.stocks))

        try:
            batch: list[tuple[str, list[dict]]] = []
            total = len(self.stocks)
            for n, stock in enumerate(self.stocks, 1):
                if self.stopped():
                    self.stopped_early = True
                    break
                symbol = stock["symbol"]
                try:
                    bars, source = load(symbol)
                except Exception as exc:  # noqa: BLE001 - one stock without prices does not stop the scan
                    self.skipped.append({"symbol": symbol, "reason": str(exc)[:200]})
                    self.log(f"[{n}/{total}] {symbol}: no prices ({str(exc)[:120]})")
                    done += 1
                    progress(done, total)
                    continue
                if len(bars) < 250:
                    self.skipped.append({"symbol": symbol, "reason": f"Only {len(bars)} day candles (listed recently?)"})
                    self.log(f"[{n}/{total}] {symbol}: only {len(bars)} day candles, left out")
                    done += 1
                    progress(done, total)
                    continue
                self.sources[source] = self.sources.get(source, 0) + 1
                self.bars[symbol] = bars
                self.log(f"[{n}/{total}] {symbol}: {len(bars):,} day candles from {source}")
                batch.append((symbol, self.cands))
                if len(batch) >= max(1, self.workers * 2):     # test these while the next stocks load
                    if not self._run(batch, first_pass):
                        self.stopped_early = True
                        break
                    batch = []
            if batch and not self.stopped_early and not self._run(batch, first_pass):
                self.stopped_early = True
            if self.stopped_early:
                self.log("Stopped: the results cover the stocks tested so far.")
            else:
                self.log(f"Backtests done on {len(self.tested)} stocks. Comparing strategies.")
            self._prepare()
            self._choose()
            if not self.stopped_early:
                self._refine()
        finally:
            if self.pool:
                self.pool.shutdown(wait=False, cancel_futures=True)

    # judging ----------------------------------------------------------------------------------------------

    def _prepare(self) -> None:
        self.scanned = sorted(self.tested)
        if not self.scanned:
            raise ValueError("No stock could be tested: "
                             + "; ".join(f"{s['symbol']}: {s['reason']}" for s in self.skipped[:5]))
        self.days = self.calendar[self.traded]
        self.split = int(np.searchsorted(self.days, self.cut))
        if self.split < 60 or len(self.days) - self.split < 60:
            raise ValueError("The prices do not cover both parts of the period. Use a longer period.")
        self.by_id: dict[int, list[dict]] = {}
        for r in self.rows:
            self.by_id.setdefault(r["id"], []).append(r)
        self.books = {cid: self._book(self.cands[cid]) for cid in self.totals}

    def _book(self, cand: dict) -> dict:
        """One candidate as a portfolio: the same settings on every stock, equal money in each."""
        values, split, days, rf = self.totals[cand["id"]][self.traded], self.split, self.days, self.params.risk_free_rate_pct
        mine = [r for r in self.by_id.get(cand["id"], []) if not r.get("error")]
        judged = [r for r in mine if r.get("oos_return_pct") is not None]
        return {
            "id": cand["id"], "strategy": cand["strategy"], "name": STRATEGIES[cand["strategy"]].name,
            "params": cand["params"], "settings": describe_params(cand["strategy"], cand["params"]),
            "exits": list(cand["exits"]), "exits_text": describe_exits(cand["exits"]), "axes": cand["axes"],
            "in_sample": segment_stats(values[:split + 1], days[:split + 1], rf),
            "unseen": segment_stats(values[split:], days[split:], rf),
            "is_trades": sum(r["is_trades"] for r in mine), "oos_trades": sum(r["oos_trades"] for r in mine),
            "charges": round(sum(r["charges"] for r in mine), 2),
            "stocks_profitable": sum(1 for r in judged if (r["oos_return_pct"] or 0) > 0), "stocks": len(judged),
            "_values": values,
        }

    def _choose(self) -> None:
        """Each strategy's settings, chosen on the in-sample part only, and how its neighbours did unseen."""
        self.chosen: dict[str, dict] = {}
        for key in dict.fromkeys(c["strategy"] for c in self.cands):
            mine = [b for b in self.books.values() if b["strategy"] == key]
            if not mine:
                continue
            enough = [b for b in mine if b["is_trades"] >= MIN_TRADES] or mine
            pick = max(enough, key=lambda b: _sharpe_key(b["in_sample"]))
            near = [b for b in mine if b is not pick and sum(abs(b["axes"][a] - pick["axes"][a]) for a in pick["axes"]) == 1]
            pick.update(neighbours=len(near), tried=len(mine), robust=round(
                sum(1 for b in near if (b["unseen"]["return_pct"] or 0) > 0) / len(near), 2) if near else None)
            self.chosen[key] = pick
        self.exit_rows: list[dict] = []

    def _refine(self) -> None:
        """Stoploss and target levels for the three strategies with the best unseen Sharpe ratio, chosen
        in-sample like the settings."""
        order = sorted((k for k in self.chosen if k != BASELINE), key=lambda k: _sharpe_key(self.chosen[k]["unseen"]),
                       reverse=True)[:REFINE_TOP]
        if not order:
            return
        self.log(f"Trying stoploss and target levels on {', '.join(STRATEGIES[k].name for k in order)}.")
        extra = []
        for key in order:
            for exits in EXIT_TRIALS:
                extra.append({"id": len(self.cands) + len(extra), "strategy": key, "params": self.chosen[key]["params"],
                              "axes": {}, "exits": exits})
        self.cands.extend(extra)
        before = set(self.totals)
        if not self._run([(s, extra) for s in self.scanned], self._add_refined):
            self.stopped_early = True
            self.log("Stopped while trying stoploss and target levels; those are left out.")
            for cid in set(self.totals) - before:
                self.totals.pop(cid)
            return
        for r in self.rows:
            if r["id"] >= extra[0]["id"]:
                self.by_id.setdefault(r["id"], []).append(r)
        for key in order:
            base = self.chosen[key]
            options = [base] + [self._book(c) for c in extra if c["strategy"] == key]
            for o in options[1:]:
                self.books[o["id"]] = o
            enough = [o for o in options if o["is_trades"] >= MIN_TRADES] or options
            best = max(enough, key=lambda o: _sharpe_key(o["in_sample"]))
            for o in options:
                self.exit_rows.append({
                    "strategy": base["name"], "settings": base["settings"], "exits": o["exits_text"],
                    "is_sharpe": o["in_sample"]["sharpe"], "is_return_pct": o["in_sample"]["return_pct"],
                    "oos_return_pct": o["unseen"]["return_pct"], "oos_sharpe": o["unseen"]["sharpe"],
                    "oos_max_drawdown_pct": o["unseen"]["max_drawdown_pct"], "oos_trades": o["oos_trades"],
                    "chosen": "Yes" if o is best else "No"})
            if best is not base:
                best.update(neighbours=base["neighbours"], robust=base["robust"], tried=base["tried"] + len(EXIT_TRIALS),
                            without_exits=base["unseen"])
                self.chosen[key] = best

    def _add_refined(self, result: dict) -> None:
        self.rows.extend(result["rows"])
        for cid, (dates, values) in result["curves"].items():
            series = pd.Series(values, index=dates).reindex(self.calendar).ffill().fillna(self.per_stock)
            self.totals.setdefault(cid, np.zeros(len(self.calendar)))
            self.totals[cid] += series.to_numpy()

    def result(self, index_name: str) -> dict:
        """Rank on unseen results only, with the luck test across every strategy compared."""
        rf = self.params.risk_free_rate_pct
        bh = self.chosen[BASELINE]
        contenders = [self.chosen[k] for k in self.chosen if k != BASELINE]
        # Choices that looked at unseen results: which strategy wins, and which three got stoploss/target trials.
        # Settings were chosen in-sample, so they do not count here.
        luck_trials = len(contenders) + sum(1 for r in self.exit_rows if r["exits"] != describe_exits((0.0, 0.0)))
        for b in contenders:
            values = b["_values"][self.split:]
            b["luck_test"] = deflated_sharpe(np.diff(values) / values[:-1], rf, luck_trials)
            b["vs_buy_hold_pct"] = None if b["unseen"]["cagr_pct"] is None or bh["unseen"]["cagr_pct"] is None \
                else round(b["unseen"]["cagr_pct"] - bh["unseen"]["cagr_pct"], 2)
            b["verdict"] = _verdict(b, bh)
        contenders.sort(key=lambda b: (VERDICTS[b["verdict"]][0], -_sharpe_key(b["unseen"])))
        for rank, b in enumerate(contenders, 1):
            b["rank"] = rank
        winner = next((b for b in contenders if b["verdict"] in ("Beats buy and hold", "Works")), None)

        names = {s["symbol"]: s.get("name") or s["symbol"] for s in self.stocks}
        bh_oos = {r["symbol"]: r.get("oos_return_pct") for r in self.by_id[bh["id"]]}
        win_oos = {r["symbol"]: r.get("oos_return_pct") for r in self.by_id.get(winner["id"], [])} if winner else {}
        plain = {c["id"] for c in self.cands if c["strategy"] != BASELINE and c["exits"] == (0.0, 0.0)}
        per_stock = []
        for symbol in self.scanned:
            mine = [r for r in self.rows if r["symbol"] == symbol and r["id"] in plain and not r.get("error")]
            fit = [r for r in mine if r["is_trades"] >= 3] or mine
            best = max(fit, key=lambda r: r["is_sharpe"] if r["is_sharpe"] is not None else -9) if fit else None
            cand = self.cands[best["id"]] if best else None
            per_stock.append({
                "symbol": symbol.removesuffix(".NS"), "name": names.get(symbol, symbol),
                "best": STRATEGIES[cand["strategy"]].name if cand else "",
                "best_settings": describe_params(cand["strategy"], cand["params"]) if cand else "",
                "best_is_return_pct": best["is_return_pct"] if best else None,
                "best_oos_return_pct": best["oos_return_pct"] if best else None,
                "winner_oos_return_pct": win_oos.get(symbol), "bh_oos_return_pct": bh_oos.get(symbol)})

        runs = []
        for r in self.rows:
            if r.get("error"):
                continue
            c = self.cands[r["id"]]
            runs.append({**{k: v for k, v in r.items() if k not in ("id", "error", "unfunded")},
                         "symbol": r["symbol"].removesuffix(".NS"), "strategy": STRATEGIES[c["strategy"]].name,
                         "settings": describe_params(c["strategy"], c["params"]), "exits": describe_exits(c["exits"])})
        days, split = self.days, self.split
        out = {
            "index": index_name, "start": self.start, "end": self.end, "cut": str(self.cut.date()), "oos_pct": self.oos_pct,
            "depth": self.depth, "depth_text": DEPTHS[self.depth][0], "capital": self.params.capital,
            "money_per_stock": round(self.per_stock, 2), "slippage_bps": self.params.slippage_bps,
            "stocks_asked": len(self.stocks), "stocks_tested": len(self.scanned), "skipped": self.skipped,
            "sources": self.sources, "stopped": self.stopped_early, "trials": len(runs),
            "settings_tried": len(self.cands), "strategies_compared": len(contenders), "luck_trials": luck_trials,
            "period": {"in_sample": [str(days[0].date()), str(days[split - 1].date())],
                       "unseen": [str(days[split].date()), str(days[-1].date())]},
            "ranking": [_public(b) for b in contenders], "buy_hold": _public(bh),
            "winner": _public(winner) if winner else None, "exit_trials": self.exit_rows,
            "per_stock": per_stock, "runs": runs,
            "equity": {"days": [d.strftime("%Y-%m-%d") for d in days], "cut": split,
                       "winner": [round(float(v), 2) for v in winner["_values"]] if winner else None,
                       "buy_hold": [round(float(v), 2) for v in bh["_values"]]},
        }
        out["explanation"] = explain(out)
        return out


def scan(stocks: list[dict], load: Callable[[str], tuple[pd.DataFrame, str]], params: BacktestParams, *,
         start: str, end: str, depth: str = "standard", oos_pct: float = 30.0, index_name: str = "",
         log: Callable[[str], None] = lambda _m: None, progress: Callable[[int, int], None] = lambda _d, _t: None,
         stopped: Callable[[], bool] = lambda: False, workers: int = 1) -> dict:
    """The whole scan. ``stocks`` are ``{"symbol", "name"}`` rows; ``load(symbol)`` returns day candles and the
    name of their source, or raises. ``params.capital`` is split equally between the stocks."""
    if not stocks:
        raise ValueError("No stocks to scan")
    if len(stocks) > MAX_STOCKS:
        raise ValueError(f"{len(stocks)} stocks is more than {MAX_STOCKS}. Pick a smaller index.")
    if not 10 <= oos_pct <= 50:
        raise ValueError("Keep between 10% and 50% of the period unseen")
    if depth not in DEPTHS:
        raise ValueError(f"depth must be one of {', '.join(DEPTHS)}")
    per_stock = params.capital / len(stocks)
    if per_stock < MIN_STOCK_CAPITAL:
        raise ValueError(f"₹{params.capital:,.0f} split across {len(stocks)} stocks is ₹{per_stock:,.0f} each. "
                         f"Use at least ₹{MIN_STOCK_CAPITAL * len(stocks):,.0f} (₹{MIN_STOCK_CAPITAL:,.0f} a stock).")
    params.validate()
    job = _Scan(stocks, params, start, end, depth, oos_pct, log, stopped, workers)
    log(f"{len(job.cands)} strategy settings × {len(stocks)} stocks = {len(job.cands) * len(stocks):,} backtests. "
        f"Settings are chosen on {pd.Timestamp(start):%d %b %Y} to {job.cut - pd.Timedelta(days=1):%d %b %Y}; "
        f"strategies are judged on {job.cut:%d %b %Y} onwards.")
    job.run(load, progress)
    return job.result(index_name)


def _verdict(b: dict, bh: dict) -> str:
    unseen = b["unseen"]
    if unseen["return_pct"] is None or unseen["return_pct"] <= 0:
        return "Loses money"
    if b["oos_trades"] < MIN_TRADES:
        return "Too few trades"
    if (unseen["sharpe"] or 0) <= 0:
        return "Below a fixed deposit"
    if b.get("robust") is not None and b["robust"] < ROBUST_PASS:
        return "Fragile"
    if b.get("luck_test") is None or b["luck_test"] < DSR_PASS:
        return "Could be luck"
    if (unseen["sharpe"] or 0) > (bh["unseen"]["sharpe"] or 0):
        return "Beats buy and hold"
    return "Works"


def _chance(p: float) -> str:
    return "over 99%" if p >= 0.995 else f"{p * 100:.0f}%"


def _public(b: dict) -> dict:
    return {k: v for k, v in b.items() if not k.startswith("_") and k != "axes"}


def explain(r: dict) -> str:
    """The result in plain words, from the numbers only (nothing is generated by a language model)."""
    bh = r["buy_hold"]["unseen"]
    lines = [f"Tested {r['strategies_compared']} strategies ({r['settings_tried']} settings in all) on "
             f"{r['stocks_tested']} of {r['stocks_asked']} {r['index'] or 'index'} stocks: {r['trials']:,} backtests "
             f"after charges and {r['slippage_bps']:g} basis points of slippage. Settings were chosen on "
             f"{r['period']['in_sample'][0]} to {r['period']['in_sample'][1]}; strategies were judged only on "
             f"{r['period']['unseen'][0]} to {r['period']['unseen'][1]}, which the choice never saw."]
    w = r["winner"]
    if w:
        u = w["unseen"]
        lines.append(f"Winner: {w['name']} ({w['settings']}; {w['exits_text'].lower()}). On the unseen part, run on "
                     f"every stock with equal money, it returned {u['return_pct']:+.1f}% ({u['cagr_pct']:+.1f}% a year) "
                     f"with a worst fall of {u['max_drawdown_pct']:.1f}%, against {bh['return_pct']:+.1f}% "
                     f"({bh['cagr_pct']:+.1f}% a year, worst fall {bh['max_drawdown_pct']:.1f}%) for buying and holding the "
                     f"same stocks. It made money on {w['stocks_profitable']} of {w['stocks']} stocks over "
                     f"{w['oos_trades']} unseen trades. Chance it is not luck: {_chance(w['luck_test'])}.")
        if w["verdict"] == "Works":
            lines.append("It did not beat buy and hold on risk-adjusted return; its case is a smaller worst fall or "
                         "time out of the market, not a higher return.")
    else:
        lines.append(f"No strategy passed all checks on the unseen part. Buying and holding the same stocks returned "
                     f"{bh['return_pct']:+.1f}% there. Treat the ranking as research, not as a strategy to trade.")
    lucky = [b["name"] for b in r["ranking"][:5] if b["verdict"] in ("Could be luck", "Fragile")]
    if lucky:
        lines.append(f"Looked good but failed a check: {', '.join(lucky)}. These are the results a plain optimiser "
                     "would have shown as winners.")
    lines.append("Caveats: today's index members are the stocks that did well enough to be in it, so every past "
                 "result is flattered (survivorship). Past results do not promise future ones. Taxes on gains are not "
                 "deducted. Paper trade a strategy before risking money.")
    if r["skipped"]:
        lines.append(f"{len(r['skipped'])} stocks were left out (see the Notes sheet).")
    if r["stopped"]:
        lines.append("The scan was stopped early: these results cover only the stocks tested.")
    return "\n\n".join(lines)


__all__ = ["DEPTHS", "PriceCache", "VERDICTS", "candidates", "deflated_sharpe", "explain", "run_stock",
           "scan", "segment_stats", "strategy_keys"]
