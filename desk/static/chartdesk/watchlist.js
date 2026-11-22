// @ts-check
// The watchlist beside the chart: saved on the server, prices refreshed every few seconds while it shows.
// A symbol whose price could not be loaded shows the reason, never a zero.

const POLL_MS = 5000;

/** @param {string} tag @param {Record<string, any>} [attrs] @param {...any} kids */
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

/** @param {string} venue @param {number} v */
export function formatPrice(venue, v) {
  const dp = venue !== "crypto" || v >= 1000 ? 2 : v >= 1 ? 4 : Math.min(10, Math.max(4, Math.ceil(-Math.log10(v)) + 3));
  const text = v.toLocaleString(venue === "crypto" ? "en-US" : "en-IN", { minimumFractionDigits: Math.min(dp, 2), maximumFractionDigits: dp });
  return venue === "crypto" ? text : "₹" + text;
}

/**
 * @param {HTMLElement} root
 * @param {{ api: (path: string, opts?: any) => Promise<any>, open: (venue: string, symbol: string) => void,
 *   current: () => { venue: string, symbol: string } }} deps
 */
export function mountWatchlist(root, { api, open, current }) {
  /** @type {{venue: string, symbol: string}[]} */ let items = [];
  /** @type {Record<string, any>} */ let prices = {};
  let timer = /** @type {any} */ (null), error = "", loaded = false;

  const key = (i) => `${i.venue}:${i.symbol}`;
  const list = h("ul", { class: "cd-watch-list" });
  const status = h("p", { class: "hint cd-watch-status", hidden: true });
  const addBtn = h("button", { type: "button", class: "cd-watch-add", text: "+ Add this chart", onclick: () => add() });
  root.replaceChildren(h("div", { class: "cd-watch-head" }, h("strong", { text: "Watchlist" }), addBtn), status, list);

  function render() {
    const now = current();
    addBtn.hidden = items.some((i) => i.venue === now.venue && i.symbol === now.symbol);
    status.textContent = error; status.hidden = !error;
    list.replaceChildren(...items.map((i) => {
      const p = prices[key(i)];
      const active = i.venue === now.venue && i.symbol === now.symbol;
      const value = !p ? h("span", { class: "hint", text: loaded ? "…" : "" })
        : p.error ? h("span", { class: "cd-watch-err", title: p.error, text: "No price" })
        : h("span", { class: "cd-watch-price" }, formatPrice(i.venue, p.price),
          p.change_pct == null ? null : h("small", { class: p.change_pct >= 0 ? "up" : "down", text: ` ${p.change_pct >= 0 ? "+" : ""}${p.change_pct.toFixed(2)}%` }));
      return h("li", { class: active ? "active" : "" },
        h("button", { type: "button", class: "cd-watch-open", title: p && p.error ? p.error : `${i.symbol} (${i.venue === "india" ? "NSE/BSE" : i.venue})`,
          onclick: () => open(i.venue, i.symbol) },
          h("span", { class: "cd-watch-sym", text: i.symbol }), value),
        h("button", { type: "button", class: "cd-x", "aria-label": `Remove ${i.symbol} from the watchlist`, text: "×", onclick: () => remove(i) }));
    }));
    if (!items.length) list.append(h("li", { class: "hint", text: "Empty. Open a chart and press Add." }));
  }

  async function save(next) {
    try { items = (await api("/md/watchlist", { method: "PUT", body: { items: next } })).items; error = ""; }
    catch (err) { error = `Watchlist not saved: ${err.message}`; }
    render(); refresh();
  }
  function add() { const c = current(); save([...items, { venue: c.venue, symbol: c.symbol }]); }
  function remove(i) { save(items.filter((x) => key(x) !== key(i))); }

  async function refresh() {
    if (!items.length) return;
    try { prices = (await api("/md/prices")).prices; loaded = true; error = ""; }
    catch (err) { error = `Prices not updated: ${err.message}`; }
    render();
  }

  async function load() {
    try { items = (await api("/md/watchlist")).items; error = ""; }
    catch (err) { error = `Watchlist not loaded: ${err.message}`; }
    render(); refresh();
  }

  return {
    show() { if (!timer) { load(); timer = setInterval(refresh, POLL_MS); } },
    hide() { if (timer) { clearInterval(timer); timer = null; } },
    render,
  };
}
