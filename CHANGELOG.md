# Changelog

All notable changes to TradeSetu are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Account funds** on the P&L tab for the account selected at the top. Paper: starting capital, cash available, invested, account value and change since start. Live: your broker's available to trade, used margin, cash (opening balance, added today) and collateral, for Equity and F&O and for Commodity (MCX) (`GET /api/funds?mode=paper|live`).
- **Excel downloads** on the P&L tab. Profit/Loss (closed trades), Order journal, Fills and Tax lots now download as colour-coded .xlsx files: profit in green, loss in red, Buy/Sell in colour, failed orders in red, tax heads shaded, rupee and date formats, frozen header with filters and a totals row that follows the filter. Closed trades add a "By source" sheet (each algo and manual) and tax lots a "By financial year" sheet. Tick **Plain CSV instead** for the old CSV (`GET /api/export/{kind}.xlsx`).
- **size by risk** on the order ticket. Enter a stoploss (and a target) and **Work out quantity** sizes the buy so that the stoploss loses a set share of capital, charges included, capped by capital and the maximum order value, with the reward-to-risk and the win rate it needs (`POST /api/order/size`). New Settings **Risk per trade**: capital for risk, risk %, a refuse-above limit checked on every manual order with a stoploss, and an optional rule that a manual share buy needs a stoploss.
- **trade journal** on the P&L tab. One row per trade (partial exits counted once), with a note for the plan (setup, reason, stoploss, target) and the review (followed the plan, mistakes, lesson); a buy from the ticket saves its plan. Results in R when a stoploss is planned (`GET/POST /api/journal`, `PUT/DELETE /api/journal/{id}`).
- **Your results**: expectancy, win rate, average win and loss, profit factor, expectancy in R, losing streak and largest fall of your own closed trades, the 95% range of the average, a warning under 30 trades, grouped by setup, source, hour, weekday, holding time, side, plan followed or mistake, with the other account for comparison.
- **?** on every tile the desk can explain (P&L, results, backtest): what the number means, worked out with the number shown, and a link to the manual.
- **Can you trust this result?** under every backtest: enough trades, better than the risk-free rate, and 1,000 random orders of the same trades (drawdown to expect, range of final values, chance of a loss). The manual explains survivorship bias in index lists.
- Financial statements in every stock's details, read from the company's own quarterly results filings on NSE (XBRL): quarterly results (12 quarters), profit and loss (years and the last four quarters), balance sheet (March and September), cash flow, ratios (ROE, ROCE, debt to equity, margins, interest cover, debtor and inventory days; bank ratios and NPAs for banks), peers in the same industry, consolidated or standalone. Filings are read once, one a second, and kept on disk (`GET /api/financials`, `POST /api/financials/refresh`).
- Results Drift shows the quarter's sales and profit against a year before (NSE results comparison, or the filings), a Strong / Weak / Mixed verdict, whether the results back the price move, and a "Results back the move" filter.
- Broker choice in Settings → Broker: Upstox, Dhan, Angel One, Fyers and Groww beside Zerodha Kite. The new brokers are experimental: built on each broker's published API and official SDK and covered by offline tests, not yet run against a live account. Kite stays the default.

- Algo details: click an algo (stock or F&O, paper or live) for each stock or leg it traded: buy and sell price, realised and unrealised P&L, Active or Closed, orders still out and every fill.
- Edit an algo that is not running live. While it holds a position only the budget and exit rules can change; the name is fixed; a change of Paper/Live stops it.
- Profit/Loss (closed trades) CSV download.

### Changed

- Sector Compass is now four plain groups (Leading, Losing pace, Lagging, Catching up) decided by two numbers shown for every sector: its return against the Nifty 50 over 3 months and over 4 weeks. The chart is optional, uses the same numbers, and no longer overlaps its labels.
- The P&L tab leads with a plain summary (total, booked, still holding, today) and tables in plain words; statistics and the tax estimate moved under Advanced.
- New TradeSetu logo in the README, the desk sidebar, the sign-in page, the browser tab icon, the installed-app icons and the social preview.

### Fixed

- Results Drift showed nothing when BSE refused the server (403). It now retries BSE with cookies, falls back to NSE's event calendar, retries every 30 minutes after a failure, says why in plain words, and can send exchange requests through `DESK_EXCHANGE_PROXY`.
- Stock algos: a sell went ahead when cancelling the algo's target/stoploss GTT failed (the error was ignored), so the GTT could fire later and sell shares that were no longer the algo's. The sell now waits until the GTT is gone; if it has fired, nothing more is sold. A trailing or at-cost stoploss is not moved while the old GTT cannot be cancelled.
- Background jobs (paper GTT check, live fill sync, daily loss limit, F&O algos, paper expiry settlement, MCX square-off, scanner) failed silently. A failure is now logged and alerted, once an hour; an algo that hits an unexpected error says so.
- Daily loss limit: warns which positions it has no price for, instead of leaving them out without a word.
- Paper MCX delivery cut-off: closes the part of a contract held by hand and leaves an algo's lots to the algo (it skipped the whole contract). F&O algo positions are counted per account (paper or live).
- A blocked stock algo's alert no longer repeats its reason.
- F&O algos: an algo could not close its own leg when another algo held the same contract the other way (a condor's sold call that is an iron butterfly's bought wing): the account nets to 0 and the closing buy was refused with "You are short 0". A closing order from an F&O algo is now checked against that algo's own share of the position.
- F&O algos: a block for want of prices said "Log in to Kite" whatever went wrong. It now names what the broker answered (a login needed, too many requests, a timeout), and the blocked alert no longer repeats its reason.
- Option chain: says every listed expiry has passed (refresh the contract list) instead of failing with a server error.
- CI installs the desk extras, so the ~220 desk API tests run instead of skipping. The F&O API tests read contracts on the fake Kite's anchor day, so they no longer fail once the real date passes it.
- A "No edge" badge in Results Drift took the sidebar's style and stretched down the page.
- CSV downloads (order journal, fills, tax lots) failed with "missing or wrong request token": plain links cannot send the request token the API needs. They now download through the API with the token. Text cells a spreadsheet would run as a formula are written as text.

## [0.1.0] - 2026-11-18

First public release.

### Added
- Trading desk for Indian markets: sector cards, Nifty indices, stock details, charts from 1-minute to daily candles with pivots and indicator panels.
- F&O: option chain, expiries, index futures, 30+ option strategies with payoff charts and backtests.
- MCX gold and silver: futures, options and algos with MCX session, holiday and delivery cut-off rules.
- Strategy builder, built-in and legend strategies, daily and intraday backtests with Zerodha charges, exit rules (stoploss, target, trailing, re-entry) and a walk-forward optimiser.
- Breakout scanner with GTT orders; paper and live algos with risk limits, a daily loss limit and a kill switch.
- P&L, Telegram alerts, tax export, two-factor sign-in and passkeys, installable phone app, Docker/Dokploy hosting.
- Optional AI research agents, derived from TradingAgents (Apache-2.0, see NOTICE).
