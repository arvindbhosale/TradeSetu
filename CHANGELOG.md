# Changelog

All notable changes to TradeSetu are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Financial statements in every stock's details, read from the company's own quarterly results filings on NSE (XBRL): quarterly results (12 quarters), profit and loss (years and the last four quarters), balance sheet (March and September), cash flow, ratios (ROE, ROCE, debt to equity, margins, interest cover, debtor and inventory days; bank ratios and NPAs for banks), peers in the same industry, consolidated or standalone. Filings are read once, one a second, and kept on disk (`GET /api/financials`, `POST /api/financials/refresh`).
- Results Drift shows the quarter's sales and profit against a year before (NSE results comparison, or the filings), a Strong / Weak / Mixed verdict, whether the results back the price move, and a "Results back the move" filter.
- Broker choice in Settings → Broker: Upstox, Dhan, Angel One, Fyers and Groww beside Zerodha Kite. The new brokers are experimental: built on each broker's published API and official SDK and covered by offline tests, not yet run against a live account. Kite stays the default.

- Algo details: click an algo (stock or F&O, paper or live) for each stock or leg it traded: buy and sell price, realised and unrealised P&L, Active or Closed, orders still out and every fill.
- Edit an algo that is not running live. While it holds a position only the budget and exit rules can change; the name is fixed; a change of Paper/Live stops it.
- Profit/Loss (closed trades) CSV download.

### Changed
- The P&L tab leads with a plain summary (total, booked, still holding, today) and tables in plain words; statistics and the tax estimate moved under Advanced.
- New TradeSetu logo in the README, the desk sidebar, the sign-in page, the browser tab icon, the installed-app icons and the social preview.

### Fixed
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
