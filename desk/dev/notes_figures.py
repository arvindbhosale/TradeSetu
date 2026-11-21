"""Figures for the learning notes (desk/static/notes/LEARNING_NOTES.md).

Run from the repository root:

    pip install matplotlib
    python -m desk.dev.notes_figures                 # option payoffs, Greeks, risk, SEBI study
    python -m desk.dev.notes_figures --data DIR      # also the price charts, from end-of-day CSVs

Every chart is either exact arithmetic (payoffs at expiry, Black-Scholes values for stated inputs) or real
published data: SEBI's studies of individual F&O traders, and NSE closing prices (Yahoo Finance end-of-day
CSVs, one file per symbol, as written by the notes-data fetch). Nothing here is simulated or made up.
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import numpy as np

OUT = Path(__file__).resolve().parent.parent / "static" / "notes" / "img"

# Validated categorical order (blue, orange, aqua, yellow) and the usual up/down pair for candles and P&L.
BLUE, ORANGE, AQUA, YELLOW, VIOLET = "#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#6250d6"
GREEN, RED = "#008300", "#e34948"
INK, INK2, MUTED, GRID, SURFACE = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e0", "#ffffff"


def _plt():
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({
        "font.family": ["Inter", "DejaVu Sans"], "font.size": 10, "axes.edgecolor": GRID, "axes.labelcolor": INK2,
        "axes.titlesize": 11, "axes.titleweight": "semibold", "axes.titlecolor": INK, "axes.titlelocation": "left",
        "xtick.color": INK2, "ytick.color": INK2, "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.8,
        "axes.spines.top": False, "axes.spines.right": False, "figure.facecolor": SURFACE,
        "axes.facecolor": SURFACE, "legend.frameon": False, "lines.linewidth": 2, "savefig.dpi": 160,
    })
    return plt


def _save(fig, name: str, source: str = "") -> None:
    if source:
        fig.text(0.01, 0.005, source, fontsize=7.5, color=MUTED, ha="left", va="bottom")
    OUT.mkdir(parents=True, exist_ok=True)
    fig.savefig(OUT / name, bbox_inches="tight", facecolor=SURFACE)
    import matplotlib.pyplot as plt

    plt.close(fig)
    from PIL import Image

    # 256 colours keep line art sharp and cut the file (and the PDF) to about a third.
    with Image.open(OUT / name) as image:
        small = image.convert("RGB").quantize(256, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    small.save(OUT / name, optimize=True)


def _rupees(ax, axis: str = "y") -> None:
    from matplotlib.ticker import FuncFormatter

    fmt = FuncFormatter(lambda v, _: f"₹{v:,.0f}" if v >= 0 else f"−₹{-v:,.0f}")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)


def _thousands(ax, axis: str = "x") -> None:
    from matplotlib.ticker import FuncFormatter

    (ax.xaxis if axis == "x" else ax.yaxis).set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.0f}"))


# --- Black-Scholes (European options; NSE index options are European and cash-settled) -------------------------

def _ncdf(x):
    return 0.5 * (1.0 + np.vectorize(math.erf)(np.asarray(x) / math.sqrt(2.0)))


def _npdf(x):
    return np.exp(-0.5 * np.asarray(x) ** 2) / math.sqrt(2 * math.pi)


def bs(spot, strike, years, vol, rate, kind="call"):
    """Price, delta, gamma, theta (per day), vega (per 1 vol point) of a European option."""
    spot = np.asarray(spot, dtype=float)
    years = np.maximum(np.asarray(years, dtype=float), 1e-9)
    d1 = (np.log(spot / strike) + (rate + 0.5 * vol * vol) * years) / (vol * np.sqrt(years))
    d2 = d1 - vol * np.sqrt(years)
    disc = np.exp(-rate * years)
    if kind == "call":
        price = spot * _ncdf(d1) - strike * disc * _ncdf(d2)
        delta = _ncdf(d1)
        theta = -spot * _npdf(d1) * vol / (2 * np.sqrt(years)) - rate * strike * disc * _ncdf(d2)
    else:
        price = strike * disc * _ncdf(-d2) - spot * _ncdf(-d1)
        delta = _ncdf(d1) - 1
        theta = -spot * _npdf(d1) * vol / (2 * np.sqrt(years)) + rate * strike * disc * _ncdf(-d2)
    gamma = _npdf(d1) / (spot * vol * np.sqrt(years))
    vega = spot * _npdf(d1) * np.sqrt(years) / 100
    return price, delta, gamma, theta / 365, vega


# The worked examples use one stated, hypothetical set-up so every number can be checked by hand.
SPOT, LOT, RATE, VOL, DAYS = 26000.0, 65, 0.06, 0.13, 7
SETUP = (f"Illustration: Nifty at {SPOT:,.0f}, lot {LOT}, implied volatility {VOL:.0%}, rate {RATE:.0%}, "
         f"{DAYS} days to expiry. Premiums are Black-Scholes values for these inputs, not market quotes.")


def premium(strike, kind):
    return float(bs(SPOT, strike, DAYS / 365, VOL, RATE, kind)[0])


def leg_payoff(spots, kind, strike, qty, prem):
    """P&L at expiry of one leg, per unit. qty +1 bought, -1 sold."""
    if kind == "fut":
        return qty * (spots - strike)
    intrinsic = np.maximum(spots - strike, 0) if kind == "call" else np.maximum(strike - spots, 0)
    return qty * (intrinsic - prem)


def strategy(legs, spots):
    """legs: (kind, strike, qty). Returns P&L per lot at expiry and the premiums used."""
    total, used = np.zeros_like(spots), []
    for kind, strike, qty in legs:
        prem = 0.0 if kind == "fut" else round(premium(strike, kind), 1)
        used.append((kind, strike, qty, prem))
        total = total + leg_payoff(spots, kind, strike, qty, prem)
    return total * LOT, used


def _payoff_ax(ax, spots, pnl, title, color=BLUE, note=""):
    ax.axhline(0, color=INK2, linewidth=1)
    ax.fill_between(spots, pnl, 0, where=pnl >= 0, color=GREEN, alpha=0.10, linewidth=0)
    ax.fill_between(spots, pnl, 0, where=pnl < 0, color=RED, alpha=0.10, linewidth=0)
    ax.plot(spots, pnl, color=color)
    ax.axvline(SPOT, color=MUTED, linewidth=1, linestyle=":")
    ax.set_title(title)
    ax.set_xlabel("Nifty at expiry")
    _thousands(ax)
    _rupees(ax)
    if note:
        ax.text(0.02, 0.96, note, transform=ax.transAxes, fontsize=8, color=INK2, va="top")


def _legs_note(used):
    words = []
    for kind, strike, qty, prem in used:
        side = "Buy" if qty > 0 else "Sell"
        if kind == "fut":
            words.append(f"{side} future at {strike:,.0f}")
        else:
            words.append(f"{side} {strike:,.0f} {'CE' if kind == 'call' else 'PE'} @ {prem:,.1f}")
    return "\n".join(words)


def payoff_figures(plt) -> None:
    spots = np.linspace(SPOT * 0.94, SPOT * 1.06, 600)

    # Futures
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    for ax, qty, title in ((axes[0], 1, "Buy one Nifty future"), (axes[1], -1, "Sell one Nifty future")):
        pnl, used = strategy([("fut", SPOT, qty)], spots)
        _payoff_ax(ax, spots, pnl, title, note=_legs_note(used))
    _save(fig, "fut-payoff.png", f"Profit or loss at expiry for one lot of {LOT}. Each 1-point move = ₹{LOT}. "
                                 "Charges and taxes not included.")

    # The four single options
    atm = SPOT
    fig, axes = plt.subplots(2, 2, figsize=(10, 7))
    cases = [("call", 1, "Buy a call (CE)"), ("put", 1, "Buy a put (PE)"),
             ("call", -1, "Sell a call (CE)"), ("put", -1, "Sell a put (PE)")]
    for ax, (kind, qty, title) in zip(axes.flat, cases, strict=True):
        pnl, used = strategy([(kind, atm, qty)], spots)
        _payoff_ax(ax, spots, pnl, title, note=_legs_note(used))
    fig.tight_layout()
    _save(fig, "opt-four-basics.png", SETUP)

    specs = {
        "opt-covered-protective.png": [
            ("Covered call", [("fut", SPOT, 1), ("call", 26300, -1)]),
            ("Protective put", [("fut", SPOT, 1), ("put", 25700, 1)]),
        ],
        "opt-spreads.png": [
            ("Bull call spread", [("call", 26000, 1), ("call", 26300, -1)]),
            ("Bear put spread", [("put", 26000, 1), ("put", 25700, -1)]),
        ],
        "opt-long-vol.png": [
            ("Long straddle", [("call", 26000, 1), ("put", 26000, 1)]),
            ("Long strangle", [("call", 26300, 1), ("put", 25700, 1)]),
        ],
        "opt-short-vol.png": [
            ("Short straddle", [("call", 26000, -1), ("put", 26000, -1)]),
            ("Short strangle", [("call", 26300, -1), ("put", 25700, -1)]),
        ],
        "opt-condor-fly.png": [
            ("Iron condor", [("call", 26300, -1), ("call", 26600, 1), ("put", 25700, -1), ("put", 25400, 1)]),
            ("Iron butterfly", [("call", 26000, -1), ("put", 26000, -1), ("call", 26400, 1), ("put", 25600, 1)]),
        ],
    }
    for name, pair in specs.items():
        fig, axes = plt.subplots(1, 2, figsize=(10, 3.9))
        for ax, (title, legs) in zip(axes, pair, strict=True):
            pnl, used = strategy(legs, spots)
            _payoff_ax(ax, spots, pnl, title, note=_legs_note(used))
        fig.tight_layout()
        _save(fig, name, SETUP)


def greek_figures(plt) -> None:
    spots = np.linspace(24500, 27500, 400)
    strike = SPOT

    # Delta against spot for three times to expiry.
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    for days, color in ((30, BLUE), (7, ORANGE), (1, AQUA)):
        _, dc, g, _, _ = bs(spots, strike, days / 365, VOL, RATE, "call")
        _, dp, _, _, _ = bs(spots, strike, days / 365, VOL, RATE, "put")
        axes[0].plot(spots, dc, color=color, label=f"{days} day{'s' if days > 1 else ''} left")
        axes[0].plot(spots, dp, color=color, linestyle="--")
        axes[1].plot(spots, g * 100, color=color, label=f"{days} day{'s' if days > 1 else ''} left")
    axes[0].set_title("Delta of the 26,000 CE (solid) and PE (dashed)")
    axes[0].set_ylabel("Delta")
    axes[1].set_title("Gamma: how fast delta changes")
    axes[1].set_ylabel("Change in delta per 100-point move")
    for ax in axes:
        ax.set_xlabel("Nifty")
        _thousands(ax)
        ax.axvline(strike, color=MUTED, linewidth=1, linestyle=":")
        ax.legend(fontsize=8, loc="lower right" if ax is axes[0] else "upper right")
    fig.tight_layout()
    _save(fig, "greeks-delta-gamma.png", f"Black-Scholes, strike 26,000, implied volatility {VOL:.0%}, rate {RATE:.0%}.")

    # Theta: value of an at-the-money option as expiry approaches.
    days = np.linspace(45, 0.05, 500)
    price, _, _, theta, _ = bs(SPOT, strike, days / 365, VOL, RATE, "call")
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.8))
    axes[0].plot(days, price, color=BLUE)
    axes[0].invert_xaxis()
    axes[0].set_title("Time decay: 26,000 CE value, Nifty unchanged at 26,000")
    axes[0].set_xlabel("Days to expiry")
    axes[0].set_ylabel("Premium (points)")
    axes[1].plot(days, -theta * LOT, color=ORANGE)
    axes[1].invert_xaxis()
    axes[1].set_title("Loss per day from time decay (one lot)")
    axes[1].set_xlabel("Days to expiry")
    _rupees(axes[1])
    fig.tight_layout()
    _save(fig, "greeks-theta.png", f"Black-Scholes, implied volatility {VOL:.0%}, rate {RATE:.0%}. "
                                   "Decay speeds up in the last week.")

    # Vega: value against implied volatility.
    vols = np.linspace(0.08, 0.30, 200)
    fig, ax = plt.subplots(figsize=(10, 3.6))
    for days, color in ((30, BLUE), (7, ORANGE)):
        price, *_ = bs(SPOT, strike, days / 365, vols, RATE, "call")
        ax.plot(vols * 100, price, color=color, label=f"{days} days left")
    ax.set_title("Vega: 26,000 CE value as implied volatility changes, Nifty unchanged")
    ax.set_xlabel("Implied volatility (%)")
    ax.set_ylabel("Premium (points)")
    ax.legend(fontsize=8)
    _save(fig, "greeks-vega.png", f"Black-Scholes, Nifty and strike 26,000, rate {RATE:.0%}. "
                                  "Longer-dated options react more to volatility.")


def risk_figures(plt) -> None:
    # Gain needed to recover a loss: g = 1/(1-l) - 1.
    loss = np.linspace(1, 90, 300)
    gain = (1 / (1 - loss / 100) - 1) * 100
    fig, ax = plt.subplots(figsize=(10, 3.6))
    ax.plot(loss, gain, color=RED)
    for point in (10, 25, 50, 75):
        need = (1 / (1 - point / 100) - 1) * 100
        ax.plot([point], [need], "o", color=RED, markersize=6, markeredgecolor=SURFACE, markeredgewidth=2)
        ax.annotate(f"−{point}% needs +{need:.0f}%", (point, need), textcoords="offset points", xytext=(8, -4),
                    fontsize=8.5, color=INK)
    ax.set_ylim(0, 400)
    ax.set_title("A loss needs a bigger gain to get back to where you started")
    ax.set_xlabel("Loss on capital (%)")
    ax.set_ylabel("Gain needed (%)")
    _save(fig, "risk-recovery.png", "Arithmetic: gain needed = 1 ÷ (1 − loss) − 1.")

    # Capital left after a run of losses at different risk per trade.
    streak = np.arange(0, 21)
    fig, ax = plt.subplots(figsize=(10, 3.6))
    for risk, color in ((0.01, BLUE), (0.02, AQUA), (0.05, YELLOW), (0.10, RED)):
        left = 100 * (1 - risk) ** streak
        ax.plot(streak, left, color=color, label=f"{risk:.0%} of capital risked per trade")
        ax.annotate(f"{left[-1]:.0f}%", (streak[-1], left[-1]), textcoords="offset points", xytext=(6, -3),
                    fontsize=8.5, color=INK)
    ax.set_title("Capital left after losing trades in a row")
    ax.set_xlabel("Losing trades in a row")
    ax.set_ylabel("Capital left (%)")
    ax.set_xticks(range(0, 21, 2))
    ax.legend(fontsize=8, loc="lower left")
    _save(fig, "risk-streak.png", "Arithmetic: capital left = (1 − risk per trade) ^ losses.")


# SEBI studies of individual traders in equity F&O. FY22-FY25: SEBI, "Comparative study of growth in Equity
# Derivatives Segment vis-a-vis Cash Market after recent measures", July 2025, Table 11. FY26: SEBI press release
# 50/2026, 20 August 2026 (study of FY25-FY26, top 15 brokers).
SEBI = [("FY22", 90.2, 40824), ("FY23", 91.7, 65747), ("FY24", 91.1, 74812), ("FY25", 91.0, 105603),
        ("FY26", 87.7, 91685)]


def sebi_figure(plt) -> None:
    years = [r[0] for r in SEBI]
    fig, axes = plt.subplots(1, 2, figsize=(10, 3.6))
    bars = axes[0].bar(years, [r[1] for r in SEBI], color=RED, width=0.55)
    axes[0].bar_label(bars, labels=[f"{r[1]:.1f}%" for r in SEBI], fontsize=8.5, padding=3, color=INK)
    axes[0].set_ylim(0, 100)
    axes[0].set_title("Individual F&O traders who lost money")
    bars = axes[1].bar(years, [r[2] for r in SEBI], color=RED, width=0.55)
    axes[1].bar_label(bars, labels=[f"₹{r[2]:,}" for r in SEBI], fontsize=8.5, padding=3, color=INK)
    axes[1].set_title("Net loss of individual traders (₹ crore)")
    axes[1].set_ylim(0, 125000)
    axes[1].yaxis.set_major_formatter(__import__("matplotlib").ticker.FuncFormatter(lambda v, _: f"{v:,.0f}"))
    for ax in axes:
        ax.grid(axis="x", visible=False)
    fig.tight_layout()
    _save(fig, "sebi-fno-losses.png", "Source: SEBI study, July 2025, Table 11 (FY22–FY25); SEBI press release "
                                      "50/2026, 20 Aug 2026 (FY26). Losses after transaction costs.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", type=Path, help="folder of end-of-day CSVs for the price charts")
    args = parser.parse_args()
    plt = _plt()
    payoff_figures(plt)
    greek_figures(plt)
    risk_figures(plt)
    sebi_figure(plt)
    if args.data:
        from desk.dev import notes_price_figures

        notes_price_figures.build(plt, args.data, _save)
    print(f"Figures written to {OUT}")
    if (OUT.parent / "notes-stats.json").exists():
        from desk.dev import notes_text

        print(f"Notes written to {notes_text.write()}")


if __name__ == "__main__":
    main()
