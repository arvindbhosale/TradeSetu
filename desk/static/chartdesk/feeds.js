// @ts-check
// Live prices for the chart. Crypto streams straight from the exchange's public WebSocket (Binance's kline
// stream, or Coinbase's ticker when the server fell back to Coinbase); when the socket cannot connect the
// chart asks the server for fresh candles every few seconds instead. NSE/BSE poll the broker's last price
// through the server, because the broker's key must never reach the browser.

/** @typedef {import("./indicators.js").Bar} Bar */
/** @typedef {{ state: "live"|"poll"|"closed"|"error"|"connecting", text: string }} Status */

const IST = 19800;
const POLL_SECONDS = 5;

/** The bar a trade at `time` (chart clock) falls in, merged into `bars`. Returns the bar, or null when the
 * trade is older than the newest bar (a late message).
 * @param {Bar[]} bars @param {number} time @param {number} price @param {number} size
 * @param {number} seconds @param {number} anchor @returns {Bar|null} */
export function mergeTrade(bars, time, price, size, seconds, anchor) {
  if (!(price > 0)) return null;
  const start = time - (((time - anchor) % seconds) + seconds) % seconds;
  const last = bars[bars.length - 1];
  if (last && start < last.time) return null;
  if (last && start === last.time) {
    last.high = Math.max(last.high, price); last.low = Math.min(last.low, price);
    last.close = price; last.volume += size;
    return last;
  }
  const bar = { time: start, open: price, high: price, low: price, close: price, volume: size };
  bars.push(bar);
  return bar;
}

/** A finished or forming bar from the server or a stream, put in `bars` (replacing one with the same time).
 * @param {Bar[]} bars @param {Bar} bar @returns {boolean} whether it was used */
export function mergeBar(bars, bar) {
  const last = bars[bars.length - 1];
  if (last && bar.time < last.time) return false;
  if (last && bar.time === last.time) bars[bars.length - 1] = bar; else bars.push(bar);
  return true;
}

/**
 * @param {any} info the server's candles answer
 * @param {{ bars: Bar[], onChange: (bar: Bar) => void, onBars: (bars: Bar[]) => void,
 *   onStatus: (s: Status) => void, refetch: () => Promise<any>, quote: () => Promise<any>,
 *   WebSocketImpl?: any }} hooks
 * @returns {() => void} stop
 */
export function startFeed(info, hooks) {
  const live = info.live || {};
  let stopped = false, socket = /** @type {any} */ (null), timer = /** @type {any} */ (null), failures = 0, everOpened = false;
  const WS = hooks.WebSocketImpl || (typeof WebSocket !== "undefined" ? WebSocket : null);

  const stop = () => {
    stopped = true;
    if (timer) clearInterval(timer);
    if (socket) { try { socket.close(); } catch (err) { /* already closed */ } }
  };

  const poll = (reason) => {
    if (stopped || timer) return;
    hooks.onStatus({ state: "poll", text: `Updating every ${POLL_SECONDS} s through the desk${reason ? ` (${reason})` : ""}` });
    timer = setInterval(async () => {
      try {
        const fresh = await hooks.refetch();
        if (stopped) return;
        hooks.onBars(fresh.candles);
        hooks.onStatus({ state: "poll", text: `${fresh.source} · updating every ${POLL_SECONDS} s` });
      } catch (err) {
        if (!stopped) hooks.onStatus({ state: "error", text: `Live prices stopped: ${err.message}` });
      }
    }, POLL_SECONDS * 1000);
  };

  if (live.kind === "none") {          // an imported file: nothing to follow
    hooks.onStatus({ state: "closed", text: `${info.source || "Imported file"} · not live` });
    return stop;
  }

  if (live.kind === "refetch") {       // the classic chart: fresh candles from the desk every few seconds
    const every = live.every || 15;
    const status = (/** @type {any} */ fresh) => {
      const s = fresh.session || {};
      if (!s.open) hooks.onStatus({ state: "closed", text: `Market closed · ${fresh.source} data` });
      else hooks.onStatus({ state: /yahoo/i.test(fresh.source || "") ? "poll" : "live", text: `Live · ${fresh.source} · every ${every} s${s.time_ist ? ` · ${s.time_ist.slice(11, 19)} IST` : ""}` });
    };
    status(info);
    let open = !!(info.session && info.session.open), ticks = 0;
    const tick = async () => {
      if (stopped) return;
      if (!open && ++ticks % 4) return;          // market closed: look once a minute for the open
      try {
        const fresh = await hooks.refetch();
        if (stopped) return;
        open = !!(fresh.session && fresh.session.open);
        hooks.onBars(fresh.candles); status(fresh);
      } catch (err) {
        if (!stopped) hooks.onStatus({ state: "error", text: `Live prices stopped: ${err.message}` });
      }
    };
    timer = setInterval(tick, every * 1000);
    return stop;
  }

  if (live.kind === "poll") {          // NSE/BSE: the broker's last price, plus fresh candles each minute
    let ticks = 0, warned = false;
    const tick = async () => {
      if (stopped) return;
      try {
        if (++ticks % 20 === 0) { const fresh = await hooks.refetch(); if (!stopped) hooks.onBars(fresh.candles); }
        const q = await hooks.quote();
        if (stopped) return;
        if (!q.session.open) { hooks.onStatus({ state: "closed", text: `Market closed · ${info.source} prices` }); return; }
        const now = Math.floor(Date.now() / 1000) + IST;
        const bar = mergeTrade(hooks.bars, now, q.price, 0, info.seconds, info.anchor);
        if (bar) hooks.onChange(bar);
        hooks.onStatus({ state: "live", text: `Live · ${info.source} · every ${live.every || 3} s` });
        warned = false;
      } catch (err) {
        if (!stopped && !warned) { warned = true; hooks.onStatus({ state: "error", text: `Live prices stopped: ${err.message}` }); }
      }
    };
    if (!info.session || !info.session.open) hooks.onStatus({ state: "closed", text: `Market closed · ${info.source} prices` });
    else hooks.onStatus({ state: "connecting", text: "Connecting to live prices…" });
    tick();
    timer = setInterval(tick, (live.every || 3) * 1000);
    return stop;
  }

  if (!WS || !live.url) { poll("no live stream"); return stop; }

  const connect = () => {
    if (stopped) return;
    hooks.onStatus({ state: "connecting", text: `Connecting to ${live.kind === "binance" ? "Binance" : "Coinbase"}…` });
    let opened = false;
    try { socket = new WS(live.url); } catch (err) { poll("the browser blocked the stream"); return; }
    socket.onopen = () => {
      opened = true; failures = 0;
      if (everOpened) hooks.refetch().then((fresh) => { if (!stopped) hooks.onBars(fresh.candles); }).catch(() => { /* the stream carries on */ });
      everOpened = true;
      if (live.kind === "coinbase") socket.send(JSON.stringify({ type: "subscribe", product_ids: [live.product], channels: ["ticker"] }));
      hooks.onStatus({ state: "live", text: `Live · ${live.kind === "binance" ? "Binance" : "Coinbase"} stream` });
    };
    socket.onmessage = (event) => {
      let msg;
      try { msg = JSON.parse(event.data); } catch (err) { return; }
      if (live.kind === "binance" && msg && msg.k) {
        const k = msg.k;
        const bar = { time: Math.floor(k.t / 1000) + IST, open: +k.o, high: +k.h, low: +k.l, close: +k.c, volume: +k.v };
        if ([bar.open, bar.high, bar.low, bar.close].every((v) => v > 0) && mergeBar(hooks.bars, bar)) hooks.onChange(hooks.bars[hooks.bars.length - 1]);
      } else if (live.kind === "coinbase" && msg && msg.type === "ticker") {
        const time = Math.floor(Date.parse(msg.time) / 1000) + IST;
        const bar = mergeTrade(hooks.bars, time, +msg.price, +msg.last_size || 0, info.seconds, info.anchor);
        if (bar) hooks.onChange(bar);
      } else if (msg && msg.type === "error") {
        hooks.onStatus({ state: "error", text: `Coinbase stream: ${msg.message || "error"}` });
      }
    };
    socket.onclose = () => {
      if (stopped) return;
      failures++;
      if (!opened || failures >= 3) { poll(opened ? "the stream kept dropping" : "the stream could not connect"); return; }
      hooks.onStatus({ state: "connecting", text: "Stream dropped, reconnecting…" });
      setTimeout(connect, 2000);
    };
  };
  connect();
  return stop;
}
