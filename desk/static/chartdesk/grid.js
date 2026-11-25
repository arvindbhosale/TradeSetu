// @ts-check
// Several charts in one window: 1, 2, 3, 4 or 6 charts, each with its own stock, candle size, indicators
// and drawings, in panes the user resizes by dragging the lines between them. Used by the chart window
// (charts.html, also what "Pop out" opens) and by "Several charts" on the Charts tab. Each pane is the
// Charts tab's chart in its compact form (chartdesk.js mount with compact: true), saved as view pane1..pane6.

import { mount } from "./chartdesk.js";

/** Each arrangement: columns and rows, and each chart's cell as [first column, after last column, first row,
 * after last row]. Keep in step with desk/chartstore.py GRID_LAYOUTS. */
export const LAYOUTS = /** @type {Record<string, { label: string, cols: number, rows: number, cells: number[][] }>} */ ({
  "1": { label: "1 chart", cols: 1, rows: 1, cells: [[0, 1, 0, 1]] },
  "2v": { label: "2 side by side", cols: 2, rows: 1, cells: [[0, 1, 0, 1], [1, 2, 0, 1]] },
  "2h": { label: "2, one above the other", cols: 1, rows: 2, cells: [[0, 1, 0, 1], [0, 1, 1, 2]] },
  "3": { label: "3: one large, two small", cols: 2, rows: 2, cells: [[0, 1, 0, 2], [1, 2, 0, 1], [1, 2, 1, 2]] },
  "4": { label: "4 in a square", cols: 2, rows: 2, cells: [[0, 1, 0, 1], [1, 2, 0, 1], [0, 1, 1, 2], [1, 2, 1, 2]] },
  "6": { label: "6: three by two", cols: 3, rows: 2, cells: [[0, 1, 0, 1], [1, 2, 0, 1], [2, 3, 0, 1], [0, 1, 1, 2], [1, 2, 1, 2], [2, 3, 1, 2]] },
});
/** The buttons' order (object keys that look like numbers would sort first). */
export const ORDER = ["1", "2v", "2h", "3", "4", "6"];
export const MAX_PANES = 6;
const GUTTER = 8;                 // px between panes, the handle that resizes them
const MIN_SHARE = 0.12;           // the smallest a column or row gets, as a share of the window,
const MIN_PX = { col: 300, row: 220 };   // and in pixels, so a chart's bar still fits
const STORE = "desk-chartgrid";

/** @typedef {{ layout: string, link: boolean, sizes: Record<string, { cols: number[], rows: number[] }> }} GridState */

/** The saved arrangement, made safe: a known layout and, for each, one size per column and row.
 * @param {any} raw @returns {GridState} */
export function cleanGrid(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  const layout = typeof r.layout === "string" && r.layout in LAYOUTS ? r.layout : "2v";
  /** @type {GridState["sizes"]} */ const sizes = {};
  const ok = (/** @type {any} */ a, /** @type {number} */ n) => Array.isArray(a) && a.length === n && a.every((v) => typeof v === "number" && Number.isFinite(v) && v > 0 && v <= 100);
  if (r.sizes && typeof r.sizes === "object") {
    for (const [k, v] of Object.entries(r.sizes)) {
      const l = LAYOUTS[k];
      if (l && v && ok(/** @type {any} */ (v).cols, l.cols) && ok(/** @type {any} */ (v).rows, l.rows)) sizes[k] = { cols: [.../** @type {any} */ (v).cols], rows: [.../** @type {any} */ (v).rows] };
    }
  }
  return { layout, link: r.link === true, sizes };
}

/** @param {string} layout @param {GridState["sizes"]} sizes */
export function sizesOf(layout, sizes) {
  const l = LAYOUTS[layout];
  return sizes[layout] || { cols: Array(l.cols).fill(1), rows: Array(l.rows).fill(1) };
}

/** A CSS grid track list with a gutter track between each pair. @param {number[]} fr */
export function tracks(fr) {
  return fr.map((f) => `minmax(0, ${Math.round(f * 1000) / 1000}fr)`).join(` ${GUTTER}px `);
}

/** Where a cell sits in the grid with gutters (1-based CSS lines). @param {number[]} cell */
export function placeOf(cell) {
  const [c0, c1, r0, r1] = cell;
  return { column: `${2 * c0 + 1} / ${2 * c1}`, row: `${2 * r0 + 1} / ${2 * r1}` };
}

/** The handles between panes: one per inner column line, as tall as the cells that border it, and one per
 * inner row line, as wide as the cells that border it. @param {string} layout */
export function handlesOf(layout) {
  const l = LAYOUTS[layout];
  /** @type {{ axis: "col"|"row", at: number, column: string, row: string }[]} */ const out = [];
  for (let k = 1; k < l.cols; k++) {
    const touching = l.cells.filter((c) => c[1] === k || c[0] === k);
    const r0 = Math.min(...touching.map((c) => c[2])), r1 = Math.max(...touching.map((c) => c[3]));
    out.push({ axis: "col", at: k, column: `${2 * k} / ${2 * k + 1}`, row: `${2 * r0 + 1} / ${2 * r1}` });
  }
  for (let k = 1; k < l.rows; k++) {
    const touching = l.cells.filter((c) => c[3] === k || c[2] === k);
    const c0 = Math.min(...touching.map((c) => c[0])), c1 = Math.max(...touching.map((c) => c[1]));
    out.push({ axis: "row", at: k, column: `${2 * c0 + 1} / ${2 * c1}`, row: `${2 * k} / ${2 * k + 1}` });
  }
  return out;
}

/** Moves the line between track ``at - 1`` and ``at`` by ``delta`` pixels out of ``space`` (the pixels the
 * tracks share), keeping every track at least MIN_SHARE of the whole and ``minPx`` pixels (less only when the
 * two tracks together are too small for that). @param {number[]} fr @param {number} at @param {number} delta
 * @param {number} space @param {number} [minPx] @returns {number[]} */
export function resize(fr, at, delta, space, minPx = 0) {
  const total = fr.reduce((a, b) => a + b, 0), out = fr.map((f) => f / total);
  if (!(space > 0)) return out;
  const pair = out[at - 1] + out[at], min = Math.min(Math.max(MIN_SHARE, minPx / space), pair / 2);
  const left = Math.max(min, Math.min(pair - min, out[at - 1] + delta / space));
  out[at - 1] = left; out[at] = pair - left;
  return out.map((f) => Math.round(f * 10000) / 10000);
}

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
  for (const c of children.flat()) if (c != null && c !== false) el.append(c instanceof Node ? c : String(c));
  return el;
}

/** A small picture of an arrangement for its button. @param {string} key */
function icon(key) {
  const l = LAYOUTS[key], ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 18"); svg.setAttribute("aria-hidden", "true"); svg.setAttribute("class", "cdg-icon");
  const w = 24 / l.cols, hgt = 18 / l.rows;
  l.cells.forEach(([c0, c1, r0, r1]) => {
    const r = document.createElementNS(ns, "rect");
    r.setAttribute("x", String(c0 * w + 1)); r.setAttribute("y", String(r0 * hgt + 1));
    r.setAttribute("width", String((c1 - c0) * w - 2)); r.setAttribute("height", String((r1 - r0) * hgt - 2)); r.setAttribute("rx", "1.5");
    svg.append(r);
  });
  return svg;
}

/** The Charts tab's own chart settings on this device: what a new pane starts with. */
function liveStart() {
  try {
    const saved = JSON.parse(localStorage.getItem("desk-chartdesk") || "null");
    if (saved && typeof saved === "object") {
      const { venue, symbol, tf, type, volume, indicators } = saved;
      return { venue, symbol, tf, type, volume, indicators };
    }
  } catch (err) { /* storage blocked */ }
  return null;
}

/**
 * @param {HTMLElement} root
 * @param {{ api: (path: string, opts?: any) => Promise<any>, mode?: () => string, window?: boolean,
 *   seed?: { venue: string, symbol: string, tf?: string }|null, openWindow?: (() => void)|null }} deps
 *   ``window``: this is the chart window (charts.html), not the Charts tab. ``seed``: a popped-out chart, shown
 *   alone in pane 1 until the user picks another arrangement. ``openWindow`` adds "Open in a new window".
 */
export function mountGrid(root, { api, mode = () => "paper", window: own = false, seed = null, openWindow = null }) {
  /** @type {GridState} */ let gs = load();
  if (seed) gs = { ...gs, layout: "1" };
  let touched = false, visible = false, saveTimer = /** @type {any} */ (0), following = false;
  /** @type {({ el: HTMLElement, mod: any }|null)[]} */ const panes = Array(MAX_PANES).fill(null);
  const start = liveStart();

  const pick = h("div", { class: "seg cdg-layouts", role: "group", "aria-label": "How many charts" });
  const link = /** @type {HTMLInputElement} */ (h("input", { type: "checkbox", onchange: () => { gs.link = link.checked; save(); } }));
  const equal = h("button", { type: "button", class: "cd-btn", text: "Equal sizes", title: "Make every chart the same size",
    onclick: () => { delete gs.sizes[gs.layout]; save(); place(); } });
  const board = h("div", { class: "cdg-board" });
  const bar = h("div", { class: "cdg-bar" }, h("span", { class: "cdg-label", text: "Charts" }), pick,
    h("label", { class: "check cdg-link", title: "Change the stock in one chart and the others follow, for one stock on several candle sizes" }, link, "Same stock in every chart"),
    equal,
    openWindow ? h("button", { type: "button", class: "cd-btn", text: "Open in a new window", onclick: () => openWindow() }) : null,
    own ? h("a", { class: "cd-btn cdg-home", href: "/", text: "Back to the desk", hidden: !(/** @type {any} */ (window).__deskNativeApp === "mobile" || !window.opener) }) : null);
  root.replaceChildren(h("div", { class: "cdg" + (own ? " cdg-window" : "") }, bar, board,
    h("p", { class: "hint cd-credit cdg-credit" }, "Charts drawn with ",
      h("a", { href: "https://www.tradingview.com/", target: "_blank", rel: "noopener noreferrer", text: "TradingView Lightweight Charts" }),
      ". Crypto prices from Binance or Coinbase public data; NSE/BSE from your broker. Drag the lines between charts to resize them; double-click a line to even it out. Orders are placed from the Charts tab.")));

  function load() {
    try { return cleanGrid(JSON.parse(localStorage.getItem(STORE) || "null")); } catch (err) { return cleanGrid(null); }
  }
  function save() {
    touched = true;
    try { localStorage.setItem(STORE, JSON.stringify(gs)); } catch (err) { /* storage blocked */ }
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => { api("/md/grid", { method: "PUT", body: { grid: gs } }).catch(() => { /* this device keeps it */ }); }, 600);
  }
  api("/md/grid").then((r) => {
    if (r && r.grid && !touched) { gs = cleanGrid(r.grid); if (seed) gs.layout = "1"; render(); }
  }).catch(() => { /* this device's copy */ });

  /** @param {number} i */
  function paneAt(i) {
    let p = panes[i];
    if (p) return p;
    const el = h("div", { class: "cdg-cell", "aria-label": `Chart ${i + 1}`, role: "region" });
    board.append(el);
    const mod = mount(el, { api, mode, compact: true, view: `pane${i + 1}`, start,
      seed: i === 0 && seed ? { venue: seed.venue, symbol: seed.symbol, ...(seed.tf ? { tf: seed.tf } : {}) } : null,
      onSymbol: (venue, symbol) => follow(i, venue, symbol) });
    p = panes[i] = { el, mod };
    return p;
  }

  /** "Same stock in every chart": the other charts open the stock this one changed to. @param {number} from @param {string} venue @param {string} symbol */
  function follow(from, venue, symbol) {
    if (!gs.link || following) return;
    following = true;
    try { panes.forEach((p, i) => { if (p && i !== from) p.mod.setSymbol(venue, symbol); }); }
    finally { following = false; }
  }

  function render() {
    pick.replaceChildren(...ORDER.map((key) => { const l = LAYOUTS[key]; return h("button", { type: "button", "aria-pressed": String(key === gs.layout), title: l.label, "aria-label": l.label,
      onclick: () => { if (key === gs.layout) return; gs.layout = key; save(); render(); } }, icon(key), h("span", { class: "cdg-n", text: String(l.cells.length) })); }));
    link.checked = gs.link;
    place();
  }

  /** Puts each chart in its cell and the handles between them; charts not in this arrangement stop. */
  function place() {
    const l = LAYOUTS[gs.layout], sz = sizesOf(gs.layout, gs.sizes);
    board.style.gridTemplateColumns = tracks(sz.cols);
    board.style.gridTemplateRows = tracks(sz.rows);
    board.dataset.count = String(l.cells.length);
    board.querySelectorAll(".cdg-handle").forEach((x) => x.remove());
    for (let i = 0; i < MAX_PANES; i++) {
      const cell = l.cells[i];
      if (!cell) { const p = panes[i]; if (p) { p.el.hidden = true; p.mod.hide(); } continue; }
      const p = paneAt(i), at = placeOf(cell);
      p.el.hidden = false; p.el.style.gridColumn = at.column; p.el.style.gridRow = at.row;
      if (visible) p.mod.show();
    }
    handlesOf(gs.layout).forEach((hd) => {
      const el = h("div", { class: `cdg-handle cdg-${hd.axis}`, role: "separator", tabindex: "0",
        "aria-orientation": hd.axis === "col" ? "vertical" : "horizontal", "aria-label": hd.axis === "col" ? "Resize the charts left and right" : "Resize the charts above and below",
        title: "Drag to resize. Double-click to even out." });
      el.style.gridColumn = hd.column; el.style.gridRow = hd.row;
      el.addEventListener("pointerdown", (e) => drag(e, hd.axis, hd.at, el));
      el.addEventListener("dblclick", () => { setTrack(hd.axis, hd.at, null); });
      el.addEventListener("keydown", (e) => {
        const step = e.shiftKey ? 60 : 20;
        const d = hd.axis === "col" ? (e.key === "ArrowLeft" ? -step : e.key === "ArrowRight" ? step : 0) : (e.key === "ArrowUp" ? -step : e.key === "ArrowDown" ? step : 0);
        if (!d) return;
        e.preventDefault(); setTrack(hd.axis, hd.at, d); save();
      });
      board.append(el);
    });
  }

  /** @param {"col"|"row"} axis */
  function space(axis) {
    const r = board.getBoundingClientRect(), l = LAYOUTS[gs.layout], n = axis === "col" ? l.cols : l.rows;
    return (axis === "col" ? r.width : r.height) - GUTTER * (n - 1);
  }

  /** Moves one line by ``delta`` px, or evens out the two tracks beside it when ``delta`` is null.
   * @param {"col"|"row"} axis @param {number} at @param {number|null} delta @param {number[]} [from] */
  function setTrack(axis, at, delta, from) {
    const sz = sizesOf(gs.layout, gs.sizes), key = axis === "col" ? "cols" : "rows";
    let next = (from || sz[key]).slice();
    if (delta == null) { const half = (next[at - 1] + next[at]) / 2; next[at - 1] = next[at] = half; }
    else next = resize(next, at, delta, space(axis), MIN_PX[axis]);
    gs.sizes = { ...gs.sizes, [gs.layout]: { ...sz, [key]: next } };
    if (delta == null) save();
    board.style.gridTemplateColumns = tracks(gs.sizes[gs.layout].cols);
    board.style.gridTemplateRows = tracks(gs.sizes[gs.layout].rows);
  }

  /** @param {PointerEvent} e @param {"col"|"row"} axis @param {number} at @param {HTMLElement} el */
  function drag(e, axis, at, el) {
    if (e.button !== 0) return;
    e.preventDefault();
    const sz = sizesOf(gs.layout, gs.sizes), from = (axis === "col" ? sz.cols : sz.rows).slice();
    const x0 = e.clientX, y0 = e.clientY;
    el.setPointerCapture(e.pointerId); el.classList.add("dragging"); board.classList.add("resizing");
    let frame = 0, last = 0;
    const move = (/** @type {PointerEvent} */ ev) => {
      last = axis === "col" ? ev.clientX - x0 : ev.clientY - y0;
      if (!frame) frame = requestAnimationFrame(() => { frame = 0; setTrack(axis, at, last, from); });
    };
    const up = () => {
      el.removeEventListener("pointermove", move); el.removeEventListener("pointerup", up); el.removeEventListener("pointercancel", up);
      if (frame) { cancelAnimationFrame(frame); frame = 0; }
      setTrack(axis, at, last, from);
      el.classList.remove("dragging"); board.classList.remove("resizing");
      if (last) save();
    };
    el.addEventListener("pointermove", move); el.addEventListener("pointerup", up); el.addEventListener("pointercancel", up);
  }

  render();
  return {
    show() { visible = true; LAYOUTS[gs.layout].cells.forEach((_, i) => paneAt(i).mod.show()); },
    hide() { visible = false; panes.forEach((p) => { if (p) p.mod.hide(); }); },
    modeChanged() { /* panes place no orders */ },
  };
}
