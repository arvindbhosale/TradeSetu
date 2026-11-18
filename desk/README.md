# TradeSetu

Desktop trading desk for NSE and BSE, built on TradingAgents.

- **Analyze**: the multi-agent pipeline (analysts, bull/bear researchers, trader, risk team, portfolio manager) on one stock, returning a rating and the full reports.
- **Backtest**: rule strategies (SMA/EMA crossover, Supertrend, RSI, MACD, Donchian, Bollinger, buy and hold) on adjusted daily bars with Zerodha delivery charges; and the agents over a grid of symbols and dates, scored against the Nifty.
- **Trade**: paper account, and live orders on Zerodha through a Kite MCP server.

**User manual with screenshots: [static/manual/USER_MANUAL.md](static/manual/USER_MANUAL.md), also inside the app under Help.**

## Two ways to run it

- **On a server (VPS, Dokploy, Docker)**: a website behind a sign-in. See [DEPLOY.md](../DEPLOY.md).
- **On your own computer**: a local window, no sign-in, reachable only from that machine. Below.

Also in the app: **Charts** (Yahoo daily candles with SMA/EMA, Bollinger Bands, Supertrend, Donchian, monthly pivot points with next-session daily levels, and RSI, MACD and volume panels; switch the tab off in Settings), **Algos** (a strategy trading one stock by itself, paper or live), and a light/dark theme button.

## Algos

An algo runs one backtest strategy on one stock. Each trading day after 15:50 IST it reads the day's closing bar; if the strategy's position changed it sends one order: paper fills at the last price, live goes to Kite as an AMO limit order for the next open.

- Created stopped. A live algo starts only after you type `ARM LIVE <NAME>`, and only while "Allow live orders" is on.
- Every algo order passes the same risk limits and kill switch as a manual one.
- A live order counts as a position only after Kite reports it filled. Unfilled or rejected orders leave the algo as it was.
- A new algo waits for a fresh entry signal instead of joining a trend already running.
- Kite signs you out every morning. If you have not signed in that day, a live algo skips the day and logs it.
- Daily bars only. Not an intraday or high-frequency engine.
- Deleting an algo never sells its shares.

## Run on your own computer

```bash
pip install -e ".[desk,window]"
```

```bash
tradesetu
```

`--browser` opens it in the web browser instead of a window; `--no-window` only serves. Without `pywebview` the desk opens in the browser.

In **Settings**: choose the LLM provider and paste its API key. Nothing runs the agents without one.

## Symbols

| You type | Resolves to |
|---|---|
| `RELIANCE`, `reliance`, `NSE:RELIANCE`, `RELIANCE-EQ` | `RELIANCE.NS` |
| `BSE:RELIANCE`, `RELIANCE.BO` | BSE listing |
| `NIFTY`, `BANKNIFTY`, `SENSEX`, `INDIAVIX` | the index |

A bare symbol is NSE by default (Settings can make it BSE). US tickers are not reachable while an Indian default is set.

BSE caveat: Yahoo carries little or no price history for many `.BO` listings and none by scrip code (`500325`). Strategy backtests on a BSE symbol fall back to the NSE listing's prices and say so. For AI analysis use the NSE symbol; BSE orders through Kite work as normal.

## Kite (Zerodha) connection

The desk ships with Zerodha's hosted server:

```json
{ "mcpServers": { "kite": { "command": "npx", "args": ["mcp-remote", "https://mcp.kite.trade/mcp"] } } }
```

This needs Node.js: the hosted server uses OAuth, and `mcp-remote` handles the sign-in by opening Zerodha's login in your browser on first connect. Press **Connect** and complete the sign-in; if the account panel then asks for it, press **Log in to Kite**, open the link, and **Refresh**. Kite sessions expire every morning.

Without Node.js the desk connects to the URL directly, which works for a self-hosted server but not for the hosted one (it has no OAuth client of its own).

**The hosted server is read-only.** Zerodha does not register `place_order`, `modify_order` or `cancel_order` on `mcp.kite.trade`. With it you get holdings, positions, funds and live prices, and the desk shows "Kite: read-only".

To place live orders, run [`kite-mcp-server`](https://github.com/zerodha/kite-mcp-server) yourself with a Kite Connect API key and secret, and point the MCP setting at it:

```json
{ "mcpServers": { "kite": { "url": "http://localhost:8080/mcp" } } }
```

The desk reads the tool list from whichever server is connected and enables live trading only when `place_order` is there.

## Other brokers (experimental)

`desk/brokers/` puts Upstox, Dhan, Angel One, Fyers and Groww behind the same interface the desk uses for Kite. Each adapter (`upstox.py`, `dhan.py`, ...) calls the broker's REST API and answers in Kite's shape; `base.py` holds what they share. `brokers.Switch` is the one broker object the desk holds, forwarding to the broker chosen in Settings.

Instrument names stay Kite's everywhere. `instruments.py` reads each broker's daily instrument file into fields (exchange, underlying, expiry, strike, option type) and builds the Kite symbol from them: `NIFTY25NOVFUT`, `NIFTY25NOV24000CE` for the month's last expiry, `NIFTY25N1824000PE` for a weekly (year, month code 1-9/O/N/D, day). Index names are matched through an alias table (`Nifty Fin Service`, `FINNIFTY`, `FINNIFTY-INDEX` all become `NIFTY FIN SERVICE`). Each row keeps the broker's own key for API calls. Instrument files are cached in `~/.tradingagents/desk/instruments/` for the day; session tokens in `broker-sessions.json` (mode 600).

Upstox and Fyers sign in by OAuth: the broker redirects to `/broker/callback` on the desk, which accepts only the one-time `state` the desk issued. Angel One and Groww (with an API key) sign in with a TOTP made from the secret in Settings. Dhan uses a pasted access token.

Not covered yet: GTT orders (Kite only), live websocket ticks (the desk polls quotes for every broker), and testing against live accounts.

## Live order safeguards

- Live is off until **Allow live orders** is ticked in Settings.
- Every order is previewed with its checks, value and charges, then needs the exact phrase typed (`BUY 10 RELIANCE`). The confirmation works once and expires in 2 minutes.
- Limits per order value, orders per day, and position size; a kill switch that blocks everything.
- Delivery (CNC) sells are checked against holdings; no short delivery.
- Outside 09:15-15:30 IST only AMO orders are accepted. Exchange holidays are not known to the desk; the broker rejects those.
- An AI rating only pre-fills the ticket. Nothing is sent without your confirmation.

SEBI's retail algo-trading rules apply to orders sent through a broker API (for example a static IP registered with the broker). Check Zerodha's current requirements before trading live.

## What the numbers leave out

- Strategy backtests ignore liquidity, circuit-limit days, tax on gains and delisted stocks. Real results would be lower.
- Agent backtests are slow and cost LLM credits (one full analysis per cell), are not repeatable run to run, and the model may know what happened after a past date.
- Yahoo prices for NSE/BSE are delayed. Live prices come from Kite once connected.
- Charges follow Zerodha's published equity schedule in `desk/charges.py`. Rates change; update them there.
- Paper orders fill at once at the last price. Real orders may fill partly or not at all.

Not investment advice. You are responsible for every order you confirm.

## Files

Settings (including API keys, file mode 600), the paper account, the order journal and the price cache live in `~/.tradingagents/desk/`.

## Development

`python -m desk.dev.fake_kite_mcp` is a stand-in Kite MCP server (add `--read-only` to mimic the hosted one). Tests: `pytest tests/test_desk.py tests/test_india.py`.
