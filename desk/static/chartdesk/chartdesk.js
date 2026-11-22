// @ts-check
// The Charts tab: candles, line or area with volume, live prices and indicators that move with every tick.
// Loaded only when the tab first opens (app.js imports it), so the rest of the desk does not carry it.
// Drawing uses the bundled Lightweight Charts library (Apache-2.0, attribution shown under the chart).

import { CATALOG, COMPUTE } from "./indicators.js";
import { mergeBar, startFeed } from "./feeds.js";

/** @typedef {import("./indicators.js").Bar} Bar */
/** @typedef {{ uid: number, kind: string, params: Record<string, number> }} Active */

const LW = /** @type {any} */ (window).LightweightCharts;
const STORE = "desk-chartdesk";
const TYPES = { candles: "Candles", line: "Line", area: "Area" };
const VENUES = { crypto: "Crypto", india: "NSE / BSE" };
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
 * @param {{ api: (path: string) => Promise<any> }} deps
 */
export function mount(root, { api }) {
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

  root.replaceChildren(
    h("div", { class: "panel cd-panel" },
      h("form", { class: "cd-bar", onsubmit: (e) => { e.preventDefault(); st.symbol = symbolInput.value.trim().toUpperCase(); open(); } },
        venueSeg,
        h("div", { class: "cd-symbol" }, symbolInput, symbolList, h("button", { class: "primary", type: "submit", text: "Show" })),
        tfSeg, typeSeg, indicatorMenu),
      head, error, note, loading, chartArea));

  function segment(host, options, current, pick) {
    host.replaceChildren(...Object.entries(options).map(([key, label]) => h("button", { type: "button", "data-key": key,
      "aria-pressed": String(key === current), text: label, onclick: () => pick(key) })));
  }
  function renderControls() {
    segment(venueSeg, VENUES, st.venue, (key) => {
      if (key === st.venue) return;
      st.venue = key; st.symbol = key === "crypto" ? "BTCUSDT" : "RELIANCE"; renderControls(); open();
    });
    const tfs = st.catalog ? st.catalog.timeframes : {};
    segment(tfSeg, Object.fromEntries(Object.keys(tfs).map((k) => [k, k === "1d" ? "1D" : k === "1w" ? "1W" : k])), st.tf,
      (key) => { st.tf = key; renderControls(); open(); });
    [...tfSeg.children].forEach((b) => b.setAttribute("title", tfs[/** @type {HTMLElement} */ (b).dataset.key || ""] || ""));
    segment(typeSeg, TYPES, st.type, (key) => { st.type = key; renderControls(); save(); draw(); });
    symbolInput.value = st.symbol;
    symbolInput.placeholder = st.venue === "crypto" ? "BTCUSDT" : "RELIANCE, NIFTY";
    const list = st.catalog ? (st.venue === "crypto" ? st.catalog.crypto.map((c) => [c.symbol, `${c.name} (${c.coin})`])
      : st.catalog.indices.map((i) => [i.symbol, i.name])) : [];
    symbolList.replaceChildren(...list.map(([value, label]) => h("option", { value, label })));
    renderIndicatorMenu();
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

  function showError(message) {
    error.replaceChildren(h("strong", { text: "Chart not shown. " }), message, " ",
      h("button", { type: "button", class: "cd-retry", text: "Try again", onclick: () => open() }));
    error.hidden = false;
  }

  async function open() {
    const request = ++st.request;
    stopLive();
    error.hidden = true; loading.hidden = false;
    save();
    try {
      if (!st.catalog) {
        st.catalog = await api("/md/catalog");
        if (!(st.tf in st.catalog.timeframes)) st.tf = "1h";
        renderControls();
      }
      const info = await api(query());
      if (request !== st.request) return;
      if (!Array.isArray(info.candles) || !info.candles.length) throw new Error(`${info.source || "The provider"} sent no candles.`);
      st.info = info; st.bars = info.candles.map((c) => ({ ...c }));
      st.decimals = decimalsFor(st.venue, st.bars[st.bars.length - 1].close);
      loading.hidden = true; chartArea.hidden = false;
      const notes = info.notes || [];
      note.textContent = notes.join(" "); note.hidden = !notes.length;
      draw(true);
      startLive();
    } catch (err) {
      if (request !== st.request) return;
      loading.hidden = true; chartArea.hidden = true; destroy(); st.bars = []; st.info = null;
      head.replaceChildren(); note.hidden = true;
      showError(err instanceof Error ? err.message : String(err));
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
    st.charts = []; st.series = new Map(); st.main = st.price = st.vol = null;
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
    main.subscribeCrosshairMove((param) => describe(param && param.time != null ? param.time : null));
    setData();
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
      describe(null); renderHead();
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
      st.visible = true;
      if (!st.info) { if (error.hidden) open(); return; }
      if (!st.stopFeed) open();          // back on the tab: fresh candles, then live again
    },
    hide() { st.visible = false; stopLive(); },
  };
}
