# TradeSetu User Manual

A trading desk for NSE and BSE: charts, strategy building, backtesting, paper trading, algo trading and live orders on Zerodha.

The screenshots use sample data, not real prices.

## Contents

1. [Read this first](#1-read-this-first)
2. [Sign in and find your way around](#2-sign-in-and-find-your-way-around)
   - [Install it as an app](#install-it-as-an-app)
   - [Markets: sector cards](#markets-sector-cards)
   - [How Uptrend and Downtrend are decided (and changing it)](#how-uptrend-and-downtrend-are-decided)
   - [Stock details: fundamentals and technicals](#stock-details-fundamentals-and-technicals)
   - [Market Pulse](#market-pulse)
   - [Indices](#indices)
   - [Market tools](#market-tools)
   - [F&O expiries](#fo-expiries)
   - [Option chain: track, paper and live trade](#option-chain-track-paper-and-live-trade)
   - [Index futures: prices and charts](#index-futures-prices-and-charts)
   - [Gold & silver (MCX)](#gold--silver-mcx)
   - [P&L and alerts](#pl-and-alerts)
   - [Price alerts](#price-alerts)
   - [Learn from your trades](#learn-from-your-trades)
   - [Export and tax report](#export-and-tax-report)
3. [Settings](#3-settings)
   - [Risk per trade](#risk-per-trade)
4. [Charts](#4-charts)
5. [Strategies: combine indicators](#5-strategies-combine-indicators)
   - [Strategies of market legends](#strategies-of-market-legends)
6. [Backtest](#6-backtest)
   - [Exit rules: stoploss, target, trailing, re-entry](#exit-rules-stoploss-target-trailing-re-entry)
   - [Create an algo from the result](#create-an-algo-from-the-result)
   - [Positional (swing) backtests](#positional-swing-backtests)
   - [Optimise a strategy (walk-forward)](#optimise-a-strategy-walk-forward)
   - [Pair trading: two stocks that move together](#pair-trading-two-stocks-that-move-together)
   - [Test a stock-picking signal across an index](#test-a-stock-picking-signal-across-an-index)
   - [Index futures backtest](#index-futures-backtest)
   - [Option strategies: straddles, condors, spreads, hedges](#option-strategies-straddles-condors-spreads-hedges)
   - [Portfolios: Ray Dalio's All Weather](#portfolios-ray-dalios-all-weather)
7. [Scanner: breakouts with GTT orders](#7-scanner-breakouts-with-gtt-orders)
   - [Legend screens: Buffett, Graham, Lynch, Greenblatt, Kedia](#legend-screens-buffett-graham-lynch-greenblatt-kedia)
8. [Algos: automatic trading](#8-algos-automatic-trading)
9. [Trade: orders, GTT, Kite](#9-trade-orders-gtt-kite)
10. [Analyze: the AI agents](#10-analyze-the-ai-agents)
11. [Your daily routine](#11-your-daily-routine)
12. [Going live: checklist](#12-going-live-checklist)
13. [When something goes wrong](#13-when-something-goes-wrong)
14. [Words used in this manual](#14-words-used-in-this-manual)

---

## 1. Read this first

- **Paper first.** Everything works in Paper mode with pretend money. Use it until a strategy has behaved as you expect for several weeks.
- **A backtest is not a promise.** It shows how rules would have done on past prices, with costs, under ideal fills. Real trading does worse.
- **Nothing here is investment advice.** You are responsible for every order the desk sends for you.
- **Live trading is off until you switch it on**, and each live action asks you to type a confirmation.

What the desk cannot do:

| Limit | Why |
|---|---|
| Run for days without you | Zerodha signs every Kite session out each morning. You sign in again once a day. |
| React in milliseconds | The desk checks prices and candles about every 20 seconds. |
| Give exchange-grade live data for free | Charts and live prices come from Kite, which needs the paid Kite Connect plan (₹500 a month) for market data; the free Personal API has none. Without Kite, backtests use Yahoo's delayed data; the scanner and sector history always do. |
| Short stocks | It is long-only: it buys, then sells what it bought. |
| Trade futures, or write (sell short) options | Index options can be **bought** and later sold (paper and live). Futures orders and option writing are not offered; both can be **backtested** (index futures, straddles, condors, spreads). |
| Give fundamentals from Kite | Kite's API has no fundamentals (P/E, market cap, ROE…). Those come from Yahoo Finance, refreshed every 3 days, and the financial statements from the companies' own filings on NSE. Prices, quotes and charts come from Kite. |

---

## 2. Sign in and find your way around

Open your desk's address and sign in with the user name and password set when it was installed.

![Sign-in page](img/01-sign-in.png)

Five wrong passwords lock that device out for 15 minutes.

If two-factor sign-in is on (see Settings), the page then asks for the 6-digit code from your authenticator app:

![Sign-in asking for the code](img/23-sign-in-code.png)

The menu on the left opens the nine sections of the desk. At its foot are **User manual** (this document), the **Dark / Light theme** switch (remembered on this device) and **Sign out**.

The bar at the top is always visible:

![Top bar](img/03-header.png)

| Item | Meaning |
|---|---|
| **Market open / closed** | NSE cash market hours, 09:15 to 15:30 IST, Monday to Friday. Exchange holidays are not known to the desk. |
| **Buying open until 15:00** | Your trade window. New buys are accepted only inside it (see Settings). |
| **Kite: …** | *not connected*, *read-only* (prices and portfolio only) or *trading* (can place orders). |
| **Paper / Live** | Which account orders go to. Live turns red. |

### Install it as an app

TradeSetu can be installed on a phone, tablet or computer. It then opens from the home screen or desktop in its own window, without the browser's address bar.

| Device | How |
|---|---|
| **Android (Chrome)** | Open the desk, then **Settings → Install as an app**, or the browser menu → *Install app*. |
| **iPhone / iPad (Safari)** | Share button → **Add to Home Screen**. |
| **Windows, Mac, Linux (Chrome or Edge)** | **Install app** at the foot of the left menu, or the install icon in the address bar. |

On a phone the menu moves to a bar along the bottom; swipe it sideways to reach every section. The theme switch, manual and sign-out are at the top of **Settings**.

![The desk on a phone](img/18-phone.png)

The app always needs a connection. Prices, your account and orders are read live every time and never from a saved copy, so offline it shows a short notice instead of old numbers. Algos and orders run on the server and carry on whether or not the app is open.

### Markets: sector cards

The first screen. Each card is one sector of the market.

![Sector cards with daily candles](img/24-market-candles.png)

**On each card**

| Part | Meaning |
|---|---|
| **Name and trend badge** | ▲ Uptrend, ▼ Downtrend or ◆ Sideways. |
| **Big percentage** | The sector's move today. |
| **Index** | The sector's index value. It starts at 100 one year ago. |
| **Candles** | The last 45 daily candles of the sector index (green: closed above its open; red: below). |
| **1 week / 1 month / 3 months** | Moves over those periods. |
| **Bottom line** | How many of the sector's stocks have prices, and what share of them trade above their own 50-day average. |

**How the index is made.** Every stock in a sector has a weight (equal at first). Each day the index moves by the weighted average of its stocks' moves. A stock listed during the year joins from its first day.

**How the trend is decided:** by the trend rule, which you set. See the next section.

**The four tiles at the top** count sectors in uptrend, sectors up today, and name today's strongest and weakest.

**Filter and sort.** Show only Uptrend, Downtrend or Sideways sectors; sort by today, 1 week, 1 month, 3 months, 1 year or name.

**Auto-update.** The board reloads every 30 seconds. During market hours the desk fetches fresh prices about every 3 minutes: from Kite when it is connected (live), otherwise delayed prices. The line under the title says which and when.

#### Open a sector

Click a card.

![Sector detail](img/00-sector-detail.png)

- The chart shows the sector index as daily candles for the year, starting at 100. Each day's open, high, low and close are the weighted average of its stocks' own open, high, low and close (each measured from the previous close), so the candle shows the sector's real range that day.
- The table lists every stock: price, moves, 52-week range, **Mkt cap**, **P/E**, **ROE %**, its own trend, and **Contribution**: how many points it adds to or takes from the sector's move today. The biggest contributors are at the top.
- Click a stock's name to open its details (next sections).

#### Change the weights

1. Type a number in a stock's **Weight** box. Any numbers work; they are scaled to 100% (shown under **Share**).
2. Press **Save weights**. The index and chart are rebuilt with the new weights.
3. **Equal weights** sets them all back to the same.

A weight of 0 keeps the stock listed but out of the index.

#### Add, remove, rename, create

- **Add stocks**: type NSE symbols separated by spaces and press **Add**.
- **Remove** on a row takes that stock out.
- **Rename** and **Delete sector** are below the table.
- **+ New sector** on the board creates your own (for example "Drones"). The board holds up to 50.

The first load after installing or restarting downloads a year of prices for about 560 stocks and takes several minutes; cards fill in as it goes.

The sectors are a starting set of liquid NSE stocks grouped by me, not the exchange's official classification, and they do not cover every listed company. Edit them to match your view.

### How Uptrend and Downtrend are decided

One rule decides the trend badge everywhere: sector cards, index cards, each stock in a table and the stock details. It works on daily candles. **You choose it**: press **How a trend is decided** above the cards on Markets or Indices.

![Trend rule editor](img/25-trend-rule.png)

| Method | Uptrend when | Downtrend when | Settings |
|---|---|---|---|
| **Two moving averages** (default) | last close above the slow average **and** fast average above slow | both below | fast (20), slow (50) days |
| **Price against one moving average** | close more than *band %* above the average | more than *band %* below | period (50), band (1%) |
| **Supertrend direction** | Supertrend is up | Supertrend is down (never Sideways) | period (10), multiplier (3) |
| **Return over a period** | up more than *threshold %* over the look-back | down more than *threshold %* | look-back (20 days), threshold (3%) |

Anything that is neither is **Sideways**. Too little history shows **Not enough data**. Press **Apply** and every board recalculates at once; the rule is saved with your settings. Changing the rule changes only the badges and the Uptrend/Downtrend filter, not prices or the index.

### Stock details: fundamentals and technicals

Click any stock name (sector table, index table) to open its details.

![Stock details](img/26-stock-details.png)

- **Quote** (from Kite when logged in, otherwise delayed): last price, change, open, high, low, previous close, volume, average price, circuit limits, buy/sell quantity.
- **Technicals**, worked out on daily candles: trend (your rule), 20/50/200-day averages and the price's distance from them, RSI 14, MACD, ADX with +DI/−DI, ATR %, Supertrend, 52-week high and low, and returns over 1 week, 1 month, 3 months, 6 months and 1 year.
- **Fundamentals** (Yahoo Finance; Kite has none): market cap, P/E, forward P/E, P/B, EPS, book value, ROE, ROA, debt to equity, margins, revenue and earnings growth, dividend yield, beta, promoter/institution holdings, sector and industry. They update in the background; a stock seen for the first time can show "loading" for a minute.
- **Financial statements** (the company's own results filings on NSE): tabs for **Quarterly results** (last 12 quarters), **Profit & loss** (years to March, plus the last four quarters added up), **Balance sheet** (every March, and September when it is newer), **Cash flow** (years, and the latest six months), **Ratios** and **Peers**. Above them: market cap, P/E on the last four quarters' profit, price to book, return on equity, return on capital employed (banks: return on assets and NPAs), debt to equity, and the latest quarter's sales and profit against the same quarter a year before. Money is in ₹ crore.
  - **Consolidated (group)** or **Standalone (company only)**: consolidated is shown when the company files it.
  - Where it comes from: every listed company files each quarter's results with NSE in a machine-readable form (XBRL). The desk reads those files: the quarter's profit and loss every quarter, the balance sheet and cash flow every six months (September and March), the full year in the March filing. A company opened for the first time takes about half a minute (one file a second); after that it is on your computer and only new filings are read. **Refresh** reads NSE's list of filings again.
  - Ratios are worked out from the filings: return on equity on the average of the opening and closing shareholders' equity, return on capital employed as profit before interest and tax on equity plus borrowings, debtor and inventory days on sales. Banks get bank ratios (return on assets, cost to income, advances to deposits, gross and net NPA).
  - **Peers**: companies in the same industry in NSE's index lists, biggest first, with Yahoo Finance's trailing figures so every company is measured the same way.
  - Limits: only NSE-listed companies; history goes back as far as NSE has the filings in this form (the desk reads from 2015 at most, ten years); insurance companies file another format and show few lines. NSE answers from your own internet connection; it blocks many cloud servers, so on a hosted desk the statements may not load and the reason is shown.
- **Chart** opens the stock in Charts (Kite candles, any interval); **Trade** opens the order form with the symbol filled in.

### Indices

A board of NSE's indices, laid out like the sector cards: broad market (Nifty 50, Next 50, 100, 200, 500, Midcap, Smallcap…), sectoral (Bank, IT, Auto, Pharma, FMCG, Metal, Realty, PSU Bank…) and thematic (CPSE, Energy, Infra, Consumption, MNC…), 52 in all. The **BSE** group holds every index Kite lists on BSE (Sensex, Sensex 50, Bankex, BSE 100, 200, 500, Midcap, Smallcap, the sectoral indices…), read from Kite's instrument list once a day after you log in. Before that only Sensex, Sensex 50 and Bankex are shown.

![Indices board](img/27-nifty-indices.png)

- **Levels** are Kite's official index values when Kite is logged in (live during market hours). Before you log in, they come from Yahoo where it has the index, or are rebuilt from the index's stocks (equal weight) for indices of up to 100 stocks. Each card names its source. The board switches to Kite within seconds of your login.
- Each card shows the level, today's move, 1 week / 1 month / 3 months / 1 year, the trend (your rule) and 45 daily candles.
- **Constituent lists** come from NSE's published files and are re-read once a day, so additions and removals appear on their own.
- BSE publishes no constituent file. The desk reads BSE's daily market-movers data, which names every index each traded stock belongs to, and builds each BSE index's stock list from it once a day. A stock that closed exactly unchanged is missing from that day's data, so a member is kept until it has been absent for 10 days.

Click a card to open the index:

![Index detail with constituents](img/28-index-detail.png)

- A Kite candle chart of the index: **5m, 15m, 1h** or **1D** (intraday needs Kite logged in).
- **Constituents**: every stock in the index with company, industry, price, today's move, day range, volume, market cap, P/E, ROE, **weight** and **contribution** (points it adds to or takes from the index today). Click a stock for its details.
- **Weights are approximate.** NSE weights by free-float market cap and does not publish the daily weights; the desk uses full market cap once it has every stock's, and equal weights until then (the line above the table says which). Contributions are therefore close, not exact.

### Market Pulse

At the top of **Markets**: one reading from **0 (extreme fear) to 100 (extreme greed)** on a speedometer, with what it read a week and a month ago. It is the plain average of six readings, each 0 to 100:

| Reading | 0 means | 100 means |
|---|---|---|
| Nifty against its 200-day average | 10% or more below | 10% or more above |
| Nifty's one-month return | the weakest month of the past year | the strongest |
| India VIX | the highest VIX of the past year | the lowest |
| Stocks above their 50-day average | none | all |
| Near 52-week highs against near lows (within 2%) | all near lows | all near highs |
| Stocks up today | none | all |

Bands: below 20 extreme fear, 20 to 40 fear, 40 to 60 neutral, 60 to 80 greed, above 80 extreme greed. The stock readings use every stock the desk prices (your sectors and the main Nifty indices' stocks). A reading of extreme fear is not a buy signal on its own; it says the crowd is scared.

The **heat map** below it colours each Nifty sector index (or each Nifty 50 stock, bigger tiles for bigger companies) by its move today, this week or this month. Click a stock tile for its details.

### Market tools

A tab of its own with four tools.

- **Whale Deals**: NSE's **bulk deals** (one client trading more than 0.5% of a company's shares in a day) and **block deals** (trades of ₹10 crore or more in the block window), with the client's name, side, shares, price and value. NSE publishes them after the close; the desk reads the file every two hours and keeps 30 days. Filter by days, minimum value or name. Below: **volume spikes**, stocks trading at least 3 times their 20-day average volume today.
- **Results Drift** (post-earnings drift): stocks that announced results in the last 60 days (BSE's results calendar, or NSE's event calendar when BSE refuses), the price **reaction** (close before the board meeting to the close of the next session), the volume on those days, and the move **since**. **Drifting up**: reacted up by at least your threshold (4% by default), still above the low of the reaction days, and higher since. **Holding up**: above that low but not higher since. The same downwards. Only stocks the desk prices are listed (the Nifty 500 and your sectors).
  - **Sales vs year ago** and **Profit vs year ago**: the quarter just announced against the same quarter last year, from the company's NSE filing. They fill in over a few minutes after the results come out (stocks with a signal first).
  - **Results**: **Strong** when profit grew 15% or more with sales not falling, or a loss turned to a profit; **Weak** when profit fell 10% or more, or sales and profit both fell, or a profit turned to a loss; **Mixed** otherwise.
  - **Results back the move**: Yes when a stock going up had Strong results or one going down had Weak results; No when they point opposite ways. Show **Results back the move** to list only those. Studies of post-earnings drift find it stronger when the results themselves surprise, so a move the numbers back is the better candidate.
- **Sector Compass**: each Nifty sector index against the Nifty 50 in two plain numbers: its return minus the Nifty's over the last **3 months** (ahead or behind) and over the last **4 weeks** (gaining or slipping). They put it in one of four groups: **Leading** (ahead and gaining), **Losing pace** (ahead but slipping), **Lagging** (behind and slipping) and **Catching up** (behind but gaining). Sectors tend to move round in that order: Catching up, Leading, Losing pace, Lagging. Each sector shows both returns next to the Nifty's and the group it was in 5 weeks ago. **Daily** uses 1 month and 1 week. **Show as a chart** draws the same groups on one chart, with each sector's path if you tick it. It describes the recent past, not the future.
- **Stock Radar**: every stock the desk tracks in one sortable table: returns, distance from the 52-week high, strength against the Nifty over 3 months, above the 50 and 200-day averages, volume against average, trend, P/E, ROE and signals (deals, Results Drift, volume spike). The **score** (0 to 100) weights strength against the Nifty 30%, nearness to the 52-week high 20%, the two averages 20%, one-month return 15% and volume 15%; strength, return and volume are ranked within the list, so it compares stocks with each other.

NSE's and BSE's files are read from the server the desk runs on. If either site refuses it, the tool says so and keeps the last data it read.

**Exchange data on a server.** NSE and BSE refuse many cloud and data-centre addresses (the tool shows "403 Forbidden"). On your own computer this does not happen. For a desk hosted on a server, set the environment variable `DESK_EXCHANGE_PROXY` to a proxy on an Indian home or office connection, for example `http://user:password@203.0.113.10:3128`. Only NSE and BSE requests go through it; your broker does not. After a failed read the results calendar is tried again every 30 minutes.

### F&O expiries

A reference list of everything that trades in futures and options: the indices and about 200 stocks, each with its **lot size** and **expiry dates**.

![F&O expiries on a phone](img/19-fno-phone.png)

- The highlighted date is the next expiry, with the days left. **W** marks a weekly expiry, **M** a monthly one.
- Type in **Find a stock** to filter the stock list.
- The list comes from Kite's instrument file, which the exchange updates every morning. When a contract expires it drops off and the next one moves up by itself; the desk rebuilds the list each day. **Refresh from Kite** does it at once.
- Connect Kite once (Trade section) for the list to load. A Kite login is not needed for it.

The desk does not trade futures. Index **options** are traded from the option chain above this list.

### Option chain: track, paper and live trade

At the top of **F&O expiries**. Pick the index (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, SENSEX, BANKEX, SENSEX50), the expiry and how many strikes either side of the money, then **Load chain**. Needs Kite logged in. With **Live** ticked it refreshes from Kite every 5 seconds in market hours.

![Option chain, contract chart, order and positions](img/29-option-chain.png)

| Item | Meaning |
|---|---|
| **Tiles** | Spot, days to expiry, **PCR** (put OI ÷ call OI), **max pain** (the strike where option buyers as a whole collect least at expiry), **ATM straddle** price and the move it implies, ATM implied volatility, lot size. |
| **Calls (left) / Puts (right)** | OI, OI change, volume, IV %, delta, change and last price for each strike. In-the-money cells are shaded; the at-the-money row is outlined. |
| **IV and delta** | Worked out by the desk from each option's last price (Black-Scholes), not supplied by Kite. |
| **OI change** | Since the desk first read the chain today, not since yesterday's close. |

**Trade a contract.** Click a price (LTP). Its Kite candle chart opens with an order form below:

1. Choose **Side**, **Lots** (the quantity is lots × lot size), **Limit price** and **Product** (NRML to carry over, MIS for intraday).
2. **Review order** shows the premium, last price and estimated charges (₹20 brokerage, STT 0.15% on sell premium, exchange fees, stamp duty, GST).
3. Type the confirmation and place it. Paper or Live follows the switch at the top; live needs live trading on in Settings.

Rules the desk enforces: LIMIT orders only (no market orders on options), whole lots, at most the exchange freeze quantity per order, no orders on expired contracts, and **no writing**: you can sell only what you hold.

**Option positions** lists what you hold on the selected account with average, last price and P&L; **Exit** fills a sell order for the whole position. Paper options still held after expiry are settled at their intrinsic value against the index close on the expiry day (worthless if out of the money). Live positions are settled by the exchange as usual.

### Index futures: prices and charts

On **F&O expiries**, under the option chain. Every index future that is trading (NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, SENSEX, BANKEX; current, next and far month) with Kite's live price.

![Index futures](img/36-index-futures.png)

| Column | Meaning |
|---|---|
| **LTP / Today** | Last price and change from yesterday's close. |
| **Index** | The index level now. |
| **Basis** | Future − index. Positive is normal (the cost of carrying the position to expiry); a negative basis is a sign of heavy short selling. |
| **Basis % p.a.** | The basis as a yearly rate. Near expiry it swings a lot because few days are left. |
| **OI / Volume** | Open interest and today's volume. |

With **Live** ticked it refreshes every 10 seconds in market hours. **Click a contract** for its Kite candle chart:

![A futures contract chart](img/37-futures-chart.png)

- **1m, 5m, 15m, 1h**: the contract's own candles. A contract lives about three months, so intraday history stops at its listing.
- **1D**: Kite's **continuous** series, the front-month contract day by day, stitched across expiries for several years. It jumps a little at each monthly roll (the gap between the expiring and the next contract).

The desk charts futures; it does not place futures orders.

### Gold & silver (MCX)

The **Gold & silver** tab trades MCX gold and silver futures and options through the same Kite account, paper or live. It is the legal way for a resident Indian to trade gold the way XAU/USD traders do: the MCX price is the international price times the rupee rate, plus import duty, and settles in rupees. (Offshore XAU/USD, GBP/USD and other forex CFDs are not legal for residents under FEMA, and the desk does not connect to them.)

**Before you start:** activate the **commodity segment** in Zerodha Console, then log in to Kite and press **Reload contracts from Kite**.

**The contracts.** All are compulsorily delivered (see *Physical delivery* below).

| Contract | One lot | Quoted | ₹1 on the price is | Options |
|---|---|---|---|---|
| GOLD | 1 kg | ₹ per 10 g | ₹100 a lot | yes |
| GOLDM (mini) | 100 g | ₹ per 10 g | ₹10 a lot | yes |
| GOLDTEN | 10 g | ₹ per 10 g | ₹1 a lot | where MCX lists them |
| GOLDGUINEA | 8 g | ₹ per 8 g | ₹1 a lot | no |
| GOLDPETAL | 1 g | ₹ per 1 g | ₹1 a lot | no |
| SILVER | 30 kg | ₹ per kg | ₹30 a lot | yes |
| SILVERM (mini) | 5 kg | ₹ per kg | ₹5 a lot | yes |
| SILVERMIC (micro) | 1 kg | ₹ per kg | ₹1 a lot | where MCX lists them |

**Quantity is in lots.** Kite takes MCX quantities as a number of lots, not grams. "2" on a GOLDM order is 200 g. The order review shows the contract value in rupees (price × lots × the multiplier above), the margin estimate and the charges. A futures order is held to your **Max order value** by its margin (the **MCX futures margin %** in Settings, 15% by default; Zerodha's real figure includes any extra margin MCX levies and decides at order time). One GOLDM lot at ₹1,30,000 per 10 g is worth ₹13 lakh and needs about ₹2 lakh; one GOLD lot needs about ₹20 lakh. Start with GOLDM, GOLDPETAL or SILVERMIC.

**The futures table** shows each contract's lot, expiry, last price, ₹ value per lot, the margin estimate, OI and volume. It refreshes every 10 seconds in the MCX session with **Live** ticked. **Click a contract** for its chart and an order ticket: side, lots, limit price (on the contract's tick, ₹1 for futures), NRML or MIS. Type the phrase to confirm, as for any order.

**The option chain** is centred on the future the options are written on (an MCX option turns into that future if it is in the money at expiry). IV and delta are worked out with Black-76, the futures-option model. Click a price to trade it.

**Rules the desk applies to every MCX order**

- LIMIT orders only, sent only in the MCX session (no AMO), and new positions only inside your **MCX trade window** (Settings, 09:00 to 23:00 by default).
- Manual orders buy, and sell what you hold. A short future or a written option is opened only by an armed F&O algo.
- The kill switch, max order value, orders per day and daily loss limit apply as for NSE orders.
- Only the eight contracts above are accepted on MCX. Crude oil, base metals and agri are refused.

**Session.** MCX trades 09:00 to 23:30 IST while the US is on summer time (mid-March to early November) and 09:00 to 23:55 while it is on winter time. The desk follows the US clock change by itself. MCX keeps its own holidays: list them in **Settings → MCX holidays**, one per line. `2026-12-25` closes the day; `2026-11-24 evening` means only the evening session (from 17:00) trades, as on many NSE holidays.

**Physical delivery: the cut-off.** Every contract above settles by delivering gold or silver. Zerodha does not take retail positions into delivery: it publishes a square-off date for each expiry (Market Intel → "MCX precious metals expiry", usually 3 to 7 days before expiry) and its risk desk closes what is left. The desk works the same way with a **delivery cut-off** (Settings, 8 calendar days by default):

- No new position in a contract within the cut-off. Closing orders are still accepted, with a warning.
- F&O algos close every MCX leg as soon as it is inside the cut-off. A futures algo then rolls to the next contract; an option algo enters again only on an expiry outside the cut-off.
- Paper positions are closed at the cut-off at the last price, the way Zerodha would close a real one. A live position you hold by hand is **not** closed by the desk: close it yourself, or Zerodha will, at its own price.
- Check Zerodha's bulletin for each expiry. If its date is earlier than the cut-off allows for, raise the cut-off.

**Charges** (Zerodha, non-agri commodities): futures brokerage 0.03% or ₹20 an order, CTT 0.01% on the sell side, MCX 0.0021%, stamp 0.002% on buys; options brokerage ₹20, CTT 0.05% of premium on the sell side, MCX 0.0418% of premium, stamp 0.003% on buys; SEBI ₹10 a crore; GST 18% on brokerage, exchange and SEBI charges. Gains are non-speculative business income, like F&O, in the tax report.

**MCX algos.** In **Algos → Options & Futures**, pick a metal under *Index or MCX metal*. Everything in [Options & Futures algos](#options--futures-algos) applies, with these differences: entry and exit times run from 09:00 to 23:25; expiries are monthly; the algo runs only in the MCX session; live margin is checked against Kite's **commodity** funds; a signal algo reads the metal's continuous futures day candles from Kite (there is no other rupee price history); and the delivery cut-off above closes positions before any expiry-day rule would.

### P&L and alerts

How the desk's own trading has done, on the account selected at the top (Paper or Live).

![P&L and alerts](img/20-pnl.png)

A sentence at the top says it in words, for example *Booked profit of ₹2,400 on 6 closed trades; 1 position still held, up ₹310 at current prices.*

| Tile | Meaning |
|---|---|
| **Total profit/loss** | Booked plus still holding. |
| **Booked (closed trades)** | Profit or loss on trades bought and sold, after charges. |
| **Still holding** | What the positions not yet closed would make or lose at the current price. |
| **Today** | Profit or loss today: what was closed today, plus the open result of positions opened today. This is the number the daily loss limit watches. |

**Account funds** sits under the tiles, so you do not need the Trade tab to see your money:

- **Paper:** Starting capital, Cash available (free to buy with), Invested (what you hold, at buy price), Account value (cash plus what you hold at the current price) and Change since start.
- **Live:** read from your broker for Equity and F&O and for Commodity (MCX): Available to trade (after margin in use), Used margin, Cash (with the opening balance and money added today, where the broker reports them) and Collateral from pledged holdings when there is any. Your broker must be connected and logged in.

Below: **Still holding** (each open position with its entry and current price), **Closed trades** (buy price, sell price, profit/loss), **Profit/Loss by algo** (each algo, the scanner, and your manual orders, so you can see which one earns its keep) and the running total day by day.

**Advanced** (closed until you open it): win rate, best and worst day, the largest fall from a peak (max drawdown), charges paid, the daily loss limit, the tax estimate and what is counted.

It counts fills the desk made: every paper fill, and live fills read from Kite's trade book for orders this desk placed. Trades you place in Kite directly, and the order a triggered live GTT sends, are not seen. For the live account Kite's console is the final word.

Every tile with a **?** explains itself: press it for what the number means, worked out with the number on screen, and a link to this manual.

### Price alerts

**P&L & alerts → Price alerts.** A message when a share, a future or a coin reaches your price.

![Price alerts](img/21-price-alerts.png)

1. Choose **Market**: *Indian shares, F&O and MCX* or *Crypto*.
2. Type the **Symbol**: `RELIANCE`, `BSE:TCS`, `NFO:NIFTY25DECFUT`, `MCX:GOLDM25DECFUT`, or a coin such as `BTCUSDT`.
3. Choose **rises to** or **falls to**, enter the **Price** and an optional **Note** (it comes with the message), then **Add alert**.

How it behaves:

- Each alert fires **once**, then shows *Fired* with the time and price. **Delete** removes one; **Remove fired alerts** clears the old ones.
- An alert on the wrong side is refused. If the price is already 2,600, "rises to 2,500" would fire at once, so the desk says so instead.
- **Indian prices come from your broker only**, and only while that market is open (NSE/BSE 09:15–15:30, MCX in its own hours). The broker must be connected and logged in. A delayed free price is never used, because it would fire late or on yesterday's close.
- **Crypto** is checked all day, every day, from the exchange's public prices.
- Prices are checked every 15 seconds while TradeSetu is running. On the desktop app that means the app must be open; on a server it runs all the time.
- The message goes to the **Alerts** list below and, when set up, to **Telegram** (Settings → Alerts to your phone).

### Learn from your trades

Two panels on the P&L tab turn your own trades into lessons. Both use the account selected at the top.

**Trade journal.** One row per trade: a position from the buy until it is sold again (a trade sold in parts counts once). Press **Write note** to record:

- **Plan**: the setup (your own name for the pattern, for example *Breakout*), why you took the trade, and the stoploss and target you planned.
- **Review**, once it closes: whether you followed the plan, the mistakes made (chased the entry, moved the stoploss, exited early, too big, against the trend, revenge trade and others), and the lesson.

A buy from the order ticket with a setup, a reason or a stoploss filled in saves its plan by itself. It is tied to the trade when the order fills; until then it waits under **Plans waiting for a fill**. With a stoploss in the plan, the journal shows each trade's result in **R**: the profit or loss divided by the risk planned (entry to stoploss). +2R made twice what was risked; −1R lost exactly the planned risk; worse than −1R means the stoploss was not kept or the price gapped through it.

**Your results.** What the closed trades say, after charges:

| Tile | Meaning |
|---|---|
| **Expectancy** | The average result of one trade. Positive means the way you trade makes money over many trades. |
| **Win rate**, **Average win**, **Average loss** | Read together: a 40% win rate pays when wins are well over twice the losses. |
| **Profit factor** | Money won ÷ money lost. |
| **Expectancy in R** | The average result in multiples of the planned risk, for trades with a stoploss in the journal. |
| **Worst losing streak**, **Largest fall** | What you had to sit through. |

The sentence above the tiles gives the likely range (95%) of the average result. When that range includes zero, the trades so far cannot tell an edge from luck. Under 30 trades the desk says so: a few lucky or unlucky trades decide the averages.

**Group by** splits the same figures by setup, who placed the trade (you, an algo, the scanner), the hour and day it was bought, how long it was held, long or short, whether you followed the plan, and mistake. Look for the setup that pays and the mistake that costs most, then trade more of the first and stop the second. A group with fewer than 30 trades is marked: it is a hint, not proof. The line under the table compares the other account (paper against live).

### Export and tax report

The **Download** panel on the P&L tab. All on the account selected at the top; the tax summary is under **Advanced**.

![Tax summary and downloads](img/32-tax-export.png)

| Download | Contents |
|---|---|
| **Profit/Loss (closed trades)** | Every closed trade: when, source, symbol, quantity, buy and sell price, profit/loss and return. |
| **Order journal** | Every order the desk sent or refused: time, source, symbol, side, quantity, price, result, reason. |
| **Fills** | Every fill with price, product and charges. |
| **Tax lots** | Each sale matched to its buys first-in first-out, with buy and sell dates, days held, cost, proceeds, charges, gain and category. |

Each button downloads an Excel file (.xlsx) that opens in Excel, Google Sheets or LibreOffice. Profit is shaded green and loss red, Buy and Long are green, Sell and Short red, failed orders are a red row, and each tax head has its own shade. The header row stays in view with a filter on every column, and the **Total (rows shown)** line at the bottom adds up only the rows the filter leaves, so filtering one algo or one month gives that algo's or month's total. Profit/Loss also has a **By source** sheet (trades, wins, losses and net for each algo and for manual trades); Tax lots has a **By financial year** sheet with the tax estimate. Tick **Plain CSV instead** for a plain CSV file.

**Tax summary by financial year (April–March):**

| Category | Rule used |
|---|---|
| **STCG** | Delivery shares held 12 months or less; estimated at 20%. |
| **LTCG** | Held more than 12 months; 12.5% on gains above ₹1.25 lakh a year. |
| **Intraday (speculative)** | Bought and sold the same day (MIS); taxed at your slab, so no rate is applied. |
| **F&O (business)** | Options and futures; non-speculative business income at your slab. Turnover (sum of absolute profit per trade) is shown for audit limits. |

The estimates ignore surcharge, cess, grandfathering (shares bought before Feb 2018), set-off of losses across heads and the rebate. Use them for planning; your tax filing should use your broker's tax P&L and a CA.

**Alerts** at the bottom lists what the desk did or could not do: orders filled or refused, an algo blocked or closed by a stop or target, the daily loss limit, Kite signing you out, security changes. Set up Telegram in Settings to get the same on your phone.

---

## 3. Settings

![Settings](img/04-settings.png)

Press **Save settings** after any change.

### AI model

Needed only for the **AI Analyze** section and AI backtests. Charts, strategies, backtests, the scanner and algos do not use it.

**Paid services** (you buy an API key from the company): *OpenAI*, *Anthropic Claude*, *Google Gemini*, *xAI Grok*, *DeepSeek*, *Mistral*, *OpenRouter (paid models)*. Choose one in **Quick setup**; the provider, endpoint and both model boxes fill in with that company's current models, and the model boxes suggest the others it offers. Paste your key and save. **Deep model** does the heavy reasoning (research debate, final decision); **Quick model** handles the many short steps, so a cheaper model there saves money.

![Paid AI services in Quick setup](img/33-paid-models.png)

To use a free service:

1. In **Quick setup** choose one: *Groq (free tier)*, *Google Gemini (free tier)*, *OpenRouter (free models)*, or *Any OpenAI-compatible service*.
2. The provider and model names fill in. The hint tells you where to get the key.
3. Paste the key in **API key** and save.

For any other service with an OpenAI-compatible API (FreeLLM and similar): choose *Any OpenAI-compatible service*, put its base address in **Endpoint URL** (it usually ends in `/v1`), put its model name in both model boxes, and paste its key.

After saving, press **Test AI connection**. The desk sends one small request to each model and shows the answer or the exact reason it failed (wrong address, rejected key, unknown model name, rate limit). Fix what it reports before running an analysis.

An address like `localhost` or `127.0.0.1` in **Endpoint URL** means the desk's own server, not your computer. A service running on your PC is not reachable from a hosted desk.

Free tiers limit how many requests you may make. One analysis makes dozens of requests, so on the smallest plans it can be slow or fail part-way.

### Risk limits

| Setting | What it does |
|---|---|
| **Max order value** | No single buy may be worth more than this. |
| **Max orders a day** | No more buys than this in a day. |
| **Buy size, % of capital** | How much a suggested or scanner order uses. |
| **Max per stock, %** | Ceiling on one holding. |
| **Limit buffer %** | How far from the last price a limit order is placed so it fills (0.3% by default). |
| **Paper capital** | Starting money of the paper account. |

The size and count limits apply to buys only. A sell is never blocked by them, so you can always exit.

### Risk per trade

| Setting | What it does |
|---|---|
| **Capital for risk** | The capital the risk % is a share of. 0 uses the paper capital. Set it to your real trading capital before trading live. |
| **Risk a trade, %** | What **Work out quantity** on the order ticket risks: 1% by default, at most 10%. |
| **Refuse above, %** | A manual order whose stoploss would lose more than this share of capital is refused (2% by default; 0 turns the check off). |
| **Refuse a manual share buy without a stoploss** | Off by default. On, a buy of shares from the ticket needs a stoploss price. |

The loss counted is the fall from the entry to the stoploss times the quantity, plus the charges of buying and selling. A price that gaps through the stoploss (news overnight, a circuit) fills worse, so the real loss can be larger. Algos follow their own exit rules and are not checked against this limit.

### Daily loss limit

**Daily loss limit (₹)**, in Risk limits. When today's loss on an account reaches it:

1. The kill switch turns on by itself: no new buys, by you or any algo.
2. Intraday algos sell what they hold.
3. You get an alert.

Delivery holdings are left alone, and you can still sell anything yourself. The limit is counted for paper and live separately, from the **Today** figure on the P&L screen. It trips once a day: you turn the kill switch off yourself when you are ready. 0 switches the limit off.

### Trade window

| Setting | Default | Meaning |
|---|---|---|
| **Start buying at** | 09:30 | No new buy before this. |
| **Stop buying at** | 15:00 | No new buy from this time. |
| **Square off intraday at** | 15:00 | Intraday algos sell whatever they hold at this time. |
| **Take new buys only inside this window** | on | Untick to allow orders at any time, including after-market orders. |

Selling is allowed whenever the market is open.

### Safety switches

- **Kill switch**: blocks every new buy and buy GTT, paper and live, manual and automatic. Selling stays allowed, so you and your algos can always exit. Use it when something looks wrong.
- **Allow live orders with real money**: off by default. Nothing reaches Zerodha until this is ticked.
- **Show the Charts tab**: untick to hide charts.
- **If Kite cannot serve a chart, fall back to delayed Yahoo data**: off by default. With it off, charts come from Kite only.

### Alerts to your phone (Telegram)

![Alerts and holidays in Settings](img/22-alerts-holidays.png)

1. In Telegram, open a chat with **@BotFather**, send `/newbot`, follow the two questions, and copy the token it gives you.
2. Open your new bot's chat and press **Start** (a bot cannot message you until you do).
3. Open a chat with **@userinfobot**; it replies with your numeric id. That is your chat id.
4. In Settings paste the **Bot token** and **Chat id**, tick **Send alerts to Telegram**, and Save.
5. Press **Send a test alert**. A message should arrive within seconds; if not, the reason is shown.

The same alerts always appear on the P&L & alerts screen, with or without Telegram.

### Exchange holidays

List the days NSE is closed, one per line as `YYYY-MM-DD`. On those days the top bar shows **Holiday**, orders are refused and algos do not run.

- **Add the fixed-date holidays** fills in the five that never move (26 Jan, 1 May, 15 Aug, 2 Oct, 25 Dec) for this year and next.
- The festival holidays change every year. NSE publishes the list each December (nseindia.com → Resources → Holidays). Paste the *trading holidays* once a year and Save.
- The note under the box warns when the current year looks incomplete (NSE usually has about 15).

A holiday missing from the list is treated as a normal day: the desk will try, and the broker will reject the orders.

**MCX holidays** are a separate list under it, because MCX often trades its evening session on NSE holidays. See [Gold & silver (MCX)](#gold--silver-mcx).

### Two-factor sign-in

Adds a second step to signing in: a code from an authenticator app on your phone. Strongly recommended, because this desk can place real orders.

![Setting up two-factor sign-in](img/21-two-factor.png)

1. Install an authenticator app (Google Authenticator, Microsoft Authenticator, Authy, 1Password).
2. In Settings → **Two-factor sign-in**, type your desk password and press **Set up two-factor**.
3. Scan the QR code with the app (or type the key shown under it).
4. Enter the 6-digit code the app shows and press **Turn on**.
5. **Save the eight recovery codes** shown next, somewhere other than your phone. They are shown once. Each works one time in place of a code.

From then on, sign-in asks for the password and a code. Five wrong attempts lock that device out for 15 minutes, codes included.

To turn it off: password plus a current code (or a recovery code). If you lose both the phone and the recovery codes, whoever runs the server can set `DESK_2FA_RESET=1` in the server environment, redeploy once, then remove it.

### Passkeys

Sign in with your phone's or laptop's fingerprint, face or screen-lock PIN: no password, no code.

![Passkeys in Settings](img/34-passkeys.png)

**Add one** (on each device you use):

1. Open the desk on that device, signed in as usual.
2. Settings → **Passkeys**: give it a name ("My phone"), type your desk password, press **Add a passkey on this device**.
3. The phone or browser asks for your fingerprint, face or PIN. Done.

**Sign in:** on the sign-in page press **Sign in with a passkey** and confirm with your fingerprint, face or PIN.

![Passkey sign-in](img/35-passkey-sign-in.png)

- A passkey counts as both factors (the device you hold plus your fingerprint, face or PIN), so it skips the authenticator code even when two-factor is on.
- It works only on the desk's own address and only over HTTPS. A fake site cannot use it, and a passkey made on one address does not work on another. If your desk is reached through more than one address, set `DESK_PUBLIC_URL` (for example `https://desk.example.com`) in the server environment so every passkey is made for that one.
- The desk stores only the public half of the key. Adding one needs your password, so a stolen browser session cannot plant one.
- **Remove** next to a passkey stops it working at once (do this if a device is lost). Password sign-in always remains.
- Passkeys sync with your Google or Apple account, so one made on a phone usually works on your other devices signed in to the same account.

### Broker

Choose the broker the desk trades through. **Zerodha Kite** is the tested broker. **Upstox, Dhan, Angel One, Fyers and Groww** are experimental: they are built on each broker's published API but have not yet been run against a live account. Use them in Paper mode first, compare holdings, positions and prices with your broker's own app, and check every live order there.

Pick the broker, fill in its fields and press **Save settings**. Secret fields show only their last four characters once saved; leave them empty to keep the saved value. Then on the **Trade** tab press **Connect**, then **Log in to …**.

| Broker | What to enter | Signing in |
|---|---|---|
| Zerodha Kite | Nothing here: the MCP server below | Through the Kite MCP server, as before |
| Upstox | API key and secret from your Upstox developer app | **Log in to Upstox** gives a link to Upstox's login page; after signing in you come back to the desk. Register the redirect URL shown under the broker list on the Upstox app. |
| Dhan | Client id and the access token from web.dhan.co → My Profile → DhanHQ Trading APIs | **Log in to Dhan** checks the token. Paste a new token when it expires. |
| Angel One | SmartAPI key, client code, account PIN and TOTP secret (the text under the QR code at smartapi.angelone.in/enable-totp) | **Log in to Angel One** signs in at once with a fresh one-time code. |
| Fyers | App ID and secret key from myapi.fyers.in | Like Upstox: a link to Fyers' login page, then back to the desk. |
| Groww | An access token from groww.in/trade-api, or an API key with its secret (approve the key on Groww each day) or TOTP secret | **Log in to Groww** signs in with what you gave. |

Every broker's session ends overnight; log in again each trading day. Instruments keep Kite's names whichever broker you use (`NFO:NIFTY25NOV24000CE`), so algos and watchlists carry over when you change broker. GTT orders are Kite only, and charges are estimated with Zerodha's schedule.

### Broker connection (MCP)

Kite only. Set up during installation. Leave it unless you move the Kite server.

---

## 4. Charts

The Charts section opens on the **Live chart**. The older chart, with Supertrend, Parabolic SAR, Donchian, pivots, ADX and Stochastic, is one tap away under **Classic chart** and works as before.

### Live chart

![Live Bitcoin chart with EMA, Bollinger Bands and RSI](img/07-live-chart.png)

1. Pick **Crypto**, **NSE / BSE** or **Imported**.
2. Type or pick a symbol (`BTCUSDT`, `ETHUSDT`; `RELIANCE`, `NIFTY`) and press **Show**.
3. Pick the candle size: **1m**, **5m**, **15m**, **1h**, **4h**, **1D** or **1W**, and **Candles**, **Line** or **Area**.
4. Open **Indicators** to add a moving average (simple or exponential), Bollinger Bands, VWAP, RSI or MACD. Add the same one twice for two periods (for example EMA 20 and EMA 50). Periods and widths are under **Advanced: indicator settings**. Tick **Volume bars** to show or hide volume.

Indicators move with every live price, not only when a candle closes. The pill at the top right says where prices come from:

- **Live · Binance stream** or **Live · Coinbase stream**: prices stream straight from the exchange.
- **Updating every 5 s through the desk**: the exchange's stream could not connect from your network, so the desk fetches fresh candles instead.
- **Live · Kite** (or your broker's name): NSE/BSE last price from your broker every 3 seconds, with fresh candles each minute.
- **Market closed** or **Live prices stopped**: the reason is shown; the chart keeps the last prices it had.

Where prices come from:

- **Crypto**: Binance's public market data. If Binance does not answer (it has been blocked in India before), the desk uses Coinbase Exchange and says so above the chart. Coinbase quotes US dollars, so a USDT pair shows the coin's USD price (usually within 1%). BNB and TRX are not on Coinbase. No API key is needed.
- **NSE / BSE**: only your broker (log in on the Trade section). There is no free, legal live feed for Indian stocks, so without a broker the chart says so instead of showing other data. Yahoo is never used here. 4-hour candles are 09:15–13:15 and 13:15–15:30; weekly candles start on Monday.

If prices cannot be loaded you see **Chart not shown** with the reason and **Try again**, never an empty chart or a zero. Your symbol, candle size and indicators are remembered on this device.

### Watchlist, drawings, imported files and crypto paper trading

![Chart with a trend line, Fibonacci levels, a horizontal line, the watchlist and paper trading](img/08-chart-tools.png)

**Watchlist.** Open a chart and press **+ Add this chart**. Each row shows the last price and the change today; tap it to open that chart, **×** to remove it. Crypto prices come from the exchange, NSE/BSE from your broker (logged in), imported files show their last close. A price that cannot be loaded shows **No price**, never a zero. The list (up to 50) is saved by the desk, so it is the same on every device that opens it.

**Drawings.** Press **Trend line** (tap two points), **Horizontal line** (tap one price) or **Fibonacci** (tap a high and a low; the 0, 23.6, 38.2, 50, 61.8, 78.6 and 100% levels are drawn between them). Dragging the chart does not place a point. **Undo** removes the last drawing and **Clear all** removes them all. Drawings are saved for each symbol and come back the next time you open it.

**Import a CSV.** Pick **Imported**, choose a file, give it a name and press **Import CSV**. Columns: Date (or Date and Time), Open, High, Low, Close, and Volume if you have it; dates may be `2026-07-24`, `24-07-2026` or `24/07/2026 09:15`, read as India time. Bad rows are skipped and listed; a file with more than 1 in 10 bad rows is refused. The candle size is read from the file, so a daily file cannot be shown as 1h (those buttons are greyed out); weekly is built from it. Imported prices are never live. Use this for NSE/BSE until a broker is connected: when the broker is not logged in, the chart offers **Import a CSV instead**.

**Crypto paper trading.** Under the chart, with practice money only. No real order is ever sent from here.

1. Open the coin's chart. Pick **Buy** or **Sell**, **Market** or **Limit**, enter the quantity (and the limit price) and press **Buy BTC** / **Sell BTC**.
2. A market order fills at the last price. A limit order waits under **Waiting orders** and fills when the price reaches it; the desk checks every 5 seconds. A waiting buy holds its cash, a waiting sell holds its coins. You can only sell what you hold (no short selling).
3. **Positions** shows quantity, average price, last price and profit/loss; **History** every fill with its fee. Your average price and waiting limit orders are drawn on the chart.
4. The tiles show account value, profit/loss since the start, free cash and fees paid. If a coin's price cannot be loaded, account value shows **–** instead of a wrong number.

Each fill pays a 0.1% fee and the smallest order is 1 USDT. **Advanced: fees, reset** has **Reset paper account**: set a starting balance (default 10,000 USDT) and press twice. It deletes all paper orders, positions and history.

### Classic chart

Candlestick charts with the desk's server-side indicators.

![Daily chart with Bollinger Bands and Supertrend](img/05-chart-daily.png)

Charts are drawn from Kite's own candle data, so **connect and log in to Kite first** (Trade section). This needs a Kite Connect key on the paid plan; the free Personal API carries no market data. If Kite is signed out or cannot serve a chart, the desk says so instead of quietly showing other data.

1. Type a symbol: `RELIANCE`, `TCS`, `NIFTY`, `BANKNIFTY`.
2. Choose the candle size: **1m**, **2m**, **3m**, **4m**, **5m**, **15m**, **30m**, **1h** or **1D**.
3. Press **Show chart**.
4. Tick what you want drawn. The chart redraws at once.

**On the price chart:** SMA 20/50/200, EMA 20, VWAP (intraday only), Bollinger Bands, Supertrend, Parabolic SAR, Donchian channel, Pivots (traditional: daily, weekly, monthly).

**Panels below:** Volume, RSI, MACD, ADX, Stochastic. Tick as many as you like; they share one time axis.

Scroll to zoom, drag to move. Hover to read a candle's open, high, low and close.

### Live charts

![Five-minute live chart with VWAP and pivot points](img/06-chart-5min-live.png)

With **Live** ticked, the chart refreshes every 15 seconds while the market is open. The pill at the top right tells you where the candles come from:

- **Live · Kite**: live candles from Kite.
- **Live · Yahoo (delayed)**: only if you switched the fallback on in Settings and Kite could not serve the chart.
- **Market closed**: the last session is shown.

How far back each candle size goes on a chart:

| Candle | History |
|---|---|
| 1 minute | 14 days |
| 2 minutes | 14 days |
| 3 minutes | 30 days |
| 4 minutes | 20 days |
| 5 minutes | 60 days |
| 15 minutes | 120 days |
| 30 minutes | 180 days |
| 1 hour | 1 year |

Kite has no 2 or 4-minute candles, so the desk builds them from Kite's 1-minute candles (3 and 30 minutes are Kite's own). Every size lines up with the session open, as Kite's do: a 4-minute chart reads 09:15, 09:19, 09:23 … and its last candle of the day, 15:27, holds the 3 minutes left before 15:30. On MCX the candles start at 09:00. Live charts, backtests and algos use the same candles.
| 1 day | about 5 years |

Kite's daily candles are not always adjusted for splits and bonuses, so an old split can show as a sudden drop on a chart. Backtests fold those gaps back (see Intraday backtests).

**Pivots** are traditional (floor) pivots: P = (H + L + C) / 3, R1 = 2P − L, S1 = 2P − H, R2 = P + (H − L), S2 = P − (H − L), R3 = H + 2(P − L), S3 = L − 2(H − P), from the previous day's, week's or month's high, low and close. Tick **daily**, **weekly** and **monthly** separately; each draws P solid and R1-R3, S1-S3 dashed. Kite does not publish pivot levels (neither does the exchange), so the desk computes them from the candles. The table under the chart lists today's levels (on a 1D chart, the daily row is for the next session). A level appears only when the whole previous period is in the chart's data: weekly pivots on a 1-minute chart need the last full week, monthly pivots need a 15-minute or longer chart.

The dark theme applies to charts too:

![Chart in the dark theme](img/07-chart-dark.png)

---

## 5. Strategies: combine indicators

Build your own strategy from several indicators, with no code.

![Strategy builder](img/08-strategy-builder.png)

A strategy has two rule lists:

- **Buy when**: the entry rules.
- **Sell when**: the exit rules.

Each rule compares an indicator with a number or with another indicator.

### Build one

1. Open **Strategies**. Give the strategy a name.
2. In **Buy when**, choose whether **ALL** rules must be true (AND) or **ANY** one (OR).
3. For each rule pick: the indicator on the left, its settings (for example the period), the comparison, and the right side (a number or another indicator).
4. **+ Add rule** adds another line. **Remove** deletes one.
5. Do the same under **Sell when**.
6. Press **Save strategy**.

The example in the picture:

> **Buy when ALL:** RSI(14) is below 40 **and** Close is above SMA(100)
> **Sell when ANY:** RSI(14) is above 62 **or** Close crosses below the Supertrend line

Comparisons: *is above*, *is below*, *is at or above*, *is at or below*, *crosses above*, *crosses below*. A "crosses" rule is true only on the candle where the crossing happens.

Indicators you can use: Close, Open, High, Low, Previous close, SMA, EMA, VWAP, Bollinger upper/middle/lower, Supertrend line, Parabolic SAR, highest high and lowest low of the last N candles, traditional pivots P, R1-R3 and S1-S3 (daily, weekly or monthly), RSI, MACD line/signal/histogram, ADX, +DI, −DI, Stochastic %K/%D, CCI, Awesome Oscillator, ATR, Volume, Average volume.

**Time and day rules.** Two more choices on the left side:

- **Time of day** *is after*, *is before*, *is at or after* or *is at or before* a time such as 09:20. Intraday candles only. Example: "Buy when Close crosses above VWAP **and** Time of day is after 09:30".
- **Day of week** *is one of* the days you tick. Example: "Buy when … **and** Day of week is one of Mon, Tue".

Good to know:

- If the buy rules and sell rules are both true on the same candle, the sell wins.
- The strategy holds one position at a time: buy, then sell, then it can buy again.
- A strategy that uses VWAP works on intraday candles only.

### Your saved strategies

![Saved strategies](img/09-my-strategies.png)

**Backtest** opens it in the Backtest section. **Edit** loads it back into the builder. **Delete** removes it (not allowed while an algo uses it).

Saved strategies appear under **My strategies** in the Backtest and Algos strategy lists.

### Built-in strategies

| Strategy | Buys when | Sells when |
|---|---|---|
| SMA crossover | Fast average above slow | Fast below slow |
| EMA crossover | Fast EMA above slow | Fast below slow |
| Supertrend | Price above the Supertrend band | Price falls below it |
| RSI mean reversion | RSI is oversold | RSI recovers |
| MACD | MACD line above signal | Below signal |
| Donchian breakout | New N-day high | New M-day low |
| Bollinger reversion | Close below lower band | Close back at the middle |
| Price-volume breakout | Close above the 20-candle high on 3× average volume | New 10-candle low |
| Heikin-Ashi trend | N green Heikin-Ashi candles in a row | First red candle |
| Parabolic SAR | Price above the SAR | Price below it |
| Awesome Oscillator | Oscillator above zero | Below zero |
| Dual Thrust | Close above open + share of recent range | Close below open − share of range |
| Bollinger + RSI | Below lower band and RSI oversold | RSI overbought |
| ADX trend | ADX strong and +DI above −DI | −DI above +DI |
| VWAP trend (intraday) | Price above VWAP and fast EMA above slow | Either fails |
| Opening range breakout (intraday) | Close above the high of the first 15 minutes | Close below its low |
| ML: return forecast (Qlib-style) | A model forecasts a rise above the threshold over the next days | The forecast drops below it |
| Buy and hold | First candle | Never (the baseline to beat) |

Heikin-Ashi, Parabolic SAR, Awesome Oscillator and Dual Thrust follow the strategies in the `je-suis-tm/quant-trading` project; Bollinger + RSI and ADX trend follow `freqtrade/freqtrade-strategies`; the price-volume breakout follows Zerodha's algo video. All are written from the indicators' standard definitions.

### Machine learning: return forecast

**ML: return forecast (Qlib-style)** learns from the stock's own past which price and volume patterns came before a rise, and buys while its forecast for the next few days is above your threshold.

- **What it looks at**: returns over 1 to 60 days, the price against its 5 to 60-day averages, volatility, where the close sits in its 10 to 60-day range, volume against its average, and the shape of each day's candle. These follow the Alpha158 features of Microsoft's Qlib project (MIT licence). The model is a ridge regression, Qlib's linear baseline.
- **No peeking**: it trains walk-forward. On each day it learns only from days whose outcome was already known, retrains every 21 bars on the last **Training bars**, and never uses a later price.
- **Settings**: *Forecast days ahead* (5), *Buy when forecast rise above %* (0.5), *Training bars* (250, about a year).
- **It starts flat**: it buys nothing until it has the training bars, so the first year of a backtest shows no trades. Give it a longer period.
- **What to expect**: on a test series with a pattern planted in it, it finds the pattern; on a random walk it finds nothing, as it should. Real stocks are close to random from day to day, and a simple forecast has little edge once charges are paid. Run it through the **AI scan**, which judges it on prices its settings never saw and against buy and hold, before you trust it with an algo.

### Strategies of market legends

Strategies built from the published rules of well-known traders, plus scalping and swing set-ups. In every strategy picker they are grouped (**Market legends, Trend following, Breakout, Swing trading, Mean reversion, Scalping & intraday, Baseline**). Choosing one shows its rules, the book they come from, and where it works:

![A legend strategy with its market badges](img/38-legend-strategies.png)

**Markets** badges: ✓ Bull, ✓ Sideways, ✓ Bear mark the market types it was designed for. All strategies here buy and sell (long only). "Bear" means it either stays in cash during falls, or makes money when its signal is traded **short in the index futures backtest**.

| Strategy | Rules | Bull | Sideways | Bear |
|---|---|---|---|---|
| **Mark Minervini**: Trend Template breakout | All 8 Trend Template conditions, then a close above the 20-day pivot; out below the 50-day. Cut losses at 7-8% (set the stoploss). | ✓ | | cash |
| **Turtle Traders** (Dennis & Eckhardt) | Buy a 20-day high, sell a 10-day low, stop 2 × ATR below entry | ✓ | | ✓ (short) |
| **Nicolas Darvas**: box | A tight box near the 52-week high broken on volume; out through the box floor | ✓ | | cash |
| **Jesse Livermore**: pivotal point | New 50-day high above the 200-day on volume; out on a 10-day low | ✓ | | ✓ (short) |
| **Stan Weinstein**: Stage 2 | Breakout above a rising 30-week (150-day) average on volume; out below it | ✓ | | ✓ Stage 4 short |
| **William O'Neil**: CAN SLIM breakout | 7-week base breakout within 5% of the 52-week high, volume 1.5×; out below the 50-day; 7-8% stop | ✓ | | cash |
| **Larry Connors**: RSI(2) | Above the 200-day, RSI(2) under 10; out above the 5-day average | ✓ | ✓ | cash |
| **Linda Raschke**: Holy Grail | ADX > 30 and rising, pullback to the 20 EMA, buy above that bar's high | ✓ | | ✓ (short) |
| **Larry Williams**: volatility breakout | Close > open + 0.6 × yesterday's range; hold 3 days | ✓ | ✓ | |
| **Paul Tudor Jones**: 200-day rule | Long above the 200-day average, cash below | ✓ | | cash |
| **John Bollinger**: Squeeze | Band width near a 6-month low, then a close above the upper band | ✓ | ✓ | |
| **Toby Crabel**: NR7 | Narrowest range of 7 days, then a close above its high | ✓ | ✓ | |
| **Warren Buffett**: greedy when others are fearful | Buy 20% below the 52-week high with RSI under 35; sell back near the high | | | ✓ buys falls |
| Scalp: RSI(2) above VWAP (1-5 min) | RSI(2) under 10 while above VWAP; out above 70 or below VWAP | ✓ | ✓ | |
| Scalp: VWAP bounce (1-5 min) | A dip to VWAP that closes back above it, EMA rising | ✓ | | |
| Swing: pullback to the 50-day | In an uptrend, a dip to the 50-day with RSI cooled; out at a new 20-day high | ✓ | | |
| Swing: inside-bar breakout | An inside day, then a close above the mother bar's high | ✓ | ✓ | |

Where a strategy departs from its author it says so in its description. For example, Williams enters intraday on a stop, but on daily candles the desk enters at the next open. O'Neil's earnings and market checks (the C, A and M of CAN SLIM) are left to you.

**Buffett, Graham, Peter Lynch, Joel Greenblatt and Vijay Kedia** chose businesses, not chart patterns. Their rules are on the Scanner tab as screens (below). **Ray Dalio's All Weather** is an asset mix and is on Backtest → ETF portfolios. **F&O strategies** (straddles, strangles, guts, condors, butterflies, spreads, ratio spreads, calendars, diagonals, protective put, covered call, collar) are on Backtest → Option strategy, with a payoff calculator.

None of these guarantees anything. A legend's rules worked in their markets and years; test them on Indian stocks and on recent years before trusting them.

---

## 6. Backtest

Test a strategy on past prices before risking money.

![Daily backtest of a custom strategy](img/10-backtest-daily.png)

Everything is on one screen. At the top, **What to test** picks the kind of backtest, and **From**, **To** and **Capital** are shared by all of them:

| What to test | Section below |
|---|---|
| **Stocks: buy and sell shares** | This one |
| **Option strategy** (straddle, strangle, iron condor…) | [Option strategies](#option-strategies-straddles-condors-spreads-hedges) |
| **Buy index options: calls or puts** | [Options buying backtest](#options-buying-backtest-simulated) |
| **Index futures** | [Index futures backtest](#index-futures-backtest) |
| More tools: **Find the best settings**, **ETF portfolios**, **AI agents' ratings** | [Optimise](#optimise-a-strategy-walk-forward), [Portfolios](#portfolios-ray-dalios-all-weather), [AI agents](#ai-agents-backtest) |

1. Choose **Stocks**, then enter the **Stock**, the **Candle** size and the **Trade type**: **Intraday** (buy and sell the same day) or **Positional** (hold for days). Day candles are always positional.
2. **Use of capital per trade %**: how much of the capital each buy uses.
3. **Entry when and exit when.** Pick a **Strategy** (built-in or one you saved), or **My own rules** to type the rules right here: *Buy when* (entry) and *Sell when* (exit), exactly as on the Strategies tab, time and day rules included. **Save these rules as a strategy** keeps them for later.
4. For an intraday trade, **Start buying at**, **Stop buying at** and **Sell everything at** set the trading hours for this test (empty = your Settings trade window). A positional trade has no such times.
5. **Exit rules** (0 means off), below. For a positional trade, **Advanced → Sell after holding this many trading days** sells it at the open of that trading day after the buy (1 = the next session; 0 = no limit).
6. Press **Run backtest**.

### Exit rules: stoploss, target, trailing, re-entry

The same box is used by stock backtests, stock algos, and each leg of an option strategy.

| Box | What it does |
|---|---|
| **Measure in** | **% of the price** or **points** (rupees a share). For an option leg: % of the premium or points of premium. |
| **Stoploss** | Sell when the price falls this far below the buy price. |
| **Target** | Sell when the price rises this far above the buy price. |
| **Trailing stoploss** | Two boxes: *each time the price moves this much in your favour*, *move the stoploss this much*. Example: buy at 100, stoploss 10 points (90). With 10 and 5, the stoploss moves to 95 when the price reaches 110 and to 100 at 120. It never moves back. |
| **Move stoploss to the entry price once the profit is** | Once the trade is this far in profit, the stoploss jumps to the buy price, so the trade cannot turn into a loss (apart from gaps and charges). |
| **Re-enter after a stoploss** | Up to 5 times. **When the price comes back to the entry price**: buy again at the old buy price once the price returns there. **Straight away, on the next candle**: buy again at the next candle's open. |

Re-entry happens only after a plain stoploss. A target, a trailing stoploss, a stoploss moved to cost or a sell signal ends the trade normally, and the next buy waits for a fresh signal.

If both the stoploss and the target are inside one candle, the stoploss is assumed to have come first.

### Create an algo from the result

Under every stock, option strategy and index futures result there is a **Create algo** panel. Give it a name, a **Budget** (stocks) or **Maximum loss a day** (options and futures), choose **Paper** or **Live**, and press **Create algo**. It copies the stock, candle, rules and exit rules you just tested. With **My own rules**, the rules are saved as a strategy under the algo's name. The algo is created stopped; start it on the Algos tab.

The algo also gets the trade type (intraday or positional) and the holding limit. Two differences from the backtest: an intraday algo uses the trade window in **Settings**, not the times typed for the test (a positional algo on minute or hourly candles buys any time in market hours, or inside the Settings window when Settings enforces it), and an option strategy algo stops on **Maximum loss (₹)** instead of the whole-position % stoploss. Buy-index-options backtests have no algo.

### Reading the result

| Tile | Meaning |
|---|---|
| **Total return** | Gain or loss over the whole period, after all charges. Buy & hold is shown for comparison. |
| **CAGR** | The return per year. |
| **Max drawdown** | The worst fall from a peak. Could you sit through it? |
| **Sharpe / Sortino** | Return for the risk taken. Above 1 is good; below 0 means a fixed deposit did better. |
| **Win rate** | Share of trades that made money. |
| **Profit factor** | Money won ÷ money lost. Above 1.5 is healthy. |
| **Charges paid** | Brokerage, STT, stamp duty, exchange fees, GST and DP charges. |

The chart compares your strategy (blue) with simply holding the stock (orange) and the Nifty 50 (green). If blue is not clearly better than orange, the strategy added work, not money.

**Can you trust this result?** Under the tiles the desk checks three things:

- **Enough trades.** Fewer than 30 closed trades and the result can be luck.
- **Better than a safe rate.** What the capital would have grown to at the risk-free rate in Settings (6.5% a year by default). A strategy that ends below it took risk for nothing.
- **The same trades in random orders.** The closed trades are drawn again at random, with repeats, 1,000 times. It shows the fall from a peak you should be ready for (typical, and 1 run in 20), the range of final values and how often the same trades lost money. A strategy whose result depends on the lucky order of a few trades shows a wide range.

The **Trades** table lists every trade with its entry, exit, profit and the reason it closed: *signal*, *stoploss*, *trailing stoploss*, *stoploss at cost*, *target*, *square-off* (intraday) or *held N days* (the holding limit). The **Re-entries** tile counts the buys made again after a stoploss.

### Intraday backtests

![Intraday backtest of an opening range breakout](img/11-backtest-intraday.png)

Choose a 1, 2, 3, 4, 5, 15, 30-minute or 1-hour candle. With **Trade type: Intraday** (the default on these candles) the backtest trades the way an intraday algo does:

- Buys only between **Start buying at** and **Stop buying at** (your Settings trade window, 09:30 to 15:00 by default, when left empty).
- Sells everything at the **Sell everything at** time. Nothing is held overnight.
- Uses intraday (MIS) charges.

### Positional (swing) backtests

With **Trade type: Positional** the same minute or hourly candles decide the entries and exits, but the trade is held across days:

- No trading hours: it can buy on any candle, and nothing is sold at a set time.
- It is held until the strategy's exit, the stoploss, the target or the holding limit closes it.
- **Gaps.** A stoploss or target the price jumps past overnight sells at the next day's open price, not at its level: a stoploss at 95 when the stock opens at 90 sells at 90. This is the real cost of holding overnight, and the result includes it.
- Delivery (CNC) charges: STT on both sides, stamp duty and the DP charge on the sell. A trade bought and sold the same day is charged at intraday rates, as the exchange treats it.
- **Advanced → Sell after holding this many trading days** sells at the open of that trading day after the buy.

Day candles are always positional: the trade-type choice is fixed and there are no trading hours. Results saved or algos made before this choice keep working as before: intraday candles meant intraday, day candles positional.

**With Kite logged in**, every stock backtest, daily or intraday, uses Kite's candles. Kite keeps intraday candles of every size from early 2015, and the desk sets no limit of its own: it asks for them in pieces Kite accepts (60 days of 1-minute candles a request, 100 days of 3 and 5-minute, 200 of 15 and 30-minute, 400 of hourly), about three requests a second. A range that starts before 2015 is tested from where Kite's history starts, and the page says so. Years of 1-minute candles take a minute or two to download; a second run on the same dates (other stoploss or target) reuses them for 30 minutes.

Kite's prices are not always adjusted for splits and bonuses. When a day opens at a usual split or bonus ratio below the previous close (half for a 1:1 bonus, a tenth for a ten-for-one split), the desk checks the free feed's adjusted prices for that day: no fall there means a corporate action, and earlier prices are divided by the ratio so the gap does not fire a stoploss. The page lists each one it adjusted.

**Without Kite**, the free feed (Yahoo) keeps only 7 days of 1, 3 and 4-minute candles, 60 days of 2, 5, 15 and 30-minute, and 2 years of hourly.

**Prices from** (a tile above the results) names the source and the dates the candles cover. When they cover less than the dates you chose, or Kite could not serve them, **Can you trust this result?** says so first: the figures are for the shorter period only.

**Stoploss and target that never fired.** If you set a stoploss or target and no trade reached it, the trust box says so, with the deepest fall and highest rise any trade had from its entry. The trade list shows both for each trade (**Lowest / highest**, in % of the entry price). An intraday trade that never moved 3% cannot hit a 15% stoploss, so the results are the same as with none. Re-entry happens only after a stoploss, so it changes nothing either until the stoploss fires. Re-entries are marked "(re-entry)" in the trade list.

### AI scan: find a winning strategy across an index

**Backtest → What to test → AI scan.** Pick an index (Nifty 50, Nifty 100…), the dates, the capital and how wide to search, then **Run AI scan**. The desk backtests every built-in day-candle strategy on every stock of the index, over a small range of each strategy's settings, with delivery charges (brokerage, STT, stamp duty, exchange and SEBI fees, GST, DP charges) and slippage. The manual backtest is unchanged.

How it avoids picking a lucky winner:

- **Two parts of the period.** Settings are chosen on the earlier part (in-sample, 70% by default). Strategies are ranked only on the later part, which the choice never saw ("unseen").
- **One strategy across all stocks.** Each strategy runs as one portfolio: the same settings on every stock, the capital split equally. A strategy that works on many stocks is worth more than a separate best fit for each stock (the **Each stock** table shows why).
- **Luck test.** The winner's Sharpe ratio is corrected for how many strategies were compared (deflated Sharpe ratio). It must reach 95%.
- **Neighbouring settings.** If most settings next to the chosen ones lost money on unseen data, the result is marked **Fragile**.
- **Enough trades.** Fewer than 30 unseen trades across all stocks is **Too few trades**.
- **Stoploss and target** levels (5% and 10% stoploss, 20% target) are tried on the three best strategies, chosen in-sample as well.

Verdicts: **Beats buy and hold**, **Works** (passed, but holding the same stocks did better), **Could be luck**, **Fragile**, **Below a fixed deposit**, **Too few trades**, **Loses money**. If nothing passes, the page says so: then buying and holding was better, and the ranking is research, not a strategy to trade.

**Download Excel** gives a colour-coded workbook: the ranking (Summary), the method and caveats (Read me), each stock, the stoploss and target trials, every backtest run, and stocks left out. The explanation is written from the numbers by fixed rules; no AI model invents figures.

Notes: day candles only (intraday across 50+ stocks would hit the broker's history limits, three requests a second on Kite). Prices come from your broker when it is signed in, else Yahoo, and are saved, so a second scan of the same dates loads faster. A Nifty 50 scan at the Standard search is about 12,000 backtests and takes a few minutes; it runs in the background and **Stop** keeps what was tested so far. Today's index members are the stocks that did well enough to be in it, so every past result is flattered (survivorship). Set `DESK_SCAN_WORKERS` to change how many processor cores it uses (default: all but one, at most 4).

### Optimise a strategy (walk-forward)

**Backtest → What to test → Find the best settings.** Finds settings for a strategy without fooling yourself.

![Walk-forward optimiser](img/30-optimiser.png)

1. Enter symbol, dates, candle and strategy. For each setting give a **from / to / step** range (for example fast average 5 to 30 step 5). The count under the form shows how many combinations; the limit is 400.
2. **Choose by**: Sharpe, CAGR, total return, Calmar (CAGR ÷ drawdown) or profit factor.
3. **Windows** (folds), **In-sample share %** and **Minimum trades** (settings that trade less are ignored).
4. **Run optimiser.** It runs in the background; long ranges take a minute or two.

How to read it:

- **Walk-forward** is the honest result. The period is cut into windows; in each the best setting is chosen on the first part (in-sample) and then traded on the next part, which it has not seen. Only those unseen parts are stitched into the walk-forward line and tiles.
- **Efficiency** = unseen CAGR ÷ in-sample CAGR. Under about 50%, the "best" settings were fitted to noise.
- **Settings stable?** Whether the windows kept choosing similar settings. If every window picks something different, no setting is really better.
- **In-sample grid**: every combination on all the data, best first. It flatters; do not trade its top row just because it is top.
- **Is the best setting luck?** A score from 0% to 100%. The period is cut into 10 pieces; many times over, the best setting is picked on half of them and checked on the other half. The score is how often that pick then landed in the bottom half. Under 20% is good. Over 50% means the winner is mostly luck: do not trust it, whatever its return.
- **Every setting at a glance**: a coloured map of every combination of two settings (green better, red worse; the best has a dark border). A winner in a broad green area is believable. A lone green square among red ones is usually luck. A dash means the setting was impossible (for example a fast average longer than the slow one) or traded too little.

![Is the best setting luck?](img/31-optimiser-luck.png)

### Pair trading: two stocks that move together

**Backtest → What to test → Pair trading.** Some stocks usually move together, like two large private banks. When the gap between them becomes unusually wide, the test buys the cheaper one and sells the dearer one, equal money in each, and closes when the gap is back to normal.

![Pair trading](img/32-pair-trading.png)

1. Pick an **Example pair** or type **Stock A** and **Stock B**, the dates and the capital.
2. **Run pair test.**

Advanced settings, in plain words:

| Setting | Meaning |
|---|---|
| **Look back (days)** | How many past days decide what "normal" is. 60 by default. |
| **Open when the gap is** | How unusual the gap must be to trade, in multiples of its usual size (a z-score). 2 means about the widest 5% of days. |
| **Close when it is back to** | The gap at which the trade is closed. 0.5 by default. |
| **Stoploss gap** | Close if the gap keeps widening to this size. 0 turns it off. |
| **Close after (days)** | Close a trade that has not worked after this many days. 0 turns it off. |
| **Costs per trade, each stock** | Brokerage, taxes and slippage, charged on both stocks at entry and at exit. |

How to read it:

- **Move together** is the correlation of the two stocks' daily moves (1 = always together). Pairs work best above about 0.7.
- **Gap closes in** is how long the gap usually takes to halve. If it "does not", it is not a pair.
- **Right now** says whether the rule would trade today, and which way.
- **The gap** chart shows the z-score with the opening levels as dotted lines.

Decisions use each day's closing prices and are traded at the next day's open, so the test never uses a price it could not have had.

**In India:** a share you sell must be bought back the same day. To hold a pair overnight you need the stock futures of both, so choose two F&O stocks and size by lots. This test uses share prices and ignores lot sizes and the cost of rolling futures each month.

### Test a stock-picking signal across an index

**Backtest → What to test → Test a stock-picking signal.** Does "buy the stocks with the best 6-month return" really work on Nifty 500? This test answers that kind of question.

![Signal test](img/33-signal-test.png)

1. Choose the **Index** (Nifty 500 by default), the **Signal**, the dates and how long to **Hold** (1 week to 3 months).
2. **Run signal test.** Nifty 500 loads 500 price histories the first time (a few minutes through your broker, which limits history requests); they are saved, so the next test is quick. **Stop** tests on what has loaded so far.

Signals: 12-month momentum (leaving out the last month), 6- and 3-month momentum, last week's losers, near the 52-week high, low volatility, above the 200-day average, and low RSI.

How it works: every holding period, all stocks are ranked by the signal using only prices up to that day, and we look at what each did over the next period.

| Result | Meaning |
|---|---|
| **Verdict** | *Works*, *Weak*, *Does not work* or *Works in reverse* (the stocks ranked first did worse). |
| **Rank match** | How well the ranking matched what happened next: −1 exactly backwards, 0 no link, +1 perfect. A steady 0.03 or more is already useful. |
| **Right more often than not?** | Share of dates on which the ranking pointed the right way. |
| **Steadiness** | Above 2, the result is unlikely to be chance. |
| **Groups** | The stocks split into 5 groups by signal, with each group's next return per year. A working signal climbs from the lowest group to the highest. |
| **Top group against all stocks** | ₹1 in the top group, rebalanced each period with costs, against ₹1 spread over every stock. |
| **Ranked first today** | The stocks the signal picks on the latest date. |

**Caution:** the stock list is today's index. Stocks that dropped out of the index in the past (often after falling) are missing, which makes every signal look a little better than it was. See *What a backtest leaves out*.

### Options buying backtest (simulated)

**Backtest → What to test → Buy index options.** Buys index options on a strategy's signals: calls when it says buy, puts when it says sell, or both.

![Options backtest](img/31-options-backtest.png)

Settings: index, dates, candle, signal strategy, **Buy** (calls / puts / both), **Strike** (ITM 1, ATM, OTM 1 or 2), lots, minimum days to expiry (rolls to the next expiry if closer), expiry weekday, stoploss and target as % of premium, **Volatility × VIX**, slippage and capital.

**It is simulated, not replayed.** Kite keeps no candles for expired option contracts, so each premium is priced with Black-Scholes from the index level and India VIX (× your multiplier), with time decay to expiry. Real premiums differ: skew (OTM puts are dearer), sudden IV crush after events, wide spreads in far strikes. Positions left at expiry settle at intrinsic value. Entries the capital cannot afford are skipped and counted in the note. Use it to compare ideas, not to forecast profit. For real premiums, paper trade from the option chain.

### Index futures backtest

**Backtest → What to test → Index futures.** Trades index futures in lots on any strategy's signals.

![Index futures backtest](img/39-futures-backtest.png)

- **Direction**: *Buy only*; *Sell only* (sell while the strategy is out, buy back when it says buy); *Buy and sell* (always in: bought on the signal, sold otherwise). Nothing is shorted before the strategy's first signal.
- **Lots**, **Margin %** (SPAN + exposure for index futures is about 11-14% of the contract value; an entry the equity cannot margin is skipped and counted), **Stoploss / Target %** of the futures price (checked against each candle's high and low; if both are inside one candle the stop is assumed first), **Slippage %**, **Cost of carry % a year**.
- **Prices.** With Kite logged in, day candles are Kite's continuous futures. At every monthly expiry the series jumps to the next contract. That jump is not profit, so the desk takes out an estimate of the basis (cost of carry × days to the next expiry) and charges a round trip as if you rolled. Intraday runs use the live contract's own candles when they reach back far enough, otherwise the index as a proxy. Without Kite the index from Yahoo stands in, and the note says so.
- **Charges**: Zerodha futures schedule (0.03% or ₹20 brokerage, 0.05% STT on sells from 1 April 2026 (0.02% before), exchange fees, stamp duty, GST).
- Returns are on your capital. Futures are leveraged: one NIFTY lot controls about ₹16 lakh, so a 1% move is about ₹16,000. Buy & hold is the index without leverage.
- Monthly expiry is the last Thursday until August 2025 and the last Tuesday from September 2025 (NSE), moved earlier for holidays you have listed.

### Option strategies: straddles, condors, spreads, hedges

**Backtest → What to test → Option strategy.** 34 multi-leg index option strategies. For each one you can do two things:

- **Payoff now**: work the strategy out on today's market (strikes, premiums, profit and loss at expiry, breakevens, chance of profit, Greeks, margin) before you trade it.
- **Run**: backtest it on the index's real candles, with real option prices where the desk has them ([Real option prices](#real-option-prices-nse-history)) and modelled ones elsewhere.

![Option strategies backtest](img/40-option-strategies.png)

#### Step by step

1. **Strategy**: pick one. The list is grouped as *Neutral: sell premium*, *Big move either way: buy premium*, *Bullish* and *Bearish*. The line under it says what it does and shows the **Markets** badges.
2. **Index**: NIFTY, BANKNIFTY, FINNIFTY, MIDCPNIFTY, NIFTYNXT50, SENSEX, BANKEX or SENSEX50.
3. **Expiry**: *Weekly* only for NIFTY (Tuesday) and SENSEX (Thursday). Every other index has had only monthly options since November 2024. Choose *Monthly* for them.
4. **Strikes**. Every strike is counted in strikes from the at-the-money (ATM) strike, the strike nearest the index:
   - **Strikes away from ATM (sold legs) (k)**: how far the sold legs of a strangle, condor, guts, jade lizard, diagonal or collar sit from ATM.
   - **Hedge strikes further out (bought legs) (w)**: how far the hedge legs sit beyond them, or the gap between the legs of a spread, butterfly or ratio spread.
   - One strike is 50 points on NIFTY and FINNIFTY, 25 on MIDCPNIFTY, and 100 on BANKNIFTY, NIFTYNXT50, SENSEX and BANKEX. SENSEX50 uses the gap between the strikes in Kite's contract list. So on NIFTY, k = 2 and w = 4 put the sold legs 100 points out and the wings 200 points beyond them.
   - A box the strategy does not use is hidden.
5. **Lots**: the quantity is lots × the exchange lot size (from Kite's contract list, or NIFTY 65, BANKNIFTY 30, FINNIFTY 60, MIDCPNIFTY 120, NIFTYNXT50 25, SENSEX 20, BANKEX 30; SENSEX50 only from Kite's list). A "2×" leg trades twice the lots.
6. Open **Payoff now (today's market)** to see it on today's market, or press **Run** to backtest it. Margin %, Volatility × VIX, put skew and slippage are under **Advanced settings**.

#### Payoff now

![Payoff of an iron condor](img/43-option-payoff.png)

Leave **Spot**, **IV %** and **Days to expiry** empty to use the latest index price (Kite, or the last close), India VIX × the *Volatility × VIX* factor, and the next expiry. Type them in to test "what if": the index 2% higher, IV at 20%, two days left. Calendars and diagonals also show **Days to far expiry**.

The model prices every leg with Black-Scholes at one volatility. For figures on real prices, open **Option chain**, copy each leg's LTP into the **Premium** boxes in the Legs table and press **Recalculate with these premiums**. Spot, IV and days stay as they were.

| Figure | Meaning |
|---|---|
| **Net credit / Net debit** | Premium received (credit) or paid (debit) for the whole position, and per unit. |
| **Max profit / Max loss** | The best and worst result at the near expiry, in rupees. **Unlimited** means it keeps growing as the index moves further. A fall stops at zero, so a loss on the downside is always a number, though it can be very large. |
| **On the chart** | The index level of each figure is written on the chart: **Max profit** and **Max loss** with the index range where they happen (for example *23,900 to 24,100*, or *23,700 or lower*), each **Breakeven** (no profit, no loss) with its distance from spot, and **Spot** under the axis. A thick band on the line marks the range. **Unlimited** sides are labelled at the chart's edge instead of a number. |
| **Expected move** | The index range one standard deviation either side of spot by the near expiry, at the IV in the title: about a 68% chance the index finishes inside. Shaded purple on the chart. Breakevens inside the band are likely to be tested; a seller wants them outside it. |
| **Breakevens** | Index levels where the expiry P&L is zero, and how far they are from spot. |
| **Chance of profit** | The chance the index ends in a profit zone at the near expiry, at this IV (lognormal). It ignores skew and fat tails. A high chance of profit usually comes with a large maximum loss. |
| **Margin (estimate)** | Covered legs: the worst loss at expiry from the strikes. Uncovered short options and futures: **Margin %** of the contract value. Bought options are paid in full, less the premium of the sold ones (not for calendars and diagonals). Check Zerodha's margin calculator before trading. |
| **Delta** | Rupees gained (+) or lost (−) for a 1-point rise in the index. |
| **Theta** | Rupees earned (+) or lost (−) per calendar day if nothing else changes. |
| **Vega** | Rupees gained or lost for a 1-point rise in IV (14% to 15%). |
| **Gamma** | How much delta changes for a 1-point move. A large negative gamma means losses grow faster as the index moves. |

**The chart.** The blue line is the P&L at the near expiry, shaded green where it is a profit and red where it is a loss. The dashed orange line is the P&L **today** if the index jumped to that level now. The yellow line is spot, and the circles are the breakevens. Hover to read any level.

#### Run (backtest)

| Run | How it trades |
|---|---|
| **Intraday** | On the ticked weekdays, open at the **entry time** on 1, 2, 3, 4, 5, 15 or 30-minute candles and close at the **exit time** (or at expiry on expiry day). |
| **Positional** | Day candles: open on the ticked weekdays at the open, close **N days before expiry** or settle at expiry (intrinsic value). |
| **On a strategy's signal** | For directional strategies only (spreads, back spreads, diagonals, protective put, covered call, collar, synthetics). Bullish ones open while the chosen strategy is long and bearish ones while it is out. Rolled at expiry. |

**Exit rules for each leg** uses the [exit rules box](#exit-rules-stoploss-target-trailing-re-entry) on each leg's premium. **Apply to** chooses *Sold legs*, *Bought legs* or *Every leg*.

- On a **sold leg** it works the other way round: the stoploss sits above the premium you sold at (a rising premium is your loss), and a falling premium is your profit. Example: sell at ₹100 with a 30% stoploss; the leg is bought back if its premium reaches ₹130. The other legs stay on (the common "straddle with stoploss").
- **Re-enter** sells that leg again, up to 5 times: when its premium comes back to the price it was first sold at, or straight away at the strike the entry rule picks then.
- A **bought leg** that covers a sold leg is never closed alone. If its exit rule fires while a sold leg is still open, the whole position is closed, so you are never left with an uncovered sold option.

**Exit rules for the whole position**: **Stoploss** and **Target** as a % of the premium received (or paid) for the whole position. **Put skew** raises the volatility of lower strikes (puts below the money are dearer in real markets; 0 = none). A calendar or diagonal sells its later-expiry leg back at the model price when the near expiry settles.

**Modelled premiums are a simulation.** Kite keeps no candles for expired options, so without stored history every premium is computed (Black-Scholes) from the index's real candles and India VIX. Real option sellers face things the model does not: volatility spikes and crush around events, skew, wide spreads in far strikes, and expiry-day gamma. Selling options wins often and loses big. The worst day matters more than the win rate, so look at **Max drawdown** first.

#### Real option prices (NSE history)

The backtest can use the prices options really traded at instead of modelled ones. Open **Real option prices (NSE history)** above the form.

**NSE history (day candles).** NSE publishes one price file a trading day (the F&O "bhavcopy") with every contract's open, high, low, close, settlement price, open interest and contracts traded. **Download from NSE** fetches them for the dates and indices you tick (NIFTY, BANKNIFTY, FINNIFTY by default; MIDCPNIFTY and NIFTYNXT50 too). It keeps index options within 15% of the index and up to 100 days to expiry, which is about 70 MB a year for the three default indices.

- About one file a second: 5 years is about 1,250 files and 40 minutes. You can keep using the desk meanwhile.
- Days already stored are skipped, so **Stop** and download again later to continue. Days that failed are tried again.
- A missing file on a past weekday is an exchange holiday. NSE refusing ("403") is an error, never stored as prices; after 5 refusals in a row the download stops and says so.
- NSE changed the file format on 8 July 2024; both formats are read.

With history stored, a **positional** or **signal** backtest opens each leg at the option's real **open** price that day, values it at its real **close**, checks stoplosses and targets against the option's own **low and high**, and uses the **expiries NSE actually listed** (holiday-shifted ones included). A strike that did not trade that day uses NSE's settlement price; a strike or day not stored is modelled. The note under the result counts how many fills came from each.

**Live chain (intraday).** NSE's file has one price a day, so an intraday entry at 09:20 cannot come from it. Instead the desk records the live chain from your broker while the market is open and the broker is connected: the near two expiries of each index in **Record the live chain of** (NIFTY by default), 20 strikes either side of the money, every 5 minutes (one quote request per index each time, well inside broker limits). Intraday backtests use these prices, at most 10 minutes old, from the day recording began; the move within a candle is the model's, scaled to the recorded price. Leave the box empty to stop recording.

**Premiums** in Advanced settings chooses *Real prices where stored* (default) or *Modelled only*, for comparing the two.

Things to know:

- NIFTY had only monthly options before February 2019, FINNIFTY options began in January 2021, and BANKNIFTY and FINNIFTY have had only monthly expiries since November 2024. With history stored, *Weekly* in those periods takes the nearest expiry NSE listed.
- A bhavcopy close is the last trade's price, not where you could have sold at 15:30. Keep slippage on, more so for far strikes.
- Everything is in one file, `option_history.db`, in the desk's data folder. To use history downloaded on your computer on a server, copy that file into the server's data folder (the desk's volume) while the desk is stopped.

**Downloading on a server.** NSE refuses many cloud and data-centre addresses. Set `DESK_EXCHANGE_PROXY` (see *Exchange data on a server* under [Market tools](#market-tools) and DEPLOY.md) and the download goes through it. From a terminal on your own computer (or inside the server's container) you can also run:

```
python -m desk.chainhistory import --from 2016-01-01 --underlyings NIFTY,BANKNIFTY,FINNIFTY
python -m desk.chainhistory status
```


#### Every strategy: legs, payoff and when to use it

In the tables, **K** is the ATM strike, **W** is the wing width in points (w × strike step), **C** is the net credit per unit and **D** is the net debit per unit. Breakevens are at the near expiry. Multiply per-unit figures by the quantity for rupees. **Payoff now** works all of this out for you.

**Neutral: sell premium.** These earn when the index stays in a range and IV falls. Theta is positive and vega negative.

| Strategy | Legs (k, w in strikes) | Max profit | Max loss | Breakevens | Use it when |
|---|---|---|---|---|---|
| **Short straddle** | Sell ATM CE + sell ATM PE | C | Unlimited | K − C, K + C | You expect the index to pin near the strike, for example on a quiet expiry week. Most decay, least room. |
| **Short straddle with wings** | Short straddle + buy CE at +w and PE at −w (default w = 9) | C | W − C | K − C, K + C | The same view with the tail capped and far less margin. |
| **Short strangle** | Sell CE at +k, sell PE at −k | C | Unlimited | PE strike − C, CE strike + C | A range-bound market with room to move. Smaller credit, wider range. |
| **Short guts** | Sell **in-the-money** CE at −k + ITM PE at +k (k ≥ 1) | C − gap between the strikes | Unlimited | PE strike − C, CE strike + C (the PE strike is the higher one) | The same as a strangle, with a larger credit. Early exercise does not apply (index options are European), but ITM legs are less liquid. |
| **Iron condor** | Short strangle + buy CE at +k+w, PE at −k−w | C | W − C | PE strike − C, CE strike + C | Range-bound, defined risk, low margin. The standard weekly-income trade. |
| **Iron butterfly** | Short straddle + buy CE at +w, PE at −w | C | W − C | K − C, K + C | A pin at the strike with defined risk. Larger credit than a condor, narrower zone. |
| **Jade lizard** | Sell PE at −k, sell CE at +k, buy CE at +k+w | C | Downside: put strike − C (large); upside: W − C (none if C ≥ W) | Put strike − C (and a second one above if C < W) | Neutral to mildly bullish. Choose k and w so the credit is at least the call-spread width, which removes the upside risk. |
| **Long call butterfly** | Buy CE at −w, sell 2 × CE at K, buy CE at +w | W − D | D | K − W + D, K + W − D | You expect the index at K on expiry. Small debit, about 1:3 to 1:5 payoff. |
| **Long put butterfly** | Buy PE at +w, sell 2 × PE at K, buy PE at −w | W − D | D | K − W + D, K + W − D | The same as the call butterfly, built with puts. |
| **Long call calendar** | Sell near ATM CE, buy next-expiry CE at K | At K on the near expiry (the far call's time value − D) | About D | Two, around K (shown by Payoff now) | Quiet near-term, with IV likely to rise. A fall in IV hurts. |
| **Long put calendar** | Sell near ATM PE, buy next-expiry PE at K | Same as the call calendar | About D | Two, around K | Same as the call calendar. |
| **Call ratio spread (1×2)** | Buy ATM CE, sell 2 × CE at +w | W + C (or W − D) at the short strike | **Unlimited above** | K + 2W + C; also K + D below if it costs a debit | Neutral to mildly bullish, expecting a drift up to the short strike but no rally. |
| **Put ratio spread (1×2)** | Buy ATM PE, sell 2 × PE at −w | W + C at the short strike | Large below (to zero) | K − 2W − C; also K − D above if it costs a debit | Neutral to mildly bearish, no crash expected. |

**Big move either way: buy premium.** These earn when the index moves far or IV rises. Theta is negative and vega positive.

| Strategy | Legs | Max profit | Max loss | Breakevens | Use it when |
|---|---|---|---|---|---|
| **Long straddle** | Buy ATM CE + ATM PE | Unlimited | D | K − D, K + D | Before results, the Budget, RBI policy or elections, when you expect a big move but not its direction. Buy before IV rises, not after. |
| **Long strangle** | Buy CE at +k, PE at −k | Unlimited | D | PE strike − D, CE strike + D | Cheaper than a straddle; needs a bigger move. |
| **Long guts** | Buy ITM CE at −k + ITM PE at +k (k ≥ 1) | Unlimited | D − gap between the strikes | PE strike − D, CE strike + D (the PE strike is the higher one) | Like a strangle, with in-the-money legs that hold their value better. |
| **Strap (2 calls + 1 put)** | Buy 2 × ATM CE + 1 × ATM PE | Unlimited | D | K − D, K + D/2 | A big move expected, more likely up. |
| **Strip (1 call + 2 puts)** | Buy 1 × ATM CE + 2 × ATM PE | Unlimited (large down) | D | K − D/2, K + D | A big move expected, more likely down. |
| **Reverse iron condor** | Buy CE at +k, PE at −k; sell CE at +k+w, PE at −k−w | W − D | D | Bought PE strike − D, bought CE strike + D | A breakout from a range, with capped cost and capped profit. |
| **Reverse iron butterfly** | Buy ATM CE + PE; sell CE at +w, PE at −w | W − D | D | K − D, K + D | A cheaper long straddle with profit capped at the wings. |
| **Short call butterfly** | Sell CE at −w, buy 2 × CE at K, sell CE at +w | C | W − C | K − W + C, K + W − C | A move past either outer strike. Small credit, loses most at K. |

**Bullish.**

| Strategy | Legs | Max profit | Max loss | Breakeven | Use it when |
|---|---|---|---|---|---|
| **Bull call spread** | Buy ATM CE, sell CE at +w | W − D | D | K + D | A moderate rise. Cheaper than a call alone. |
| **Bull put spread (credit)** | Sell ATM PE, buy PE at −w | C | W − C | K − C | You expect the index to hold up. Time decay works for you. |
| **Call ratio back spread (1×2)** | Sell ATM CE, buy 2 × CE at +w | Unlimited | W − C (or W + D) at the upper strike | K + W + (W − C) above; K + C below if a credit | A strong rally expected. A small credit or loss on a fall; the worst result is a slow rise to the upper strike. |
| **Call diagonal** | Buy next-expiry ATM CE, sell near CE at +k | Near the short strike on the near expiry | About D | Two (shown by Payoff now) | A slow rise. Sell a new near call each expiry against the long one. |
| **Protective put** | Long futures + buy PE at −k | Unlimited | (Futures − put strike) + put premium | Futures price + put premium | Long the index but insured against a crash. |
| **Covered call** | Long futures + sell CE at +k | Call strike − futures + C | Large (futures to zero) − C | Futures − C | Long the index, flat to slowly rising market: the call adds income. |
| **Collar** | Long futures + buy PE at −k + sell CE at +k | Call strike − futures + net credit | Futures − put strike − net credit | Futures − net credit | Long the index with low-cost insurance; the call pays for the put. |
| **Synthetic long futures** | Buy ATM CE, sell ATM PE | Unlimited | Large (to zero) | K + D (or K − C) | Futures-like exposure from options. Margin is like futures. |

**Bearish.**

| Strategy | Legs | Max profit | Max loss | Breakeven | Use it when |
|---|---|---|---|---|---|
| **Bear put spread** | Buy ATM PE, sell PE at −w | W − D | D | K − D | A moderate fall. |
| **Bear call spread (credit)** | Sell ATM CE, buy CE at +w | C | W − C | K + C | You expect the index to stay down or flat. |
| **Put ratio back spread (1×2)** | Sell ATM PE, buy 2 × PE at −w | Large on a crash | W − C (or W + D) at the lower strike | K − W − (W − C) below; K − C above if a credit | A sharp fall expected. The worst result is a slow fall to the lower strike. |
| **Put diagonal** | Buy next-expiry ATM PE, sell near PE at −k | Near the short strike on the near expiry | About D | Two (shown by Payoff now) | A slow fall. Roll the near put each expiry. |
| **Synthetic short futures** | Sell ATM CE, buy ATM PE | Large (to zero) | Unlimited | K + C (or K − D) | Futures-like short exposure. |

#### Rules for Indian index options

- **Lot sizes change.** NSE revises them twice a year. The desk reads them from Kite's contract list when you are logged in; check the Qty column before you place an order.
- **Expiry day.** Only NIFTY (Tuesday) and SENSEX (Thursday) have weekly expiries. Under SEBI's F&O rules (in force since late 2024 and early 2025), short options carry an extra 2% exposure margin on expiry day, and positions spread across expiries lose their margin benefit that day. Expect a calendar or diagonal to need more margin on the near expiry.
- **Settlement.** Index options are European and cash-settled at the closing price. An in-the-money option left to expire pays STT on its intrinsic value, so close in-the-money legs before 15:30 unless you have checked that cost.
- **Hedge first.** When you enter a spread, condor or butterfly, place the **buy** legs first and then the sells. The margin benefit applies only once the hedge is in, and a sell-first order can be rejected or blocked at the full naked margin.
- **Never leave a naked short unattended.** Short straddles, strangles, guts, ratio spreads and synthetics have unlimited or very large risk. Use the leg stoploss, the whole-position stop, or wings.
- **Liquidity.** Far strikes and non-NIFTY monthly series often have wide bid–ask spreads. Raise **Slippage %** in the backtest for them, and use limit orders.

### Portfolios: Ray Dalio's All Weather

**Backtest → What to test → ETF portfolios.** Fixed-weight mixes of Indian ETFs, put back to their weights on a schedule.

![All Weather portfolio backtest](img/41-all-weather.png)

| Portfolio | Weights | Data from |
|---|---|---|
| **Ray Dalio: All Weather** | 30% NIFTYBEES, 40% LTGILTBEES (long gilt), 15% GILT5YBEES, 7.5% GOLDBEES, 7.5% SILVERBEES | 2022 |
| **Ray Dalio: All Weather, longer history** | 30% NIFTYBEES, 55% LTGILTBEES, 15% GOLDBEES | 2018 |
| **Harry Browne: Permanent Portfolio** | 25% each NIFTYBEES, LTGILTBEES, GOLDBEES, cash | 2018 |
| 60/40 Nifty and gold | 60% NIFTYBEES, 40% GOLDBEES | 2009 |

Change any weight, add any NSE ETF or stock, or type `CASH`; weights must add up to 100%. **Rebalance** monthly, quarterly, yearly or never. Cash earns the rate you set, because the liquid ETF pays its return as dividends that its price does not show. The comparison line is the Nifty (NIFTYBEES) alone.

The point of All Weather is a smaller worst fall, not a higher return. Compare the **Max drawdown** and **Sharpe** columns, not only the return.

Yahoo printed NIFTYBEES and GOLDBEES at 1/10 and 1/100 of their price on 19-20 December 2019. The desk detects such one- or two-day errors (a whole-number drop that comes back) and corrects them everywhere, and the note lists what it corrected.

### What a backtest leaves out

It assumes every order fills at the next candle's open, with a small slippage. It ignores days a stock is locked at its circuit limit, how thin trading can be in small stocks, tax on your gains, and stocks that were delisted. Real results will be lower.

**Survivorship.** The Nifty and sector lists are today's members. Companies that fell out of an index, or were delisted, are missing from tests and screens run on them, and those were mostly the losers. A test across today's index members therefore looks better than trading through those years would have been. Test single stocks over periods you can check, and treat results across a list with extra caution.

### AI agents backtest

The **AI agents** button tests the AI's ratings over past dates. It is slow, uses your LLM credits, and the model may already know what happened after a past date. Use it as a rough check only.

---

## 7. Scanner: breakouts with GTT orders

The scanner looks through a list of stocks for a specific setup and can place the orders for it.

### Legend screens: Buffett, Graham, Lynch, Greenblatt, Kedia

At the top of **Scanner**. Each famous investor's published numbers, run on an index's stocks with today's fundamentals.

![Legend screens](img/42-legend-screens.png)

| Screen | Rules | Default stocks |
|---|---|---|
| **Warren Buffett**: quality at a sensible price | ROE ≥ 15%, debt/equity ≤ 0.5, net margin ≥ 10%, P/E ≤ 35, ₹10,000 Cr+; ranked by ROE | Nifty 200 |
| **Benjamin Graham**: defensive value | P/E ≤ 15, P/B ≤ 1.5, P/E × P/B ≤ 22.5, debt ≤ equity, pays a dividend; cheapest first | Nifty 500 |
| **Peter Lynch**: growth at a reasonable price | PEG ≤ 1, profit growth 15-50%, debt/equity ≤ 0.8; lowest PEG first | Nifty 500 |
| **Joel Greenblatt**: Magic Formula | Rank by earnings yield + rank by return on capital (ROA stands in); banks, finance and utilities out; ₹5,000 Cr+ | Nifty 500 |
| **Vijay Kedia**: SMILE | ₹500-20,000 Cr, sales +15%, profit +20%, ROE ≥ 15%, debt ≤ equity, promoters ≥ 50% | Nifty Smallcap 250 |

- Choose the screen and the stocks (Nifty 50 up to Total Market), press **Run screen**. Click a stock for its full details.
- Figures come from Yahoo (Kite has none) and load in the background at about 2 stocks a second. A first run on Nifty 500 says how many are still loading; run it again a few minutes later.
- These are **screens, not backtests**. Only today's figures are available, and testing them on old prices would use knowledge of the future. Each screen leaves out what numbers cannot judge (moat, management, the size of the opportunity) and says so. Use the list to start research, not as a buy list.

![Scanner](img/12-scanner.png)

### The setup

1. **Breakout candle** (daily): volume at least **3×** the 20-day average, and a close above the highest high of the previous 20 days.
2. **Still holding**: every close since stays within **1%** of that candle's high.
3. **Entry**: a buy GTT at the **low of the breakout candle**, waiting for a pullback.
4. **Protection**: once that buy triggers, a stoploss GTT **5%** below (and a target, if you set one).

All six numbers can be changed at the top.

### Use it

1. Open **Scanner**. Adjust the numbers if you wish.
2. Open **Stocks to scan** to edit the list (large caps from the Nifty 100 by default; any NSE symbols, separated by spaces).
3. Press **Save and scan**. The first scan downloads each stock's history and takes a few minutes; later scans are quick.
4. **Signals** lists the stocks that match, newest breakout first, with the buy trigger and stoploss prices.
5. Press **Place buy GTTs**. In Paper mode they are placed at once. In Live mode you must type `PLACE LIVE GTT`.
6. Tick **Run live** so the desk follows the orders: when a buy triggers it places the stoploss.

On most days few or no stocks match. That is normal.

### It never repeats an order

The scanner remembers each setup by stock and breakout date. Scan again tomorrow and the same breakout shows *buy GTT waiting*, and **Place buy GTTs** skips it. This is what stops an automatic system from buying the same thing every day.

### Following the orders

| Status | Meaning |
|---|---|
| **buy GTT waiting** | The price has not come down to the trigger yet. |
| **bought, stoploss placed** | The buy triggered and the exit GTT is in place. |
| **bought, NO stoploss** | The exit GTT was refused or cancelled. Act on this yourself. |
| **closed** | The stoploss or target triggered. |
| **cancelled** | You cancelled it. Shares already bought stay with you. |

**Cancel** removes a setup's open GTTs. **Remove** clears a finished line.

The quantity of each order comes from Settings: *Buy size %* of your capital, capped by *Max order value*.

---

## 8. Algos: automatic trading

An algo runs one strategy on one stock by itself.

![New algo form](img/16-algo-form.png)

### Three kinds

Choose the **Candle** and the **Trade type**. Day candles are always positional.

| | Positional, day candles | Positional, minute or hourly candles | Intraday |
|---|---|---|---|
| **Candle** | 1 day | 1 minute to 1 hour | 1 minute to 1 hour |
| **Product** | Delivery (CNC) | Delivery (CNC) | Intraday (MIS) |
| **Reads the market** | Once, after the close (15:50) | After every finished candle, in market hours | After every finished candle |
| **Sends its order** | When your trade window opens next morning | At once, in market hours (inside the Settings trade window only when Settings enforces it) | At once, inside the trade window |
| **Target and stoploss** | Placed as a GTT after the buy fills | Watched on the live price every 20 seconds **and** placed as a GTT | Watched on the live price every 20 seconds |
| **End of day** | Keeps the position | Keeps the position | Sells everything at the square-off time |
| **Daily loss limit hit** | Keeps the position | Keeps the position | Sells |

**Why intraday uses MIS and positional uses CNC.** Brokers square off every MIS position themselves around 15:20, so a swing trade must be delivery (CNC). A positional buy needs the full price in cash (no intraday leverage).

**Positional overnight.** The GTT sits at your broker, so the stoploss and target still work overnight, when the app is off or restarting, and in the morning before you log in to your broker. The algo keeps its position, entry day and GTT in its saved file, so a restart carries on where it was. A trailing stoploss moves the GTT up once a candle (day candles: once a day). A stoploss the price gaps past at the open is filled by the GTT near the open price, which can be well below the stoploss. **Advanced → Sell after holding this many trading days** sells once that many trading days have passed since the buy (day candles: the sell is queued for that day's open).

### Create one

1. Open **Algos**.
2. Enter a **Name** and the **Symbol**.
3. Choose the **Strategy** (built-in or one of yours), the **Candle** size and the **Trade type** (Intraday or Positional).
4. Set the **Budget** in rupees and the **Exit rules** ([the same box as the backtest](#exit-rules-stoploss-target-trailing-re-entry): stoploss and target in % or points, trailing stoploss, stoploss to the entry price, re-entry).

The quickest way: backtest first, then press **Create algo** under the result.

How exit rules work in an algo: an algo on minute or hourly candles checks them against the live price every 20 seconds, and a re-entry buys again when the live price comes back to the entry price (or at once); a positional one also places them as a GTT. A **daily** algo places its stoploss and target as a GTT, moves the stoploss up once a day after the close, and re-enters when a day's high reaches the entry price again. The stoploss never moves down.
5. Choose **Paper** or **Live**.
6. Press **Create algo**. It is created stopped.

### Run it

![Running algos](img/17-algos.png)

| Button | What it does |
|---|---|
| **Details** (or click the name) | Each stock or leg the algo traded: buy price, sell price, realised P&L (booked, after charges), unrealised P&L (at the current price), whether it is **Active** or **Closed**, the orders still out, and every fill. |
| **Edit** | Changes the algo in the form above (**Save changes**). Shown unless the algo is running live: stop a live algo to edit it. The name cannot change, because its profit/loss history is filed under it. While the algo holds a position, only the budget, the exit rules and the holding limit can change (for an F&O algo: maximum loss, target, each leg's exit rules and the limit buffer); a live daily algo whose target and stoploss sit at the broker as a GTT can change only its budget. Changing Paper/Live stops the algo, so it goes live only through Start and the typed phrase. |
| **Start** | Starts it. A live algo asks you to type `ARM LIVE <NAME>`. |
| **Stop** | Stops it. It does not sell what it holds. |
| **Run now** | Checks the market immediately instead of waiting. |
| **Clear position** | Marks a stopped algo as holding nothing, after you dealt with the shares yourself. Sends no order. |
| **Delete** | Removes a stopped algo that holds nothing. |

Options & Futures algos have the same buttons, plus **Exit** (close every leg now, then stop). Their arming phrase is `ARM LIVE FNO <NAME>`.

Open **Activity** to see everything the algo did and why.

### Statuses you will see

| Status | Meaning |
|---|---|
| **Running: flat / holding N** | Working normally. |
| **Running: BUY queued for 09:30** | A daily signal came after the close; the order goes when the window opens. |
| **Running: waiting for a fresh entry** | The strategy was already in a trend when you started. The algo waits for a new signal instead of jumping in late. |
| **Running: waiting for the signal to reset** | It was stopped out or hit its target. It will not buy again until the strategy gives a new signal. |
| **Running: waiting to re-enter** | It hit a plain stoploss and has re-entries left. It buys again as the exit rules say. |
| **Running: order sent** | A live order is with Zerodha, not yet filled. |
| **Running: done for the day** | Past the square-off time (intraday). |
| **Running: market closed, holding N** | A positional algo waiting overnight; its GTT guards the position. |
| **Blocked: Kite not connected / needs a login** | Sign in to Kite on the Trade tab. |
| **Blocked: candles are delayed** | The newest candle is too old to trust. The algo refuses to trade on it. |
| **Blocked: order refused** | A risk limit or the broker refused the order. Activity shows the reason. |
| **Blocked: kill switch is on** | No new buys. An algo holding a position still exits on its signal, stop or target. Turn the switch off in Settings to resume. |

### Rules every algo follows

- Your risk limits, trade window and kill switch apply to every algo order.
- A live order counts as a position only after Zerodha reports it filled.
- An intraday limit order still unfilled after 3 minutes is cancelled.
- Deleting or stopping an algo never sells its shares.

### Live intraday algos need live candles

Yahoo's intraday candles arrive late. A live intraday algo uses Kite's candles when your Kite Connect key includes historical data; otherwise it reports *Blocked: candles are delayed* rather than trading on old prices. Paper intraday algos have the same safeguard.

### Supervisor

**Algos → Supervisor** watches your running algos and steps in only to cut risk. Your algos still place every order. The supervisor can do four things and nothing else:

| Action | What happens |
|---|---|
| **Hold** | The algo takes no new buy or entry for the rest of the day. Its stoploss, target and square-off keep working. **Allow buys** (stocks) or **Allow entries** (F&O) on the algo lifts it. |
| **Close** | The algo sells what it holds (F&O: closes every leg) and stops. Works with the kill switch on. |
| **Kill switch** | Turns the desk's kill switch on: no new order anywhere, exits still work. You switch it off yourself. |
| **Suggestion** | A note and an alert. Nothing is done. |

It never opens a trade, starts an algo, changes its size or settings, goes live, or switches the kill switch off.

**Modes.** *Off*. *Watch*: every action becomes an alert and a suggestion. *Protect*: actions are carried out, on paper algos; on live algos only when you tick **act on live algos too**.

**Fixed rules** (every minute, no AI):

- A stock algo's position this far below its buy price is closed (default 10%; 0 switches it off). A backstop for algos run without a stoploss.
- When today's loss on an account passes 80% of the daily loss limit, every algo on that account is held for the day.
- An F&O algo past 80% of its maximum loss is flagged (it closes itself at the maximum).
- Live algos running with no daily loss limit are flagged.

**AI review** (optional). In market hours, every few minutes (15 by default), the algos' state goes to the AI service in Settings → AI, which answers with actions from the list above. Its answer is checked: only those four actions, only algos that are running, a reason for each; anything else is refused and logged. The AI acts at most **Most AI actions a day** times (3 by default); after that its answers become suggestions. Treat it as a second opinion: a language model can be wrong, and its trading judgement cannot be backtested honestly, so run it on paper first. Each review is one request to your AI service.

Every step, carried out or not, is listed under **What it did** with the reason. **Check now** runs the fixed rules at once; **Ask the AI now** runs one AI review.

![Supervisor](img/supervisor.png)

The stock algo list also has **Exit**: sell what the algo holds now and stop it.

### Options & Futures algos

**Algos → Options & Futures** runs an option strategy (any option strategy from the Backtest screen) or an index future by itself, in **Paper** or **Live**. Both modes take the same path: every leg is a limit order through your risk limits, kill switch and trade window. Paper fills in the paper book at Kite's last price; Live waits for Zerodha to fill each order.

**What it trades**

| Trade | Runs | Opens | Closes |
|---|---|---|---|
| **Option strategy, intraday** | On the weekdays you tick | At the **Entry time** | At the **Exit time** the same day (MIS) |
| **Option strategy, positional** | On the weekdays you tick | At the **Entry time** | **Exit days before expiry**, at the exit time (NRML) |
| **Option strategy, on a signal** | Directional structures only | When a strategy on the index's day candles turns on (bearish structures: turns off) | When the signal changes, or at expiry (then it re-enters in the next expiry) |
| **Index futures** | Long, short, or long and short | On the signal from the index's day candles, read at the entry time | When the signal changes; rolls to the next month at the exit time on expiry day |

Strikes are counted from the at-the-money strike when the algo enters: **k** for the sold legs, **w** for the wings, exactly as in the backtest. Press **Preview entry now** to see the contracts, premiums, the order they would be sent in, and the margin (Zerodha's, or the desk's estimate) against your funds.

**How it enters and exits**

- **Hedge first.** Bought legs go first. Sold legs are sent only once every bought leg has filled, so the margin benefit is in place. On the way out, sold legs are bought back first and the hedges sold last.
- A live leg not filled within 2 minutes is cancelled and sent again at a fresh price. After 3 unfilled tries, or if Zerodha refuses a leg, the entry is given up and the legs already filled are closed.
- Exits never give up: a refused or unfilled closing order is tried again every tick, and the algo shows *Blocked: closing order refused* until it goes through.
- Orders larger than the exchange's freeze quantity are split, at most 10 slices.

**Risk**

| Setting | What it does |
|---|---|
| **Maximum loss (₹)** | Required for live. When the position's P&L, with charges, reaches minus this amount, every leg is closed and the algo takes no new entry that day. |
| **Target (₹)** | Closes everything at this profit. |
| **Exit rules for each leg** | The same box as the backtest, on each leg's premium, for *Sold legs*, *Bought legs* or *Every leg*. Example: sold at ₹100 with a 30% stoploss, the leg is bought back at ₹130 and the other legs stay. With re-entry, it is sold again when the premium comes back to ₹100. A bought hedge whose exit fires while a sold leg is open closes the whole position. When every leg has exited on its own rules, the algo is done for the day. |
| **Exit** button | The algo's own kill switch: closes every leg now and stops. Works with the global kill switch on. |
| **Kill switch** (Settings) | Blocks new entries; exits still go through. |
| **Daily loss limit** (Settings) | Now counts sold options and short futures too. When it trips, F&O algos close their positions. |
| **Allow naked F&O shorts** (Settings) | Off by default. Without it, a structure that sells an option no bought option covers (short straddle, strangle, guts, ratio spreads, jade lizard, synthetics) runs only in paper. |

**Margin.** Before each entry the algo asks Zerodha's margin calculator for its legs (SPAN plus exposure and the premium of bought options, with the hedge benefit) and compares it with the funds Kite reports, or, in paper, the paper cash not used by other paper F&O algos. An entry that does not fit is skipped for the day. **Start** shows the margin needed and the funds first, and warns when they fall short. Zerodha's figure needs a Kite login (in paper too) and the desk's own kite-mcp-server (`deploy/kite-mcp`), which adds the margin calculator; with any other Kite server the desk uses its estimate (the backtest's, plus SEBI's 2% on a sold leg on its expiry day) and says so. Zerodha's own check at order time decides.

**Margin calculator** (F&O tab). Add legs (an index, F&O stock or MCX metal, the expiry, Call, Put or Future, the strike, Buy or Sell, lots) and press **Work out margin** for Zerodha's margin: the total with the hedge benefit, what the legs would need as separate orders, and the benefit, against your funds (Kite's in live, paper cash in paper). **Breakdown by leg** shows SPAN, exposure and premium for each. The product (carry forward or intraday) is under **Advanced**. Your open positions are not counted. It needs a Kite login and the desk's kite-mcp-server.

**Before you go live**

- Option prices come only from Kite, so a **paper** F&O algo needs Kite connected too.
- **Static IP.** Since 1 April 2026, Zerodha accepts API orders only from a static IP registered on your Kite Connect app (developers.kite.trade → your app → **IP Whitelist**, up to two IPs, one change a week). Orders go out from the machine running kite-mcp-server, so register that machine's public IP, or run kite-mcp-server on a server with a static IP and point the desk's MCP settings at it. An order from another IP is refused, and the algo's Activity says so.
- **Rate.** Kite allows at most 10 API orders a second. The desk keeps live orders under 8 a second.
- **Daily login.** Kite signs you out every morning. Live F&O algos are included in the *Log in to Kite* alert.
- Futures orders are held to your **Max order value** by about 12% of the contract value (their margin). One NIFTY lot needs about ₹2 lakh, so raise the limit before trading futures.
- Every opening leg counts towards **Max orders per day**: a four-leg condor uses four.
- Close in-the-money legs before 15:30 on expiry day: every leg is closed at the exit time on its expiry day for this reason.

---

## 9. Trade: orders, GTT, Kite

![Trade section](img/15-trade.png)

### Connect Zerodha (once a day)

1. Open **Trade**. Press **Connect** if the Kite pill says *not connected*.
2. Press **Log in to Kite**, open the link, sign in at Zerodha.
3. Come back and press **Refresh**. Your funds, holdings, positions and today's orders appear.

A green line at the top of the panel, "Kite market data is working", confirms your API key is on the paid Kite Connect plan. A yellow line means Kite refused to serve candles: charts will not load and the desk falls back to delayed data elsewhere. The plan belongs to the API key the desk was installed with (`KITE_API_KEY` in the server settings), so make sure that key is the paid one.

Zerodha ends the session every morning, so repeat steps 2 and 3 each trading day.

### Place an order

![Order review](img/13-order-review.png)

1. Choose **Paper** or **Live** in the top bar.
2. Fill the ticket: symbol, **BUY** or **SELL**, quantity.
3. **Product**: *CNC* to hold the shares, *MIS* to buy and sell the same day.
4. **Type**: *LIMIT* (you set the price; recommended) or *MARKET*. *SL* and *SL-M* are stop orders for live trading.
5. **Get price** shows the last price.
6. Press **Review order**. The desk shows its checks, the order value and the charges.
7. Type the phrase shown, for example `BUY 10 INFY`, and press **Place**.

The confirmation works once and expires after two minutes.

### Size by risk

Decide the loss you accept before the quantity. On the ticket, fill **Stoploss ₹** (where the trade is wrong), optionally **Target ₹**, and **Risk % of capital** (from Settings), then press **Work out quantity**. The desk:

- sets the quantity so that hitting the stoploss loses that share of your capital, charges included, rounded down;
- caps it at your capital and your maximum order value, and says when it did;
- fills **Then stoploss %** and **Then target %** to match, for the GTT after a delivery buy;
- with a target, shows the gain against the risk and the win rate the trade needs to pay over time. A target twice as far as the stoploss pays if more than about a third of such trades reach it.

Example: capital ₹10,00,000, risk 1% (₹10,000), entry ₹1,000, stoploss ₹950. Each share risks ₹50, so 200 shares would risk ₹10,000; the charges take a few shares off, and the desk suggests about 195.

**Review order** shows the loss if the stoploss hits. Fill **Setup** and **Why this trade** too, and the plan goes to your trade journal when the order is placed.

Paper orders fill immediately at the last price. A real order may fill partly, later, or not at all; "sent" is not "filled". Check **Today's orders**.

### Target and stoploss on a buy

Fill **Then target %** and **Then stoploss %** on a delivery (CNC) buy. After the buy is placed, the desk prepares the matching GTT and shows it for your confirmation:

![GTT review](img/14-gtt-review.png)

### GTT orders

A GTT (Good Till Triggered) waits, for up to a year, until the price reaches your trigger. Then Zerodha sends a limit order.

| Type | Use it to |
|---|---|
| **Single trigger, BUY** | Buy if the price falls to (or rises to) a level. |
| **Single trigger, SELL** | A stoploss or a target on shares you hold. |
| **Target + stoploss (OCO)** | Both at once. When one triggers, the other is cancelled. |

To place one: fill the **GTT order** form, press **Review GTT**, type the phrase (for example `GTT SELL 10 INFY`), and place it. The list below shows every GTT with its status; **Cancel** removes an active one.

Know this about GTTs:

- A triggered GTT sends a **limit** order. In a sharp fall the price can jump past it and the order stays unfilled.
- GTTs are for delivery (CNC). A sell GTT needs the shares in your demat when it triggers.
- Zerodha rejects a GTT whose trigger is within 0.25% of the current price.
- A GTT can trigger at any time the market is open, including before your trade window.
- Paper GTTs are checked about every 20 seconds during market hours and fill in full.

### Paper account and journal

**Paper account** shows the pretend cash and holdings; **Reset** starts it again. **Order journal** lists every order and GTT the desk has sent, with who sent it (*manual*, *algo:…*, *scanner*) and the result.

---

## 10. Analyze: the AI agents

![Analyze](img/02-analyze.png)

A team of AI agents (technical, sentiment, news and fundamentals analysts, bull and bear researchers, a trader, a risk team and a portfolio manager) studies one stock and gives a rating: Buy, Overweight, Hold, Underweight or Sell.

1. Set up an AI model in Settings.
2. Open **Analyze**, enter a symbol and a date, press **Run analysis**.
3. Watch the activity log. A run takes several minutes.
4. Read the decision and the reports. A suggested order is shown; **Open in order ticket** copies it to the Trade section, where you still review and confirm it.

The rating is a language model's opinion. It can be wrong, and two runs can disagree. Use it as one input, never as an instruction.

---

## 11. Your daily routine

**Before 09:15**

1. Sign in to the desk.
2. **Trade → Log in to Kite → Refresh**. The Kite pill should say *trading*.
3. Glance at **Algos**: none should say *Blocked*.

**During the day**

- Daily algos send their queued orders at 09:30. Intraday algos trade from 09:30 to 15:00 and sell at 15:00.
- The scanner follows its GTTs if **Run live** is ticked.
- Check the **Order journal** and Kite's own order book once or twice.

**After 15:30**

- From 15:50 daily algos read the day's candle and queue tomorrow's orders.
- Run the **Scanner** for new breakouts and place the GTTs.

**If you will be away:** stop your live algos. They cannot trade without the morning Kite login anyway, and you will not be there to notice a problem.

---

## 12. Going live: checklist

1. Backtested the strategy on at least three stocks and two different periods.
2. Ran it as a **paper** algo for a few weeks and read its Activity log.
3. In the Kite Connect developer console, registered your server's fixed IP address (required by SEBI for orders sent through an API).
4. Kite pill shows **trading**, not *read-only*.
5. Set small **risk limits** in Settings: a low max order value and few orders a day.
6. Ticked **Allow live orders with real money**.
7. Started with one algo and a small budget.
8. Know where the **Kill switch** is.

---

## 13. When something goes wrong

| What you see | What to do |
|---|---|
| "New buys are taken only between 09:30 and 15:00" | You are outside your trade window. Wait, or change it in Settings. |
| "The market is closed" | NSE trades 09:15 to 15:30 on weekdays. Check the top bar: **Holiday** means the day is in your holiday list. |
| The kill switch turned itself on | The daily loss limit was reached. See the alert on the P&L screen. Turn the switch off in Settings when you are ready. |
| Telegram test says "chat not found" | Open your bot in Telegram and press Start, then test again. Check the chat id is your own numeric id. |
| The authenticator code is refused | Check your phone's clock is set automatically. Each code works once; wait for the next one. |
| "Kite needs a login" | Trade → Log in to Kite → Refresh. |
| "Charts use Kite's data …" | Connect and log in to Kite. If it still fails, your Kite Connect key may be on the free Personal plan, which has no candles; upgrade it, or switch on the Yahoo fallback in Settings. |
| AI Analyze fails with "Could not reach the AI service" | The Endpoint URL in Settings is wrong or not reachable from the server. Press **Test AI connection** and follow what it reports. |
| Sector cards say "Loading prices…" | The first load takes several minutes. A stock that stays unpriced has no data in the feed; remove it from the sector. |
| "The connected Kite server is read-only" | The desk is connected to Zerodha's hosted server, which cannot place orders. See DEPLOY.md for the self-hosted Kite server. |
| "Order value … is above your limit" | Lower the quantity or raise *Max order value*. |
| "A buy limit … would not fill now" (paper) | The paper account keeps no waiting orders. Use a price at or above the last price, or a GTT. |
| "no price history" / "no candles" | Check the symbol. Use the NSE symbol. Intraday data covers a limited period. |
| Algo says "Blocked: candles are delayed" | Normal with Yahoo data in a live market. Use daily algos, or add Kite's historical data. |
| Algo says "waiting for a fresh entry" for days | The strategy has been in the same trend since you started it. It will act on the next new signal. |
| A scanner line says "bought, NO stoploss" | Place a stoploss yourself in the GTT form or in Kite. |
| An order shows as sent but you see no shares | It may not have filled. Check Today's orders and Kite. |
| Anything looks wrong and you are unsure | Turn on the **Kill switch**, then check your positions in Kite itself. |

---

## 14. Words used in this manual

| Word | Meaning |
|---|---|
| **AMO** | After-market order: placed when the market is closed, sent at the next open. |
| **Candle** | One bar of the chart: the open, high, low and close of a period (1 minute to 1 day). |
| **CNC** | Delivery: you buy shares and keep them in your demat. |
| **GTT** | Good Till Triggered: an order that waits for a price. |
| **Limit order** | An order with a price you will not go beyond. |
| **LTP** | Last traded price. |
| **MIS** | Intraday: bought and sold the same day. |
| **OCO** | One cancels the other: a target and a stoploss placed together. |
| **Paper trading** | Trading with pretend money to test without risk. |
| **Slippage** | The difference between the price you expected and the price you got. |
| **Square-off** | Closing an intraday position before the day ends. |
| **Stoploss** | A price at which you sell to limit a loss. |
| **Target** | A price at which you sell to take a profit. |
| **Trade window** | The hours in which the desk accepts new buys. |
