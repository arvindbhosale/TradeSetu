"""NSE/BSE cash-market session times and price ticks."""

from __future__ import annotations

import math
from datetime import datetime, time
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OPEN, CLOSE = time(9, 15), time(15, 30)
TICK = 0.05   # a multiple of 0.05 is valid at every tick size the exchanges use


# Exchange holidays as "YYYY-MM-DD". Filled from Settings; the exchange publishes the list each year.
HOLIDAYS: set[str] = set()


def set_holidays(dates) -> None:
    HOLIDAYS.clear()
    HOLIDAYS.update(str(d) for d in dates)


def is_trading_day(moment: datetime) -> bool:
    return moment.weekday() < 5 and moment.strftime("%Y-%m-%d") not in HOLIDAYS


def now_ist() -> datetime:
    return datetime.now(IST)


def session_status(moment: datetime | None = None) -> dict:
    """Whether the cash market is in its regular session.

    Closed on weekends, on the holidays listed in Settings, and outside
    09:15-15:30 on a trading day. A holiday missing from that list reads as
    open here, and the broker rejects the order.
    """
    moment = (moment or now_ist()).astimezone(IST)
    weekday = is_trading_day(moment)
    holiday = moment.weekday() < 5 and not weekday
    is_open = weekday and OPEN <= moment.time() < CLOSE
    if holiday:
        note = "Exchange holiday (from your holiday list in Settings). The market is closed today."
    elif is_open:
        note = "Regular session (09:15-15:30 IST)."
    elif weekday and moment.time() < OPEN:
        note = "Before the open. Orders go as AMO and queue for 09:15 IST."
    else:
        note = "Market closed. Orders go as AMO for the next session."
    return {"open": is_open, "holiday": holiday, "time_ist": moment.strftime("%Y-%m-%d %H:%M:%S"), "note": note}


def round_to_tick(price: float, side: str | None = None, tick: float = TICK) -> float:
    """Round to a valid price. A BUY rounds up and a SELL down, so a marketable
    limit stays marketable; with no side, to the nearest tick."""
    steps = price / tick
    if side and side.upper() == "BUY":
        steps = math.ceil(steps - 1e-9)
    elif side and side.upper() == "SELL":
        steps = math.floor(steps + 1e-9)
    else:
        steps = round(steps)
    return round(steps * tick, 2)


def is_tick_multiple(price: float, tick: float = TICK) -> bool:
    return abs(price / tick - round(price / tick)) < 1e-6
