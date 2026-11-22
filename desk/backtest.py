"""Strategy backtest on daily bars, with Indian delivery charges.

How a run trades, so its figures can be read for what they are:

- A signal formed on bar ``t``'s close is acted on at bar ``t+1``'s open.
- Trade type. **Intraday** (intraday candles only, MIS charges): entries only
  inside the trade window, and whatever is open is sold at the square-off time;
  nothing is carried overnight. **Positional** (any candle; day candles are always
  positional): no trade window and no square-off, the position is held across
  days until the strategy, the stoploss, the target or the holding limit closes
  it, with delivery (CNC) charges. A positional trade bought and sold the same
  day is charged at intraday rates, as the exchange treats it.
- An optional holding limit (``max_hold_days``) sells a positional trade at the
  open of the given trading day after the buy: 1 is the next session.
- Fills are at the open, worse by ``slippage_bps``; a stoploss or target fills
  at its level, or at the open when the day gaps through it. When a day's range
  covers both the stoploss and the target, the stoploss is taken.
- Exit rules (``risk.ExitRules``): stoploss and target in % or points, a
  trailing stoploss that moves in steps, stoploss to cost, and re-entry after a
  stoploss. The trailing stoploss follows highs up to the previous candle.
- A re-entry "when price comes back" fills at the earlier entry price when a
  candle's high reaches it (at the open if it opens above). On that candle the
  exits are checked on its close only: the order of its high and low is unknown.
- Long only, whole shares, one position.
- Prices are adjusted for splits and bonuses (Yahoo's for dividends too).
- Each trade records how far it fell and rose from its entry (``worst_pct``, ``best_pct``), and
  ``metrics.exits`` says whether the stoploss and target were ever within reach.

What it leaves out: liquidity and impact, circuit-limit days when an order
cannot fill, taxes on gains, and survivorship (a delisted stock has no
history to test). Results are an upper bound on what the strategy earned.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd

from .charges import DEFAULT_SCHEDULE, ChargeSchedule, order_charges
from .risk import ExitRules
from .strategies import STRATEGIES

TRADING_DAYS = 252


@dataclass
class BacktestParams:
    capital: float = 1_000_000.0
    allocation_pct: float = 100.0      # share of equity put into each position
    stop_loss_pct: float = 0.0         # 0 disables; used when ``exits`` is not given
    target_pct: float = 0.0            # 0 disables; used when ``exits`` is not given
    trailing_stop_pct: float = 0.0     # 0 disables; trails the highest high since entry (older setting)
    exits: ExitRules | None = None     # stoploss, target, trailing, stoploss to cost, re-entry
    slippage_bps: float = 5.0
    risk_free_rate_pct: float = 6.5
    exchange: str = "NSE"
    intraday: bool = False             # bars are intraday candles
    positional: bool = False           # hold across days (CNC); intraday candles only, day candles always are
    max_hold_days: int = 0             # positional: sell at the open this many trading days after the buy; 0 = no limit
    trade_start: str = "09:30"         # first entry of the day (intraday trades)
    trade_end: str = "15:00"           # no entries from here on (intraday trades)
    square_off: str = "15:00"          # open intraday positions are closed at this time

    @property
    def holds_overnight(self) -> bool:
        return self.positional or not self.intraday

    @property
    def trade_type(self) -> str:
        return "positional" if self.holds_overnight else "intraday"

    def rules(self) -> ExitRules:
        if self.exits is not None:
            return self.exits
        return ExitRules(unit="pct", stop_loss=self.stop_loss_pct, target=self.target_pct)

    def as_dict(self) -> dict:
        out = {k: v for k, v in self.__dict__.items() if k != "exits"}
        out["exits"] = self.rules().as_dict()
        out["trade_type"] = self.trade_type
        return out

    def validate(self) -> None:
        self.rules().validate("Stock")
        if not self.capital > 0 or not math.isfinite(self.capital):
            raise ValueError("Capital must be a positive amount")
        if not 0 < self.allocation_pct <= 100:
            raise ValueError("Allocation must be between 0 and 100%")
        for label, value, top in (("Stoploss", self.stop_loss_pct, 90), ("Target", self.target_pct, 1000),
                                  ("Trailing stop", self.trailing_stop_pct, 90),
                                  ("Slippage", self.slippage_bps, 500),
                                  ("Risk-free rate", self.risk_free_rate_pct, 50)):
            if not 0 <= value <= top or not math.isfinite(value):
                raise ValueError(f"{label} must be between 0 and {top}")
        if self.exchange not in ("NSE", "BSE"):
            raise ValueError("Exchange must be NSE or BSE")
        if isinstance(self.max_hold_days, bool) or not isinstance(self.max_hold_days, int) \
                or not 0 <= self.max_hold_days <= 2000:
            raise ValueError("Sell after (days held) must be a whole number of days from 0 (no limit) to 2000")
        if self.max_hold_days and not self.holds_overnight:
            raise ValueError("A holding limit in days needs a positional trade: intraday trades close the same day")
        if self.holds_overnight:
            return                     # no trade window: the times are not used
        start, end, close = (_clock(t) for t in (self.trade_start, self.trade_end, self.square_off))
        if not start < end <= close:
            raise ValueError("Trade window must start before it ends, and end by the square-off time")


def _clock(text: str):
    try:
        return pd.Timestamp(f"2000-01-01 {text}").time()
    except (ValueError, TypeError):
        raise ValueError(f"{text!r} is not a time as HH:MM") from None


def _max_quantity(budget: float, price: float, exchange: str, schedule: ChargeSchedule,
                  product: str = "CNC") -> int:
    """The most whole shares ``budget`` buys at ``price`` with charges paid."""
    qty = int(budget // price)
    while qty > 0:
        cost = qty * price + order_charges("BUY", qty, price, product, exchange, schedule).total
        if cost <= budget:
            break
        qty -= 1
    return qty


def run_strategy(bars: pd.DataFrame, strategy, strategy_params: dict | None = None,
                 params: BacktestParams | None = None, benchmark: pd.Series | None = None,
                 schedule: ChargeSchedule = DEFAULT_SCHEDULE) -> dict:
    """Backtest one strategy on ``bars`` (Open/High/Low/Close, time index).

    ``strategy`` is a Strategy or the key of a built-in one.
    """
    params = params or BacktestParams()
    params.validate()
    if isinstance(strategy, str):
        if strategy not in STRATEGIES:
            raise ValueError(f"unknown strategy: {strategy}")
        strategy = STRATEGIES[strategy]
    resolved = strategy.resolve(strategy_params)
    if len(bars) < 30:
        raise ValueError(f"only {len(bars)} candles in the range; a backtest needs at least 30")
    if strategy.intraday_only and not params.intraday:
        raise ValueError(f"{strategy.name} needs intraday candles. Choose a minute-based or 1-hour interval.")
    same_day_only = not params.holds_overnight           # intraday trade type: MIS, window, square-off
    product = "MIS" if same_day_only else "CNC"
    stamp = "%Y-%m-%d %H:%M" if params.intraday else "%Y-%m-%d"
    times = bars.index.time if params.intraday else None
    days = bars.index.normalize() if params.intraday else None
    # The trading session each bar falls in, counted from 0: what the holding limit counts.
    sessions = (np.cumsum(np.r_[True, days[1:] != days[:-1]]) - 1) if params.intraday else np.arange(len(bars))
    max_hold = params.max_hold_days if params.holds_overnight else 0
    if same_day_only:
        window_start, window_end, close_at = (_clock(t) for t in
                                              (params.trade_start, params.trade_end, params.square_off))

    target = strategy.positions(bars, resolved).to_numpy()
    opens, highs, lows, closes = (bars[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    dates = bars.index
    slip = params.slippage_bps / 10_000
    rules = params.rules()
    trail_frac = params.trailing_stop_pct / 100
    peak = 0.0                # highest high since entry, as known before the current bar

    cash, qty = params.capital, 0
    entry_price = entry_cost = 0.0
    entry_index = -1
    blocked = False          # after a stoploss/target exit, wait for the signal to reset
    rearm = None             # a re-entry waiting: {"price": earlier entry, "left": re-entries still allowed}
    reentries_left = 0
    total_charges = 0.0
    trades: list[dict] = []
    equity = np.empty(len(bars))
    in_market = 0
    reentries = 0
    reentry_trade = False    # the open position is a re-entry after a stoploss
    worst = best = 0.0       # the open position's lowest low and highest high since entry

    def close_trade(i: int, price: float, reason: str) -> None:
        nonlocal cash, qty, total_charges, entry_cost
        if product == "CNC" and sessions[i] == sessions[entry_index]:
            # Bought and sold in one session: the exchange nets it as an intraday trade, so both legs pay
            # intraday rates (no delivery STT or DP charge). The buy was charged as delivery: refund the difference.
            paid = order_charges("BUY", qty, entry_price, "CNC", params.exchange, schedule).total
            due = order_charges("BUY", qty, entry_price, "MIS", params.exchange, schedule).total
            cash += paid - due
            total_charges -= paid - due
            entry_cost -= paid - due
            fees = order_charges("SELL", qty, price, "MIS", params.exchange, schedule).total
        else:
            fees = order_charges("SELL", qty, price, product, params.exchange, schedule).total
        proceeds = qty * price - fees
        cash += proceeds
        total_charges += fees
        pnl = proceeds - entry_cost
        trades.append({
            "entry_date": dates[entry_index].strftime(stamp),
            "exit_date": dates[i].strftime(stamp),
            "quantity": qty, "entry_price": round(entry_price, 2), "exit_price": round(price, 2),
            "pnl": round(float(pnl), 2), "return_pct": round(float(pnl / entry_cost * 100), 2),
            "days": int((dates[i] - dates[entry_index]).days), "exit": reason, "open": False,
            "reentry": reentry_trade, **_excursion(entry_price, min(worst, price), max(best, price)),
        })
        qty = 0

    def buy(i: int, price: float, again: bool = False) -> bool:
        nonlocal cash, qty, entry_price, entry_cost, entry_index, total_charges, peak, reentry_trade, worst, best
        shares = _max_quantity(min(cash, cash * params.allocation_pct / 100), price,
                               params.exchange, schedule, product)
        if shares <= 0:
            return False
        fees = order_charges("BUY", shares, price, product, params.exchange, schedule).total
        entry_cost = shares * price + fees
        cash -= entry_cost
        total_charges += fees
        qty, entry_price, entry_index = shares, price, i
        peak = worst = best = price
        reentry_trade = again
        return True

    def exit_levels():
        stop, kind, goal = rules.levels(entry_price, peak, 1)
        if trail_frac:
            # The older trailing stop: a set % below the highest high so far.
            trail = peak * (1 - trail_frac)
            if stop is None or trail > stop:
                stop, kind = trail, "trailing stoploss"
        return stop, kind, goal

    for i in range(len(bars)):
        want = target[i - 1] if i > 0 else 0
        if want == 0:
            blocked, rearm = False, None
        held_before = qty > 0
        may_enter = True
        if same_day_only:
            new_day = i == 0 or days[i] != days[i - 1]
            if new_day:
                blocked, rearm = False, None
                if held_before:                      # never carried overnight: sold at the last close
                    close_trade(i - 1, closes[i - 1] * (1 - slip), "square-off")
                    equity[i - 1] = cash
                    held_before = False
            if held_before and times[i] >= close_at:
                close_trade(i, opens[i] * (1 - slip), "square-off")
                held_before = False
            may_enter = window_start <= times[i] < window_end

        # 0. The holding limit: sold at this session's first open once the trade has been held long enough.
        if held_before and max_hold and sessions[i] - sessions[entry_index] >= max_hold:
            close_trade(i, opens[i] * (1 - slip), f"held {max_hold} day{'s' if max_hold > 1 else ''}")
            held_before, blocked, rearm = False, True, None

        # 1. Act on the last bar's signal at this bar's open, or re-enter after a stoploss.
        close_only = False
        if held_before and want == 0:
            close_trade(i, opens[i] * (1 - slip), "signal")
        elif not held_before and qty == 0 and want == 1 and may_enter and rearm is not None:
            level = rearm["price"]
            fill = None
            if rules.reentry_mode == "at_once" or opens[i] >= level:
                fill = opens[i]
            elif highs[i] >= level:
                fill, close_only = level, True
            if fill is not None and buy(i, fill * (1 + slip), again=True):
                reentries_left, rearm = rearm["left"] - 1, None
                reentries += 1
        elif not held_before and qty == 0 and want == 1 and not blocked and may_enter and buy(i, opens[i] * (1 + slip)):
            reentries_left = rules.reentry

        # 2. Stoploss and target on a position held through today.
        if qty > 0 and (rules.active or trail_frac):
            stop, kind, goal = exit_levels()
            gapped = held_before      # an entry made at this open cannot gap past its own levels
            low, high = (closes[i], closes[i]) if close_only else (lows[i], highs[i])
            if stop is not None and low <= stop:
                fill = min(stop, opens[i]) if gapped else stop
                close_trade(i, fill * (1 - slip), kind)
                blocked = True
                if kind == "stoploss" and reentries_left > 0:
                    rearm = {"price": entry_price / (1 + slip), "left": reentries_left}
            elif goal is not None and high >= goal:
                fill = max(goal, opens[i]) if gapped else goal
                close_trade(i, fill * (1 - slip), "target")
                blocked = True

        if qty > 0:
            peak = max(peak, highs[i])
            worst, best = min(worst, closes[i] if close_only else lows[i]), max(best, highs[i])
            in_market += 1
        equity[i] = cash + qty * closes[i]

    if qty > 0:     # still holding: marked at the last close, exit charges not yet paid
        value = qty * closes[-1]
        trades.append({
            "entry_date": dates[entry_index].strftime(stamp), "exit_date": None,
            "quantity": qty, "entry_price": round(entry_price, 2), "exit_price": round(closes[-1], 2),
            "pnl": round(value - entry_cost, 2), "return_pct": round((value - entry_cost) / entry_cost * 100, 2),
            "days": int((dates[-1] - dates[entry_index]).days), "exit": "open", "open": True,
            "reentry": reentry_trade, **_excursion(entry_price, worst, best),
        })

    curve = pd.Series(equity, index=dates)
    hold = params.capital * bars["Close"] / bars["Close"].iloc[0]
    if params.intraday:        # statistics and the chart are read day by day
        curve, hold = curve.groupby(days).last(), hold.groupby(days).last()
        in_market = int(round(in_market / len(bars) * len(curve)))
        dates = curve.index
    out = {
        "strategy": {"key": strategy.key, "name": strategy.name, "params": resolved},
        "params": params.as_dict(),
        "period": {"start": dates[0].strftime("%Y-%m-%d"), "end": dates[-1].strftime("%Y-%m-%d"),
                   "bars": len(bars), "days": len(curve)},
        "trade_type": params.trade_type,
        "metrics": {**_metrics(curve, trades, params, total_charges, in_market), "reentries": reentries,
                    "exits": exit_summary(trades, rules)},
        "buy_hold": _curve_stats(hold, params),
        "trades": trades,
        "equity": _points(curve),
        "buy_hold_equity": _points(hold),
        "benchmark_equity": None, "benchmark": None,
    }
    if benchmark is not None:
        bench = benchmark.reindex(dates).ffill().dropna()
        if len(bench) >= 2 and bench.index[0] <= dates[min(5, len(dates) - 1)]:
            scaled = params.capital * bench / bench.iloc[0]
            out["benchmark_equity"] = _points(scaled)
            out["benchmark"] = _curve_stats(scaled, params)
    return out


def _excursion(entry: float, worst: float, best: float) -> dict:
    """How far a trade went against and for its entry while open, in % of the entry price."""
    return {"worst_pct": round((worst / entry - 1) * 100, 2), "best_pct": round((best / entry - 1) * 100, 2)}


def exit_summary(trades: list[dict], rules: ExitRules) -> dict:
    """Closed trades by exit reason, and whether the stoploss and target could ever have been reached: the
    deepest fall and highest rise any trade saw from its entry. A 15% stoploss on trades that never fell more
    than 3% changes nothing, and the page says so."""
    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t["exit"]] = reasons.get(t["exit"], 0) + 1
    out = {"by_reason": reasons, "rules": rules.text(), "unit": rules.unit,
           "stop_loss": rules.stop_loss, "target": rules.target,
           "deepest_fall_pct": min((t["worst_pct"] for t in trades), default=None),
           "highest_rise_pct": max((t["best_pct"] for t in trades), default=None)}
    stops = sum(n for k, n in reasons.items() if "stoploss" in k)
    out["stop_unused"] = bool(rules.stop_loss) and stops == 0
    out["target_unused"] = bool(rules.target) and reasons.get("target", 0) == 0
    return out


def _points(series: pd.Series) -> list:
    return [[d.strftime("%Y-%m-%d"), round(float(v), 2)] for d, v in series.items()]


def _curve_stats(curve: pd.Series, params: BacktestParams) -> dict:
    start, end = float(curve.iloc[0]), float(curve.iloc[-1])
    base = params.capital if curve.iloc[0] == params.capital else start
    years = max((curve.index[-1] - curve.index[0]).days, 1) / 365.25
    total = end / base - 1
    cagr = (end / base) ** (1 / years) - 1 if end > 0 else -1.0
    returns = curve.pct_change().dropna()
    rf_daily = params.risk_free_rate_pct / 100 / TRADING_DAYS
    excess = returns - rf_daily
    sd = returns.std(ddof=1)
    sharpe = float(excess.mean() / sd * math.sqrt(TRADING_DAYS)) if sd and sd > 0 else None
    downside = math.sqrt(float((np.minimum(excess, 0) ** 2).mean())) if len(excess) else 0.0
    sortino = float(excess.mean() / downside * math.sqrt(TRADING_DAYS)) if downside > 0 else None
    peak = curve.cummax()
    drawdown = curve / peak - 1
    trough = drawdown.idxmin()
    return {
        "final_value": round(end, 2), "total_return_pct": round(total * 100, 2),
        "cagr_pct": round(cagr * 100, 2),
        "volatility_pct": round(float(sd) * math.sqrt(TRADING_DAYS) * 100, 2) if sd == sd else None,
        "sharpe": None if sharpe is None else round(sharpe, 2),
        "sortino": None if sortino is None else round(sortino, 2),
        "max_drawdown_pct": round(float(drawdown.min()) * 100, 2),
        "max_drawdown_date": trough.strftime("%Y-%m-%d"),
    }


def _metrics(curve, trades, params, total_charges, in_market) -> dict:
    stats = _curve_stats(curve, params)
    closed = [t for t in trades if not t["open"]]
    wins = [t["pnl"] for t in closed if t["pnl"] > 0]
    losses = [t["pnl"] for t in closed if t["pnl"] <= 0]
    gross_loss = -sum(losses)
    stats = {k: (float(v) if isinstance(v, (np.floating, float)) else v) for k, v in stats.items()}
    stats.update({
        "trades": len(closed), "open_trades": len(trades) - len(closed),
        "win_rate_pct": round(len(wins) / len(closed) * 100, 1) if closed else None,
        "profit_factor": round(float(sum(wins) / gross_loss), 2) if gross_loss > 0 else None,
        "avg_win": round(float(sum(wins) / len(wins)), 2) if wins else None,
        "avg_loss": round(float(sum(losses) / len(losses)), 2) if losses else None,
        "avg_days_held": round(sum(t["days"] for t in closed) / len(closed), 1) if closed else None,
        "exposure_pct": round(in_market / len(curve) * 100, 1),
        "total_charges": round(float(total_charges), 2),
    })
    years = max((curve.index[-1] - curve.index[0]).days, 1) / 365.25
    stats["checks"] = checks([float(t["pnl"]) for t in closed], params.capital, float(curve.iloc[-1]), years,
                             params.risk_free_rate_pct)
    return stats


MIN_TRADES = 30          # fewer closed trades than this and the result says little about the strategy
MONTE_CARLO_RUNS = 1000


def checks(pnls: list[float], capital: float, final: float, years: float, safe_rate_pct: float,
           runs: int = MONTE_CARLO_RUNS, seed: int = 7) -> dict:
    """How far to trust a backtest: enough trades, better than a safe deposit, and what other orders of the
    same trades would have done (Monte Carlo: the trades drawn again at random, with replacement, ``runs``
    times). The draws keep each trade's rupee result, so they ignore that a strategy sizes from its capital."""
    safe_final = capital * (1 + safe_rate_pct / 100) ** years
    out = {"trades": len(pnls), "min_trades": MIN_TRADES, "few_trades": len(pnls) < MIN_TRADES,
           "safe_rate_pct": safe_rate_pct, "safe_final": round(safe_final, 2), "beats_safe": final > safe_final}
    if len(pnls) < 5 or capital <= 0:
        return out
    rng = np.random.default_rng(seed)
    draws = rng.choice(np.asarray(pnls, dtype=float), size=(runs, len(pnls)), replace=True)
    equity = capital + np.cumsum(draws, axis=1)
    peaks = np.maximum.accumulate(np.concatenate([np.full((runs, 1), capital), equity], axis=1), axis=1)[:, 1:]
    worst = ((equity - peaks) / peaks).min(axis=1) * 100
    finals = equity[:, -1]
    pct = lambda values, q: round(float(np.percentile(values, q)), 2)  # noqa: E731
    out["monte_carlo"] = {
        "runs": runs,
        "drawdown_pct": {"median": pct(worst, 50), "bad": pct(worst, 5)},       # 5th percentile: 1 run in 20 worse
        "final": {"low": pct(finals, 5), "median": pct(finals, 50), "high": pct(finals, 95)},
        "loss_chance_pct": round(float((finals < capital).mean() * 100), 1),
        "below_safe_pct": round(float((finals < safe_final).mean() * 100), 1),
    }
    return out
