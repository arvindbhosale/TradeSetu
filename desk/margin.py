"""Zerodha's own margin for a set of F&O legs, from Kite Connect's basket margin calculator.

Zerodha's kite-mcp-server has no margin tool. The desk's build of it (``deploy/kite-mcp``) adds
``get_basket_margins``, which calls ``POST /margins/basket``: SPAN, exposure and option premium per order,
for the basket as separate orders ("initial") and as one hedged position ("final"). The difference is the
hedge benefit. A server without the tool (Zerodha's hosted mcp.kite.trade, another broker) gets a plain
reason instead of a number: the desk's own estimate is never shown as Zerodha's figure.
"""

from __future__ import annotations

from .kite import KiteError

TOOL = "get_basket_margins"
MAX_LEGS = 20


class MarginUnavailable(Exception):
    """Zerodha's margin cannot be asked for now. ``needs_login`` marks an absent or expired Kite session."""

    def __init__(self, message: str, needs_login: bool = False):
        super().__init__(message)
        self.needs_login = needs_login


def supported(kite) -> bool:
    return bool(getattr(kite, "connected", False) and TOOL in (getattr(kite, "tools", None) or {}))


def why_not(kite) -> str | None:
    """Why Zerodha's margin is not available, or None when it is."""
    broker = getattr(kite, "name", "kite")
    if broker != "kite":
        from .brokers import BROKERS

        label = (BROKERS.get(broker) or {}).get("label") or broker
        return (f"Zerodha's margin calculator works only with Zerodha (Kite); {label} is selected. "
                "Choose Zerodha on the Trade tab to use it.")
    if not getattr(kite, "connected", False):
        return "Log in to Kite on the Trade tab to get Zerodha's margin. It needs a Kite login in paper mode too."
    if TOOL not in (getattr(kite, "tools", None) or {}):
        return ("The connected Kite server has no margin calculator. Zerodha's hosted server does not offer one; "
                "the desk's own kite-mcp-server (deploy/kite-mcp) does.")
    return None


def _number(value) -> float:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0


def _part(block) -> dict:
    block = block if isinstance(block, dict) else {}
    return {"total": round(_number(block.get("total")), 2), "span": round(_number(block.get("span")), 2),
            "exposure": round(_number(block.get("exposure")), 2),
            "premium": round(_number(block.get("option_premium")), 2),
            "additional": round(_number(block.get("additional")), 2)}


def basket(kite, legs: list[dict], product: str = "NRML") -> dict:
    """Zerodha's margin for ``legs``: dicts with exchange, tradingsymbol, side (BUY/SELL), quantity and optionally
    price (the premium to value a bought option at; Kite uses the last price without it).

    ``total`` is what the position blocks with the hedge benefit, ``without_hedge`` what the legs would block as
    separate orders. Raises MarginUnavailable."""
    reason = why_not(kite)
    if reason:
        raise MarginUnavailable(reason, needs_login=not getattr(kite, "connected", False))
    if not legs:
        raise MarginUnavailable("Add at least one leg.")
    if len(legs) > MAX_LEGS:
        raise MarginUnavailable(f"At most {MAX_LEGS} legs at a time.")
    orders = []
    for g in legs:
        side = str(g.get("side", "")).upper()
        quantity = int(g.get("quantity") or 0)
        if side not in ("BUY", "SELL") or quantity <= 0 or not g.get("tradingsymbol") or not g.get("exchange"):
            raise MarginUnavailable("Each leg needs a contract, Buy or Sell, and a quantity above zero.")
        price = _number(g.get("price"))
        orders.append({"exchange": str(g["exchange"]).upper(), "tradingsymbol": str(g["tradingsymbol"]).upper(),
                       "transaction_type": side, "quantity": quantity, "product": product,
                       "variety": "regular", **({"order_type": "LIMIT", "price": price} if price > 0
                                                else {"order_type": "MARKET"})})
    try:
        answer = kite.call(TOOL, {"orders": orders, "consider_positions": False}, timeout=30.0)
    except KiteError as exc:
        text = str(exc)
        if exc.needs_login:
            raise MarginUnavailable("Log in to Kite on the Trade tab to get Zerodha's margin (the Kite session has "
                                    "ended or not started).", needs_login=True) from None
        raise MarginUnavailable(f"Zerodha did not work out the margin: {text}") from None
    if not isinstance(answer, dict) or not isinstance(answer.get("final"), dict):
        raise MarginUnavailable("Zerodha's margin calculator gave an answer the desk does not understand.")
    final, initial = _part(answer["final"]), _part(answer.get("initial"))
    per_order = [o for o in answer.get("orders") or [] if isinstance(o, dict)]
    rows = []
    for n, g in enumerate(orders):
        match = per_order[n] if n < len(per_order) else {}
        rows.append({"exchange": g["exchange"], "tradingsymbol": g["tradingsymbol"], "side": g["transaction_type"],
                     "quantity": g["quantity"], **_part(match)})
    without = max(initial["total"], final["total"])
    return {"source": "kite", "product": product, "total": final["total"], "span": final["span"],
            "exposure": final["exposure"], "premium": final["premium"], "additional": final["additional"],
            "without_hedge": round(without, 2), "hedge_benefit": round(without - final["total"], 2), "legs": rows}
