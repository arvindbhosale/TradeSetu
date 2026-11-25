"""Statutory charges and brokerage on an NSE/BSE equity, F&O or MCX commodity order.

The rates are Zerodha's published equity schedule. Exchanges, SEBI and the
Finance Act revise them; ``ChargeSchedule`` is the one place to change.
STT and stamp duty are rounded by the exchange per contract note, so a real
contract note can differ from these figures by a rupee or two.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field


@dataclass(frozen=True)
class ChargeSchedule:
    brokerage_delivery_pct: float = 0.0          # equity delivery is free
    brokerage_intraday_pct: float = 0.0003       # 0.03% ...
    brokerage_cap: float = 20.0                  # ... or Rs 20 an order, whichever is lower
    stt_delivery_pct: float = 0.001              # 0.1% on buy and on sell
    stt_intraday_sell_pct: float = 0.00025       # 0.025% on the sell side
    exchange_txn_pct: dict = field(default_factory=lambda: {"NSE": 0.0000297, "BSE": 0.0000375})
    sebi_pct: float = 0.000001                   # Rs 10 a crore
    stamp_delivery_buy_pct: float = 0.00015      # 0.015% on buy
    stamp_intraday_buy_pct: float = 0.00003      # 0.003% on buy
    gst_pct: float = 0.18                        # on brokerage, exchange and SEBI charges
    dp_charge_per_sell: float = 15.34            # per scrip per day on a delivery sell, GST included
    # Options (on premium). Brokerage is a flat Rs 20 an order.
    options_brokerage: float = 20.0
    options_stt_sell_pct: float = 0.0015         # 0.15% of premium on the sell side (from 1 April 2026)
    options_exchange_txn_pct: dict = field(default_factory=lambda: {"NFO": 0.0003553, "BFO": 0.000325})
    options_stamp_buy_pct: float = 0.00003       # 0.003% on buy
    # Futures (on contract value). Brokerage 0.03% or Rs 20 an order, whichever is lower.
    futures_brokerage_pct: float = 0.0003
    futures_stt_sell_pct: float = 0.0005         # 0.05% on the sell side (from 1 April 2026)
    futures_exchange_txn_pct: dict = field(default_factory=lambda: {"NFO": 0.0000183, "BFO": 0.0})
    futures_stamp_buy_pct: float = 0.00002       # 0.002% on buy
    # MCX non-agricultural commodities (gold, silver). Futures on contract value, options on premium.
    mcx_futures_ctt_sell_pct: float = 0.0001     # CTT 0.01% on the sell side
    mcx_futures_txn_pct: float = 0.000021        # MCX 0.0021%
    mcx_options_ctt_sell_pct: float = 0.0005     # CTT 0.05% of premium on the sell side
    mcx_options_txn_pct: float = 0.000418        # MCX 0.0418% of premium


DEFAULT_SCHEDULE = ChargeSchedule()


@dataclass(frozen=True)
class Charges:
    turnover: float
    brokerage: float
    stt: float
    exchange_txn: float
    sebi: float
    stamp_duty: float
    gst: float
    dp: float

    @property
    def total(self) -> float:
        return (self.brokerage + self.stt + self.exchange_txn + self.sebi
                + self.stamp_duty + self.gst + self.dp)

    def as_dict(self) -> dict:
        return {**{k: round(v, 2) for k, v in asdict(self).items()}, "total": round(self.total, 2)}


def order_charges(
    side: str,
    quantity: float,
    price: float,
    product: str = "CNC",
    exchange: str = "NSE",
    schedule: ChargeSchedule = DEFAULT_SCHEDULE,
    include_dp: bool = True,
) -> Charges:
    """Charges on one executed order. ``product`` CNC is delivery, MIS intraday."""
    side, product, exchange = side.upper(), product.upper(), exchange.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    if exchange in ("NFO", "BFO"):
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be positive")
        turnover = quantity * price               # the premium
        brokerage = min(schedule.options_brokerage, turnover)
        stt = turnover * schedule.options_stt_sell_pct if side == "SELL" else 0.0
        txn = turnover * schedule.options_exchange_txn_pct[exchange]
        sebi = turnover * schedule.sebi_pct
        stamp = turnover * schedule.options_stamp_buy_pct if side == "BUY" else 0.0
        gst = (brokerage + txn + sebi) * schedule.gst_pct
        return Charges(turnover, brokerage, stt, txn, sebi, stamp, gst, 0.0)
    if product not in ("CNC", "MIS"):
        raise ValueError(f"product must be CNC or MIS, got {product!r}")
    if exchange not in schedule.exchange_txn_pct:
        raise ValueError(f"no charge schedule for exchange {exchange!r}")
    if quantity <= 0 or price <= 0:
        raise ValueError("quantity and price must be positive")

    turnover = quantity * price
    delivery = product == "CNC"
    if delivery:
        brokerage = turnover * schedule.brokerage_delivery_pct
        stt = turnover * schedule.stt_delivery_pct
        stamp = turnover * schedule.stamp_delivery_buy_pct if side == "BUY" else 0.0
        dp = schedule.dp_charge_per_sell if side == "SELL" and include_dp else 0.0
    else:
        brokerage = min(turnover * schedule.brokerage_intraday_pct, schedule.brokerage_cap)
        stt = turnover * schedule.stt_intraday_sell_pct if side == "SELL" else 0.0
        stamp = turnover * schedule.stamp_intraday_buy_pct if side == "BUY" else 0.0
        dp = 0.0
    txn = turnover * schedule.exchange_txn_pct[exchange]
    sebi = turnover * schedule.sebi_pct
    gst = (brokerage + txn + sebi) * schedule.gst_pct
    return Charges(turnover, brokerage, stt, txn, sebi, stamp, gst, dp)


def futures_charges(side: str, quantity: float, price: float, exchange: str = "NFO",
                    schedule: ChargeSchedule = DEFAULT_SCHEDULE) -> Charges:
    """Charges on one executed futures order (contract value = quantity x price)."""
    side, exchange = side.upper(), exchange.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    if exchange not in ("NFO", "BFO"):
        raise ValueError("futures trade on NFO or BFO")
    if quantity <= 0 or price <= 0:
        raise ValueError("quantity and price must be positive")
    turnover = quantity * price
    brokerage = min(turnover * schedule.futures_brokerage_pct, schedule.brokerage_cap)
    stt = turnover * schedule.futures_stt_sell_pct if side == "SELL" else 0.0
    txn = turnover * schedule.futures_exchange_txn_pct[exchange]
    sebi = turnover * schedule.sebi_pct
    stamp = turnover * schedule.futures_stamp_buy_pct if side == "BUY" else 0.0
    gst = (brokerage + txn + sebi) * schedule.gst_pct
    return Charges(turnover, brokerage, stt, txn, sebi, stamp, gst, 0.0)


def commodity_charges(side: str, quantity: float, price: float, multiplier: float, option: bool,
                      schedule: ChargeSchedule = DEFAULT_SCHEDULE) -> Charges:
    """Charges on one executed MCX (non-agri) order. ``quantity`` is in lots as Kite takes it; ``multiplier`` turns
    lots x price into rupees (100 for GOLD, quoted per 10 g in 1 kg lots). Brokerage: futures 0.03% or ₹20, options
    a flat ₹20. Physical delivery is charged extra by Zerodha (0.25%); the desk never takes a contract to delivery."""
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    if quantity <= 0 or price <= 0 or multiplier <= 0:
        raise ValueError("quantity, price and multiplier must be positive")
    turnover = quantity * price * multiplier
    if option:
        brokerage = min(schedule.options_brokerage, turnover)
        ctt = turnover * schedule.mcx_options_ctt_sell_pct if side == "SELL" else 0.0
        txn = turnover * schedule.mcx_options_txn_pct
        stamp = turnover * schedule.options_stamp_buy_pct if side == "BUY" else 0.0
    else:
        brokerage = min(turnover * schedule.futures_brokerage_pct, schedule.brokerage_cap)
        ctt = turnover * schedule.mcx_futures_ctt_sell_pct if side == "SELL" else 0.0
        txn = turnover * schedule.mcx_futures_txn_pct
        stamp = turnover * schedule.futures_stamp_buy_pct if side == "BUY" else 0.0
    sebi = turnover * schedule.sebi_pct
    gst = (brokerage + txn + sebi) * schedule.gst_pct
    return Charges(turnover, brokerage, ctt, txn, sebi, stamp, gst, 0.0)


def same_day_delivery_charges(fills: list[dict], schedule: ChargeSchedule = DEFAULT_SCHEDULE) -> list[float | None]:
    """Charges on delivery (CNC) share fills once the day is netted: shares bought and sold on the same day are
    squared off at the exchange, never delivered, so that quantity pays intraday rates (no delivery STT or DP).

    ``fills`` are dicts with time_ist, side, quantity, price, product, exchange and tradingsymbol (and mode, kept
    apart). Returns, for each fill in the order given, its charges after netting, or None where nothing changes.
    """
    out: list[float | None] = [None] * len(fills)
    days: dict[tuple, list[int]] = {}
    for i, fill in enumerate(fills):
        if str(fill.get("product", "")).upper() != "CNC" or fill.get("exchange") not in schedule.exchange_txn_pct:
            continue
        key = (fill.get("mode"), fill["exchange"], fill["tradingsymbol"], str(fill["time_ist"])[:10])
        days.setdefault(key, []).append(i)
    for members in days.values():
        members.sort(key=lambda i: str(fills[i]["time_ist"]))
        bought = sum(fills[i]["quantity"] for i in members if str(fills[i]["side"]).upper() == "BUY")
        sold = sum(fills[i]["quantity"] for i in members if str(fills[i]["side"]).upper() == "SELL")
        left = {"BUY": min(bought, sold), "SELL": min(bought, sold)}
        if not left["BUY"]:
            continue
        for i in members:
            fill = fills[i]
            side, quantity, price = str(fill["side"]).upper(), fill["quantity"], fill["price"]
            intraday = min(quantity, left[side])
            if not intraday or quantity <= 0 or price <= 0:
                continue
            left[side] -= intraday
            total = order_charges(side, intraday, price, "MIS", fill["exchange"], schedule).total
            if quantity > intraday:
                total += order_charges(side, quantity - intraday, price, "CNC", fill["exchange"], schedule).total
            out[i] = round(total, 2)
    return out
