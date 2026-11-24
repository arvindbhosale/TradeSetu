// @ts-check
// Replay: the chart cut at a past moment, then played forward one candle at a time for practice. Everything
// after the cut is hidden, and indicators are worked out on the shown candles only, so they look exactly as they
// did at that moment. Practice trades fill against the replay's candles (replaybook.js) and are saved as practice
// results on the desk, apart from the paper book, the live account, the journal and algo statistics. Nothing here
// places an order: "Back to live chart" opens the live chart with the same indicators, and trading there goes
// through the usual paper or live ticket.
//
// Blind replay picks a random stock from the watchlist and a random date, and hides both until the replay ends,
// so a well-known day cannot be traded from memory. The candles are in the browser while replaying, so this is
// honest practice, not a test that cannot be cheated.

import { advance, cancel, dayOf, exitAtNextOpen, finish, newBook, openPnl, place, setLevels, totals } from "./replaybook.js";
import { defaultLevels, nearest, roundPrice } from "./trade.js";

/** @typedef {import("./replaybook.js").Bar} Bar */
/** @typedef {import("./replaybook.js").Trade} Trade */

const SPEEDS = { "0.5": "½ a second", "1": "1 a second", "2": "2 a second", "5": "5 a second", "10": "10 a second" };
const DEFAULT_SPEED = "2";
// Blind replay draws from the watchlist; with none of the market in it, from these.
const BLIND_INDIA = ["RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK", "SBIN", "ITC", "LT", "AXISBANK", "BHARTIARTL",
  "KOTAKBANK", "HINDUNILVR", "MARUTI", "BAJFINANCE", "SUNPHARMA", "TITAN", "ASIANPAINT", "NTPC", "POWERGRID", "ULTRACEMCO"];
const BLIND_CRYPTO = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "ADAUSDT", "DOGEUSDT", "LINKUSDT"];

/** @param {string} tag @param {Record<string, any>} [attrs] @param {...any} kids @returns {HTMLElement} */
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v; else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}

const css = (/** @type {string} */ name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** A bar time (India wall clock in seconds) as the desk's start text: a date for day candles, else date and time.
 * @param {number} t @param {boolean} daily */
export function startText(t, daily) {
  const iso = new Date(t * 1000).toISOString();
  return daily ? iso.slice(0, 10) : iso.slice(0, 16);
}

/** A bar time for people: 12 Mar 2021, 10:15 (or just the date for day candles). @param {number} t @param {boolean} daily */
export function whenText(t, daily) {
  const d = new Date(t * 1000);
  const date = d.toLocaleDateString("en-IN", { day: "2-digit", month: "short", year: "numeric", timeZone: "UTC" });
  return daily ? date : `${date}, ${d.toISOString().slice(11, 16)}`;
}

/** The trading-day number of each bar day counted from the replay's first moment (Day 1), for blind mode's time
 * axis. ``start`` (that moment) keeps the numbering when the candle size changes.
 * @param {Bar[]} bars @param {number} startIndex @param {number|null} [start] */
export function dayNumbers(bars, startIndex, start = null) {
  /** @type {Map<number, number>} */ const out = new Map();
  const days = [...new Set(bars.map((b) => dayOf(b.time)))];
  const first = start != null ? dayOf(start) : bars[startIndex] ? dayOf(bars[startIndex].time) : days[days.length - 1];
  let at = days.findIndex((d) => d >= first);
  if (at < 0) at = days.length;
  days.forEach((d, i) => out.set(d, i - at + 1));
  return out;
}

/**
 * @param {HTMLElement} panel the replay panel under the chart
 * @param {HTMLElement} bar the replay controls over the chart
 * @param {{ api: (path: string, opts?: any) => Promise<any>, venue: () => string, symbol: () => string, tf: () => string,
 *   tfLabel: (tf: string) => string, decimals: () => number, money: (v: number) => string,
 *   enter: (r: { info: any, bars: Bar[], blind: boolean, fresh: boolean, label: (t: number, clock: boolean) => string }) => void,
 *   stepped: (all: boolean) => void, leave: (live?: { venue: string, symbol: string, drawings: boolean }) => void,
 *   chart: () => ({ main: any, price: any, box: HTMLElement }|null), setTf: (tf: string) => void,
 *   pickOnChart: (on: boolean) => void }} deps
 */
export function mountReplay(panel, bar, deps) {
  const { api } = deps;
  const rs = {
    setup: false, active: false, ended: false, loading: false, blind: false,
    venue: "", symbol: "", name: "", tf: "", info: /** @type {any} */ (null),
    all: /** @type {Bar[]} */ ([]), bars: /** @type {Bar[]} */ ([]), startIndex: 0, lock: 0, more: false,
    book: newBook(), timer: /** @type {any} */ (0), speed: DEFAULT_SPEED, log: /** @type {string[]} */ ([]),
    session: /** @type {any} */ (null), saved: /** @type {any} */ (null), past: /** @type {any} */ (null),
    draft: { side: "long", type: "market", product: "intraday", stop: /** @type {number|null} */ (null), target: /** @type {number|null} */ (null) },
    lines: /** @type {any[]} */ ([]), drag: /** @type {any} */ (null), msg: "", bad: false, picking: false, keepDrawings: false,
  };
  try { const s = localStorage.getItem("desk-replay-speed"); if (s && s in SPEEDS) rs.speed = s; } catch (err) { /* storage blocked */ }

  const india = () => rs.venue === "india" || rs.venue === "imported";
  const daily = () => ["1d", "1w"].includes(rs.tf);
  const intradayRules = () => india() && !daily();
  const lastBar = () => rs.bars[rs.bars.length - 1];
  const round = (/** @type {number} */ p) => roundPrice(rs.venue === "crypto" ? "crypto" : "india", p, deps.decimals());
  const money = (/** @type {number} */ v) => deps.money(v);
  const signed = (/** @type {number} */ v) => `${v >= 0 ? "+" : "−"}${money(Math.abs(v))}`;
  const label = () => (rs.blind && !rs.ended ? "Hidden stock" : rs.name || rs.symbol);
  const when = (/** @type {number} */ t) => (rs.blind && !rs.ended ? blindLabel(t, !daily()) : whenText(t, daily()));
  let days = /** @type {Map<number, number>} */ (new Map());
  function blindLabel(/** @type {number} */ t, /** @type {boolean} */ clock) {
    const n = days.get(dayOf(t));
    const d = n == null ? "Day ?" : `Day ${n}`;
    return clock ? `${d}, ${new Date(t * 1000).toISOString().slice(11, 16)}` : d;
  }

  // -- the setup (before a replay) -------------------------------------------------------------------------
  const dateIn = /** @type {HTMLInputElement} */ (h("input", { type: "date", name: "cd-r-date", "aria-label": "Start date" }));
  const timeIn = /** @type {HTMLInputElement} */ (h("input", { type: "time", name: "cd-r-time", step: "60", "aria-label": "Start time" }));
  const setupMsg = h("div", { class: "cd-paper-msg", role: "status", hidden: true });

  function say(/** @type {string} */ text, /** @type {boolean} */ bad = false) { rs.msg = text; rs.bad = bad; renderMsg(); }
  function renderMsg() {
    for (const el of [setupMsg, panelMsg]) { el.textContent = rs.msg; el.className = "cd-paper-msg " + (rs.bad ? "error" : "ok"); el.hidden = !rs.msg; }
  }

  // -- the controls over the chart ---------------------------------------------------------------------------
  const playBtn = /** @type {HTMLButtonElement} */ (h("button", { type: "button", class: "primary", onclick: () => toggle() }));
  const backBtn = /** @type {HTMLButtonElement} */ (h("button", { type: "button", text: "◀ Back", title: "Previous candle (Left arrow). Only back to your last trade or order.", onclick: () => back() }));
  const nextBtn = /** @type {HTMLButtonElement} */ (h("button", { type: "button", text: "Next ▶", title: "Next candle (Right arrow)", onclick: () => { pause(); step(1); } }));
  const dayBtn = /** @type {HTMLButtonElement} */ (h("button", { type: "button", text: "Next day ⏭", title: "Play to the next day's first candle", onclick: () => { pause(); nextDay(); } }));
  const speedSel = /** @type {HTMLSelectElement} */ (h("select", { "aria-label": "Speed", onchange: () => {
    rs.speed = speedSel.value; try { localStorage.setItem("desk-replay-speed", rs.speed); } catch (err) { /* storage blocked */ }
    if (rs.timer) { pause(); play(); }
  } }, Object.entries(SPEEDS).map(([v, t]) => h("option", { value: v, text: `${t}` }))));
  const clock = h("span", { class: "cd-r-clock" });
  const endBtn = /** @type {HTMLButtonElement} */ (h("button", { type: "button", text: "End replay", onclick: () => end() }));
  bar.replaceChildren(h("span", { class: "pill warn cd-r-pill", text: "REPLAY · practice" }), backBtn, playBtn, nextBtn, dayBtn,
    h("label", { class: "cd-r-speed" }, "Speed", speedSel), clock, endBtn);

  // -- the practice ticket and results under the chart ---------------------------------------------------
  const sideSeg = h("div", { class: "seg cd-side", role: "group", "aria-label": "Buy or short sell" });
  const typeSeg = h("div", { class: "seg", role: "group", "aria-label": "Order type" });
  const productSeg = h("div", { class: "seg", role: "group", "aria-label": "Intraday or delivery" });
  const qtyIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", name: "cd-r-qty", "aria-label": "Quantity" }));
  const priceIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", name: "cd-r-price", "aria-label": "Limit price" }));
  const stopIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", name: "cd-r-stop", "aria-label": "Stoploss" }));
  const targetIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", name: "cd-r-target", "aria-label": "Target" }));
  const priceLabel = h("label", {}, "Limit price", priceIn);
  const submit = /** @type {HTMLButtonElement} */ (h("button", { type: "submit", class: "primary" }));
  const linesBtn = /** @type {HTMLButtonElement} */ (h("button", { type: "button", class: "cd-t-lines", onclick: () => {
    if (rs.draft.stop || rs.draft.target) { rs.draft.stop = rs.draft.target = null; stopIn.value = targetIn.value = ""; } else planLines();
    renderTicket(); drawLines();
  } }));
  const ticketSum = h("p", { class: "hint cd-t-sum" });
  const form = h("form", { class: "cd-ticket cd-t-form", onsubmit: (/** @type {Event} */ e) => { e.preventDefault(); placeOrder(); } },
    sideSeg, typeSeg, productSeg, h("label", {}, "Quantity", qtyIn), priceLabel, h("label", {}, "Stoploss", stopIn), h("label", {}, "Target", targetIn), linesBtn, submit);
  const openBox = h("div", { class: "cd-r-open" });
  const tiles = h("div", { class: "cd-tiles" });
  const tradesBox = h("div", { class: "cd-table-wrap" });
  const logBox = h("ul", { class: "cd-r-log hint" });
  const panelMsg = h("div", { class: "cd-paper-msg", role: "status", hidden: true });
  const pastBox = h("div", { class: "cd-r-past" });

  for (const el of [qtyIn, priceIn, stopIn, targetIn]) el.addEventListener("input", () => {
    rs.draft.stop = num(stopIn); rs.draft.target = num(targetIn); renderTicket(); drawLines();
  });

  /** @param {HTMLInputElement} el */
  function num(el) { const v = Number(el.value); return el.value !== "" && Number.isFinite(v) && v > 0 ? v : null; }

  /** @param {HTMLElement} host @param {Record<string,string>} options @param {string} current @param {(k: string) => void} pick */
  function seg(host, options, current, pick) {
    host.replaceChildren(...Object.entries(options).map(([k, text]) => h("button", { type: "button", "aria-pressed": String(k === current), text, onclick: () => pick(k) })));
  }

  // -- render ---------------------------------------------------------------------------------------------
  function render() {
    bar.hidden = !rs.active;
    panel.hidden = !(rs.setup || rs.active);
    if (!rs.active) { renderSetup(); return; }
    renderBar(); renderPanel();
  }

  function renderSetup() {
    const tf = deps.tf(), venue = deps.venue(), intraday = !["1d", "1w"].includes(tf);
    if (!dateIn.value) {
      const d = new Date(Date.now() - 30 * 86400000);
      dateIn.value = d.toISOString().slice(0, 10);
    }
    dateIn.max = new Date(Date.now() + 19800000).toISOString().slice(0, 10);
    timeIn.disabled = !intraday;
    const canBlind = venue === "india" || venue === "crypto" || venue === "imported";
    panel.replaceChildren(
      h("div", { class: "cd-paper-headline" }, h("strong", { text: "Replay: practise on past candles" }),
        h("button", { type: "button", class: "cd-x", "aria-label": "Close replay", text: "×", onclick: () => { rs.setup = false; setPicking(false); render(); } })),
      h("p", { class: "hint", text: `Hides every candle after the start and shows them one by one, with your indicators. ${deps.symbol() || "Pick a symbol"} · ${deps.tfLabel(tf)} candles. Practice trades are kept apart from paper and live trading.` }),
      h("form", { class: "cd-ticket", onsubmit: (/** @type {Event} */ e) => { e.preventDefault(); startFrom(dateIn.value + (intraday && timeIn.value ? "T" + timeIn.value : "")); } },
        h("label", {}, "Start date", dateIn), h("label", {}, intraday ? "Start time (optional)" : "Start time (not for day candles)", timeIn),
        h("button", { type: "submit", class: "primary", text: "Start replay", disabled: rs.loading || !deps.symbol() }),
        h("button", { type: "button", text: rs.picking ? "Cancel picking" : "Pick a candle on the chart", disabled: rs.loading || !deps.symbol(), onclick: () => setPicking(!rs.picking) }),
        h("button", { type: "button", text: "Blind replay", title: "A random stock from your watchlist and a random date, both hidden until you end the replay", disabled: rs.loading || !canBlind, onclick: () => startBlind() })),
      h("p", { class: "hint", hidden: !rs.picking, text: "Tap a candle: the replay starts right after it." }),
      h("p", { class: "hint", text: venue === "crypto" ? "Crypto history comes from Binance." : venue === "india" ? "NSE/BSE history comes from your broker: intraday candles from Feb 2015, day candles from 2008. Expired option contracts have no history." : "" }),
      setupMsg, pastBox);
    renderMsg(); loadPast();
  }

  function renderBar() {
    playBtn.textContent = rs.timer ? "❚❚ Pause" : "▶ Play";
    playBtn.disabled = rs.ended || rs.loading;
    nextBtn.disabled = dayBtn.disabled = rs.ended || rs.loading || (rs.bars.length >= rs.all.length && !rs.more);
    backBtn.disabled = rs.ended || rs.loading || !canBack();
    speedSel.value = rs.speed;
    const b = lastBar();
    const left = rs.all.length - rs.bars.length;
    clock.textContent = b ? `${label()} · ${when(b.time)} · ${left.toLocaleString("en-IN")} candles left${rs.more ? " (more load at the end)" : ""}` : "";
  }

  function renderTicket() {
    if (!india()) rs.draft.side = "long";
    seg(sideSeg, india() ? { long: "Buy", short: "Short sell" } : { long: "Buy" }, rs.draft.side, (k) => {
      if (k !== rs.draft.side) { rs.draft.side = k; rs.draft.stop = rs.draft.target = null; stopIn.value = targetIn.value = ""; }
      if (k === "short") rs.draft.product = "intraday";
      renderTicket(); drawLines();
    });
    sideSeg.dataset.side = rs.draft.side === "long" ? "BUY" : "SELL";
    seg(typeSeg, { market: "At next open", limit: "Limit" }, rs.draft.type, (k) => {
      rs.draft.type = k;
      const b = lastBar();
      if (k === "limit" && !priceIn.value && b) priceIn.value = String(round(b.close));
      renderTicket(); drawLines();
    });
    productSeg.hidden = !india();
    if (daily() && rs.draft.product === "intraday" && rs.draft.side === "long") rs.draft.product = "delivery";
    seg(productSeg, daily() ? { delivery: "Delivery" } : { intraday: "Intraday", delivery: "Delivery" }, rs.draft.product, (k) => {
      if (k === "delivery" && rs.draft.side === "short") { say("Shares can be sold short only intraday.", true); return; }
      rs.draft.product = k; renderTicket();
    });
    priceLabel.hidden = rs.draft.type !== "limit";
    const buy = rs.draft.side === "long";
    submit.textContent = `Place practice ${buy ? "buy" : "short sale"}`;
    submit.className = "primary " + (buy ? "cd-buy" : "cd-sell");
    submit.disabled = rs.ended || !!rs.book.position || !!rs.book.pending;
    qtyIn.placeholder = india() ? "Shares" : "Amount";
    const b = lastBar(), q = num(qtyIn), p = rs.draft.type === "limit" ? num(priceIn) : b ? b.close : null;
    const parts = [];
    if (q && p) parts.push(`${buy ? "Buy" : "Short sell"} ${q} ≈ ${money(q * p)}`);
    if (q && p && rs.draft.stop) parts.push(`Stoploss: ${signed((buy ? rs.draft.stop - p : p - rs.draft.stop) * q)}`);
    if (q && p && rs.draft.target) parts.push(`Target: ${signed((buy ? rs.draft.target - p : p - rs.draft.target) * q)}`);
    if (q && p && rs.draft.stop && rs.draft.target && Math.abs(p - rs.draft.stop) > 0) parts.push(`Reward to risk ${(Math.abs(rs.draft.target - p) / Math.abs(p - rs.draft.stop)).toFixed(2)}`);
    if (rs.draft.type === "market") parts.push("Fills at the next candle's open");
    if (rs.draft.side === "short") parts.push("Intraday only: bought back by 3:20 PM");
    ticketSum.textContent = parts.join(" · ") + (rs.draft.stop || rs.draft.target ? " (before charges)" : "");
    linesBtn.textContent = rs.draft.stop || rs.draft.target ? "Remove lines" : "Add stoploss and target on chart";
  }

  function renderOpen() {
    const p = rs.book.position, o = rs.book.pending, b = lastBar();
    const rows = [];
    if (p && b) {
      const v = openPnl(rs.book, b.close);
      const stop = /** @type {HTMLInputElement} */ (h("input", { type: "number", step: "any", min: "0", value: p.stop == null ? "" : String(p.stop), "aria-label": "Stoploss of the open trade" }));
      const target = /** @type {HTMLInputElement} */ (h("input", { type: "number", step: "any", min: "0", value: p.target == null ? "" : String(p.target), "aria-label": "Target of the open trade" }));
      const apply = () => levels({ stop: num(stop), target: num(target) });
      stop.addEventListener("change", apply); target.addEventListener("change", apply);
      rows.push(h("div", { class: "cd-t-row" },
        h("strong", { text: `${p.side === "long" ? "Bought" : "Short sold"} ${p.qty} at ${money(p.entry)}` }),
        h("span", { class: v >= 0 ? "up" : "down", text: `Open P&L ${signed(v)}` }),
        h("span", { class: "hint", text: `${p.product === "intraday" ? "Intraday" : "Delivery"} · since ${when(p.time)}` }),
        h("span", { class: "cd-ticket" }, h("label", {}, "Stoploss", stop), h("label", {}, "Target", target)),
        h("button", { type: "button", class: "cd-cancel", text: "Exit at next open", disabled: rs.ended || rs.book.exitNext, onclick: () => { exitAtNextOpen(rs.book); touched("Exits at the next candle's open."); } }),
        rs.book.exitNext ? h("span", { class: "hint", text: "Exiting at the next open" }) : null));
    }
    if (o) {
      rows.push(h("div", { class: "cd-t-row" },
        h("span", { text: `Waiting: ${o.side === "long" ? "Buy" : "Short sell"} ${o.qty} ${o.type === "limit" ? `at ${money(/** @type {number} */ (o.price))} or better` : "at the next open"}` }),
        o.stop ? h("span", { class: "down", text: `Stoploss ${money(o.stop)}` }) : null,
        o.target ? h("span", { class: "up", text: `Target ${money(o.target)}` }) : null,
        h("button", { type: "button", class: "cd-cancel", text: "Cancel", onclick: () => { cancel(rs.book); touched("Order cancelled."); } })));
    }
    openBox.replaceChildren(...rows);
  }

  function renderResults() {
    const t = totals(rs.book.trades);
    const saved = rs.saved ? rs.saved.summary : null;
    const tile = (/** @type {string} */ name, /** @type {string} */ value, /** @type {string} */ cls = "") => h("div", { class: "cd-tile" }, h("span", { class: "hint", text: name }), h("strong", { class: cls, text: value }));
    const net = saved ? saved.net : t.net, charges = saved ? saved.charges : t.charges;
    tiles.replaceChildren(
      tile("Trades", `${t.trades}${t.trades ? ` · ${t.wins} won` : ""}`),
      tile("Win rate", t.winRate == null ? "–" : `${t.winRate.toFixed(0)}%`),
      tile("P&L after charges", signed(net), net >= 0 ? "up" : "down"),
      tile("Charges · worst drawdown", `${money(charges)} · ${money(Math.abs(saved ? saved.max_drawdown : t.maxDrawdown))}`));
    if (!rs.book.trades.length) { tradesBox.replaceChildren(h("p", { class: "hint", text: "No practice trades yet. Play the chart, then place a buy or short sale when you see a setup." })); return; }
    const rows = (rs.saved ? rs.saved.trades : rs.book.trades).slice().reverse();
    tradesBox.replaceChildren(h("table", { class: "cd-table" },
      h("thead", {}, h("tr", {}, ["Side", "Qty", "Entry", "Exit", "Opened", "Closed", "Why", "P&L", "After charges"].map((c) => h("th", { text: c })))),
      h("tbody", {}, rows.map((/** @type {any} */ r) => h("tr", {},
        h("td", { text: r.side === "long" ? "Buy" : "Short" }), h("td", { text: String(r.qty) }), h("td", { text: money(r.entry) }), h("td", { text: money(r.exit) }),
        h("td", { text: when(r.entry_time) }), h("td", { text: when(r.exit_time) }), h("td", { text: r.reason }),
        h("td", { class: r.gross >= 0 ? "up" : "down", text: signed(r.gross) }),
        h("td", { class: (r.net ?? r.gross) >= 0 ? "up" : "down", text: r.net == null ? "…" : signed(r.net) }))))));
  }

  function renderPanel() {
    renderTicket(); renderOpen(); renderResults();
    logBox.replaceChildren(...rs.log.slice(-5).reverse().map((t) => h("li", { text: t })));
    const head = h("div", { class: "cd-paper-headline" }, h("strong", { text: `Practice trades · ${label()}` }),
      h("span", { class: "pill", text: rs.blind ? "Blind replay" : "Replay" }));
    const keep = h("label", { class: "check" }, h("input", { type: "checkbox", checked: rs.keepDrawings, onchange: (/** @type {Event} */ e) => { rs.keepDrawings = /** @type {HTMLInputElement} */ (e.target).checked; } }), "Keep my replay drawings on the live chart");
    const reveal = rs.ended ? h("div", { class: "cd-paper-msg ok" },
      h("strong", { text: `${rs.name || rs.symbol}` }), ` · ${deps.tfLabel(rs.tf)} candles · from ${whenText(rs.session.start, daily())} to ${whenText(lastBar().time, daily())}. `,
      rs.saved ? `Saved as practice: ${rs.saved.summary.trades} trades, ${signed(rs.saved.summary.net)} after charges.` : "") : null;
    const live = h("div", { class: "cd-reset-row" },
      h("button", { type: "button", class: "primary", text: rs.ended ? `Open ${rs.name || rs.symbol} live chart` : "Back to live chart", onclick: () => leave(true) }),
      rs.ended ? h("button", { type: "button", text: "Another blind replay", onclick: () => { exitQuiet(); startBlind(); } }) : null,
      rs.ended ? null : h("button", { type: "button", text: "End replay", onclick: () => end() }), rs.blind && !rs.ended ? null : keep);
    panel.replaceChildren(...[head, reveal, rs.ended ? null : form, rs.ended ? null : ticketSum, panelMsg, openBox, tiles, tradesBox, logBox, live,
      h("p", { class: "hint cd-t-help", text: "Drag the Stoploss, Target or Limit line on the chart to move it. Fills: at the next candle's open; a gap through a stoploss fills at the open; a candle that touches both stoploss and target counts the stoploss. Intraday trades are squared off at 3:20 PM. ◀ Back goes back only to your last trade or order. Keys: Space play or pause, Right arrow next candle." })].filter((x) => x != null));
    renderMsg();
  }

  async function loadPast() {
    try { rs.past = await api("/replay/sessions"); } catch (err) { rs.past = null; }
    if (rs.active || !rs.setup) return;
    const p = rs.past;
    if (!p || !p.sessions.length) { pastBox.replaceChildren(); return; }
    const s = p.score;
    pastBox.replaceChildren(h("div", { class: "lbl", text: `Your practice so far: ${s.trades} trades, ${s.win_rate == null ? "–" : s.win_rate + "%"} won, ${signed(s.net)} after charges` }),
      h("div", { class: "cd-table-wrap" }, h("table", { class: "cd-table" },
        h("thead", {}, h("tr", {}, ["Stock", "Candles", "Replayed from", "Trades", "Won", "After charges", ""].map((c) => h("th", { text: c })))),
        h("tbody", {}, p.sessions.slice(0, 10).map((/** @type {any} */ r) => h("tr", {},
          h("td", { text: r.blind ? `${r.symbol} (blind)` : r.symbol }), h("td", { text: r.tf }),
          h("td", { text: r.blind && !r.ended ? "Hidden" : whenText(r.start, ["1d", "1w"].includes(r.tf)) }),
          h("td", { text: String(r.summary.trades) }), h("td", { text: r.summary.win_rate == null ? "–" : r.summary.win_rate + "%" }),
          h("td", { class: r.summary.net >= 0 ? "up" : "down", text: signed(r.summary.net) }),
          h("td", {}, h("button", { type: "button", class: "cd-cancel", text: "Delete", "aria-label": "Delete this practice session", onclick: async () => {
            try { await api(`/replay/sessions/${r.id}`, { method: "DELETE" }); loadPast(); } catch (err) { say(`Not deleted: ${err.message}`, true); }
          } }))))))));
  }

  // -- starting -------------------------------------------------------------------------------------------
  function setPicking(on) { rs.picking = on; deps.pickOnChart(on); if (rs.setup && !rs.active) renderSetup(); }

  /** @param {string} start */
  async function startFrom(start) {
    if (!start || start.length < 10) { say("Pick the start date.", true); return; }
    await begin({ venue: deps.venue(), symbol: deps.symbol(), tf: deps.tf(), start, blind: false });
  }

  async function startBlind() {
    const venue = deps.venue(), tf = deps.tf();
    let pool = [];
    if (venue === "imported") pool = [deps.symbol()].filter(Boolean);
    else {
      try {
        const w = await api("/md/watchlist");
        pool = (w.items || []).filter((/** @type {any} */ i) => i.venue === venue).map((/** @type {any} */ i) => i.symbol).filter((/** @type {string} */ s) => !s.startsWith("IDX:"));
      } catch (err) { /* the built-in list below */ }
      if (!pool.length) pool = venue === "crypto" ? BLIND_CRYPTO : BLIND_INDIA;
    }
    if (!pool.length) { say("Import a file first.", true); return; }
    let last = "";
    for (let i = 0; i < 3; i++) {
      const symbol = pool[Math.floor(Math.random() * pool.length)];
      const ok = await begin({ venue, symbol, tf, start: "random", blind: true }, true);
      if (ok === true) return;
      last = String(ok);
    }
    say(`Blind replay could not start: ${last}`, true);
  }

  /** @param {{ venue: string, symbol: string, tf: string, start: string, blind: boolean }} o @param {boolean} [quiet]
   * @returns {Promise<true|string>} */
  async function begin(o, quiet = false) {
    rs.loading = true; say(o.blind ? "Picking a random stock and date…" : "Loading the candles…"); if (rs.setup) renderSetup();
    try {
      const info = await api("/md/replay?" + new URLSearchParams({ venue: o.venue, symbol: o.symbol, tf: o.tf, start: o.start }));
      setPicking(false);
      rs.loading = false;
      rs.venue = o.venue; rs.symbol = info.symbol || o.symbol; rs.name = info.name || rs.symbol; rs.tf = o.tf; rs.blind = o.blind;
      rs.book = newBook(); rs.log = []; rs.saved = null; rs.session = { id: "", start: info.start }; rs.ended = false;
      rs.draft = { side: "long", type: "market", product: ["1d", "1w"].includes(o.tf) ? "delivery" : "intraday", stop: null, target: null };
      qtyIn.value = ""; priceIn.value = ""; stopIn.value = ""; targetIn.value = "";
      load(info);
      rs.active = true; rs.setup = false;
      say(o.blind ? "Blind replay: the stock and dates stay hidden until you end it. Press Play." : `Replay from ${whenText(info.start, daily())}. Press Play or Next.`);
      render();
      return true;
    } catch (err) {
      rs.loading = false;
      if (!quiet) say(err.message, true);
      if (rs.setup) renderSetup();
      return err.message;
    }
  }

  /** Shows a loaded window: the candles before the cut, the rest kept back. @param {any} info @param {boolean} [fresh] a new
   * replay (drawings start empty) rather than the same one at another candle size */
  function load(info, fresh = true) {
    rs.info = info;
    rs.all = info.candles.map((/** @type {Bar} */ c) => ({ ...c }));
    rs.startIndex = info.start_index; rs.lock = info.start_index; rs.more = !!info.more;
    rs.bars = rs.all.slice(0, info.start_index);
    rs.book.last = rs.bars[rs.bars.length - 1] || null;
    days = dayNumbers(rs.all, rs.startIndex, rs.session ? rs.session.start : null);
    deps.enter({ info: { ...info, name: rs.blind ? "Hidden stock" : info.name, candles: rs.bars, live: { kind: "none" } }, bars: rs.bars, blind: rs.blind, fresh, label: blindLabel });
  }

  // -- playing ----------------------------------------------------------------------------------------------
  function canBack() { return rs.bars.length - 1 >= Math.max(rs.lock, rs.startIndex, 1); }
  function play() {
    if (rs.timer || rs.ended) return;
    rs.timer = setInterval(() => { step(1); }, 1000 / Number(rs.speed));
    renderBar();
  }
  function pause() { if (rs.timer) { clearInterval(rs.timer); rs.timer = 0; } if (rs.active) renderBar(); }
  function toggle() { if (rs.timer) pause(); else play(); }

  /** @param {number} n */
  async function step(n) {
    if (!rs.active || rs.ended || rs.loading) return;
    let changed = false;
    for (let i = 0; i < n; i++) {
      if (rs.bars.length >= rs.all.length) {
        if (!(await more())) { pause(); break; }
      }
      changed = reveal() || changed;
    }
    deps.stepped(false);
    after(changed);
  }

  /** Shows the next candle and plays it through the practice book. Returns whether the book changed. */
  function reveal() {
    const b = rs.all[rs.bars.length];
    rs.bars.push(b);
    const events = advance(rs.book, b, { squareOff: intradayRules() });
    events.forEach((e) => rs.log.push(`${when(b.time)}: ${e.text}`));
    if (events.length) rs.lock = rs.bars.length;
    if (events.some((e) => e.trade)) save(false);
    return events.length > 0;
  }

  function nextDay() {
    if (!rs.active || rs.ended) return;
    const start = lastBar() ? dayOf(lastBar().time) : 0;
    (async () => {
      for (let i = 0; i < 3000; i++) {
        if (rs.bars.length >= rs.all.length && !(await more())) break;
        reveal();
        if (daily() || dayOf(lastBar().time) !== start) break;
      }
      deps.stepped(true);
      after(true);
    })();
  }

  function back() {
    if (!canBack()) { say("Back goes only to your last trade or order, so a practice result cannot be redone after seeing what came next.", true); return; }
    pause();
    rs.bars.pop();
    rs.book.last = lastBar() || null;
    deps.stepped(true);
    after(false);
  }

  /** Loads the next stretch of candles when the replay reaches the end. */
  async function more() {
    if (!rs.more) { say("No more candles: this is the newest history.", false); return false; }
    rs.loading = true; renderBar();
    try {
      const last = rs.all[rs.all.length - 1];
      const r = await api("/md/replay?" + new URLSearchParams({ venue: rs.venue, symbol: rs.symbol, tf: rs.tf, after: String(last.time) }));
      rs.loading = false;
      const fresh = (r.candles || []).filter((/** @type {Bar} */ c) => c.time > last.time);
      rs.more = !!r.more && fresh.length > 0;
      fresh.forEach((/** @type {Bar} */ c) => rs.all.push({ ...c }));
      days = dayNumbers(rs.all, rs.startIndex, rs.session ? rs.session.start : null);
      return fresh.length > 0;
    } catch (err) {
      rs.loading = false;
      say(`More candles could not be loaded: ${err.message}`, true);
      return false;
    }
  }

  /** @param {boolean} changed */
  function after(changed) {
    if (!rs.active) return;
    renderBar();
    if (changed) renderPanel(); else { renderOpen(); renderResults(); renderTicket(); logBox.replaceChildren(...rs.log.slice(-5).reverse().map((t) => h("li", { text: t }))); }
    drawLines();
  }

  // -- orders -----------------------------------------------------------------------------------------------
  function placeOrder() {
    const b = lastBar();
    if (!b || rs.ended) return;
    const qty = num(qtyIn);
    if (!qty) { say("Enter the quantity.", true); return; }
    if (india() && Math.floor(qty) !== qty) { say("Shares come in whole numbers.", true); return; }
    const err = place(rs.book, { side: /** @type {any} */ (rs.draft.side), type: /** @type {any} */ (rs.draft.type), price: num(priceIn), qty,
      stop: rs.draft.stop, target: rs.draft.target, product: /** @type {any} */ (india() ? rs.draft.product : "delivery") }, b,
    { shortOk: india() && rs.draft.product === "intraday", squareOff: intradayRules(), seconds: rs.info ? rs.info.seconds : 0 });
    if (err) { say(err, true); return; }
    rs.draft.stop = rs.draft.target = null; stopIn.value = targetIn.value = "";
    touched(`Practice ${rs.draft.side === "long" ? "buy" : "short sale"} placed: ${rs.draft.type === "market" ? "fills at the next open" : "waits for the limit price"}.`);
  }

  /** @param {{ stop: number|null, target: number|null }} lv */
  function levels(lv) {
    const b = lastBar();
    if (!b) return;
    const err = setLevels(rs.book, lv, b.close);
    if (err) { say(err, true); renderOpen(); drawLines(); return; }
    touched("Stoploss and target moved.");
  }

  /** The book changed by the user's hand: Back cannot go before this candle. @param {string} text */
  function touched(text) { rs.lock = rs.bars.length; rs.log.push(`${when(lastBar().time)}: ${text}`); say(text); after(true); }

  async function save(ended) {
    const body = { id: rs.session.id || undefined, venue: rs.venue, symbol: rs.symbol, name: rs.name, tf: rs.tf, start: rs.session.start || (rs.all[rs.startIndex] || lastBar()).time,
      blind: rs.blind, ended, trades: rs.book.trades.map((t) => ({ side: t.side, qty: t.qty, entry: t.entry, exit: t.exit, entry_time: t.entry_time, exit_time: t.exit_time, reason: t.reason, product: t.product })) };
    try {
      const r = await api("/replay/sessions", { method: "POST", body });
      rs.session.id = r.session.id; rs.saved = r.session;
      if (rs.active) renderResults();
    } catch (err) { say(`Practice result not saved: ${err.message}`, true); }
  }

  async function end() {
    if (!rs.active || rs.ended) return;
    pause();
    const t = finish(rs.book);
    if (t) rs.log.push(`${when(t.exit_time)}: End of replay: closed at ${t.exit}`);
    rs.ended = true;
    if (rs.book.trades.length || rs.session.id) await save(true);
    else rs.saved = null;
    deps.enter({ info: { ...rs.info, name: rs.name, candles: rs.bars, live: { kind: "none" } }, bars: rs.bars, blind: false, fresh: false, label: blindLabel });
    say(rs.book.trades.length ? "Replay ended. The result is saved with your practice, not your paper or live trades." : "Replay ended. No practice trades were made.");
    render(); drawLines();
  }

  /** Leaves the replay for the live chart (the replayed stock's after a blind replay). @param {boolean} reveal */
  async function leave(reveal) {
    if (rs.active && !rs.ended && (rs.book.position || rs.book.trades.length)) await end();
    const go = reveal ? { venue: rs.venue, symbol: rs.symbol, drawings: rs.keepDrawings && !rs.blind } : null;
    exitQuiet();
    deps.leave(go || undefined);
  }

  /** Drops the replay without going live (another replay starts, or the user opened another chart). */
  function exitQuiet() {
    pause(); clearLines();
    if (rs.active && !rs.ended && rs.book.trades.length) save(true);
    rs.active = false; rs.ended = false; rs.setup = false; rs.all = []; rs.bars = []; rs.msg = "";
    render();
  }

  // -- the lines on the chart ---------------------------------------------------------------------------------
  function clearLines() {
    const c = deps.chart();
    rs.lines.forEach((l) => { try { c && c.price.removePriceLine(l.line); } catch (err) { /* chart redrawn */ } });
    rs.lines = [];
  }

  function drawLines() {
    clearLines();
    const c = deps.chart();
    if (!c || !rs.active) return;
    /** @param {string} kind @param {number} price @param {string} owner @param {string} title */
    const add = (kind, price, owner, title) => {
      const color = kind === "stop" ? css("--bad") : kind === "target" ? css("--good") : css("--accent");
      rs.lines.push({ kind, owner, price, line: c.price.createPriceLine({ price, color, lineWidth: 2, lineStyle: owner === "draft" ? 2 : 0, axisLabelVisible: true, title }) });
    };
    const p = rs.book.position, o = rs.book.pending;
    if (p) {
      add("entry", p.entry, "fixed", `Practice ${p.side === "long" ? "buy" : "short"} ${p.qty}`);
      if (p.stop) add("stop", p.stop, "book", "Stoploss");
      if (p.target) add("target", p.target, "book", "Target");
    } else if (o) {
      if (o.type === "limit" && o.price) add("entry", o.price, "fixed", `Waiting ${o.side === "long" ? "buy" : "short"}`);
      if (o.stop) add("stop", o.stop, "book", "Stoploss");
      if (o.target) add("target", o.target, "book", "Target");
    } else if (!rs.ended) {
      if (rs.draft.stop) add("stop", rs.draft.stop, "draft", "Stoploss (plan)");
      if (rs.draft.target) add("target", rs.draft.target, "draft", "Target (plan)");
      const lp = num(priceIn);
      if (rs.draft.type === "limit" && lp) add("limit", lp, "draft", "Limit (plan)");
    }
    bind();
  }

  /** Puts a stoploss and target on the plan around the last price (the Stoploss/Target boxes start empty). */
  function planLines() {
    const b = lastBar();
    if (!b) return;
    const ref = rs.draft.type === "limit" && num(priceIn) ? /** @type {number} */ (num(priceIn)) : b.close;
    const d = defaultLevels(ref, rs.draft.side === "short");
    rs.draft.stop = round(d.stop); rs.draft.target = round(d.target);
    stopIn.value = String(rs.draft.stop); targetIn.value = String(rs.draft.target);
  }

  function hit(/** @type {number} */ y) {
    const c = deps.chart();
    if (!c) return -1;
    return nearest(rs.lines.map((l) => ({ y: l.owner === "fixed" ? null : c.price.priceToCoordinate(l.price) })), y);
  }

  let bound = /** @type {HTMLElement|null} */ (null);
  function bind() {
    const c = deps.chart();
    if (!c || bound === c.box) return;
    bound = c.box;
    const box = c.box;
    const yOf = (/** @type {PointerEvent} */ e) => e.clientY - box.getBoundingClientRect().top;
    box.addEventListener("pointerdown", (e) => {
      if (!rs.active || rs.ended || !rs.lines.length) return;
      const i = hit(yOf(e));
      if (i < 0) return;
      const chart = deps.chart();
      if (!chart) return;
      e.stopPropagation(); e.preventDefault();
      pause();
      rs.drag = { i };
      chart.main.applyOptions({ handleScroll: false, handleScale: false });
      try { box.setPointerCapture(e.pointerId); } catch (err) { /* old browsers */ }
    }, true);
    for (const type of ["mousedown", "touchstart"]) box.addEventListener(type, (e) => { if (rs.drag) { e.stopPropagation(); if (e.cancelable) e.preventDefault(); } }, { capture: true, passive: false });
    box.addEventListener("pointermove", (e) => {
      const chart = deps.chart();
      if (!chart || !rs.active) return;
      if (!rs.drag) { if (hit(yOf(e)) >= 0) box.style.cursor = "ns-resize"; return; }
      e.stopPropagation();
      const l = rs.lines[rs.drag.i], p = chart.price.coordinateToPrice(yOf(e));
      if (!l || p == null || !(p > 0)) return;
      l.price = round(p);
      l.line.applyOptions({ price: l.price });
      if (l.owner === "draft") {
        if (l.kind === "stop") { rs.draft.stop = l.price; stopIn.value = String(l.price); }
        else if (l.kind === "target") { rs.draft.target = l.price; targetIn.value = String(l.price); }
        else priceIn.value = String(l.price);
        renderTicket();
      }
    }, true);
    const done = (/** @type {PointerEvent} */ e) => {
      if (!rs.drag) return;
      e.stopPropagation();
      const l = rs.lines[rs.drag.i];
      rs.drag = null;
      const chart = deps.chart();
      if (chart) chart.main.applyOptions({ handleScroll: true, handleScale: true });
      if (l && l.owner === "book") { levels(l.kind === "stop" ? { stop: l.price, target: (rs.book.position || rs.book.pending)?.target ?? null } : { stop: (rs.book.position || rs.book.pending)?.stop ?? null, target: l.price }); return; }
      drawLines();
    };
    box.addEventListener("pointerup", done, true);
    box.addEventListener("pointercancel", (e) => { if (rs.drag) { rs.drag = null; const chart = deps.chart(); if (chart) chart.main.applyOptions({ handleScroll: true, handleScale: true }); drawLines(); } }, true);
  }

  // keyboard: Space play/pause, arrows step
  document.addEventListener("keydown", (e) => {
    if (!rs.active || rs.ended || panel.offsetParent === null && bar.offsetParent === null) return;
    const t = /** @type {HTMLElement} */ (e.target);
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (e.key === " ") { e.preventDefault(); toggle(); }
    else if (e.key === "ArrowRight") { e.preventDefault(); pause(); step(1); }
    else if (e.key === "ArrowLeft") { e.preventDefault(); back(); }
  });

  render();
  return {
    /** Whether a replay is running (the chart must not go live meanwhile). */
    active: () => rs.active,
    /** The Replay button: opens or closes the setup. */
    toggleSetup() { if (rs.active) return; rs.setup = !rs.setup; if (!rs.setup) setPicking(false); render(); if (rs.setup) panel.scrollIntoView({ block: "nearest", behavior: "smooth" }); },
    /** A candle was tapped while picking: the replay starts right after it. @param {number} time */
    picked(time) {
      if (!rs.picking) return;
      const tf = deps.tf(), sec = ({ "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800 })[tf] || 86400;
      startFrom(startText(time + sec, ["1d", "1w"].includes(tf)));
    },
    /** The user changed the candle size during a replay: reload at the same moment, keeping the practice book.
     * @param {string} tf */
    async changeTf(tf) {
      if (!rs.active || rs.ended) return false;
      pause();
      const b = lastBar(), oldSec = rs.info ? rs.info.seconds : 0;
      const at = b.time + oldSec;
      rs.loading = true; renderBar();
      try {
        const info = await api("/md/replay?" + new URLSearchParams({ venue: rs.venue, symbol: rs.symbol, tf, start: startText(at, ["1d", "1w"].includes(tf)) }));
        rs.loading = false; rs.tf = tf; deps.setTf(tf);
        const book = rs.book;
        load(info, false);
        rs.book = book; book.last = lastBar() || null;
        say(`Now ${deps.tfLabel(tf)} candles from the same moment. A bigger candle that was still forming shows in full on the next step.`);
        render(); drawLines();
        return true;
      } catch (err) { rs.loading = false; say(`Candle size not changed: ${err.message}`, true); renderBar(); return false; }
    },
    /** The chart was drawn again (theme, chart type): its lines went with the old series. */
    chartDrawn() { rs.lines = []; bound = null; if (rs.active) drawLines(); },
    /** Whether a press at this height is on one of the replay's lines. @param {number} y */
    claims(y) { return rs.active && !rs.ended && rs.lines.length > 0 && hit(y) >= 0; },
    /** Fill the practice ticket from a Long or Short position drawing. @param {{ side: string, entry: number, stoploss: number, target: number, quantity: number }} plan */
    fill(plan) {
      if (!rs.active || rs.ended) return;
      rs.draft.side = plan.side === "BUY" ? "long" : "short";
      if (rs.draft.side === "short") rs.draft.product = "intraday";
      rs.draft.type = "limit"; priceIn.value = String(round(plan.entry));
      qtyIn.value = String(india() ? Math.max(1, Math.floor(plan.quantity)) : plan.quantity);
      rs.draft.stop = round(plan.stoploss); rs.draft.target = round(plan.target);
      stopIn.value = String(rs.draft.stop); targetIn.value = String(rs.draft.target);
      say("Filled from the position drawing. Check it, then place the practice order.");
      renderTicket(); drawLines(); panel.scrollIntoView({ block: "nearest", behavior: "smooth" });
    },
    /** Puts default Stoploss and Target lines on the plan. */
    planLines() { planLines(); renderTicket(); drawLines(); },
    /** Another chart was opened: the replay stops (its trades are saved). */
    abandon() { if (rs.active) exitQuiet(); else if (rs.setup) render(); },
    pause,
    /** The setup panel reflects the chart's symbol and candle size. */
    refresh() { if (rs.setup && !rs.active) renderSetup(); },
  };
}
