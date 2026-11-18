"""MCX precious metals: contract specs, the MCX session, and the physical-delivery cut-off.

What the desk trades on MCX: gold and silver futures, and the options on them, through the same Kite account.
Every contract here is **compulsorily delivered**: a position still open when the contract enters its delivery
(staggered delivery / tender) period ends in physical gold or silver changing hands. Zerodha does not take
retail clients into delivery: it publishes a square-off date for each expiry (a bulletin, "MCX precious metals
expiry"), usually 3 to 7 days before expiry, and its risk desk closes what is left. The desk therefore takes no
new position, and its algos close theirs, once a contract is within ``mcx_exit_days`` calendar days of expiry
(Settings; 8 by default, which was early enough for every bulletin from Nov 2025 to Apr 2026). Check the
bulletin for each expiry and raise the setting if Zerodha's date is earlier.

Quantity. Kite takes MCX order quantity in **lots** (the exchange's own convention for commodities: the
instrument list gives lot_size 1). The price is quoted per a smaller unit than a lot, so the rupee value of a
position is ``price x lots x units_per_lot``: GOLD is quoted per 10 g and a lot is 1 kg, so a ₹1 move is ₹100 a
lot. ``units_per_lot`` below is that factor, from MCX's contract specifications. If Kite ever lists one of these
with a lot size other than 1, the quantity is in units instead and the factor is ``units_per_lot / lot_size``;
``multiplier`` handles both.

Session. MCX's non-agricultural session is 09:00 to 23:30 IST while the United States is on daylight time
(mid-March to early November) and 09:00 to 23:55 IST while it is on standard time. MCX holidays are not NSE's:
on many NSE holidays MCX shuts only its morning session and opens at 17:00. They go in Settings as their own
list (``mcx_holidays``): a date closes the whole day, a date followed by "evening" opens only from 17:00.

Expiry dates are never computed here: they come from Kite's instrument list, like every other F&O date.
"""

from __future__ import annotations

import re
from datetime import datetime, time
from zoneinfo import ZoneInfo

from . import market

EXCHANGE = "MCX"
OPEN = time(9, 0)
EVENING_OPEN = time(17, 0)
CLOSE_SUMMER, CLOSE_WINTER = time(23, 30), time(23, 55)
_NEW_YORK = ZoneInfo("America/New_York")

# name -> what one lot is. ``units_per_lot``: rupees per ₹1 move of the quoted price, per lot (MCX specifications).
METALS: dict[str, dict] = {
    "GOLD":       {"label": "Gold", "metal": "gold", "quote": "₹ per 10 g", "lot": "1 kg", "units_per_lot": 100},
    "GOLDM":      {"label": "Gold Mini", "metal": "gold", "quote": "₹ per 10 g", "lot": "100 g", "units_per_lot": 10},
    "GOLDTEN":    {"label": "Gold Ten", "metal": "gold", "quote": "₹ per 10 g", "lot": "10 g", "units_per_lot": 1},
    "GOLDGUINEA": {"label": "Gold Guinea", "metal": "gold", "quote": "₹ per 8 g", "lot": "8 g", "units_per_lot": 1},
    "GOLDPETAL":  {"label": "Gold Petal", "metal": "gold", "quote": "₹ per 1 g", "lot": "1 g", "units_per_lot": 1},
    "SILVER":     {"label": "Silver", "metal": "silver", "quote": "₹ per kg", "lot": "30 kg", "units_per_lot": 30},
    "SILVERM":    {"label": "Silver Mini", "metal": "silver", "quote": "₹ per kg", "lot": "5 kg", "units_per_lot": 5},
    "SILVERMIC":  {"label": "Silver Micro", "metal": "silver", "quote": "₹ per kg", "lot": "1 kg", "units_per_lot": 1},
}
_NAME = re.compile(r"[A-Z]+")


def name_of(tradingsymbol: str) -> str | None:
    """The metal a Kite MCX symbol trades: GOLDM26DECFUT -> GOLDM, SILVER26DEC95000CE -> SILVER."""
    match = _NAME.match(str(tradingsymbol).upper())
    return match.group(0) if match and match.group(0) in METALS else None


def is_metal(exchange: str, tradingsymbol: str) -> bool:
    return str(exchange).upper() == EXCHANGE and name_of(tradingsymbol) is not None


def multiplier(exchange: str, tradingsymbol: str, lot_size: int = 1) -> float:
    """Rupees per ₹1 of price, per unit of order quantity. 1 for anything that is not an MCX metal."""
    if str(exchange).upper() != EXCHANGE:
        return 1
    name = name_of(tradingsymbol)
    if name is None:
        raise ValueError(f"{tradingsymbol} is not an MCX precious-metal contract the desk knows")
    lot = max(int(lot_size or 1), 1)
    units = METALS[name]["units_per_lot"]
    return units // lot if units % lot == 0 else units / lot


# -- the session ----------------------------------------------------------------------------------------------------

HOLIDAYS: dict[str, str] = {}          # "YYYY-MM-DD" -> "closed" | "evening"


def parse_holidays(value) -> list[str]:
    """``mcx_holidays`` as stored: sorted "YYYY-MM-DD" or "YYYY-MM-DD evening". Raises ValueError."""
    if isinstance(value, str):
        value = re.split(r"[\n,;]+", value)
    if not isinstance(value, list) or len(value) > 400:
        raise ValueError("MCX holidays must be a list of dates")
    out: dict[str, str] = {}
    for item in value:
        text = " ".join(str(item).split()).lower()
        if not text:
            continue
        day, _, rest = text.partition(" ")
        try:
            day = datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise ValueError(f"“{str(item)[:24]}” is not a date as YYYY-MM-DD, optionally followed by “evening”") from None
        if rest not in ("", "evening"):
            raise ValueError(f"“{str(item)[:24]}”: after the date write nothing (closed all day) or “evening” "
                             f"(only the evening session opens)")
        out[day] = "evening" if rest else "closed"
    return [d if kind == "closed" else f"{d} evening" for d, kind in sorted(out.items())]


def set_holidays(entries) -> None:
    HOLIDAYS.clear()
    for entry in entries or []:
        day, _, rest = str(entry).partition(" ")
        HOLIDAYS[day] = "evening" if rest.strip() == "evening" else "closed"


def close_time(moment: datetime) -> time:
    """23:55 while New York is on standard time, 23:30 while it is on daylight time (judged at noon IST)."""
    noon = moment.astimezone(market.IST).replace(hour=12, minute=0, second=0, microsecond=0)
    return CLOSE_SUMMER if noon.astimezone(_NEW_YORK).dst() else CLOSE_WINTER


def opens_at(moment: datetime) -> time | None:
    """When MCX opens on this day, or None when it does not trade at all."""
    moment = moment.astimezone(market.IST)
    if moment.weekday() >= 5:
        return None
    kind = HOLIDAYS.get(moment.strftime("%Y-%m-%d"))
    if kind == "closed":
        return None
    return EVENING_OPEN if kind == "evening" else OPEN


def session_status(moment: datetime | None = None) -> dict:
    moment = (moment or market.now_ist()).astimezone(market.IST)
    start, end = opens_at(moment), close_time(moment)
    clock = moment.time()
    is_open = start is not None and start <= clock < end
    hours = f"{start.strftime('%H:%M') if start else '09:00'}-{end.strftime('%H:%M')}"
    if start is None:
        note = ("MCX holiday (from your MCX holiday list in Settings). MCX is closed today." if moment.weekday() < 5
                else "Weekend. MCX is closed.")
    elif is_open:
        note = f"MCX session ({hours} IST)."
    elif clock < start:
        note = f"Before the MCX open ({start.strftime('%H:%M')} IST)."
    else:
        note = "MCX is closed for the day."
    return {"open": is_open, "holiday": moment.weekday() < 5 and start is None, "evening_only": start == EVENING_OPEN,
            "opens": start.strftime("%H:%M") if start else None, "closes": end.strftime("%H:%M"),
            "time_ist": moment.strftime("%Y-%m-%d %H:%M:%S"), "note": note}


def days_to_expiry(expiry: str, moment: datetime) -> int:
    return (datetime.strptime(expiry, "%Y-%m-%d").date() - moment.astimezone(market.IST).date()).days


def in_delivery_window(expiry: str, moment: datetime, exit_days: int) -> bool:
    """Whether a contract is too close to expiry to hold: ``exit_days`` calendar days or fewer to go."""
    return days_to_expiry(expiry, moment) <= exit_days
