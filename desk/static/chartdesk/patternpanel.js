// @ts-check
// The chart's Patterns panel: candle patterns marked on the chart, what followed each one on this chart's own
// candles (against an ordinary candle), the same counted across an index's stocks, and a shaded range of likely
// prices ahead. The sums are in patterns.js; this file draws them.
//
// Only closed candles count. On a live chart the newest candle is still forming while the market is open, so it
// is left out until it closes; a replay shows closed candles only. Nothing here is a prediction, and the panel
// says so where the numbers are.

import { DEFAULT_FILTERS, HORIZONS, KEYS, MIN_CASES, PATTERNS, cleanFilters, cone, detect, odds } from "./patterns.js";

/** @typedef {import("./indicators.js").Bar} Bar */
/** @typedef {{ on: boolean, names: boolean, cone: boolean, ahead: number, horizon: number, trend: string, place: string, volume: boolean,
 *   near: number, minCases: number, hide: string[] }} Settings */

export const DEFAULT_SETTINGS = /** @type {Settings} */ ({ on: false, names: false, cone: true, ahead: 5, horizon: 10, ...DEFAULT_FILTERS, minCases: MIN_CASES, hide: ["doji"] });
const CONE_STEPS = [5, 10, 20];

/** Settings as saved: known values only. @param {any} raw @returns {Settings} */
export function cleanSettings(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  const f = cleanFilters(r), min = Math.round(Number(r.minCases));
  return { on: r.on === true, names: r.names === true, cone: r.cone !== false, ahead: HORIZONS.includes(Number(r.ahead)) ? Number(r.ahead) : 5,
    horizon: CONE_STEPS.includes(Number(r.horizon)) ? Number(r.horizon) : 10, ...f,
    minCases: min >= 5 && min <= 500 ? min : MIN_CASES,
    hide: Array.isArray(r.hide) ? r.hide.filter((/** @type {any} */ k) => KEYS.includes(k)) : DEFAULT_SETTINGS.hide.slice() };
}

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
const SIGNAL = { up: "Up", down: "Down", neutral: "Either way" };
const pct = (/** @type {number|null|undefined} */ v) => (v == null ? "–" : `${v > 0 ? "+" : ""}${v.toFixed(2)}%`);
const share = (/** @type {number|null|undefined} */ v) => (v == null ? "–" : `${v.toFixed(1)}%`);

/** The newest candle that has closed: on a live chart during market hours the last one is still forming.
 * @param {number} n @param {{ replaying: boolean, live: boolean }} s */
export function closedCount(n, s) { return s.replaying || !s.live ? n : Math.max(0, n - 1); }

/**
 * @param {HTMLElement} root
 * @param {{ api: (path: string, opts?: any) => Promise<any>, bars: () => Bar[], info: () => any, venue: () => string, tf: () => string,
 *   replaying: () => boolean, live: () => boolean, chart: () => ({ main: any, price: any }|null), settings: () => Settings,
 *   save: (s: Settings) => void, format: (v: number) => string, when: (t: number) => string, heikinAshi: () => boolean,
 *   busyTool: () => boolean }} deps
 */
export function mountPatterns(root, deps) {
  const st = {
    found: /** @type {string[][]} */ ([]), result: /** @type {any} */ (null), cone: /** @type {any} */ (null), closed: -1, lastTime: 0,
    picked: /** @type {number|null} */ (null), pooled: /** @type {any} */ (null), pooledJob: /** @type {string|null} */ (null),
    pooledMsg: "", poll: /** @type {any} */ (0), lines: /** @type {any[]} */ ([]), layer: /** @type {ConeLayer|null} */ (null), key: "",
  };
  const S = () => deps.settings();
  /** @param {Partial<Settings>} change */
  const set = (change) => { deps.save({ ...S(), ...change }); };

  /** Works the patterns, odds and range out again when a candle has closed (or ``force``). */
  function compute(force = false) {
    const bars = deps.bars(), s = S();
    if (!s.on || !bars.length) { st.result = null; st.cone = null; st.found = []; return false; }
    const closed = closedCount(bars.length, { replaying: deps.replaying(), live: deps.live() });
    const lastTime = closed ? bars[closed - 1].time : 0;
    const key = JSON.stringify([s.trend, s.place, s.volume, s.near, s.minCases, s.horizon, s.cone]);
    if (!force && closed === st.closed && lastTime === st.lastTime && key === st.key) return false;
    st.closed = closed; st.lastTime = lastTime; st.key = key;
    const shown = bars.slice(0, closed);
    st.found = detect(shown);
    st.result = odds(shown, { filters: s, minCases: s.minCases, found: st.found });
    st.cone = s.cone ? cone(shown, s.horizon) : null;
    return true;
  }

  function markers() {
    const c = deps.chart(), s = S();
    if (!c) return;
    if (!s.on) { c.price.setMarkers([]); return; }
    const bars = deps.bars(), good = css("--good"), bad = css("--bad"), muted = css("--muted");
    /** @type {any[]} */ const out = [];
    st.found.forEach((keys, i) => keys.forEach((k) => {
      if (s.hide.includes(k)) return;
      const sig = PATTERNS[k][1];
      out.push({ time: bars[i].time, position: sig === "up" ? "belowBar" : "aboveBar", shape: sig === "up" ? "arrowUp" : sig === "down" ? "arrowDown" : "circle",
        color: sig === "up" ? good : sig === "down" ? bad : muted, text: s.names ? PATTERNS[k][0] : "", id: `${k}@${bars[i].time}` });
    }));
    c.price.setMarkers(out);
  }

  /** Redraws what is on the chart: markers, the range, the room to the right for it. */
  /** @param {boolean} [scroll] bring the newest candle and the range into view */
  function paint(scroll = false) {
    const c = deps.chart(), s = S();
    if (!c) return;
    markers();
    if (st.layer) {
      st.layer.set(s.on && st.cone ? st.cone : null, deps.bars().length, st.closed);
    }
    st.lines.forEach((line) => { try { c.price.removePriceLine(line); } catch (err) { /* chart redrawn */ } });
    st.lines = [];
    if (s.on && st.cone) {
      const end = st.cone.steps[st.cone.steps.length - 1], colour = css("--accent");
      st.lines = [["q90", "8 in 10 high"], ["q10", "8 in 10 low"]].map(([k, title]) => c.price.createPriceLine({ price: end[k], color: colour,
        lineVisible: false, axisLabelVisible: true, title }));
    }
    c.main.timeScale().applyOptions({ rightOffset: s.on && st.cone ? s.horizon + 3 : 4 });
    if (scroll) c.main.timeScale().scrollToRealTime();
  }

  // -- the panel -----------------------------------------------------------------------------------------
  /** @param {any} a */
  function afterCells(a) {
    return [h("td", { class: "num" }, `${share(a.up_pct)}`), h("td", { class: "num " + (a.avg_pct > 0 ? "up" : a.avg_pct < 0 ? "down" : "") }, pct(a.avg_pct)),
      h("td", { class: "num down" }, pct(a.worst_pct)), h("td", { class: "num up" }, pct(a.best_pct))];
  }
  /** @param {any} v */
  const verdictTag = (v) => h("span", { class: "cd-verdict v-" + v.key, title: v.z == null ? "" : `${v.z > 0 ? "+" : ""}${v.z} standard errors from the normal candle` }, v.text);

  /** @param {any} r the odds (this chart's or pooled) @param {string} title @param {string} unit */
  function oddsTable(r, title, unit) {
    const s = S(), hz = String(s.ahead), normal = r.normal[hz];
    const rows = r.patterns.slice().sort((a, b) => {
      const za = a.after[hz].verdict.z, zb = b.after[hz].verdict.z;
      if ((za == null) !== (zb == null)) return za == null ? 1 : -1;
      return za == null ? b.after[hz].cases - a.after[hz].cases : zb - za;
    });
    return h("div", { class: "cd-pat-block" },
      h("h4", { text: title }),
      h("p", { class: "hint" }, `Normal candle (any ${unit} that passes your filters): higher ${s.ahead} candle${s.ahead > 1 ? "s" : ""} later `,
        h("strong", { text: share(normal.up_pct) }), ` of ${normal.cases.toLocaleString("en-IN")} times, average ${pct(normal.avg_pct)}. A pattern is worth something only if it beats this.`),
      h("div", { class: "cd-table-wrap" }, h("table", { class: "cd-table cd-pat-table" },
        h("thead", {}, h("tr", {}, h("th", { text: "Pattern" }), h("th", { text: "Textbook" }), h("th", { class: "num", text: "Seen" }),
          h("th", { class: "num", text: `Higher after ${s.ahead}` }), h("th", { class: "num", text: "Average move" }), h("th", { class: "num", text: "Worst" }),
          h("th", { class: "num", text: "Best" }), h("th", { text: "What the past shows" }), r === st.result ? h("th", { text: "On chart" }) : null)),
        h("tbody", {}, rows.map((p) => {
          const a = p.after[hz];
          return h("tr", { class: a.verdict.key === "few" ? "cd-few" : "", title: PATTERNS[p.key][3] },
            h("td", {}, h("strong", { text: p.name })), h("td", { text: SIGNAL[/** @type {"up"|"down"|"neutral"} */ (p.signal)] }), h("td", { class: "num", text: String(a.cases) }),
            afterCells(a), h("td", {}, verdictTag(a.verdict)),
            r === st.result ? h("td", {}, h("input", { type: "checkbox", "aria-label": `Show ${p.name} on the chart`, checked: !S().hide.includes(p.key),
              onchange: (/** @type {Event} */ e) => { const on = /** @type {HTMLInputElement} */ (e.target).checked, hide = S().hide.filter((k) => k !== p.key); set({ hide: on ? hide : [...hide, p.key] }); markers(); } })) : null);
        })))),
      h("p", { class: "hint", text: `${r.tested} pattern and look-ahead pairs are tested, so a few look good by luck alone. Only a gap of 3 standard errors or more is called "more often than normal". Grey rows have fewer than ${s.minCases} cases.` }));
  }

  function conePart() {
    const s = S();
    if (!s.cone) return null;
    if (!st.cone) return h("p", { class: "hint", text: `Range of likely prices: needs at least ${s.horizon + 80} closed candles.` });
    const last = st.cone.steps[st.cone.steps.length - 1], f = deps.format;
    return h("div", { class: "cd-pat-block" },
      h("h4", { text: `Range of likely prices, next ${s.horizon} candles` }),
      h("p", {}, "Half of this stock's past moves of that length would end between ", h("strong", { text: `${f(last.q25)} and ${f(last.q75)}` }),
        ", and 8 in 10 between ", h("strong", { text: `${f(last.q10)} and ${f(last.q90)}` }), ` (middle ${f(last.q50)}, from ${f(st.cone.from)}).`),
      h("p", { class: "hint" }, `Built from ${st.cone.samples.toLocaleString("en-IN")} past moves of this chart, scaled to how much the price is moving now. `,
        st.cone.held ? `Checked on the newest third of the candles with the older two-thirds only: the 8-in-10 range held the price ${st.cone.held.pct}% of ${st.cone.held.checks} times (near 80% means the range is honest). ` : "",
        h("strong", { text: "Range from past moves, not a prediction." })));
  }

  function pickedPart() {
    if (st.picked == null || !st.result) return null;
    const bars = deps.bars(), i = bars.findIndex((b) => b.time === st.picked);
    const keys = i >= 0 && i < st.found.length ? st.found[i] : [];
    if (!keys.length) return null;
    return h("div", { class: "cd-pat-block cd-pat-picked" },
      h("div", { class: "cd-pat-pickhead" }, h("h4", { text: `Candle of ${deps.when(bars[i].time)}` }), h("button", { type: "button", class: "cd-x", "aria-label": "Close", text: "×", onclick: () => { st.picked = null; render(); } })),
      keys.map((k) => {
        const row = st.result.patterns.find((/** @type {any} */ p) => p.key === k);
        return h("div", { class: "cd-pat-one" },
          h("p", {}, h("strong", { text: PATTERNS[k][0] }), ` (textbook: ${SIGNAL[PATTERNS[k][1]].toLowerCase()}). ${PATTERNS[k][3]}`),
          h("div", { class: "cd-table-wrap" }, h("table", { class: "cd-table" },
            h("thead", {}, h("tr", {}, h("th", { text: "After" }), h("th", { class: "num", text: "Cases" }), h("th", { class: "num", text: "Higher" }), h("th", { class: "num", text: "Normal candle" }),
              h("th", { class: "num", text: "Average move" }), h("th", { class: "num", text: "Worst" }), h("th", { class: "num", text: "Best" }), h("th", { text: "What the past shows" }))),
            h("tbody", {}, HORIZONS.map((hz) => {
              const a = row.after[hz];
              return h("tr", { class: a.verdict.key === "few" ? "cd-few" : "" }, h("td", { text: `${hz} candle${hz > 1 ? "s" : ""}` }), h("td", { class: "num", text: String(a.cases) }),
                h("td", { class: "num", text: share(a.up_pct) }), h("td", { class: "num", text: share(st.result.normal[hz].up_pct) }),
                h("td", { class: "num " + (a.avg_pct > 0 ? "up" : a.avg_pct < 0 ? "down" : "") }, pct(a.avg_pct)), h("td", { class: "num down" }, pct(a.worst_pct)),
                h("td", { class: "num up" }, pct(a.best_pct)), h("td", {}, verdictTag(a.verdict)));
            })))));
      }));
  }

  function pooledPart() {
    if (deps.venue() !== "india" || deps.tf() !== "1d") {
      return h("p", { class: "hint", text: "Rare patterns (Morning Star, Three White Soldiers) show up only a few times on one stock. On NSE day candles you can add up the cases of every Nifty 50 stock." });
    }
    const running = !!st.pooledJob;
    return h("div", { class: "cd-pat-block" },
      h("div", { class: "cd-pat-row" },
        h("button", { type: "button", text: running ? "Counting…" : st.pooled ? "Count again across Nifty 50" : "Count across all Nifty 50 stocks", disabled: running, onclick: () => startPooled() }),
        running ? h("button", { type: "button", text: "Stop", onclick: () => stopPooled() }) : null,
        h("span", { class: "hint", text: st.pooledMsg })),
      st.pooled ? oddsTable(st.pooled, `All ${st.pooled.stocks} ${st.pooled.index_name} stocks, day candles`, "day of any of these stocks") : null,
      st.pooled ? h("p", { class: "hint", text: st.pooled.note + (st.pooled.skipped.length ? ` Skipped: ${st.pooled.skipped.join(", ")}.` : "") }) : null);
  }

  async function startPooled() {
    const s = S(), bars = deps.bars(), closed = closedCount(bars.length, { replaying: deps.replaying(), live: deps.live() });
    const end = closed ? new Date(bars[closed - 1].time * 1000).toISOString().slice(0, 10) : undefined;
    st.pooledMsg = "Loading day candles for each stock (first time takes a minute or two)…"; st.pooled = null;
    try {
      const { job } = await deps.api("/patterns/pooled", { method: "POST", body: { index: "nifty50", filters: { trend: s.trend, place: s.place, volume: s.volume, near: s.near }, min_cases: s.minCases, end } });
      st.pooledJob = job.id; render(); pollPooled();
    } catch (err) { st.pooledMsg = `Not counted: ${err.message}`; render(); }
  }
  async function stopPooled() { if (st.pooledJob) { try { await deps.api(`/jobs/${st.pooledJob}/stop`, { method: "POST" }); } catch (err) { /* finishes anyway */ } } }
  function pollPooled() {
    clearTimeout(st.poll);
    st.poll = setTimeout(async () => {
      if (!st.pooledJob) return;
      try {
        const job = await deps.api(`/jobs/${st.pooledJob}`);
        if (job.status === "done") { st.pooled = job.result; st.pooledJob = null; st.pooledMsg = ""; render(); return; }
        if (job.status === "error") { st.pooledJob = null; st.pooledMsg = `Not counted: ${job.error || "the count failed"}`; render(); return; }
        const p = job.progress || {};
        st.pooledMsg = p.total ? `${p.done} of ${p.total} stocks loaded…` : "Waiting for its turn…";
        const msg = root.querySelector(".cd-pat-row .hint");
        if (msg) msg.textContent = st.pooledMsg;
      } catch (err) { st.pooledMsg = `Lost track of the count: ${err.message}`; st.pooledJob = null; render(); return; }
      pollPooled();
    }, 1500);
  }

  function controls() {
    const s = S();
    const sel = (/** @type {string} */ label, /** @type {Record<string,string>} */ options, /** @type {string} */ value, /** @type {(v: string) => void} */ pick) =>
      h("label", {}, label, h("select", { onchange: (/** @type {Event} */ e) => pick(/** @type {HTMLSelectElement} */ (e.target).value) },
        Object.entries(options).map(([v, t]) => h("option", { value: v, text: t, selected: v === value }))));
    const num = (/** @type {string} */ label, /** @type {number} */ value, /** @type {number} */ min, /** @type {number} */ max, /** @type {number} */ step, /** @type {(v: number) => void} */ pick) =>
      h("label", {}, label, h("input", { type: "number", inputmode: "decimal", value: String(value), min: String(min), max: String(max), step: String(step),
        onchange: (/** @type {Event} */ e) => { const el = /** @type {HTMLInputElement} */ (e.target), v = Number(el.value); if (!(v >= min && v <= max)) { el.value = String(value); return; } pick(v); } }));
    return h("div", { class: "cd-pat-controls" },
      sel("Look ahead", { 1: "1 candle", 5: "5 candles", 10: "10 candles" }, String(s.ahead), (v) => set({ ahead: Number(v) })),
      sel("Trend before it", { any: "Any", up: "Uptrend", down: "Downtrend" }, s.trend, (v) => set({ trend: /** @type {any} */ (v) })),
      sel("Where", { any: "Anywhere", low: "Near a recent low", high: "Near a recent high" }, s.place, (v) => set({ place: /** @type {any} */ (v) })),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: s.names, onchange: (/** @type {Event} */ e) => { set({ names: /** @type {HTMLInputElement} */ (e.target).checked }); } }), "Pattern names on the chart"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: s.volume, onchange: (/** @type {Event} */ e) => set({ volume: /** @type {HTMLInputElement} */ (e.target).checked }) }), "Only on above-average volume"),
      h("label", { class: "check" }, h("input", { type: "checkbox", checked: s.cone, onchange: (/** @type {Event} */ e) => set({ cone: /** @type {HTMLInputElement} */ (e.target).checked }) }), "Range of likely prices"),
      s.cone ? sel("Range for the next", { 5: "5 candles", 10: "10 candles", 20: "20 candles" }, String(s.horizon), (v) => set({ horizon: Number(v) })) : null,
      h("details", { class: "cd-pat-adv" }, h("summary", { text: "Advanced" }),
        h("div", { class: "cd-pat-controls" },
          num("Fewest cases to trust", s.minCases, 5, 500, 1, (v) => set({ minCases: Math.round(v) })),
          num("Near a high or low: within × ATR(14)", s.near, 0.1, 5, 0.1, (v) => set({ near: v })),
          h("p", { class: "hint", text: "Trend: Uptrend when the candle before closed above its 50-candle average and the 20-candle average is above the 50; Downtrend when both are below. A recent low or high is the lowest low or highest high of the 20 candles before the pattern's last three. Volume is compared with the 20 candles before. A move is measured from the close of the pattern's last candle." }))));
  }

  function render() {
    const s = S();
    root.hidden = !s.on;
    if (!s.on) { root.replaceChildren(); return; }
    const r = st.result, vol = deps.bars().some((b) => b.volume > 0);
    root.replaceChildren(.../** @type {HTMLElement[]} */ ([
      h("div", { class: "cd-pat-head" }, h("h3", { text: "Candle patterns" }), h("span", { class: "hint", text: "Past odds on these candles, not promises. Tap a marked candle for its details; hover one to see its name." })),
      controls(),
      s.volume && !vol ? h("p", { class: "error-text", text: "This chart has no volume, so \"Only on above-average volume\" finds nothing." }) : null,
      deps.heikinAshi() ? h("p", { class: "hint", text: "Patterns are found on the real candles, not the Heikin Ashi ones drawn." }) : null,
      pickedPart(),
      conePart(),
      r ? oddsTable(r, `This chart: ${r.bars.toLocaleString("en-IN")} closed candles`, "candle") : h("p", { class: "hint", text: "No candles yet." }),
      pooledPart()].filter(Boolean)));
  }

  return {
    /** The chart was (re)built: attach the range layer and click handling, then paint. */
    chartDrawn() {
      const c = deps.chart();
      if (!c) return;
      st.layer = new ConeLayer({ q80: css("--accent"), mid: css("--text2"), text: css("--text2"), format: deps.format });
      c.price.attachPrimitive(st.layer);
      c.main.subscribeClick((/** @type {any} */ param) => {
        if (!S().on || deps.busyTool() || !param || param.time == null) return;
        const i = deps.bars().findIndex((b) => b.time === param.time);
        if (i < 0 || !(st.found[i] || []).length) return;
        st.picked = param.time; render();
        root.scrollIntoView({ behavior: "smooth", block: "nearest" });
      });
      compute(true); paint(); render();
    },
    /** Candles changed. Cheap unless a candle closed. @param {boolean} [force] */
    update(force) { if (compute(force)) { paint(); render(); } },
    /** Settings changed (from the panel or the toolbar button). */
    changed() { compute(true); paint(true); render(); },
    /** Empty candles to leave right of the last one, for the range. */
    room() { const s = S(); return s.on && s.cone ? s.horizon + 3 : 0; },
    /** A new symbol or timeframe: earlier picks and pooled counts no longer apply. */
    reset() { st.picked = null; st.closed = -1; if (!st.pooledJob) { st.pooled = null; st.pooledMsg = ""; } },
  };
}

/** The shaded range of likely prices ahead of the last closed candle, drawn in the empty space to the right
 * with the chart library's plugin interface (no made-up future dates on the time axis). */
class ConeLayer {
  /** @param {{ q80: string, mid: string, text: string, format: (v: number) => string }} colors */
  constructor(colors) {
    this.colors = colors;
    /** @type {any} */ this.data = null; this.total = 0; this.closed = 0;
    /** @type {any} */ this.chart = null; /** @type {any} */ this.series = null; /** @type {null|(() => void)} */ this.request = null;
  }
  /** @param {{ chart: any, series: any, requestUpdate: () => void }} p */
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.request = requestUpdate; }
  detached() { this.chart = this.series = this.request = null; }
  updateAllViews() { /* worked out when drawing */ }
  /** @param {any} data @param {number} total bars on the chart @param {number} closed */
  set(data, total, closed) { this.data = data; this.total = total; this.closed = closed; if (this.request) this.request(); }
  /** Keeps the range inside the price scale when it is in view. @param {number} from @param {number} to */
  autoscaleInfo(from, to) {
    if (!this.data || to < this.closed - 1) return null;
    const last = this.data.steps[this.data.steps.length - 1];
    return { priceRange: { minValue: Math.min(this.data.from, last.q10), maxValue: Math.max(this.data.from, last.q90) } };
  }
  paneViews() { return [{ zOrder: () => "bottom", renderer: () => ({ draw: (/** @type {any} */ t) => this.draw(t, false), drawBackground: (/** @type {any} */ t) => this.draw(t, true) }) }]; }
  /** @param {any} target @param {boolean} back */
  draw(target, back) {
    if (!this.chart || !this.series || !this.data) return;
    const ts = this.chart.timeScale(), s = this.series, start = this.closed - 1;
    const x0 = ts.logicalToCoordinate(start), y0 = s.priceToCoordinate(this.data.from);
    if (x0 == null || y0 == null) return;
    const pts = this.data.steps.map((/** @type {any} */ p) => ({ x: ts.logicalToCoordinate(start + p.ahead), y: (/** @type {string} */ k) => s.priceToCoordinate(p[k]) }));
    if (pts.some((/** @type {any} */ p) => p.x == null)) return;
    target.useBitmapCoordinateSpace((/** @type {any} */ scope) => {
      const ctx = scope.context, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
      const band = (/** @type {string} */ a, /** @type {string} */ b, /** @type {number} */ alpha) => {
        ctx.beginPath(); ctx.moveTo(x0 * hr, y0 * vr);
        pts.forEach((/** @type {any} */ p) => ctx.lineTo(p.x * hr, p.y(a) * vr));
        for (let i = pts.length - 1; i >= 0; i--) ctx.lineTo(pts[i].x * hr, pts[i].y(b) * vr);
        ctx.closePath(); ctx.globalAlpha = alpha; ctx.fill();
      };
      ctx.save();
      if (back) {
        ctx.fillStyle = this.colors.q80;
        band("q90", "q10", 0.10); band("q75", "q25", 0.16);
      } else {
        ctx.globalAlpha = 0.9; ctx.strokeStyle = this.colors.mid; ctx.setLineDash([4 * hr, 4 * hr]); ctx.lineWidth = hr;
        ctx.beginPath(); ctx.moveTo(x0 * hr, y0 * vr); pts.forEach((/** @type {any} */ p) => ctx.lineTo(p.x * hr, p.y("q50") * vr)); ctx.stroke();
        ctx.setLineDash([]);
      }
      ctx.restore();
    });
  }
}
