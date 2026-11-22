// @ts-check
// Crypto paper trading under the chart: a virtual USDT balance, market and limit orders, positions,
// waiting orders and history. Practice only: nothing here reaches an exchange or a broker.

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

/** @param {number|null|undefined} v @param {number} [dp] */
const usd = (v, dp = 2) => (v == null ? "–" : v.toLocaleString("en-US", { minimumFractionDigits: Math.min(dp, 2), maximumFractionDigits: dp }));
/** @param {number} v */
const qty = (v) => v.toLocaleString("en-US", { maximumFractionDigits: 8 });
/** @param {number|null|undefined} v */
const signed = (v) => (v == null ? "–" : `${v >= 0 ? "+" : ""}${usd(v)}`);
/** @param {number} t */
const when = (t) => new Date(t * 1000).toLocaleString("en-IN", { day: "2-digit", month: "short", hour: "2-digit", minute: "2-digit" });

/**
 * @param {HTMLElement} root
 * @param {{ api: (path: string, opts?: any) => Promise<any>, symbol: () => string, lastPrice: () => number|null,
 *   onBook: (book: any) => void }} deps
 */
export function mountPaper(root, { api, symbol, lastPrice, onBook }) {
  const st = { side: "BUY", type: "MARKET", tab: "positions", book: /** @type {any} */ (null), timer: /** @type {any} */ (null), armed: false };
  const qtyIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", placeholder: "0.01", "aria-label": "Quantity" }));
  const priceIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", "aria-label": "Limit price" }));
  const balanceIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "100", step: "100", value: "10000", "aria-label": "Starting balance" }));
  const worth = h("span", { class: "hint" });
  const msg = h("div", { class: "cd-paper-msg", role: "status", hidden: true });
  const summary = h("div", { class: "cd-paper-summary" });
  const tabs = h("div", { class: "seg", role: "group", "aria-label": "Paper book" });
  const body = h("div", { class: "cd-paper-body" });
  const sideSeg = h("div", { class: "seg cd-side", role: "group", "aria-label": "Buy or sell" });
  const typeSeg = h("div", { class: "seg", role: "group", "aria-label": "Order type" });
  const priceLabel = h("label", {}, "Limit price (USDT)", priceIn);
  const submit = /** @type {HTMLButtonElement} */ (h("button", { type: "submit", class: "primary" }));
  const resetBtn = /** @type {HTMLButtonElement} */ (h("button", { type: "button", class: "cd-reset", onclick: () => reset() }));

  root.replaceChildren(
    h("div", { class: "cd-paper-headline" }, h("strong", { text: "Paper trading" }),
      h("span", { class: "pill", text: "Practice money · no real orders" })),
    summary,
    h("form", { class: "cd-ticket", onsubmit: (e) => { e.preventDefault(); place(); } },
      sideSeg, typeSeg, h("label", {}, "Quantity", qtyIn), priceLabel, submit, worth),
    msg, tabs, body,
    h("details", { class: "cd-adv" }, h("summary", { text: "Advanced: fees, reset" }),
      h("p", { class: "hint", id: "cd-paper-rules" }),
      h("div", { class: "cd-reset-row" }, h("label", {}, "Starting balance (USDT)", balanceIn), resetBtn)));

  function seg(host, options, current, pick) {
    host.replaceChildren(...Object.entries(options).map(([k, label]) => h("button", { type: "button", "aria-pressed": String(k === current), text: label, onclick: () => pick(k) })));
  }

  function renderTicket() {
    seg(sideSeg, { BUY: "Buy", SELL: "Sell" }, st.side, (k) => { st.side = k; renderTicket(); });
    seg(typeSeg, { MARKET: "Market", LIMIT: "Limit" }, st.type, (k) => {
      st.type = k; if (k === "LIMIT" && !priceIn.value && lastPrice()) priceIn.value = String(lastPrice()); renderTicket();
    });
    priceLabel.hidden = st.type !== "LIMIT";
    sideSeg.dataset.side = st.side;
    const coin = symbol().replace(/USDT$/, "");
    submit.textContent = `${st.side === "BUY" ? "Buy" : "Sell"} ${coin}`;
    submit.className = "primary " + (st.side === "BUY" ? "cd-buy" : "cd-sell");
    qtyIn.placeholder = `${coin} amount`;
    const p = st.type === "LIMIT" ? Number(priceIn.value) : lastPrice();
    const q = Number(qtyIn.value);
    worth.textContent = q > 0 && p ? `≈ ${usd(q * p)} USDT` : "";
    resetBtn.textContent = st.armed ? "Press again to delete all paper data" : "Reset paper account";
    resetBtn.classList.toggle("danger", st.armed);
  }
  qtyIn.addEventListener("input", renderTicket); priceIn.addEventListener("input", renderTicket);

  function show(text, bad) { msg.textContent = text; msg.className = "cd-paper-msg " + (bad ? "error" : "ok"); msg.hidden = !text; }

  async function place() {
    submit.disabled = true;
    try {
      const body = { symbol: symbol(), side: st.side, type: st.type, quantity: Number(qtyIn.value) };
      if (st.type === "LIMIT") body.price = Number(priceIn.value);
      const r = await api("/cpaper/order", { method: "POST", body });
      show(r.filled ? `${r.trade.side === "BUY" ? "Bought" : "Sold"} ${qty(r.trade.quantity)} ${r.trade.symbol.replace(/USDT$/, "")} at ${usd(r.trade.price, 4)} (fee ${usd(r.trade.fee, 4)} USDT).`
        : `Limit order placed: ${r.order.side === "BUY" ? "buy" : "sell"} ${qty(r.order.quantity)} at ${usd(r.order.price, 4)}. It fills when the price gets there.`, false);
      update(r.book);
    } catch (err) { show(err.message, true); }
    submit.disabled = false;
  }

  async function cancel(id) {
    try { update(await api(`/cpaper/order/${id}`, { method: "DELETE" })); show("Order cancelled.", false); }
    catch (err) { show(err.message, true); }
  }

  async function reset() {
    if (!st.armed) { st.armed = true; renderTicket(); setTimeout(() => { st.armed = false; renderTicket(); }, 5000); return; }
    st.armed = false;
    try { update(await api("/cpaper/reset", { method: "POST", body: { balance: Number(balanceIn.value) } })); show("Paper account reset. All paper orders, positions and history were deleted.", false); }
    catch (err) { show(err.message, true); }
    renderTicket();
  }

  function update(book) {
    st.book = book; onBook(book); render();
  }

  function render() {
    const b = st.book;
    if (!b) return;
    const missing = b.missing_prices.length ? `No live price for ${b.missing_prices.join(", ")}, so totals are not shown.` : "";
    summary.replaceChildren(h("div", { class: "cd-tiles" },
      tile("Account value", b.equity == null ? "–" : `${usd(b.equity)} USDT`),
      tile("Profit / loss", b.pnl == null ? "–" : `${signed(b.pnl)} (${b.pnl_pct >= 0 ? "+" : ""}${b.pnl_pct.toFixed(2)}%)`, b.pnl == null ? "" : b.pnl >= 0 ? "up" : "down"),
      tile("Cash free", `${usd(b.free_cash)} USDT`),
      tile("Fees paid", `${usd(b.fees)} USDT`),
      missing || b.check_error ? h("p", { class: "error cd-paper-warn", text: [missing, b.check_error && `Waiting orders could not be checked: ${b.check_error}`].filter(Boolean).join(" ") }) : null));
    seg(tabs, { positions: `Positions (${b.positions.length})`, orders: `Waiting orders (${b.orders.length})`, history: `History (${b.trade_count})` }, st.tab, (k) => { st.tab = k; render(); });
    const rules = root.querySelector("#cd-paper-rules");
    if (rules) rules.textContent = `Each fill pays a ${b.fee_pct}% fee. The smallest order is ${b.min_order} USDT. Waiting limit orders are checked against the last price every ${b.check_seconds} seconds on the server, so they fill even with this page closed. History keeps the last ${b.max_trades.toLocaleString("en-US")} fills. Reset deletes all paper orders, positions and history.`;
    if (st.tab === "positions") {
      body.replaceChildren(b.positions.length ? table(["Coin", "Quantity", "Average cost", "Last", "Value", "Profit / loss"],
        b.positions.map((p) => [p.coin, qty(p.quantity), usd(p.avg, 4), p.last == null ? h("span", { class: "cd-watch-err", title: p.error || "", text: "No price" }) : usd(p.last, 4),
          p.value == null ? "–" : usd(p.value), p.unrealized == null ? "–" : h("span", { class: p.unrealized >= 0 ? "up" : "down", text: `${signed(p.unrealized)} (${p.unrealized_pct.toFixed(2)}%)` })]))
        : h("p", { class: "hint", text: "No positions. Buy a coin above to start." }));
    } else if (st.tab === "orders") {
      body.replaceChildren(b.orders.length ? table(["Placed", "Order", "Limit price", ""],
        b.orders.map((o) => [when(o.created), `${o.side === "BUY" ? "Buy" : "Sell"} ${qty(o.quantity)} ${o.symbol.replace(/USDT$/, "")}`, usd(o.price, 4),
          h("button", { type: "button", class: "cd-cancel", text: "Cancel", onclick: () => cancel(o.id) })]))
        : h("p", { class: "hint", text: "No waiting orders." }));
    } else {
      body.replaceChildren(b.trades.length ? table(["Time", "Trade", "Price", "Fee", "Realised profit"],
        b.trades.map((t) => [when(t.time), `${t.side === "BUY" ? "Bought" : "Sold"} ${qty(t.quantity)} ${t.symbol.replace(/USDT$/, "")} (${t.kind})`, usd(t.price, 4), usd(t.fee, 4),
          t.realized == null ? "–" : h("span", { class: t.realized >= 0 ? "up" : "down", text: signed(t.realized) })]))
        : h("p", { class: "hint", text: "No trades yet." }));
    }
  }

  function tile(label, value, cls = "") { return h("div", { class: "cd-tile" }, h("span", { class: "hint", text: label }), h("strong", { class: cls, text: value })); }
  function table(head, rows) {
    return h("div", { class: "cd-table-wrap" }, h("table", { class: "cd-table" },
      h("thead", {}, h("tr", {}, head.map((x) => h("th", { text: x })))),
      h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => h("td", {}, c)))))));
  }

  async function refresh() {
    try { update(await api("/cpaper")); }
    catch (err) { summary.replaceChildren(h("p", { class: "error", text: `Paper account not loaded: ${err.message}` })); }
  }

  renderTicket();
  return {
    show() { root.hidden = false; renderTicket(); if (!st.timer) { refresh(); st.timer = setInterval(refresh, POLL_MS); } },
    hide() { root.hidden = true; if (st.timer) { clearInterval(st.timer); st.timer = null; } },
    symbolChanged() { priceIn.value = ""; qtyIn.value = ""; show("", false); renderTicket(); },
    priceChanged() { renderTicket(); },
  };
}
