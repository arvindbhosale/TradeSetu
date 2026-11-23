"""Multi-leg index option strategies, backtested on simulated premiums.

Kite keeps no candles for expired option contracts, so, as in the options
buying backtest, every leg is priced with Black-Scholes from the index's real
candles and India VIX (× a factor). Real premiums differ: puts below the money
trade at higher volatility than calls (skew), volatility collapses after
events, and far strikes have wide spreads. Results show how a structure
behaves, not what it would have earned.

Three ways to run a structure:

- ``intraday``: each chosen weekday, open the legs at the entry time on 1-15
  minute candles and close them at the exit time. A stop on each short leg
  (``leg_sl_pct`` of its premium) buys that leg back on its own, the way the
  common "straddle with stoploss" is traded; the other leg stays.
- ``positional``: day candles; open on a chosen weekday, close a given number
  of days before expiry or settle at expiry.
- ``signal``: day candles; open while a strategy is long, close when it turns
  flat (directional spreads, protective put, covered call). A position still
  on at expiry is rolled to the next expiry.

Margin: a short leg covered by a long leg further out needs about its maximum
loss; uncovered short options and futures need ``margin_pct`` of the
contract value (SPAN plus exposure is about 10-14% for index options sold as
a straddle or strangle). Bought options are paid in full, less the premium
the sold ones bring in (not for calendars and diagonals). Entries the equity
cannot margin are skipped.

Legs may carry a ratio (two lots of one leg per lot of the position, as in
ratio spreads and butterflies) and a later expiry (calendars, diagonals): the
far leg is sold back at its model price when the near expiry settles.

Book profit in steps (``steps``, ``scaleout.Steps``): a lot of the position is one lot of every leg (``ratio`` lots of
a leg marked ×2), so a step closes the bought and the sold legs together and never leaves a sold leg uncovered.
Its level is the open position's profit at a candle's close, in rupees per lot or in % of its premium (received or
paid; the margin when that is nil), checked like the whole-position stop and target.

``analyse`` works out one structure now, without a backtest: premiums, the
payoff at the near expiry and today, breakevens, maximum profit and loss,
chance of profit, Greeks and margin.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .backtest import BacktestParams, _curve_stats, _metrics, _points
from .charges import futures_charges
from .fno import BSE_INDICES, exchange_of
from .futures import monthly_expiries
from .options import RATE, STRIKE_STEP, bs_greeks, bs_price, implied_vol, option_charges
from .risk import ExitRules
from .scaleout import NONE as NO_STEPS, Steps

# (action, type, offset[, ratio[, series]]): action +1 buy / -1 sell; offset in strikes from the money, k = short
# distance, w = wing; ratio = lots of this leg per lot of the position (default 1); series "far" = the expiry after the
# near one (calendars, diagonals), otherwise the near expiry.
STRUCTURES = {
    "short_straddle": {"name": "Short straddle", "legs": [(-1, "CE", "0"), (-1, "PE", "0")], "markets": ("sideways",),
                       "about": "Sell the at-the-money call and put. Earns the time value when the index stays near the strike; "
                                "loses fast on a big move either way. Unlimited risk without wings."},
    "short_straddle_hedged": {"name": "Short straddle with wings (hedged)", "w": 9,
                              "legs": [(-1, "CE", "0"), (-1, "PE", "0"), (1, "CE", "+w"), (1, "PE", "-w")], "markets": ("sideways",),
                              "about": "The straddle plus cheap far calls and puts bought as insurance. Caps the loss and cuts "
                                       "the margin a lot, for a smaller credit."},
    "short_strangle": {"name": "Short strangle", "legs": [(-1, "CE", "+k"), (-1, "PE", "-k")], "markets": ("sideways",),
                       "about": "Sell an out-of-the-money call and put. Wider profit range than a straddle, smaller credit, "
                                "same unlimited tail risk."},
    "iron_condor": {"name": "Iron condor", "legs": [(-1, "CE", "+k"), (1, "CE", "+k+w"), (-1, "PE", "-k"), (1, "PE", "-k-w")],
                    "markets": ("sideways",), "about": "A short strangle with wings: defined risk, defined reward, low margin."},
    "iron_fly": {"name": "Iron butterfly", "legs": [(-1, "CE", "0"), (-1, "PE", "0"), (1, "CE", "+w"), (1, "PE", "-w")],
                 "markets": ("sideways",), "about": "A short straddle with wings at a set distance: larger credit than a condor, "
                                                    "narrower profit zone, defined risk."},
    "long_straddle": {"name": "Long straddle", "legs": [(1, "CE", "0"), (1, "PE", "0")], "markets": ("bull", "bear"),
                      "about": "Buy the at-the-money call and put: pays on a big move either way (results, budget, elections); "
                               "loses the time value when nothing happens."},
    "long_strangle": {"name": "Long strangle", "legs": [(1, "CE", "+k"), (1, "PE", "-k")], "markets": ("bull", "bear"),
                      "about": "Cheaper than a long straddle; needs an even bigger move."},
    "bull_call_spread": {"bias": "bull", "name": "Bull call spread", "legs": [(1, "CE", "0"), (-1, "CE", "+w")], "markets": ("bull",),
                         "about": "Buy a call, sell a higher one: a cheaper bullish bet with capped profit and loss."},
    "bull_put_spread": {"bias": "bull", "name": "Bull put spread (credit)", "legs": [(-1, "PE", "0"), (1, "PE", "-w")], "markets": ("bull", "sideways"),
                        "about": "Sell a put, buy a lower one: earns the credit if the index holds up."},
    "bear_put_spread": {"bias": "bear", "name": "Bear put spread", "legs": [(1, "PE", "0"), (-1, "PE", "-w")], "markets": ("bear",),
                        "about": "Buy a put, sell a lower one: a cheaper bearish bet with capped profit and loss."},
    "bear_call_spread": {"bias": "bear", "name": "Bear call spread (credit)", "legs": [(-1, "CE", "0"), (1, "CE", "+w")], "markets": ("bear", "sideways"),
                         "about": "Sell a call, buy a higher one: earns the credit if the index stays down."},
    "protective_put": {"bias": "bull", "name": "Protective put (futures + put)", "legs": [(1, "FUT", "0"), (1, "PE", "-k")], "markets": ("bull",),
                       "about": "Long futures hedged with a put: bull-market upside, crash losses capped below the put strike."},
    "covered_call": {"bias": "bull", "name": "Covered call (futures + short call)", "legs": [(1, "FUT", "0"), (-1, "CE", "+k")],
                     "markets": ("bull", "sideways"),
                     "about": "Long futures with a call sold above: extra income in quiet or slowly rising markets, upside capped."},
    # -- added: guts, butterflies, reverse condor/fly, ratios, calendars, diagonals, strap/strip, collar, synthetics --
    "short_guts": {"name": "Short guts", "min_k": 1, "legs": [(-1, "CE", "-k"), (-1, "PE", "+k")], "markets": ("sideways",),
                   "about": "Sell an in-the-money call and an in-the-money put (call strike below the money, put strike above). "
                            "A short strangle with the strikes swapped: bigger credit, same profit zone width, unlimited risk."},
    "jade_lizard": {"name": "Jade lizard", "legs": [(-1, "PE", "-k"), (-1, "CE", "+k"), (1, "CE", "+k+w")], "markets": ("sideways", "bull"),
                    "about": "Sell an OTM put and a bear call spread above. When the credit is larger than the call spread's width "
                             "there is no risk on the upside; the risk is a fall below the put."},
    "long_call_butterfly": {"name": "Long call butterfly", "legs": [(1, "CE", "-w"), (-1, "CE", "0", 2), (1, "CE", "+w")],
                            "markets": ("sideways",),
                            "about": "Buy a lower call, sell two at the money, buy a higher call. Small debit, best when the index "
                                     "expires at the middle strike; loss capped at the debit."},
    "long_put_butterfly": {"name": "Long put butterfly", "legs": [(1, "PE", "+w"), (-1, "PE", "0", 2), (1, "PE", "-w")],
                           "markets": ("sideways",),
                           "about": "The same payoff built with puts: buy a higher put, sell two at the money, buy a lower put."},
    "long_call_calendar": {"name": "Long call calendar", "legs": [(-1, "CE", "0"), (1, "CE", "0", 1, "far")], "markets": ("sideways",),
                           "about": "Sell this expiry's ATM call, buy the next expiry's call at the same strike. The near call decays "
                                    "faster; best when the index is at the strike on the near expiry. A rise in volatility helps."},
    "long_put_calendar": {"name": "Long put calendar", "legs": [(-1, "PE", "0"), (1, "PE", "0", 1, "far")], "markets": ("sideways",),
                          "about": "The calendar built with puts: sell the near ATM put, buy the next expiry's put at the same strike."},
    "call_ratio_spread": {"name": "Call ratio spread (1×2)", "legs": [(1, "CE", "0"), (-1, "CE", "+w", 2)], "markets": ("sideways", "bull"),
                          "about": "Buy one ATM call, sell two higher calls. Profits most at the short strike; a strong rally above "
                                   "the upper breakeven loses without limit."},
    "put_ratio_spread": {"name": "Put ratio spread (1×2)", "legs": [(1, "PE", "0"), (-1, "PE", "-w", 2)], "markets": ("sideways", "bear"),
                         "about": "Buy one ATM put, sell two lower puts. Profits most at the short strike; a crash below the lower "
                                  "breakeven loses heavily."},
    "long_straddle_strap": {"name": "Strap (2 calls + 1 put)", "legs": [(1, "CE", "0", 2), (1, "PE", "0")], "markets": ("bull", "bear"),
                            "about": "A long straddle with an extra call: pays on a big move either way, twice as fast on a rise."},
    "long_straddle_strip": {"name": "Strip (1 call + 2 puts)", "legs": [(1, "CE", "0"), (1, "PE", "0", 2)], "markets": ("bull", "bear"),
                            "about": "A long straddle with an extra put: pays on a big move either way, twice as fast on a fall."},
    "long_guts": {"name": "Long guts", "min_k": 1, "legs": [(1, "CE", "-k"), (1, "PE", "+k")], "markets": ("bull", "bear"),
                  "about": "Buy an in-the-money call and an in-the-money put. Pays on a big move either way like a strangle; "
                           "dearer, but the in-the-money legs keep their value better."},
    "reverse_iron_condor": {"name": "Reverse iron condor (long)", "legs": [(1, "CE", "+k"), (-1, "CE", "+k+w"), (1, "PE", "-k"), (-1, "PE", "-k-w")],
                            "markets": ("bull", "bear"),
                            "about": "Buy a strangle and sell the wings: a debit that pays when the index breaks out of the range "
                                     "either way, with profit and loss both capped."},
    "reverse_iron_fly": {"name": "Reverse iron butterfly (long)", "legs": [(1, "CE", "0"), (1, "PE", "0"), (-1, "CE", "+w"), (-1, "PE", "-w")],
                         "markets": ("bull", "bear"),
                         "about": "Buy the ATM straddle and sell the wings: a cheaper long straddle with the profit capped at the wings."},
    "short_call_butterfly": {"name": "Short call butterfly", "legs": [(-1, "CE", "-w"), (1, "CE", "0", 2), (-1, "CE", "+w")],
                             "markets": ("bull", "bear"),
                             "about": "Sell a lower call, buy two ATM, sell a higher call: a small credit kept when the index moves "
                                      "past either outer strike; loses most at the middle strike."},
    "call_ratio_backspread": {"bias": "bull", "name": "Call ratio back spread (1×2)", "legs": [(-1, "CE", "0"), (1, "CE", "+w", 2)],
                              "markets": ("bull",),
                              "about": "Sell one ATM call, buy two higher calls: unlimited profit on a strong rally, a small gain or "
                                       "loss on a fall, the worst loss at the upper strike."},
    "put_ratio_backspread": {"bias": "bear", "name": "Put ratio back spread (1×2)", "legs": [(-1, "PE", "0"), (1, "PE", "-w", 2)],
                             "markets": ("bear",),
                             "about": "Sell one ATM put, buy two lower puts: large profit on a crash, the worst loss at the lower strike."},
    "call_diagonal": {"bias": "bull", "name": "Call diagonal (bullish)", "legs": [(1, "CE", "0", 1, "far"), (-1, "CE", "+k")],
                      "markets": ("bull", "sideways"),
                      "about": "Buy the next expiry's ATM call, sell this expiry's higher call. A calendar leaning up: earns the "
                                "near call's decay while the index rises slowly towards the short strike."},
    "put_diagonal": {"bias": "bear", "name": "Put diagonal (bearish)", "legs": [(1, "PE", "0", 1, "far"), (-1, "PE", "-k")],
                     "markets": ("bear", "sideways"),
                     "about": "Buy the next expiry's ATM put, sell this expiry's lower put: a calendar leaning down."},
    "collar": {"bias": "bull", "name": "Collar (futures + put + short call)", "legs": [(1, "FUT", "0"), (1, "PE", "-k"), (-1, "CE", "+k")],
               "markets": ("bull", "sideways"),
               "about": "Long futures with a put below and a call sold above: the call pays for most of the put. Loss and "
                        "profit both capped."},
    "synthetic_long": {"bias": "bull", "name": "Synthetic long futures", "legs": [(1, "CE", "0"), (-1, "PE", "0")], "markets": ("bull",),
                       "about": "Buy the ATM call, sell the ATM put: moves like long futures. Unlimited risk on a fall."},
    "synthetic_short": {"bias": "bear", "name": "Synthetic short futures", "legs": [(-1, "CE", "0"), (1, "PE", "0")], "markets": ("bear",),
                        "about": "Sell the ATM call, buy the ATM put: moves like short futures. Unlimited risk on a rise."},
}
MODES = ("intraday", "positional", "signal")
LEG_SCOPES = ("sold", "bought", "all")
FUT_SLIP = 0.0002          # futures fill about 0.02% from the last price; option slippage is the user's setting
NSE_TUESDAY_FROM = date(2025, 9, 1)


def weekly_weekday(underlying: str, day: date) -> int:
    """Weekday of the weekly expiry in force on ``day`` (Mon=0)."""
    if underlying in BSE_INDICES:
        if day < date(2025, 1, 1):
            return 4                     # Friday
        return 1 if day < date(2025, 9, 1) else 3
    return 3 if day < NSE_TUESDAY_FROM else 1


def expiry_after(underlying: str, moment: pd.Timestamp, kind: str, min_days: float, holidays=frozenset()) -> pd.Timestamp:
    """The first weekly or monthly expiry at least ``min_days`` after ``moment``, at 15:30."""
    exchange = exchange_of(underlying)
    if kind == "monthly":
        start = moment.date()
        candidates = monthly_expiries(start, start + timedelta(days=100), holidays, exchange)
    else:
        candidates, day = [], moment.date()
        for _ in range(40):
            if day.weekday() == weekly_weekday(underlying, day):
                shifted = day
                while shifted.weekday() >= 5 or shifted.isoformat() in holidays:
                    shifted -= timedelta(days=1)
                candidates.append(shifted)
            day += timedelta(days=1)
    for d in candidates:
        stamp = pd.Timestamp(d) + pd.Timedelta(hours=15, minutes=30)
        if (stamp - moment).total_seconds() / 86400 >= min_days and stamp >= moment:
            return stamp
    raise ValueError("no expiry found")


MAX_AHEAD = 3          # trade at most this many expiries after the nearest one


def check_ahead(ahead) -> int:
    """``ahead``: which expiry to trade, 0 the nearest, 1 the one after it, up to ``MAX_AHEAD``."""
    if isinstance(ahead, bool) or not isinstance(ahead, (int, float)) or ahead != int(ahead) or not 0 <= ahead <= MAX_AHEAD:
        raise ValueError(f"Expiry must be the nearest one or up to {MAX_AHEAD} expiries after it.")
    return int(ahead)


def expiry_shift(raw, ahead=0) -> tuple:
    """Your own legs (as sent, see ``own_legs``) and the expiry to trade. When every option leg is on the far
    expiry, they are all on one expiry: the legs move to "near" and ``ahead`` grows by one, so the position is
    held to that expiry rather than sold back at an earlier one nothing is on."""
    ahead = check_ahead(ahead)
    if not isinstance(raw, list):
        return raw, ahead
    options = [g for g in raw if isinstance(g, dict) and str(g.get("type", "")).upper() != "FUT"]
    if options and all(str(g.get("expiry") or "near") == "far" for g in options):
        return [{**g, "expiry": "near"} if isinstance(g, dict) else g for g in raw], ahead + 1
    return raw, ahead


def expiry_ahead_of(underlying: str, moment: pd.Timestamp, kind: str, min_days: float, ahead: int = 0,
                    holidays=frozenset()) -> pd.Timestamp:
    """The expiry ``ahead`` expiries after the first one at least ``min_days`` after ``moment``."""
    expiry = expiry_after(underlying, moment, kind, min_days, holidays)
    for _ in range(ahead):
        expiry = expiry_after(underlying, expiry + pd.Timedelta(minutes=1), kind, 0.0, holidays)
    return expiry


def _offset(text: str, k: int, w: int) -> int:
    """Strikes from the money for a leg: "0", "+k", "-k", "+w", "-w", "+k+w" or "-k-w"."""
    sign = -1 if text.startswith("-") else 1
    return sign * ((k if "k" in text else 0) + (w if "w" in text else 0))


def leg_specs(structure: str) -> list[dict]:
    """The legs of a structure as dicts: action, type, off, ratio, series."""
    out = []
    for raw in STRUCTURES[structure]["legs"]:
        action, kind, off, *rest = raw
        out.append({"action": action, "type": kind, "off": off, "ratio": int(rest[0]) if rest else 1,
                    "series": rest[1] if len(rest) > 1 else "near"})
    return out


CUSTOM = "custom"
MAX_LEGS = 8
MAX_STRIKES = 60       # a leg's strike at most this many strikes from the money
MAX_RATIO = 10


def own_legs(raw) -> list[dict]:
    """Your own legs, checked: a list of {"side": "BUY"|"SELL", "type": "CE"|"PE"|"FUT", "strikes": strikes from the
    at-the-money strike (+ above, − below; ignored for futures), "lots": lots of this leg per lot of the position,
    "expiry": "near"|"far"}. Returned as leg specs (action, type, off, n, ratio, series).

    Strikes are counted from the money, not fixed prices: a backtest opens a fresh position each time over months or
    years while the index moves thousands of points, so "24000 CE" would be deep in or far out of the money on most
    entries. ATM+2 means the same trade every time."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= MAX_LEGS:
        raise ValueError(f"Give 1 to {MAX_LEGS} legs.")
    out = []
    for n, leg in enumerate(raw, 1):
        if not isinstance(leg, dict):
            raise ValueError(f"Leg {n} is not a leg.")
        side = str(leg.get("side", "")).upper()
        kind = str(leg.get("type", "")).upper()
        if side not in ("BUY", "SELL"):
            raise ValueError(f"Leg {n}: choose Buy or Sell.")
        if kind not in ("CE", "PE", "FUT"):
            raise ValueError(f"Leg {n}: choose Call (CE), Put (PE) or Futures.")
        strikes, lots = leg.get("strikes", 0), leg.get("lots", 1)
        for label, value, low, high in (("strikes from ATM", strikes, -MAX_STRIKES, MAX_STRIKES), ("lots", lots, 1, MAX_RATIO)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value != int(value) or not low <= value <= high:
                raise ValueError(f"Leg {n}: {label} must be a whole number from {low} to {high}.")
        series = str(leg.get("expiry") or "near")
        if series not in ("near", "far"):
            raise ValueError(f"Leg {n}: expiry must be near or far.")
        if kind == "FUT":
            strikes, series = 0, "near"
        out.append({"action": 1 if side == "BUY" else -1, "type": kind, "off": f"{int(strikes):+d}" if strikes else "0",
                    "n": int(strikes), "ratio": int(lots), "series": series})
    if all(x["type"] == "FUT" for x in out):
        raise ValueError("Add at least one option leg (the Futures backtest trades futures alone).")
    if all(x["series"] == "far" for x in out if x["type"] != "FUT"):
        raise ValueError("At least one option leg must be on the near expiry (the far one is sold back when it expires).")
    return out


def legs_for(structure: str, k: int, w: int, legs=None) -> list[dict]:
    """The legs to trade, each with ``n``, its strikes from the money: your own ``legs`` (see ``own_legs``) when
    given, otherwise the structure's at short distance ``k`` and wing ``w``."""
    if legs is not None:
        return own_legs(legs)
    if structure not in STRUCTURES:
        raise ValueError(f"structure must be one of {', '.join(STRUCTURES)} or {CUSTOM} with your own legs")
    return [{**x, "n": _offset(x["off"], k, w)} for x in leg_specs(structure)]


def strike_words(n: int) -> str:
    """A leg's strike in words: ATM, ATM+2, ATM−3."""
    return "ATM" if n == 0 else f"ATM{'+' if n > 0 else '−'}{abs(n)}"


def legs_text(specs: list[dict]) -> str:
    """Your own legs in short words: Sell ATM+2 CE, Buy ATM+6 CE (far)."""
    parts = []
    for x in specs:
        what = "Futures" if x["type"] == "FUT" else f"{strike_words(x['n'])} {x['type']}"
        parts.append(f"{'Buy' if x['action'] > 0 else 'Sell'} {str(x['ratio']) + '× ' if x['ratio'] > 1 else ''}{what}"
                     f"{' (far)' if x['series'] == 'far' else ''}")
    return ", ".join(parts)


def strategy_name(structure: str, legs) -> str:
    """What to call a structure: the preset's name, "<preset> (your strikes)" or "Your own strategy"."""
    if structure in STRUCTURES:
        return STRUCTURES[structure]["name"] + (" (your strikes)" if legs is not None else "")
    return "Your own strategy"


def _intrinsic(kind: str, strike: float, spot: float) -> float:
    return max(spot - strike, 0.0) if kind == "CE" else max(strike - spot, 0.0)


def estimate_margin(legs: list[dict], spot: float, qty: int, margin_pct: float) -> float:
    """About what a broker blocks for the position: see the module note.

    ``legs`` carry action, type, strike, ratio and entry (premium per unit). A leg of ratio 2 counts as two lots.
    """
    units = [g for g in legs for _ in range(g.get("ratio", 1))]
    shorts = [g for g in units if g["action"] < 0 and g["type"] != "FUT"]
    futures = [g for g in units if g["type"] == "FUT"]
    need = sum(margin_pct / 100 * spot * qty for _ in futures)
    naked, gaps = [], {"CE": 0.0, "PE": 0.0}
    used: set = set()
    for g in shorts:
        # Any bought option of the same type covers a sold one; the loss is capped at the strike gap (zero
        # when the bought one is nearer the money, as in a debit spread).
        cover = [(n, x) for n, x in enumerate(units) if x["action"] > 0 and x["type"] == g["type"] and n not in used]
        if cover:
            n, x = min(cover, key=lambda nx: abs(nx[1]["strike"] - g["strike"]))
            used.add(n)
            gap = x["strike"] - g["strike"] if g["type"] == "CE" else g["strike"] - x["strike"]
            gaps[g["type"]] += max(gap, 0.0) * qty
        elif not (g["type"] == "CE" and futures):          # a call sold against long futures is covered
            naked.append(g)
    if naked or futures:
        need += max(gaps.values())                    # only one side of a condor or fly can lose at expiry
        if naked:
            need += margin_pct / 100 * spot * qty * (1 if len(naked) <= 2 else len(naked) / 2)
    else:
        # Every short is covered: block the worst loss at expiry from the strikes alone (premiums aside). The
        # payoff is straight between strikes, so the strikes and zero are the only points to check.
        points = [0.0] + [g["strike"] for g in units]
        worst = min(sum(g["action"] * _intrinsic(g["type"], g["strike"], s) for g in units) for s in points)
        need += max(-worst, 0.0) * qty
    # Premiums: bought options are paid in full; what the sold ones bring in offsets that, except in a calendar or
    # diagonal, where the near short loses its cover on its own expiry day and is margined without the offset.
    options = [g for g in units if g["type"] != "FUT"]
    paid = sum(g["entry"] for g in options if g["action"] > 0)
    received = sum(g["entry"] for g in options if g["action"] < 0)
    split = any(g.get("series") == "far" for g in options)
    need += (paid if split else max(paid - received, 0.0)) * qty
    return need


def _label(legs: list[dict], near: pd.Timestamp) -> str:
    parts = []
    for g in legs:
        text = "FUT" if g["type"] == "FUT" else f"{g['strike']:.0f}{g['type']}"
        if g.get("ratio", 1) > 1:
            text = f"{g['ratio']}×{text}"
        if g.get("expiry") is not None and g["expiry"] > near:
            text += f" ({g['expiry']:%d %b})"
        parts.append(("+" if g["action"] > 0 else "−") + text)
    return " ".join(parts)


class _Pricer:
    def __init__(self, vol_by_day: pd.Series, iv_scale: float, skew: float):
        self.vol, self.scale, self.skew = vol_by_day, iv_scale, skew

    def sigma(self, stamp, spot, strike) -> float:
        base = float(self.vol.get(stamp.normalize(), self.vol.iloc[-1])) * self.scale
        # A simple skew: volatility rises as the strike falls below the spot (puts and in-the-money calls).
        return max(base * (1 + self.skew * max(0.0, (spot - strike) / spot) * 10), 0.03)

    source = "model"
    used: dict | None = None

    def price(self, leg: dict, spot: float, stamp, expiry: pd.Timestamp) -> float:
        years = max((expiry - stamp).total_seconds(), 0.0) / (365 * 86400)
        if leg["type"] == "FUT":
            return spot * (1 + RATE * years)
        return max(bs_price(spot, leg["strike"], years, self.sigma(stamp, spot, leg["strike"]), leg["type"]), 0.05 if years > 0 else 0.0)

    def range(self, leg: dict, stamp, first: float, low: float, high: float) -> tuple[float, float, float]:
        """The leg's premium at a candle's open, lowest and highest point (from the index's open, low and high)."""
        at = [self.price(leg, x, stamp, leg["expiry"]) for x in (first, low, high)]
        return at[0], min(at), max(at)

    def count(self) -> None:
        """Note where the price just used for a fill came from."""
        if self.used is not None:
            self.used[self.source] = self.used.get(self.source, 0) + 1


class _HistoryPricer(_Pricer):
    """Real premiums where ``chainhistory`` has them, the model otherwise. Day candles read the NSE bhavcopy: the
    option's open at 09:15, its close at 15:30 (NSE's settlement price for a strike that did not trade that day), its
    own low and high for stops. Intraday candles read the live snapshots the recorder saved, at most 10 minutes old;
    the move within a candle is the model's, scaled to the recorded price."""

    def __init__(self, vol_by_day, iv_scale, skew, history, underlying: str, intraday: bool):
        super().__init__(vol_by_day, iv_scale, skew)
        self.history, self.underlying, self.intraday = history, underlying, intraday
        self.used = {"traded": 0, "settle": 0, "recorded": 0, "model": 0, "expiry": 0}

    def _row(self, leg, stamp, expiry):
        if leg["type"] == "FUT" or stamp >= expiry:
            return None
        return self.history.eod(self.underlying, stamp.date(), expiry.date(), leg["strike"], leg["type"])

    def _real(self, leg, stamp, expiry) -> tuple[float, str] | None:
        if leg["type"] == "FUT" or stamp >= expiry:
            return None
        if self.intraday:
            price = self.history.snapshot(self.underlying, stamp.to_pydatetime(), expiry.date(), leg["strike"], leg["type"])
            return (price, "recorded") if price else None
        row = self._row(leg, stamp, expiry)
        if row is None:
            return None
        if stamp.hour * 60 + stamp.minute <= 9 * 60 + 15:
            # At the open only the day's opening trade is known; a strike that did not trade falls back to the model.
            return (row["open"], "traded") if row["volume"] > 0 and row["open"] > 0 else None
        if row["volume"] > 0 and row["close"] > 0:
            return row["close"], "traded"
        return (row["settle"], "settle") if row["settle"] > 0 else None

    def price(self, leg, spot, stamp, expiry) -> float:
        real = self._real(leg, stamp, expiry)
        if real is not None:
            self.source = real[1]
            return real[0]
        self.source = "model"
        return super().price(leg, spot, stamp, expiry)

    def range(self, leg, stamp, first, low, high):
        if not self.intraday:
            row = self._row(leg, stamp, leg["expiry"])
            if row is not None and row["volume"] > 0 and min(row["open"], row["low"], row["high"]) > 0:
                self.source = "traded"
                return row["open"], row["low"], row["high"]
            self.source = "model"
            return _Pricer.range(self, leg, stamp, first, low, high)
        real = self._real(leg, stamp, leg["expiry"])
        model = [_Pricer.price(self, leg, x, stamp, leg["expiry"]) for x in (first, low, high)]
        if real is None or model[0] <= 0:
            self.source = "model"
            return model[0], min(model), max(model)
        self.source = "recorded"
        scale = real[0] / model[0]
        return real[0], min(model) * scale, max(model) * scale

    def expiries(self, stamp, kind: str) -> list[pd.Timestamp]:
        """The expiries NSE listed that day, as 15:30 stamps; monthly keeps the last one of each month."""
        days = self.history.expiries(self.underlying, stamp.date())
        if kind == "monthly":
            last: dict = {}
            for d in days:
                last[(d.year, d.month)] = d
            days = sorted(last.values())
        return [pd.Timestamp(d) + pd.Timedelta(hours=15, minutes=30) for d in days]


def run_structure(underlying: str, bars: pd.DataFrame, vix: pd.Series, *, structure: str, mode: str, lot_size: int,
                  lots: int = 1, capital: float = 500_000.0, k: int = 2, w: int = 4, expiry_kind: str = "weekly",
                  min_days: float = 0.0, weekdays=(0, 1, 2, 3, 4), entry_time: str = "09:20", exit_time: str = "15:15",
                  exit_days_before: int = 0, leg_sl_pct: float = 0.0, stop_pct: float = 0.0, target_pct: float = 0.0,
                  signal: pd.Series | None = None, margin_pct: float = 12.0, iv_scale: float = 1.0, skew: float = 0.0,
                  slippage_pct: float = 1.0, holidays=frozenset(), risk_free_rate_pct: float = 6.5,
                  leg_exits: ExitRules | None = None, leg_scope: str = "sold", history=None, legs=None,
                  stop_rs: float = 0.0, target_rs: float = 0.0, expiry_ahead: int = 0, steps: Steps = NO_STEPS) -> dict:
    """Backtest one structure. ``leg_exits`` are each option leg's own exit rules (stoploss, target, trailing,
    stoploss to cost, re-entry; ``risk.ExitRules``, distances on the leg's premium) for the legs ``leg_scope``
    names: ``sold``, ``bought`` or ``all``. Without them ``leg_sl_pct`` is a % stoploss on each sold leg.

    ``history`` (a ``chainhistory.ChainHistory``): price legs from NSE's bhavcopy (day candles) or recorded live
    snapshots (intraday) where it has them, on the expiries NSE actually listed; the model fills the gaps. The
    result's ``pricing`` counts where each fill's price came from.

    ``legs``: your own legs in place of the structure's (see ``own_legs``; ``structure`` is then a preset whose
    strikes you changed, or ``custom``). ``stop_rs`` / ``target_rs``: close the whole position when its profit or
    loss (closed legs plus open ones at the candle's close, before charges) reaches this many rupees.
    ``expiry_ahead``: trade the expiry this many after the nearest one that suits (0 the nearest); a far leg is on
    the expiry after that."""
    legs, ahead = expiry_shift(legs, expiry_ahead)
    specs = legs_for(structure, k, w, legs)
    if mode not in MODES:
        raise ValueError(f"mode must be one of {', '.join(MODES)}")
    intraday = mode == "intraday"
    if intraday and bars.index.normalize().nunique() == len(bars):
        raise ValueError("Intraday mode needs 1-15 minute candles, not day candles.")
    if not intraday and bars.index.normalize().nunique() != len(bars):
        raise ValueError("Positional and signal modes use day candles.")
    if mode == "signal" and signal is None:
        raise ValueError("Signal mode needs a strategy.")
    bias = STRUCTURES.get(structure, {}).get("bias")
    if mode == "signal" and not bias:
        raise ValueError("Signal mode is for directional structures (spreads, protective put, covered call). "
                         "Run straddles, strangles, condors and flies on a calendar (intraday or positional)."
                         if legs is None or structure in STRUCTURES else
                         "Signal mode needs a bullish or bearish preset. Start your legs from one (a spread, "
                         "protective put, covered call), or run your own strategy intraday or positional.")
    if lots < 1 or lot_size < 1 or not 0 <= k <= 40 or not 1 <= w <= 60:
        raise ValueError("lots ≥ 1; short distance 0-40 strikes; wing 1-60 strikes")
    for label, value in (("Stoploss (₹)", stop_rs), ("Target (₹)", target_rs)):
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1e8:
            raise ValueError(f"{label} for the whole position must be between 0 and 10 crore")
    if legs is None and k < STRUCTURES[structure].get("min_k", 0):
        raise ValueError(f"{STRUCTURES[structure]['name']} needs the legs at least {STRUCTURES[structure]['min_k']} strike "
                         "from the money (k); at 0 it is a straddle.")
    if leg_scope not in LEG_SCOPES:
        raise ValueError(f"leg_scope must be one of {', '.join(LEG_SCOPES)}")
    if steps.active and sum(steps.lots()) > lots:
        raise ValueError(f"the profit steps close {sum(steps.lots())} lots but the position has {lots}")
    if steps.active and steps.unit not in ("pct", "rs"):
        raise ValueError("an option strategy's profit steps are in ₹ a lot or % of the premium")
    if leg_exits is None:
        leg_exits = ExitRules(unit="pct", stop_loss=leg_sl_pct)
    leg_exits.validate("Each leg")
    for label, value, top in (("Leg stop", leg_sl_pct, 1000), ("Stop", stop_pct, 1000), ("Target", target_pct, 100),
                              ("Slippage", slippage_pct, 20), ("Margin", margin_pct, 100)):
        if not 0 <= value <= top:
            raise ValueError(f"{label} must be between 0 and {top}%")
    if len(bars) < 20:
        raise ValueError("not enough candles")
    exchange = exchange_of(underlying)
    if expiry_kind == "weekly" and underlying not in ("NIFTY", "SENSEX") and bars.index[-1] > pd.Timestamp("2024-11-20"):
        raise ValueError(f"{underlying} has had only monthly options since November 2024. Choose monthly expiry.")
    vol = (vix / 100).reindex(pd.DatetimeIndex(bars.index.normalize().unique())).ffill().bfill()
    if vol.isna().all():
        raise ValueError("No India VIX history for this period; it is needed as the volatility.")
    if float(vol.max()) > 1.5:
        raise ValueError("The India VIX series does not look like VIX (values above 150).")
    pricer = (_HistoryPricer(vol, iv_scale, skew, history, underlying, intraday) if history is not None
              else _Pricer(vol, iv_scale, skew))
    name = strategy_name(structure, legs)
    step = STRIKE_STEP.get(underlying, 50)
    qty = lots * lot_size
    slip = slippage_pct / 100
    t_entry, t_exit = (pd.Timestamp(f"2000-01-01 {t}").time() for t in (entry_time, exit_time))
    params = BacktestParams(capital=capital, risk_free_rate_pct=risk_free_rate_pct, intraday=intraday)
    stamps = bars.index
    opens, highs, lows, closes = (bars[c].to_numpy(float) for c in ("Open", "High", "Low", "Close"))
    want = (signal.reindex(stamps).fillna(0).astype(int).to_numpy() if signal is not None else np.zeros(len(bars), int))
    if mode == "signal" and bias == "bear":
        # Bearish structures are on while the strategy is flat (after its first signal, not while it warms up).
        started = np.maximum.accumulate(want > 0)
        want = np.where((want == 0) & started, 1, 0)

    cash, total_charges, in_market, skipped = capital, 0.0, 0, 0
    trades, equity = [], np.empty(len(bars))
    book = None             # {"legs": [...], "expiry", "opened_i", "margin", "net_open", "qty": lots open × lot size}
    plan = steps.lots() if steps.active else []
    steps_booked = 0

    def leg_fee(leg, side, price, size=None):
        size = (book["qty"] if book else qty) if size is None else size
        if leg["type"] == "FUT":
            return futures_charges(side, size * leg["ratio"], price, exchange).total
        return option_charges(side, size * leg["ratio"], price, exchange)

    def pick_expiry(stamp, after, least):
        """The first expiry ``least`` days after ``after``: one NSE listed that day when the history has the day."""
        if history is not None:
            listed = [e for e in pricer.expiries(stamp, expiry_kind)
                      if e >= after and (e - after).total_seconds() / 86400 >= least]
            if listed:
                return listed[0]
        return expiry_after(underlying, after, expiry_kind, least, holidays)

    def open_book(i, spot, stamp):
        nonlocal cash, total_charges, skipped, book
        expiry = pick_expiry(stamp, stamp, min_days if not intraday else 0.0)
        for _ in range(ahead):
            expiry = pick_expiry(stamp, expiry + pd.Timedelta(minutes=1), 0.0)
        far = (pick_expiry(stamp, expiry + pd.Timedelta(minutes=1), 0.0)
               if any(x["series"] == "far" for x in specs) else None)
        atm = round(spot / step) * step
        legs = []
        for x in specs:
            kind = x["type"]
            strike = max(atm + x["n"] * step, step) if kind != "FUT" else 0.0
            leg = {"action": x["action"], "type": kind, "strike": float(strike), "ratio": x["ratio"], "series": x["series"],
                   "expiry": far if x["series"] == "far" else expiry, "open": True, "n": x["n"]}
            mid = pricer.price(leg, spot, stamp, leg["expiry"])
            if kind != "FUT":
                pricer.count()
                leg["source"] = pricer.source
            leg_slip = FUT_SLIP if kind == "FUT" else slip
            leg["entry"] = mid * (1 + leg_slip) if x["action"] > 0 else mid * (1 - leg_slip)
            leg["mid"] = leg["best"] = mid
            leg["reentries_left"] = leg_exits.reentry
            legs.append(leg)
        margin = estimate_margin(legs, spot, qty, margin_pct)
        if margin > cash:
            skipped += 1
            if pricer.used is not None:             # nothing was filled
                for g in legs:
                    if g["type"] != "FUT":
                        pricer.used[g.get("source", "model")] -= 1
            return
        fees = sum(leg_fee(g, "BUY" if g["action"] > 0 else "SELL", g["entry"], qty) for g in legs)
        cash -= fees
        total_charges += fees
        net = sum(-g["action"] * g["ratio"] * g["entry"] for g in legs if g["type"] != "FUT") * qty     # + credit, − debit
        book = {"legs": legs, "expiry": expiry, "opened_i": i, "margin": margin, "net_open": net, "fees": fees,
                "realised": 0.0, "stopped": 0, "reentries": 0, "spot": spot, "qty": qty, "lots": lots, "done": 0,
                "partials": []}

    def settle_leg(g, price, size, side_fee=True) -> float:
        """Close ``size`` (lots open × lot size) of a leg at ``price``; returns the fill."""
        nonlocal cash, total_charges
        leg_slip = FUT_SLIP if g["type"] == "FUT" else slip
        fill = price * (1 - leg_slip) if g["action"] > 0 else price * (1 + leg_slip)
        if g["type"] != "FUT" and price <= 0:
            fill = 0.0
        pnl = g["action"] * (fill - g["entry"]) * size * g["ratio"]
        fee = leg_fee(g, "SELL" if g["action"] > 0 else "BUY", max(fill, 0.05), size) \
            if side_fee and (fill > 0 or g["type"] == "FUT") else 0.0
        cash += pnl - fee
        total_charges += fee
        book["fees"] += fee
        book["realised"] += pnl
        return fill

    def close_leg(g, price, side_fee=True):
        g["open"], g["exit"] = False, settle_leg(g, price, book["qty"], side_fee)

    def mtm(spot, stamp):
        return sum(g["action"] * (pricer.price(g, spot, stamp, g["expiry"]) - g["entry"]) * book["qty"] * g["ratio"]
                   for g in book["legs"] if g["open"])

    def per_lot(spot, stamp) -> float | None:
        """The open legs' profit for one lot of the position: rupees, or % of their premium (the margin when nil)."""
        open_legs = [g for g in book["legs"] if g["open"]]
        if not open_legs:
            return None
        prices = {id(g): pricer.price(g, spot, stamp, g["expiry"]) for g in open_legs}
        pnl = sum(g["action"] * (prices[id(g)] - g["entry"]) * lot_size * g["ratio"] for g in open_legs)
        if steps.unit == "rs":
            return pnl
        base = abs(sum(-g["action"] * g["ratio"] * g["entry"] for g in open_legs if g["type"] != "FUT")) * lot_size
        base = base or book["margin"] / book["lots"]
        return pnl / base * 100 if base else None

    def book_part(i, spot, stamp, n):
        """Close ``n`` lots of every open leg: the sold legs and their hedges together."""
        nonlocal steps_booked
        before = book["realised"] - book["fees"]
        for g in book["legs"]:
            if g["open"]:
                settle_leg(g, pricer.price(g, spot, stamp, g["expiry"]), n * lot_size)
                if g["type"] != "FUT":
                    pricer.count()
        book["lots"] -= n
        book["qty"] = book["lots"] * lot_size
        steps_booked += 1
        book["partials"].append({"date": stamp.strftime("%Y-%m-%d %H:%M"), "lots": n, "quantity": n * lot_size,
                                 "price": round(spot, 2), "pnl": round(book["realised"] - book["fees"] - before, 2),
                                 "why": f"profit step {book['done']}"})

    def close_book(i, spot, stamp, reason, at_expiry=False):
        nonlocal book
        for g in book["legs"]:
            if g["open"]:
                if at_expiry and g["expiry"] > book["expiry"]:
                    # A later-expiry leg of a calendar or diagonal is sold back at the near expiry's close.
                    close_leg(g, pricer.price(g, spot, book["expiry"], g["expiry"]))
                    pricer.count()
                elif at_expiry:
                    price = (max(spot - g["strike"], 0) if g["type"] == "CE" else max(g["strike"] - spot, 0)
                             if g["type"] == "PE" else spot)
                    close_leg(g, price, side_fee=g["type"] == "FUT")
                    if g["type"] != "FUT" and pricer.used is not None:
                        pricer.used["expiry"] += 1
                else:
                    close_leg(g, pricer.price(g, spot, stamp, g["expiry"]))
                    if g["type"] != "FUT":
                        pricer.count()
        pnl = book["realised"] - book["fees"]
        strikes = _label(book["legs"], book["expiry"])
        fmt = "%Y-%m-%d %H:%M" if intraday else "%Y-%m-%d"
        trades.append({"contract": f"{underlying} {book['expiry']:%d %b} {strikes}", "entry_date": stamps[book["opened_i"]].strftime(fmt),
                       "exit_date": stamp.strftime(fmt), "quantity": qty, "lots": lots, "entry_price": round(book["net_open"] / qty, 2),
                       "exit_price": round(spot, 2), "pnl": round(pnl, 2),
                       "return_pct": round(pnl / book["margin"] * 100, 2) if book["margin"] else None,
                       "days": int((stamp - stamps[book["opened_i"]]).days), "exit": reason, "legs_stopped": book["stopped"],
                       "reentries": book["reentries"], "margin": round(book["margin"], 0), "open": False,
                       **({"partials": book["partials"]} if book["partials"] else {})})
        book = None

    def in_scope(g) -> bool:
        return g["type"] != "FUT" and (leg_scope == "all" or (g["action"] < 0) == (leg_scope == "sold"))

    def premium_range(g, i, stamp) -> tuple[float, float, float]:
        """The leg's premium at this candle's open, lowest and highest point."""
        return pricer.range(g, stamp, opens[i], lows[i], highs[i])

    def reopen(g, i, stamp, price_mid):
        """Open the leg again (a re-entry), as a new leg of the same book."""
        nonlocal cash, total_charges
        leg = {k: v for k, v in g.items() if k not in ("exit", "rearm")}
        leg_slip = slip
        leg["entry"] = price_mid * (1 + leg_slip) if g["action"] > 0 else price_mid * (1 - leg_slip)
        leg["mid"] = leg["best"] = price_mid
        leg["open"], leg["reentries_left"] = True, g["rearm"]["left"] - 1
        leg["reopened_i"] = i
        fee = leg_fee(leg, "BUY" if leg["action"] > 0 else "SELL", leg["entry"])
        cash -= fee
        total_charges += fee
        book["fees"] += fee
        book["reentries"] += 1
        g["rearm"] = None
        book["legs"].append(leg)

    def check_reentries(i, stamp):
        """Legs stopped out earlier that may enter again on this candle."""
        for g in list(book["legs"]):
            rearm = g.get("rearm")
            if not rearm or rearm["after"] >= i:
                continue
            if leg_exits.reentry_mode == "at_once":
                # Straight away: the entry rule picks the strike again, from the index now.
                atm = round(opens[i] / step) * step
                fresh = {**g, "strike": float(max(atm + g["n"] * step, step))}
                reopen(fresh | {"rearm": rearm}, i, stamp, pricer.price(fresh, opens[i], stamp, g["expiry"]))
                pricer.count()
                g["rearm"] = None
                continue
            # When the premium is back at the earlier entry price: the same strike, filled at that price.
            first, low, high = premium_range(g, i, stamp)
            level = rearm["price"]
            if g["action"] > 0 and high >= level:
                reopen(g, i, stamp, max(first, level) if first >= level else level)
                pricer.count()
            elif g["action"] < 0 and low <= level:
                reopen(g, i, stamp, min(first, level) if first <= level else level)
                pricer.count()

    def check_stops(i, stamp):
        """Each leg's own exit rules (at the candle's worst point), then the whole position's stop and target."""
        if leg_exits.active:
            for g in list(book["legs"]):
                if not (g["open"] and in_scope(g)) or g.get("reopened_i") == i:
                    continue
                side = g["action"]
                first, low, high = premium_range(g, i, stamp)
                stop, kind, goal = leg_exits.levels(g["mid"], g["best"], side)
                worst, best_now = (low, high) if side > 0 else (high, low)
                stop_hit = stop is not None and (worst - stop) * side <= 0
                goal_hit = not stop_hit and goal is not None and (best_now - goal) * side >= 0
                if side > 0 and (stop_hit or goal_hit) and any(x["open"] and x["action"] < 0 and x["type"] != "FUT"
                                                               for x in book["legs"]):
                    # A bought leg covers a sold one: never closed alone (as the live algo does), so all legs go.
                    return f"{kind if stop_hit else 'target'} on a bought leg"
                if stop_hit or goal_hit:
                    pricer.count()
                if stop_hit:
                    gapped = (first - stop) * side <= 0
                    close_leg(g, first if gapped else stop)
                    book["stopped"] += 1
                    if kind == "stoploss" and g["reentries_left"] > 0:
                        g["rearm"] = {"price": g["mid"], "left": g["reentries_left"], "after": i}
                elif goal_hit:
                    gapped = (first - goal) * side >= 0
                    close_leg(g, first if gapped else goal)
                else:
                    g["best"] = max(g["best"], high) if side > 0 else min(g["best"], low)
        if (stop_pct or target_pct or stop_rs or target_rs or steps.active) and book:
            base = abs(book["net_open"]) or book["margin"]
            running = book["realised"] + mtm(closes[i], stamp)
            if stop_rs and running <= -stop_rs:
                return "stoploss in ₹"
            if stop_pct and running <= -stop_pct / 100 * base:
                return "stoploss"
            if steps.active:
                profit = per_lot(closes[i], stamp)
                moved = steps.lock(book["done"])
                if profit is not None and moved is not None and profit <= moved:
                    return steps.lock_name(book["done"])
                while profit is not None and book["done"] < len(plan) and profit >= steps.levels[book["done"]]:
                    n = plan[book["done"]]
                    book["done"] += 1
                    if n >= book["lots"]:
                        return f"profit step {book['done']}"
                    book_part(i, closes[i], stamp, n)
                running = book["realised"] + mtm(closes[i], stamp)
            if target_rs and running >= target_rs:
                return "target in ₹"
            if target_pct and running >= target_pct / 100 * base:
                return "target"
        return None

    def live(b) -> bool:
        """Whether the position still has an open leg, or a stopped leg waiting to re-enter."""
        return any(g["open"] or g.get("rearm") for g in b["legs"])

    days_index = stamps.normalize()
    tried_day = None
    for i in range(len(bars)):
        stamp, spot = stamps[i], closes[i]
        if intraday:
            moment = stamp.time()
            if book is None and t_entry <= moment < t_exit and stamp.weekday() in weekdays and tried_day != days_index[i]:
                tried_day = days_index[i]                       # one entry a day
                open_book(i, opens[i], stamp)
            if book is not None:
                if moment < t_exit:
                    check_reentries(i, stamp)
                reason = check_stops(i, stamp)
                last_of_day = i + 1 == len(bars) or days_index[i + 1] != days_index[i]
                expired = stamp >= book["expiry"]
                if reason or moment >= t_exit or last_of_day or expired or not live(book):
                    close_book(i, spot, stamp, reason or ("expiry" if expired else "time exit"), at_expiry=expired)
        else:
            if book is not None:
                expiry_day = book["expiry"].normalize()
                if stamp.normalize() >= expiry_day:
                    settle = closes[i] if stamp.normalize() == expiry_day else opens[i]
                    close_book(i, settle, stamp, "expiry", at_expiry=True)
                else:
                    check_reentries(i, stamp + pd.Timedelta(hours=15, minutes=30))
                    reason = check_stops(i, stamp + pd.Timedelta(hours=15, minutes=30))
                    days_left = np.busday_count(stamp.date(), expiry_day.date())
                    if reason:
                        close_book(i, spot, stamp + pd.Timedelta(hours=15, minutes=30), reason)
                    elif exit_days_before and days_left <= exit_days_before:
                        close_book(i, spot, stamp + pd.Timedelta(hours=15, minutes=30), "days before expiry")
                    elif mode == "signal" and i > 0 and not want[i - 1]:
                        close_book(i, opens[i], stamp + pd.Timedelta(hours=9, minutes=15), "signal")
                    elif not live(book):
                        close_book(i, spot, stamp + pd.Timedelta(hours=15, minutes=30), "all legs stopped")
            if book is None:
                go = (mode == "positional" and stamp.weekday() in weekdays) or (mode == "signal" and i > 0 and want[i - 1])
                if go and not (trades and trades[-1]["exit_date"] == stamp.strftime("%Y-%m-%d") and mode == "positional"):
                    open_book(i, opens[i], stamp + pd.Timedelta(hours=9, minutes=15))
        if book is not None:
            in_market += 1
        equity[i] = cash + (mtm(spot, stamp if intraday else stamp + pd.Timedelta(hours=15, minutes=30)) if book else 0.0)

    if book is not None:
        last = stamps[-1] if intraday else stamps[-1] + pd.Timedelta(hours=15, minutes=30)
        running = book["realised"] + mtm(closes[-1], last) - book["fees"]
        strikes = _label(book["legs"], book["expiry"])
        trades.append({"contract": f"{underlying} {book['expiry']:%d %b} {strikes}", "entry_date": stamps[book["opened_i"]].strftime("%Y-%m-%d"),
                       "exit_date": None, "quantity": qty, "lots": lots, "entry_price": round(book["net_open"] / qty, 2), "exit_price": round(closes[-1], 2),
                       "pnl": round(running, 2), "return_pct": round(running / book["margin"] * 100, 2) if book["margin"] else None,
                       "days": int((stamps[-1] - stamps[book["opened_i"]]).days), "exit": "open", "legs_stopped": book["stopped"],
                       "reentries": book["reentries"], "margin": round(book["margin"], 0), "open": True,
                       **({"partials": book["partials"]} if book["partials"] else {})})
    curve = pd.Series(equity, index=stamps)
    hold = capital * bars["Close"] / bars["Close"].iloc[0]
    if intraday:
        curve, hold = curve.groupby(days_index).last(), hold.groupby(days_index).last()
        in_market = int(round(in_market / len(bars) * len(curve)))
    metrics = _metrics(curve, trades, params, total_charges, in_market)
    metrics.update(skipped_entries=skipped, legs_stopped=sum(t["legs_stopped"] for t in trades),
                   reentries=sum(t.get("reentries", 0) for t in trades), steps_booked=steps_booked,
                   avg_margin=round(float(np.mean([t["margin"] for t in trades])), 0) if trades else None)
    return {"strategy": {"name": f"{name} · {mode}"}, "structure": structure, "mode": mode,
            "legs": [{"side": "BUY" if x["action"] > 0 else "SELL", "type": x["type"], "strikes": x["n"], "lots": x["ratio"],
                      "expiry": x["series"]} for x in specs], "legs_text": legs_text(specs),
            "params": {**params.__dict__, "lots": lots, "lot_size": lot_size, "k": k, "w": w, "expiry_kind": expiry_kind,
                       "expiry_ahead": ahead, "leg_sl_pct": leg_sl_pct, "stop_pct": stop_pct, "target_pct": target_pct, "iv_scale": iv_scale,
                       "stop_rs": stop_rs, "target_rs": target_rs, "steps": steps.as_dict() if steps.active else None,
                       "leg_exits": leg_exits.as_dict(), "leg_scope": leg_scope},
            "simulated": pricer.used is None or pricer.used["model"] > 0, "pricing": pricer.used,
            "symbol": underlying, "skipped_entries": skipped,
            "period": {"start": curve.index[0].strftime("%Y-%m-%d"), "end": curve.index[-1].strftime("%Y-%m-%d"),
                       "bars": len(bars), "days": len(curve)},
            "metrics": metrics, "buy_hold": _curve_stats(hold, params), "trades": trades, "equity": _points(curve),
            "buy_hold_equity": _points(hold), "benchmark_equity": None, "benchmark": None}


# -- payoff analyser ------------------------------------------------------------------------------------------------

def analyse(underlying: str, structure: str, *, spot: float, iv: float, days: float, far_days: float | None = None,
            k: int = 2, w: int = 4, lots: int = 1, lot_size: int = 65, margin_pct: float = 12.0,
            premiums: list | None = None, labels: list | None = None, rate: float = RATE, points: int = 241,
            legs=None) -> dict:
    """Legs, premiums, payoff, breakevens, maximum profit and loss, Greeks and margin of one structure, now.

    Premiums are Black-Scholes at ``iv`` (a fraction, 0.14 for 14%) unless ``premiums`` gives one per leg (for example
    the last prices from the option chain; ``None`` in the list keeps the model price). A given option premium is
    valued at its own implied volatility (Greeks and today's curve), falling back to ``iv`` when no volatility gives
    that price. ``labels`` names where each given premium came from (default "given"). ``days`` is the calendar days
    to the near expiry; calendars and diagonals also need ``far_days`` (default ``days`` + 7 for weekly, + 28 beyond).
    The expiry payoff is at the near expiry; a later-expiry leg is valued then at ``iv``. Rupee figures are for
    ``lots`` × ``lot_size``. ``legs``: your own legs (see ``own_legs``) in place of the structure's.
    """
    legs, shifted = expiry_shift(legs, 0)
    if shifted:
        # Every option leg on the far expiry: the position is all on that one, so it is the expiry the payoff is at.
        days = far_days if far_days is not None else days + (7 if days <= 7 else 28)
        far_days = None
    specs = legs_for(structure, k, w, legs)
    spec = STRUCTURES.get(structure, {"name": strategy_name(structure, legs), "about": legs_text(specs)})
    if not spot > 0:
        raise ValueError("spot must be above zero")
    if not 0.01 <= iv <= 3:
        raise ValueError("volatility must be between 1% and 300%")
    if not 0 <= days <= 400:
        raise ValueError("days to expiry must be 0-400")
    if lots < 1 or lot_size < 1 or not 0 <= k <= 40 or not 1 <= w <= 60:
        raise ValueError("lots ≥ 1; short distance 0-40 strikes; wing 1-60 strikes")
    if legs is None and k < spec.get("min_k", 0):
        raise ValueError(f"{spec['name']} needs the legs at least {spec['min_k']} strike from the money (k); "
                         "at 0 it is a straddle.")
    has_far = any(x["series"] == "far" for x in specs)
    if has_far:
        far_days = far_days if far_days is not None else days + (7 if days <= 7 else 28)
        if not far_days > days:
            raise ValueError("the far expiry must be after the near one")
    if premiums is not None and (not isinstance(premiums, list) or len(premiums) != len(specs)):
        raise ValueError(f"premiums needs one value per leg ({len(specs)})")
    if labels is not None and (not isinstance(labels, list) or len(labels) != len(specs)):
        raise ValueError(f"labels needs one value per leg ({len(specs)})")
    step = STRIKE_STEP.get(underlying, 50)
    qty = lots * lot_size
    atm = round(spot / step) * step
    t_near = days / 365

    def years_left(leg, after_near: bool) -> float:
        total = (far_days if leg["series"] == "far" else days) / 365
        return max(total - (t_near if after_near else 0.0), 0.0)

    def value(leg, s: float, years: float) -> float:
        if leg["type"] == "FUT":
            return s * (1 + rate * years)
        return bs_price(s, leg["strike"], years, leg["vol"], leg["type"], rate)

    legs = []
    for n, x in enumerate(specs):
        if x["type"] != "FUT" and atm + x["n"] * step <= 0:
            raise ValueError(f"{strike_words(x['n'])} is below zero at this index level. Choose a strike nearer the money.")
        leg = {**x, "strike": float(atm + x["n"] * step) if x["type"] != "FUT" else 0.0, "vol": iv,
               "implied": None}
        years = years_left(leg, False)
        model = value(leg, spot, years)
        given = premiums[n] if premiums is not None else None
        if given is not None:
            if isinstance(given, bool) or not isinstance(given, (int, float)) or given < 0:
                raise ValueError("each premium must be a number ≥ 0")
            label = labels[n] if labels is not None and labels[n] else "given"
            leg["entry"], leg["source"] = float(given), str(label)
            if leg["type"] != "FUT":
                leg["implied"] = implied_vol(float(given), spot, leg["strike"], years, leg["type"])
                if leg["implied"] and 0.01 <= leg["implied"] <= 3:
                    leg["vol"] = leg["implied"]
                else:
                    leg["implied"] = None
        else:
            leg["entry"], leg["source"] = model, "model"
        leg["days"] = far_days if x["series"] == "far" else days
        greeks = (bs_greeks(spot, leg["strike"], years, leg["vol"], leg["type"], rate) if leg["type"] != "FUT"
                  else {"delta": 1.0, "gamma": 0.0, "theta": 0.0, "vega": 0.0})
        leg["greeks"] = greeks
        legs.append(leg)

    def pnl_at(s: float, after_near: bool) -> float:
        """Position P&L in rupees at index level s: at the near expiry, or now (today's prices at s)."""
        total = 0.0
        for g in legs:
            years = years_left(g, after_near)
            worth = (_intrinsic(g["type"], g["strike"], s) if g["type"] != "FUT" and years <= 0 else value(g, s, years))
            total += g["action"] * g["ratio"] * (worth - g["entry"])
        return total * qty

    strikes = sorted({g["strike"] for g in legs if g["type"] != "FUT"})
    # Chart range: the strikes plus about four standard deviations of the move to the near expiry (at least 3%).
    reach = max(4 * spot * iv * math.sqrt(max(days, 1.0) / 365), 0.03 * spot, 2 * step)
    low = max(min(strikes + [spot]) - reach, step)
    high = max(strikes + [spot]) + reach
    grid = sorted(set(np.linspace(low, high, points).round(2).tolist()) | set(strikes) | {round(spot, 2)})
    at_expiry = [pnl_at(s, True) for s in grid]
    today = [pnl_at(s, False) for s in grid]

    # Breakevens: where the expiry P&L crosses zero (straight-line interpolation between grid points).
    breakevens = []
    for a, b, pa, pb in zip(grid, grid[1:], at_expiry, at_expiry[1:], strict=False):
        if pa == 0 and (not breakevens or abs(breakevens[-1] - a) > 1e-6):
            breakevens.append(a)
        elif pa * pb < 0:
            breakevens.append(a + (b - a) * (-pa) / (pb - pa))

    # Beyond the strikes the expiry P&L is a straight line (or flattens for a later-expiry leg); its slope far out
    # says whether profit or loss is unlimited on a rise. A fall stops at zero, so that end is always finite.
    far_up = max(strikes + [spot]) * 4
    slope_up = (pnl_at(far_up, True) - pnl_at(far_up * 0.99, True)) / (far_up * 0.01)
    floor = pnl_at(0.01, True)
    unlimited_profit = slope_up > 1e-3 * qty
    unlimited_loss = slope_up < -1e-3 * qty
    finite = at_expiry + [floor, pnl_at(far_up, True)]
    max_profit = None if unlimited_profit else max(finite)
    max_loss = None if unlimited_loss else min(finite)
    # Where on the index each extreme sits, as runs of levels {"from", "to"} at the near expiry. "from" 0 means the
    # run reaches down to a crash; "to" None means it carries on above the chart (flat beyond the last strike).
    levels = [(0.0, floor)] + list(zip(grid, at_expiry, strict=True)) + [(None, pnl_at(far_up, True))]

    def where(best: float | None) -> list[dict]:
        if best is None:
            return []
        tol = 0.01 + 1e-6 * abs(best)
        runs, run = [], None
        for lvl, p in levels:
            if abs(p - best) <= tol:
                run = run or {"from": lvl, "to": lvl}
                run["to"] = None if lvl is None else round(lvl, 2)
            elif run:
                runs.append(run)
                run = None
        if run:
            runs.append(run)
        return [r for r in runs if r["from"] is not None]   # far-out only: not a level a trader can act on

    # Chance of profit at the near expiry: lognormal index at iv with drift r (risk-neutral), over the profit zones.
    pop = None
    if t_near > 0:
        sd = iv * np.sqrt(t_near)
        mu = np.log(spot) + (rate - iv * iv / 2) * t_near

        def below(x):
            return 0.0 if x <= 0 else float(0.5 * (1 + math.erf((math.log(x) - mu) / (sd * math.sqrt(2)))))

        edges = [0.0] + breakevens + [math.inf]
        pop = 0.0
        for a, b in zip(edges, edges[1:], strict=False):
            mid = (a + b) / 2 if b != math.inf else max(a * 1.5, high * 1.5)
            if pnl_at(max(mid, 0.01), True) > 0:
                pop += (1.0 if b == math.inf else below(b)) - below(a)
        pop = round(pop * 100, 1)

    # Expected move: the index range one standard deviation either side of spot by the near expiry at the title IV
    # (lognormal, about a 68% chance of finishing inside).
    move = None
    if t_near > 0 and iv > 0:
        sd = iv * math.sqrt(t_near)
        move = {"low": round(spot * math.exp(-sd), 2), "high": round(spot * math.exp(sd), 2), "pct": round(sd * 100, 2)}

    net = sum(-g["action"] * g["ratio"] * g["entry"] for g in legs if g["type"] != "FUT")
    greeks = {name: round(sum(g["action"] * g["ratio"] * g["greeks"][name] for g in legs) * qty, 2)
              for name in ("delta", "gamma", "theta", "vega")}
    margin = estimate_margin(legs, spot, qty, margin_pct)
    return {
        "structure": structure, "name": strategy_name(structure, legs), "about": spec["about"], "underlying": underlying,
        "legs_text": legs_text(specs),
        "spot": round(spot, 2), "atm": atm, "step": step, "iv_pct": round(iv * 100, 2), "days": days,
        "far_days": far_days if has_far else None, "lots": lots, "lot_size": lot_size, "quantity": qty,
        "legs": [{"side": "BUY" if g["action"] > 0 else "SELL", "type": g["type"], "strike": g["strike"] or None, "strikes": g["n"],
                  "lots": g["ratio"] * lots, "quantity": g["ratio"] * qty, "expiry": "far" if g["series"] == "far" else "near",
                  "days": g["days"], "premium": round(g["entry"], 2), "source": g["source"],
                  "iv_pct": None if g["type"] == "FUT" else round(g["vol"] * 100, 2), "iv_implied": g["implied"] is not None,
                  "delta": round(g["greeks"]["delta"], 4), "gamma": round(g["greeks"]["gamma"], 5),
                  "theta": round(g["greeks"]["theta"], 2), "vega": round(g["greeks"]["vega"], 2)} for g in legs],
        "net_premium": round(net, 2), "net_rupees": round(net * qty, 2),
        "credit": net > 0, "max_profit": None if max_profit is None else round(max_profit, 2),
        "max_loss": None if max_loss is None else round(max_loss, 2),
        "unlimited_profit": unlimited_profit, "unlimited_loss": unlimited_loss,
        "max_profit_at": where(max_profit), "max_loss_at": where(max_loss),
        "expected_move": move,
        "breakevens": [round(b, 2) for b in breakevens], "pop_pct": pop, "greeks": greeks,
        "margin": round(margin, 0),
        "reward_risk": (round(max_profit / -max_loss, 2) if max_profit is not None and max_loss not in (None, 0) and max_loss < 0
                        else None),
        "payoff": [{"spot": round(s, 2), "expiry": round(e, 2), "today": round(t, 2)} for s, e, t in zip(grid, at_expiry, today, strict=True)],
        "notes": (["Premiums are Black-Scholes at one volatility for every strike unless you type them in; real "
                   "out-of-the-money puts are dearer (skew)."] if any(g["source"] == "model" for g in legs) else [])
                 + ["Margin is an estimate. Check the broker's margin calculator (SPAN + exposure) before trading."]
                 + (["The later-expiry leg is valued at the near expiry at today's volatility; a fall in volatility "
                     "hurts a calendar or diagonal."] if has_far else []),
    }
