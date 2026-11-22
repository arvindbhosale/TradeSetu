// @ts-check
// Trading from the chart: Buy and Sell with a stoploss and a target drawn as lines on the price chart.
// Drag the Stoploss, Target or Limit price line to set it before the order; drag a placed order's line to move
// its stoploss or target, which asks for a confirmation first. Every order is reviewed before it is sent:
// NSE/BSE orders take the desk's own order path (paper or live, the same risk limits, kill switch and trade window
// as the Trade tab), crypto orders go to the paper book only.

const POLL_MS = 10000;
const HIT_PX = 7;
const DEFAULT_STOP = 0.01;      // "Add on chart" puts the stoploss 1% under the price and the target 2% over it
const DEFAULT_TARGET = 0.02;

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

/** Round a dragged or typed price to what the market accepts: 0.05 rupee ticks, or the coin's decimals.
 * @param {string} venue @param {number} price @param {number} decimals */
export function roundPrice(venue, price, decimals) {
  if (!(price > 0)) return 0;
  if (venue === "india") return Math.round(Math.round(price / 0.05) * 0.05 * 100) / 100;
  const f = 10 ** decimals;
  return Math.round(price * f) / f;
}

/** Profit (above zero) or loss (below) if a buy of ``quantity`` at ``entry`` sells at ``exit``, before charges.
 * @param {number} quantity @param {number} entry @param {number} exit */
export function outcome(quantity, entry, exit) { return (exit - entry) * quantity; }

/** Starting stoploss and target around a price. @param {number} price */
export function defaultLevels(price) { return { stop: price * (1 - DEFAULT_STOP), target: price * (1 + DEFAULT_TARGET) }; }

/** The price line nearest a y coordinate, if within reach. @param {{y: number|null}[]} lines @param {number} y */
export function nearest(lines, y) {
  let best = -1, gap = HIT_PX + 1;
  lines.forEach((l, i) => { if (l.y != null && Math.abs(l.y - y) < gap) { gap = Math.abs(l.y - y); best = i; } });
  return best;
}

/**
 * @param {HTMLElement} root
 * @param {{ api: (path: string, opts?: any) => Promise<any>, venue: () => string, symbol: () => string,
 *   lastPrice: () => number|null, decimals: () => number, mode: () => string, money: (v: number) => string,
 *   chart: () => ({ main: any, price: any, box: HTMLElement }|null), onBook: (book: any) => void }} deps
 */
export function mountTrade(root, deps) {
  const { api } = deps;
  const st = {
    side: "BUY", type: "LIMIT", product: "MIS", visible: false, timer: /** @type {any} */ (null),
    stop: /** @type {number|null} */ (null), target: /** @type {number|null} */ (null), price: /** @type {number|null} */ (null),
    items: /** @type {any[]} */ ([]), book: /** @type {any} */ (null), lines: /** @type {any[]} */ ([]),
    drag: /** @type {any} */ (null), busy: false, scaled: /** @type {any} */ (null), levels: /** @type {number[]} */ ([]),
  };
  const qtyIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", name: "cd-t-qty", "aria-label": "Quantity" }));
  const priceIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", name: "cd-t-price", "aria-label": "Limit price" }));
  const stopIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", name: "cd-t-stop", "aria-label": "Stoploss" }));
  const targetIn = /** @type {HTMLInputElement} */ (h("input", { type: "number", min: "0", step: "any", inputmode: "decimal", name: "cd-t-target", "aria-label": "Target" }));
  const sideSeg = h("div", { class: "seg cd-side", role: "group", "aria-label": "Buy or sell" });
  const typeSeg = h("div", { class: "seg", role: "group", "aria-label": "Order type" });
  const productSeg = h("div", { class: "seg", role: "group", "aria-label": "Intraday or delivery" });
  const priceLabel = h("label", {}, "Limit price", priceIn);
  const stopLabel = h("label", {}, "Stoploss", stopIn);
  const targetLabel = h("label", {}, "Target", targetIn);
  const levelsBtn = /** @type {HTMLButtonElement} */ (h("button", { type: "button", class: "cd-t-lines", onclick: () => toggleLevels() }));
  const submit = /** @type {HTMLButtonElement} */ (h("button", { type: "submit", class: "primary" }));
  const summary = h("p", { class: "hint cd-t-sum" });
  const modePill = h("span", { class: "pill" });
  const msg = h("div", { class: "cd-paper-msg", role: "status", hidden: true });
  const list = h("div", { class: "cd-t-list" });
  const off = h("p", { class: "hint", hidden: true });
  const form = h("form", { class: "cd-ticket cd-t-form", onsubmit: (e) => { e.preventDefault(); review(); } },
    sideSeg, typeSeg, productSeg, h("label", {}, "Quantity", qtyIn), priceLabel, stopLabel, targetLabel, levelsBtn, submit);
  const dialog = /** @type {HTMLDialogElement} */ (h("dialog", { class: "cd-confirm" }));
  root.replaceChildren(
    h("div", { class: "cd-paper-headline" }, h("strong", { text: "Trade from the chart" }), modePill),
    off, form, summary, msg, list, dialog,
    h("p", { class: "hint cd-t-help", text: "Drag the Stoploss, Target or Limit price line on the chart to set it. Drag a placed order's line to move its stoploss or target; you are asked to confirm first." }));

  const venue = () => deps.venue();
  const india = () => venue() === "india";
  const fmt = (/** @type {number} */ v) => deps.money(v);
  const num = (/** @type {HTMLInputElement} */ el) => { const v = Number(el.value); return el.value !== "" && Number.isFinite(v) && v > 0 ? v : null; };
  const round = (/** @type {number} */ p) => roundPrice(venue(), p, deps.decimals());
  const entry = () => (st.type === "LIMIT" && num(priceIn)) || deps.lastPrice();
  const unit = () => (india() ? "" : " USDT");
  const signedMoney = (/** @type {number} */ v) => `${v >= 0 ? "+" : "−"}${fmt(Math.abs(v))}${unit()}`;

  function seg(host, options, current, pick) {
    host.replaceChildren(...Object.entries(options).map(([k, label]) => h("button", { type: "button", "aria-pressed": String(k === current), text: label, onclick: () => pick(k) })));
  }

  function show(text, bad) { msg.textContent = text; msg.className = "cd-paper-msg " + (bad ? "error" : "ok"); msg.hidden = !text; }

  // -- the ticket ----------------------------------------------------------------------------------------------
  function render() {
    const v = venue(), canTrade = v === "india" || v === "crypto";
    off.hidden = canTrade; form.hidden = !canTrade; summary.hidden = !canTrade;
    off.textContent = canTrade ? "" : "Imported prices are not live, so there is nothing to trade. Open NSE / BSE or Crypto to trade from the chart.";
    const live = india() && deps.mode() === "live";
    modePill.textContent = !canTrade ? "" : india() ? (live ? "LIVE · real money" : "Paper · practice money") : "Paper · practice money (crypto is paper only)";
    modePill.className = "pill " + (live ? "bad" : "");
    seg(sideSeg, { BUY: "Buy", SELL: "Sell" }, st.side, (k) => { st.side = k; if (k === "SELL") { st.stop = st.target = null; stopIn.value = targetIn.value = ""; } render(); drawLines(); });
    seg(typeSeg, { MARKET: "Market", LIMIT: "Limit" }, st.type, (k) => {
      st.type = k;
      if (k === "LIMIT" && !priceIn.value && deps.lastPrice()) priceIn.value = String(round(/** @type {number} */ (deps.lastPrice())));
      render(); drawLines();
    });
    seg(productSeg, { MIS: "Intraday", CNC: "Delivery" }, st.product, (k) => { st.product = k; render(); });
    productSeg.hidden = !india();
    sideSeg.dataset.side = st.side;
    priceLabel.hidden = st.type !== "LIMIT";
    const buy = st.side === "BUY";
    stopLabel.hidden = targetLabel.hidden = levelsBtn.hidden = !buy;
    levelsBtn.textContent = st.stop || st.target ? "Remove lines" : "Add stoploss and target on chart";
    const name = india() ? deps.symbol().replace(/^(NSE|BSE):/, "") : deps.symbol().replace(/USDT$/, "");
    submit.textContent = `Review ${buy ? "buy" : "sell"}`;
    submit.className = "primary " + (buy ? "cd-buy" : "cd-sell");
    qtyIn.placeholder = india() ? "Shares" : `${name} amount`;
    qtyIn.step = india() ? "1" : "any";
    const q = num(qtyIn), p = entry();
    const parts = [];
    if (q && p) parts.push(`${buy ? "Buy" : "Sell"} ${q} ${name} ≈ ${fmt(q * p)}${unit()}`);
    if (q && p && st.stop) parts.push(`Stoploss ${fmt(st.stop)}: ${signedMoney(outcome(q, p, st.stop))}`);
    if (q && p && st.target) parts.push(`Target ${fmt(st.target)}: ${signedMoney(outcome(q, p, st.target))}`);
    if (q && p && st.stop && st.target && p > st.stop) parts.push(`Reward to risk ${((st.target - p) / (p - st.stop)).toFixed(2)}`);
    if (india() && !buy) parts.push("A sell from the chart sells shares you hold.");
    summary.textContent = parts.join(" · ") + (parts.length && buy && (st.stop || st.target) ? " (before charges)" : "");
  }

  function toggleLevels() {
    if (st.stop || st.target) { st.stop = st.target = null; stopIn.value = targetIn.value = ""; }
    else {
      const p = entry();
      if (!p) { show("No price yet to place the lines at.", true); return; }
      const d = defaultLevels(p);
      st.stop = round(d.stop); st.target = round(d.target);
      stopIn.value = String(st.stop); targetIn.value = String(st.target);
    }
    render(); drawLines();
  }

  for (const el of [qtyIn, priceIn, stopIn, targetIn]) el.addEventListener("input", () => {
    st.stop = num(stopIn); st.target = num(targetIn); st.price = num(priceIn); render(); drawLines();
  });

  // -- review and place ----------------------------------------------------------------------------------------
  function row(label, value, cls = "") { return [h("dt", { text: label }), h("dd", { class: cls, text: value })]; }

  /** @param {string} title @param {any[]} body @param {{ label: string, cls?: string, phrase?: string, run: () => Promise<void> }|null} action
   * @param {() => void} [onCancel] */
  function ask(title, body, action, onCancel) {
    const send = /** @type {HTMLButtonElement} */ (h("button", { type: "button", class: action ? action.cls || "primary" : "", text: action ? action.label : "" }));
    const typed = /** @type {HTMLInputElement} */ (h("input", { type: "text", autocomplete: "off", spellcheck: "false", "aria-label": "Confirmation phrase" }));
    const out = h("div", { class: "cd-confirm-out" });
    let done = false;
    const close = () => { if (dialog.open) dialog.close(); };
    dialog.onclose = () => { if (!done && onCancel) onCancel(); };
    const cancel = h("button", { type: "button", text: action ? "Cancel" : "Close", onclick: close });
    if (action && action.phrase) {
      send.disabled = true;
      typed.placeholder = action.phrase;
      typed.addEventListener("input", () => { send.disabled = typed.value.trim().toUpperCase().replace(/\s+/g, " ") !== action.phrase; });
    }
    send.addEventListener("click", async () => {
      send.disabled = true; cancel.setAttribute("disabled", "");
      try { await action?.run(); done = true; close(); }
      catch (err) {
        const reasons = /** @type {any} */ (err).payload && /** @type {any} */ (err).payload.errors;
        out.replaceChildren(h("div", { class: "error", text: "Not done: " + (reasons ? reasons.join(" ") : /** @type {Error} */ (err).message) }));
        cancel.removeAttribute("disabled");
      }
    });
    dialog.replaceChildren(h("div", { class: "cd-confirm-body" }, h("h3", { text: title }), ...body,
      action && action.phrase ? h("p", {}, "Type ", h("code", { text: action.phrase }), " to confirm.") : null,
      action && action.phrase ? typed : null, out,
      h("div", { class: "cd-confirm-buttons" }, cancel, action ? send : null)));
    if (typeof dialog.showModal === "function") dialog.showModal(); else dialog.setAttribute("open", "");
    (action && action.phrase ? typed : send).focus();
  }

  function notes(warnings, errors) {
    return [...(warnings || []).map((w) => h("div", { class: "warnbox", text: w })),
      errors && errors.length ? h("div", { class: "error", text: "This cannot be placed:\n• " + errors.join("\n• ") }) : null];
  }

  async function review() {
    const q = num(qtyIn);
    if (!q) { show("Enter a quantity.", true); return; }
    if (st.type === "LIMIT" && !num(priceIn)) { show("Enter a limit price, or choose Market.", true); return; }
    show("", false);
    if (india()) await reviewIndia(q); else reviewCrypto(q);
  }

  async function reviewIndia(q) {
    const body = { mode: deps.mode(), symbol: deps.symbol(), side: st.side, quantity: q, product: st.product, order_type: st.type,
      price: st.type === "LIMIT" ? num(priceIn) : 0, stop: st.side === "BUY" ? st.stop : null, target: st.side === "BUY" ? st.target : null };
    let r;
    submit.disabled = true;
    try { r = await api("/chart-orders/preview", { method: "POST", body }); }
    catch (err) { const reasons = err.payload && err.payload.errors; show(reasons ? reasons.join(" ") : err.message, true); submit.disabled = false; return; }
    submit.disabled = false;
    const o = r.order, live = r.mode === "live", pct = (v) => `${v}%`;
    const lines = [h("dl", { class: "kv" },
      row("Order", `${o.side === "BUY" ? "Buy" : "Sell"} ${o.quantity} ${o.tradingsymbol} (${o.exchange})`),
      row("Price", o.order_type === "MARKET" ? "Market (fills at the price available)" : `Limit ${fmt(o.price)}`),
      row("Product", o.product === "MIS" ? "Intraday (MIS)" : "Delivery (CNC)"),
      row("Last price", r.last_price ? `${fmt(r.last_price)} (${r.last_price_source})` : r.last_price_source),
      row("Order value", fmt(r.value)),
      r.margin ? row("Margin needed", `${fmt(r.margin.amount)}${r.margin.source === "Zerodha" ? " (Zerodha)" : " (estimate)"}`) : null,
      r.charges ? row("Charges (about)", fmt(r.charges.total)) : null,
      r.stop ? row("Stoploss", `${fmt(r.stop)}${r.risk ? `: lose about ${fmt(r.risk.loss)}, ${pct(r.risk.of_capital_pct)} of capital${r.risk.limit_pct ? ` (limit ${pct(r.risk.limit_pct)})` : ""}` : ""}`, "down") : null,
      r.target ? row("Target", `${fmt(r.target)}${r.reward != null ? `: gain about ${fmt(r.reward)} after charges` : ""}`, "up") : null),
    r.protection ? h("p", { class: "hint", text: r.protection }) : null,
    r.margin && r.margin.note ? h("p", { class: "hint", text: r.margin.note }) : null,
    ...notes(r.warnings, r.ok ? [] : r.errors)];
    ask(`${live ? "LIVE order (real money)" : "Paper order"}: ${o.side === "BUY" ? "Buy" : "Sell"} ${o.quantity} ${o.tradingsymbol}`, lines,
      r.ok ? { label: live ? "Place LIVE order" : "Place paper order", cls: live ? "danger" : "primary", phrase: live ? r.phrase : undefined,
        run: async () => {
          const placed = await api("/chart-orders/place", { method: "POST", body: { token: r.token, phrase: r.phrase } });
          const p = placed.placed;
          show(live ? `Sent to Kite: order ${p.order_id || "(no id)"}. Sent is not filled; ${placed.chart_order ? "the stoploss and target follow the fill." : "check Today's orders."}`
            : `Paper order filled at ${fmt(p.fill_price)}.${placed.chart_order ? " " + (placed.chart_order.state === "protected" ? "Stoploss and target are placed." : (placed.chart_order.log || []).slice(-1)[0] || "") : ""}`,
          !!(placed.chart_order && placed.chart_order.state === "unprotected"));
          clearDraft(); refresh();
        } } : null);
  }

  function reviewCrypto(q) {
    const p = /** @type {number} */ (entry()), fee = (p * q) * ((st.book && st.book.fee_pct) || 0.1) / 100;
    const coin = deps.symbol().replace(/USDT$/, "");
    const lines = [h("dl", { class: "kv" },
      row("Order", `${st.side === "BUY" ? "Buy" : "Sell"} ${q} ${coin}`),
      row("Price", st.type === "MARKET" ? `Market, about ${fmt(p)} USDT` : `Limit ${fmt(p)} USDT`),
      row("Order value", `${fmt(q * p)} USDT`), row("Fee (about)", `${fmt(fee)} USDT`),
      st.stop ? row("Stoploss", `${fmt(st.stop)}: ${signedMoney(outcome(q, p, st.stop))}`, "down") : null,
      st.target ? row("Target", `${fmt(st.target)}: ${signedMoney(outcome(q, p, st.target))}`, "up") : null),
    st.stop || st.target ? h("p", { class: "hint", text: "Once the buy fills, the stoploss sells at the last price when the price falls to it and the target sells when the price reaches it. Whichever goes first cancels the other. Checked every few seconds on the server." }) : null];
    ask(`Paper order: ${st.side === "BUY" ? "Buy" : "Sell"} ${q} ${coin}`, lines, { label: "Place paper order", run: async () => {
      const body = { symbol: deps.symbol(), side: st.side, type: st.type, quantity: q, price: st.type === "LIMIT" ? p : undefined,
        stop: st.side === "BUY" ? st.stop : undefined, target: st.side === "BUY" ? st.target : undefined };
      const r = await api("/cpaper/order", { method: "POST", body });
      show(r.filled ? `${r.trade.side === "BUY" ? "Bought" : "Sold"} ${r.trade.quantity} ${coin} at ${fmt(r.trade.price)} USDT.${r.bracket ? " Stoploss and target are set." : ""}`
        : `Limit order placed at ${fmt(r.order.price)} USDT. It fills when the price gets there${r.order.stop || r.order.target ? ", then its stoploss and target start" : ""}.`, false);
      clearDraft(); bookChanged(r.book); deps.onBook(r.book);
    } });
  }

  function clearDraft() { st.stop = st.target = null; stopIn.value = targetIn.value = ""; render(); drawLines(); }

  function fillPrice() {
    const p = deps.lastPrice();
    if (st.type === "LIMIT" && !priceIn.value && p) { priceIn.value = String(round(p)); render(); }
  }

  // -- placed orders: the list and its lines ----------------------------------------------------------------------
  /** The open stoploss/target sets for this symbol, the same shape for both venues. */
  function placed() {
    if (india()) {
      return st.items.filter((i) => ["waiting", "protected", "unprotected"].includes(i.state)).map((i) => ({
        id: i.id, quantity: i.quantity, entry: i.entry_price, stop: i.stop, target: i.target, mode: i.mode,
        label: `${i.mode === "live" ? "LIVE" : "Paper"} ${i.product === "MIS" ? "intraday" : "delivery"} buy of ${i.quantity}`,
        state: { waiting: "Waiting for the buy to fill", protected: "Stoploss and target placed", unprotected: "NOT protected" }[i.state],
        bad: i.state === "unprotected", note: (i.log || []).slice(-1)[0] || "" }));
    }
    const sym = deps.symbol();
    return ((st.book && st.book.brackets) || []).filter((b) => b.symbol === sym).map((b) => ({
      id: b.id, quantity: b.quantity, entry: b.entry, stop: b.stop, target: b.target, mode: "paper",
      label: `Paper ${b.quantity} ${sym.replace(/USDT$/, "")}`, state: "Stoploss and target set", bad: false, note: "" }));
  }

  function renderList() {
    const rows = placed();
    const done = india() ? st.items.filter((i) => !["waiting", "protected", "unprotected"].includes(i.state)).slice(0, 3) : [];
    list.replaceChildren(
      ...(rows.length ? [h("div", { class: "lbl", text: "Your stoploss and target orders" })] : []),
      ...rows.map((r) => h("div", { class: "cd-t-row" + (r.bad ? " bad" : "") },
        h("span", {}, h("strong", { text: r.label }), ` · ${r.stop ? "Stoploss " + fmt(r.stop) : "no stoploss"} · ${r.target ? "Target " + fmt(r.target) : "no target"} · `,
          h("span", { class: r.bad ? "down" : "hint", text: r.state })),
        r.note ? h("span", { class: "hint cd-t-note", text: r.note }) : null,
        h("button", { type: "button", class: "cd-cancel", text: "Remove", onclick: () => removePlaced(r) }))),
      ...done.map((i) => h("div", { class: "cd-t-row done" }, h("span", { class: "hint", text: `${i.mode === "live" ? "LIVE" : "Paper"} buy of ${i.quantity}: ${i.closed || i.state}` }))));
  }

  function removePlaced(r) {
    ask(`Remove the stoploss and target?`, [h("p", { text: india() && r.state.startsWith("Waiting")
      ? "The buy has not filled: this cancels the buy order too."
      : `The ${r.quantity} you hold stay; nothing will sell them at the stoploss or target any more.` })],
    { label: r.mode === "live" ? "Remove from Kite" : "Remove", cls: "danger", run: async () => {
      if (india()) await api(`/chart-orders/${r.id}`, { method: "DELETE" });
      else { const book = await api(`/cpaper/bracket/${r.id}`, { method: "DELETE" }); bookChanged(book); deps.onBook(book); }
      show("Stoploss and target removed.", false); refresh();
    } });
  }

  /** A placed order's line was dragged to a new price: confirm, then change it, or put the line back. */
  function moved(r, key, price) {
    const after = { stop: r.stop, target: r.target, [key]: price };
    const run = async () => {
      const out = await api(`/cpaper/bracket/${r.id}`, { method: "PUT", body: after });
      bookChanged(out.book); deps.onBook(out.book);
      show(`${key === "stop" ? "Stoploss" : "Target"} moved to ${fmt(price)}.`, false); refresh();
    };
    const name = key === "stop" ? "stoploss" : "target";
    const was = r[key];
    const body = [h("dl", { class: "kv" }, row("From", was ? fmt(was) : "none"), row("To", fmt(price), key === "stop" ? "down" : "up"),
      r.entry ? row("If it hits", signedMoney(outcome(r.quantity, r.entry, price)) + " before charges", key === "stop" ? "down" : "up") : null),
    r.mode === "live" ? h("p", { class: "warnbox", text: "This changes your LIVE order at Zerodha." }) : null];
    if (india()) {
      // Ask the desk first so a refused change shows its reasons before anything is confirmed.
      api(`/chart-orders/${r.id}/change/preview`, { method: "POST", body: after }).then((p) => {
        ask(`Move the ${name} of ${r.label}?`, [...body, ...notes(p.warnings, p.ok ? [] : p.errors)],
          p.ok ? { label: r.mode === "live" ? "Change LIVE order" : "Change", cls: r.mode === "live" ? "danger" : "primary", run: async () => {
            await api(`/chart-orders/${r.id}/change`, { method: "POST", body: { token: p.token } });
            show(`${key === "stop" ? "Stoploss" : "Target"} moved to ${fmt(price)}.`, false); refresh();
          } } : null, () => drawLines());
      }).catch((err) => { show(err.message, true); drawLines(); });
      return;
    }
    ask(`Move the ${name}?`, body, { label: "Change", run }, () => drawLines());
  }

  // -- lines on the chart ------------------------------------------------------------------------------------------
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function clearLines() {
    const c = deps.chart();
    st.lines.forEach((l) => { try { c && c.price.removePriceLine(l.line); } catch (err) { /* chart redrawn */ } });
    st.lines = [];
  }

  function title(kind, price, quantity, from, tag) {
    const base = { stop: "Stoploss", target: "Target", price: "Limit price" }[kind] + (tag ? ` ${tag}` : "");
    return kind !== "price" && quantity && from ? `${base} ${signedMoney(outcome(quantity, from, price))}` : base;
  }

  /** Keep every line inside the price scale: the chart's autoscale looks at the candles only, so a stoploss or
   * target beyond the visible range would be drawn off the chart. The levels are not changed while a line is
   * being dragged, so the scale holds still under the pointer. */
  function keepInView(c) {
    if (st.scaled !== c.price) {
      st.scaled = c.price;
      c.price.applyOptions({ autoscaleInfoProvider: (/** @type {() => any} */ original) => {
        const r = original();
        if (!r || !r.priceRange || !st.levels.length) return r;
        const pad = (r.priceRange.maxValue - r.priceRange.minValue) * 0.02;
        return { ...r, priceRange: { minValue: Math.min(r.priceRange.minValue, Math.min(...st.levels) - pad),
          maxValue: Math.max(r.priceRange.maxValue, Math.max(...st.levels) + pad) } };
      } });
    }
  }

  function drawLines() {
    clearLines();
    const c = deps.chart();
    if (!c || !st.visible) { st.levels = []; return; }
    keepInView(c);
    const add = (kind, price, owner, tag, quantity, from) => {
      const color = kind === "stop" ? css("--bad") : kind === "target" ? css("--good") : css("--accent");
      const line = c.price.createPriceLine({ price, color, lineWidth: 2, lineStyle: owner ? 0 : 2,   // solid for a placed order, dashed while still a plan
        axisLabelVisible: true, title: title(kind, price, quantity, from, tag) });
      st.lines.push({ line, kind, owner, price, tag, quantity, from });
    };
    const q = num(qtyIn) || 0, e = entry() || 0;
    if (st.side === "BUY") {
      if (st.stop) add("stop", st.stop, null, "", q, e);
      if (st.target) add("target", st.target, null, "", q, e);
    }
    if (st.type === "LIMIT" && num(priceIn) && (st.stop || st.target)) add("price", /** @type {number} */ (num(priceIn)), null, "", 0, 0);
    placed().forEach((r) => {
      const tag = r.mode === "live" ? "(live)" : "(paper)";
      if (r.stop) add("stop", r.stop, r, tag, r.quantity, r.entry);
      if (r.target) add("target", r.target, r, tag, r.quantity, r.entry);
    });
    st.levels = st.lines.map((l) => l.price);
  }

  function hit(y) {
    const c = deps.chart();
    if (!c) return -1;
    return nearest(st.lines.map((l) => ({ y: c.price.priceToCoordinate(l.price) })), y);
  }

  let bound = /** @type {HTMLElement|null} */ (null);
  function bind() {
    const c = deps.chart();
    if (!c || bound === c.box) return;
    bound = c.box;
    const box = c.box;
    const yOf = (/** @type {PointerEvent} */ e) => e.clientY - box.getBoundingClientRect().top;
    box.addEventListener("pointerdown", (e) => {
      if (!st.visible || !st.lines.length) return;
      const i = hit(yOf(e));
      if (i < 0) return;
      const chart = deps.chart();
      if (!chart) return;
      e.stopPropagation(); e.preventDefault();
      st.drag = { i, id: e.pointerId };
      chart.main.applyOptions({ handleScroll: false, handleScale: false });
      try { box.setPointerCapture(e.pointerId); } catch (err) { /* old browsers */ }
    }, true);
    // The chart library listens for mouse and touch events: keep them from panning the chart while a line moves.
    for (const type of ["mousedown", "touchstart"]) box.addEventListener(type, (e) => { if (st.drag) { e.stopPropagation(); if (e.cancelable) e.preventDefault(); } }, { capture: true, passive: false });
    box.addEventListener("pointermove", (e) => {
      const chart = deps.chart();
      if (!chart) return;
      if (!st.drag) { box.style.cursor = st.visible && hit(yOf(e)) >= 0 ? "ns-resize" : ""; return; }
      e.stopPropagation();
      const l = st.lines[st.drag.i];
      const p = chart.price.coordinateToPrice(yOf(e));
      if (!l || p == null || !(p > 0)) return;
      l.price = round(p);
      l.line.applyOptions({ price: l.price, title: title(l.kind, l.price, l.owner ? l.quantity : num(qtyIn) || 0, l.owner ? l.from : entry() || 0, l.tag) });
      if (!l.owner) {
        if (l.kind === "stop") { st.stop = l.price; stopIn.value = String(l.price); }
        else if (l.kind === "target") { st.target = l.price; targetIn.value = String(l.price); }
        else { priceIn.value = String(l.price); }
        render();
      }
    }, true);
    const end = (/** @type {PointerEvent} */ e) => {
      if (!st.drag) return;
      e.stopPropagation();
      const l = st.lines[st.drag.i];
      st.drag = null;
      const chart = deps.chart();
      if (chart) chart.main.applyOptions({ handleScroll: true, handleScale: true });
      if (!l) return;
      if (l.owner) {
        const was = l.owner[l.kind];
        if (was != null && Math.abs(was - l.price) < 1e-12) return;
        moved(l.owner, l.kind, l.price);
      } else drawLines();
    };
    box.addEventListener("pointerup", end, true);
    box.addEventListener("pointercancel", (e) => { if (st.drag) { st.drag = null; const chart = deps.chart(); if (chart) chart.main.applyOptions({ handleScroll: true, handleScale: true }); drawLines(); } e.stopPropagation(); }, true);
  }

  // -- data ---------------------------------------------------------------------------------------------------
  async function refresh() {
    if (india()) {
      try { const r = await api("/chart-orders?" + new URLSearchParams({ symbol: deps.symbol() })); st.items = r.items; }
      catch (err) { st.items = []; }
    }
    renderList(); if (!st.drag && !dialog.open) drawLines();
  }

  function bookChanged(book) { st.book = book; if (!india()) { renderList(); if (!st.drag && !dialog.open) drawLines(); } }

  render();
  return {
    show() {
      st.visible = true; root.hidden = false; render(); refresh();
      if (!st.timer) st.timer = setInterval(() => { if (india()) refresh(); }, POLL_MS);
    },
    hide() { st.visible = false; root.hidden = true; clearLines(); if (st.timer) { clearInterval(st.timer); st.timer = null; } },
    symbolChanged() { st.items = []; priceIn.value = ""; qtyIn.value = ""; clearDraft(); show("", false); fillPrice(); refresh(); },
    /** The chart was drawn again (new symbol, candle size, theme): its lines went with the old series. */
    chartDrawn() { st.lines = []; bind(); fillPrice(); drawLines(); },
    priceChanged() { if (st.type === "MARKET") render(); },
    modeChanged() { render(); },
    /** Whether a press at this height on the price chart is on one of the order lines (they take the press). @param {number} y */
    claims(y) { return st.visible && st.lines.length > 0 && hit(y) >= 0; },
    /** Fill the ticket from a Long position drawing: entry as the limit price, its stoploss, target and quantity.
     * @param {{ side: string, entry: number, stoploss: number, target: number, quantity: number }} plan */
    fill(plan) {
      if (plan.side !== "BUY") { show("A short position cannot be placed from the chart: Sell only sells what you hold.", true); root.scrollIntoView({ block: "nearest" }); return; }
      st.side = "BUY"; st.type = "LIMIT";
      priceIn.value = String(round(plan.entry));
      qtyIn.value = String(india() ? Math.max(1, Math.floor(plan.quantity)) : plan.quantity);
      st.stop = round(plan.stoploss); st.target = round(plan.target);
      stopIn.value = String(st.stop); targetIn.value = String(st.target);
      show("Filled from the position drawing. Check it, then press Review buy.", false);
      render(); drawLines(); root.scrollIntoView({ block: "nearest", behavior: "smooth" });
    },
    bookChanged,
  };
}
