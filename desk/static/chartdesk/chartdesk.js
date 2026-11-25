// @ts-check
// The Charts tab: candles, line, area or Heikin Ashi with volume, live prices, any number of indicators (each
// with its own settings and colours) and drawing tools. Loaded only when the tab first opens (app.js imports
// it), so the rest of the desk does not carry it. Drawing uses the bundled Lightweight Charts library
// (Apache-2.0, attribution shown under the chart).
//
// Two presets share this code: "live" (crypto, NSE/BSE from the broker, imported files, watchlist and crypto
// paper trading) and "classic" (NSE/BSE through the desk's /api/chart, which has the 2/3/4/30-minute candles
// and the optional delayed Yahoo fallback).

import { CATALOG, GROUPS, cleanParams, compute as computeIndicator, search } from "./indicators.js";
import { mergeBar, startFeed } from "./feeds.js";
import { DrawingLayer, TOOLS, logicalOf, newPosition, normalise, positionPlan, timeOf } from "./drawings.js";
import { mountWatchlist } from "./watchlist.js";
import { mountPaper } from "./paper.js";
import { mountTrade } from "./trade.js";
import { mountReplay, whenText } from "./replay.js";
import { cleanSettings as cleanPatternSettings, mountPatterns } from "./patternpanel.js";

/** @typedef {import("./indicators.js").Bar} Bar */
/** @typedef {import("./drawings.js").Drawing} Drawing */
/** @typedef {{ uid: number, kind: string, params: Record<string, any>, colors: Record<string, string>, width: number|null, hidden: boolean }} Active */

const LW = /** @type {any} */ (window).LightweightCharts;
const STORE = "desk-chartdesk";
const TYPES = { candles: "Candles", ha: "Heikin Ashi", line: "Line", area: "Area" };
const VENUES = { crypto: "Crypto", india: "NSE / BSE", imported: "Imported" };
const MAX_INDICATORS = 30;
const DEFAULTS = { venue: "crypto", symbol: "BTCUSDT", tf: "1h", type: "candles", volume: true,
  indicators: [{ kind: "ema", params: { period: 20 } }, { kind: "bollinger", params: { period: 20, width: 2 } }, { kind: "rsi", params: { period: 14 } }] };
const CLASSIC_DEFAULTS = { venue: "india", symbol: "", tf: "1d", type: "candles", volume: true,
  indicators: [{ kind: "sma", params: { period: 50 } }, { kind: "rsi", params: { period: 14 } }] };

/** @param {string} tag @param {Record<string, any>} [attrs] @param {...any} children @returns {HTMLElement} */
function h(tag, attrs = {}, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "text") el.textContent = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const child of children.flat()) {
    if (child == null || child === false) continue;
    el.append(child.nodeType ? child : document.createTextNode(String(child)));
  }
  return el;
}

const css = (/** @type {string} */ name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Decimal places for a price: 2 for rupees, more for small crypto prices. @param {string} venue @param {number} price */
export function decimalsFor(venue, price) {
  if (venue !== "crypto" || !(price > 0)) return 2;
  if (price >= 1000) return 2;
  if (price >= 1) return 4;
  return Math.min(10, Math.max(4, Math.ceil(-Math.log10(price)) + 3));
}

/** Line data with blank entries during an indicator's warm-up, so every pane has a point for every bar and the
 * panes' time axes line up. Points after the last bar (lines drawn ahead of price) are kept as they are.
 * @param {Bar[]} bars @param {{time: number, value: number}[]} pts */
export function padded(bars, pts) {
  const at = new Map(pts.map((p) => [p.time, p.value]));
  /** @type {{time: number, value?: number}[]} */ const out = bars.map((b) => (at.has(b.time) ? { time: b.time, value: at.get(b.time) } : { time: b.time }));
  const last = bars.length ? bars[bars.length - 1].time : -Infinity;
  pts.forEach((p) => { if (p.time > last) out.push(p); });
  return out;
}

/** Heikin Ashi candles from real ones. @param {Bar[]} bars @returns {Bar[]} */
export function heikinAshi(bars) {
  /** @type {Bar[]} */ const out = [];
  bars.forEach((b, i) => {
    const close = (b.open + b.high + b.low + b.close) / 4, open = i ? (out[i - 1].open + out[i - 1].close) / 2 : (b.open + b.close) / 2;
    out.push({ time: b.time, open, high: Math.max(b.high, open, close), low: Math.min(b.low, open, close), close, volume: b.volume });
  });
  return out;
}

/** Seconds in a candle size such as 5m, 1h, 1d, 1w. @param {string} tf */
export function secondsOf(tf) {
  const m = /^(\d+)([mhdw])$/.exec(tf || "");
  if (!m) return 86400;
  return Number(m[1]) * { m: 60, h: 3600, d: 86400, w: 604800 }[/** @type {"m"|"h"|"d"|"w"} */ (m[2])];
}

/** The classic chart's /api/chart answer in the live chart's shape. Day candles come as dates; the volume
 * comes as a panel. @param {any} r @param {string} tf */
export function classicInfo(r, tf) {
  const time = (/** @type {any} */ t) => (typeof t === "string" ? Math.floor(Date.parse(t + "T00:00:00Z") / 1000) : t);
  const panel = (r.panels || []).find((/** @type {any} */ p) => p.key === "volume");
  const vol = new Map(panel ? panel.points.map((/** @type {any} */ p) => [time(p.time), p.value]) : []);
  const candles = (r.candles || []).map((/** @type {any} */ c) => ({ time: time(c.time), open: c.open, high: c.high, low: c.low, close: c.close, volume: vol.get(time(c.time)) || 0 }));
  const live = r.live_price, last = candles[candles.length - 1];
  if (last && live > 0 && r.session && r.session.open) { last.close = live; last.high = Math.max(last.high, live); last.low = Math.min(last.low, live); }
  return { candles, source: r.source, symbol: r.symbol, name: r.symbol, session: r.session, session_offset: 0, seconds: secondsOf(tf),
    anchor: 33300, live: { kind: "refetch", every: 15 }, notes: [r.note, r.truncated ? "Showing the most recent 1,500 candles." : null].filter(Boolean) };
}

/** An indicator as saved: known kind, settings checked, colours as #rrggbb, width 1 to 4.
 * @param {any} i @returns {Omit<Active, "uid">|null} */
export function cleanActive(i) {
  if (!i || typeof i !== "object" || !CATALOG[i.kind]) return null;
  const keys = new Set(CATALOG[i.kind].outputs.map((o) => o.key));
  const colors = Object.fromEntries(Object.entries(i.colors && typeof i.colors === "object" ? i.colors : {})
    .filter(([k, v]) => keys.has(k) && typeof v === "string" && /^#[0-9a-f]{6}$/i.test(v)));
  const width = Number(i.width);
  return { kind: i.kind, params: cleanParams(i.kind, i.params), colors, width: width >= 1 && width <= 4 ? Math.round(width) : null, hidden: !!i.hidden };
}

/** A number for an indicator's legend: thousands grouped, fewer decimals when large. @param {number|null|undefined} v */
export function valueText(v) {
  if (v == null || !Number.isFinite(v)) return "–";
  const a = Math.abs(v);
  if (a >= 1e7) return v.toLocaleString("en-US", { maximumFractionDigits: 0, notation: "compact" });
  return v.toLocaleString("en-US", { maximumFractionDigits: a >= 1000 ? 0 : a >= 1 ? 2 : 4, minimumFractionDigits: a >= 1000 ? 0 : a >= 1 ? 2 : 0 });
}

/** @param {string} key */
function loadLocal(key, defaults) {
  try {
    const saved = JSON.parse(localStorage.getItem(key) || "null");
    if (saved && typeof saved === "object") return { ...defaults, ...saved };
  } catch (err) { /* storage blocked or bad */ }
  return { ...defaults };
}

/**
 * @param {HTMLElement} root
 * @param {{ api: (path: string, opts?: any) => Promise<any>, preset?: "live"|"classic", intervals?: Record<string, string>,
 *   mode?: () => string, compact?: boolean, view?: string, seed?: Record<string, any>|null, start?: Record<string, any>|null,
 *   onSymbol?: (venue: string, symbol: string) => void, popOut?: ((at: { venue: string, symbol: string, tf: string }) => void)|null }} deps
 *   ``mode`` is the desk's Paper or Live switch, for NSE/BSE orders from the chart. ``compact`` is one chart of several
 *   in a chart window (grid.js): just the chart, its indicators and drawings, saved under its own ``view``; ``start``
 *   is its settings before it has any of its own, ``seed`` overrides the saved symbol (a popped-out chart), and
 *   ``onSymbol`` hears the stock changing. ``popOut`` adds a "Pop out" button that opens the chart in its own window.
 */
export function mount(root, { api, preset = "live", intervals = {}, mode = () => "paper", compact = false, view = "", seed = null, start = null,
  onSymbol = () => {}, popOut = null }) {
  const classic = preset === "classic";
  const lite = classic || compact;                  // no watchlist, trading, paper book or replay
  const viewName = compact && view ? view : preset;
  const storeKey = compact ? STORE + "-" + viewName : classic ? STORE + "-classic" : STORE;
  const defaults = classic ? CLASSIC_DEFAULTS : { ...DEFAULTS, ...(start || {}) };
  let uid = 0;
  const st = {
    venue: "crypto", symbol: "", tf: "1h", type: "candles", volume: true,
    active: /** @type {Active[]} */ ([]),
    info: /** @type {any} */ (null), bars: /** @type {Bar[]} */ ([]), catalog: /** @type {any} */ (null),
    request: 0, stopFeed: /** @type {null|(() => void)} */ (null), visible: false,
    charts: /** @type {any[]} */ ([]), main: /** @type {any} */ (null), price: /** @type {any} */ (null), vol: /** @type {any} */ (null),
    series: /** @type {Map<number, any[]>} */ (new Map()), fills: /** @type {Map<number, BandFill>} */ (new Map()), frame: 0, decimals: 2,
    results: /** @type {Map<number, Record<string, {time:number, value:number}[]>>|null} */ (null),
    lookup: /** @type {Map<number, Record<string, Map<number, number>>>} */ (new Map()), pendingAll: false, panesLeft: 0,
    busy: false, importMsg: "", drawings: /** @type {Drawing[]} */ ([]), tool: /** @type {string|null} */ (null), clicks: /** @type {any[]} */ ([]),
    layer: /** @type {DrawingLayer|null} */ (null), book: /** @type {any} */ (null), orderLines: /** @type {any[]} */ ([]),
    selected: /** @type {string|null} */ (null), drag: /** @type {any} */ (null),
    layoutReady: /** @type {Promise<void>|null} */ (null), saveTimer: /** @type {any} */ (0), touched: false,
    replaying: false, blind: false, blindLabel: /** @type {null|((t: number, clock: boolean) => string)} */ (null), picking: false,
    patterns: cleanPatternSettings(null),
  };

  /** @param {any} saved */
  function applyLayout(saved) {
    const s = { ...defaults, ...(saved || {}) };
    st.venue = classic ? "india" : s.venue in VENUES ? s.venue : "crypto";
    st.symbol = String(s.symbol ?? defaults.symbol).toUpperCase();
    st.tf = String(s.tf || defaults.tf); st.type = s.type in TYPES ? s.type : "candles"; st.volume = s.volume !== false;
    st.active = (Array.isArray(s.indicators) ? s.indicators : defaults.indicators).slice(0, MAX_INDICATORS)
      .map(cleanActive).filter(Boolean).map((a) => ({ uid: ++uid, .../** @type {any} */ (a) }));
    st.patterns = cleanPatternSettings(s.patterns);
    if (compact) st.patterns = { ...st.patterns, on: false };
  }
  function layout() {
    return { venue: st.venue, symbol: st.symbol, tf: st.tf, type: st.type, volume: st.volume,
      indicators: st.active.map((a) => ({ kind: a.kind, params: a.params, colors: a.colors, width: a.width, hidden: a.hidden })),
      patterns: st.patterns };
  }
  applyLayout({ ...loadLocal(storeKey, defaults), ...(seed || {}) });
  let lastSymbol = st.venue + ":" + st.symbol;

  /** Saves the chart's settings on this device at once and on the desk shortly after, so the desktop app, a
   * phone and a browser open the same chart. */
  function save() {
    st.touched = true;
    const data = layout();
    try { localStorage.setItem(storeKey, JSON.stringify(data)); } catch (err) { /* storage blocked */ }
    clearTimeout(st.saveTimer);
    st.saveTimer = setTimeout(async () => {
      try { await api("/md/layout", { method: "PUT", body: { view: viewName, layout: data } }); }
      catch (err) { showNote(`Chart settings not saved on the desk: ${err.message}`); }
    }, 600);
  }

  function fetchLayout() {
    st.layoutReady = api("/md/layout?" + new URLSearchParams({ view: viewName })).then((r) => {
      if (r && r.layout && !st.touched) applyLayout({ ...r.layout, ...(seed || {}) });
    }).catch((err) => { showNote(`Chart settings could not be loaded from the desk (using this device's): ${err.message}`); });
    return st.layoutReady;
  }

  // -- layout ------------------------------------------------------------------------------------------
  // A chart of several has drop-down lists in place of rows of buttons, so its bar fits a small pane.
  const venueSeg = compact ? h("select", { class: "cd-select", "aria-label": "Market" }) : h("div", { class: "seg", role: "group", "aria-label": "Market", hidden: classic });
  const symbolInput = /** @type {HTMLInputElement} */ (h("input", { name: "cd-symbol", list: `cd-symbols-${viewName}`, autocomplete: "off", spellcheck: "false", "aria-label": "Symbol" }));
  const symbolList = h("datalist", { id: `cd-symbols-${viewName}` });
  const tfSeg = compact ? h("select", { class: "cd-select", "aria-label": "Timeframe" }) : h("div", { class: "seg cd-scroll", role: "group", "aria-label": "Timeframe" });
  const typeSeg = compact ? h("select", { class: "cd-select", "aria-label": "Chart type" }) : h("div", { class: "seg", role: "group", "aria-label": "Chart type" });
  const indicatorBtn = h("button", { type: "button", class: "cd-btn", onclick: () => openPicker() });
  const drawToggle = compact ? h("button", { type: "button", class: "cd-btn", text: "Draw", "aria-pressed": "false", title: "Drawing tools",
    onclick: (/** @type {Event} */ e) => { drawBar.hidden = !drawBar.hidden; /** @type {HTMLElement} */ (e.currentTarget).setAttribute("aria-pressed", String(!drawBar.hidden)); } }) : null;
  const popBtn = popOut ? h("button", { type: "button", class: "cd-btn", text: "Pop out", title: "Open this chart in its own window, where you can also show several charts",
    onclick: () => popOut({ venue: st.venue, symbol: st.blind ? "" : st.symbol, tf: st.tf }) }) : null;
  const patternBtn = h("button", { type: "button", class: "cd-btn", hidden: compact, text: "Patterns", title: "Candle patterns, what followed them on this chart, and a range of likely prices",
    onclick: () => { st.patterns = { ...st.patterns, on: !st.patterns.on }; save(); patterns.changed(); renderControls(); if (st.patterns.on) patternRoot.scrollIntoView({ behavior: "smooth", block: "nearest" }); } });
  const patternRoot = h("section", { class: "panel cd-patterns", hidden: true, "aria-label": "Candle patterns" });
  const replayBtn = lite ? null : h("button", { type: "button", class: "cd-btn", text: "Replay", title: "Practise on past candles, shown one at a time", onclick: () => replay.toggleSetup() });
  const replayBar = h("div", { class: "cd-replaybar", role: "toolbar", "aria-label": "Replay controls", hidden: true });
  const replayRoot = h("section", { class: "panel cd-replay", hidden: true, "aria-label": "Replay practice" });
  const head = h("div", { class: "cd-head" });
  const error = h("div", { class: "error cd-error", role: "alert", hidden: true });
  const note = h("p", { class: "hint", hidden: true });
  const extra = h("p", { class: "hint cd-extra", hidden: true });
  const legend = h("div", { class: "cd-legend", "aria-live": "off" });
  const mainBox = h("div", { class: "cd-main" });
  const panes = h("div", { class: "cd-panes" });
  const drawBar = h("div", { class: "cd-drawbar", role: "group", "aria-label": "Drawing tools", hidden: compact });
  const editBar = h("div", { class: "cd-edit", hidden: true });
  // The editor of a picked drawing floats over the chart, so the chart does not jump when it opens.
  const chartArea = h("div", { class: "cd-area", hidden: true }, drawBar, legend, h("div", { class: "cd-mainwrap" }, mainBox, editBar), panes,
    h("p", { class: "hint cd-credit" }, "Charts drawn with ",
      h("a", { href: "https://www.tradingview.com/", target: "_blank", rel: "noopener noreferrer", text: "TradingView Lightweight Charts" }),
      compact ? "." :
      classic ? ". Candles from your broker, or delayed Yahoo data if you switched that on in Settings. Indicators are worked out by the desk."
        : ". Crypto prices from Binance or Coinbase public data; NSE/BSE from your broker. Indicators are worked out by the desk."));
  const loading = h("p", { class: "hint", text: "Loading…", hidden: true });
  const importPanel = h("div", { class: "cd-import", hidden: true });
  const watchRoot = h("aside", { class: "panel cd-watch", "aria-label": "Watchlist" });
  const paperRoot = h("section", { class: "panel cd-paper", hidden: true, "aria-label": "Paper trading" });
  const tradeRoot = h("section", { class: "panel cd-trade", hidden: true, "aria-label": "Trade from the chart" });
  const dialog = /** @type {HTMLDialogElement} */ (h("dialog", { class: "cd-dialog", "aria-label": "Indicators" }));

  const column = h("div", { class: "cd-col" },
    h("div", { class: "panel cd-panel" },
      h("form", { class: "cd-bar", onsubmit: (/** @type {Event} */ e) => { e.preventDefault(); st.symbol = symbolInput.value.trim().toUpperCase(); open(); } },
        venueSeg,
        h("div", { class: "cd-symbol" }, symbolInput, symbolList, h("button", { class: "primary", type: "submit", text: "Show" })),
        tfSeg, typeSeg, indicatorBtn, drawToggle, patternBtn, replayBtn, popBtn),
      importPanel, replayBar, head, error, note, extra, loading, chartArea),
    compact ? null : patternRoot, lite ? null : replayRoot, lite ? null : tradeRoot, lite ? null : paperRoot);
  root.replaceChildren(compact ? h("div", { class: "cd-compact" }, column, dialog)
    : classic ? h("div", { class: "cd-grid cd-solo" }, column, dialog) : h("div", { class: "cd-grid" }, watchRoot, column, dialog));

  const watch = lite ? { show() {}, hide() {}, render() {} } : mountWatchlist(watchRoot, { api, open: (venue, symbol) => { st.venue = venue; st.symbol = symbol; renderControls(); open(); },
    current: () => ({ venue: st.venue, symbol: st.blind ? "" : st.symbol }) });
  const lastPrice = () => (st.bars.length ? st.bars[st.bars.length - 1].close : null);
  const paper = lite ? { show() {}, hide() {}, symbolChanged() {}, setBook() {} }
    : mountPaper(paperRoot, { api, onBook: (book) => { st.book = book; orderLines(); trade.bookChanged(book); } });
  // Trading from the chart is on the live chart; the classic chart only shows.
  const trade = lite ? { show() {}, hide() {}, symbolChanged() {}, chartDrawn() {}, priceChanged() {}, modeChanged() {}, bookChanged() {}, fill() {}, claims() { return false; } }
    : mountTrade(tradeRoot, { api, venue: () => st.venue, symbol: () => st.symbol, lastPrice, mode,
      decimals: () => st.decimals, money: (v) => money(v), onBook: (book) => paper.setBook(book),
      chart: () => (st.main && st.price ? { main: st.main, price: st.price, box: mainBox } : null) });
  const replay = lite ? { active: () => false, toggleSetup() {}, picked() {}, changeTf: async () => false, chartDrawn() {}, claims() { return false; },
    fill() {}, planLines() {}, abandon() {}, pause() {}, refresh() {} }
    : mountReplay(replayRoot, replayBar, { api, venue: () => st.venue, symbol: () => st.symbol, tf: () => st.tf,
      tfLabel: (tf) => (timeframes()[tf] || tf), decimals: () => st.decimals, money: (v) => money(v),
      chart: () => (st.main && st.price ? { main: st.main, price: st.price, box: mainBox } : null),
      enter: (r) => enterReplay(r), stepped: (all) => schedule(all), leave: (go) => leaveReplay(go),
      setTf: (tf) => { st.tf = tf; renderControls(); },
      pickOnChart: (on) => { st.picking = on; mainBox.style.cursor = on ? "crosshair" : ""; } });

  const patterns = mountPatterns(patternRoot, { api, bars: () => st.bars, info: () => st.info, venue: () => (classic ? "india" : st.venue), tf: () => st.tf,
    replaying: () => st.replaying,
    live: () => !st.replaying && !!st.info && st.venue !== "imported" && (st.venue === "crypto" || !!(st.info.session && st.info.session.open)),
    chart: () => (st.main && st.price ? { main: st.main, price: st.price } : null), settings: () => st.patterns,
    save: (next) => { st.patterns = next; save(); patterns.changed(); },
    format: (v) => (st.venue === "crypto" ? "" : "₹") + num(v), when: (t) => (st.blind && st.blindLabel ? st.blindLabel(t, intraday()) : whenText(t, !intraday())),
    heikinAshi: () => st.type === "ha", busyTool: () => !!(st.tool || st.picking || st.drag) });

  /** A replay shows its candles: no live feed, its own drawings, the trade and paper panels put away.
   * @param {{ info: any, bars: Bar[], blind: boolean, fresh: boolean, label: (t: number, clock: boolean) => string }} r */
  function enterReplay(r) {
    st.request++;                      // a live chart still loading must not replace the replay
    stopLive();
    st.replaying = true; st.blind = r.blind; st.blindLabel = r.label;
    st.info = r.info; st.bars = r.bars;
    if (st.bars.length) st.decimals = decimalsFor(st.venue, st.bars[st.bars.length - 1].close);
    if (r.fresh) { st.drawings = []; st.tool = null; st.clicks = []; st.selected = null; }
    patterns.reset();
    error.hidden = true; loading.hidden = true; chartArea.hidden = false;
    const notes = r.info.notes || [];
    note.textContent = notes.join(" "); note.hidden = !notes.length;
    draw(true);
    setStatus({ state: "closed", text: r.blind ? "Blind replay · not live" : "Replay · not live" });
    renderControls();
  }

  /** Back to the live chart, of the replayed stock when ``go`` says so, with the replay's drawings if kept.
   * @param {{ venue: string, symbol: string, drawings: boolean }} [go] */
  async function leaveReplay(go) {
    const kept = go && go.drawings ? st.drawings.slice() : [];
    st.replaying = false; st.blind = false; st.blindLabel = null;
    if (go) { st.venue = go.venue; st.symbol = go.symbol; }
    if (kept.length) {
      try {
        const live = (await api("/md/drawings?" + new URLSearchParams({ venue: st.venue, symbol: st.symbol }))).items;
        await api("/md/drawings", { method: "PUT", body: { venue: st.venue, symbol: st.symbol, items: [...live, ...kept].slice(-100) } });
      } catch (err) { showNote(`Replay drawings not kept: ${err.message}`); }
    }
    renderControls(); open();
  }

  /** @param {string} text */
  function showNote(text) { extra.textContent = text; extra.hidden = false; }

  function timeframes() { return classic ? intervals : (st.catalog ? st.catalog.timeframes : {}); }

  /** @param {HTMLElement} host @param {Record<string,string>} options @param {string} current @param {(k: string) => void} pick */
  function segment(host, options, current, pick) {
    if (host instanceof HTMLSelectElement) {
      host.replaceChildren(...Object.entries(options).map(([key, label]) => h("option", { value: key, "data-key": key, text: label, selected: key === current })));
      host.value = current; host.onchange = () => pick(host.value);
      return;
    }
    host.replaceChildren(...Object.entries(options).map(([key, label]) => h("button", { type: "button", "data-key": key,
      "aria-pressed": String(key === current), text: label, onclick: () => pick(key) })));
  }
  function renderControls() {
    segment(venueSeg, VENUES, st.venue, (key) => {
      if (key === st.venue) return;
      st.venue = key;
      const imports = st.catalog ? st.catalog.imports : [];
      st.symbol = key === "crypto" ? "BTCUSDT" : key === "india" ? "RELIANCE" : (imports[0] ? imports[0].symbol : "");
      renderControls(); open();
    });
    const tfs = timeframes();
    segment(tfSeg, Object.fromEntries(Object.keys(tfs).map((k) => [k, compact ? tfs[k] : k === "1d" ? "1D" : k === "1w" ? "1W" : k])), st.tf,
      (key) => { if (replay.active()) { replay.changeTf(key); return; } st.tf = key; renderControls(); open(); });
    [...tfSeg.children].forEach((b) => b.setAttribute("title", tfs[/** @type {HTMLElement} */ (b).dataset.key || ""] || ""));
    const meta = st.venue === "imported" && st.catalog ? st.catalog.imports.find((/** @type {any} */ i) => i.symbol === st.symbol) : null;
    if (meta) {
      [...tfSeg.children].forEach((b) => {
        const s = secondsOf(/** @type {HTMLElement} */ (b).dataset.key || "");
        if (s < meta.base_seconds || (s < 86400 && s % meta.base_seconds)) {
          b.setAttribute("disabled", ""); b.setAttribute("title", `This file has ${meta.candle} candles`);
        }
      });
    }
    segment(typeSeg, TYPES, st.type, (key) => { st.type = key; renderControls(); save(); draw(); });
    symbolInput.value = st.blind ? "" : st.symbol;
    symbolInput.placeholder = st.venue === "crypto" ? "BTCUSDT" : "RELIANCE, NIFTY";
    const list = !st.catalog ? [] : st.venue === "crypto" ? st.catalog.crypto.map((/** @type {any} */ c) => [c.symbol, `${c.name} (${c.coin})`])
      : st.venue === "imported" ? st.catalog.imports.map((/** @type {any} */ i) => [i.symbol, `${i.rows} ${i.candle} candles`])
      : st.catalog.indices.map((/** @type {any} */ i) => [i.symbol, i.name]);
    symbolList.replaceChildren(...list.map(([value, label]) => h("option", { value, label })));
    indicatorBtn.textContent = `Indicators${st.active.length ? ` (${st.active.length})` : ""}`;
    patternBtn.setAttribute("aria-pressed", String(st.patterns.on));
    renderDrawBar(); renderEditBar(); renderImport(); watch.render();
    if (!lite) { if (st.visible && st.venue === "crypto" && !st.replaying) paper.show(); else paper.hide(); }
    if (st.visible && st.info && !st.replaying) trade.show(); else trade.hide();
    if (replayBtn) { replayBtn.hidden = st.replaying; replayBtn.setAttribute("aria-pressed", String(!replayRoot.hidden)); }
    replay.refresh();
  }

  // -- indicators: picker and settings ---------------------------------------------------------------
  /** @param {Active} a */
  function label(a) {
    const c = CATALOG[a.kind], spec = c.params;
    const values = Object.entries(a.params).filter(([k]) => !spec[k].options || k === "type").map(([k, v]) => (spec[k].options ? spec[k].options[v] : v));
    return c.short + (values.length ? " " + values.join(", ") : "");
  }

  function changed() { save(); renderControls(); draw(); if (dialog.open && dialog.dataset.view === "picker") renderPicker(); }

  /** @param {string} kind */
  function add(kind) {
    if (st.active.length >= MAX_INDICATORS) return false;
    st.active.push({ uid: ++uid, kind, params: cleanParams(kind, {}), colors: {}, width: null, hidden: false });
    changed();
    return true;
  }

  let query = "";
  function openPicker() { dialog.dataset.view = "picker"; renderPicker(); if (!dialog.open) dialog.showModal(); const s = dialog.querySelector("input[type=search]"); if (s) /** @type {HTMLInputElement} */ (s).focus(); }

  function renderPicker() {
    const results = h("div", { class: "cd-pick-list" });
    const item = (/** @type {string} */ k) => {
      const c = CATALOG[k], count = st.active.filter((a) => a.kind === k).length;
      return h("button", { type: "button", class: "cd-pick", title: c.about, onclick: (/** @type {Event} */ e) => {
        if (!add(k)) { status.textContent = `A chart holds ${MAX_INDICATORS} indicators at most. Remove one first.`; return; }
        status.textContent = `Added ${c.name}.`;
        /** @type {HTMLElement} */ (e.currentTarget).querySelector(".cd-pick-count")?.replaceChildren(String(count + 1) + " on chart");
      } }, h("span", { class: "cd-pick-name" }, h("strong", { text: c.name }), " ", h("span", { class: "hint", text: c.short })),
      h("span", { class: "cd-pick-about", text: c.about }), h("span", { class: "cd-pick-count", text: count ? `${count} on chart` : "" }));
    };
    const fill = () => {
      const found = search(query);
      if (query.trim()) {          // a search lists the best matches first
        results.replaceChildren(found.length ? h("section", {}, h("h4", { text: `${found.length} found` }), found.map(item))
          : h("p", { class: "hint", text: `No indicator matches “${query}”.` }));
        return;
      }
      results.replaceChildren(...Object.entries(GROUPS).map(([g, name]) => {
        const kinds = found.filter((k) => CATALOG[k].group === g);
        return h("section", {}, h("h4", { text: `${name} (${kinds.length})` }), kinds.map(item));
      }));
    };
    const status = h("p", { class: "hint cd-pick-status", role: "status" });
    const input = /** @type {HTMLInputElement} */ (h("input", { type: "search", value: query, placeholder: "Search: RSI, moving average, volume…", "aria-label": "Search indicators",
      oninput: (/** @type {Event} */ e) => { query = /** @type {HTMLInputElement} */ (e.target).value; fill(); } }));
    fill();
    const onChart = st.active.length ? h("div", { class: "cd-onchart" }, st.active.map((a) => h("div", { class: "cd-onchart-row" + (a.hidden ? " off" : "") },
      h("span", { class: "cd-onchart-name", text: label(a) }),
      h("button", { type: "button", class: "cd-mini", text: "Settings", onclick: () => openSettings(a.uid) }),
      h("button", { type: "button", class: "cd-mini", text: a.hidden ? "Show" : "Hide", onclick: () => { a.hidden = !a.hidden; changed(); } }),
      h("button", { type: "button", class: "cd-x", "aria-label": `Remove ${CATALOG[a.kind].name}`, text: "×", onclick: () => { st.active = st.active.filter((x) => x.uid !== a.uid); changed(); } }))))
      : h("p", { class: "hint", text: "None yet. Pick from the list below; add the same one again for another period." });
    dialog.replaceChildren(
      h("div", { class: "cd-dialog-head" }, h("h3", { text: "Indicators" }), h("button", { type: "button", class: "cd-x", "aria-label": "Close", text: "×", onclick: () => dialog.close() })),
      h("div", { class: "lbl", text: `On this chart (${st.active.length} of ${MAX_INDICATORS})` }), onChart,
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: st.volume, onchange: (/** @type {Event} */ e) => { st.volume = /** @type {HTMLInputElement} */ (e.target).checked; changed(); } }), "Volume bars on the price chart"),
      h("div", { class: "lbl", text: `Add (${Object.keys(CATALOG).length} indicators)` }), input, status, results);
  }

  /** @param {number} id */
  function openSettings(id) {
    const a = st.active.find((x) => x.uid === id);
    if (!a) return;
    dialog.dataset.view = "settings";
    const c = CATALOG[a.kind], colours = defaultColours(a);
    const field = (/** @type {string} */ key, /** @type {any} */ p) => {
      if (p.options) {
        return h("label", {}, p.label, h("select", { onchange: (/** @type {Event} */ e) => { a.params[key] = /** @type {HTMLSelectElement} */ (e.target).value; changed(); } },
          Object.entries(p.options).map(([v, text]) => h("option", { value: v, text, selected: a.params[key] === v }))));
      }
      return h("label", {}, p.label, h("input", { type: "number", inputmode: "decimal", value: String(a.params[key]), min: String(p.min), max: String(p.max), step: String(p.step || 1),
        onchange: (/** @type {Event} */ e) => {
          const el = /** @type {HTMLInputElement} */ (e.target), v = Number(el.value);
          if (el.value === "" || !Number.isFinite(v) || v < p.min || v > p.max) { el.value = String(a.params[key]); msg.textContent = `${p.label} must be from ${p.min} to ${p.max}.`; return; }
          msg.textContent = ""; a.params = cleanParams(a.kind, { ...a.params, [key]: v }); el.value = String(a.params[key]); changed();
        } }));
    };
    const msg = h("p", { class: "error-text", role: "alert" });
    const lines = c.outputs.map((o) => h("label", { class: "cd-colour" }, h("input", { type: "color", value: a.colors[o.key] || colours[o.key],
      oninput: (/** @type {Event} */ e) => { a.colors[o.key] = /** @type {HTMLInputElement} */ (e.target).value; save(); draw(); } }), o.label));
    dialog.replaceChildren(
      h("div", { class: "cd-dialog-head" }, h("h3", { text: c.name }), h("button", { type: "button", class: "cd-x", "aria-label": "Close", text: "×", onclick: () => dialog.close() })),
      h("p", { class: "hint", text: c.about + (c.repaints ? " Its newest part can change as candles come in." : "") }),
      Object.keys(c.params).length ? h("div", { class: "cd-fields" }, Object.entries(c.params).map(([k, p]) => field(k, p))) : h("p", { class: "hint", text: "This indicator has no settings." }),
      msg,
      h("div", { class: "lbl", text: "Colours" }), h("div", { class: "cd-colours" }, lines),
      h("label", { class: "cd-width" }, "Line thickness", h("select", { onchange: (/** @type {Event} */ e) => { const v = Number(/** @type {HTMLSelectElement} */ (e.target).value); a.width = v || null; save(); draw(); } },
        h("option", { value: "0", text: "As designed", selected: a.width == null }), [1, 2, 3, 4].map((w) => h("option", { value: String(w), text: String(w), selected: a.width === w })))),
      h("div", { class: "cd-dialog-foot" },
        h("button", { type: "button", text: "Reset to defaults", onclick: () => { a.params = cleanParams(a.kind, {}); a.colors = {}; a.width = null; changed(); openSettings(id); } }),
        h("button", { type: "button", text: "Remove", onclick: () => { st.active = st.active.filter((x) => x.uid !== id); changed(); openPicker(); } }),
        h("button", { type: "button", text: "All indicators", onclick: () => openPicker() }),
        h("button", { type: "button", class: "primary", text: "Done", onclick: () => dialog.close() })));
    if (!dialog.open) dialog.showModal();
  }

  // -- colours -----------------------------------------------------------------------------------------
  const palette = () => [1, 2, 3, 4, 5, 6].map((i) => css("--s" + i));

  /** Each line's colour when the user has not picked one: the price chart's indicators take turns through
   * the palette; one in its own pane starts it again. @param {Active} a @returns {Record<string, string>} */
  function defaultColours(a) {
    const c = CATALOG[a.kind], pal = palette();
    let base = 0;
    if (c.pane === "price") for (const x of st.active) { if (x.uid === a.uid) break; if (CATALOG[x.kind].pane === "price") base++; }
    return Object.fromEntries(c.outputs.map((o) => {
      const k = o.colour ?? 0;
      return [o.key, k === -1 ? css("--good") : k === -2 ? css("--bad") : pal[(base + k) % pal.length]];
    }));
  }

  // -- drawings ----------------------------------------------------------------------------------------
  function renderDrawBar() {
    /** @type {Record<string, string[]>} */ const groups = {};
    Object.entries(TOOLS).forEach(([k, t]) => { (groups[t.group] = groups[t.group] || []).push(k); });
    const tool = st.tool ? TOOLS[/** @type {keyof TOOLS} */ (st.tool)] : null;
    const hint = !tool ? (st.drawings.length ? "Tap a drawing to move, recolour or delete it." : "")
      : st.clicks.length ? `Point ${st.clicks.length + 1} of ${tool.clicks}: ${tool.hint}` : tool.hint;
    drawBar.replaceChildren(
      ...Object.entries(groups).map(([g, kinds]) => h("div", { class: "cd-toolgroup", role: "group", "aria-label": g },
        kinds.map((k) => h("button", { type: "button", "aria-pressed": String(st.tool === k), text: TOOLS[/** @type {keyof TOOLS} */ (k)].label,
          onclick: () => { st.tool = st.tool === k ? null : k; st.clicks = []; select(null); if (st.layer) { st.layer.preview = null; st.layer.update(); } renderDrawBar(); } })))),
      h("div", { class: "cd-toolgroup" },
        h("button", { type: "button", text: "Undo", disabled: !st.drawings.length, onclick: () => { st.drawings.pop(); select(null); saveDrawings(); } }),
        h("button", { type: "button", text: "Clear all", disabled: !st.drawings.length, onclick: () => { st.drawings = []; select(null); saveDrawings(); } })),
      h("span", { class: "hint cd-draw-hint", text: hint }));
  }

  /** @param {string|null} id */
  function select(id) {
    st.selected = id;
    if (st.layer) { st.layer.selected = id; st.layer.update(); }
    renderEditBar();
  }

  const money = (/** @type {number} */ v) => {
    const text = v.toLocaleString(st.venue === "crypto" ? "en-US" : "en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
    return st.venue === "crypto" ? text : "₹" + text;
  };

  function renderEditBar() {
    const d = st.drawings.find((x) => x.id === st.selected);
    editBar.hidden = !d;
    if (!d) { editBar.replaceChildren(); return; }
    const idx = st.drawings.indexOf(d), tool = TOOLS[/** @type {keyof TOOLS} */ (d.kind)];
    const colour = d.color || (st.layer ? st.layer.color(d, idx) : "#888888");
    const parts = [h("strong", { text: tool ? tool.label : d.kind }),
      h("label", { class: "cd-colour" }, h("input", { type: "color", value: /^#[0-9a-f]{6}$/i.test(colour) ? colour : "#888888",
        oninput: (/** @type {Event} */ e) => { d.color = /** @type {HTMLInputElement} */ (e.target).value; if (st.layer) st.layer.update(); },
        onchange: () => saveDrawings() }), "Colour")];
    if (d.kind === "long" || d.kind === "short") {
      const plan = positionPlan(d, st.bars.length ? st.bars[st.bars.length - 1].close : null);
      const dp = st.decimals;
      const box = (/** @type {string} */ name, /** @type {number} */ index, /** @type {number} */ value) => h("label", {}, name, h("input", { type: "number", inputmode: "decimal", step: "any", min: "0", value: value.toFixed(dp),
        onchange: (/** @type {Event} */ e) => {
          const v = Number(/** @type {HTMLInputElement} */ (e.target).value);
          if (!(v > 0)) { renderEditBar(); return; }
          if (index === 3) d.qty = v; else d.points[index] = { ...d.points[index], price: v };
          const fixed = normalise(d); d.points = fixed.points;
          saveDrawings(); renderEditBar();
        } }));
      const rr = plan.riskReward == null ? "–" : plan.riskReward.toFixed(2);
      parts.push(box("Entry", 0, plan.entry), box("Stoploss", 2, plan.stoploss), box("Target", 1, plan.target),
        h("label", {}, "Quantity", h("input", { type: "number", inputmode: "decimal", step: "any", min: "0", value: String(plan.quantity),
          onchange: (/** @type {Event} */ e) => { const v = Number(/** @type {HTMLInputElement} */ (e.target).value); if (v > 0) { d.qty = v; saveDrawings(); } renderEditBar(); } })),
        h("span", { class: "cd-plan" },
          h("span", { class: "down", text: `Risk ${money(plan.risk)} (${Math.abs(plan.riskPct).toFixed(2)}%)` }), " · ",
          h("span", { class: "up", text: `Reward ${money(plan.reward)} (${Math.abs(plan.rewardPct).toFixed(2)}%)` }), ` · Risk : Reward 1 : ${rr}`,
          plan.now == null ? "" : ` · At last price ${plan.now >= 0 ? "+" : "−"}${money(Math.abs(plan.now))}`));
      if (st.replaying) {
        parts.push(h("button", { type: "button", class: "primary", text: d.kind === "long" ? "Practise buy with this plan" : "Practise short sale with this plan",
          title: "Fill the practice ticket with this entry, stoploss, target and quantity", onclick: () => replay.fill(plan) }));
      } else if (!lite && (st.venue === "india" || st.venue === "crypto")) {
        const shortOk = d.kind === "long" || st.venue === "india";
        parts.push(h("button", { type: "button", class: "primary", text: d.kind === "long" ? "Buy with this plan" : "Short sell with this plan",
          title: shortOk ? "Fill the trade ticket below with this entry, stoploss, target and quantity" : "Crypto paper trading has no short selling",
          disabled: !shortOk, onclick: () => trade.fill(plan) }));
      }
    }
    parts.push(h("button", { type: "button", class: "cd-del", text: "Delete", onclick: () => { st.drawings = st.drawings.filter((x) => x !== d); select(null); saveDrawings(); } }),
      h("button", { type: "button", text: "Done", onclick: () => select(null) }));
    editBar.replaceChildren(...parts);
  }

  /** The chart point (bar time and price) under a spot on the price chart. @param {number} x @param {number} y */
  function pointAtXY(x, y) {
    if (!st.price || !st.info || !st.main) return null;
    const price = st.price.coordinateToPrice(y), logical = st.main.timeScale().coordinateToLogical(x);
    if (price == null || !(price > 0) || logical == null) return null;
    return { time: timeOf(st.bars, logical, st.info.seconds), price, logical };
  }

  /** @param {PointerEvent|MouseEvent} e */
  function local(e) { const r = mainBox.getBoundingClientRect(); return { x: e.clientX - r.left, y: e.clientY - r.top, w: r.width }; }
  const onScale = (/** @type {number} */ x, /** @type {number} */ w) => !st.main || x > w - st.main.priceScale("right").width();

  // Taps are read from the chart box directly: the library drops a second click that comes quickly after
  // the first (it waits to see a double click), which loses the second point of a line. A press on a
  // drawing starts dragging it, and the chart does not pan meanwhile.
  let down = /** @type {{x: number, y: number}|null} */ (null);
  mainBox.addEventListener("pointerdown", (e) => {
    down = { x: e.clientX, y: e.clientY };
    if (st.tool || !st.layer || !st.main) return;
    const p = local(e);
    if (onScale(p.x, p.w) || trade.claims(p.y) || replay.claims(p.y)) return;       // an order line takes the press (trade.js / replay.js drag it)
    const hit = st.layer.hit(p.x, p.y, e.pointerType === "touch" ? 14 : 7);
    if (!hit) return;
    const d = st.drawings.find((x) => x.id === hit.id), at = pointAtXY(p.x, p.y);
    if (!d || !at) return;
    st.drag = { d, handle: hit.handle, at, orig: d.points.map((q) => ({ ...q })), moved: false };
    select(d.id);
    st.main.applyOptions({ handleScroll: false, handleScale: false });
    e.preventDefault(); e.stopPropagation();
  }, true);
  ["mousedown", "touchstart", "touchmove"].forEach((type) => mainBox.addEventListener(type, (e) => {
    if (st.drag) { e.stopPropagation(); if (type === "touchmove" && e.cancelable) e.preventDefault(); }
  }, { capture: true, passive: false }));
  window.addEventListener("pointermove", (e) => {
    if (!st.drag) { hover(e); return; }
    const p = local(e), at = pointAtXY(p.x, Math.max(0, Math.min(p.y, mainBox.clientHeight)));
    if (!at) return;
    dragTo(st.drag, at);
    st.drag.moved = true;
    if (st.layer) st.layer.update();
  });
  window.addEventListener("pointerup", (e) => {
    if (st.drag) {
      const drag = st.drag; st.drag = null;
      if (st.main) st.main.applyOptions({ handleScroll: true, handleScale: true });
      if (drag.moved) { drag.d.points = normalise(drag.d).points; saveDrawings(); }
      renderEditBar();
      down = null;
      return;
    }
    if (!down || !st.main || !mainBox.contains(/** @type {Node} */ (e.target))) { down = null; return; }
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    down = null;
    if (moved > 6) return;                              // a drag pans the chart
    const p = local(e);
    if (onScale(p.x, p.w)) return;
    if (st.picking && !st.tool) { const at = pointAtXY(p.x, p.y); if (at) replay.picked(at.time); return; }
    if (st.tool) { place(p.x, p.y); return; }
    if (st.selected) select(null);
  });
  document.addEventListener("keydown", (e) => {
    if (!st.selected || !st.visible || root.offsetParent === null) return;
    if (compact && !root.contains(document.activeElement) && !root.matches(":hover")) return;   // another chart's key
    const t = /** @type {HTMLElement} */ (e.target);
    if (t && (t.tagName === "INPUT" || t.tagName === "SELECT" || t.tagName === "TEXTAREA" || t.isContentEditable)) return;
    if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); st.drawings = st.drawings.filter((x) => x.id !== st.selected); select(null); saveDrawings(); }
    else if (e.key === "Escape") select(null);
  });

  /** @param {PointerEvent} e */
  function hover(e) {
    if (!st.layer || st.tool || !mainBox.contains(/** @type {Node} */ (e.target))) return;
    const p = local(e);
    if (trade.claims(p.y) || replay.claims(p.y)) return;
    const hit = st.layer.hit(p.x, p.y);
    mainBox.style.cursor = hit ? (hit.handle >= 0 ? "grab" : "move") : "";
  }

  /** Moves a dragged drawing: a handle moves its own point; the body moves the whole drawing.
   * @param {any} drag @param {{time: number, price: number, logical: number}} at */
  function dragTo(drag, at) {
    const d = /** @type {Drawing} */ (drag.d), o = /** @type {any[]} */ (drag.orig), sec = st.info.seconds;
    if (drag.handle < 0) {
      const dl = at.logical - drag.at.logical, dp = at.price - drag.at.price;
      if (o.some((q) => q.price + dp <= 0)) return;
      d.points = o.map((q) => ({ time: timeOf(st.bars, logicalOf(st.bars, q.time, sec) + dl, sec), price: q.price + dp }));
      return;
    }
    const pts = o.map((q) => ({ ...q })), i = drag.handle;
    if (d.kind === "hline") pts[0] = { time: o[0].time, price: at.price };
    else if (d.kind === "vline") pts[0] = { time: at.time, price: o[0].price };
    else if (d.kind === "long" || d.kind === "short") {
      if (i === 0) { pts[0] = { time: at.time, price: at.price }; pts[2] = { time: at.time, price: o[2].price }; }
      else if (i === 1) pts[1] = { time: at.time, price: at.price };
      else pts[2] = { time: o[0].time, price: at.price };
    } else pts[i] = { time: at.time, price: at.price };
    d.points = pts;
  }

  /** A tap while a tool is on: the next point of the drawing. @param {number} x @param {number} y */
  function place(x, y) {
    const tool = st.tool ? TOOLS[/** @type {keyof TOOLS} */ (st.tool)] : null;
    const pt = pointAtXY(x, y);
    if (!tool || !pt || !st.info) return;
    const at = { time: pt.time, price: pt.price }, last = st.clicks[st.clicks.length - 1];
    if (last && last.time === at.time && Math.abs(last.price - at.price) < 1e-9 * at.price) return;
    st.clicks.push(at);
    if (st.clicks.length < tool.clicks) { renderDrawBar(); return; }
    const kind = /** @type {string} */ (st.tool);
    const points = kind === "long" || kind === "short" ? newPosition(kind, at, st.info.seconds) : st.clicks.slice();
    /** @type {Drawing} */ const d = { id: Math.random().toString(36).slice(2, 10), kind, points };
    if (kind === "long" || kind === "short") d.qty = 1;
    st.drawings.push(d);
    st.clicks = []; st.tool = null;
    if (st.layer) st.layer.preview = null;
    saveDrawings();
    if (kind === "long" || kind === "short") select(d.id);
  }

  /** @param {any} param */
  function chartMove(param) {
    if (!st.layer || !st.tool || !st.clicks.length || !param || !param.point) return;
    const pt = pointAtXY(param.point.x, param.point.y);
    st.layer.preview = pt ? { id: "preview", kind: st.tool, points: [...st.clicks, { time: pt.time, price: pt.price }] } : null;
    st.layer.update();
  }

  async function saveDrawings() {
    if (st.layer) { st.layer.items = st.drawings; st.layer.update(); }
    renderDrawBar(); renderEditBar();
    if (st.replaying) return;           // replay drawings stay with the replay (kept on the live chart only if asked)
    try { await api("/md/drawings", { method: "PUT", body: { venue: st.venue, symbol: st.symbol, items: st.drawings } }); }
    catch (err) { setStatus({ state: "error", text: `Drawings not saved: ${err.message}` }); }
  }

  /** @param {number} request */
  async function loadDrawings(request) {
    try {
      const items = (await api("/md/drawings?" + new URLSearchParams({ venue: st.venue, symbol: st.symbol }))).items;
      if (request !== st.request) return;
      st.drawings = items;
    } catch (err) {
      st.drawings = [];
      showNote(`Saved drawings could not be loaded: ${err.message}`);
    }
    if (st.layer) { st.layer.items = st.drawings; st.layer.update(); }
    renderDrawBar(); renderEditBar();
  }

  // -- paper orders on the chart -----------------------------------------------------------------------
  function orderLines() {
    st.orderLines.forEach((line) => { try { st.price && st.price.removePriceLine(line); } catch (err) { /* chart redrawn */ } });
    st.orderLines = [];
    if (!st.price || !st.book || st.venue !== "crypto") return;
    const pos = st.book.positions.find((/** @type {any} */ p) => p.symbol === st.symbol);
    if (pos) st.orderLines.push(st.price.createPriceLine({ price: pos.avg, color: css("--accent"), lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: true, title: "Paper average" }));
    st.book.orders.filter((/** @type {any} */ o) => o.symbol === st.symbol).forEach((/** @type {any} */ o) => st.orderLines.push(st.price.createPriceLine({
      price: o.price, color: o.side === "BUY" ? css("--good") : css("--bad"), lineWidth: 1, lineStyle: LW.LineStyle.Dotted, axisLabelVisible: true,
      title: `Paper ${o.side === "BUY" ? "buy" : "sell"} limit` })));
  }

  // -- imported files ----------------------------------------------------------------------------------
  function renderImport() {
    importPanel.hidden = st.venue !== "imported";
    if (importPanel.hidden) return;
    const imports = st.catalog ? st.catalog.imports : [];
    const nameIn = /** @type {HTMLInputElement} */ (h("input", { name: "cd-import-name", placeholder: "RELIANCE", autocomplete: "off", "aria-label": "Name for the import" }));
    const fileIn = /** @type {HTMLInputElement} */ (h("input", { type: "file", accept: ".csv,text/csv,text/plain", "aria-label": "CSV file" }));
    const result = h("p", { class: "hint", hidden: !st.importMsg, text: st.importMsg });
    fileIn.addEventListener("change", () => { const f = fileIn.files && fileIn.files[0]; if (f && !nameIn.value) nameIn.value = f.name.replace(/\.[^.]+$/, "").replace(/[^A-Za-z0-9&._-]/g, "").slice(0, 30).toUpperCase(); });
    const go = async (/** @type {Event} */ e) => {
      e.preventDefault();
      const f = fileIn.files && fileIn.files[0];
      result.hidden = false; result.className = "hint";
      if (!f) { result.className = "error"; result.textContent = "Choose a CSV file first."; return; }
      if (f.size > 5_000_000) { result.className = "error"; result.textContent = "The file is larger than 5 MB."; return; }
      result.textContent = "Importing…";
      try {
        const meta = await api("/md/imports", { method: "POST", body: { symbol: nameIn.value, text: await f.text() } });
        st.catalog = await api("/md/catalog");
        st.symbol = meta.symbol;
        const dropped = meta.dropped ? ` ${meta.dropped} bad row(s) skipped (lines ${meta.dropped_lines.join(", ")}${meta.dropped > 5 ? ", …" : ""}).` : "";
        st.importMsg = `Imported ${meta.rows.toLocaleString("en-IN")} ${meta.candle} candles, ${meta.first.slice(0, 10)} to ${meta.last.slice(0, 10)} (dates read ${meta.dates}).${dropped}`;
        renderControls(); open();
      } catch (err) { result.className = "error"; result.textContent = `Not imported: ${err.message}`; }
    };
    result.classList.add("cd-import-result");
    importPanel.replaceChildren(
      h("form", { class: "cd-import-form", onsubmit: go },
        h("label", {}, "CSV file", fileIn), h("label", {}, "Name", nameIn), h("button", { type: "submit", class: "primary", text: "Import CSV" })),
      h("p", { class: "hint", text: "Columns: Date (or Date and Time), Open, High, Low, Close, and Volume if you have it. Times are read as India time. For NSE/BSE charts without a broker; imported prices are never live." }),
      result,
      h("div", { class: "cd-chips" }, imports.length ? h("div", { class: "cd-chips" }, imports.map((/** @type {any} */ i) => h("span", { class: "cd-chip" + (i.symbol === st.symbol ? " on" : "") },
        h("button", { type: "button", class: "cd-chip-open", text: i.symbol, title: `${i.rows} ${i.candle} candles`, onclick: () => { st.symbol = i.symbol; renderControls(); open(); } }),
        h("button", { type: "button", class: "cd-x", "aria-label": `Delete import ${i.symbol}`, text: "×", onclick: async () => {
          try { await api(`/md/imports/${encodeURIComponent(i.symbol)}`, { method: "DELETE" }); st.catalog = await api("/md/catalog");
            if (st.symbol === i.symbol) st.symbol = st.catalog.imports[0] ? st.catalog.imports[0].symbol : "";
            renderControls(); open();
          } catch (err) { result.hidden = false; result.className = "error"; result.textContent = `Not deleted: ${err.message}`; }
        } })))) : null));
  }

  // -- data --------------------------------------------------------------------------------------------
  function fetchCandles() {
    if (classic) return api("/chart?" + new URLSearchParams({ symbol: st.symbol, interval: st.tf, panels: "volume" })).then((r) => classicInfo(r, st.tf));
    return api("/md/candles?" + new URLSearchParams({ venue: st.venue, symbol: st.symbol, tf: st.tf }));
  }

  /** @param {string} message @param {boolean} [suggestImport] */
  function showError(message, suggestImport) {
    error.replaceChildren(h("span", {}, h("strong", { text: "Chart not shown. " }), message, " ",
      h("button", { type: "button", class: "cd-retry", text: "Try again", onclick: () => open() }),
      suggestImport ? h("button", { type: "button", class: "cd-retry", text: "Import a CSV instead",
        onclick: () => { st.venue = "imported"; st.symbol = st.catalog && st.catalog.imports[0] ? st.catalog.imports[0].symbol : ""; renderControls(); open(); } }) : null));
    error.hidden = false;
  }

  async function open() {
    if (replay.active()) { replay.abandon(); st.replaying = false; st.blind = false; st.blindLabel = null; }
    const request = ++st.request;
    st.busy = true;
    stopLive();
    error.hidden = true; loading.hidden = false;
    try {
      if (st.layoutReady) { await st.layoutReady; st.layoutReady = null; if (request !== st.request) return; renderControls(); }
      if (!st.catalog) {
        st.catalog = await api("/md/catalog");
        renderControls();
      }
      const tfs = timeframes();
      if (!(st.tf in tfs)) { st.tf = classic ? "1d" : "1h"; renderControls(); }
      save();
      if (st.venue === "imported" && st.symbol) {
        // An import has one candle size; a smaller one cannot be shown, so step up to the first that can.
        const meta = st.catalog.imports.find((/** @type {any} */ i) => i.symbol === st.symbol);
        const fits = (/** @type {string} */ tf) => secondsOf(tf) >= meta.base_seconds && (secondsOf(tf) >= 86400 || secondsOf(tf) % meta.base_seconds === 0);
        if (meta && !fits(st.tf)) { st.tf = Object.keys(tfs).find(fits) || "1d"; renderControls(); }
      }
      if (!st.symbol) {
        loading.hidden = true; chartArea.hidden = true; destroy(); st.bars = []; st.info = null; head.replaceChildren(); note.hidden = true;
        if (classic) { note.textContent = "Type a symbol (RELIANCE, TCS, NIFTY) and press Show."; note.hidden = false; }
        renderControls();
        return;
      }
      if (st.venue !== "imported") st.importMsg = "";
      const info = await fetchCandles();
      if (request !== st.request) return;
      if (!Array.isArray(info.candles) || !info.candles.length) throw new Error(`${info.source || "The provider"} sent no candles.`);
      st.info = info; st.bars = info.candles.map((/** @type {Bar} */ c) => ({ ...c }));
      st.decimals = decimalsFor(st.venue, st.bars[st.bars.length - 1].close);
      loading.hidden = true; chartArea.hidden = false;
      const notes = info.notes || [];
      note.textContent = notes.join(" "); note.hidden = !notes.length;
      st.drawings = []; st.tool = null; st.clicks = []; st.selected = null;
      patterns.reset();
      draw(true);
      startLive();
      renderControls(); paper.symbolChanged(); trade.symbolChanged();
      loadDrawings(request);
      const now = st.venue + ":" + st.symbol;
      if (now !== lastSymbol) { lastSymbol = now; onSymbol(st.venue, st.symbol); }
    } catch (err) {
      if (request !== st.request) return;
      loading.hidden = true; chartArea.hidden = true; destroy(); st.bars = []; st.info = null;
      head.replaceChildren(); note.hidden = true;
      const payload = /** @type {any} */ (err).payload || {};
      showError(err instanceof Error ? err.message : String(err), !classic && st.venue === "india" && !!payload.needs_login);
      renderControls();
    } finally {
      if (request === st.request) st.busy = false;
    }
  }

  function stopLive() { if (st.stopFeed) { st.stopFeed(); st.stopFeed = null; } }

  function startLive() {
    if (!st.visible || !st.info) return;
    const info = st.info, request = st.request;
    st.stopFeed = startFeed(info, {
      bars: st.bars,
      onChange: () => schedule(),
      onBars: (rows) => { if (request !== st.request) return; rows.forEach((r) => mergeBar(st.bars, { ...r })); schedule(true); },
      onStatus: (s) => { if (request === st.request) setStatus(s); },
      refetch: () => fetchCandles(),
      quote: () => api("/md/quote?" + new URLSearchParams({ symbol: st.symbol })),
    });
  }

  // -- drawing the chart -------------------------------------------------------------------------------
  /** Numbers in Indian grouping for rupees (1,22,000), international for crypto (122,000). @param {number} v @param {number} [dp] */
  function num(v, dp = st.decimals) {
    return v.toLocaleString(st.venue === "crypto" ? "en-US" : "en-IN", { minimumFractionDigits: Math.min(dp, 2), maximumFractionDigits: dp });
  }

  /** @param {number} height @param {boolean} [logo] the library's logo, shown once on the price chart @param {boolean} [price] */
  function options(height, logo = false, price = true) {
    const d = st.decimals;
    const blind = st.blind && st.blindLabel ? /** @type {(t: number, clock: boolean) => string} */ (st.blindLabel) : null;
    const hidden = blind ? { timeFormatter: (/** @type {number} */ t) => blind(t, intraday()) } : {};
    const ticks = blind ? { tickMarkFormatter: (/** @type {number} */ t, /** @type {number} */ type) => (type >= 3 ? new Date(t * 1000).toISOString().slice(11, 16) : blind(t, false)) } : {};
    return { height, autoSize: true,
      layout: { background: { color: css("--surface") }, textColor: css("--text2"), fontSize: 12, attributionLogo: logo },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line"), minimumWidth: window.innerWidth < 720 ? 84 : 104 },   // the same width on every pane keeps the time axes aligned
      timeScale: { borderColor: css("--line"), timeVisible: !["1d", "1w"].includes(st.tf), secondsVisible: false, rightOffset: 4, ...ticks },
      crosshair: { mode: LW.CrosshairMode.Normal },
      localization: { ...hidden, priceFormatter: price ? (/** @type {number} */ p) => p.toLocaleString(st.venue === "crypto" ? "en-US" : "en-IN", { minimumFractionDigits: d, maximumFractionDigits: d }) : valueText } };
  }

  function destroy() {
    st.charts.forEach((c) => c.remove());
    st.charts = []; st.series = new Map(); st.fills = new Map(); st.main = st.price = st.vol = null; st.layer = null; st.orderLines = [];
    panes.replaceChildren();
  }

  const intraday = () => !["1d", "1w"].includes(st.tf);
  /** @param {Active} a */
  const shown = (a) => !a.hidden && (!CATALOG[a.kind].intraday || intraday());

  /** @param {boolean} [fit] */
  function draw(fit) {
    if (!st.info || !st.bars.length) return;
    const keep = !fit && st.main ? st.main.timeScale().getVisibleLogicalRange() : null;
    destroy();
    const up = css("--good"), down = css("--bad"), accent = css("--accent");
    const main = LW.createChart(mainBox, options(mainBox.clientHeight || 420, true));
    st.main = main; st.charts.push(main);
    const ownPanes = st.active.filter((a) => shown(a) && CATALOG[a.kind].pane === "own").length;
    st.panesLeft = ownPanes;
    if (ownPanes) main.timeScale().applyOptions({ visible: false });   // only the bottom chart shows dates
    const fmt = { type: "price", precision: st.decimals, minMove: 1 / 10 ** st.decimals };
    const candle = { upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down, priceFormat: fmt };
    if (st.type === "candles" || st.type === "ha") st.price = main.addCandlestickSeries(candle);
    else if (st.type === "line") st.price = main.addLineSeries({ color: accent, lineWidth: 2, priceFormat: fmt });
    else st.price = main.addAreaSeries({ lineColor: accent, topColor: accent + "55", bottomColor: accent + "05", lineWidth: 2, priceFormat: fmt });
    if (st.volume) {
      st.vol = main.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
      main.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    }
    st.active.forEach((a) => {
      if (!shown(a)) return;
      const c = CATALOG[a.kind], host = c.pane === "price" ? main : pane(a), colours = { ...defaultColours(a), ...a.colors };
      /** @type {any[]} */ const made = [];
      c.outputs.forEach((o) => {
        const color = colours[o.key], width = a.width || o.width || (c.pane === "price" && c.outputs.length > 2 ? 1 : 2);
        const base = { color, priceLineVisible: false, lastValueVisible: c.pane !== "price", crosshairMarkerVisible: false, title: c.pane === "own" && c.outputs.length > 1 ? o.label : "" };
        if (o.type === "histogram") made.push(host.addHistogramSeries({ ...base, priceFormat: c.pane === "own" ? { type: "custom", formatter: valueText } : fmt }));
        else if (o.type === "dots") made.push(host.addLineSeries({ ...base, lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: Math.max(2, width), priceFormat: c.pane === "own" ? { type: "custom", formatter: valueText } : fmt }));
        else made.push(host.addLineSeries({ ...base, lineWidth: width, lineStyle: o.dashed ? LW.LineStyle.Dashed : LW.LineStyle.Solid,
          lineType: o.type === "step" ? LW.LineType.WithSteps : LW.LineType.Simple, priceFormat: c.pane === "own" ? { type: "custom", formatter: valueText } : fmt }));
      });
      const guideOn = made[c.outputs.findIndex((o) => o.type !== "histogram")] || made[0];
      (c.guides || []).forEach((g) => guideOn.createPriceLine({ price: g, color: css("--muted"), lineWidth: 1, lineStyle: LW.LineStyle.Dotted, axisLabelVisible: true }));
      if (c.fill) {
        const fill = new BandFill(colours[c.fill[0]]);
        made[c.outputs.findIndex((o) => o.key === /** @type {[string, string]} */ (c.fill)[0])].attachPrimitive(fill);
        st.fills.set(a.uid, fill);
      }
      st.series.set(a.uid, made);
    });
    // One time axis: scrolling or zooming any chart moves them all.
    let syncing = false;
    st.charts.forEach((chart) => chart.timeScale().subscribeVisibleLogicalRangeChange((/** @type {any} */ range) => {
      if (syncing || !range) return; syncing = true;
      st.charts.forEach((other) => { if (other !== chart) other.timeScale().setVisibleLogicalRange(range); });
      syncing = false;
    }));
    main.subscribeCrosshairMove((/** @type {any} */ param) => { describe(param && param.time != null ? param.time : null); chartMove(param); });
    st.layer = new DrawingLayer({ bars: st.bars, seconds: st.info.seconds, colors: [css("--s4"), css("--s6"), css("--s5"), css("--s1")],
      good: up, bad: down, text: css("--text"), surface: css("--surface"), format: (p) => num(p), money,
      last: () => (st.bars.length ? st.bars[st.bars.length - 1].close : null) });
    st.layer.items = st.drawings; st.layer.selected = st.selected;
    st.price.attachPrimitive(st.layer);
    setData();
    orderLines();
    trade.chartDrawn();
    replay.chartDrawn();
    patterns.chartDrawn();
    const n = st.bars.length;
    main.timeScale().setVisibleLogicalRange(keep || { from: Math.max(0, n - 150), to: n + Math.max(4, patterns.room()) });
  }

  /** A pane of its own under the price chart for one indicator. @param {Active} a */
  function pane(a) {
    const box = h("div", { class: "cd-pane" });
    panes.append(h("div", { class: "cd-pane-title" }, h("button", { type: "button", class: "cd-pane-name", text: label(a), title: "Settings", onclick: () => openSettings(a.uid) }),
      h("button", { type: "button", class: "cd-x", "aria-label": `Remove ${CATALOG[a.kind].name}`, text: "×", onclick: () => { st.active = st.active.filter((x) => x.uid !== a.uid); changed(); } })), box);
    const chart = LW.createChart(box, options(140, false, false));
    st.panesLeft--;
    if (st.panesLeft > 0) chart.timeScale().applyOptions({ visible: false });
    st.charts.push(chart);
    return chart;
  }

  /** Every shown indicator's output for the current bars. */
  function compute() {
    /** @type {Map<number, Record<string, {time:number, value:number}[]>>} */ const out = new Map();
    const ctx = { sessionOffset: st.info.session_offset || 0, seconds: st.info.seconds, market: classic ? "india" : st.venue };
    st.active.forEach((a) => {
      if (!st.series.has(a.uid)) return;
      try { out.set(a.uid, computeIndicator(a.kind, st.bars, a.params, ctx)); }
      catch (err) { showNote(`${CATALOG[a.kind].name} could not be worked out: ${err.message}`); }
    });
    return out;
  }

  const priceBars = () => (st.type === "ha" ? heikinAshi(st.bars) : st.bars);
  const priceRow = (/** @type {Bar} */ b) => (st.type === "candles" || st.type === "ha" ? { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close } : { time: b.time, value: b.close });
  const volRow = (/** @type {Bar} */ b) => ({ time: b.time, value: b.volume, color: (b.close >= b.open ? css("--good") : css("--bad")) + "66" });

  /** Histogram bars coloured by sign (or by the candle's direction for volume).
   * @param {Active} a @param {import("./indicators.js").Output} o @param {any} p */
  function barColour(a, o, p) {
    if (p.value == null) return p;
    if (o.updown) { const b = st.bars.find((x) => x.time === p.time); return { ...p, color: (b && b.close < b.open ? css("--bad") : css("--good")) + "88" }; }
    if (!o.signed) return p;
    const own = a.colors[o.key];
    if (own) return { ...p, color: p.value >= 0 ? own : own + "77" };
    return { ...p, color: (p.value >= 0 ? css("--good") : css("--bad")) + "99" };
  }

  /** @param {Active} a @param {any[]} made @param {Record<string, any[]>} r */
  function fillIndicator(a, made, r) {
    const c = CATALOG[a.kind];
    c.outputs.forEach((o, i) => {
      const pts = r[o.key] || [];
      const data = o.sparse ? pts : padded(st.bars, pts);
      made[i].setData(o.type === "histogram" ? data.map((p) => barColour(a, o, p)) : data);
    });
    shade(a, r);
  }

  /** @param {Active} a @param {Record<string, any[]>} r */
  function shade(a, r) {
    const fill = st.fills.get(a.uid), keys = CATALOG[a.kind].fill;
    if (fill && keys) fill.set(r[keys[0]] || [], r[keys[1]] || []);
  }

  /** @param {Map<number, Record<string, {time:number, value:number}[]>>} results */
  function index(results) {
    st.lookup = new Map([...results].map(([id, r]) => [id, Object.fromEntries(Object.entries(r).map(([k, pts]) => [k, new Map(pts.map((p) => [p.time, p.value]))]))]));
  }

  function setData() {
    st.price.setData(priceBars().map(priceRow));
    if (st.vol) st.vol.setData(st.bars.map(volRow));
    const results = compute();
    st.active.forEach((a) => {
      const made = st.series.get(a.uid), r = results.get(a.uid);
      if (made && r) fillIndicator(a, made, r);
    });
    st.results = results; index(results);
    patterns.update();
    describe(null); renderHead();
  }

  /** A live price changed the newest bar: update only the last point of each series (most indicators use
   * past bars only, so no earlier value changes). Indicators that redraw their past, and a batch of fresh
   * candles, redraw in full. @param {boolean} [all] */
  function schedule(all) {
    st.pendingAll = st.pendingAll || !!all;
    if (st.frame) return;
    st.frame = requestAnimationFrame(() => {
      st.frame = 0;
      if (!st.price) return;
      if (st.pendingAll) { st.pendingAll = false; setData(); return; }
      const last = st.bars[st.bars.length - 1], ha = st.type === "ha" ? heikinAshi(st.bars) : null;
      st.price.update(priceRow(ha ? ha[ha.length - 1] : last));
      if (st.vol) st.vol.update(volRow(last));
      const results = compute();
      st.active.forEach((a) => {
        const made = st.series.get(a.uid), r = results.get(a.uid), c = CATALOG[a.kind];
        if (!made || !r) return;
        if (c.repaints || c.outputs.some((o) => o.back || o.sparse)) { fillIndicator(a, made, r); return; }
        c.outputs.forEach((o, i) => {
          const pts = r[o.key] || [], p = pts[pts.length - 1];
          if (!p || p.time < last.time) return;
          try { made[i].update(o.type === "histogram" ? barColour(a, o, p) : p); } catch (err) { fillIndicator(a, made, r); }
        });
        shade(a, r);
      });
      st.results = results; index(results);
      describe(null); renderHead(); trade.priceChanged(); patterns.update();
      if (st.selected) renderPlanOnly();
    });
  }

  /** Keeps the position editor's "at last price" figure current without rebuilding its inputs. */
  function renderPlanOnly() {
    const d = st.drawings.find((x) => x.id === st.selected), el = editBar.querySelector(".cd-plan");
    if (!d || !el || (d.kind !== "long" && d.kind !== "short")) return;
    if (editBar.contains(document.activeElement)) return;
    renderEditBar();
  }

  function renderHead() {
    const bars = st.bars, last = bars[bars.length - 1], prev = bars[bars.length - 2];
    const change = prev ? (last.close / prev.close - 1) * 100 : null;
    const status = head.querySelector(".cd-status");
    head.replaceChildren(
      h("div", { class: "cd-title" }, h("strong", { text: st.info.name || st.info.symbol }),
        h("span", { class: "cd-last", text: (st.venue === "crypto" ? "" : "₹") + num(last.close) }),
        change == null ? null : h("span", { class: "cd-change " + (change >= 0 ? "up" : "down"), text: `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` }),
        h("span", { class: "hint", text: `${timeframes()[st.tf] || st.tf} candles · ${st.info.source}` })),
      status || h("span", { class: "pill cd-status", text: "" }));
  }

  /** @param {{state: string, text: string}} s */
  function setStatus(s) {
    let pill = head.querySelector(".cd-status");
    if (!pill) { pill = h("span", { class: "pill cd-status" }); head.append(pill); }
    pill.textContent = s.text;
    pill.className = "pill cd-status " + ({ live: "good", poll: "warn", error: "bad", closed: "", connecting: "" }[s.state] || "");
  }

  /** The legend: the candle under the crosshair (or the newest) and each indicator's values there, with
   * buttons to change, hide or remove it. @param {number|null} time */
  function describe(time) {
    const bars = st.bars;
    const i = time == null ? bars.length - 1 : bars.findIndex((b) => b.time === time);
    const b = bars[i];
    if (!b) return;
    const pr = (/** @type {number} */ v) => num(v), items = [];
    items.push(h("span", { class: "cd-ohlc", text: `O ${pr(b.open)}  H ${pr(b.high)}  L ${pr(b.low)}  C ${pr(b.close)}${st.volume ? `  Vol ${num(Math.round(b.volume), 0)}` : ""}` }));
    const names = patterns.namesAt(b.time);
    if (names.length) items.push(h("span", { class: "cd-leg cd-leg-pat", text: names.join(", ") }));
    st.active.forEach((a) => {
      const c = CATALOG[a.kind], colours = { ...defaultColours(a), ...a.colors };
      const controls = [
        h("button", { type: "button", class: "cd-leg-name", title: "Settings", text: label(a), onclick: () => openSettings(a.uid) }),
        h("button", { type: "button", class: "cd-leg-btn", title: a.hidden ? "Show" : "Hide", "aria-label": `${a.hidden ? "Show" : "Hide"} ${c.name}`, text: a.hidden ? "◌" : "◉", onclick: () => { a.hidden = !a.hidden; changed(); } }),
        h("button", { type: "button", class: "cd-leg-btn", title: "Remove", "aria-label": `Remove ${c.name}`, text: "×", onclick: () => { st.active = st.active.filter((x) => x.uid !== a.uid); changed(); } }),
      ];
      if (c.pane === "own") { items.push(h("span", { class: "cd-leg off-pane" }, controls)); return; }
      let values = [];
      if (a.hidden) values = [h("span", { class: "hint", text: "hidden" })];
      else if (c.intraday && !intraday()) values = [h("span", { class: "hint", text: "needs a candle under 1 day" })];
      else {
        const look = st.lookup.get(a.uid);
        if (a.kind === "vwap" && look && !look.line.size) values = [h("span", { class: "hint", text: "needs volume, and this has none" })];
        else if (look) values = c.outputs.slice(0, 7).map((o) => h("span", { class: "cd-val" }, h("i", { style: `background:${colours[o.key]}` }), pr(look[o.key].get(b.time) ?? NaN).replace("NaN", "–")));
      }
      items.push(h("span", { class: "cd-leg" + (a.hidden ? " off" : "") }, controls, values));
    });
    legend.replaceChildren(...items);
    // values in each pane's title
    const titles = panes.querySelectorAll(".cd-pane-title");
    let k = 0;
    st.active.forEach((a) => {
      if (!shown(a) || CATALOG[a.kind].pane !== "own") return;
      const t = titles[k++], look = st.lookup.get(a.uid), colours = { ...defaultColours(a), ...a.colors };
      if (!t || !look) return;
      let vals = t.querySelector(".cd-pane-vals");
      if (!vals) { vals = h("span", { class: "cd-pane-vals" }); t.insertBefore(vals, t.lastChild); }
      vals.replaceChildren(...CATALOG[a.kind].outputs.map((o) => h("span", { class: "cd-val" }, h("i", { style: `background:${colours[o.key]}` }), valueText(look[o.key].get(b.time)))));
    });
  }

  new MutationObserver(() => { if (st.info) draw(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const scheme = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  if (scheme && scheme.addEventListener) scheme.addEventListener("change", () => { if (st.info) draw(); });

  fetchLayout();
  renderControls();
  return {
    show() {
      st.visible = true; watch.show();
      if (!lite && st.venue === "crypto") paper.show();
      if (st.replaying) return;          // a replay stays where it was
      if (!st.info) { if (error.hidden && !st.busy) open(); return; }
      if (!st.stopFeed) open();          // back on the tab: fresh candles, then live again
    },
    hide() { st.visible = false; stopLive(); replay.pause(); watch.hide(); paper.hide(); trade.hide(); if (dialog.open) dialog.close(); },
    /** The desk's Paper / Live switch changed. */
    modeChanged() { trade.modeChanged(); },
    /** Opens a symbol (the stock window's Chart button). @param {string} symbol */
    openSymbol(symbol) { st.symbol = String(symbol || "").toUpperCase(); if (!classic) st.venue = "india"; renderControls(); open(); },
    /** Another chart in the same window changed stock and this one follows (charts linked). Shown at once
     * only when on screen. @param {string} venue @param {string} symbol */
    setSymbol(venue, symbol) {
      if (classic || !(venue in VENUES) || (venue === st.venue && symbol === st.symbol)) return;
      st.venue = venue; st.symbol = String(symbol || "").toUpperCase(); lastSymbol = st.venue + ":" + st.symbol;
      renderControls();
      if (st.visible) open(); else { save(); st.info = null; }
    },
  };
}

/** Shades the area between two lines of an indicator (Bollinger Bands, Keltner, Ichimoku's cloud). Drawn
 * under the lines with the chart library's plugin interface. */
class BandFill {
  /** @param {string} color */
  constructor(color) {
    this.color = color;
    /** @type {{time:number, value:number}[]} */ this.a = [];
    /** @type {Map<number, number>} */ this.b = new Map();
    /** @type {any} */ this.chart = null; /** @type {any} */ this.series = null; /** @type {null|(() => void)} */ this.request = null;
  }
  /** @param {{ chart: any, series: any, requestUpdate: () => void }} p */
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.request = requestUpdate; }
  detached() { this.chart = this.series = this.request = null; }
  updateAllViews() { /* worked out when drawing */ }
  /** @param {{time:number, value:number}[]} a @param {{time:number, value:number}[]} b */
  set(a, b) { this.a = a; this.b = new Map(b.map((p) => [p.time, p.value])); if (this.request) this.request(); }
  paneViews() { return [{ zOrder: () => "bottom", renderer: () => ({ draw: () => {}, drawBackground: (/** @type {any} */ target) => this.draw(target) }) }]; }
  /** @param {any} target */
  draw(target) {
    if (!this.chart || !this.series || !this.a.length) return;
    const ts = this.chart.timeScale(), s = this.series;
    target.useBitmapCoordinateSpace((/** @type {any} */ scope) => {
      const ctx = scope.context, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
      ctx.save(); ctx.fillStyle = this.color; ctx.globalAlpha = 0.08;
      /** @type {{x:number, ya:number, yb:number}[]} */ let run = [];
      const flush = () => {
        if (run.length > 1) {
          ctx.beginPath(); ctx.moveTo(run[0].x * hr, run[0].ya * vr);
          run.forEach((p) => ctx.lineTo(p.x * hr, p.ya * vr));
          for (let i = run.length - 1; i >= 0; i--) ctx.lineTo(run[i].x * hr, run[i].yb * vr);
          ctx.closePath(); ctx.fill();
        }
        run = [];
      };
      for (const p of this.a) {
        const other = this.b.get(p.time), x = ts.timeToCoordinate(p.time);
        if (other == null || x == null) { flush(); continue; }
        const ya = s.priceToCoordinate(p.value), yb = s.priceToCoordinate(other);
        if (ya == null || yb == null) { flush(); continue; }
        run.push({ x, ya, yb });
      }
      flush();
      ctx.restore();
    });
  }
}
