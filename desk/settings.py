"""The desk's settings: one JSON file in the user's home, read and written whole."""

from __future__ import annotations

import contextlib
import copy
import json
import os
import re
import threading
from pathlib import Path

from tradingagents.llm_clients.api_key_env import PROVIDER_API_KEY_ENV

# The MCP server entry the desk starts with: Zerodha's hosted Kite server
# through mcp-remote, as Zerodha documents it for desktop clients.
DEFAULT_MCP_CONFIG = {
    "mcpServers": {
        "kite": {
            "command": "npx",
            "args": ["mcp-remote", "https://mcp.kite.trade/mcp"],
        }
    }
}

DEFAULTS: dict = {
    # Agents
    "llm_provider": "openai",
    "deep_think_llm": "",        # empty: the provider default from TradingAgents
    "quick_think_llm": "",
    "backend_url": "",
    "analysts": ["market", "social", "news", "fundamentals"],
    "max_debate_rounds": 1,
    "max_risk_discuss_rounds": 1,
    "output_language": "English",
    "default_exchange": "NSE",
    # Keys by environment variable name. Stored in this file, readable only by
    # the user; never returned by the API except masked.
    "api_keys": {},
    # Broker: "kite" (Zerodha, through the MCP server below) or one of desk.brokers.BROKERS.
    "broker": "kite",
    # Credentials for the other brokers, by broker then field. Secret fields are masked to the UI.
    "broker_keys": {},
    # Your own Kite Connect app (developers.kite.trade). With both saved the desk talks to Kite directly:
    # no Node.js or MCP server, orders and margin included, one login a day. The secret is masked to the UI.
    "kite_connect": {"api_key": "", "api_secret": ""},
    "mcp": DEFAULT_MCP_CONFIG,
    "mcp_server": "kite",
    # Trading
    "paper_capital": 1_000_000.0,
    "live_enabled": False,       # live orders stay off until switched on here
    "kill_switch": False,        # blocks every new order, paper and live
    "product": "CNC",            # CNC (delivery) or MIS (intraday)
    "position_pct": 5.0,         # % of capital a full Buy allocates
    "limit_buffer_pct": 0.3,     # marketable-limit distance from the last price
    "max_order_value": 50_000.0,
    "max_orders_per_day": 10,
    "max_position_pct": 20.0,    # ceiling on one holding, % of capital
    # Risk per trade. The order ticket sizes a buy so that its stoploss loses risk_per_trade_pct of the capital;
    # a manual order whose stoploss would lose more than max_risk_pct (0 = no check) is refused. risk_capital is
    # the capital these are a share of (0 = the paper capital). require_stoploss refuses a manual share buy that
    # gives no stoploss.
    "risk_capital": 0.0,
    "risk_per_trade_pct": 1.0,
    "max_risk_pct": 2.0,
    "require_stoploss": False,
    # F&O algos may run structures with sold options no bought option covers (short straddles, strangles,
    # ratio spreads, synthetics) live only when this is on. Paper is not gated.
    "fno_naked_live": False,
    # MCX gold and silver. New MCX positions only inside this window (and the MCX session, 09:00 to 23:30/23:55).
    "mcx_trade_start": "09:00",
    "mcx_trade_end": "23:00",
    # Every MCX gold/silver contract is physically delivered. No new position, and algos close theirs, once a
    # contract is this many calendar days or fewer from expiry. Zerodha squares off before the delivery period.
    "mcx_exit_days": 8,
    # About what Zerodha blocks for an MCX future (SPAN + ELM + any additional margin MCX levies), % of value.
    "mcx_margin_pct": 15.0,
    # MCX holidays: "YYYY-MM-DD" closes the day, "YYYY-MM-DD evening" opens only the 17:00 session.
    "mcx_holidays": [],
    # How Uptrend / Downtrend / Sideways is decided on the Markets and Indices boards.
    "trend_rule": {"method": "ma", "fast": 20, "slow": 50, "period": 50, "band_pct": 1.0,
                   "st_period": 10, "st_multiplier": 3.0, "lookback": 20, "threshold_pct": 3.0},
    # Daily loss limit, in rupees, per account (paper and live separately). When
    # today's loss reaches it the kill switch turns on and intraday algos close
    # their positions. 0 switches the limit off.
    "daily_loss_limit": 0.0,
    # Supervisor (desk/supervisor.py): off, watch (alerts only) or protect (may hold entries and close positions).
    "supervisor_mode": "off",
    "supervisor_ai": False,          # also ask the AI service in Settings → AI, every supervisor_minutes
    "supervisor_minutes": 15,
    "supervisor_live": False,        # protect mode acts on live algos too (otherwise suggestions only)
    "supervisor_loss_pct": 10.0,     # close a stock algo's position this far below its buy price (0: off)
    "supervisor_max_actions": 3,     # automatic AI actions per day
    # Alerts
    "alerts_enabled": False,
    "telegram_chat_id": "",
    # Exchange holidays, one YYYY-MM-DD each. The exchange publishes the list every year.
    "holidays": [],
    # Trading window, IST. New orders are taken only between trade_start and
    # trade_end; intraday (MIS) positions are closed at square_off.
    "enforce_trade_window": True,
    "trade_start": "09:30",
    "trade_end": "15:00",
    "square_off": "15:00",
    # Charts from Yahoo price history. Off hides the Charts tab.
    "charts_enabled": True,
    # Where chart candles come from: "kite" (Kite's historical data only) or
    # "kite_then_yahoo" (Yahoo's delayed data when Kite cannot serve the chart).
    "chart_source": "kite",
    # The index strip along the top of every page, and which indices it shows (ids from desk/ticker.py).
    "ticker_show": True,
    "ticker_indices": ["nifty50", "bsesensex", "niftybank", "niftyfinance", "niftynext50", "niftymidcap100",
                       "niftysmallcap100", "indiavix"],
    # Backtest
    # The live option chain of these indices is saved every chain_recorder_minutes during the session (near two
    # expiries, ATM ± 20 strikes), so intraday option backtests can use real premiums later. Empty switches it off.
    "chain_recorder": ["NIFTY"],
    "chain_recorder_minutes": 5,
    "risk_free_rate_pct": 6.5,   # annual, roughly the 91-day T-bill yield
    "slippage_bps": 5.0,
}

_SECRET_ENV_NAMES = frozenset(v for v in PROVIDER_API_KEY_ENV.values() if v) | {
    "FRED_API_KEY", "ALPHA_VANTAGE_API_KEY", "TELEGRAM_BOT_TOKEN",
}


def home() -> Path:
    # TRADESETU_HOME; BHARAT_DESK_HOME is the old name and still works.
    base = os.environ.get("TRADESETU_HOME") or os.environ.get("BHARAT_DESK_HOME")
    path = Path(base) if base else Path.home() / ".tradingagents" / "desk"
    path.mkdir(parents=True, exist_ok=True)
    return path


def mask(value: str) -> str:
    return "" if not value else ("•" * 8 + value[-4:] if len(value) > 8 else "•" * 8)


class Settings:
    """Thread-safe settings store. Unknown keys in the file are dropped on load."""

    def __init__(self, path: Path | None = None):
        self.path = path or home() / "settings.json"
        self._lock = threading.RLock()
        self._data = copy.deepcopy(DEFAULTS)
        self._load()
        self.apply_market()

    def _load(self) -> None:
        try:
            stored = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(stored, dict):
            for key in DEFAULTS:
                if key in stored:
                    # A value this version cannot use keeps its default.
                    with contextlib.suppress(ValueError):
                        self._data[key] = _validated(key, stored[key], self._data[key])

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        with contextlib.suppress(OSError):
            os.chmod(tmp, 0o600)     # keys live here; no effect on Windows
        os.replace(tmp, self.path)

    def get(self, key: str):
        with self._lock:
            return copy.deepcopy(self._data[key])

    def all(self) -> dict:
        with self._lock:
            return copy.deepcopy(self._data)

    def public(self) -> dict:
        """Settings as the UI may see them: keys masked."""
        data = self.all()
        data["api_keys"] = {name: mask(value) for name, value in data["api_keys"].items()}
        kc = data["kite_connect"]
        data["kite_connect"] = {"api_key": kc.get("api_key", ""), "api_secret": mask(kc.get("api_secret", ""))}
        from .brokers import secret_fields

        data["broker_keys"] = {broker: {key: mask(value) if key in secret_fields(broker) else value
                                        for key, value in fields.items()}
                               for broker, fields in data["broker_keys"].items()}
        return data

    def update(self, changes: dict) -> dict:
        """Apply ``changes``; raises ValueError on an unknown key or a wrong type."""
        with self._lock:
            staged = copy.deepcopy(self._data)
            for key, value in changes.items():
                if key not in DEFAULTS:
                    raise ValueError(f"unknown setting: {key}")
                staged[key] = _validated(key, value, staged[key])
            if staged["mcx_trade_start"] >= staged["mcx_trade_end"]:
                raise ValueError("The MCX trade window must start before it ends")
            self._data = staged
            self._save()
            self.export_keys()
            self.apply_market()
            return self.public()

    def apply_market(self) -> None:
        """Hand the holiday list to the market clock."""
        from . import market, mcx

        market.set_holidays(self.get("holidays"))
        mcx.set_holidays(self.get("mcx_holidays"))

    def export_keys(self) -> None:
        """Put the stored keys in the environment, where the LLM clients read them."""
        for name, value in self.get("api_keys").items():
            if value:
                os.environ[name] = value

    def agent_config(self) -> dict:
        """The TradingAgents config these settings describe."""
        from tradingagents.default_config import build_default_config

        data, config = self.all(), build_default_config()
        config["llm_provider"] = data["llm_provider"]
        for key in ("deep_think_llm", "quick_think_llm"):
            if data[key]:
                config[key] = data[key]
        config["backend_url"] = data["backend_url"] or None
        for key in ("max_debate_rounds", "max_risk_discuss_rounds", "output_language"):
            config[key] = data[key]
        config["default_exchange"] = data["default_exchange"]
        return config


def _validated(key: str, value, current):
    default = DEFAULTS[key]
    if key == "api_keys":
        if not isinstance(value, dict):
            raise ValueError("api_keys must be an object")
        merged = dict(current)
        for name, secret in value.items():
            if name not in _SECRET_ENV_NAMES:
                raise ValueError(f"unknown key name: {name}")
            if not isinstance(secret, str):
                raise ValueError(f"{name} must be a string")
            if secret.startswith("•"):      # the masked value sent back unchanged
                continue
            if secret:
                merged[name] = secret.strip()
            else:
                merged.pop(name, None)
                os.environ.pop(name, None)
        return merged
    if key == "broker":
        from .brokers import BROKERS

        if value not in BROKERS:
            raise ValueError(f"broker must be one of {', '.join(BROKERS)}")
        return value
    if key == "broker_keys":
        from .brokers import field_names

        if not isinstance(value, dict):
            raise ValueError("broker_keys must be an object")
        merged = copy.deepcopy(current)
        for broker, fields in value.items():
            allowed = field_names(broker)
            if not allowed:
                raise ValueError(f"unknown broker: {broker}")
            if not isinstance(fields, dict):
                raise ValueError(f"broker_keys.{broker} must be an object")
            entry = dict(merged.get(broker) or {})
            for name, text in fields.items():
                if name not in allowed:
                    raise ValueError(f"unknown field for {broker}: {name}")
                if not isinstance(text, str):
                    raise ValueError(f"{broker}.{name} must be text")
                if text.startswith("•"):      # the masked value sent back unchanged
                    continue
                if text.strip():
                    entry[name] = text.strip()
                else:
                    entry.pop(name, None)
            merged[broker] = entry
        return merged
    if key == "kite_connect":
        if not isinstance(value, dict) or set(value) - {"api_key", "api_secret"}:
            raise ValueError("kite_connect takes api_key and api_secret")
        merged = dict(current)
        for name, text in value.items():
            if not isinstance(text, str):
                raise ValueError(f"kite_connect.{name} must be text")
            if text.startswith("•"):          # the masked secret sent back unchanged
                continue
            text = text.strip()
            if text and not re.fullmatch(r"[A-Za-z0-9]{6,64}", text):
                raise ValueError(f"The Kite {name.replace('_', ' ')} has letters and digits only. Copy it again "
                                 "from developers.kite.trade.")
            merged[name] = text
        return merged
    if key == "mcp":
        if not isinstance(value, dict) or not isinstance(value.get("mcpServers"), dict):
            raise ValueError('mcp must be an object with an "mcpServers" object')
        for name, entry in value["mcpServers"].items():
            if not isinstance(entry, dict) or not (entry.get("command") or entry.get("url")):
                raise ValueError(f'MCP server "{name}" needs a "command" or a "url"')
        return value
    if key == "analysts":
        allowed = {"market", "social", "news", "fundamentals"}
        if not isinstance(value, list) or not value or not set(value) <= allowed:
            raise ValueError(f"analysts must be a non-empty list from {sorted(allowed)}")
        return [a for a in ("market", "social", "news", "fundamentals") if a in value]
    if key in ("mcx_trade_start", "mcx_trade_end"):
        if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value.strip()):
            raise ValueError(f"{key} must be a time as HH:MM")
        if not "09:00" <= value.strip() <= "23:55":
            raise ValueError(f"{key} must be within the MCX session, 09:00 to 23:55")
        return value.strip()
    if key == "mcx_holidays":
        from .mcx import parse_holidays
        return parse_holidays(value)
    if key == "mcx_exit_days":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or not 1 <= value <= 30:
            raise ValueError("mcx_exit_days must be a whole number of days from 1 to 30")
        return int(value)
    if key == "mcx_margin_pct":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 3 <= value <= 100:
            raise ValueError("mcx_margin_pct must be between 3 and 100")
        return float(value)
    if key in ("trade_start", "trade_end", "square_off"):
        if not isinstance(value, str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", value.strip()):
            raise ValueError(f"{key} must be a time as HH:MM")
        if not "09:15" <= value.strip() <= "15:30":
            raise ValueError(f"{key} must be within market hours, 09:15 to 15:30")
        return value.strip()
    if key == "trend_rule":
        from .trend import validate as validate_trend
        return validate_trend(value)
    if key == "holidays":
        from datetime import datetime as _dt
        if isinstance(value, str):
            value = re.split(r"[\s,;]+", value.strip())
        if not isinstance(value, list) or len(value) > 400:
            raise ValueError("holidays must be a list of dates")
        out = set()
        for item in value:
            if not item:
                continue
            try:
                out.add(_dt.strptime(str(item).strip(), "%Y-%m-%d").strftime("%Y-%m-%d"))
            except ValueError:
                raise ValueError(f"“{str(item)[:20]}” is not a date as YYYY-MM-DD") from None
        return sorted(out)
    if key == "chain_recorder":
        from .options import SPOT
        if isinstance(value, str):
            value = [v for v in re.split(r"[\s,;]+", value.strip().upper()) if v]
        if not isinstance(value, list) or len(value) > 4 or not all(isinstance(v, str) and v.upper() in SPOT for v in value):
            raise ValueError(f"chain_recorder must be up to 4 of {', '.join(SPOT)}")
        return list(dict.fromkeys(v.upper() for v in value))
    if key == "chain_recorder_minutes":
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value or not 1 <= value <= 60:
            raise ValueError("chain_recorder_minutes must be a whole number from 1 to 60")
        return int(value)
    if key == "ticker_indices":
        from .ticker import validate as validate_ticker
        return validate_ticker(value)
    if key == "telegram_chat_id":
        text = str(value).strip()
        if text and not re.fullmatch(r"-?\d{3,20}|@[A-Za-z0-9_]{4,40}", text):
            raise ValueError("The Telegram chat id is a number (for example 123456789) or a channel name starting with @")
        return text
    if key == "supervisor_mode" and value not in ("off", "watch", "protect"):
        raise ValueError("supervisor_mode must be off, watch or protect")
    if key in ("supervisor_minutes", "supervisor_max_actions"):
        low, high = (5, 120) if key == "supervisor_minutes" else (0, 20)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or int(value) != value \
                or not low <= value <= high:
            raise ValueError(f"{key} must be a whole number from {low} to {high}")
        return int(value)
    if key == "supervisor_loss_pct" and (isinstance(value, bool) or not isinstance(value, (int, float))
                                         or not 0 <= value <= 50):
        raise ValueError("supervisor_loss_pct must be between 0 and 50 (0 for off)")
    if key == "chart_source" and value not in ("kite", "kite_then_yahoo"):
        raise ValueError("chart_source must be kite or kite_then_yahoo")
    if key == "default_exchange" and value not in ("NSE", "BSE"):
        raise ValueError("default_exchange must be NSE or BSE")
    if key == "product" and value not in ("CNC", "MIS"):
        raise ValueError("product must be CNC or MIS")
    if isinstance(default, bool):
        if not isinstance(value, bool):
            raise ValueError(f"{key} must be true or false")
        return value
    if isinstance(default, (int, float)):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{key} must be a number")
        if value < 0 or value != value or value in (float("inf"),):
            raise ValueError(f"{key} must be a non-negative number")
        if key in ("max_debate_rounds", "max_risk_discuss_rounds") and not 1 <= value <= 5:
            raise ValueError(f"{key} must be between 1 and 5")
        if key in ("risk_per_trade_pct", "max_risk_pct") and value > 10:
            raise ValueError(f"{key} cannot exceed 10: one trade losing more than a tenth of capital is not risk control")
        if key == "risk_per_trade_pct" and value <= 0:
            raise ValueError("risk_per_trade_pct must be above 0")
        if key.endswith("_pct") and value > 100:
            raise ValueError(f"{key} cannot exceed 100")
        return int(value) if isinstance(default, int) else float(value)
    if isinstance(default, str):
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        return value.strip()
    return value
