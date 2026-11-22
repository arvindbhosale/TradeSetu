// @ts-check
// The Charts tab: candles, line or area with volume, live prices and indicators that move with every tick.
// Loaded only when the tab first opens (app.js imports it), so the rest of the desk does not carry it.
// Drawing uses the bundled Lightweight Charts library (Apache-2.0, attribution shown under the chart).

import { CATALOG, COMPUTE } from "./indicators.js";
import { mergeBar, startFeed } from "./feeds.js";
import { DrawingLayer, TOOLS, timeOf } from "./drawings.js";
import { mountWatchlist } from "./watchlist.js";
import { mountPaper } from "./paper.js";
import { mountTrade } from "./trade.js";

/** @typedef {import("./indicators.js").Bar} Bar */
/** @typedef {{ uid: number, kind: string, params: Record<string, number> }} Active */

const LW = /** @type {any} */ (window).LightweightCharts;
const STORE = "desk-chartdesk";
const TYPES = { candles: "Candles", line: "Line", area: "Area" };
const VENUES = { crypto: "Crypto", india: "NSE / BSE", imported: "Imported" };
const DEFAULTS = { venue: "crypto", symbol: "BTCUSDT", tf: "1h", type: "candles", volume: true,
  indicators: [{ kind: "ema", params: { period: 20 } }, { kind: "bollinger", params: { period: 20, width: 2 } }, { kind: "rsi", params: { period: 14 } }] };

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

const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();

/** Decimal places for a price: 2 for rupees, more for small crypto prices. @param {string} venue @param {number} price */
export function decimalsFor(venue, price) {
  if (venue !== "crypto" || !(price > 0)) return 2;
  if (price >= 1000) return 2;
  if (price >= 1) return 4;
  return Math.min(10, Math.max(4, Math.ceil(-Math.log10(price)) + 3));
}

/** Line data with blank entries during an indicator's warm-up, so every pane has a point for every bar and the
 * panes' time axes line up. @param {Bar[]} bars @param {{time: number, value: number}[]} pts */
export function padded(bars, pts) {
  const at = new Map(pts.map((p) => [p.time, p.value]));
  return bars.map((b) => (at.has(b.time) ? { time: b.time, value: at.get(b.time) } : { time: b.time }));
}

function load() {
  try {
    const saved = JSON.parse(localStorage.getItem(STORE) || "null");
    if (saved && typeof saved === "object") return { ...DEFAULTS, ...saved };
  } catch (err) { /* storage blocked or bad */ }
  return { ...DEFAULTS };
}

/**
 * @param {HTMLElement} root
 * @param {{ api: (path: string, opts?: any) => Promise<any>, mode?: () => string }} deps
 *   ``mode`` is the desk's Paper or Live switch, for NSE/BSE orders from the chart (crypto is always paper).
 */
export function mount(root, { api, mode = () => "paper" }) {
  const saved = load();
  let uid = 0;
  const st = {
    venue: saved.venue in VENUES ? saved.venue : "crypto", symbol: String(saved.symbol || "BTCUSDT"),
    tf: saved.tf, type: saved.type in TYPES ? saved.type : "candles", volume: saved.volume !== false,
    active: /** @type {Active[]} */ ((Array.isArray(saved.indicators) ? saved.indicators : DEFAULTS.indicators)
      .filter((i) => i && CATALOG[i.kind]).map((i) => ({ uid: ++uid, kind: i.kind, params: { ...defaultsOf(i.kind), ...(i.params || {}) } }))),
    info: /** @type {any} */ (null), bars: /** @type {Bar[]} */ ([]), catalog: /** @type {any} */ (null),
    request: 0, stopFeed: /** @type {null|(() => void)} */ (null), visible: false,
    charts: /** @type {any[]} */ ([]), main: /** @type {any} */ (null), price: /** @type {any} */ (null), vol: /** @type {any} */ (null),
    series: /** @type {Map<number, any[]>} */ (new Map()), frame: 0, decimals: 2,
    results: /** @type {Map<number, any>|null} */ (null), pendingAll: false, panesLeft: 0,
    busy: false, importMsg: "", drawings: /** @type {any[]} */ ([]), tool: /** @type {string|null} */ (null), firstPoint: /** @type {any} */ (null),
    layer: /** @type {DrawingLayer|null} */ (null), book: /** @type {any} */ (null), orderLines: /** @type {any[]} */ ([]),
  };

  function defaultsOf(kind) { return Object.fromEntries(Object.entries(CATALOG[kind].params).map(([k, p]) => [k, p.value])); }
  function save() {
    try {
      localStorage.setItem(STORE, JSON.stringify({ venue: st.venue, symbol: st.symbol, tf: st.tf, type: st.type, volume: st.volume,
        indicators: st.active.map((a) => ({ kind: a.kind, params: a.params })) }));
    } catch (err) { /* storage blocked */ }
  }

  // -- layout ------------------------------------------------------------------------------------------
  const venueSeg = h("div", { class: "seg", role: "group", "aria-label": "Market" });
  const symbolInput = /** @type {HTMLInputElement} */ (h("input", { name: "cd-symbol", list: "cd-symbols", autocomplete: "off", spellcheck: "false", "aria-label": "Symbol" }));
  const symbolList = h("datalist", { id: "cd-symbols" });
  const tfSeg = h("div", { class: "seg cd-scroll", role: "group", "aria-label": "Timeframe" });
  const typeSeg = h("div", { class: "seg", role: "group", "aria-label": "Chart type" });
  const indicatorMenu = h("details", { class: "cd-menu" });
  const head = h("div", { class: "cd-head" });
  const error = h("div", { class: "error cd-error", role: "alert", hidden: true });
  const note = h("p", { class: "hint", hidden: true });
  const legend = h("div", { class: "cd-legend", "aria-live": "off" });
  const mainBox = h("div", { class: "cd-main" });
  const panes = h("div", { class: "cd-panes" });
  const chartArea = h("div", { class: "cd-area", hidden: true }, legend, mainBox, panes,
    h("p", { class: "hint cd-credit" }, "Charts drawn with ",
      h("a", { href: "https://www.tradingview.com/", target: "_blank", rel: "noopener noreferrer", text: "TradingView Lightweight Charts" }),
      ". Crypto prices from Binance or Coinbase public data; NSE/BSE from your broker."));
  const loading = h("p", { class: "hint", text: "Loading…", hidden: true });
  const drawBar = h("div", { class: "cd-drawbar", role: "group", "aria-label": "Drawing tools" });
  const importPanel = h("div", { class: "cd-import", hidden: true });
  const watchRoot = h("aside", { class: "panel cd-watch", "aria-label": "Watchlist" });
  const paperRoot = h("section", { class: "panel cd-paper", hidden: true, "aria-label": "Paper trading" });
  const tradeRoot = h("section", { class: "panel cd-trade", hidden: true, "aria-label": "Trade from the chart" });
  chartArea.insertBefore(drawBar, mainBox);

  root.replaceChildren(h("div", { class: "cd-grid" }, watchRoot, h("div", { class: "cd-col" },
    h("div", { class: "panel cd-panel" },
      h("form", { class: "cd-bar", onsubmit: (e) => { e.preventDefault(); st.symbol = symbolInput.value.trim().toUpperCase(); open(); } },
        venueSeg,
        h("div", { class: "cd-symbol" }, symbolInput, symbolList, h("button", { class: "primary", type: "submit", text: "Show" })),
        tfSeg, typeSeg, indicatorMenu),
      importPanel, head, error, note, loading, chartArea),
    tradeRoot, paperRoot)));

  const watch = mountWatchlist(watchRoot, { api, open: (venue, symbol) => { st.venue = venue; st.symbol = symbol; renderControls(); open(); },
    current: () => ({ venue: st.venue, symbol: st.symbol }) });
  const lastPrice = () => (st.bars.length ? st.bars[st.bars.length - 1].close : null);
  const paper = mountPaper(paperRoot, { api, onBook: (book) => { st.book = book; orderLines(); trade.bookChanged(book); } });
  const trade = mountTrade(tradeRoot, { api, venue: () => st.venue, symbol: () => st.symbol, lastPrice, mode,
    decimals: () => st.decimals, money: (v) => money(v), onBook: (book) => paper.setBook(book),
    chart: () => (st.main && st.price ? { main: st.main, price: st.price, box: mainBox } : null) });

  function segment(host, options, current, pick) {
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
    const tfs = st.catalog ? st.catalog.timeframes : {};
    segment(tfSeg, Object.fromEntries(Object.keys(tfs).map((k) => [k, k === "1d" ? "1D" : k === "1w" ? "1W" : k])), st.tf,
      (key) => { st.tf = key; renderControls(); open(); });
    [...tfSeg.children].forEach((b) => b.setAttribute("title", tfs[/** @type {HTMLElement} */ (b).dataset.key || ""] || ""));
    const meta = st.venue === "imported" && st.catalog ? st.catalog.imports.find((i) => i.symbol === st.symbol) : null;
    if (meta) {
      const secs = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800 };
      [...tfSeg.children].forEach((b) => {
        const s = secs[/** @type {HTMLElement} */ (b).dataset.key || ""];
        if (s < meta.base_seconds || (s < 86400 && s % meta.base_seconds)) {
          b.setAttribute("disabled", ""); b.setAttribute("title", `This file has ${meta.candle} candles`);
        }
      });
    }
    segment(typeSeg, TYPES, st.type, (key) => { st.type = key; renderControls(); save(); draw(); });
    symbolInput.value = st.symbol;
    symbolInput.placeholder = st.venue === "crypto" ? "BTCUSDT" : "RELIANCE, NIFTY";
    const list = !st.catalog ? [] : st.venue === "crypto" ? st.catalog.crypto.map((c) => [c.symbol, `${c.name} (${c.coin})`])
      : st.venue === "imported" ? st.catalog.imports.map((i) => [i.symbol, `${i.rows} ${i.candle} candles`])
      : st.catalog.indices.map((i) => [i.symbol, i.name]);
    symbolList.replaceChildren(...list.map(([value, label]) => h("option", { value, label })));
    renderIndicatorMenu(); renderDrawBar(); renderImport(); watch.render();
    if (st.visible && st.venue === "crypto") paper.show(); else paper.hide();
    if (st.visible && st.info) trade.show(); else trade.hide();
  }

  // -- drawings ----------------------------------------------------------------------------------------
  function renderDrawBar() {
    const need = st.tool ? TOOLS[st.tool].points : 0;
    drawBar.replaceChildren(
      ...Object.entries(TOOLS).map(([k, t]) => h("button", { type: "button", "aria-pressed": String(st.tool === k), text: t.label,
        onclick: () => { st.tool = st.tool === k ? null : k; st.firstPoint = null; if (st.layer) { st.layer.preview = null; st.layer.update(); } renderDrawBar(); } })),
      h("button", { type: "button", text: "Undo", disabled: !st.drawings.length, onclick: () => { st.drawings.pop(); saveDrawings(); } }),
      h("button", { type: "button", text: "Clear all", disabled: !st.drawings.length, onclick: () => { st.drawings = []; saveDrawings(); } }),
      h("span", { class: "hint cd-draw-hint", text: !st.tool ? "" : st.firstPoint ? "Now tap the second point." :
        need === 1 ? "Tap the chart at the price." : st.tool === "fib" ? "Tap the swing start, then the swing end." : "Tap the first point." }));
  }

  /** @param {any} param */
  function pointAt(param) {
    if (!param || !param.point || param.logical == null || !st.price || !st.info) return null;
    const price = st.price.coordinateToPrice(param.point.y);
    if (price == null || !(price > 0)) return null;
    return { time: timeOf(st.bars, param.logical, st.info.seconds), price };
  }

  // Taps are read from the chart box directly: the library drops a second click that comes quickly after
  // the first (it waits to see a double click), which loses the second point of a line.
  let down = /** @type {{x: number, y: number}|null} */ (null);
  mainBox.addEventListener("pointerdown", (e) => { down = { x: e.clientX, y: e.clientY }; }, true);
  mainBox.addEventListener("pointerup", (e) => {
    if (!down || !st.tool || !st.main) return;
    const moved = Math.hypot(e.clientX - down.x, e.clientY - down.y);
    down = null;
    if (moved > 6) return;                              // a drag pans the chart
    const rect = mainBox.getBoundingClientRect();
    const x = e.clientX - rect.left, y = e.clientY - rect.top;
    if (x > rect.width - st.main.priceScale("right").width()) return;     // on the price scale
    chartClick({ point: { x, y }, logical: st.main.timeScale().coordinateToLogical(x) });
  }, true);

  /** @param {any} param */
  function chartClick(param) {
    if (!st.tool) return;
    const pt = pointAt(param);
    if (!pt) return;
    const need = TOOLS[st.tool].points;
    if (st.firstPoint && st.firstPoint.time === pt.time && Math.abs(st.firstPoint.price - pt.price) < 1e-9 * pt.price) return;
    if (need === 1 || st.firstPoint) {
      const points = need === 1 ? [pt] : [st.firstPoint, pt];
      st.drawings.push({ id: Math.random().toString(36).slice(2, 10), kind: st.tool, points });
      st.firstPoint = null; st.tool = null;
      if (st.layer) st.layer.preview = null;
      saveDrawings();
    } else {
      st.firstPoint = pt; renderDrawBar();
    }
  }

  /** @param {any} param */
  function chartMove(param) {
    if (!st.layer || !st.tool || !st.firstPoint) return;
    const pt = pointAt(param);
    st.layer.preview = pt ? { id: "preview", kind: /** @type {any} */ (st.tool), points: [st.firstPoint, pt] } : null;
    st.layer.update();
  }

  async function saveDrawings() {
    if (st.layer) { st.layer.items = st.drawings; st.layer.update(); }
    renderDrawBar();
    try { await api("/md/drawings", { method: "PUT", body: { venue: st.venue, symbol: st.symbol, items: st.drawings } }); }
    catch (err) { setStatus({ state: "error", text: `Drawings not saved: ${err.message}` }); }
  }

  async function loadDrawings(request) {
    try {
      const items = (await api("/md/drawings?" + new URLSearchParams({ venue: st.venue, symbol: st.symbol }))).items;
      if (request !== st.request) return;
      st.drawings = items;
    } catch (err) {
      st.drawings = [];
      note.textContent = `Saved drawings could not be loaded: ${err.message}`; note.hidden = false;
    }
    if (st.layer) { st.layer.items = st.drawings; st.layer.update(); }
    renderDrawBar();
  }

  // -- paper orders on the chart -----------------------------------------------------------------------
  function orderLines() {
    st.orderLines.forEach((line) => { try { st.price && st.price.removePriceLine(line); } catch (err) { /* chart redrawn */ } });
    st.orderLines = [];
    if (!st.price || !st.book || st.venue !== "crypto") return;
    const pos = st.book.positions.find((p) => p.symbol === st.symbol);
    if (pos) st.orderLines.push(st.price.createPriceLine({ price: pos.avg, color: css("--accent"), lineWidth: 1, lineStyle: LW.LineStyle.Dashed, axisLabelVisible: true, title: "Paper average" }));
    st.book.orders.filter((o) => o.symbol === st.symbol).forEach((o) => st.orderLines.push(st.price.createPriceLine({
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
    const go = async (e) => {
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
      h("div", { class: "cd-chips" }, imports.length ? h("div", { class: "cd-chips" }, imports.map((i) => h("span", { class: "cd-chip" + (i.symbol === st.symbol ? " on" : "") },
        h("button", { type: "button", class: "cd-chip-open", text: i.symbol, title: `${i.rows} ${i.candle} candles`, onclick: () => { st.symbol = i.symbol; renderControls(); open(); } }),
        h("button", { type: "button", class: "cd-x", "aria-label": `Delete import ${i.symbol}`, text: "×", onclick: async () => {
          try { await api(`/md/imports/${encodeURIComponent(i.symbol)}`, { method: "DELETE" }); st.catalog = await api("/md/catalog");
            if (st.symbol === i.symbol) st.symbol = st.catalog.imports[0] ? st.catalog.imports[0].symbol : "";
            renderControls(); open();
          } catch (err) { result.hidden = false; result.className = "error"; result.textContent = `Not deleted: ${err.message}`; }
        } })))) : null));
  }

  function renderIndicatorMenu() {
    const chips = st.active.map((a) => h("span", { class: "cd-chip" }, label(a),
      h("button", { type: "button", class: "cd-x", "aria-label": `Remove ${CATALOG[a.kind].name}`, text: "×",
        onclick: () => { st.active = st.active.filter((x) => x.uid !== a.uid); changed(true); } })));
    const settings = st.active.filter((a) => Object.keys(CATALOG[a.kind].params).length).map((a) =>
      h("fieldset", { class: "cd-params" }, h("legend", { text: label(a) }),
        Object.entries(CATALOG[a.kind].params).map(([key, p]) => h("label", {}, p.label,
          h("input", { type: "number", value: String(a.params[key]), min: String(p.min), max: String(p.max), step: String(p.step || 1),
            onchange: (e) => {
              const v = Number(e.target.value);
              if (!Number.isFinite(v) || v < p.min || v > p.max) { e.target.value = String(a.params[key]); return; }
              a.params[key] = p.step ? v : Math.round(v); changed(true);
            } })))));
    indicatorMenu.replaceChildren(
      h("summary", { class: "cd-btn" }, `Indicators${st.active.length ? ` (${st.active.length})` : ""}`),
      h("div", { class: "cd-drop" },
        h("div", { class: "cd-chips" }, chips.length ? chips : h("span", { class: "hint", text: "None on the chart." })),
        h("div", { class: "lbl", text: "Add" }),
        h("div", { class: "cd-add" }, Object.entries(CATALOG).map(([kind, c]) => h("button", { type: "button", text: c.name,
          onclick: () => { st.active.push({ uid: ++uid, kind, params: defaultsOf(kind) }); changed(true); } }))),
        h("label", { class: "check" }, h("input", { type: "checkbox", checked: st.volume, onchange: (e) => { st.volume = e.target.checked; changed(true); } }), "Volume bars"),
        settings.length ? h("details", { class: "cd-adv" }, h("summary", { text: "Advanced: indicator settings" }), settings) : null));
    if (indicatorMenu.dataset.open === "1") indicatorMenu.setAttribute("open", "");
    indicatorMenu.ontoggle = () => { indicatorMenu.dataset.open = indicatorMenu.hasAttribute("open") ? "1" : ""; };
  }

  function label(a) {
    const c = CATALOG[a.kind], values = Object.values(a.params);
    return c.short + (values.length ? " " + values.join(", ") : "");
  }

  function changed(redraw) { save(); renderIndicatorMenu(); if (redraw) draw(); }

  // -- data --------------------------------------------------------------------------------------------
  function query(tf = st.tf) { return "/md/candles?" + new URLSearchParams({ venue: st.venue, symbol: st.symbol, tf }); }

  /** @param {string} message @param {boolean} [suggestImport] */
  function showError(message, suggestImport) {
    error.replaceChildren(h("span", {}, h("strong", { text: "Chart not shown. " }), message, " ",
      h("button", { type: "button", class: "cd-retry", text: "Try again", onclick: () => open() }),
      suggestImport ? h("button", { type: "button", class: "cd-retry", text: "Import a CSV instead",
        onclick: () => { st.venue = "imported"; st.symbol = st.catalog && st.catalog.imports[0] ? st.catalog.imports[0].symbol : ""; renderControls(); open(); } }) : null));
    error.hidden = false;
  }

  async function open() {
    const request = ++st.request;
    st.busy = true;
    stopLive();
    error.hidden = true; loading.hidden = false;
    save();
    try {
      if (!st.catalog) {
        st.catalog = await api("/md/catalog");
        if (!(st.tf in st.catalog.timeframes)) st.tf = "1h";
        renderControls();
      }
      if (st.venue === "imported" && st.symbol) {
        // An import has one candle size; a smaller one cannot be shown, so step up to the first that can.
        const meta = st.catalog.imports.find((i) => i.symbol === st.symbol);
        const secs = { "1m": 60, "5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400, "1w": 604800 };
        const fits = (tf) => secs[tf] >= meta.base_seconds && (secs[tf] >= 86400 || secs[tf] % meta.base_seconds === 0);
        if (meta && !fits(st.tf)) { st.tf = Object.keys(secs).find(fits) || "1d"; renderControls(); }
      }
      if (st.venue === "imported" && !st.symbol) {
        loading.hidden = true; chartArea.hidden = true; destroy(); st.bars = []; st.info = null; head.replaceChildren(); note.hidden = true;
        renderControls();
        return;
      }
      if (st.venue !== "imported") st.importMsg = "";
      const info = await api(query());
      if (request !== st.request) return;
      if (!Array.isArray(info.candles) || !info.candles.length) throw new Error(`${info.source || "The provider"} sent no candles.`);
      st.info = info; st.bars = info.candles.map((c) => ({ ...c }));
      st.decimals = decimalsFor(st.venue, st.bars[st.bars.length - 1].close);
      loading.hidden = true; chartArea.hidden = false;
      const notes = info.notes || [];
      note.textContent = notes.join(" "); note.hidden = !notes.length;
      st.drawings = []; st.tool = null; st.firstPoint = null;
      draw(true);
      startLive();
      renderControls(); paper.symbolChanged(); trade.symbolChanged();
      loadDrawings(request);
    } catch (err) {
      if (request !== st.request) return;
      loading.hidden = true; chartArea.hidden = true; destroy(); st.bars = []; st.info = null;
      head.replaceChildren(); note.hidden = true;
      const payload = /** @type {any} */ (err).payload || {};
      showError(err instanceof Error ? err.message : String(err), st.venue === "india" && !!payload.needs_login);
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
      refetch: () => api(query()),
      quote: () => api("/md/quote?" + new URLSearchParams({ symbol: st.symbol })),
    });
  }

  // -- drawing -----------------------------------------------------------------------------------------
  /** Numbers in Indian grouping for rupees (1,22,000), international for crypto (122,000). @param {number} v @param {number} [dp] */
  function num(v, dp = st.decimals) {
    return v.toLocaleString(st.venue === "india" ? "en-IN" : "en-US", { minimumFractionDigits: Math.min(dp, 2), maximumFractionDigits: dp });
  }

  /** @param {number} height @param {boolean} [logo] the library's logo, shown once on the price chart */
  function options(height, logo = false) {
    const d = st.decimals;
    return { height, autoSize: true,
      layout: { background: { color: css("--surface") }, textColor: css("--text2"), fontSize: 12, attributionLogo: logo },
      grid: { vertLines: { color: css("--line") }, horzLines: { color: css("--line") } },
      rightPriceScale: { borderColor: css("--line"), minimumWidth: window.innerWidth < 720 ? 84 : 104 },   // the same width on every pane keeps the time axes aligned
      timeScale: { borderColor: css("--line"), timeVisible: !["1d", "1w"].includes(st.tf), secondsVisible: false, rightOffset: 4 },
      crosshair: { mode: LW.CrosshairMode.Normal },
      localization: { priceFormatter: (/** @type {number} */ p) => p.toLocaleString(st.venue === "india" ? "en-IN" : "en-US", { minimumFractionDigits: d, maximumFractionDigits: d }) } };
  }

  function destroy() {
    st.charts.forEach((c) => c.remove());
    st.charts = []; st.series = new Map(); st.main = st.price = st.vol = null; st.layer = null; st.orderLines = [];
    panes.replaceChildren();
  }

  const colors = () => [1, 2, 3, 4, 5, 6].map((i) => css("--s" + i));

  function draw(fit) {
    if (!st.info || !st.bars.length) return;
    const keep = !fit && st.main ? st.main.timeScale().getVisibleLogicalRange() : null;
    destroy();
    const up = css("--good"), down = css("--bad"), accent = css("--accent"), palette = colors();
    const main = LW.createChart(mainBox, options(mainBox.clientHeight || 420, true));
    st.main = main; st.charts.push(main);
    const intradayTf = !["1d", "1w"].includes(st.tf);
    const ownPanes = st.active.filter((a) => CATALOG[a.kind].pane === "own" && (!CATALOG[a.kind].intraday || intradayTf)).length;
    st.panesLeft = ownPanes;
    if (ownPanes) main.timeScale().applyOptions({ visible: false });   // only the bottom chart shows dates
    const fmt = { type: "price", precision: st.decimals, minMove: 1 / 10 ** st.decimals };
    if (st.type === "candles") st.price = main.addCandlestickSeries({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down, priceFormat: fmt });
    else if (st.type === "line") st.price = main.addLineSeries({ color: accent, lineWidth: 2, priceFormat: fmt });
    else st.price = main.addAreaSeries({ lineColor: accent, topColor: accent + "55", bottomColor: accent + "05", lineWidth: 2, priceFormat: fmt });
    if (st.volume) {
      st.vol = main.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
      main.priceScale("vol").applyOptions({ scaleMargins: { top: 0.8, bottom: 0 } });
    }
    let colour = 0;
    const intraday = !["1d", "1w"].includes(st.tf);
    st.active.forEach((a) => {
      const c = CATALOG[a.kind];
      if (c.intraday && !intraday) return;
      const host = c.pane === "price" ? main : pane(c.name);
      const lineOpts = (color, extra = {}) => ({ color, lineWidth: 2, priceLineVisible: false, lastValueVisible: c.pane !== "price", crosshairMarkerVisible: false, ...extra });
      /** @type {any[]} */ const made = [];
      if (a.kind === "bollinger") {
        const col = palette[colour++ % palette.length];
        made.push(host.addLineSeries(lineOpts(col, { lineWidth: 1 })), host.addLineSeries(lineOpts(col, { lineWidth: 1, lineStyle: LW.LineStyle.Dashed })), host.addLineSeries(lineOpts(col, { lineWidth: 1 })));
      } else if (a.kind === "macd") {
        made.push(host.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false }), host.addLineSeries(lineOpts(palette[0])), host.addLineSeries(lineOpts(palette[1])));
      } else {
        const series = host.addLineSeries(lineOpts(palette[colour++ % palette.length]));
        if (a.kind === "rsi") [70, 30].forEach((g) => series.createPriceLine({ price: g, color: css("--muted"), lineWidth: 1, lineStyle: LW.LineStyle.Dotted, axisLabelVisible: true }));
        made.push(series);
      }
      st.series.set(a.uid, made);
    });
    // One time axis: scrolling or zooming any chart moves them all.
    let syncing = false;
    st.charts.forEach((chart) => chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (syncing || !range) return; syncing = true;
      st.charts.forEach((other) => { if (other !== chart) other.timeScale().setVisibleLogicalRange(range); });
      syncing = false;
    }));
    main.subscribeCrosshairMove((param) => { describe(param && param.time != null ? param.time : null); chartMove(param); });
    st.layer = new DrawingLayer({ bars: st.bars, seconds: st.info.seconds, colors: [css("--s4"), css("--s6"), css("--s5"), css("--s1")],
      text: css("--text"), format: (p) => num(p) });
    st.layer.items = st.drawings;
    st.price.attachPrimitive(st.layer);
    setData();
    orderLines();
    trade.chartDrawn();
    const n = st.bars.length;
    main.timeScale().setVisibleLogicalRange(keep || { from: Math.max(0, n - 150), to: n + 4 });
  }

  function pane(name) {
    const box = h("div", { class: "cd-pane" });
    panes.append(h("div", { class: "cd-pane-title", text: name }), box);
    const chart = LW.createChart(box, options(140));
    st.panesLeft--;
    if (st.panesLeft > 0) chart.timeScale().applyOptions({ visible: false });
    st.charts.push(chart);
    return chart;
  }

  /** Every indicator's output for the current bars. */
  function compute() {
    const out = new Map();
    st.active.forEach((a) => {
      if (!st.series.has(a.uid)) return;
      out.set(a.uid, COMPUTE[a.kind](st.bars, { ...a.params, sessionOffset: st.info.session_offset }));
    });
    return out;
  }

  const priceRow = (b) => (st.type === "candles" ? { time: b.time, open: b.open, high: b.high, low: b.low, close: b.close } : { time: b.time, value: b.close });
  const volRow = (b) => ({ time: b.time, value: b.volume, color: (b.close >= b.open ? css("--good") : css("--bad")) + "66" });
  const parts = (a, r) => (a.kind === "bollinger" ? [r.upper, r.middle, r.lower] : a.kind === "macd" ? [r.histogram, r.macd, r.signal] : [r.line]);

  function setData() {
    st.price.setData(st.bars.map(priceRow));
    if (st.vol) st.vol.setData(st.bars.map(volRow));
    const results = compute(), up = css("--good"), down = css("--bad");
    st.active.forEach((a) => {
      const made = st.series.get(a.uid), r = results.get(a.uid);
      if (!made || !r) return;
      parts(a, r).forEach((pts, i) => {
        const data = padded(st.bars, pts);
        made[i].setData(a.kind === "macd" && i === 0 ? data.map((p) => (p.value != null ? { ...p, color: (p.value >= 0 ? up : down) + "99" } : p)) : data);
      });
    });
    st.results = results;
    describe(null); renderHead();
  }

  /** A live price changed the newest bar: update only the last point of each series (indicators are causal,
   * so no earlier value changes). A batch of fresh candles redraws everything. */
  /** @param {boolean} [all] */
  function schedule(all) {
    st.pendingAll = st.pendingAll || !!all;
    if (st.frame) return;
    st.frame = requestAnimationFrame(() => {
      st.frame = 0;
      if (!st.price) return;
      if (st.pendingAll) { st.pendingAll = false; setData(); return; }
      const last = st.bars[st.bars.length - 1];
      st.price.update(priceRow(last));
      if (st.vol) st.vol.update(volRow(last));
      const results = compute(), up = css("--good"), down = css("--bad");
      st.active.forEach((a) => {
        const made = st.series.get(a.uid), r = results.get(a.uid);
        if (!made || !r) return;
        parts(a, r).forEach((pts, i) => {
          const p = pts[pts.length - 1];
          if (!p || p.time !== last.time) return;
          made[i].update(a.kind === "macd" && i === 0 ? { ...p, color: (p.value >= 0 ? up : down) + "99" } : p);
        });
      });
      st.results = results;
      describe(null); renderHead(); trade.priceChanged();
    });
  }

  function money(v) {
    const d = st.decimals, text = v.toLocaleString(st.venue === "india" ? "en-IN" : "en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
    return st.venue === "india" ? "₹" + text : text;
  }

  function renderHead() {
    const bars = st.bars, last = bars[bars.length - 1], prev = bars[bars.length - 2];
    const change = prev ? (last.close / prev.close - 1) * 100 : null;
    const status = head.querySelector(".cd-status");
    head.replaceChildren(
      h("div", { class: "cd-title" }, h("strong", { text: st.info.name || st.info.symbol }),
        h("span", { class: "cd-last", text: money(last.close) }),
        change == null ? null : h("span", { class: "cd-change " + (change >= 0 ? "up" : "down"), text: `${change >= 0 ? "+" : ""}${change.toFixed(2)}%` }),
        h("span", { class: "hint", text: `${st.catalog.timeframes[st.tf]} candles · ${st.info.source}` })),
      status || h("span", { class: "pill cd-status", text: "" }));
  }

  function setStatus(s) {
    let pill = head.querySelector(".cd-status");
    if (!pill) { pill = h("span", { class: "pill cd-status" }); head.append(pill); }
    pill.textContent = s.text;
    pill.className = "pill cd-status " + ({ live: "good", poll: "warn", error: "bad", closed: "", connecting: "" }[s.state] || "");
  }

  function describe(time) {
    const bars = st.bars;
    const i = time == null ? bars.length - 1 : bars.findIndex((b) => b.time === time);
    const b = bars[i];
    if (!b) return;
    const palette = colors(), items = [];
    items.push(h("span", { class: "cd-ohlc", text: `O ${money(b.open)}  H ${money(b.high)}  L ${money(b.low)}  C ${money(b.close)}${st.volume ? `  Vol ${num(Math.round(b.volume), 0)}` : ""}` }));
    let colour = 0;
    const intraday = !["1d", "1w"].includes(st.tf);
    st.active.forEach((a) => {
      const c = CATALOG[a.kind], r = st.results && st.results.get(a.uid);
      if (c.intraday && !intraday) { items.push(h("span", { class: "hint", text: `${c.short} needs a timeframe under 1 day` })); return; }
      if (!r) return;
      const at = (pts) => { const p = pts.find((x) => x.time === b.time); return p ? p.value : null; };
      const fmt = (v, dp = st.decimals) => (v == null ? "–" : num(v, dp));
      let text;
      if (a.kind === "bollinger") text = `${label(a)}  ${fmt(at(r.upper))} / ${fmt(at(r.middle))} / ${fmt(at(r.lower))}`;
      else if (a.kind === "macd") text = `${label(a)}  ${fmt(at(r.macd), 4)}  signal ${fmt(at(r.signal), 4)}`;
      else if (a.kind === "rsi") text = `${label(a)}  ${fmt(at(r.line), 1)}`;
      else if (a.kind === "vwap" && !r.line.length) text = "VWAP needs volume, and this has none";
      else text = `${label(a)}  ${fmt(at(r.line))}`;
      const col = c.pane === "price" ? palette[colour++ % palette.length] : null;
      items.push(h("span", {}, col ? h("i", { style: `background:${col}` }) : null, text));
    });
    legend.replaceChildren(...items);
  }

  new MutationObserver(() => { if (st.info) draw(); }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  const scheme = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  if (scheme && scheme.addEventListener) scheme.addEventListener("change", () => { if (st.info) draw(); });

  renderControls();
  return {
    show() {
      st.visible = true; watch.show();
      if (st.venue === "crypto") paper.show();
      if (!st.info) { if (error.hidden && !st.busy) open(); return; }
      if (!st.stopFeed) open();          // back on the tab: fresh candles, then live again
    },
    hide() { st.visible = false; stopLive(); watch.hide(); paper.hide(); trade.hide(); },
    /** The desk's Paper / Live switch changed. */
    modeChanged() { trade.modeChanged(); },
  };
}
