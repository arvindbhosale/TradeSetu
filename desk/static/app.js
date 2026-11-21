"use strict";
(() => {
  const TOKEN = document.querySelector('meta[name="desk-token"]').content;
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
  const inr = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });
  const inr2 = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const rupees = (v, dp = 0) => (v == null ? "–" : "₹" + (dp ? inr2 : inr).format(v));
  const pct = (v, sign = true) => (v == null ? "–" : (sign && v > 0 ? "+" : "") + v.toFixed(2) + "%");
  const state = { app: null, mode: "paper", pending: null };

  // -- helpers ---------------------------------------------------------------

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

  async function api(path, { method = "GET", body } = {}) {
    let response;
    try {
      response = await fetch("/api" + path, {
        method,
        headers: { "X-Desk-Token": TOKEN, ...(body ? { "Content-Type": "application/json" } : {}) },
        body: body ? JSON.stringify(body) : undefined,
      });
    } catch (err) {
      throw new Error("The desk's local service is not answering. Restart the app.");
    }
    let payload = null;
    try { payload = await response.json(); } catch (err) { /* empty body */ }
    if (!response.ok) {
      const detail = payload && (payload.error || payload.detail);
      const error = new Error(typeof detail === "string" ? detail : `Request failed (${response.status})`);
      error.payload = payload || {};
      if (response.status === 401 && error.payload.signed_out) { window.location.replace("/login"); }
      throw error;
    }
    return payload;
  }

  // A file from the API. A plain link cannot send the request token the API needs, so the file is fetched with
  // it and handed to the browser from memory.
  async function download(path, fallbackName) {
    let response;
    try { response = await fetch("/api" + path, { headers: { "X-Desk-Token": TOKEN } }); }
    catch (err) { throw new Error("The desk's local service is not answering. Restart the app."); }
    if (!response.ok) {
      let payload = {};
      try { payload = await response.json(); } catch (err) { /* not JSON */ }
      if (response.status === 401 && payload.signed_out) window.location.replace("/login");
      throw new Error(payload.error || `Download failed (${response.status})`);
    }
    const match = /filename="([^"]+)"/.exec(response.headers.get("Content-Disposition") || "");
    const url = URL.createObjectURL(await response.blob());
    const link = h("a", { href: url, download: match ? match[1] : fallbackName, hidden: true });
    document.body.append(link); link.click(); link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  function showError(id, err) {
    const box = $(id);
    box.textContent = err ? (err.message || String(err)) : "";
    box.hidden = !err;
  }

  function busy(button, on, label) {
    if (on) { button.dataset.label = button.textContent; button.textContent = label; button.disabled = true; }
    else { button.textContent = button.dataset.label || button.textContent; button.disabled = false; }
  }

  function table(columns, rows, empty) {
    if (!rows.length) return h("p", { class: "hint", text: empty });
    return h("table", {},
      h("thead", {}, h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : null, text: c.label })))),
      h("tbody", {}, rows.map((row) => h("tr", {}, columns.map((c) => {
        const value = c.get(row);
        const cell = h("td", { class: [c.num ? "num" : "", c.tone ? c.tone(row) : ""].join(" ").trim() || null });
        if (value instanceof Node) cell.append(value); else cell.textContent = value == null ? "–" : value;
        return cell;
      })))));
  }

  const tone = (v) => (v > 0 ? "pos" : v < 0 ? "neg" : "");
  // Where a fill came from, in words: "algo:Name" is a stock algo, "fno:Name" an F&O algo.
  const sourceLabel = (source) => (source.startsWith("algo:") ? "Algo: " + source.slice(5) : source.startsWith("fno:") ? "F&O algo: " + source.slice(4)
    : source === "manual" ? "Manual" : source.charAt(0).toUpperCase() + source.slice(1));

  // A small, safe Markdown renderer: text is only ever set as text nodes.
  function markdown(source) {
    const root = h("div", { class: "md" });
    const inline = (text, parent) => {
      text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g).forEach((part) => {
        if (part.startsWith("**") && part.endsWith("**") && part.length > 4) parent.append(h("strong", { text: part.slice(2, -2) }));
        else if (part.startsWith("`") && part.endsWith("`") && part.length > 2) parent.append(h("code", { text: part.slice(1, -1) }));
        else if (part) parent.append(part);
      });
      return parent;
    };
    const lines = String(source || "").replace(/\r/g, "").split("\n");
    let list = null, tableRows = null;
    const flushTable = () => {
      if (!tableRows) return;
      const rows = tableRows.filter((r) => !r.every((c) => /^:?-{2,}:?$/.test(c)));
      if (rows.length) {
        root.append(h("div", { class: "scroll" }, h("table", {},
          h("thead", {}, h("tr", {}, rows[0].map((c) => inline(c, h("th"))))),
          h("tbody", {}, rows.slice(1).map((r) => h("tr", {}, r.map((c) => inline(c, h("td")))))))));
      }
      tableRows = null;
    };
    for (const raw of lines) {
      const line = raw.trimEnd();
      if (/^\s*\|.*\|\s*$/.test(line)) {
        list = null;
        (tableRows = tableRows || []).push(line.trim().slice(1, -1).split("|").map((c) => c.trim()));
        continue;
      }
      flushTable();
      const heading = line.match(/^#{1,6}\s+(.*)$/);
      const bullet = line.match(/^\s*(?:[-*+]|\d+[.)])\s+(.*)$/);
      if (heading) { list = null; root.append(inline(heading[1], h("h4"))); }
      else if (bullet) { if (!list) { list = h("ul"); root.append(list); } list.append(inline(bullet[1], h("li"))); }
      else if (!line.trim()) { list = null; }
      else { list = null; root.append(inline(line, h("p"))); }
    }
    flushTable();
    return root;
  }

  // -- shell -----------------------------------------------------------------

  function selectTab(name) {
    $$(".tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.tab === name)));
    $$("section.tab").forEach((s) => { s.hidden = s.id !== "tab-" + name; });
    const active = $(`.tabs button[data-tab="${name}"] span`);
    if (active) $("#page-title").textContent = active.textContent;
    if (name === "markets") { refreshSectors(); refreshPulse(); }
    if (name === "tools") showTool(toolState.tool);
    if (name === "mcx") { loadMcx(true); refreshMcxPositions(); }
    if (name === "fno") { refreshFno(); if (!oc.underlyings.length) loadUnderlyings(); refreshOptionPositions(); loadFutures(true); }
    if (name === "pnl") refreshPnl();
    if (name === "indices") refreshIndices();
    try { history.replaceState(null, "", "#" + name); } catch (err) { /* not allowed in this frame */ }
    if (name === "trade") refreshTrade();
    if (name === "algos") { refreshAlgos(); refreshFnoAlgos(); }
    if (name === "strategies") refreshStrategyList();
    if (name === "scanner") refreshScanner();
  }
  $$(".tabs button").forEach((b) => b.addEventListener("click", () => selectTab(b.dataset.tab)));

  function setMode(mode) {
    state.mode = mode;
    $$("#mode button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.mode === mode)));
    const pill = $("#ticket-mode");
    pill.textContent = mode === "live" ? "LIVE · real money" : "Paper";
    pill.className = "pill " + (mode === "live" ? "bad" : "");
    clearReview();
    renderBanner();
  }
  $$("#mode button").forEach((b) => b.addEventListener("click", () => setMode(b.dataset.mode)));

  function renderBanner() {
    const banner = $("#banner");
    const s = state.app.settings;
    const parts = [];
    if (s.kill_switch) parts.push("Kill switch is on: no orders can be placed.");
    if (state.mode === "live") {
      if (!s.live_enabled) parts.push("Live mode selected, but live orders are switched off in Settings.");
      else if (!state.app.kite.connected) parts.push("Live mode: connect Kite on the Trade tab.");
      else if (!state.app.kite.can_trade) parts.push("Live mode: the connected Kite server is read-only and cannot place orders.");
      else parts.push("LIVE mode: orders go to the exchange with real money.");
    }
    banner.textContent = parts.join(" ");
    banner.hidden = !parts.length;
  }

  function renderPills() {
    const session = state.app.session, kite = state.app.kite;
    const sp = $("#pill-session");
    sp.textContent = session.open ? "Market open" : session.holiday ? "Holiday" : "Market closed";
    sp.className = "pill " + (session.open ? "good" : "");
    sp.title = `${session.time_ist} IST. ${session.note}`;
    const kp = $("#pill-kite");
    kp.textContent = !kite.connected ? "Kite: not connected" : kite.can_trade ? "Kite: trading" : "Kite: read-only";
    kp.className = "pill " + (!kite.connected ? "" : kite.can_trade ? "good" : "warn");
    const w = state.app.window, wp = $("#pill-window"), enforced = state.app.settings.enforce_trade_window;
    wp.hidden = !enforced;
    wp.textContent = w.entries ? `Buying open until ${w.trade_end}` : `Buying ${w.trade_start}–${w.trade_end}`;
    wp.className = "pill " + (w.entries ? "good" : "");
    wp.title = `Your trade window: new buys ${w.trade_start} to ${w.trade_end} IST, intraday square-off at ${w.square_off_at}. Change it in Settings.`;
  }

  async function loadState() {
    state.app = await api("/state");
    renderPills();
    renderBanner();
    return state.app;
  }

  // -- jobs --------------------------------------------------------------------

  function watchJob(jobId, ui, onDone) {
    const tick = async () => {
      let job;
      try { job = await api("/jobs/" + jobId); }
      catch (err) { ui.status.textContent = "lost"; ui.status.className = "pill bad"; ui.log.textContent += "\n" + err.message; return; }
      ui.title.textContent = job.title;
      const calls = job.counters.llm_calls ? ` · ${job.counters.llm_calls} LLM calls` : "";
      ui.status.textContent = `${job.status} · ${Math.round(job.elapsed)}s${calls}`;
      ui.status.className = "pill " + (job.status === "done" ? "good" : job.status === "error" ? "bad" : "warn");
      const atEnd = ui.log.scrollTop + ui.log.clientHeight >= ui.log.scrollHeight - 8;
      ui.log.textContent = job.events.map((e) => `${String(e.t).padStart(6)}s  ${e.message}`).join("\n")
        + (job.error ? `\n\nFailed: ${job.error}` : "");
      if (atEnd) ui.log.scrollTop = ui.log.scrollHeight;
      if (job.status === "done" || job.status === "error") onDone(job);
      else setTimeout(tick, 1500);
    };
    tick();
  }

  // -- analyze -----------------------------------------------------------------

  $("#form-analyze").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target, button = $("button[type=submit]", form);
    showError("#analyze-error", null);
    $("#analyze-result").hidden = true;
    busy(button, true, "Starting…");
    try {
      const { job } = await api("/analyze", { method: "POST", body: {
        symbol: form.symbol.value, date: form.date.value, use_portfolio: form.use_portfolio.checked } });
      $("#analyze-job").hidden = false;
      $("#analyze-log").textContent = "";
      watchJob(job.id, { title: $("#analyze-title"), status: $("#analyze-status"), log: $("#analyze-log") }, (done) => {
        busy(button, false);
        if (done.status === "done") renderAnalysis(done.result);
      });
    } catch (err) { showError("#analyze-error", err); busy(button, false); }
  });

  async function renderAnalysis(result) {
    $("#analyze-result").hidden = false;
    const rating = $("#analyze-rating");
    rating.textContent = `${result.rating} · ${result.symbol}`;
    rating.className = "rating " + (/buy|overweight/i.test(result.rating) ? "buy" : /sell|underweight/i.test(result.rating) ? "sell" : "");
    const reports = $("#analyze-reports");
    reports.replaceChildren(...result.reports.slice().reverse().map((r, i) =>
      h("details", { open: i === 0 }, h("summary", { text: r.title }), markdown(r.text))));
    const plan = $("#analyze-plan");
    plan.replaceChildren(h("p", { class: "hint", text: "Working out a suggested order…" }));
    try {
      const suggestion = await api("/order/plan", { method: "POST", body: { symbol: result.symbol, rating: result.rating, mode: state.mode } });
      plan.replaceChildren(h("p", {}, h("strong", { text: "Suggested order: " }),
        suggestion.order ? `${suggestion.order.side} ${suggestion.order.quantity} ${suggestion.order.tradingsymbol} at ₹${inr2.format(suggestion.order.price)} (${state.mode}). ` : "none. ",
        suggestion.reason));
      if (suggestion.order) {
        plan.append(h("button", { class: "primary", text: "Open in order ticket", onclick: () => { fillTicket(suggestion.order); selectTab("trade"); } }));
        plan.append(h("p", { class: "hint", text: "Nothing is sent until you review and confirm it on the Trade tab. This is the model's opinion, not investment advice." }));
      }
    } catch (err) { plan.replaceChildren(h("p", { class: "hint", text: "No order suggestion: " + err.message })); }
  }

  // -- strategy backtest ---------------------------------------------------------

  // The strategy choices, grouped, for a <select>.
  function strategyBlurb(strategy) {
    const mark = (m) => h("span", { class: "regime " + (strategy.markets.includes(m) ? "yes" : "no"), text: `${strategy.markets.includes(m) ? "✓" : "✗"} ${m[0].toUpperCase() + m.slice(1)}` });
    return h("span", {}, strategy.description + (strategy.source && !strategy.custom ? ` (after ${strategy.source})` : ""),
      strategy.custom ? "" : h("span", { class: "regimes" }, h("b", { text: "Markets: " }), mark("bull"), mark("sideways"), mark("bear"),
        strategy.note ? h("span", { class: "hint", text: " " + strategy.note }) : ""));
  }
  function strategyOptions(select, keep = () => true) {
    const current = select.value, list = state.app.strategies.filter(keep);
    const group = (label, items) => (items.length ? h("optgroup", { label }, items.map((s) => h("option", { value: s.key, text: s.name }))) : null);
    const styles = [["Legend", "Market legends"], ["Trend", "Trend following"], ["Breakout", "Breakout"], ["Swing", "Swing trading"],
      ["Mean reversion", "Mean reversion (sideways markets)"], ["Scalping", "Scalping & intraday (1-15 min candles)"], ["Baseline", "Baseline"], ["Other", "Other"]];
    select.replaceChildren(...[group("My strategies", list.filter((s) => s.custom)),
      ...styles.map(([key, label]) => group(label, list.filter((s) => !s.custom && (s.style || "Other") === key)))].filter(Boolean));
    if (list.some((s) => s.key === current)) select.value = current;
  }
  function intervalOptions(select) {
    select.replaceChildren(...Object.entries(state.app.intervals).reverse().map(([key, label]) => h("option", { value: key, text: label })));
    select.value = "1d";
  }

  const OWN_RULES = "__rules__";        // the stock backtest's "My own rules" choice: rules typed on the screen

  function btStrategyOptions() {
    const select = $("#form-bt").strategy, current = select.value;
    strategyOptions(select);
    select.prepend(h("optgroup", { label: "Build your own" }, h("option", { value: OWN_RULES, text: "My own rules: entry when / exit when (below)" })));
    if (current === OWN_RULES) select.value = OWN_RULES;
  }

  function renderStrategyParams() {
    const key = $("#form-bt").strategy.value;
    $("#bt-rules").hidden = key !== OWN_RULES;
    if (key === OWN_RULES) {
      $("#bt-desc").textContent = "Buys when the entry rules pass and sells when the exit rules pass. Use Time of day and Day of week for time conditions (intraday candles).";
      $("#bt-params").replaceChildren();
      return;
    }
    const strategy = state.app.strategies.find((s) => s.key === key);
    $("#bt-desc").replaceChildren(strategyBlurb(strategy));
    $("#bt-params").replaceChildren(...strategy.params.map((p) =>
      h("label", {}, p.label, h("input", { name: "p_" + p.name, type: "number", min: p.minimum, max: p.maximum,
        step: p.integer ? 1 : 0.1, value: p.default, required: true }))));
  }

  // -- one Backtest screen: what to test, then one set of dates and capital ----------------

  const BT_PANES = { strategy: "#bt-strategy", structures: "#bt-structures", options: "#bt-options", futures: "#bt-futures",
    optimise: "#bt-optimise", portfolio: "#bt-portfolio", agents: "#bt-agents" };
  const BT_SHARED = ["#form-bt", "#form-sbt", "#form-obt", "#form-fbt"];     // these take From, To and Capital from the top

  function syncSharedDates() {
    const from = $("#bt-from").value, to = $("#bt-to").value, capital = $("#bt-capital").value;
    BT_SHARED.forEach((id) => { const f = $(id); f.start.value = from; f.end.value = to; if (capital) f.capital.value = capital; });
  }

  function showBacktestPane(kind) {
    Object.entries(BT_PANES).forEach(([key, id]) => { $(id).hidden = key !== kind; });
    $$(".bt-shared").forEach((el) => { el.hidden = !BT_SHARED.includes({ strategy: "#form-bt", structures: "#form-sbt", options: "#form-obt", futures: "#form-fbt" }[kind]); });
    const result = $("#bt-result");
    if ($(BT_PANES[kind]).contains(result) === false) result.hidden = true;
  }

  $("#bt-what").addEventListener("change", (event) => showBacktestPane(event.target.value));
  ["#bt-from", "#bt-to", "#bt-capital"].forEach((id) => $(id).addEventListener("input", syncSharedDates));

  // -- exit rules: stoploss and target in % or points, trailing in steps, stoploss to cost, re-entry -------

  function exitRulesBox(leg = false) {
    const what = leg ? "premium" : "price";
    const field = (key, label, extra = {}) => h("label", {}, label, h("input", { "data-x": key, type: "number", min: 0, step: "any", value: 0, ...extra }));
    const unit = h("select", { "data-x": "unit" }, h("option", { value: "pct", text: leg ? "% of the premium" : "% of the price" }),
      h("option", { value: "points", text: leg ? "points of premium" : "points (₹ a share)" }));
    const reentry = h("select", { "data-x": "reentry" }, [0, 1, 2, 3, 4, 5].map((n) => h("option", { value: n, text: n ? `Up to ${n} time${n > 1 ? "s" : ""}` : "No" })));
    const mode = h("select", { "data-x": "reentry_mode" }, h("option", { value: "back_to_entry", text: `When the ${what} comes back to the entry ${what}` }),
      h("option", { value: "at_once", text: leg ? "Straight away, at the strike the entry rule picks then" : "Straight away, on the next candle" }));
    const modeLabel = h("label", {}, "Re-enter", mode);
    const box = h("div", { class: "exits" },
      h("div", { class: "row" }, h("label", {}, "Measure in", unit), field("stop_loss", "Stoploss"), field("target", "Target")),
      h("div", { class: "row" }, field("trail_every", `Trailing stoploss: each time the ${what} moves this much in your favour`),
        field("trail_by", "move the stoploss this much"), field("cost_after", "Move stoploss to the entry price once the profit is")),
      h("div", { class: "row" }, h("label", {}, "Re-enter after a stoploss", reentry), modeLabel),
      h("p", { class: "hint", text: `0 means off. Example: buy at 100 with a stoploss of 10 points (90). A trailing stoploss of 10 and 5 moves it to 95 when the ${what} reaches 110, and to 100 at 120. It never moves back.`
        + (leg ? " For a sold leg it works the other way: the stoploss is above the premium you sold at, and a falling premium is your profit." : "")
        + " Re-entry happens only after a plain stoploss, not after a trailing stoploss or one moved to cost." }));
    const sync = () => { modeLabel.hidden = reentry.value === "0"; };
    reentry.addEventListener("change", sync); sync();
    box.read = () => {
      const out = {};
      $$("[data-x]", box).forEach((el) => { out[el.dataset.x] = el.tagName === "SELECT" && el.dataset.x !== "reentry" ? el.value : Number(el.value || 0); });
      return out;
    };
    box.load = (x) => { if (!x) return; $$("[data-x]", box).forEach((el) => { if (x[el.dataset.x] != null) el.value = x[el.dataset.x]; }); sync(); };
    return box;
  }

  // -- the stock backtest ------------------------------------------------------------------

  function syncStockForm() {
    const form = $("#form-bt"), intraday = form.interval.value !== "1d";
    $$(".bt-intraday", form).forEach((el) => { el.hidden = !intraday; });
  }

  function stockBody(form) {
    const params = {};
    $$("#bt-params input").forEach((input) => { params[input.name.slice(2)] = Number(input.value); });
    const own = form.strategy.value === OWN_RULES;
    const body = { symbol: form.symbol.value, start: form.start.value, end: form.end.value, interval: form.interval.value,
      capital: Number(form.capital.value), allocation_pct: Number(form.allocation_pct.value), exits: $("#bt-exits").read() };
    if (own) body.rules = $("#bt-rules").read(); else { body.strategy = form.strategy.value; body.strategy_params = params; }
    if (form.interval.value !== "1d") Object.assign(body, { trade_start: form.trade_start.value, trade_end: form.trade_end.value, square_off: form.square_off.value });
    return body;
  }

  $("#form-bt").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target, button = $("button[type=submit]", form);
    showError("#bt-error", null);
    syncSharedDates();
    busy(button, true, "Running…");
    try {
      const body = stockBody(form);
      const result = await api("/backtest/strategy", { method: "POST", body });
      $("#bt-strategy").append($("#bt-result"));
      renderBacktest(result);
      offerDeploy("stock", body, result);
    } catch (err) { showError("#bt-error", err); $("#bt-result").hidden = true; }
    busy(button, false);
  });

  // -- from a backtest to an algo, with the same settings ----------------------------------

  const deploy = { kind: null, body: null, result: null };

  function offerDeploy(kind, body, result) {
    Object.assign(deploy, { kind, body, result });
    const panel = $("#bt-deploy"), name = $("#bt-deploy-name");
    $("#bt-deploy-msg").textContent = ""; showError("#bt-deploy-error", null);
    const daily = !body.interval || body.interval === "1d";
    if (kind === "futures" && !daily) {
      panel.hidden = false; $("#bt-deploy-go").disabled = true;
      $("#bt-deploy-what").textContent = "Futures algos read day candles. Run this backtest on 1-day candles to make an algo from it.";
      return;
    }
    $("#bt-deploy-go").disabled = false;
    panel.hidden = !["stock", "structure", "futures"].includes(kind);
    if (panel.hidden) return;
    const label = kind === "stock" ? `${result.symbol.replace(/\.(NS|BO)$/, "")} ${result.strategy.name}` : result.strategy.name.split(" · ")[0] + " " + (body.underlying || "");
    name.value = label.replace(/[^\w &.-]/g, " ").replace(/\s+/g, " ").trim().slice(0, 40);
    $("#bt-deploy-budget-label").hidden = kind !== "stock"; $("#bt-deploy-loss-label").hidden = kind === "stock";
    if (kind === "stock") $("#bt-deploy-budget").value = Math.min(Number(body.capital) * Number(body.allocation_pct) / 100, 100000);
    const notes = {
      stock: "The algo uses this strategy, candle size and these exit rules. It trades inside the trade window in Settings"
        + (!daily && (body.trade_start !== state.app.settings.trade_start || body.trade_end !== state.app.settings.trade_end || body.square_off !== state.app.settings.square_off)
          ? ` (${state.app.settings.trade_start}–${state.app.settings.trade_end}, sell everything at ${state.app.settings.square_off}), not the times above: change them in Settings to match.` : "."),
      structure: "The algo uses this option strategy, these times and days, strikes, lots and each leg's exit rules. The whole position closes at the maximum loss you set here (the backtest's % stoploss and target for the whole position are not carried over).",
      futures: "The algo holds the near-month future while this strategy says so, on day candles. It closes everything at the maximum loss you set here.",
    };
    $("#bt-deploy-what").textContent = notes[kind] + " It is created stopped: start it from the Algos tab.";
  }

  async function createDeployedAlgo() {
    const button = $("#bt-deploy-go"), name = $("#bt-deploy-name").value, mode = $("#bt-deploy-mode").value;
    const { kind, body } = deploy;
    showError("#bt-deploy-error", null); busy(button, true, "Creating…");
    try {
      if (kind === "stock") {
        let strategy = body.strategy, params = body.strategy_params || {};
        if (body.rules) {                // rules typed on the screen: saved as a strategy so the algo can run them
          const saved = await api("/strategies", { method: "POST", body: { name, ...body.rules } }).catch((err) => {
            throw /already exists/.test(err.message) ? new Error("Your rules are saved as a strategy under the algo's name, and a strategy with that name already exists. Change the algo name.") : err;
          });
          strategy = saved.key; params = {};
          await reloadStrategies();
        }
        await api("/algos", { method: "POST", body: { name, symbol: body.symbol, strategy, strategy_params: params, interval: body.interval,
          budget: Number($("#bt-deploy-budget").value), mode, exits: body.exits } });
        await refreshAlgos();
      } else if (kind === "structure") {
        await api("/fno-algos", { method: "POST", body: { name, kind: "structure", structure: body.structure, underlying: body.underlying,
          expiry_kind: body.expiry_kind, schedule: body.mode, entry_time: body.entry_time, exit_time: body.exit_time, exit_days_before: body.exit_days_before,
          min_days: body.min_days, strategy: body.strategy, weekdays: body.weekdays, k: body.k, w: body.w, lots: body.lots,
          leg_exits: body.leg_exits, leg_scope: body.leg_scope, max_loss: Number($("#bt-deploy-loss").value || 0), target: 0,
          margin_pct: body.margin_pct, mode } });
        await refreshFnoAlgos();
      } else if (kind === "futures") {
        await api("/fno-algos", { method: "POST", body: { name, kind: "futures", direction: body.direction, underlying: body.underlying,
          expiry_kind: "monthly", schedule: "signal", strategy: body.strategy, strategy_params: body.strategy_params, lots: body.lots,
          max_loss: Number($("#bt-deploy-loss").value || 0), target: 0, margin_pct: body.margin_pct, mode } });
        await refreshFnoAlgos();
      }
      $("#bt-deploy-msg").replaceChildren(`Created “${name}” (${mode}), stopped. `,
        h("button", { type: "button", text: "Open Algos", onclick: () => { selectTab("algos"); $(`#algo-kind button[data-kind=${kind === "stock" ? "stocks" : "fno"}]`).click(); } }));
    } catch (err) { showError("#bt-deploy-error", err); }
    busy(button, false);
  }
  $("#bt-deploy-go").addEventListener("click", createDeployedAlgo);

  function renderBacktest(r) {
    $("#bt-result").hidden = false;
    $("#bt-deploy").hidden = true;
    const m = r.metrics;
    const tile = (k, v, s) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v", text: v }), s ? h("div", { class: "s", text: s }) : null);
    $("#bt-tiles").replaceChildren(...[
      tile("Total return", pct(m.total_return_pct), `Buy & hold ${pct(r.buy_hold.total_return_pct)}`),
      tile("CAGR", pct(m.cagr_pct), r.benchmark ? `${r.benchmark_name} ${pct(r.benchmark.cagr_pct)}` : null),
      tile("Max drawdown", pct(m.max_drawdown_pct, false), `on ${m.max_drawdown_date}`),
      tile("Sharpe", m.sharpe == null ? "–" : m.sharpe.toFixed(2), `Sortino ${m.sortino == null ? "–" : m.sortino.toFixed(2)}`),
      tile("Win rate", m.win_rate_pct == null ? "–" : m.win_rate_pct + "%", `${m.trades} closed trade${m.trades === 1 ? "" : "s"}`),
      tile("Profit factor", m.profit_factor == null ? "–" : m.profit_factor.toFixed(2), `In market ${m.exposure_pct}% of days`),
      tile("Final value", rupees(m.final_value), `from ${rupees(r.params.capital)}`),
      tile("Charges paid", rupees(m.total_charges), "STT, stamp, GST, DP"),
      m.reentries ? tile("Re-entries", String(m.reentries), "after a stoploss") : null].filter(Boolean));
    renderChecks(r);
    $("#bt-chart-title").textContent = `Portfolio value · ${r.symbol} · ${r.strategy.name} · ${state.app.intervals[r.interval]} candles · ${r.period.start} to ${r.period.end}`;
    $("#bt-note").textContent = r.data_note || ""; $("#bt-note").hidden = !r.data_note;
    const series = [{ name: r.strategy.name, color: "var(--s1)", points: r.equity },
      { name: r.buy_hold_name || "Buy & hold " + r.symbol, color: "var(--s2)", points: r.buy_hold_equity }];
    if (r.benchmark_equity) series.push({ name: r.benchmark_name, color: "var(--s3)", points: r.benchmark_equity });
    drawChart($("#bt-chart"), $("#bt-legend"), series);
    const stat = (label, get, fmt) => ({ label, get, fmt });
    const rows = [stat("Total return", (x) => x.total_return_pct, pct), stat("CAGR", (x) => x.cagr_pct, pct),
      stat("Volatility", (x) => x.volatility_pct, (v) => pct(v, false)), stat("Sharpe", (x) => x.sharpe, (v) => (v == null ? "–" : v.toFixed(2))),
      stat("Max drawdown", (x) => x.max_drawdown_pct, (v) => pct(v, false)), stat("Final value", (x) => x.final_value, (v) => rupees(v))];
    const cols = [{ name: r.strategy.name, data: m }, { name: "Buy & hold", data: r.buy_hold }];
    if (r.benchmark) cols.push({ name: r.benchmark_name, data: r.benchmark });
    $("#bt-compare").replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, h("th", { text: "" }), cols.map((c) => h("th", { class: "num", text: c.name })))),
      h("tbody", {}, rows.map((row) => h("tr", {}, h("td", { text: row.label }), cols.map((c) => h("td", { class: "num", text: row.fmt(row.get(c.data)) })))))),
      r.benchmark_note ? h("p", { class: "hint", text: r.benchmark_note }) : null);
    if (r.portfolio) {
      $("#bt-trades").replaceChildren(table([{ label: "ETF", get: (a) => a.symbol }, { label: "Weight", num: true, get: (a) => a.weight + "%" }], r.assets, ""),
        h("p", { class: "hint", text: `${m.trades} rebalances.` }));
      return;
    }
    $("#bt-trades").replaceChildren(table([
      ...(r.simulated ? [{ label: "Contract", get: (t) => t.contract }] : []),
      ...(r.structure ? [{ label: "Legs stopped out", num: true, get: (t) => t.legs_stopped }, { label: "Re-entries", num: true, get: (t) => t.reentries || 0 },
        { label: "Margin", num: true, get: (t) => rupees(t.margin) }] : []),
      ...(r.futures ? [{ label: "Side", get: (t) => t.side }, { label: "Rolls", num: true, get: (t) => t.rolls }] : []),
      { label: "Entry", get: (t) => t.entry_date }, { label: "Exit", get: (t) => t.exit_date || "open" },
      { label: "Qty", num: true, get: (t) => inr.format(t.quantity) },
      { label: r.structure ? "Net credit (+) / debit (−) per unit" : "Entry ₹", num: true, get: (t) => inr2.format(t.entry_price) },
      { label: r.structure ? "Index at exit" : "Exit ₹", num: true, get: (t) => inr2.format(t.exit_price) },
      { label: "Net P&L", num: true, get: (t) => rupees(t.pnl), tone: (t) => tone(t.pnl) },
      { label: "Return", num: true, get: (t) => pct(t.return_pct), tone: (t) => tone(t.pnl) },
      { label: "Days", num: true, get: (t) => t.days }, { label: "Exit reason", get: (t) => t.exit },
    ], r.trades.slice().reverse(), "The strategy made no trades in this period."));
  }

  function drawChart(host, legend, series) {
    const NS = "http://www.w3.org/2000/svg";
    const W = 1000, H = 340, pad = { l: 70, r: 16, t: 10, b: 26 };
    const svg = (tag, attrs) => { const el = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); return el; };
    const dates = series[0].points.map((p) => p[0]);
    const byDate = series.map((s) => new Map(s.points));
    const all = series.flatMap((s) => s.points.map((p) => p[1]));
    let lo = Math.min(...all), hi = Math.max(...all);
    const span = hi - lo || hi * 0.1 || 1;
    lo -= span * 0.05; hi += span * 0.05;
    const x = (i) => pad.l + (dates.length === 1 ? 0 : (i / (dates.length - 1)) * (W - pad.l - pad.r));
    const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": "Portfolio value over time: " + series.map((s) => s.name).join(", ") });
    const compact = (v) => (v >= 1e7 ? (v / 1e7).toFixed(2) + " Cr" : v >= 1e5 ? (v / 1e5).toFixed(2) + " L" : inr.format(v));
    for (let i = 0; i <= 4; i++) {
      const value = lo + ((hi - lo) * i) / 4, yy = y(value);
      root.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: yy, y2: yy }));
      const label = svg("text", { x: pad.l - 8, y: yy + 4, "text-anchor": "end" }); label.textContent = "₹" + compact(value); root.append(label);
    }
    const ticks = Math.min(6, dates.length);
    for (let i = 0; i < ticks; i++) {
      const index = Math.round((i / Math.max(ticks - 1, 1)) * (dates.length - 1));
      const label = svg("text", { x: x(index), y: H - 6, "text-anchor": i === 0 ? "start" : i === ticks - 1 ? "end" : "middle" });
      label.textContent = dates[index].slice(0, 7); root.append(label);
    }
    series.forEach((s, si) => {
      let d = "", pen = false;
      dates.forEach((date, i) => {
        const v = byDate[si].get(date);
        if (v == null) { pen = false; return; }
        d += `${pen ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`; pen = true;
      });
      root.append(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
    });
    const cross = svg("line", { class: "cross", y1: pad.t, y2: H - pad.b, visibility: "hidden" });
    const dots = series.map((s) => svg("circle", { r: 4, fill: s.color, stroke: "var(--surface)", "stroke-width": 2, visibility: "hidden" }));
    root.append(cross, ...dots);
    const tip = $("#tooltip");
    const hide = () => { cross.setAttribute("visibility", "hidden"); dots.forEach((dot) => dot.setAttribute("visibility", "hidden")); tip.hidden = true; };
    root.addEventListener("pointermove", (event) => {
      const box = root.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * W;
      const i = Math.max(0, Math.min(dates.length - 1, Math.round(((px - pad.l) / (W - pad.l - pad.r)) * (dates.length - 1))));
      cross.setAttribute("x1", x(i)); cross.setAttribute("x2", x(i)); cross.setAttribute("visibility", "visible");
      const rows = [h("div", {}, h("strong", { text: dates[i] }))];
      series.forEach((s, si) => {
        const v = byDate[si].get(dates[i]);
        if (v == null) { dots[si].setAttribute("visibility", "hidden"); return; }
        dots[si].setAttribute("cx", x(i)); dots[si].setAttribute("cy", y(v)); dots[si].setAttribute("visibility", "visible");
        rows.push(h("div", {}, h("i", { style: `background:${s.color}` }), `${s.name}: ${rupees(v)}`));
      });
      tip.replaceChildren(...rows); tip.hidden = false;
      const tw = tip.offsetWidth, left = event.clientX + 14 + tw > window.innerWidth ? event.clientX - tw - 14 : event.clientX + 14;
      tip.style.left = left + "px"; tip.style.top = Math.max(8, event.clientY - 20) + "px";
    });
    root.addEventListener("pointerleave", hide);
    host.replaceChildren(root);
    legend.replaceChildren(...series.map((s) => h("span", {}, h("i", { style: `background:${s.color}` }), s.name)));
  }

  // -- agent backtest ------------------------------------------------------------

  $("#form-bta").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target, button = $("button[type=submit]", form);
    showError("#bta-error", null);
    busy(button, true, "Starting…");
    try {
      const { job } = await api("/backtest/agents", { method: "POST", body: {
        symbols: form.symbols.value, start: form.start.value, end: form.end.value, every_n_days: Number(form.every_n_days.value) } });
      $("#bta-job").hidden = false; $("#bta-result").replaceChildren(); $("#bta-log").textContent = "";
      watchJob(job.id, { title: $("#bta-title"), status: $("#bta-status"), log: $("#bta-log") }, (done) => {
        busy(button, false);
        if (done.status !== "done") return;
        const r = done.result;
        $("#bta-result").replaceChildren(
          h("p", {}, `${r.cells_run} run, ${r.skipped} skipped, ${r.failures.length} failed · ${r.resolved} scored, ${r.pending} pending`),
          table([{ label: "Rating", get: (x) => x.rating }, { label: "Count", num: true, get: (x) => x.count },
            { label: "Called direction", num: true, get: (x) => (x.hit_rate_pct == null ? "n/a" : x.hit_rate_pct + "%") },
            { label: "Mean alpha vs benchmark", num: true, get: (x) => pct(x.mean_alpha_pct), tone: (x) => tone(x.mean_alpha_pct) }],
          r.by_rating, "No decision has settled yet: outcomes need 5 trading days after each date."),
          r.failures.length ? h("div", { class: "warnbox", text: "Failed: " + r.failures.map((f) => `${f[0]} ${f[1]}: ${f[2]}`).join("; ") }) : null,
          h("p", { class: "hint", text: "Memory log: " + r.log_path }));
      });
    } catch (err) { showError("#bta-error", err); busy(button, false); }
  });

  // -- trade -------------------------------------------------------------------

  async function refreshTrade() {
    renderKiteInfo();
    refreshPaper(); refreshJournal(); refreshGtts();
    if (state.app.kite.connected) refreshKiteAccount();
  }

  function renderKiteInfo() {
    const kite = state.app.kite;
    $("#kite-connect").textContent = kite.connected ? "Disconnect" : "Connect";
    $("#kite-login").hidden = !kite.connected; $("#kite-refresh").hidden = !kite.connected;
    const info = $("#kite-info"), label = kite.broker_label || "Kite", rest = kite.broker && kite.broker !== "kite";
    $("#broker-title").textContent = rest ? label + (kite.broker_status === "experimental" ? " (experimental)" : "") : "Kite (Zerodha)";
    $("#kite-login").textContent = "Log in to " + (rest ? label : "Kite");
    if (!kite.connected) { info.textContent = kite.error || (rest ? `Not connected. Connect to read your ${label} account with the keys in Settings → Broker.` : "Not connected. Connect to read your Zerodha account through the MCP server in Settings."); $("#kite-account").replaceChildren(); return; }
    if (rest) { info.textContent = `Connected to ${label}'s API. ` + (kite.signed_in ? "Signed in for today." : "Press “Log in to " + label + "” to sign in.") + " Experimental: check every live order in your broker's app."; return; }
    info.textContent = `Connected over ${kite.transport} to ${kite.endpoint}. ` + (kite.can_trade
      ? "This server can place orders."
      : "This server is read-only (no place_order tool): portfolio and prices only.")
      + (kite.fallback_from ? ` (${kite.fallback_from} is not installed, so the desk connected to the URL directly.)` : "");
  }

  $("#kite-connect").addEventListener("click", async (event) => {
    const button = event.target; showError("#kite-error", null);
    const connecting = !state.app.kite.connected;
    busy(button, true, connecting ? "Connecting…" : "Disconnecting…");
    if (connecting) $("#kite-info").textContent = "Connecting. If a Zerodha sign-in page opens in your browser, complete it there; this waits up to 4 minutes.";
    try { await api(connecting ? "/kite/connect" : "/kite/disconnect", { method: "POST" }); }
    catch (err) { showError("#kite-error", err); }
    busy(button, false);
    await loadState(); renderKiteInfo();
    if (state.app.kite.connected) refreshKiteAccount();
  });

  $("#kite-login").addEventListener("click", async () => {
    showError("#kite-error", null);
    try {
      const login = await api("/kite/login", { method: "POST" });
      const box = $("#kite-account");
      box.replaceChildren(h("div", { class: "warnbox" },
        login.url ? h("p", {}, `Open this link, sign in to ${state.app.kite.broker_label || "Kite"}, then press Refresh: `, h("a", { href: login.url, target: "_blank", rel: "noopener noreferrer", style: "overflow-wrap:anywhere", text: login.url })) : h("p", { text: login.message })));
      await loadState(); renderKiteInfo();
    } catch (err) { showError("#kite-error", err); }
  });
  $("#kite-refresh").addEventListener("click", () => refreshKiteAccount());

  async function refreshKiteAccount() {
    showError("#kite-error", null);
    const box = $("#kite-account");
    let account;
    try { account = await api("/kite/account"); }
    catch (err) {
      if (err.payload && err.payload.needs_login) { const label = state.app.kite.broker_label || "Kite"; box.replaceChildren(h("div", { class: "warnbox", text: `${label} needs a login. Press “Log in to ${label}”. Sessions expire every day.` })); }
      else showError("#kite-error", err);
      return;
    }
    const profile = account.profile || {}, equity = (account.margins && (account.margins.equity || account.margins)) || {};
    const available = equity.available || {};
    const md = account.market_data;
    box.replaceChildren(
      md ? h("div", { class: md.ok ? "okbox" : "warnbox", text: md.detail }) : null,
      h("dl", { class: "kv" }, h("dt", { text: "Account" }), h("dd", { text: `${profile.user_name || "–"} (${profile.user_id || "–"})` }),
        h("dt", { text: "Available funds" }), h("dd", { text: rupees(available.live_balance ?? available.cash ?? equity.net, 2) })),
      h("h3", { text: "Holdings" }),
      table([{ label: "Symbol", get: (r) => r.tradingsymbol }, { label: "Qty", num: true, get: (r) => r.quantity },
        { label: "Avg ₹", num: true, get: (r) => inr2.format(r.average_price || 0) }, { label: "LTP ₹", num: true, get: (r) => inr2.format(r.last_price || 0) },
        { label: "P&L", num: true, get: (r) => rupees(r.pnl, 2), tone: (r) => tone(r.pnl) }], account.holdings || [], "No holdings."),
      h("h3", { text: "Positions" }),
      table([{ label: "Symbol", get: (r) => r.tradingsymbol }, { label: "Product", get: (r) => r.product }, { label: "Qty", num: true, get: (r) => r.quantity },
        { label: "P&L", num: true, get: (r) => rupees(r.pnl, 2), tone: (r) => tone(r.pnl) }], account.positions || [], "No open positions."),
      h("h3", { text: "Today's orders" }),
      table([{ label: "Order", get: (r) => r.order_id }, { label: "Symbol", get: (r) => r.tradingsymbol }, { label: "Side", get: (r) => r.transaction_type },
        { label: "Qty", num: true, get: (r) => r.quantity }, { label: "Price", num: true, get: (r) => inr2.format(r.price || r.average_price || 0) },
        { label: "Status", get: (r) => r.status }], account.orders || [], "No orders today."));
  }

  async function refreshPaper() {
    const book = await api("/paper");
    const rows = Object.entries(book.positions).map(([k, v]) => ({ instrument: k, ...v }));
    $("#paper-account").replaceChildren(
      h("dl", { class: "kv" }, h("dt", { text: "Cash" }), h("dd", { text: rupees(book.cash, 2) }), h("dt", { text: "Starting capital" }), h("dd", { text: rupees(book.capital) })),
      table([{ label: "Instrument", get: (r) => r.instrument }, { label: "Qty", num: true, get: (r) => r.quantity },
        { label: "Avg ₹", num: true, get: (r) => inr2.format(r.average_price) }], rows, "No paper positions."));
  }
  $("#paper-reset").addEventListener("click", async () => {
    if (!window.confirm("Reset the paper account to its starting capital? Paper positions and orders are cleared.")) return;
    await api("/paper/reset", { method: "POST" }); refreshPaper();
  });

  async function refreshJournal() {
    const { orders } = await api("/order/journal");
    $("#order-journal").replaceChildren(table([
      { label: "Time (IST)", get: (r) => r.time_ist }, { label: "Mode", get: (r) => r.mode }, { label: "By", get: (r) => r.source || "manual" },
      { label: "Side", get: (r) => (r.gtt ? "GTT " : "") + r.side },
      { label: "Qty", num: true, get: (r) => r.quantity }, { label: "Symbol", get: (r) => r.tradingsymbol },
      { label: "Price", num: true, get: (r) => inr2.format(r.fill_price || r.price || 0) },
      { label: "Result", get: (r) => (r.ok ? r.order_id || "sent" : "REJECTED: " + r.error), tone: (r) => (r.ok ? "" : "neg") },
    ], orders, "No orders yet."));
  }

  function fillTicket(order) {
    const form = $("#form-order");
    form.symbol.value = order.tradingsymbol + (order.exchange === "BSE" ? ".BO" : "");
    form.side.value = order.side; form.quantity.value = order.quantity; form.product.value = order.product;
    form.order_type.value = order.order_type; form.price.value = order.price || ""; form.trigger_price.value = order.trigger_price || "";
    form.variety.value = order.variety;
    clearReview();
  }

  function clearReview() { state.pending = null; const box = $("#order-review"); if (box) { box.hidden = true; box.replaceChildren(); } }
  $("#form-order").addEventListener("input", clearReview);

  $("#order-quote").addEventListener("click", async () => {
    const out = $("#order-quote-out"), form = $("#form-order");
    showError("#order-error", null); out.textContent = "…";
    try {
      const q = await api("/quote?symbol=" + encodeURIComponent(form.symbol.value));
      out.textContent = q.last_price ? `${q.exchange}:${q.tradingsymbol} ₹${inr2.format(q.last_price)} · ${q.source}` : "No price available.";
    } catch (err) { out.textContent = ""; showError("#order-error", err); }
  });

  $("#form-order").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target, button = $("button[type=submit]", form);
    showError("#order-error", null); clearReview();
    busy(button, true, "Checking…");
    try {
      const resolved = await api("/symbol?q=" + encodeURIComponent(form.symbol.value));
      if (!resolved.kite) throw new Error(`${resolved.symbol} is not an NSE/BSE equity that can be ordered.`);
      const review = await api("/order/preview", { method: "POST", body: { mode: state.mode, order: {
        exchange: resolved.kite.exchange, tradingsymbol: resolved.kite.tradingsymbol, side: form.side.value,
        quantity: Number(form.quantity.value), product: form.product.value, order_type: form.order_type.value,
        price: Number(form.price.value || 0), trigger_price: Number(form.trigger_price.value || 0), variety: form.variety.value },
        stop: form.side.value === "BUY" ? ticketStop(form, Number(form.price.value || 0)) : null } });
      renderReview(review);
    } catch (err) {
      const reasons = err.payload && err.payload.errors;
      showError("#order-error", reasons ? new Error(reasons.join("\n")) : err);
    }
    busy(button, false);
  });

  function renderReview(review) {
    const box = $("#order-review"); box.hidden = false;
    const o = review.order, live = review.mode === "live";
    const parts = [h("h3", { text: `${live ? "LIVE" : "Paper"} order: ${o.side} ${o.quantity} ${o.exchange}:${o.tradingsymbol}` }),
      h("dl", { class: "kv" },
        h("dt", { text: "Type" }), h("dd", { text: `${o.order_type} · ${o.product} · ${o.variety}` }),
        h("dt", { text: "Price" }), h("dd", { text: o.price ? rupees(o.price, 2) : "market" }),
        h("dt", { text: "Last price" }), h("dd", { text: review.last_price ? `${rupees(review.last_price, 2)} (${review.last_price_source})` : review.last_price_source }),
        h("dt", { text: "Order value" }), h("dd", { text: rupees(review.value, 2) }),
        h("dt", { text: "Est. charges" }), h("dd", { text: review.charges ? `${rupees(review.charges.total, 2)} (STT ${rupees(review.charges.stt, 2)}, stamp ${rupees(review.charges.stamp_duty, 2)}, GST ${rupees(review.charges.gst, 2)}, DP ${rupees(review.charges.dp, 2)})` : "–" }),
        review.risk ? h("dt", { text: "If the stoploss hits" }) : null,
        review.risk ? h("dd", { text: `${rupees(review.risk.stop, 2)}: lose about ${rupees(review.risk.loss)}, ${review.risk.of_capital_pct}% of ${rupees(review.risk.capital)}${review.risk.limit_pct ? ` (limit ${review.risk.limit_pct}%)` : ""}` }) : null)];
    review.warnings.forEach((w) => parts.push(h("div", { class: "warnbox", text: w })));
    if (!review.ok) {
      parts.push(h("div", { class: "error", text: "This order cannot be placed:\n• " + review.errors.join("\n• ") }));
      box.replaceChildren(h("div", { class: "confirm" + (live ? " live" : "") }, parts)); return;
    }
    state.pending = review.token;
    const input = h("input", { type: "text", autocomplete: "off", placeholder: review.phrase, "aria-label": "Confirmation phrase" });
    const send = h("button", { class: live ? "danger" : "primary", text: live ? "Place LIVE order" : "Place paper order", disabled: true });
    const result = h("div", {});
    input.addEventListener("input", () => { send.disabled = input.value.trim().toUpperCase().replace(/\s+/g, " ") !== review.phrase; });
    send.addEventListener("click", async () => {
      if (!state.pending) return;
      const token = state.pending; state.pending = null; send.disabled = true; input.disabled = true;
      try {
        const placed = await api("/order/place", { method: "POST", body: { token, phrase: input.value } });
        result.replaceChildren(h("div", { class: "warnbox", text: live
          ? `Sent to Kite. Order id ${placed.order_id || "(not returned)"}. Check its status under Today's orders: sent is not the same as filled.`
          : `Paper order ${placed.order_id} filled at ${rupees(placed.fill_price, 2)}.` }));
        const entry = placed.fill_price || o.price || review.last_price;
        try { if (await savePlan(o, review.mode, entry)) result.append(h("p", { class: "hint", text: "Plan saved to your trade journal (P&L tab)." })); }
        catch (err) { result.append(h("div", { class: "error", text: "The order went through, but its plan was not saved to the journal: " + err.message })); }
        protectAfter(o, entry);
      } catch (err) { result.replaceChildren(h("div", { class: "error", text: "Not placed: " + err.message })); }
      refreshJournal(); refreshPaper(); if (live && state.app.kite.connected) refreshKiteAccount();
    });
    parts.push(h("p", {}, "Type ", h("code", { text: review.phrase }), ` to confirm. Valid for ${review.expires_in} seconds.`), h("div", { class: "row" }, input, send), result);
    box.replaceChildren(h("div", { class: "confirm" + (live ? " live" : "") }, parts));
  }

  // -- settings ----------------------------------------------------------------

  const TIMES = ["trade_start", "trade_end", "square_off", "mcx_trade_start", "mcx_trade_end"];
  const NUMERIC = ["risk_capital", "risk_per_trade_pct", "max_risk_pct", "mcx_exit_days", "mcx_margin_pct", "daily_loss_limit", "max_debate_rounds", "max_risk_discuss_rounds", "max_order_value", "max_orders_per_day", "position_pct", "max_position_pct", "limit_buffer_pct", "paper_capital"];
  const TEXT = ["deep_think_llm", "quick_think_llm", "backend_url", "default_exchange", "product", "mcp_server"];

  function fillSettings() {
    const form = $("#form-settings"), s = state.app.settings;
    form.llm_provider.replaceChildren(...Object.keys(state.app.providers).map((p) => h("option", { value: p, text: p })));
    form.llm_provider.value = s.llm_provider;
    [...NUMERIC, ...TEXT].forEach((k) => { form[k].value = s[k]; });
    $$("input[name=analyst]", form).forEach((box) => { box.checked = s.analysts.includes(box.value); });
    form.kill_switch.checked = s.kill_switch; form.live_enabled.checked = s.live_enabled; form.fno_naked_live.checked = s.fno_naked_live; form.require_stoploss.checked = s.require_stoploss;
    form.charts_enabled.checked = s.charts_enabled; form.enforce_trade_window.checked = s.enforce_trade_window;
    form.chart_yahoo.checked = s.chart_source === "kite_then_yahoo";
    form.alerts_enabled.checked = s.alerts_enabled; form.telegram_chat_id.value = s.telegram_chat_id; form.telegram_token.value = "";
    $("#tg-saved").textContent = s.api_keys.TELEGRAM_BOT_TOKEN ? `(saved ${s.api_keys.TELEGRAM_BOT_TOKEN})` : "(not set)";
    form.holidays.value = s.holidays.join("\n");
    form.mcx_holidays.value = s.mcx_holidays.join("\n");
    const year = state.app.today.slice(0, 4), count = s.holidays.filter((d) => d.startsWith(year)).length;
    $("#holidays-note").textContent = count < 10 ? `${count} holidays listed for ${year}. NSE usually has about 15: the list looks incomplete.` : `${count} holidays listed for ${year}.`;
    TIMES.forEach((k) => { form[k].value = s[k]; });
    if (form.llm_preset.options.length === 1) {
      const groups = [["Paid AI services", (x) => x.paid], ["Free and self-hosted", (x) => !x.paid]];
      form.llm_preset.append(...groups.map(([label, keep]) => h("optgroup", { label }, state.app.llm_presets
        .map((preset, i) => [preset, i]).filter(([preset]) => keep(preset)).map(([preset, i]) => h("option", { value: i, text: preset.name })))));
      form.llm_preset.addEventListener("change", () => {
        const preset = state.app.llm_presets[form.llm_preset.value];
        if (!preset) return;
        form.llm_provider.value = preset.provider; form.backend_url.value = preset.backend_url;
        form.deep_think_llm.value = preset.deep; form.quick_think_llm.value = preset.quick; updateKeyHint();
        $("#preset-hint").textContent = `Get the key from ${preset.key_from}. Paste it below, check the model names, then Save and press Test AI connection.` +
          (preset.paid ? " Paid plans bill per use: one analysis makes dozens of calls." : " Free tiers are rate-limited; one analysis makes dozens of calls, so expect waits or failures on the smallest plans.");
      });
    }
    form.mcp.value = JSON.stringify(s.mcp, null, 2);
    form.broker.replaceChildren(...state.app.brokers.map((b) => h("option", { value: b.name, text: b.label + (b.status === "experimental" ? " (experimental)" : "") })));
    form.broker.value = s.broker;
    renderBrokerFields();
    form.api_key.value = "";
    updateKeyHint();
  }
  function updateKeyHint() {
    const form = $("#form-settings"), env = state.app.providers[form.llm_provider.value];
    const models = (state.app.llm_models || {})[form.llm_provider.value];
    $("#models-deep").replaceChildren(...((models && models.deep) || []).map((m) => h("option", { value: m.id, label: m.label })));
    $("#models-quick").replaceChildren(...((models && models.quick) || []).map((m) => h("option", { value: m.id, label: m.label })));
    $("#model-hint").textContent = models ? `Suggested models for ${form.llm_provider.value} appear when you click a model box; you can also type any model id the provider offers.` : "Type the model id your service expects.";
    const saved = env && state.app.settings.api_keys[env];
    $("#key-env").textContent = env ? `(${env}${saved ? ", saved " + saved : ", not set"})` : "(this provider needs no key)";
    form.api_key.disabled = !env;
  }
  $("#form-settings").llm_provider.addEventListener("change", updateKeyHint);

  function renderBrokerFields() {
    const form = $("#form-settings"), broker = state.app.brokers.find((b) => b.name === form.broker.value);
    const saved = (state.app.settings.broker_keys || {})[form.broker.value] || {};
    $("#mcp-panel").hidden = form.broker.value !== "kite";
    $("#broker-notes").textContent = broker ? broker.notes + (broker.oauth ? ` Register this redirect URL on the app: ${location.origin}/broker/callback` : "") : "";
    $("#broker-fields").replaceChildren(...(broker ? broker.fields : []).map((f) => h("label", {},
      f.label + (f.required ? "" : " (optional)") + (f.secret && saved[f.key] ? ` (saved ${saved[f.key]})` : ""),
      h("input", { name: "bk_" + f.key, type: f.secret ? "password" : "text", autocomplete: "off", value: f.secret ? "" : (saved[f.key] || "") }),
      f.hint ? h("span", { class: "hint", text: f.hint }) : null)));
  }
  $("#form-settings").broker.addEventListener("change", renderBrokerFields);

  $("#form-settings").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target, button = $("button[type=submit]", form);
    showError("#settings-error", null); $("#settings-msg").textContent = "";
    let mcp;
    try { mcp = JSON.parse(form.mcp.value); } catch (err) { showError("#settings-error", new Error("The MCP configuration is not valid JSON: " + err.message)); return; }
    if (form.live_enabled.checked && !state.app.settings.live_enabled
      && !window.confirm("Allow live orders? Orders you confirm in Live mode will be sent to the exchange with real money.")) { form.live_enabled.checked = false; return; }
    if (form.fno_naked_live.checked && !state.app.settings.fno_naked_live
      && !window.confirm("Allow live F&O algos to sell options with no bought option to cover them? A gap move can lose far more than the algo's maximum loss.")) { form.fno_naked_live.checked = false; return; }
    const changes = { llm_provider: form.llm_provider.value, mcp, kill_switch: form.kill_switch.checked, live_enabled: form.live_enabled.checked, fno_naked_live: form.fno_naked_live.checked, require_stoploss: form.require_stoploss.checked, charts_enabled: form.charts_enabled.checked, enforce_trade_window: form.enforce_trade_window.checked,
      chart_source: form.chart_yahoo.checked ? "kite_then_yahoo" : "kite",
      alerts_enabled: form.alerts_enabled.checked, telegram_chat_id: form.telegram_chat_id.value, holidays: form.holidays.value,
      mcx_holidays: form.mcx_holidays.value, analysts: $$("input[name=analyst]:checked", form).map((b) => b.value) };
    NUMERIC.forEach((k) => { changes[k] = Number(form[k].value); });
    TEXT.forEach((k) => { changes[k] = form[k].value; });
    TIMES.forEach((k) => { changes[k] = form[k].value; });
    changes.broker = form.broker.value;
    const brokerInfo = state.app.brokers.find((b) => b.name === form.broker.value);
    if (brokerInfo && brokerInfo.fields.length) {
      const keys = {};
      brokerInfo.fields.forEach((f) => { const v = form["bk_" + f.key].value; if (!f.secret || v) keys[f.key] = v; });
      changes.broker_keys = { [form.broker.value]: keys };
    }
    const env = state.app.providers[form.llm_provider.value];
    if (env && form.api_key.value) changes.api_keys = { [env]: form.api_key.value };
    if (form.telegram_token.value) changes.api_keys = { ...(changes.api_keys || {}), TELEGRAM_BOT_TOKEN: form.telegram_token.value };
    busy(button, true, "Saving…");
    try {
      await api("/settings", { method: "PUT", body: changes });
      await loadState(); fillSettings(); applyChartsSetting();
      $("#settings-msg").textContent = state.app.llm_problem ? "Saved. " + state.app.llm_problem : "Saved.";
    } catch (err) { showError("#settings-error", err); }
    busy(button, false);
  });


  // -- theme and session ---------------------------------------------------------

  function currentTheme() {
    return document.documentElement.dataset.theme
      || (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  }
  function renderTheme() {
    const dark = currentTheme() === "dark", button = $("#theme-toggle");
    button.textContent = dark ? "☀  Light theme" : "☾  Dark theme";
    button.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
  }
  $("#theme-toggle").addEventListener("click", () => {
    const next = currentTheme() === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    try { localStorage.setItem("desk-theme", next); } catch (err) { /* storage blocked: lasts this visit */ }
    renderTheme();
  });
  $("#logout").addEventListener("click", async () => {
    try { await api("/logout", { method: "POST" }); } catch (err) { /* already signed out */ }
    window.location.replace("/login");
  });

  // -- charts ----------------------------------------------------------------------

  const LINE_COLORS = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)", "var(--s5)", "var(--s6)"];

  function applyChartsSetting() {
    const on = state.app.settings.charts_enabled;
    $("#tabbtn-charts").hidden = !on;
    if (!on && !$("#tab-charts").hidden) selectTab("markets");
  }

  const chartState = { interval: "1d", charts: [], timer: null, request: 0 };

  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }

  function startCharts() {
    const form = $("#form-chart");
    $("#chart-interval").replaceChildren(...Object.entries(state.app.intervals).map(([key, label]) =>
      h("button", { type: "button", "data-interval": key, "aria-pressed": String(key === chartState.interval), title: label,
        text: key === "1d" ? "1D" : key, onclick: () => { chartState.interval = key;
          $$("#chart-interval button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.interval === key)));
          if (form.symbol.value.trim()) loadChart(true); } })));
    $("#chart-overlays").replaceChildren(...Object.entries(state.app.chart_overlays).map(([key, name]) =>
      h("label", { class: "check" }, h("input", { type: "checkbox", name: "overlay", value: key, checked: key === "sma50" }), name)));
    $("#chart-panels").replaceChildren(...Object.entries(state.app.chart_panels).map(([key, name]) =>
      h("label", { class: "check" }, h("input", { type: "checkbox", name: "panel", value: key, checked: key === "rsi" || key === "volume" }), name)));
    applyChartsSetting();
    form.addEventListener("submit", (event) => { event.preventDefault(); loadChart(true); });
    form.addEventListener("change", (event) => { if (event.target.name !== "symbol" && !$("#chart-result").hidden) loadChart(true); });
    new MutationObserver(() => { if (!$("#chart-result").hidden) loadChart(true); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  async function loadChart(rebuild) {
    const form = $("#form-chart"), button = $("button[type=submit]", form), request = ++chartState.request;
    if (!form.symbol.value.trim()) return;
    if (rebuild) { showError("#chart-error", null); busy(button, true, "Loading…"); }
    const overlays = $$("input[name=overlay]:checked", form).map((b) => b.value)
      .filter((key) => key !== "vwap" || chartState.interval !== "1d").join(",");
    const panels = $$("input[name=panel]:checked", form).map((b) => b.value).join(",");
    try {
      const query = new URLSearchParams({ symbol: form.symbol.value, interval: chartState.interval, overlays, panels });
      const data = await api("/chart?" + query);
      if (request !== chartState.request) return;          // a newer request has replaced this one
      if (rebuild || !chartState.series) drawPriceChart(data); else refreshPriceChart(data);
    } catch (err) {
      if (rebuild) { showError("#chart-error", err); $("#chart-result").hidden = true; stopChartTimer(); }
    }
    if (rebuild) busy(button, false);
  }

  function stopChartTimer() { if (chartState.timer) { clearInterval(chartState.timer); chartState.timer = null; } }

  function chartOptions(height) {
    return { height, autoSize: true,
      layout: { background: { color: cssVar("--surface") }, textColor: cssVar("--text2"), fontSize: 12 },
      grid: { vertLines: { color: cssVar("--line") }, horzLines: { color: cssVar("--line") } },
      rightPriceScale: { borderColor: cssVar("--line"), minimumWidth: 84 },
      timeScale: { borderColor: cssVar("--line"), timeVisible: chartState.interval !== "1d", secondsVisible: false, rightOffset: 4 },
      crosshair: { mode: LightweightCharts.CrosshairMode.Normal },
      localization: { priceFormatter: (p) => inr2.format(p) } };
  }

  function lineOptions(line, color) {
    const quiet = { priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, color };
    if (line.dots) return { ...quiet, lineVisible: false, pointMarkersVisible: true, pointMarkersRadius: 2 };
    return { ...quiet, lineWidth: line.thin || line.step ? 1 : 2,
      lineStyle: line.dashed ? LightweightCharts.LineStyle.Dashed : LightweightCharts.LineStyle.Solid,
      lineType: line.step ? LightweightCharts.LineType.WithSteps : LightweightCharts.LineType.Simple };
  }

  function drawPriceChart(r) {
    $("#chart-result").hidden = false;
    chartState.charts.forEach((c) => c.remove());
    chartState.charts = []; chartState.lines = []; chartState.panelSeries = [];
    const colors = [1, 2, 3, 4, 5, 6].map((i) => cssVar("--s" + i));
    const up = cssVar("--good"), down = cssVar("--bad");
    const main = LightweightCharts.createChart($("#chart-main"), chartOptions(440));
    chartState.charts.push(main);
    chartState.series = main.addCandlestickSeries({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
    chartState.series.setData(r.candles);
    const keys = [...new Set(r.lines.map((l) => l.key))];
    r.lines.forEach((line) => {
      const series = main.addLineSeries(lineOptions(line, colors[keys.indexOf(line.key) % colors.length]));
      series.setData(line.points); chartState.lines.push(series);
    });
    $("#chart-legend").replaceChildren(...keys.map((key) => h("span", {}, h("i", { style: `background:${colors[keys.indexOf(key) % colors.length]}` }),
      state.app.chart_overlays[key] + (key.startsWith("pivots") ? ": P solid, R/S dashed" : ""))));
    const host = $("#chart-panels-out"); host.replaceChildren();
    r.panels.forEach((panel) => {
      const box = h("div", { class: "tvpanel" });
      host.append(h("h3", { text: panel.name }), box);
      const chart = LightweightCharts.createChart(box, chartOptions(150));
      chartState.charts.push(chart);
      const entry = { key: panel.key, series: [] };
      if (panel.kind === "bars") {
        const bars = chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceLineVisible: false, lastValueVisible: false });
        bars.setData(panel.points.map((p) => ({ time: p.time, value: p.value, color: p.up ? up : down })));
        entry.series.push(bars);
      } else {
        if (panel.histogram) {
          const hist = chart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: false });
          hist.setData(panel.histogram.map((p) => ({ ...p, color: p.value >= 0 ? up : down }))); entry.series.push(hist);
        }
        panel.lines.forEach((line, i) => {
          const series = chart.addLineSeries({ ...lineOptions(line, colors[i]), lastValueVisible: true, title: line.name });
          series.setData(line.points); entry.series.push(series);
          if (i === 0) (panel.guides || []).forEach((g) => series.createPriceLine({ price: g, color: cssVar("--muted"), lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, axisLabelVisible: true }));
        });
      }
      chartState.panelSeries.push(entry);
    });
    // One time axis: scrolling or zooming any chart moves them all.
    let syncing = false;
    chartState.charts.forEach((chart) => chart.timeScale().subscribeVisibleLogicalRangeChange((range) => {
      if (syncing || !range) return; syncing = true;
      chartState.charts.forEach((other) => { if (other !== chart) other.timeScale().setVisibleLogicalRange(range); });
      syncing = false;
    }));
    const show = Math.min(r.candles.length, 160);
    main.timeScale().setVisibleLogicalRange({ from: r.candles.length - show, to: r.candles.length + 3 });
    const ohlc = $("#chart-ohlc");
    const describe = (c) => { ohlc.textContent = c ? `O ${inr2.format(c.open)}   H ${inr2.format(c.high)}   L ${inr2.format(c.low)}   C ${inr2.format(c.close)}   ${pct((c.close / c.open - 1) * 100)}` : ""; };
    main.subscribeCrosshairMove((param) => { const c = param.seriesData && param.seriesData.get(chartState.series); describe(c || chartState.lastCandle); });
    chartState.describe = describe;
    refreshPriceChart(r, true);
    stopChartTimer();
    chartState.timer = setInterval(() => {
      if ($("#tab-charts").hidden || $("#chart-result").hidden || !$("#form-chart").live.checked) return;
      if (state.app.session.open) loadChart(false);
    }, 15000);
  }

  function refreshPriceChart(r, first) {
    const candles = r.candles.slice();
    const last = { ...candles[candles.length - 1] };
    if (r.live_price && r.session.open) {       // the forming candle follows Kite's live price
      last.close = r.live_price; last.high = Math.max(last.high, r.live_price); last.low = Math.min(last.low, r.live_price);
      candles[candles.length - 1] = last;
    }
    if (!first) {
      chartState.series.setData(candles);
      r.lines.forEach((line, i) => { if (chartState.lines[i]) chartState.lines[i].setData(line.points); });
    } else if (r.live_price && r.session.open) chartState.series.update(last);
    chartState.lastCandle = last; chartState.describe(last);
    const label = state.app.intervals[r.interval];
    $("#chart-title").textContent = `${r.symbol} · ${label} candles · last ${rupees(last.close, 2)} · ${r.last}`;
    const note = [r.note, r.truncated ? "Showing the most recent 1,500 candles." : null].filter(Boolean).join(" ");
    $("#chart-note").textContent = note; $("#chart-note").hidden = !note;
    const pill = $("#chart-live"), live = $("#form-chart").live.checked;
    if (!r.session.open) { pill.textContent = `Market closed · ${r.source} data`; pill.className = "pill"; }
    else if (!live) { pill.textContent = "Live refresh off"; pill.className = "pill"; }
    else { pill.textContent = `Live · ${r.source} · updated ${r.session.time_ist.slice(11)} IST`; pill.className = "pill " + (r.source === "Kite" ? "good" : "warn"); }
    const pivots = $("#chart-pivots");
    pivots.hidden = !r.pivots;
    if (r.pivots) {
      const order = ["R3", "R2", "R1", "P", "S1", "S2", "S3"];
      pivots.replaceChildren(h("h3", { text: "Pivot levels (traditional)" }),
        h("table", {}, h("thead", {}, h("tr", {}, h("th", { text: "For" }), order.map((k) => h("th", { class: "num", text: k })))),
          h("tbody", {}, r.pivots.map((t) => h("tr", {}, h("td", { text: `${t.timeframe[0].toUpperCase() + t.timeframe.slice(1)}: ${t.for}` }),
            order.map((k) => h("td", { class: "num", text: rupees(t.levels[k], 2) })))))),
        h("p", { class: "hint", text: "Traditional (floor) pivots from the previous day, week or month's high, low and close: P = (H + L + C) / 3, R1 = 2P − L, S1 = 2P − H, R2 = P + (H − L), S2 = P − (H − L), R3 = H + 2(P − L), S3 = L − 2(H − P). Kite does not publish pivots; the desk computes them." }));
    }
  }

  // -- algos -------------------------------------------------------------------------

  function startAlgos() {
    const form = $("#form-algo");
    const choices = () => state.app.strategies.filter((s) => s.key !== "buy_hold");
    strategyOptions(form.strategy, (s) => s.key !== "buy_hold"); intervalOptions(form.interval);
    const params = () => {
      const strategy = choices().find((s) => s.key === form.strategy.value);
      $("#algo-params").replaceChildren(...strategy.params.map((p) => h("label", {}, p.label,
        h("input", { name: "p_" + p.name, type: "number", min: p.minimum, max: p.maximum, step: p.integer ? 1 : 0.1, value: p.default, required: true }))));
    };
    form.strategy.addEventListener("change", params); params();
    $("#algo-exits").replaceWith(Object.assign(exitRulesBox(false), { id: "algo-exits" }));
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const editing = algoEdit.stock;
      const button = $("button[type=submit]", form); showError("#algo-error", null); busy(button, true, editing ? "Saving…" : "Creating…");
      const strategyParams = {};
      $$("#algo-params input").forEach((input) => { strategyParams[input.name.slice(2)] = Number(input.value); });
      const body = { name: form.name.value, symbol: form.symbol.value, strategy: form.strategy.value,
        strategy_params: strategyParams, budget: Number(form.budget.value), mode: form.mode.value, interval: form.interval.value,
        exits: $("#algo-exits").read() };
      let saved = false;
      try {
        if (editing) await api(`/algos/${editing.id}`, { method: "PUT", body });
        else await api("/algos", { method: "POST", body });
        saved = true;
      } catch (err) { showError("#algo-error", err); }
      busy(button, false);
      if (saved) { endAlgoEdit("stock"); form.name.value = ""; form.symbol.value = ""; await refreshAlgos(); }
    });
    $("#algo-edit-cancel").addEventListener("click", () => endAlgoEdit("stock"));
    $("#algo-refresh").addEventListener("click", refreshAlgos);
  }

  // -- editing an algo that is not trading live ---------------------------------------------------------------------
  // The create form doubles as the edit form. The name is fixed (the P&L ledger files every fill under it). While an
  // algo holds a position, the fields tied to that position are locked; the server enforces the same rules.

  const algoEdit = { stock: null, fno: null };
  const EDIT = {
    stock: { form: "#form-algo", banner: "#algo-editing", title: "#algo-form-title", newTitle: "New algo", create: "Create algo", error: "#algo-error",
      params: "#algo-params", exits: "#algo-exits" },
    fno: { form: "#form-fno", banner: "#fno-editing", title: "#fno-form-title", newTitle: "New F&O algo", create: "Create F&O algo", error: "#fno-algo-error",
      params: "#fno-params", exits: "#fno-exits" },
  };
  const stockHolding = (a) => Boolean(a.position || a.pending || a.queued || a.protect);
  const canEdit = (a) => !(a.enabled && a.mode === "live");

  function beginAlgoEdit(kind, a, note, unlocked) {
    const cfg = EDIT[kind], form = $(cfg.form);
    algoEdit[kind] = a;
    $(cfg.title).textContent = `Edit algo: ${a.name}`;
    $("button[type=submit]", form).textContent = "Save changes";
    $("[data-create-hint]", form).textContent = "The name cannot change: the profit/loss history is filed under it.";
    form.name.value = a.name; form.name.readOnly = true;
    $$("input, select", form).forEach((el) => {
      const key = el.closest("[data-x]") ? "exits" : el.closest(cfg.params) ? "params" : el.name === "fwd" ? "weekdays" : el.name;
      el.disabled = Boolean(unlocked) && !unlocked.includes(key) && el.name !== "name";
    });
    $(`${cfg.banner} span`).textContent = note;
    $(cfg.banner).hidden = false;
    showError(cfg.error, null);
    if (kind === "stock") { $$("#algo-kind button")[0].click(); } else { $$("#algo-kind button")[1].click(); }
    form.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function endAlgoEdit(kind) {
    const cfg = EDIT[kind], form = $(cfg.form);
    if (!algoEdit[kind]) return;
    algoEdit[kind] = null;
    $(cfg.title).textContent = cfg.newTitle;
    $("button[type=submit]", form).textContent = cfg.create;
    $("[data-create-hint]", form).textContent = "Created stopped. Start it from the list below.";
    form.name.readOnly = false; form.name.value = "";
    $$("input, select", form).forEach((el) => { el.disabled = false; });
    $(cfg.banner).hidden = true;
  }

  function editStockAlgo(a) {
    if (!canEdit(a)) { showError("#algo-error", new Error(`“${a.name}” is trading live. Stop it before editing.`)); return; }
    const form = $("#form-algo");
    form.symbol.value = a.symbol.replace(/\.NS$/, "");
    form.strategy.value = a.strategy; form.strategy.dispatchEvent(new Event("change"));
    Object.entries(a.params || {}).forEach(([k, v]) => { const input = form.elements["p_" + k]; if (input) input.value = v; });
    form.interval.value = a.interval; form.budget.value = a.budget; form.mode.value = a.mode;
    $("#algo-exits").load(a.exits || { unit: "pct", stop_loss: a.stop_loss_pct || 0, target: a.target_pct || 0 });
    const holding = stockHolding(a), gttAtBroker = a.protect && a.mode === "live";
    const note = !holding ? `Editing “${a.name}”. ${a.enabled ? "It keeps running with the new settings; a new stock, strategy or candle waits for a fresh signal." : "It stays stopped."}`
      + " Changing Paper/Live stops it."
      : gttAtBroker ? `“${a.name}” holds ${a.position} shares with its target and stoploss at your broker (GTT ${a.protect}). Only the budget can change now.`
        : `“${a.name}” holds a position, so only the budget and the exit rules can change. Close or clear the position to change the rest.`;
    beginAlgoEdit("stock", a, note, holding ? (gttAtBroker ? ["budget"] : ["budget", "exits"]) : null);
  }

  function editFnoAlgo(a) {
    if (!canEdit(a)) { showError("#fno-algo-error", new Error(`“${a.name}” is trading live. Stop it before editing.`)); return; }
    const form = $("#form-fno"), fire = (el) => el.dispatchEvent(new Event("change"));
    form.kind.value = a.kind; fire(form.kind);
    if (a.structure) { form.structure.value = a.structure; fire(form.structure); }
    if (a.direction) form.direction.value = a.direction;
    form.underlying.value = a.underlying; fire(form.underlying);
    form.expiry_kind.value = a.expiry_kind;
    if (a.kind === "structure") { form.schedule.value = a.schedule; fire(form.schedule); }
    if (a.strategy) { form.strategy.value = a.strategy; fire(form.strategy); }
    Object.entries(a.params || {}).forEach(([k, v]) => { const input = form.elements["p_" + k]; if (input) input.value = v; });
    ["entry_time", "exit_time", "exit_days_before", "min_days", "k", "w", "lots", "max_loss", "target", "mode", "limit_buffer_pct", "margin_pct", "leg_scope"]
      .forEach((key) => { if (a[key] != null && form.elements[key]) form.elements[key].value = a[key]; });
    $$("input[name=fwd]", form).forEach((c) => { c.checked = (a.weekdays || []).includes(Number(c.value)); });
    $("#fno-exits").load(a.leg_exits || { unit: "pct", stop_loss: a.leg_sl_pct || 0 });
    const note = a.book ? `“${a.name}” has open legs, so only the maximum loss, the target, each leg's exit rules and the limit buffer can change. Exit, or clear the legs, to change the rest.`
      : `Editing “${a.name}”. ${a.enabled ? "It keeps running with the new settings." : "It stays stopped."} Changing Paper/Live stops it.`;
    beginAlgoEdit("fno", a, note, a.book ? ["max_loss", "target", "limit_buffer_pct", "leg_scope", "exits"] : null);
  }

  // -- one algo in detail: every leg's buy and sell price, booked and open P&L, orders still out ---------------------

  const detailView = { kind: null, id: null, data: null, timer: null };

  async function openAlgoDetail(kind, id) {
    Object.assign(detailView, { kind, id, data: null });
    const dialog = $("#algo-dialog");
    $("#ad-body").replaceChildren(h("p", { class: "hint", text: "Loading…" }));
    if (!dialog.open) dialog.showModal();
    await loadAlgoDetail();
    clearInterval(detailView.timer);
    detailView.timer = setInterval(() => { if (dialog.open && !document.hidden) loadAlgoDetail(); }, 15000);
  }

  async function loadAlgoDetail() {
    const { kind, id } = detailView;
    let d;
    try { d = await api(`/${kind === "fno" ? "fno-algos" : "algos"}/${id}/detail`); }
    catch (err) { $("#ad-body").replaceChildren(h("p", { class: "error", text: err.message })); return; }
    detailView.data = d;
    const a = d.algo, t = d.totals;
    const money = (v) => (v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + "₹" + inr2.format(Math.abs(v)));
    const price = (v) => (v == null ? "–" : inr2.format(v));
    const blocked = /^Blocked/.test(a.status);
    $("#ad-title").replaceChildren(a.name, " ", h("span", { class: "pill " + (a.mode === "live" ? "bad" : ""), text: a.mode === "live" ? "LIVE" : "Paper" }), " ",
      h("span", { class: "pill " + (blocked ? "warn" : a.enabled ? "good" : ""), text: a.status }));
    $("#ad-edit").hidden = !d.can_edit;
    const what = kind === "fno"
      ? `${a.kind === "futures" ? `${a.underlying} futures` : a.structure_name} on ${a.underlying} (${a.expiry_kind}) · ${a.lots} lot(s)`
      : `${a.strategy_name} on ${a.symbol} · ${state.app.intervals[a.interval]} candles · budget ${rupees(a.budget)}`;
    const tile = (k, v, sub, cls) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (cls || ""), text: v }), sub ? h("div", { class: "s", text: sub }) : null);
    const legs = d.legs, otherMode = (g) => (g.mode !== a.mode ? ` (${g.mode})` : "");
    const status = (g) => h("span", { class: "pill " + (g.status === "Active" ? "good" : ""), text: g.status });
    $("#ad-body").replaceChildren(...[
      h("p", { class: "hint", text: what + (d.can_edit ? "" : " · Stop it to edit.") }),
      h("div", { class: "tiles" },
        tile("Realised P&L (booked)", money(t.realized), `${t.closed} closed, after charges`, tone(t.realized)),
        tile("Unrealised P&L (open)", money(t.unrealized), t.unrealized == null ? "no live price for a held leg" : `${t.held} still held, at current price`, tone(t.unrealized)),
        tile("Total", money(t.total), "realised + unrealised", tone(t.total)),
        tile("Orders", d.orders.length ? `${d.orders.length} active` : "None active", d.orders.length ? "waiting at the broker or for the trade window" : "every order is closed")),
      h("h3", { text: kind === "fno" ? "Each leg" : "Each stock" }),
      h("div", { class: "scroll" }, table([
        { label: "Stock / contract", get: (g) => g.symbol + otherMode(g) }, { label: "Status", get: status },
        { label: "Bought", num: true, get: (g) => g.bought || "–" }, { label: "Buy price ₹", num: true, get: (g) => price(g.buy_price) },
        { label: "Sold", num: true, get: (g) => g.sold || "–" }, { label: "Sell price ₹", num: true, get: (g) => price(g.sell_price) },
        { label: "Open qty", num: true, get: (g) => (g.open_quantity ? `${Math.abs(g.open_quantity)} ${g.open_quantity < 0 ? "sold" : "bought"}` : "–") },
        { label: "Current price ₹", num: true, get: (g) => (g.status === "Active" ? price(g.last) : "–") },
        { label: "Realised P&L", num: true, get: (g) => money(g.realized), tone: (g) => tone(g.realized) },
        { label: "Unrealised P&L", num: true, get: (g) => (g.status === "Active" ? money(g.unrealized) : "–"), tone: (g) => tone(g.unrealized) }],
        legs, "Nothing bought or sold yet.")),
      d.orders.length ? h("h3", { text: "Orders still active" }) : null,
      d.orders.length ? h("div", { class: "scroll" }, table([
        { label: "Stock / contract", get: (o) => o.symbol }, { label: "Buy / Sell", get: (o) => (o.side === "BUY" ? "Buy" : "Sell"), tone: (o) => (o.side === "BUY" ? "pos" : "neg") },
        { label: "Qty", num: true, get: (o) => o.quantity ?? "–" }, { label: "Price ₹", num: true, get: (o) => price(o.price) },
        { label: "Status", get: (o) => o.status }, { label: "Order", get: (o) => o.order_id || "–" }, { label: "Sent (IST)", get: (o) => o.time_ist || "–" }], d.orders, "")) : null,
      h("details", {}, h("summary", { text: "Every buy and sell (fills)" }),
        h("div", { class: "scroll" }, table([
          { label: "Time (IST)", get: (f) => f.time_ist }, { label: "Stock / contract", get: (f) => f.symbol },
          { label: "Buy / Sell", get: (f) => (f.side === "BUY" ? "Buy" : "Sell"), tone: (f) => (f.side === "BUY" ? "pos" : "neg") },
          { label: "Qty", num: true, get: (f) => f.quantity }, { label: "Price ₹", num: true, get: (f) => price(f.price) },
          { label: "Charges ₹", num: true, get: (f) => price(f.charges) }],
          legs.flatMap((g) => g.fills.map((f) => ({ ...f, symbol: g.symbol + otherMode(g) }))).sort((x, y) => y.time_ist.localeCompare(x.time_ist)),
          "No fills yet."))),
      h("details", {}, h("summary", { text: "Activity" }),
        h("pre", { class: "log", text: a.log.slice().reverse().map((e) => `${e.time_ist}  ${e.message}`).join("\n") })),
      h("p", { class: "hint", text: "Realised P&L is after charges. Unrealised P&L is at the current price, before the charges of closing. "
        + (a.mode === "live" ? "Live fills are read from your broker's trade book for orders this desk placed; your broker's console is the final word." : "") })]
      .filter(Boolean));
  }

  $("#ad-close").addEventListener("click", () => $("#algo-dialog").close());
  $("#ad-refresh").addEventListener("click", loadAlgoDetail);
  $("#algo-dialog").addEventListener("close", () => { clearInterval(detailView.timer); detailView.timer = null; });
  $("#ad-edit").addEventListener("click", () => {
    const d = detailView.data; if (!d) return;
    $("#algo-dialog").close();
    if (detailView.kind === "fno") editFnoAlgo(d.algo); else editStockAlgo(d.algo);
  });

  async function algoAction(algo, action, button) {
    showError("#algo-error", null);
    let body;
    if (action === "start" && algo.mode === "live") {
      const { phrase } = await api(`/algos/${algo.id}/phrase`);
      const typed = window.prompt(`This algo will place LIVE orders with real money, by itself, up to ₹${inr.format(algo.budget)}.\n\nType  ${phrase}  to start it.`);
      if (typed == null) return;
      body = { phrase: typed };
    }
    if (action === "delete" && !window.confirm(`Delete the algo “${algo.name}”?`)) return;
    if (action === "clear" && !window.confirm(`Mark “${algo.name}” as holding nothing? No order is sent; do this only after you have dealt with its ${algo.position} shares yourself.`)) return;
    busy(button, true, "…");
    try {
      if (action === "delete") await api(`/algos/${algo.id}`, { method: "DELETE" });
      else await api(`/algos/${algo.id}/${action}`, { method: "POST", body: body || {} });
    } catch (err) { showError("#algo-error", err); }
    await refreshAlgos();
  }

  async function refreshAlgos() {
    let list;
    try { list = (await api("/algos")).algos; } catch (err) { showError("#algo-error", err); return; }
    const host = $("#algo-list");
    if (!list.length) { host.replaceChildren(h("p", { class: "hint", text: "No algos yet. Create one above." })); return; }
    host.replaceChildren(...list.map((a) => {
      const act = (label, action, cls) => h("button", { class: cls || null, type: "button", text: label, onclick: (e) => algoAction(a, action, e.target) });
      const blocked = /^Blocked/.test(a.status);
      const signal = a.last_signal ? `${a.last_signal.target ? "buy" : "sell / stay out"} on ${a.last_signal.bar} (close ${rupees(a.last_signal.close, 2)})` : "not checked yet";
      return h("div", { class: "algo" },
        h("div", { class: "head" },
          h("span", {}, h("button", { type: "button", class: "name", text: a.name, title: "Buy and sell prices, P&L and orders", onclick: () => openAlgoDetail("stock", a.id) }), " ",
            h("span", { class: "pill " + (a.mode === "live" ? "bad" : ""), text: a.mode === "live" ? "LIVE" : "Paper" }), " ",
            h("span", { class: "pill " + (blocked ? "warn" : a.enabled ? "good" : ""), text: a.status })),
          h("span", { class: "actions" },
            h("button", { type: "button", text: "Details", onclick: () => openAlgoDetail("stock", a.id) }),
            canEdit(a) ? h("button", { type: "button", text: "Edit", onclick: () => editStockAlgo(a) }) : null,
            a.enabled ? act("Stop", "stop") : act("Start", "start", a.mode === "live" ? "danger" : "primary"),
            act("Run now", "run"), !a.enabled && a.position ? act("Clear position", "clear") : null, !a.enabled ? act("Delete", "delete") : null)),
        h("dl", { class: "kv" },
          h("dt", { text: "Strategy" }), h("dd", { text: `${a.strategy_name} on ${a.symbol}` + (Object.keys(a.params).length ? ` · ${Object.entries(a.params).map(([k, v]) => `${k} ${v}`).join(", ")}` : "") }),
          h("dt", { text: "Trades" }), h("dd", { text: `${state.app.intervals[a.interval]} candles · ${a.product === "MIS" ? "intraday (MIS)" : "delivery (CNC)"}` }),
          h("dt", { text: "Exit rules" }), h("dd", { text: a.exits_text }),
          h("dt", { text: "Budget" }), h("dd", { text: rupees(a.budget) }),
          a.queued ? h("dt", { text: "Queued" }) : null, a.queued ? h("dd", { text: `${a.queued.side} (${a.queued.reason}), waiting for the trade window` }) : null,
          a.protect ? h("dt", { text: "Protected by" }) : null, a.protect ? h("dd", { text: `GTT ${a.protect}` }) : null,
          h("dt", { text: "Position" }), h("dd", { text: a.position ? `${a.position} shares at ${rupees(a.average_price, 2)}` : "flat" }),
          h("dt", { text: "Last signal" }), h("dd", { text: signal }),
          a.pending ? h("dt", { text: "Pending order" }) : null,
          a.pending ? h("dd", { text: `${a.pending.side} ${a.pending.quantity} at ${rupees(a.pending.price, 2)}, order ${a.pending.order_id}, sent ${a.pending.placed}` }) : null),
        h("details", {}, h("summary", { text: "Activity" }),
          h("pre", { class: "log", text: a.log.slice().reverse().map((e) => `${e.time_ist}  ${e.message}`).join("\n") })));
    }));
  }



  // -- F&O algos: option strategies and index futures ---------------------------------

  $$("#algo-kind button").forEach((b) => b.addEventListener("click", () => {
    $$("#algo-kind button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
    $("#algo-stocks").hidden = b.dataset.kind !== "stocks";
    $("#algo-fno").hidden = b.dataset.kind !== "fno";
    if (b.dataset.kind === "fno") refreshFnoAlgos();
  }));

  function fnoBody(form) {
    const num = (name) => Number(form[name].value || 0);
    const params = {};
    $$("#fno-params input").forEach((input) => { params[input.name.slice(2)] = Number(input.value); });
    return { name: form.name.value, kind: form.kind.value, structure: form.structure.value, direction: form.direction.value,
      underlying: form.underlying.value, expiry_kind: form.kind.value === "futures" ? "monthly" : form.expiry_kind.value,
      schedule: form.kind.value === "futures" ? "signal" : form.schedule.value, entry_time: form.entry_time.value, exit_time: form.exit_time.value,
      exit_days_before: num("exit_days_before"), min_days: num("min_days"), strategy: form.strategy.value, strategy_params: params,
      weekdays: $$("input[name=fwd]", form).filter((c) => c.checked).map((c) => Number(c.value)),
      k: num("k"), w: num("w"), lots: num("lots"), leg_exits: $("#fno-exits").read(), leg_scope: form.leg_scope.value, max_loss: num("max_loss"), target: num("target"),
      limit_buffer_pct: num("limit_buffer_pct"), margin_pct: num("margin_pct"), mode: form.mode.value };
  }

  function startFnoAlgos() {
    const form = $("#form-fno"), list = state.app.structures;
    $("#fno-exits").replaceWith(Object.assign(exitRulesBox(true), { id: "fno-exits" }));
    form.structure.replaceChildren(...[["sideways", "Neutral: sell premium"], ["volatile", "Big move either way: buy premium"], ["bull", "Bullish"], ["bear", "Bearish"]].map(([key, label]) =>
      h("optgroup", { label }, list.filter((x) => (key === "volatile" ? x.markets.includes("bull") && x.markets.includes("bear") && !x.bias
        : key === "sideways" ? !x.bias && x.markets.includes("sideways") : x.bias === key)).map((x) => h("option", { value: x.key, text: x.name })))));
    form.structure.value = "iron_condor";
    strategyOptions(form.strategy, (x) => !x.intraday_only && x.key !== "buy_hold"); form.strategy.value = "supertrend";
    const params = () => {
      const strategy = state.app.strategies.find((x) => x.key === form.strategy.value);
      $("#fno-params").replaceChildren(...(strategy && !$("#fno-params").hidden ? strategy.params : []).map((p) => h("label", {}, p.label,
        h("input", { name: "p_" + p.name, type: "number", min: p.minimum, max: p.maximum, step: p.integer ? 1 : 0.1, value: p.default, required: true }))));
    };
    const sync = () => {
      const futures = form.kind.value === "futures", x = list.find((y) => y.key === form.structure.value);
      if (!futures && form.schedule.value === "signal" && !x.bias) form.schedule.value = "positional";
      [...form.schedule.options].find((o) => o.value === "signal").disabled = !x.bias;
      const schedule = futures ? "signal" : form.schedule.value;
      $$(".fno-structure", form).forEach((el) => { el.hidden = futures; });
      $$(".fno-futures", form).forEach((el) => { el.hidden = !futures; });
      $$(".fno-signal", form).forEach((el) => { el.hidden = schedule !== "signal"; });
      $$(".fno-positional", form).forEach((el) => { el.hidden = schedule !== "positional"; });
      $$(".fno-days", form).forEach((el) => { el.hidden = schedule === "signal"; });
      $("#fno-params").hidden = schedule !== "signal";
      $$(".fno-k", form).forEach((el) => { el.hidden = futures || !x.uses_k; });
      $$(".fno-w", form).forEach((el) => { el.hidden = futures || !x.uses_w; });
      form.k.min = x.min_k || 0; if (Number(form.k.value) < (x.min_k || 0)) form.k.value = x.min_k;
      $("#fno-desc").replaceChildren(futures
        ? h("span", { text: "Holds the near-month future while the strategy (on the index's day candles, checked at the entry time) says buy, sell, or either; moves to the next month's contract at the exit time on expiry day." })
        : h("span", {}, x.about, " ", marketBadges(x.markets)));
      params();
    };
    form.kind.addEventListener("change", sync); form.schedule.addEventListener("change", sync); form.strategy.addEventListener("change", params);
    form.structure.addEventListener("change", () => { const x = list.find((y) => y.key === form.structure.value); form.w.value = x.w; sync(); });
    const syncUnderlying = () => {
      const option = form.underlying.selectedOptions[0];
      const metal = Boolean(option && option.parentElement.label && option.parentElement.label.startsWith("MCX"));
      if (!["NIFTY", "SENSEX"].includes(form.underlying.value)) form.expiry_kind.value = "monthly";
      [...form.expiry_kind.options].find((o) => o.value === "weekly").disabled = !["NIFTY", "SENSEX"].includes(form.underlying.value);
      form.entry_time.title = form.exit_time.title = metal ? "MCX: between 09:00 and 23:25 IST" : "NSE/BSE: between 09:15 and 15:29 IST";
      if (metal && form.exit_time.value === "15:10") form.exit_time.value = "23:00";
      if (!metal && form.exit_time.value > "15:29") form.exit_time.value = "15:10";
    };
    form.underlying.addEventListener("change", syncUnderlying); syncUnderlying();
    sync();
    $("#fno-preview").addEventListener("click", async (event) => {
      const button = event.target, box = $("#fno-preview-result");
      showError("#fno-algo-error", null); busy(button, true, "Working…");
      try {
        const r = await api("/fno-algos/preview", { method: "POST", body: { ...fnoBody(form), name: form.name.value || "Preview" } });
        box.replaceChildren(h("h3", { text: `If it entered now: ${form.underlying.value} ${rupees(r.spot, 2)}, ATM ${r.atm}` }),
          h("div", { class: "scroll" }, h("table", {}, h("thead", {}, h("tr", {}, ["#", "Side", "Contract", "Expiry", "Lots", "Qty", "Last"].map((t) => h("th", { text: t })))),
            h("tbody", {}, r.legs.map((g) => h("tr", {}, h("td", { text: g.order }), h("td", { class: g.side === "BUY" ? "up" : "down", text: g.side }),
              h("td", { text: g.tradingsymbol }), h("td", { text: g.expiry }), h("td", { class: "num", text: g.lots }), h("td", { class: "num", text: g.quantity }),
              h("td", { class: "num", text: rupees(g.ltp, 2) })))))),
          h("p", {}, `Premium ${r.net_premium >= 0 ? "received" : "paid"}: ${rupees(Math.abs(r.net_premium))}. Margin about ${rupees(r.margin)}`,
            r.expiry_day_margin ? " (with the 2% expiry-day extra)" : "", ` against ${rupees(r.funds)} available.`, r.margin > r.funds ? " Not enough: the entry would be skipped." : ""),
          ...(r.naked ? [h("p", { class: "error", text: "Naked short: a sold option has no bought option covering it. The loss is unlimited or very large." })] : []),
          ...r.notes.map((n) => h("p", { class: "hint", text: n })));
        box.hidden = false;
      } catch (err) { showError("#fno-algo-error", err); box.hidden = true; }
      busy(button, false);
    });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const editing = algoEdit.fno;
      const button = $("button[type=submit]", form); showError("#fno-algo-error", null); busy(button, true, editing ? "Saving…" : "Creating…");
      let saved = false;
      try {
        if (editing) await api(`/fno-algos/${editing.id}`, { method: "PUT", body: fnoBody(form) });
        else await api("/fno-algos", { method: "POST", body: fnoBody(form) });
        saved = true;
      } catch (err) { showError("#fno-algo-error", err); }
      busy(button, false);
      if (saved) { endAlgoEdit("fno"); await refreshFnoAlgos(); }
    });
    $("#fno-edit-cancel").addEventListener("click", () => endAlgoEdit("fno"));
    $("#fno-algo-refresh").addEventListener("click", refreshFnoAlgos);
  }

  async function fnoAction(algo, action, button) {
    showError("#fno-algo-error", null);
    let body = {};
    if (action === "start" && algo.mode === "live") {
      const { phrase } = await api(`/fno-algos/${algo.id}/phrase`);
      const typed = window.prompt(`This algo will place LIVE F&O orders with real money, by itself: ${algo.structure_name}, ${algo.lots} lot(s) on ${algo.underlying}, closed at a loss of ${rupees(algo.max_loss)}.\n\nType  ${phrase}  to start it.`);
      if (typed == null) return;
      body = { phrase: typed };
    }
    if (action === "exit" && !window.confirm(`Close every leg of “${algo.name}” now and stop it?`)) return;
    if (action === "stop" && algo.book) {
      if (!window.confirm(`“${algo.name}” holds open legs. Stopping leaves them open and unwatched: no stop, no exit. Use Exit to close them instead.\n\nStop anyway?`)) return;
      body = { force: true };
    }
    if (action === "delete" && !window.confirm(`Delete the F&O algo “${algo.name}”?`)) return;
    if (action === "clear" && !window.confirm(`Mark “${algo.name}” as holding nothing? No order is sent; do this only after closing its legs yourself.`)) return;
    busy(button, true, "…");
    try {
      if (action === "delete") await api(`/fno-algos/${algo.id}`, { method: "DELETE" });
      else await api(`/fno-algos/${algo.id}/${action}`, { method: "POST", body });
    } catch (err) { showError("#fno-algo-error", err); }
    await refreshFnoAlgos();
  }

  const fnoRupees = (v) => (v == null ? "–" : (v < 0 ? "−" : "") + "₹" + inr.format(Math.abs(Math.round(v))));

  async function refreshFnoAlgos() {
    let data;
    try { data = await api("/fno-algos"); } catch (err) { showError("#fno-algo-error", err); return; }
    const host = $("#fno-list");
    const notes = [];
    if (!data.kite_connected) notes.push(h("p", { class: "hint warn", text: "Kite is not connected: F&O algos, paper and live, need Kite for option prices. Log in on the Trade tab." }));
    if (!data.algos.length) { host.replaceChildren(...notes, h("p", { class: "hint", text: "No F&O algos yet. Create one above." })); return; }
    host.replaceChildren(...notes, ...data.algos.map((a) => {
      const act = (label, action, cls) => h("button", { class: cls || null, type: "button", text: label, onclick: (e) => fnoAction(a, action, e.target) });
      const blocked = /^Blocked/.test(a.status), book = a.book;
      const how = { intraday: `intraday ${a.entry_time}–${a.exit_time}`, positional: `positional from ${a.entry_time}, out ${a.exit_days_before} day(s) before expiry at ${a.exit_time}`, signal: `on ${a.strategy_name} (day candles), read at ${a.entry_time}` }[a.schedule];
      const risk = [a.max_loss ? `maximum loss ${rupees(a.max_loss)}` : "no maximum loss", a.target ? `target ${rupees(a.target)}` : null,
        a.leg_exits_text ? `each ${a.leg_scope === "all" ? "" : a.leg_scope + " "}leg: ${a.leg_exits_text}` : null].filter(Boolean).join(" · ");
      return h("div", { class: "algo" },
        h("div", { class: "head" },
          h("span", {}, h("button", { type: "button", class: "name", text: a.name, title: "Each leg's buy and sell prices, P&L and orders", onclick: () => openAlgoDetail("fno", a.id) }), " ",
            h("span", { class: "pill " + (a.mode === "live" ? "bad" : ""), text: a.mode === "live" ? "LIVE" : "Paper" }), " ",
            a.naked ? h("span", { class: "pill warn", text: "naked short" }) : null, " ",
            h("span", { class: "pill " + (blocked ? "warn" : a.enabled ? "good" : ""), text: a.status })),
          h("span", { class: "actions" },
            h("button", { type: "button", text: "Details", onclick: () => openAlgoDetail("fno", a.id) }),
            canEdit(a) && !a.exit_requested ? h("button", { type: "button", text: "Edit", onclick: () => editFnoAlgo(a) }) : null,
            a.enabled ? act("Stop", "stop") : act("Start", "start", a.mode === "live" ? "danger" : "primary"),
            a.enabled ? act("Run now", "run") : null, book ? act("Exit", "exit", "danger") : null,
            !a.enabled && book ? act("Clear legs", "clear") : null, !a.enabled && !book ? act("Delete", "delete") : null)),
        h("dl", { class: "kv" },
          h("dt", { text: "Trades" }), h("dd", { text: `${a.kind === "futures" ? `${a.underlying} near-month futures, ${{ long: "buy only", short: "sell only", both: "buy and sell" }[a.direction] || a.direction}` : `${a.structure_name} · ${a.underlying} ${a.expiry_kind}`} · ${a.lots} lot(s) · ${how}` }),
          h("dt", { text: "Exit rules" }), h("dd", { text: risk }),
          h("dt", { text: "Position" }), h("dd", { text: book ? `${book.phase}${book.pnl != null ? ` · P&L ${fnoRupees(book.pnl)}` : ""} · margin about ${rupees(book.margin)} · since ${book.opened}` : "flat" }),
          h("dt", { text: "Realised" }), h("dd", { text: `${fnoRupees(a.realized)} over ${a.trades} trade(s), after charges` })),
        book ? h("div", { class: "scroll" }, h("table", {},
          h("thead", {}, h("tr", {}, ["Leg", "Expiry", "Qty", "Held", "Avg", "Pending"].map((t) => h("th", { text: t })))),
          h("tbody", {}, book.legs.map((g) => h("tr", {}, h("td", { class: g.action > 0 ? "up" : "down", text: `${g.action > 0 ? "BUY" : "SELL"} ${g.tradingsymbol}` }),
            h("td", { text: g.expiry }), h("td", { class: "num", text: g.quantity }), h("td", { class: "num", text: g.open_qty + (g.stopped ? " (stoploss hit)" : "") + (g.reentry ? " (re-entry)" : "") }),
            h("td", { class: "num", text: g.avg ? rupees(g.avg, 2) : "–" }),
            h("td", { text: g.pending.map((p) => `${p.side} ${p.quantity} @ ${p.price} (#${p.order_id})`).join(", ") || "–" })))))) : null,
        h("details", {}, h("summary", { text: "Activity" }),
          h("pre", { class: "log", text: a.log.slice().reverse().map((e) => `${e.time_ist}  ${e.message}`).join("\n") })));
    }));
  }

  // -- markets: sector cards -----------------------------------------------------------

  const board = { cards: [], filter: "all", sort: "change_1d", open: null, chart: null, detail: null };
  const TREND = { Uptrend: ["up", "▲"], Downtrend: ["down", "▼"], Sideways: ["", "◆"] };

  function trendBadge(trend) {
    const [cls, mark] = TREND[trend] || ["", "…"];
    return h("span", { class: "trend " + cls }, mark + " " + trend);
  }

  const signed = (v) => h("b", { class: tone(v), text: v == null ? "–" : pct(v) });

  function renderBoard() {
    const loaded = board.cards.filter((c) => c.candles && c.candles.length > 1);
    const up = loaded.filter((c) => c.trend === "Uptrend").length, down = loaded.filter((c) => c.trend === "Downtrend").length;
    const best = loaded.slice().sort((a, b) => b.change_1d - a.change_1d);
    const tile = (k, v, sub, cls) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (cls || ""), text: v }), h("div", { class: "s", text: sub }));
    $("#sector-summary").replaceChildren(...(loaded.length ? [
      tile("Sectors in uptrend", `${up} of ${loaded.length}`, `${down} in downtrend`),
      tile("Advancing today", String(loaded.filter((c) => c.change_1d > 0).length), `${loaded.filter((c) => c.change_1d < 0).length} declining`),
      tile("Strongest today", pct(best[0].change_1d), best[0].name, tone(best[0].change_1d)),
      tile("Weakest today", pct(best[best.length - 1].change_1d), best[best.length - 1].name, tone(best[best.length - 1].change_1d)),
    ] : []));
    let list = board.cards.filter((c) => board.filter === "all" || c.trend === board.filter);
    list = list.slice().sort((a, b) => (board.sort === "name" ? a.name.localeCompare(b.name) : (b[board.sort] ?? -1e9) - (a[board.sort] ?? -1e9)));
    $("#sector-cards").replaceChildren(...(list.length ? list.map((c) => {
      const ready = c.candles && c.candles.length > 1;
      return h("button", { type: "button", class: "card", onclick: () => openSector(c.id), "aria-label": `${c.name}: ${c.trend}` },
        h("div", { class: "name" }, h("span", { text: c.name }), trendBadge(c.trend)),
        ready ? h("div", { class: "move" }, signed(c.change_1d), h("span", { text: "today · index " + c.value.toFixed(1) })) : h("div", { class: "meta", text: "Loading prices…" }),
        miniCandles(c.candles),
        ready ? h("div", { class: "foot" }, h("span", {}, "1 week", signed(c.change_1w)), h("span", {}, "1 month", signed(c.change_1m)), h("span", {}, "3 months", signed(c.change_3m))) : null,
        h("div", { class: "meta", text: `${c.priced} of ${c.stocks} stocks priced` + (c.breadth != null ? ` · ${c.breadth}% above their 50-day average` : "") }));
    }) : [h("p", { class: "hint", text: "No sector matches this filter." })]));
  }

  async function refreshSectors() {
    let data;
    try { data = await api("/sectors"); } catch (err) { showError("#sector-error", err); return; }
    showError("#sector-error", null);
    board.cards = data.sectors; board.trendText = data.trend_text;
    const progress = $("#sector-progress");
    progress.hidden = !data.progress.running;
    progress.textContent = `Loading price history: ${data.progress.done} of ${data.progress.total} stocks. Cards fill in as it goes; the first load takes several minutes.`;
    $("#sector-updated").textContent = `${data.sectors.length} sectors · ${data.updated_ist ? `prices as of ${data.updated_ist} IST (${data.live_source})` : "last closing prices"} · ${data.session_open ? "refreshing automatically" : "market closed"}`;
    if (board.open) { if (!board.editing) loadSector(board.open, false); } else renderBoard();
  }

  function openSector(id) {
    board.open = id; board.editing = false;
    $("#sector-board").hidden = true; $("#sector-detail").hidden = false;
    window.scrollTo({ top: 0 });
    loadSector(id, true);
  }

  function closeSector() {
    board.open = null; board.detail = null;
    if (board.chart) { board.chart.remove(); board.chart = null; }
    $("#sector-detail").hidden = true; $("#sector-board").hidden = false;
    refreshSectors();
  }

  async function loadSector(id, redraw) {
    let d;
    try { d = await api("/sectors/" + encodeURIComponent(id)); } catch (err) { showError("#sector-detail-error", err); return; }
    board.detail = d;
    $("#sector-title").textContent = d.name;
    $("#sector-trend").replaceWith(Object.assign(trendBadge(d.trend), { id: "sector-trend" }));
    $("#sector-detail-updated").textContent = d.updated_ist ? `Prices as of ${d.updated_ist} IST (${d.live_source})` : "Last closing prices";
    const tile = (k, v) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + tone(v), text: v == null ? "–" : pct(v) }));
    $("#sector-tiles").replaceChildren(tile("Today", d.change_1d), tile("1 week", d.change_1w), tile("1 month", d.change_1m), tile("3 months", d.change_3m), tile("1 year", d.change_1y));
    if (redraw || !board.chart) {
      if (board.chart) board.chart.remove();
      board.chart = LightweightCharts.createChart($("#sector-chart"), { ...chartOptions(360), timeScale: { borderColor: cssVar("--line"), timeVisible: false } });
      board.series = candleSeries(board.chart);
      board.series.createPriceLine({ price: 100, color: cssVar("--muted"), lineWidth: 1, lineStyle: LightweightCharts.LineStyle.Dotted, title: "start" });
      board.series.setData(d.candles); board.chart.timeScale().fitContent();
    } else board.series.setData(d.candles);
    renderSectorStocks(d);
  }

  function renderSectorStocks(d) {
    const cell = (v, cls) => h("td", { class: "num " + (cls || ""), text: v });
    const move = (v) => cell(v == null ? "–" : pct(v), tone(v));
    $("#sector-stocks").replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Stock", "Price ₹", "Today", "1 month", "3 months", "1 year", "52-week range ₹", "Mkt cap", "P/E", "ROE %", "Trend", "Contribution", "Weight", "Share", ""]
        .map((t, i) => h("th", { class: i > 0 && i < 14 && i !== 10 ? "num" : null, text: t, title: t === "Contribution" ? "Points this stock adds to the sector's move today" : null })))),
      h("tbody", {}, d.stocks.map((r) => {
        const name = r.symbol.replace(/\.(NS|BO)$/, "");
        const weight = h("input", { type: "number", min: 0, max: 1000, step: "any", value: r.weight, "data-symbol": r.symbol, "aria-label": `Weight of ${name}` });
        weight.addEventListener("input", () => { board.editing = true; });
        return h("tr", {},
          h("td", {}, h("button", { type: "button", class: "linkish", text: name, title: "Details, chart and fundamentals", onclick: () => openStock(r.symbol) })),
          ...(r.priced ? [cell(inr2.format(r.price)), move(r.change_1d), move(r.change_1m), move(r.change_3m), move(r.change_1y),
            cell(`${inr2.format(r.low_52w)} – ${inr2.format(r.high_52w)}`), cell(crore(r.market_cap)), cell(num(r.pe, 1)), cell(num(r.roe, 1)), h("td", {}, trendBadge(r.trend)),
            cell(r.contribution_1d == null ? "–" : (r.contribution_1d > 0 ? "+" : "") + r.contribution_1d.toFixed(2) + " pts", tone(r.contribution_1d))]
            : [h("td", { colspan: 11, class: "hint", text: r.note || "Price history is still loading." })]),
          h("td", { class: "num" }, weight), cell(r.priced ? r.weight_pct.toFixed(1) + "%" : "–"),
          h("td", {}, h("button", { type: "button", text: "Remove", onclick: () => saveSector((stocks) => { delete stocks[r.symbol]; }) })));
      }))));
  }

  function currentWeights() {
    const stocks = {};
    $$("#sector-stocks input[data-symbol]").forEach((input) => { stocks[input.dataset.symbol] = Number(input.value || 0); });
    return stocks;
  }

  async function saveSector(change, name) {
    showError("#sector-detail-error", null);
    const stocks = currentWeights();
    if (change) change(stocks);
    try {
      board.detail = await api("/sectors/" + encodeURIComponent(board.open), { method: "PUT", body: { name: name || board.detail.name, stocks } });
      board.editing = false;
      await loadSector(board.open, true);
    } catch (err) { showError("#sector-detail-error", err); }
  }

  function startSectors() {
    $$("#sector-filter button").forEach((b) => b.addEventListener("click", () => {
      board.filter = b.dataset.filter;
      $$("#sector-filter button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); renderBoard();
    }));
    $("#sector-sort").addEventListener("change", (event) => { board.sort = event.target.value; renderBoard(); });
    $("#sector-back").addEventListener("click", closeSector);
    $("#sector-save").addEventListener("click", () => saveSector());
    $("#sector-equal").addEventListener("click", () => saveSector((stocks) => { Object.keys(stocks).forEach((k) => { stocks[k] = 1; }); }));
    $("#form-sector-add").addEventListener("submit", (event) => {
      event.preventDefault();
      const input = event.target.symbols, added = input.value.split(/[\s,]+/).filter(Boolean);
      if (!added.length) return;
      saveSector((stocks) => { added.forEach((sym) => { stocks[sym] = 1; }); }).then(() => { input.value = ""; });
    });
    $("#sector-rename").addEventListener("click", () => {
      const name = window.prompt("New name for this sector:", board.detail.name);
      if (name) saveSector(null, name);
    });
    $("#sector-delete").addEventListener("click", async () => {
      if (!window.confirm(`Delete the sector “${board.detail.name}”? Its stocks and weights are removed from the board.`)) return;
      try { await api("/sectors/" + encodeURIComponent(board.open), { method: "DELETE" }); closeSector(); } catch (err) { showError("#sector-detail-error", err); }
    });
    $("#sector-new").addEventListener("click", async () => {
      const name = window.prompt("Name of the new sector (for example: Drones):");
      if (!name) return;
      const stocks = window.prompt(`NSE symbols for “${name}”, separated by spaces:`);
      if (!stocks) return;
      try { const made = await api("/sectors", { method: "POST", body: { name, stocks } }); await refreshSectors(); openSector(made.id); }
      catch (err) { showError("#sector-error", err); }
    });
    new MutationObserver(() => { if (board.open) loadSector(board.open, true); })
      .observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    refreshSectors();
    setInterval(() => { if (!$("#tab-markets").hidden && !document.hidden) refreshSectors(); }, 30000);
  }

  $("#llm-test").addEventListener("click", async (event) => {
    const out = $("#llm-test-out"); busy(event.target, true, "Testing…");
    try {
      const r = await api("/llm/test", { method: "POST" });
      if (r.error) out.replaceChildren(h("div", { class: "error", text: r.error }));
      else out.replaceChildren(h("p", { class: "hint", text: `Service: ${r.endpoint}` }), ...["quick", "deep"].map((tier) => {
        const t = r[tier];
        return h("div", { class: t.ok ? "okbox" : "error", text: t.ok ? `${tier} model “${t.model}” answered: ${t.reply}` : `${tier} model “${t.model}” failed.\n${t.hint}\n${t.error}` });
      }));
    } catch (err) { out.replaceChildren(h("div", { class: "error", text: err.message })); }
    busy(event.target, false);
  });


  // -- P&L and alerts ------------------------------------------------------------------------

  const pnlView = { chart: null, series: null };

  async function refreshFunds() {
    const mode = state.mode;
    let f;
    try { f = await api("/funds?mode=" + mode); }
    catch (err) { showError("#funds-error", err); return; }
    if (mode !== state.mode) return;                        // switched Paper / Live while this was loading
    showError("#funds-error", null);
    const tile = (k, v, sub, cls) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (cls || ""), text: v }), sub ? h("div", { class: "s", text: sub }) : null);
    const cash = (v) => (v == null ? "–" : rupees(v, 2));
    const signed = (v) => (v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + "\u2060₹" + inr2.format(Math.abs(v)));   // word joiner: the sign stays with the number
    const tiles = [], note = [];
    if (mode === "paper") {
      $("#funds-title").textContent = "Paper account funds";
      $("#funds-source").textContent = "practice money";
      const share = f.capital ? (f.change / f.capital) * 100 : 0;
      const shareText = Math.abs(share) < 0.005 ? "0.00%" : `${share > 0 ? "+" : "−"}${Math.abs(share).toFixed(2)}%`;
      tiles.push(h("div", { class: "tiles" },
        tile("Starting capital", rupees(f.capital), "set in Settings, or on reset"),
        tile("Cash available", cash(f.cash), "free to buy with"),
        tile("Invested", cash(f.invested), `${f.positions} ${f.positions === 1 ? "position" : "positions"}, at buy price`),
        tile("Account value", cash(f.value), "cash + what you hold, at current price"),
        tile("Change since start", signed(f.change), `${shareText} of starting capital`, tone(f.change))));
      if (f.unpriced.length) note.push(`No current price for ${f.unpriced.join(", ")}: valued at buy price.`);
      note.push("Reset the paper account on the Trade tab.");
    } else {
      $("#funds-title").textContent = "Live account funds";
      const broker = state.app.kite.broker_label || "your broker";
      $("#funds-source").textContent = `real money, from ${broker}`;
      if (!f.connected) note.push(`${broker} is not connected. Connect it in Settings to see your funds here.`);
      else if (f.needs_login) note.push(`${broker} needs a login. Log in on the Trade tab; sessions expire every day.`);
      else if (f.error) showError("#funds-error", new Error(f.error));
      for (const s of f.segments) {
        tiles.push(h("h3", { text: s.segment === "commodity" ? "Commodity (MCX)" : "Equity and F&O" }), h("div", { class: "tiles" },
          tile("Available to trade", cash(s.net), "after margin in use"),
          tile("Used margin", cash(s.used), "blocked by open orders and positions"),
          tile("Cash", cash(s.cash), s.opening_balance == null ? null
            : `opening balance ${cash(s.opening_balance)}${s.added_today ? `, added today ${cash(s.added_today)}` : ""}`),
          s.collateral ? tile("Collateral", cash(s.collateral), "margin from pledged holdings") : null));
      }
      if (f.connected && !f.needs_login && !f.error && !f.segments.length) note.push(`${broker} returned no funds for this account.`);
    }
    $("#funds-tiles").replaceChildren(...tiles);
    $("#funds-note").textContent = note.join(" ");
  }

  async function refreshPnl() {
    refreshTax(); refreshJournalPanel(); refreshFunds();
    let r, feed;
    try { [r, feed] = await Promise.all([api("/pnl?mode=" + state.mode), api("/alerts")]); }
    catch (err) { showError("#pnl-error", err); return; }
    showError("#pnl-error", null);
    const live = state.mode === "live";
    $("#pnl-about").textContent = `${live ? "LIVE account (real money)" : "Paper account (practice money)"} · switch with Paper / Live at the top`;
    const tile = (k, v, sub, cls) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (cls || ""), text: v }), sub ? h("div", { class: "s", text: sub }) : null);
    const money = (v) => (v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + "₹" + inr.format(Math.abs(v)));
    const plain = (v) => "₹" + inr.format(Math.abs(v || 0));
    const word = (v) => (v > 0 ? "profit" : v < 0 ? "loss" : "profit/loss");
    const total = r.realized + r.unrealized;
    const plural = (n, one) => `${n} ${one}${n === 1 ? "" : "s"}`;
    $("#pnl-sentence").textContent = !r.trades && !r.open.length
      ? "Nothing traded on this account yet. Profit and loss shows here once an order fills."
      : `Booked ${word(r.realized)} of ${plain(r.realized)} on ${plural(r.trades, "closed trade")}`
        + (r.open.length ? `; ${plural(r.open.length, "position")} still held, ${r.unrealized >= 0 ? "up" : "down"} ${plain(r.unrealized)} at current prices.` : ".")
        + (r.today_total ? ` Today: ${money(r.today_total)}.` : "");
    const used = r.loss_limit && r.today_total < 0 ? `${Math.min(100, Math.round((-r.today_total / r.loss_limit) * 100))}% of your daily loss limit used` : null;
    $("#pnl-tiles").replaceChildren(
      tile("Total profit/loss", money(total), "booked + still holding", tone(total)),
      tile("Booked (closed trades)", money(r.realized), `${plural(r.trades, "trade")}, after charges`, tone(r.realized)),
      tile("Still holding", money(r.unrealized), `${plural(r.open.length, "position")}, at current price`, tone(r.unrealized)),
      tile("Today", money(r.today_total), used || `booked ${money(r.today_realized)}, holding ${money(r.today_unrealized)}`, tone(r.today_total)));
    $("#pnl-stats").replaceChildren(
      tile("Win rate", r.win_rate == null ? "–" : r.win_rate + "%", "closed trades in profit"),
      tile("Best day", money(r.best_day), null, tone(r.best_day)), tile("Worst day", money(r.worst_day), null, tone(r.worst_day)),
      tile("Largest fall", money(r.max_drawdown), "from a peak (max drawdown)", tone(r.max_drawdown)),
      tile("Charges paid", "₹" + inr.format(r.charges), "brokerage, STT, stamp duty, GST"),
      tile("Daily loss limit", r.loss_limit ? "₹" + inr.format(r.loss_limit) : "Not set", r.loss_limit ? "the kill switch turns on at this loss" : "set one in Settings"));
    const host = $("#pnl-chart");
    $("#pnl-empty").hidden = r.curve.length > 0; host.hidden = r.curve.length === 0;
    if (pnlView.chart) { pnlView.chart.remove(); pnlView.chart = null; }
    if (r.curve.length) {
      pnlView.chart = LightweightCharts.createChart(host, { ...chartOptions(280), timeScale: { borderColor: cssVar("--line"), timeVisible: false },
        localization: { priceFormatter: (v) => "₹" + inr.format(v) } });
      pnlView.series = pnlView.chart.addBaselineSeries({ baseValue: { type: "price", price: 0 }, lineWidth: 2,
        topLineColor: cssVar("--good"), topFillColor1: cssVar("--good") + "44", topFillColor2: cssVar("--good") + "05",
        bottomLineColor: cssVar("--bad"), bottomFillColor1: cssVar("--bad") + "05", bottomFillColor2: cssVar("--bad") + "44" });
      pnlView.series.setData(r.curve.map((p) => ({ time: p.time, value: p.value })));
      pnlView.chart.timeScale().fitContent();
    }
    $("#pnl-open").replaceChildren(table([
      { label: "Stock / contract", get: (x) => x.symbol }, { label: "From", get: (x) => sourceLabel(x.source) },
      { label: "Bought / Sold", get: (x) => (x.quantity < 0 ? "Sold" : "Bought") }, { label: "Qty", num: true, get: (x) => Math.abs(x.quantity) },
      { label: "Entry price ₹", num: true, get: (x) => inr2.format(x.average) }, { label: "Current price ₹", num: true, get: (x) => (x.last ? inr2.format(x.last) : "–") },
      { label: "Profit/Loss", num: true, get: (x) => money(x.unrealized), tone: (x) => tone(x.unrealized) }], r.open, "Nothing held."));
    $("#pnl-closed").replaceChildren(table([
      { label: "Closed on (IST)", get: (x) => x.time_ist }, { label: "Stock / contract", get: (x) => x.symbol }, { label: "From", get: (x) => sourceLabel(x.source) },
      { label: "Qty", num: true, get: (x) => x.quantity }, { label: "Buy price ₹", num: true, get: (x) => inr2.format(x.buy_price) },
      { label: "Sell price ₹", num: true, get: (x) => inr2.format(x.sell_price) }, { label: "Profit/Loss", num: true, get: (x) => money(x.pnl), tone: (x) => tone(x.pnl) },
      { label: "Return", num: true, get: (x) => pct(x.return_pct), tone: (x) => tone(x.pnl) }], r.closed, "No closed trades yet."));
    $("#pnl-sources").replaceChildren(table([
      { label: "Algo / source", get: (x) => sourceLabel(x.source) }, { label: "Booked", num: true, get: (x) => money(x.realized), tone: (x) => tone(x.realized) },
      { label: "Still holding", num: true, get: (x) => (x.open ? money(x.unrealized) : "–"), tone: (x) => tone(x.unrealized) },
      { label: "Total", num: true, get: (x) => money(x.realized + x.unrealized), tone: (x) => tone(x.realized + x.unrealized) },
      { label: "Closed trades", num: true, get: (x) => x.trades }, { label: "Win rate", num: true, get: (x) => (x.win_rate == null ? "–" : x.win_rate + "%") }],
      r.sources, "No fills recorded on this account yet."));
    const s = state.app.settings, on = s.alerts_enabled && s.api_keys.TELEGRAM_BOT_TOKEN && s.telegram_chat_id;
    $("#alerts-state").textContent = feed.last_error ? `Telegram delivery failed: ${feed.last_error}` : on ? "Also sent to Telegram" : "Shown here only. Set up Telegram in Settings to get these on your phone.";
    $("#alerts-list").replaceChildren(...(feed.alerts.length ? feed.alerts.map((a) => h("div", { class: "alert-row" },
      h("time", { text: a.time_ist }), h("div", {}, h("span", { class: "dot " + a.level }), h("strong", { text: a.title }), a.body ? h("div", { class: "b", text: a.body }) : null)))
      : [h("p", { class: "hint", text: "No alerts yet." })]));
  }
  setInterval(() => { if (!$("#tab-pnl").hidden && !document.hidden) refreshPnl(); }, 30000);

  $$("[data-export]").forEach((button) => button.addEventListener("click", async () => {
    const kind = button.dataset.export, mode = state.mode;
    showError("#export-error", null); busy(button, true, "Downloading…");
    const ext = $("#export-csv").checked ? "csv" : "xlsx";
    try { await download(`/export/${kind}.${ext}?mode=${mode}`, `${kind}-${mode}.${ext}`); }
    catch (err) { showError("#export-error", err); }
    busy(button, false);
  }));

  $("#alerts-test").addEventListener("click", async (event) => {
    const out = $("#alerts-test-out"); busy(event.target, true, "Sending…");
    try { const r = await api("/alerts/test", { method: "POST" }); out.replaceChildren(h("div", { class: r.ok ? "okbox" : "error", text: r.ok ? "Sent. Check Telegram." : r.error })); }
    catch (err) { out.replaceChildren(h("div", { class: "error", text: err.message })); }
    busy(event.target, false);
  });

  $("#holidays-fixed").addEventListener("click", () => {
    const box = $("#form-settings").holidays, year = Number(state.app.today.slice(0, 4));
    const fixed = [year, year + 1].flatMap((y) => [`${y}-01-26`, `${y}-05-01`, `${y}-08-15`, `${y}-10-02`, `${y}-12-25`]);
    const all = new Set(box.value.split(/[\s,;]+/).filter(Boolean).concat(fixed));
    box.value = [...all].sort().join("\n");
    $("#holidays-note").textContent = "Added Republic Day, Maharashtra Day, Independence Day, Gandhi Jayanti and Christmas. The festival holidays change every year: add them from NSE's list, then Save.";
  });

  // -- two-factor sign-in ---------------------------------------------------------------------

  async function startPasskeys() {
    let r;
    try { r = await api("/passkeys"); } catch (err) { return; }
    if (!r.available) return;
    $("#passkey-panel").hidden = false;
    renderPasskeys(r.passkeys);
  }
  function renderPasskeys(list) {
    const box = $("#passkeys"), fail = h("div", {});
    const run = async (fn) => { fail.replaceChildren(); try { await fn(); } catch (err) { fail.replaceChildren(h("div", { class: "error", text: Passkey.cancelled(err) ? "Cancelled." : err.message })); } };
    const rows = list.length ? table([{ label: "Name", get: (k) => k.name }, { label: "Added", get: (k) => k.created }, { label: "Last used", get: (k) => k.last_used || "never" },
      { label: "Domain", get: (k) => k.rp_id },
      { label: "", get: (k) => h("button", { type: "button", text: "Remove", onclick: () => run(async () => {
        if (!confirm(`Remove the passkey "${k.name}"? It will no longer sign in.`)) return;
        renderPasskeys((await api(`/passkeys/${encodeURIComponent(k.id)}`, { method: "DELETE" })).passkeys); }) }) }], list, "")
      : h("p", { class: "hint", text: "No passkeys yet." });
    if (!Passkey.supported()) { box.replaceChildren(rows, h("p", { class: "hint", text: "This browser cannot create passkeys here (it needs HTTPS and a recent browser)." })); return; }
    const pass = h("input", { type: "password", autocomplete: "current-password", placeholder: "Your desk password", "aria-label": "Password" });
    const name = h("input", { placeholder: "Name, e.g. My phone", maxlength: 60, "aria-label": "Passkey name" });
    box.replaceChildren(rows, h("div", { class: "row" }, name, pass, h("button", { type: "button", class: "primary", text: "Add a passkey on this device", onclick: () => run(async () => {
      const begun = await api("/passkeys/options", { method: "POST", body: { password: pass.value } });
      const credential = await Passkey.create(begun.options);
      const done = await api("/passkeys", { method: "POST", body: { token: begun.token, credential, name: name.value || navigator.platform || "Passkey" } });
      renderPasskeys(done.passkeys); }) })), fail);
  }

  async function startSecurity() {
    startPasskeys();
    let status;
    try { status = await api("/2fa"); } catch (err) { return; }
    if (!status.available) return;
    $("#security-panel").hidden = false;
    renderSecurity(status);
  }

  function renderSecurity(status) {
    const box = $("#twofa");
    const password = () => h("input", { type: "password", autocomplete: "current-password", placeholder: "Your desk password", "aria-label": "Password" });
    const fail = h("div", {});
    const run = async (fn) => { fail.replaceChildren(); try { await fn(); } catch (err) { fail.replaceChildren(h("div", { class: "error", text: err.message })); } };
    if (status.enabled) {
      const pass = password(), code = h("input", { inputmode: "numeric", autocomplete: "one-time-code", placeholder: "Code", "aria-label": "Code" });
      box.replaceChildren(h("div", { class: "okbox", text: `Two-factor sign-in is ON. ${status.recovery_left} recovery code${status.recovery_left === 1 ? "" : "s"} left.` }),
        h("p", { class: "hint", text: "To turn it off, enter your password and a current code from the app (or a recovery code)." }),
        h("div", { class: "row" }, pass, code, h("button", { type: "button", text: "Turn off", onclick: () => run(async () => {
          renderSecurity({ available: true, ...(await api("/2fa/disable", { method: "POST", body: { password: pass.value, code: code.value } })), recovery_left: 0 }); }) })), fail);
      return;
    }
    const pass = password();
    box.replaceChildren(h("p", { class: "hint", text: "Adds a second step to sign-in: a 6-digit code from an authenticator app on your phone (Google Authenticator, Microsoft Authenticator, Authy, 1Password). Strongly recommended: this desk can place real orders." }),
      h("div", { class: "row" }, pass, h("button", { type: "button", class: "primary", text: "Set up two-factor", onclick: () => run(async () => {
        const setup = await api("/2fa/setup", { method: "POST", body: { password: pass.value } });
        const qr = h("div", { class: "qr" }); qr.append(new DOMParser().parseFromString(setup.qr_svg, "image/svg+xml").documentElement);
        const code = h("input", { inputmode: "numeric", autocomplete: "one-time-code", placeholder: "6-digit code", "aria-label": "Code from the app" });
        box.replaceChildren(h("p", {}, "1. Scan this with your authenticator app, or type the key by hand:"), qr, h("p", {}, h("code", { text: setup.secret })),
          h("p", {}, "2. Enter the 6-digit code the app now shows:"),
          h("div", { class: "row" }, code, h("button", { type: "button", class: "primary", text: "Turn on", onclick: () => run(async () => {
            const done = await api("/2fa/enable", { method: "POST", body: { code: code.value } });
            box.replaceChildren(h("div", { class: "okbox", text: "Two-factor sign-in is ON." }),
              h("p", {}, h("strong", { text: "Save these recovery codes now. They are shown only this once." }), " Each works one time if you lose your phone."),
              h("div", { class: "codes" }, done.recovery_codes.map((c) => h("span", { text: c }))),
              h("button", { type: "button", text: "I have saved them", onclick: () => renderSecurity({ available: true, enabled: true, recovery_left: done.recovery_codes.length }) }));
          }) })), fail);
      }) })), fail);
  }

  // -- F&O expiries ----------------------------------------------------------------------

  const fno = { data: null };

  function expiryChips(item) {
    return h("div", { class: "chips" }, item.expiries.slice(0, 6).map((e, i) => h("span", { class: "chip" + (i === 0 ? " next" : ""), title: `${e.type} expiry` },
      `${e.weekday} ${e.date.slice(8)}-${["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"][Number(e.date.slice(5, 7)) - 1]}-${e.date.slice(2, 4)}`,
      h("small", { text: e.days === 0 ? "today" : `${e.days}d · ${e.type === "weekly" ? "W" : "M"}` }))));
  }

  function renderFno() {
    const d = fno.data, query = $("#fno-search").value.trim().toUpperCase();
    $("#fno-when").textContent = d.fetched ? `From Kite, ${d.fetched} IST. ${d.stale ? "Out of date: " + (d.connected ? "press Refresh." : "connect Kite to refresh.") : "Updates by itself each morning and after every expiry."} W = weekly, M = monthly; the highlighted date is the next expiry.`
      : "Not loaded yet. Connect Kite on the Trade tab, then press Refresh from Kite.";
    const row = (item) => h("tr", {}, h("td", {}, h("strong", { text: item.name })), h("td", { text: item.exchange }), h("td", { class: "num", text: inr.format(item.lot_size) }),
      h("td", { class: "num", text: item.next.days === 0 ? "today" : item.next.days + " days" }), h("td", {}, expiryChips(item)));
    const head = h("thead", {}, h("tr", {}, ["Name", "Segment", "Lot size", "Next expiry in", "Expiry dates"].map((t, i) => h("th", { class: i === 2 || i === 3 ? "num" : null, text: t }))));
    $("#fno-indices").replaceChildren(d.indices.length ? h("table", {}, head.cloneNode(true), h("tbody", {}, d.indices.map(row))) : h("p", { class: "hint", text: "No index contracts loaded." }));
    const stocks = d.stocks.filter((x) => !query || x.name.includes(query));
    $("#fno-stock-title").textContent = `Stocks (${stocks.length}${query ? ` of ${d.stocks.length}` : ""})`;
    $("#fno-stocks").replaceChildren(stocks.length ? h("table", {}, head, h("tbody", {}, stocks.map(row))) : h("p", { class: "hint", text: d.stocks.length ? "No stock matches." : "No stock contracts loaded." }));
  }

  async function refreshFno(force) {
    showError("#fno-error", null);
    try { fno.data = await api(force ? "/fno/refresh" : "/fno", force ? { method: "POST" } : undefined); renderFno(); }
    catch (err) { showError("#fno-error", err); }
  }
  $("#fno-refresh").addEventListener("click", async (event) => { busy(event.target, true, "Refreshing…"); await refreshFno(true); busy(event.target, false); });
  $("#fno-search").addEventListener("input", () => { if (fno.data) renderFno(); });

  // -- install as an app --------------------------------------------------------------------

  let installPrompt = null;
  const installButtons = [$("#install-app"), $("#install-app-2")];
  window.addEventListener("beforeinstallprompt", (event) => { event.preventDefault(); installPrompt = event; installButtons.forEach((b) => { b.hidden = false; }); });
  window.addEventListener("appinstalled", () => { installPrompt = null; installButtons.forEach((b) => { b.hidden = true; }); $("#install-hint").textContent = "Installed. Open TradeSetu from your home screen or apps list."; });
  installButtons.forEach((b) => b.addEventListener("click", async () => { if (!installPrompt) return; installPrompt.prompt(); await installPrompt.userChoice; installPrompt = null; installButtons.forEach((x) => { x.hidden = true; }); }));
  if (window.matchMedia("(display-mode: standalone)").matches) $("#install-hint").textContent = "You are using the installed app.";
  $("#theme-toggle-2").addEventListener("click", () => $("#theme-toggle").click());
  $("#logout-2").addEventListener("click", () => $("#logout").click());


  // -- shared: number formats, candle charts, mini candles ---------------------------------

  const num = (v, dp = 2) => (v == null ? "–" : Number(v).toFixed(dp));
  function crore(v) {
    if (v == null) return "–";
    const cr = v / 1e7;
    return cr >= 1e5 ? `₹${(cr / 1e5).toFixed(2)} L Cr` : cr >= 1 ? `₹${inr.format(Math.round(cr))} Cr` : `₹${inr.format(v)}`;
  }
  function candleSeries(chart) {
    const up = cssVar("--good"), down = cssVar("--bad");
    return chart.addCandlestickSeries({ upColor: up, downColor: down, borderUpColor: up, borderDownColor: down, wickUpColor: up, wickDownColor: down });
  }
  function miniCandles(rows) {
    const NS = "http://www.w3.org/2000/svg", W = 240, H = 64, pad = 2;
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", `0 0 ${W} ${H}`); svg.setAttribute("class", "minicandles"); svg.setAttribute("preserveAspectRatio", "none");
    svg.setAttribute("role", "img"); svg.setAttribute("aria-label", "Recent daily candles");
    if (!rows || rows.length < 2) return svg;
    const lo = Math.min(...rows.map((c) => c.low)), hi = Math.max(...rows.map((c) => c.high)), span = hi - lo || 1;
    const step = W / rows.length, body = Math.max(step * 0.62, 1);
    const y = (v) => pad + (1 - (v - lo) / span) * (H - 2 * pad);
    rows.forEach((c, i) => {
      const cls = c.close >= c.open ? "up" : "down", x = step * (i + 0.5);
      const wick = document.createElementNS(NS, "line");
      wick.setAttribute("x1", x); wick.setAttribute("x2", x); wick.setAttribute("y1", y(c.high)); wick.setAttribute("y2", y(c.low));
      wick.setAttribute("class", cls); wick.setAttribute("stroke-width", "1"); wick.setAttribute("vector-effect", "non-scaling-stroke");
      const rect = document.createElementNS(NS, "rect");
      const top = y(Math.max(c.open, c.close)), bottom = y(Math.min(c.open, c.close));
      rect.setAttribute("x", x - body / 2); rect.setAttribute("width", body); rect.setAttribute("y", top); rect.setAttribute("height", Math.max(bottom - top, 0.8));
      rect.setAttribute("class", cls);
      svg.append(wick, rect);
    });
    return svg;
  }
  function boardCard(c, onclick, sub) {
    const ready = c.candles && c.candles.length > 1;
    return h("button", { type: "button", class: "card", onclick, "aria-label": `${c.name}: ${c.trend}` },
      sub ? h("div", { class: "group", text: sub }) : null,
      h("div", { class: "name" }, h("span", { text: c.name }), trendBadge(c.trend)),
      ready ? h("div", { class: "move" }, signed(c.change_1d), h("span", { text: `today · ${c.value >= 1000 ? inr.format(c.value) : c.value.toFixed(1)}` })) : h("div", { class: "meta", text: "Loading prices…" }),
      miniCandles(c.candles),
      ready ? h("div", { class: "foot" }, h("span", {}, "1 week", signed(c.change_1w)), h("span", {}, "1 month", signed(c.change_1m)), h("span", {}, c.change_3m != null ? "3 months" : "1 year", signed(c.change_3m != null ? c.change_3m : c.change_1y))) : null);
  }

  // -- Market Pulse and the market tools ---------------------------------------------------------

  const SVGNS = "http://www.w3.org/2000/svg";
  const sv = (tag, attrs = {}) => { const el = document.createElementNS(SVGNS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); return el; };
  const PULSE_ZONES = [[0, 20, "Extreme fear", "--bad"], [20, 40, "Fear", "--s2"], [40, 60, "Neutral", "--s4"], [60, 80, "Greed", "--s3"], [80, 100, "Extreme greed", "--good"]];
  const pulseState = { data: null, period: "change_1d", what: "sectors" };

  function heatColour(v) {
    if (v == null) return "var(--surface-2)";
    const strength = Math.min(Math.abs(v) / 3, 1), mix = Math.round(18 + strength * 62);
    return `color-mix(in srgb, var(${v >= 0 ? "--good" : "--bad"}) ${mix}%, var(--surface))`;
  }

  function drawGauge(score) {
    const svg = $("#pulse-gauge"); svg.replaceChildren();
    const cx = 120, cy = 125, r = 95, at = (v) => Math.PI * (1 - v / 100);
    const point = (v, rad) => [cx + rad * Math.cos(at(v)), cy - rad * Math.sin(at(v))];
    for (const [from, to, label, colour] of PULSE_ZONES) {
      const [x1, y1] = point(from, r), [x2, y2] = point(to, r);
      svg.append(sv("path", { d: `M ${x1} ${y1} A ${r} ${r} 0 0 1 ${x2} ${y2}`, stroke: cssVar(colour), "stroke-width": 18, fill: "none" }));
      const t = sv("title"); t.textContent = `${label}: ${from} to ${to}`; svg.lastChild.append(t);
    }
    for (const v of [0, 20, 40, 60, 80, 100]) {
      const [x, y] = point(v, r + 17); const t = sv("text", { x, y: y + 4, "text-anchor": "middle", class: "gauge-tick" }); t.textContent = v; svg.append(t);
    }
    if (score != null) {
      const [nx, ny] = point(score, r - 14);
      svg.append(sv("line", { x1: cx, y1: cy, x2: nx, y2: ny, stroke: cssVar("--text"), "stroke-width": 3, "stroke-linecap": "round" }));
    }
    svg.append(sv("circle", { cx, cy, r: 6, fill: cssVar("--text") }));
    const value = sv("text", { x: cx, y: cy + 38, "text-anchor": "middle", class: "gauge-value" }); value.textContent = score == null ? "–" : Math.round(score); svg.append(value);
    const zoneName = sv("text", { x: cx, y: cy + 58, "text-anchor": "middle", class: "gauge-zone" }); zoneName.textContent = pulseState.data ? pulseState.data.zone : ""; svg.append(zoneName);
  }

  function renderHeat() {
    const d = pulseState.data; if (!d) return;
    const key = pulseState.period;
    const items = pulseState.what === "sectors" ? d.sectors.map((s) => ({ label: s.name.replace(/^Nifty /, ""), v: s[key], size: 1 }))
      : d.stocks.map((s) => ({ label: s.symbol.replace(/\.(NS|BO)$/, ""), v: s[key], size: s.market_cap ? Math.max(1, Math.min(4, Math.round(Math.log10(s.market_cap / 1e11) * 2) + 1)) : 1, symbol: s.symbol }));
    items.sort((a, b) => (b.size - a.size) || ((b.v ?? -99) - (a.v ?? -99)));
    $("#pulse-heat").replaceChildren(...(items.length ? items.map((it) => h("button", { type: "button", class: "heat-cell", style: `background:${heatColour(it.v)};grid-column:span ${it.size}`,
      title: `${it.label}: ${pct(it.v)}`, onclick: it.symbol ? () => openStock(it.symbol) : null },
      h("span", { class: "heat-name", text: it.label }), h("span", { class: "heat-move", text: pct(it.v) })))
      : [h("p", { class: "hint", text: pulseState.what === "sectors" ? "Sector levels are still loading." : "Nifty 50 prices are still loading." })]));
  }

  async function refreshPulse() {
    let d;
    try { d = await api("/tools/pulse"); } catch (err) { showError("#pulse-error", err); return; }
    showError("#pulse-error", null);
    pulseState.data = d;
    drawGauge(d.score);
    const then = (label, v) => h("span", {}, label, " ", h("b", { text: v == null ? "–" : Math.round(v) }));
    $("#pulse-then").replaceChildren(then("1 week ago", d.week_ago), then("1 month ago", d.month_ago));
    $("#pulse-parts").replaceChildren(...(d.parts.length ? d.parts.map((p) => h("div", { class: "pulse-part" },
      h("div", { class: "pulse-part-head" }, h("span", { text: p.name }), h("b", { text: Math.round(p.score) })),
      h("div", { class: "bar" }, h("span", { style: `width:${p.score}%;background:${heatColour((p.score - 50) / 8)}` })),
      h("div", { class: "hint", text: p.detail }))) : [h("p", { class: "hint", text: "The readings need the Nifty's history and stock prices; they are still loading." })]));
    $("#pulse-updated").textContent = `${d.universe} stocks · as of ${d.updated_ist} IST`;
    renderHeat();
  }

  function sortableTable(box, columns, rows, sortState, empty = "Nothing to show yet.") {
    const sorted = rows.slice();
    if (sortState.key) {
      const col = columns.find((c) => c.key === sortState.key);
      const val = col && col.sort ? col.sort : (r) => r[sortState.key];
      sorted.sort((a, b) => { const x = val(a), y = val(b); if (x == null) return 1; if (y == null) return -1; return (x < y ? -1 : x > y ? 1 : 0) * sortState.dir; });
    }
    const head = h("tr", {}, columns.map((c) => h("th", { class: c.num ? "num" : null, style: "cursor:pointer", text: c.label + (sortState.key === c.key ? (sortState.dir > 0 ? " ▲" : " ▼") : ""),
      onclick: () => { sortState.dir = sortState.key === c.key ? -sortState.dir : -1; sortState.key = c.key; sortableTable(box, columns, rows, sortState, empty); } })));
    box.replaceChildren(rows.length ? h("table", {}, h("thead", {}, head), h("tbody", {}, sorted.map((r) => h("tr", {}, columns.map((c) => {
      const content = c.cell ? c.cell(r) : r[c.key];
      const td = h("td", { class: (c.num ? "num " : "") + (c.tone ? tone(c.tone(r)) : "") });
      if (content != null && content.nodeType) td.append(content); else td.textContent = content == null ? "–" : content;
      return td;
    }))))) : h("p", { class: "hint", text: empty }));
  }
  const stockLink = (symbol) => h("button", { type: "button", class: "linkish", text: symbol.replace(/\.(NS|BO)$/, ""), onclick: () => openStock(symbol) });
  const toolState = { tool: "deals", deals: null, drift: null, compass: null, radar: null, compassPeriod: "weekly",
    sorts: { deals: { key: "value_cr", dir: -1 }, spikes: { key: "times", dir: -1 }, drift: { key: "", dir: -1 }, radar: { key: "score", dir: -1 }, compass: { key: "", dir: -1 } } };

  function renderDeals() {
    const d = toolState.deals; if (!d) return;
    const form = $("#form-deals"), q = form.q.value.trim().toUpperCase();
    const rows = d.deals.filter((r) => !q || r.ticker.includes(q) || r.client.toUpperCase().includes(q) || r.security.toUpperCase().includes(q));
    sortableTable($("#deals-table"), [
      { key: "date", label: "Date" }, { key: "kind", label: "Type" }, { key: "ticker", label: "Stock", cell: (r) => stockLink(r.symbol) },
      { key: "client", label: "Client" }, { key: "side", label: "Buy or sell", tone: (r) => (r.side === "Buy" ? 1 : -1) },
      { key: "quantity", label: "Shares", num: true, cell: (r) => inr.format(r.quantity) }, { key: "price", label: "Price ₹", num: true, cell: (r) => inr2.format(r.price) },
      { key: "value_cr", label: "Value ₹ Cr", num: true, cell: (r) => num(r.value_cr, 2) }], rows, toolState.sorts.deals);
    $("#spikes-note").textContent = `Stocks trading at least ${d.threshold}× their 20-day average volume today, out of ${d.universe} the desk tracks. Volume comes from Kite when it is logged in.`;
    sortableTable($("#spikes-table"), [
      { key: "symbol", label: "Stock", cell: (r) => stockLink(r.symbol) }, { key: "times", label: "× average", num: true, cell: (r) => num(r.times, 1) + "×" },
      { key: "volume", label: "Volume", num: true, cell: (r) => inr.format(r.volume) }, { key: "average", label: "20-day average", num: true, cell: (r) => inr.format(r.average) },
      { key: "change_1d", label: "Today", num: true, cell: (r) => pct(r.change_1d), tone: (r) => r.change_1d },
      { key: "value_cr", label: "Traded ₹ Cr", num: true, cell: (r) => num(r.value_cr, 1) }], d.spikes, toolState.sorts.spikes);
    $("#tool-updated").textContent = d.note || (d.fetched ? `NSE deals read ${new Date(d.fetched * 1000).toLocaleString("en-IN")}` : "NSE deals have not been read yet.");
  }

  function renderDrift() {
    const d = toolState.drift; if (!d) return;
    const only = $("#form-drift").only.value;
    const rows = d.rows.filter((r) => only === "all" || (only === "agree" ? r.agrees === true : r.signal !== "No edge"));
    sortableTable($("#drift-table"), [
      { key: "symbol", label: "Stock", cell: (r) => stockLink(r.symbol) }, { key: "company", label: "Company" },
      { key: "result_date", label: "Results" }, { key: "reaction", label: "Reaction", num: true, cell: (r) => pct(r.reaction), tone: (r) => r.reaction },
      { key: "volume_times", label: "Volume ×", num: true, cell: (r) => (r.volume_times == null ? "–" : r.volume_times + "×") },
      { key: "since", label: "Since reaction", num: true, cell: (r) => pct(r.since), tone: (r) => r.since },
      { key: "sessions", label: "Sessions", num: true }, { key: "price", label: "Price ₹", num: true, cell: (r) => inr2.format(r.price) },
      { key: "signal", label: "Signal", cell: (r) => h("span", { class: "trend " + (r.signal.endsWith("up") ? "up" : r.signal.endsWith("down") ? "down" : "flat"), text: r.signal }) },
      { key: "sales_yoy", label: "Sales vs year ago", num: true, cell: (r) => (r.sales_yoy == null ? "–" : pct(r.sales_yoy)), tone: (r) => r.sales_yoy },
      { key: "profit_yoy", label: "Profit vs year ago", num: true, cell: (r) => (r.profit_yoy == null ? "–" : pct(r.profit_yoy)), tone: (r) => r.profit_yoy },
      { key: "verdict", label: "Results", cell: (r) => (r.verdict ? h("span", { class: "trend " + (r.verdict === "Strong" ? "up" : r.verdict === "Weak" ? "down" : "flat"), text: r.verdict, title: r.quarter ? `Quarter to ${r.quarter}` : "" }) : "–") },
      { key: "agrees", label: "Results back the move", sort: (r) => (r.agrees == null ? null : r.agrees ? 1 : 0), cell: (r) => (r.agrees == null ? "–" : r.agrees ? "Yes" : "No"), tone: (r) => (r.agrees == null ? null : r.agrees ? 1 : -1) }], rows, toolState.sorts.drift, driftEmpty(d, only));
    const source = d.source === "NSE" ? "NSE's event calendar" : "BSE's calendar";
    const read = d.read_at ? new Date(d.read_at * 1000).toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : null;
    $("#tool-updated").textContent = d.note
      ? d.note + (d.calendar ? ` Showing the ${d.calendar} results dates read on ${read}.` : "")
      : `${d.calendar} results in ${source} (last 60 days), ${d.priced} priced by the desk, ${d.with_numbers} with the quarter's numbers from NSE so far.`;
  }

  function driftEmpty(d, only) {
    if (!d.calendar) return d.note ? "No results dates yet: the exchanges' calendars could not be read. The reason is shown above." : "Results dates have not been read yet. They load a minute after the desk starts.";
    if (!d.priced) return `None of the ${d.calendar} companies in the results calendar has prices on the desk yet (the desk prices the Nifty 500 and your sectors). Prices load after you log in to your broker.`;
    if (only === "all") return "Nothing to show yet.";
    return only === "agree"
      ? `No stock moved at least ${d.min_move}% on results that back the move. Try a smaller move, or Show: All results.`
      : `No stock moved at least ${d.min_move}% on its results and kept going. Try a smaller move, or Show: All results.`;
  }

  // Sector Compass: four groups from two plain numbers, in the order sectors usually move through them.
  const COMPASS_GROUPS = [
    { key: "Leading", title: "Leading", plain: (L, S) => `Ahead of the Nifty over ${L}, and still gaining on it over ${S}.`, tip: "The strongest sectors right now. Traders look here first for stocks to buy.", cls: "lead" },
    { key: "Weakening", title: "Losing pace", plain: (L, S) => `Ahead of the Nifty over ${L}, but slipping behind it over ${S}.`, tip: "Strong but slowing. Hold with a tighter stoploss; be careful with fresh buys.", cls: "fade" },
    { key: "Lagging", title: "Lagging", plain: (L, S) => `Behind the Nifty over ${L}, and still slipping over ${S}.`, tip: "The weakest sectors. Usually best avoided for new buys.", cls: "lag" },
    { key: "Improving", title: "Catching up", plain: (L, S) => `Behind the Nifty over ${L}, but gaining on it over ${S}.`, tip: "An early turn. Worth watching: these often move into Leading next.", cls: "turn" }];
  const groupTitle = (key) => (COMPASS_GROUPS.find((g) => g.key === key) || { title: key }).title;
  const shortSector = (name) => name.replace(/^(Nifty|BSE) /, "");
  const points = (v) => (v == null ? "–" : `${v >= 0 ? "+" : "−"}${Math.abs(v).toFixed(1)} pts`);
  const compassSpans = (d) => (d.weekly === false ? ["1 month", "1 week"] : ["3 months", "4 weeks"]);

  function drawCompass() {
    const d = toolState.compass; if (!d) return;
    const rows = d.rows, [L, S] = compassSpans(d);
    const ranked = rows.slice().sort((a, b) => b.ahead - a.ahead);
    const say = (r) => `${shortSector(r.name)} (${points(r.ahead)})`;
    $("#compass-summary").replaceChildren(...(rows.length ? [
      h("div", {}, h("b", { text: `Best against the Nifty over ${L}: ` }), ranked.slice(0, 3).map(say).join(", ")),
      h("div", {}, h("b", { text: "Worst: " }), ranked.slice(-3).reverse().map(say).join(", ")),
      h("div", { class: "hint", text: `"pts" is the sector's return minus the Nifty 50's return, in percentage points. Nifty 50: ${pct(rows[0].bench_long)} over ${L}, ${pct(rows[0].bench_short)} over ${S}.` })] : []));
    const line = (label, mine, nifty) => h("div", { class: "compass-figs" }, h("span", { class: "hint", text: `${label}: ` }),
      h("span", { class: mine - nifty >= 0 ? "pos" : "neg", text: pct(mine) }), h("span", { class: "hint", text: ` vs Nifty ${pct(nifty)}` }));
    $("#compass-groups").replaceChildren(...COMPASS_GROUPS.map((g) => {
      const members = rows.filter((r) => r.quadrant === g.key).sort((a, b) => b.ahead - a.ahead);
      return h("div", { class: "compass-group " + g.cls },
        h("div", { class: "compass-head" }, h("h3", { text: g.title }), h("span", { class: "compass-count", text: members.length === 1 ? "1 sector" : `${members.length} sectors` })),
        h("p", { class: "compass-plain", text: g.plain(L, S) }), h("p", { class: "hint", text: g.tip }),
        members.length ? h("ul", { class: "compass-list" }, members.map((r) => h("li", {},
          h("div", { class: "compass-name" }, h("span", { text: shortSector(r.name) }),
            r.was !== r.quadrant ? h("span", { class: "compass-was", text: ` · was ${groupTitle(r.was)}` }) : null),
          line(L, r.change_long, r.bench_long), line(S, r.change_short, r.bench_short))))
          : h("p", { class: "hint", text: "None right now." }));
    }));
    const was = rows.length ? (d.weekly === false ? `${rows[0].tail.length - 1} sessions` : `${rows[0].tail.length - 1} weeks`) : "";
    $("#compass-span").textContent = rows.length ? `${d.weekly === false ? "Daily closes" : "Friday closes"}. "was" is the group a sector was in ${was} ago.` : "";
    if ($("#compass-more").open) drawCompassChart();
    $("#tool-updated").textContent = d.note || `${rows.length} sector indices against the Nifty 50.`;
  }

  function drawCompassChart() {
    const d = toolState.compass; if (!d) return;
    const svg = $("#compass-chart"); svg.replaceChildren();
    const W = 520, pad = 40, rows = d.rows, trails = $("#compass-trails").checked;
    const pts = rows.flatMap((r) => (trails ? r.tail : r.tail.slice(-1))), [L, S] = compassSpans(d);
    const sx = Math.max(1, ...pts.map((t) => Math.abs(t.ahead))) * 1.15, sy = Math.max(1, ...pts.map((t) => Math.abs(t.gaining))) * 1.15;
    const X = (v) => pad + ((v + sx) / (2 * sx)) * (W - 2 * pad), Y = (v) => W - pad - ((v + sy) / (2 * sy)) * (W - 2 * pad);
    const quads = [["Catching up", pad, pad, "--s1"], ["Leading", W / 2, pad, "--good"], ["Lagging", pad, W / 2, "--bad"], ["Losing pace", W / 2, W / 2, "--s4"]];
    for (const [label, x, y, colour] of quads) {
      svg.append(sv("rect", { x, y, width: W / 2 - pad, height: W / 2 - pad, fill: `color-mix(in srgb, ${cssVar(colour)} 9%, transparent)` }));
      const t = sv("text", { x: x + 8, y: y === pad ? y + 18 : W - pad - 8, class: "compass-quad" }); t.textContent = label; svg.append(t);
    }
    svg.append(sv("line", { x1: W / 2, y1: pad, x2: W / 2, y2: W - pad, stroke: cssVar("--line-strong") }));
    svg.append(sv("line", { x1: pad, y1: W / 2, x2: W - pad, y2: W / 2, stroke: cssVar("--line-strong") }));
    const axis = (text, x, y, rotate) => { const t = sv("text", { x, y, "text-anchor": "middle", class: "compass-axis", ...(rotate ? { transform: `rotate(-90 ${x} ${y})` } : {}) }); t.textContent = text; svg.append(t); };
    axis(`Behind the Nifty  ·  Ahead over ${L} →`, W / 2, W - 10); axis(`Slipping  ·  Gaining over ${S} →`, 14, W / 2, true);
    const colourOf = { Leading: "--good", Weakening: "--s4", Lagging: "--bad", Improving: "--s1" };
    const placed = [];
    const spot = (x, y, w) => {               // first label position that does not overlap one already drawn
      for (const dy of [-7, 13, -19, 25, -31, 37]) {
        const box = { x1: x + 7, x2: x + 7 + w, y1: y + dy - 10, y2: y + dy + 2 };
        if (!placed.some((b) => box.x1 < b.x2 && b.x1 < box.x2 && box.y1 < b.y2 && b.y1 < box.y2)) { placed.push(box); return y + dy; }
      }
      return null;
    };
    rows.slice().sort((a, b) => b.gaining - a.gaining).forEach((r) => {
      const colour = cssVar(colourOf[r.quadrant] || "--s2"), last = r.tail[r.tail.length - 1];
      if (trails) svg.append(sv("polyline", { points: r.tail.map((t) => `${X(t.ahead)},${Y(t.gaining)}`).join(" "), fill: "none", stroke: colour, "stroke-width": 1.3, opacity: 0.55 }));
      const dot = sv("circle", { cx: X(last.ahead), cy: Y(last.gaining), r: 5, fill: colour });
      const tip = sv("title", {}); tip.textContent = `${r.name}: ${groupTitle(r.quadrant)}. ${L}: ${points(r.ahead)}, ${S}: ${points(r.gaining)} against the Nifty`; dot.append(tip); svg.append(dot);
      const label = shortSector(r.name), y = spot(X(last.ahead), Y(last.gaining), label.length * 6);
      if (y != null) { const t = sv("text", { x: X(last.ahead) + 7, y, class: "compass-label" }); t.textContent = label; svg.append(t); }
    });
  }

  function renderRadar() {
    const d = toolState.radar; if (!d) return;
    const form = $("#form-radar"), q = form.q.value.trim().toUpperCase(), only = form.only.value;
    const rows = d.rows.filter((r) => (!q || r.symbol.includes(q) || (r.name || "").toUpperCase().includes(q) || (r.sector || "").toUpperCase().includes(q))
      && (only === "all" || (only === "flags" && r.flags.length) || (only === "above" && r.above_50 && r.above_200) || (only === "near" && r.from_high >= -5)));
    const yes = (v) => h("span", { class: v ? "pos" : "neg", text: v ? "Yes" : "No" });
    sortableTable($("#radar-table"), [
      { key: "symbol", label: "Stock", cell: (r) => stockLink(r.symbol) }, { key: "name", label: "Company" }, { key: "sector", label: "Sector" },
      { key: "score", label: "Score", num: true, cell: (r) => num(r.score, 0) }, { key: "price", label: "Price ₹", num: true, cell: (r) => inr2.format(r.price) },
      ...[["change_1d", "Today"], ["change_1w", "1 week"], ["change_1m", "1 month"], ["change_3m", "3 months"], ["change_1y", "1 year"]].map(([key, label]) => ({ key, label, num: true, cell: (r) => pct(r[key]), tone: (r) => r[key] })),
      { key: "from_high", label: "From 52w high", num: true, cell: (r) => pct(r.from_high) },
      { key: "rs_3m", label: "vs Nifty 3m", num: true, cell: (r) => pct(r.rs_3m), tone: (r) => r.rs_3m },
      { key: "above_50", label: "> 50-day", cell: (r) => yes(r.above_50), sort: (r) => (r.above_50 ? 1 : 0) },
      { key: "above_200", label: "> 200-day", cell: (r) => yes(r.above_200), sort: (r) => (r.above_200 ? 1 : 0) },
      { key: "volume_x", label: "Volume ×", num: true, cell: (r) => (r.volume_x == null ? "–" : num(r.volume_x, 1) + "×") },
      { key: "trend", label: "Trend", cell: (r) => trendBadge(r.trend) },
      { key: "pe", label: "P/E", num: true, cell: (r) => num(r.pe, 1) }, { key: "roe", label: "ROE %", num: true, cell: (r) => num(r.roe, 1) },
      { key: "flags", label: "Signals", cell: (r) => r.flags.join(", "), sort: (r) => r.flags.length }], rows, toolState.sorts.radar);
    $("#tool-updated").textContent = `${rows.length} of ${d.universe} stocks · as of ${d.updated_ist} IST`;
  }

  async function loadTool(name) {
    const paths = { deals: () => { const f = $("#form-deals"); return `/tools/deals?days=${f.days.value}&min_value=${Number(f.min_value.value) || 0}`; },
      drift: () => `/tools/drift?min_move=${Number($("#form-drift").min_move.value) || 4}`, compass: () => `/tools/rotation?period=${toolState.compassPeriod}`, radar: () => "/tools/radar" };
    try { toolState[name] = await api(paths[name]()); } catch (err) { showError("#tool-error", err); return; }
    showError("#tool-error", null);
    ({ deals: renderDeals, drift: renderDrift, compass: drawCompass, radar: renderRadar })[name]();
  }

  function showTool(name) {
    toolState.tool = name;
    $$("#tool-pick button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.tool === name)));
    $$("#tab-tools .tool").forEach((s) => { s.hidden = s.id !== "tool-" + name; });
    loadTool(name);
  }

  function startMarketTools() {
    $$("#heat-period button").forEach((b) => b.addEventListener("click", () => { pulseState.period = b.dataset.period; $$("#heat-period button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); renderHeat(); }));
    $$("#heat-what button").forEach((b) => b.addEventListener("click", () => { pulseState.what = b.dataset.what; $$("#heat-what button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); renderHeat(); }));
    $$("#tool-pick button").forEach((b) => b.addEventListener("click", () => showTool(b.dataset.tool)));
    $$("#compass-period button").forEach((b) => b.addEventListener("click", () => { toolState.compassPeriod = b.dataset.period; $$("#compass-period button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); loadTool("compass"); }));
    $("#compass-more").addEventListener("toggle", () => { if ($("#compass-more").open) drawCompassChart(); });
    $("#compass-trails").addEventListener("change", drawCompassChart);
    const deals = $("#form-deals"); deals.days.addEventListener("change", () => loadTool("deals")); deals.min_value.addEventListener("change", () => loadTool("deals")); deals.q.addEventListener("input", renderDeals);
    deals.addEventListener("submit", (e) => e.preventDefault());
    const drift = $("#form-drift"); drift.min_move.addEventListener("change", () => loadTool("drift")); drift.only.addEventListener("change", renderDrift); drift.addEventListener("submit", (e) => e.preventDefault());
    const radarForm = $("#form-radar"); radarForm.q.addEventListener("input", renderRadar); radarForm.only.addEventListener("change", renderRadar); radarForm.addEventListener("submit", (e) => e.preventDefault());
    if (!$("#tab-markets").hidden) refreshPulse();
    setInterval(() => {
      if (document.hidden || !state.app.session.open) return;
      if (!$("#tab-markets").hidden) refreshPulse();
      if (!$("#tab-tools").hidden && toolState.tool !== "compass") loadTool(toolState.tool);
    }, 120000);
  }

  // -- trend rule ------------------------------------------------------------------------------

  function trendRuleEditor(box) {
    const rule = state.app.settings.trend_rule, methods = state.app.trend_methods;
    const field = (key, label, step) => h("label", {}, label, h("input", { type: "number", name: key, value: rule[key], step: step || 1 }));
    const method = h("select", { name: "method" }, Object.entries(methods).map(([k, label]) => h("option", { value: k, text: label })));
    method.value = rule.method;
    const groups = {
      ma: [field("fast", "Fast average (days)"), field("slow", "Slow average (days)")],
      price_ma: [field("period", "Average (days)"), field("band_pct", "Sideways band ±%", 0.1)],
      supertrend: [field("st_period", "ATR period"), field("st_multiplier", "Multiplier", 0.1)],
      return: [field("lookback", "Look back (days)"), field("threshold_pct", "Move needed %", 0.1)],
    };
    const fields = h("div", { class: "row" });
    const draw = () => fields.replaceChildren(...groups[method.value]);
    method.addEventListener("change", draw); draw();
    const out = h("p", { class: "hint", text: "Now: " + (board.trendText || "") });
    const fail = h("div", {});
    const save = h("button", { type: "button", class: "primary", text: "Apply to all boards", onclick: async () => {
      const body = { ...rule, method: method.value };
      $$("input", fields).forEach((input) => { body[input.name] = Number(input.value); });
      fail.replaceChildren();
      try {
        const r = await api("/trend-rule", { method: "PUT", body });
        state.app.settings.trend_rule = r.rule; board.trendText = r.text; out.textContent = "Now: " + r.text;
        refreshSectors(); refreshIndices();
      } catch (err) { fail.replaceChildren(h("div", { class: "error", text: err.message })); }
    } });
    box.replaceChildren(h("h3", { text: "How a trend is decided" }), h("p", { class: "hint", text: "One rule for every card on the Markets and Indices boards, and for each stock's trend. It is worked out on daily candles." }),
      h("div", { class: "row" }, h("label", {}, "Method", method)), fields, h("div", { class: "row" }, save), out, fail);
  }
  $$(".trend-rule-btn").forEach((button) => button.addEventListener("click", () => {
    const box = button.closest(".tab").querySelector(".trend-rule");
    box.hidden = !box.hidden;
    if (!box.hidden) trendRuleEditor(box);
  }));

  // -- Indices board (Nifty and BSE) --------------------------------------------------------------

  const ib = { cards: [], group: "all", filter: "all", sort: "", open: null, chart: null, interval: "1d", detail: null };

  function renderIndexBoard() {
    let list = ib.cards.filter((c) => (ib.group === "all" || c.group === ib.group) && (ib.filter === "all" || c.trend === ib.filter));
    if (ib.sort) list = list.slice().sort((a, b) => (b[ib.sort] ?? -1e9) - (a[ib.sort] ?? -1e9));
    $("#index-cards").replaceChildren(...(list.length ? list.map((c) => boardCard(c, () => openIndex(c.id), c.has_list === false ? `${c.group} · level only` : `${c.group} · ${c.stocks || "?"} stocks`))
      : [h("p", { class: "hint", text: "No index matches this filter." })]));
  }

  async function refreshIndices() {
    if ($("#tab-indices").hidden) return;
    let data;
    try { data = await api("/indices"); } catch (err) { showError("#index-error", err); return; }
    showError("#index-error", null);
    ib.cards = data.indices; board.trendText = data.trend_text;
    const fromKite = data.indices.filter((c) => c.source === "Kite").length;
    $("#index-updated").textContent = `${data.indices.length} indices · ${fromKite ? `${fromKite} from Kite` : "Kite not logged in: levels from Yahoo where it has them"} · as of ${data.updated_ist} IST`;
    if (ib.open) loadIndex(ib.open, false); else renderIndexBoard();
  }

  function openIndex(id) {
    ib.open = id; $("#index-board").hidden = true; $("#index-detail").hidden = false; window.scrollTo({ top: 0 });
    loadIndex(id, true);
  }
  $("#index-back").addEventListener("click", () => {
    ib.open = null; if (ib.chart) { ib.chart.remove(); ib.chart = null; }
    $("#index-detail").hidden = true; $("#index-board").hidden = false; refreshIndices();
  });
  $$("#index-group button").forEach((b) => b.addEventListener("click", () => { ib.group = b.dataset.group; $$("#index-group button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); renderIndexBoard(); }));
  $$("#index-filter button").forEach((b) => b.addEventListener("click", () => { ib.filter = b.dataset.filter; $$("#index-filter button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); renderIndexBoard(); }));
  $("#index-sort").addEventListener("change", (event) => { ib.sort = event.target.value; renderIndexBoard(); });
  $$("#index-interval button").forEach((b) => b.addEventListener("click", () => { ib.interval = b.dataset.interval; $$("#index-interval button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); drawIndexChart(); }));
  $("#index-search").addEventListener("input", () => { if (ib.detail) renderIndexStocks(ib.detail); });

  async function loadIndex(id, redraw) {
    let d;
    try { d = await api("/indices/" + encodeURIComponent(id)); } catch (err) { showError("#index-detail-error", err); return; }
    ib.detail = d;
    $("#index-title").textContent = d.name;
    $("#index-trend").replaceWith(Object.assign(trendBadge(d.trend), { id: "index-trend" }));
    $("#index-source").textContent = d.kite_name ? `Kite: ${d.kite_name}` : d.source ? `Levels from ${d.source}` : "No level data yet";
    const tile = (k, v) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + tone(v), text: v == null ? "–" : pct(v) }));
    $("#index-tiles").replaceChildren(h("div", { class: "tile" }, h("div", { class: "k", text: "Level" }), h("div", { class: "v", text: d.value ? inr2.format(d.value) : "–" })),
      tile("Today", d.change_1d), tile("1 week", d.change_1w), tile("1 month", d.change_1m), tile("1 year", d.change_1y));
    $("#index-weights").textContent = d.group === "BSE" && !d.stocks.length ? "BSE's stock list for this index has not loaded yet (it is read once a day from bseindia.com)."
      : `${d.stocks.length} stocks from ${d.group === "BSE" ? "BSE's market data" : "NSE's list"} · weights: ${d.weights} · prices: ${d.quote_source || "loading"}. Click a stock for its details.`;
    renderIndexStocks(d);
    if (redraw) drawIndexChart();
  }

  async function drawIndexChart() {
    const d = ib.detail; if (!d) return;
    const note = $("#index-chart-note");
    let candlesData = d.candles, source = d.source;
    if (ib.interval !== "1d" || d.kite_name) {
      try {
        const r = await api(`/chart?symbol=IDX:${encodeURIComponent(d.id)}&interval=${ib.interval}`);
        candlesData = r.candles; source = r.source;
      } catch (err) {
        if (ib.interval !== "1d") { note.textContent = err.message; return; }
      }
    }
    note.textContent = `${state.app.intervals[ib.interval]} candles · ${source || "no data"}${source && source.startsWith("built") ? " (Kite and Yahoo have no feed for this index)" : ""}`;
    if (ib.chart) ib.chart.remove();
    ib.chart = LightweightCharts.createChart($("#index-chart"), { ...chartOptions(400), timeScale: { borderColor: cssVar("--line"), timeVisible: ib.interval !== "1d" } });
    const series = candleSeries(ib.chart); series.setData(candlesData || []);
    const show = Math.min((candlesData || []).length, 160);
    ib.chart.timeScale().setVisibleLogicalRange({ from: (candlesData || []).length - show, to: (candlesData || []).length + 3 });
    ib.chart.subscribeCrosshairMove((param) => { const c = param.seriesData && param.seriesData.get(series); $("#index-ohlc").textContent = c ? `O ${inr2.format(c.open)}   H ${inr2.format(c.high)}   L ${inr2.format(c.low)}   C ${inr2.format(c.close)}` : ""; });
  }

  function renderIndexStocks(d) {
    const q = $("#index-search").value.trim().toUpperCase();
    const rows = d.stocks.filter((r) => !q || r.symbol.includes(q) || (r.name || "").toUpperCase().includes(q));
    const cell = (v, cls) => h("td", { class: "num " + (cls || ""), text: v });
    $("#index-stocks").replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, ["Stock", "Company", "Industry", "Price ₹", "Today", "Day range ₹", "Volume", "Mkt cap", "P/E", "ROE %", "Weight", "Contribution"]
        .map((t, i) => h("th", { class: i >= 3 ? "num" : null, text: t })))),
      h("tbody", {}, rows.map((r) => h("tr", {},
        h("td", {}, h("button", { type: "button", class: "linkish", text: r.symbol.replace(/\.(NS|BO)$/, ""), onclick: () => openStock(r.symbol) })),
        h("td", { text: r.name, style: "white-space:normal;min-width:160px" }), h("td", { text: r.industry }),
        cell(r.price == null ? "–" : inr2.format(r.price)), cell(r.change_1d == null ? "–" : pct(r.change_1d), tone(r.change_1d)),
        cell(r.low != null && r.high != null ? `${inr2.format(r.low)} – ${inr2.format(r.high)}` : "–"), cell(r.volume ? inr.format(r.volume) : "–"),
        cell(crore(r.market_cap)), cell(num(r.pe, 1)), cell(num(r.roe, 1)), cell(r.weight_pct.toFixed(2) + "%"),
        cell(r.contribution_1d == null ? "–" : (r.contribution_1d > 0 ? "+" : "") + r.contribution_1d.toFixed(3) + " pts", tone(r.contribution_1d)))))));
  }
  setInterval(() => { if (!$("#tab-indices").hidden && !document.hidden && state.app.session.open) refreshIndices(); }, 60000);

  // -- one stock: quote, technicals, fundamentals ------------------------------------------------

  const sd = { chart: null, symbol: null };
  async function openStock(symbol) {
    const dialog = $("#stock-dialog"), body = $("#sd-body");
    sd.symbol = symbol;
    $("#sd-title").textContent = symbol.replace(/\.(NS|BO)$/, "");
    body.replaceChildren(h("p", { class: "hint", text: "Loading quote, technicals and fundamentals…" }));
    if (!dialog.open) dialog.showModal();
    let d;
    try { d = await api("/stock?symbol=" + encodeURIComponent(symbol)); } catch (err) { body.replaceChildren(h("div", { class: "error", text: err.message })); return; }
    const facts = (pairs) => h("div", { class: "facts" }, pairs.filter(([, v]) => v !== undefined).map(([k, v]) => h("div", {}, h("span", { text: k }), h("b", { text: v == null || v === "" ? "–" : v }))));
    const q = d.quote || {}, t = d.technicals || {}, f = d.fundamentals || {}, L = state.app.fundamental_labels;
    const chartBox = h("div", { class: "tvchart short" });
    const title = (f.name ? `${f.name} · ` : "") + [d.industry || f.industry, ...(d.sectors || [])].filter(Boolean).join(" · ");
    body.replaceChildren(
      h("p", { class: "hint", text: title }),
      h("div", { class: "tiles" },
        h("div", { class: "tile" }, h("div", { class: "k", text: "Last price" }), h("div", { class: "v", text: q.last ? rupees(q.last, 2) : "–" }), h("div", { class: "s", text: d.quote_source || "" })),
        h("div", { class: "tile" }, h("div", { class: "k", text: "Today" }), h("div", { class: "v " + tone(q.change_pct), text: q.change_pct == null ? "–" : pct(q.change_pct) })),
        h("div", { class: "tile" }, h("div", { class: "k", text: "Trend" }), h("div", { class: "v" }, trendBadge(t.trend || "–"))),
        h("div", { class: "tile" }, h("div", { class: "k", text: "Market cap" }), h("div", { class: "v", text: crore(f.market_cap) })),
        h("div", { class: "tile" }, h("div", { class: "k", text: "P/E" }), h("div", { class: "v", text: num(f.pe, 1) }))),
      chartBox,
      h("h3", { text: "Today (quote)" }),
      facts([["Open", q.open && inr2.format(q.open)], ["High", q.high && inr2.format(q.high)], ["Low", q.low && inr2.format(q.low)], ["Previous close", q.prev_close && inr2.format(q.prev_close)],
        ["Volume", q.volume && inr.format(q.volume)], ["Average price", q.average_price && inr2.format(q.average_price)],
        ["Lower circuit", q.lower_circuit && inr2.format(q.lower_circuit)], ["Upper circuit", q.upper_circuit && inr2.format(q.upper_circuit)],
        ["Buy quantity", q.buy_quantity && inr.format(q.buy_quantity)], ["Sell quantity", q.sell_quantity && inr.format(q.sell_quantity)]]),
      h("h3", { text: `Technicals (${d.technicals_source || "no candles"})` }),
      facts([["RSI (14)", num(t.rsi14, 1)], ["MACD", t.macd_state], ["ADX (14)", num(t.adx14, 1)], ["Direction", t.di_state], ["Supertrend (10, 3)", t.supertrend],
        ["ATR (14) % of price", num(t.atr14_pct)], ["SMA 20", t.sma20 && `${inr2.format(t.sma20)} (${pct(t.vs_sma20)})`], ["SMA 50", t.sma50 && `${inr2.format(t.sma50)} (${pct(t.vs_sma50)})`],
        ["SMA 200", t.sma200 && `${inr2.format(t.sma200)} (${pct(t.vs_sma200)})`], ["52-week high", t.high_52w && `${inr2.format(t.high_52w)} (${pct(t.from_high_52w)})`],
        ["52-week low", t.low_52w && `${inr2.format(t.low_52w)} (${pct(t.from_low_52w)})`], ["1 week", t.return_1w != null ? pct(t.return_1w) : null], ["1 month", t.return_1m != null ? pct(t.return_1m) : null],
        ["3 months", t.return_3m != null ? pct(t.return_3m) : null], ["6 months", t.return_6m != null ? pct(t.return_6m) : null], ["1 year", t.return_1y != null ? pct(t.return_1y) : null]]),
      h("h3", { text: "Fundamentals (Yahoo Finance; Kite's API has none)" }),
      Object.keys(f).length ? facts(Object.entries(L).map(([k, label]) => [label, f[k] == null ? null : (k === "market_cap" || k === "revenue" ? crore(f[k]) : num(f[k]))]))
        : h("p", { class: "hint", text: "No fundamentals available for this company from the feed." }),
      h("h3", { text: "Financial statements (company filings on NSE)" }),
      h("div", { id: "fin-box" }, h("p", { class: "hint", text: "Loading…" })));
    loadFinancials(symbol, "");
    if (sd.chart) sd.chart.remove();
    sd.chart = LightweightCharts.createChart(chartBox, { ...chartOptions(280), timeScale: { borderColor: cssVar("--line"), timeVisible: false } });
    try {
      const r = await api("/chart?symbol=" + encodeURIComponent(symbol) + "&interval=1d");
      const series = candleSeries(sd.chart); series.setData(r.candles);
      sd.chart.timeScale().setVisibleLogicalRange({ from: r.candles.length - 120, to: r.candles.length + 2 });
    } catch (err) { chartBox.replaceChildren(h("p", { class: "hint", text: err.message })); }
  }
  // -- one stock: financial statements from the exchange filings ------------------------------

  const fin = { tab: "quarterly", timer: null, data: null, sort: { key: "market_cap", dir: -1 } };
  const FIN_TABS = [["quarterly", "Quarterly results"], ["profit_loss", "Profit & loss"], ["balance_sheet", "Balance sheet"],
    ["cash_flow", "Cash flow"], ["ratios", "Ratios"], ["peers", "Peers"]];
  function finValue(v, kind) {
    if (v == null) return "–";
    if (kind === "money") return Math.abs(v) >= 100 ? inr.format(Math.round(v)) : inr2.format(v);
    if (kind === "days") return inr.format(Math.round(v));
    if (kind === "pct") return v.toFixed(1);
    return inr2.format(v);
  }
  function finTable(t) {
    if (!t || !t.columns.length) return h("p", { class: "hint", text: "Not in the filings read so far." });
    return h("div", { class: "scroll" }, h("table", {},
      h("thead", {}, h("tr", {}, h("th", { text: t.rows.some((r) => r.kind === "money") ? "₹ crore" : "" }), t.columns.map((c) => h("th", { class: "num", text: c })))),
      h("tbody", {}, t.rows.map((r) => h("tr", {}, h("td", { text: r.label }),
        r.values.map((v) => h("td", { class: "num" + (r.kind === "pct" && r.key.endsWith("growth") ? " " + tone(v) : ""), text: finValue(v, r.kind) })))))));
  }
  function finPeers(p) {
    const box = h("div", {});
    if (!p || !p.rows.length) return h("p", { class: "hint", text: "No industry for this stock in the NSE index lists the desk reads, so no peers." });
    box.append(h("p", { class: "hint", text: `${p.industry}: ${p.count} companies in the desk's index lists, the biggest ${Math.min(p.rows.length, 12)} shown. Figures are Yahoo Finance's trailing twelve months, the same source for every company so they compare fairly.` }));
    const grid = h("div", { class: "scroll" }); box.append(grid);
    sortableTable(grid, [
      { key: "symbol", label: "Stock", cell: (r) => (r.this ? h("b", { text: r.symbol.replace(/\.(NS|BO)$/, "") }) : stockLink(r.symbol)) },
      { key: "name", label: "Company" }, { key: "price", label: "Price ₹", num: true, cell: (r) => (r.price == null ? "–" : inr2.format(r.price)) },
      { key: "market_cap", label: "Market cap", num: true, cell: (r) => crore(r.market_cap) }, { key: "pe", label: "P/E", num: true, cell: (r) => num(r.pe, 1) },
      { key: "pb", label: "P/B", num: true, cell: (r) => num(r.pb, 1) }, { key: "roe", label: "ROE %", num: true, cell: (r) => num(r.roe, 1) },
      { key: "net_margin", label: "Net margin %", num: true, cell: (r) => num(r.net_margin, 1) },
      { key: "sales_growth", label: "Sales growth %", num: true, cell: (r) => num(r.sales_growth, 1), tone: (r) => r.sales_growth },
      { key: "profit_growth", label: "Profit growth %", num: true, cell: (r) => num(r.profit_growth, 1), tone: (r) => r.profit_growth },
      { key: "dividend_yield", label: "Dividend yield %", num: true, cell: (r) => num(r.dividend_yield, 2) }], p.rows, fin.sort);
    return box;
  }
  function renderFinancials(symbol) {
    const box = $("#fin-box"), d = fin.data; if (!box || !d || sd.symbol !== symbol) return;
    const s = d.summary || {};
    const status = d.state === "loading" ? `Reading the company's results filings from NSE${d.total ? ` (${d.done} of ${d.total})` : ""}… a company seen for the first time takes about half a minute.`
      : d.note || (d.available ? `From the quarterly results filed on NSE. Latest quarter ${s.latest_quarter}${s.audited ? ` (${s.audited.toLowerCase()})` : ""}.` : "");
    const basis = h("select", { "aria-label": "Results basis", onchange: (e) => { fin.data = null; loadFinancials(symbol, e.target.value); } },
      (d.bases && d.bases.length ? d.bases : [d.basis || "consolidated"]).map((b) => h("option", { value: b, text: b === "consolidated" ? "Consolidated (group)" : "Standalone (company only)", selected: b === d.basis })));
    const refresh = h("button", { type: "button", text: "Refresh", title: "Read NSE's list of filings again",
      onclick: async () => { try { await api("/financials/refresh", { method: "POST", body: { symbol, basis: d.basis || "" } }); } catch (err) { /* shown on next load */ } loadFinancials(symbol, d.basis || ""); } });
    const tabs = h("div", { class: "seg fin-tabs", role: "group", "aria-label": "Statement" }, FIN_TABS.map(([key, name]) =>
      h("button", { type: "button", "aria-pressed": String(fin.tab === key), text: name, onclick: () => { fin.tab = key; renderFinancials(symbol); } })));
    const tile = (k, v, sub) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v", text: v }), sub ? h("div", { class: "s", text: sub }) : null);
    const pc = (v) => (v == null ? "–" : pct(v));
    const tiles = d.available ? h("div", { class: "tiles" },
      tile("Market cap", crore(s.market_cap), s.price ? `at ₹${inr2.format(s.price)}` : "no price yet"),
      tile("P/E", num(s.pe, 1), "on last 4 quarters' profit"), tile("Price / book", num(s.pb, 2)),
      tile("ROE %", num(s.roe, 1), "last full year"),
      d.bank ? tile("ROA %", num(s.roa, 2)) : tile("ROCE %", num(s.roce, 1), "last full year"),
      d.bank ? tile("Net NPA %", num(s.net_npa_pct, 2), s.gross_npa_pct != null ? `gross ${num(s.gross_npa_pct, 2)}%` : "") : tile("Debt / equity", num(s.debt_equity, 2)),
      tile(d.bank ? "Revenue (4 quarters)" : "Sales (4 quarters)", crore(s.sales_ttm)), tile("Profit (4 quarters)", crore(s.profit_ttm)),
      tile(`${s.latest_quarter || "Latest"} ${d.bank ? "revenue" : "sales"} vs year ago`, pc(s.quarter_sales_yoy)), tile(`${s.latest_quarter || "Latest"} profit vs year ago`, pc(s.quarter_profit_yoy))) : null;
    const body = fin.tab === "peers" ? finPeers(d.peers) : d.available ? finTable(d[fin.tab])
      : h("p", { class: "hint", text: d.state === "loading" ? "" : "No statements yet." });
    box.replaceChildren(h("div", { class: "row inline" }, basis, refresh), h("p", { class: "hint", text: status }), tiles || "", tabs, body,
      fin.tab === "ratios" || fin.tab === "balance_sheet" || fin.tab === "cash_flow" ? h("p", { class: "hint", text:
        "Years end in March. The balance sheet is filed every six months (March and September), the cash flow with it. Ratios are worked out from these filings: return on equity on average shareholders' equity, return on capital employed is profit before interest and tax on equity plus borrowings." }) : "");
  }
  async function loadFinancials(symbol, basis) {
    clearTimeout(fin.timer);
    try { fin.data = await api(`/financials?symbol=${encodeURIComponent(symbol)}&basis=${encodeURIComponent(basis || "")}`); }
    catch (err) { const box = $("#fin-box"); if (box && sd.symbol === symbol) box.replaceChildren(h("div", { class: "error", text: err.message })); return; }
    renderFinancials(symbol);
    if (fin.data.state === "loading" && $("#stock-dialog").open && sd.symbol === symbol) fin.timer = setTimeout(() => loadFinancials(symbol, fin.data.basis || basis), 2500);
  }

  $("#sd-close").addEventListener("click", () => $("#stock-dialog").close());
  $("#stock-dialog").addEventListener("click", (event) => { if (event.target.id === "stock-dialog") event.target.close(); });
  $("#sd-chart").addEventListener("click", () => { $("#stock-dialog").close(); $("#form-chart").symbol.value = sd.symbol.replace(/\.NS$/, ""); selectTab("charts"); loadChart(true); });
  $("#sd-trade").addEventListener("click", () => { $("#stock-dialog").close(); $("#form-order").symbol.value = sd.symbol.replace(/\.NS$/, ""); selectTab("trade"); });

  // -- optimiser ---------------------------------------------------------------------------------

  function optRanges() {
    const form = $("#form-opt"), strategy = state.app.strategies.find((x) => x.key === form.strategy.value);
    const box = $("#opt-ranges");
    if (!strategy || !strategy.params.length) { box.replaceChildren(h("p", { class: "hint", text: "This strategy has no settings to optimise. Choose a built-in one." })); return; }
    box.replaceChildren(...strategy.params.map((p) => {
      const span = p.maximum - p.minimum;
      const lo = p.integer ? Math.max(p.minimum, Math.round(p.default / 2)) : Math.max(p.minimum, +(p.default * 0.5).toFixed(2));
      const hiv = p.integer ? Math.min(p.maximum, Math.round(p.default * 2)) : Math.min(p.maximum, +(p.default * 1.5).toFixed(2));
      const stepv = p.integer ? Math.max(1, Math.round((hiv - lo) / 6)) : +(Math.max(span / 50, (hiv - lo) / 6)).toFixed(2);
      const input = (name, value) => h("input", { type: "number", "data-param": p.name, "data-part": name, value, step: p.integer ? 1 : 0.05, min: p.minimum, max: p.maximum });
      return h("div", { class: "row" }, h("label", { class: "check" }, h("input", { type: "checkbox", "data-param": p.name, "data-part": "on", checked: true }), p.label),
        h("label", {}, "From", input("from", lo)), h("label", {}, "To", input("to", hiv)), h("label", {}, "Step", input("step", stepv)));
    }));
    countCombos();
  }
  function readRanges() {
    const ranges = {};
    $$("#opt-ranges input[data-part=on]").forEach((box) => {
      if (!box.checked) return;
      const get = (part) => Number($(`#opt-ranges input[data-param="${box.dataset.param}"][data-part="${part}"]`).value);
      ranges[box.dataset.param] = [get("from"), get("to"), get("step")];
    });
    return ranges;
  }
  function countCombos() {
    const total = Object.values(readRanges()).reduce((n, [a, b, st]) => n * (st > 0 && b >= a ? Math.floor((b - a) / st + 1e-9) + 1 : 1), 1);
    $("#opt-count").textContent = `${total} combinations × ${$("#form-opt").folds.value} windows${total > 400 ? " — too many: the limit is 400" : ""}.`;
  }
  function startOptimiser() {
    const form = $("#form-opt");
    strategyOptions(form.strategy, (x) => !x.custom && x.params.length); intervalOptions(form.interval);
    form.strategy.value = "supertrend";
    form.objective.replaceChildren(...Object.entries(state.app.optimise_objectives).map(([k, label]) => h("option", { value: k, text: label })));
    const today = state.app.today, from = new Date(today); from.setFullYear(from.getFullYear() - 5);
    form.start.value = from.toISOString().slice(0, 10); form.end.value = today;
    form.strategy.addEventListener("change", optRanges); $("#opt-ranges").addEventListener("input", countCombos); form.folds.addEventListener("input", countCombos);
    optRanges();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form); showError("#opt-error", null); busy(button, true, "Starting…");
      try {
        const { job } = await api("/optimise", { method: "POST", body: { symbol: form.symbol.value, start: form.start.value, end: form.end.value,
          interval: form.interval.value, strategy: form.strategy.value, ranges: readRanges(), objective: form.objective.value,
          folds: Number(form.folds.value), train_pct: Number(form.train_pct.value), min_trades: Number(form.min_trades.value),
          stop_loss_pct: Number(form.stop_loss_pct.value || 0) } });
        $("#opt-job").hidden = false; $("#opt-result").hidden = true;
        watchJob(job.id, { title: $("#opt-title"), status: $("#opt-status"), log: $("#opt-log") }, (done) => { busy(button, false); if (done.status === "done") renderOptimiser(done.result); });
      } catch (err) { showError("#opt-error", err); busy(button, false); }
    });
  }
  let optChart = null;
  function renderOptimiser(r) {
    $("#opt-result").hidden = false;
    const wf = r.walk_forward;
    const tile = (k, v, sub, cls) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (cls || ""), text: v }), sub ? h("div", { class: "s", text: sub }) : null);
    const best = r.grid[0];
    $("#opt-tiles").replaceChildren(
      tile("Walk-forward return", wf ? pct(wf.total_return_pct) : "–", wf ? `${wf.period[0]} to ${wf.period[1]}, unseen data only` : "no window chose a setting", wf ? tone(wf.total_return_pct) : ""),
      tile("Walk-forward CAGR", wf ? pct(wf.cagr_pct) : "–", wf ? `max drawdown ${pct(wf.max_drawdown_pct, false)}` : null),
      tile("Efficiency", wf && wf.efficiency_pct != null ? wf.efficiency_pct + "%" : "–", "unseen CAGR ÷ in-sample CAGR. Under ~50%: the settings were fitted to noise"),
      tile("Settings stable?", wf ? (wf.stable ? "Mostly" : "No") : "–", "whether the windows kept choosing the same setting"),
      tile("Best on whole period", best ? Object.entries(best.params).map(([k, v]) => `${k} ${v}`).join(", ") : "–", best ? `${r.objective_name} ${best.score} · in-sample` : null));
    if (optChart) optChart.remove();
    optChart = null;
    if (wf) {
      optChart = LightweightCharts.createChart($("#opt-chart"), { ...chartOptions(280), timeScale: { borderColor: cssVar("--line"), timeVisible: false }, localization: { priceFormatter: (v) => "₹" + inr.format(v) } });
      optChart.addLineSeries({ color: cssVar("--s1"), lineWidth: 2 }).setData(wf.equity.map(([time, value]) => ({ time, value })));
      optChart.timeScale().fitContent();
    }
    $("#opt-windows").replaceChildren(table([
      { label: "Window", get: (w) => w.window }, { label: "Chose on", get: (w) => `${w.train[0]} – ${w.train[1]}` },
      { label: "Setting chosen", get: (w) => (w.params ? Object.entries(w.params).map(([k, v]) => `${k} ${v}`).join(", ") : w.note) },
      { label: "In-sample CAGR", num: true, get: (w) => (w.in_sample ? pct(w.in_sample.cagr_pct) : "–") },
      { label: "Traded on", get: (w) => (w.test ? `${w.test[0]} – ${w.test[1]}` : "–") },
      { label: "Unseen return", num: true, get: (w) => (w.out_of_sample ? pct(w.out_of_sample.total_return_pct) : "–"), tone: (w) => tone(w.out_of_sample && w.out_of_sample.total_return_pct) },
      { label: "Unseen trades", num: true, get: (w) => (w.out_of_sample ? w.out_of_sample.trades : "–") }], r.windows, ""));
    $("#opt-grid").replaceChildren(table([
      ...r.params_names.map((name) => ({ label: name, num: true, get: (x) => x.params[name] })),
      { label: r.objective_name, num: true, get: (x) => (x.score == null ? "too few trades" : x.score) },
      { label: "Return", num: true, get: (x) => pct(x.metrics.total_return_pct), tone: (x) => tone(x.metrics.total_return_pct) },
      { label: "Max DD", num: true, get: (x) => pct(x.metrics.max_drawdown_pct, false) }, { label: "Trades", num: true, get: (x) => x.metrics.trades },
      { label: "Win rate", num: true, get: (x) => (x.metrics.win_rate_pct == null ? "–" : x.metrics.win_rate_pct + "%") }], r.grid, ""));
  }

  // -- options backtest ------------------------------------------------------------------------

  function startFuturesBacktest() {
    const form = $("#form-fbt");
    strategyOptions(form.strategy); intervalOptions(form.interval); form.strategy.value = "supertrend";
    const today = state.app.today, from = new Date(today); from.setFullYear(from.getFullYear() - 3);
    form.start.value = from.toISOString().slice(0, 10); form.end.value = today;
    const params = () => {
      const strategy = state.app.strategies.find((x) => x.key === form.strategy.value);
      const desc = $("#fbt-desc"); if (desc) desc.replaceChildren(strategyBlurb(strategy));
      $("#fbt-params").replaceChildren(...strategy.params.map((p) => h("label", {}, p.label, h("input", { name: "p_" + p.name, type: "number", min: p.minimum, max: p.maximum, step: p.integer ? 1 : 0.1, value: p.default, required: true }))));
    };
    form.strategy.addEventListener("change", params); params();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form); showError("#fbt-error", null); syncSharedDates(); busy(button, true, "Running…");
      const strategyParams = {}; $$("#fbt-params input").forEach((input) => { strategyParams[input.name.slice(2)] = Number(input.value); });
      const body = { underlying: form.underlying.value, start: form.start.value, end: form.end.value, interval: form.interval.value, strategy: form.strategy.value,
        strategy_params: strategyParams, direction: form.direction.value, lots: Number(form.lots.value), margin_pct: Number(form.margin_pct.value),
        stop_pct: Number(form.stop_pct.value || 0), target_pct: Number(form.target_pct.value || 0), slippage_pct: Number(form.slippage_pct.value),
        carry_pct: Number(form.carry_pct.value), capital: Number(form.capital.value) };
      try { const r = await api("/backtest/futures", { method: "POST", body }); $("#bt-futures").append($("#bt-result")); renderBacktest(r); offerDeploy("futures", body, r); }
      catch (err) { showError("#fbt-error", err); }
      busy(button, false);
    });
  }

  function marketBadges(markets) {
    return h("span", { class: "regimes" }, h("b", { text: "Markets: " }), ...["bull", "sideways", "bear"].map((m) =>
      h("span", { class: "regime " + (markets.includes(m) ? "yes" : "no"), text: `${markets.includes(m) ? "✓" : "✗"} ${m[0].toUpperCase() + m.slice(1)}` })));
  }

  // -- real option prices: NSE history import and the live chain recorder --------------------------------------
  const chainHistory = { timer: null, ready: false };

  function historyText(d) {
    const cover = d.underlyings.map((u) => `${u.underlying} ${u.first} to ${u.last} (${u.days.toLocaleString("en-IN")} days)`);
    const snaps = d.snapshots.map((u) => `${u.underlying} live chain ${u.first} to ${u.last}`);
    let text = cover.length ? `Stored: ${cover.join("; ")}.` : "No NSE history stored yet: premiums are modelled.";
    if (snaps.length) text += ` Recorded: ${snaps.join("; ")}.`;
    if (d.size_mb) text += ` ${d.size_mb} MB on disk.`;
    const j = d.job || {}, c = j.counts || {};
    if (j.running) text += ` Downloading ${j.day || j.start}: ${j.done || 0} of ${j.total ?? "…"} days (${c.ok || 0} stored, ${c.none || 0} holidays, ${c.error || 0} errors).`;
    else if (j.finished && !j.error) text += ` Last download finished: ${c.ok || 0} days stored, ${c.none || 0} holidays, ${c.error || 0} errors.`;
    if ((d.days || {}).error && !j.running) text += ` ${d.days.error} days failed and are tried again on the next download.`;
    return text;
  }

  async function loadChainHistory() {
    let d;
    try { d = await api("/options/history"); } catch (err) { showError("#sbt-history-error", err); return; }
    $("#sbt-history-status").textContent = historyText(d);
    const j = d.job || {};
    showError("#sbt-history-error", j.error ? new Error(j.error) : null);
    $("#sbt-history-go").hidden = !!j.running; $("#sbt-history-stop").hidden = !j.running;
    if (!chainHistory.ready) {
      chainHistory.ready = true;
      $("#sbt-history-under").replaceChildren(h("legend", { text: "Indices" }), ...d.choices.map((u) =>
        h("label", { class: "check" }, h("input", { type: "checkbox", value: u, checked: d.defaults.includes(u) }), ` ${u}`)));
      $("#sbt-rec-under").value = d.recorder.underlyings.join(", "); $("#sbt-rec-min").value = d.recorder.minutes;
    }
    const last = Object.entries(d.recorder.last || {}).map(([u, x]) => `${u} at ${x.at} (${x.rows} prices)`);
    $("#sbt-rec-status").textContent = d.recorder.underlyings.length
      ? `Recording ${d.recorder.underlyings.join(", ")} every ${d.recorder.minutes} minutes while the market is open and your broker is connected.${last.length ? " Last: " + last.join("; ") + "." : ""}`
      : "The live chain recorder is off.";
    clearTimeout(chainHistory.timer);
    if (j.running) chainHistory.timer = setTimeout(loadChainHistory, document.hidden ? 15000 : 3000);
  }

  function startChainHistory() {
    const today = state.app.today, from = new Date(today); from.setFullYear(from.getFullYear() - 5);
    $("#sbt-history-start").value = from.toISOString().slice(0, 10); $("#sbt-history-end").value = today;
    $("#sbt-history").addEventListener("toggle", () => { if ($("#sbt-history").open) loadChainHistory(); });
    $("#sbt-history-go").addEventListener("click", async (event) => {
      const underlyings = $$("#sbt-history-under input:checked").map((c) => c.value);
      showError("#sbt-history-error", null); busy(event.target, true, "Starting…");
      try { await api("/options/history/import", { method: "POST", body: { start: $("#sbt-history-start").value, end: $("#sbt-history-end").value, underlyings } }); }
      catch (err) { showError("#sbt-history-error", err); }
      busy(event.target, false); loadChainHistory();
    });
    $("#sbt-history-stop").addEventListener("click", async () => { await api("/options/history/stop", { method: "POST" }).catch(() => {}); loadChainHistory(); });
    $("#sbt-rec-save").addEventListener("click", async (event) => {
      showError("#sbt-history-error", null); busy(event.target, true, "Saving…");
      try { await api("/settings", { method: "PUT", body: { chain_recorder: $("#sbt-rec-under").value, chain_recorder_minutes: Number($("#sbt-rec-min").value || 5) } }); }
      catch (err) { showError("#sbt-history-error", err); }
      busy(event.target, false); loadChainHistory();
    });
  }

  function startStructures() {
    startChainHistory();
    const form = $("#form-sbt");
    $("#sbt-exits").replaceWith(Object.assign(exitRulesBox(true), { id: "sbt-exits" }));
    const list = state.app.structures;
    form.structure.replaceChildren(...[["sideways", "Neutral: sell premium"], ["volatile", "Big move either way: buy premium"], ["bull", "Bullish"], ["bear", "Bearish"]].map(([key, label]) =>
      h("optgroup", { label }, list.filter((x) => (key === "volatile" ? x.markets.includes("bull") && x.markets.includes("bear") && !x.bias
        : key === "sideways" ? !x.bias && x.markets.includes("sideways") : x.bias === key)).map((x) => h("option", { value: x.key, text: x.name })))));
    strategyOptions(form.strategy, (x) => !x.intraday_only); form.strategy.value = "supertrend";
    const today = state.app.today, from = new Date(today); from.setFullYear(from.getFullYear() - 2);
    form.start.value = from.toISOString().slice(0, 10); form.end.value = today;
    const sync = () => {
      const x = list.find((y) => y.key === form.structure.value), mode = form.mode.value;
      $("#sbt-desc").replaceChildren(h("span", { text: x.about }), marketBadges(x.markets));
      $$(".sbt-intraday", form).forEach((el) => { el.hidden = mode !== "intraday"; });
      $$(".sbt-daily", form).forEach((el) => { el.hidden = mode === "intraday"; });
      $$(".sbt-signal", form).forEach((el) => { el.hidden = mode !== "signal"; });
      $$(".sbt-days", form).forEach((el) => { el.hidden = mode === "signal"; });
      [...form.mode.options].find((o) => o.value === "signal").disabled = !x.bias;
      if (mode === "signal" && !x.bias) form.mode.value = "positional";
      form.k.closest("label").hidden = !x.uses_k; form.w.closest("label").hidden = !x.uses_w;
      form.k.min = x.min_k || 0; if (Number(form.k.value) < (x.min_k || 0)) form.k.value = x.min_k;
      $$(".sbt-far", form).forEach((el) => { el.hidden = !x.calendar; });
    };
    form.structure.addEventListener("change", () => { const x = list.find((y) => y.key === form.structure.value); form.w.value = x.w; sync(); });
    form.mode.addEventListener("change", sync);
    form.underlying.addEventListener("change", () => { if (!["NIFTY", "SENSEX"].includes(form.underlying.value)) form.expiry_kind.value = "monthly"; });
    sync();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form); showError("#sbt-error", null); syncSharedDates(); busy(button, true, "Running…");
      const weekdays = $$("input[name=wd]", form).filter((c) => c.checked).map((c) => Number(c.value));
      const num = (name) => Number(form[name].value || 0);
      const body = { structure: form.structure.value, underlying: form.underlying.value, mode: form.mode.value, start: form.start.value, end: form.end.value,
        expiry_kind: form.expiry_kind.value, interval: form.interval.value, entry_time: form.entry_time.value, exit_time: form.exit_time.value,
        min_days: num("min_days"), exit_days_before: num("exit_days_before"), strategy: form.strategy.value, weekdays,
        k: num("k"), w: num("w"), leg_exits: $("#sbt-exits").read(), leg_scope: form.leg_scope.value, stop_pct: num("stop_pct"), target_pct: num("target_pct"), lots: num("lots"),
        margin_pct: num("margin_pct"), iv_scale: num("iv_scale"), skew: num("skew"), slippage_pct: num("slippage_pct"), capital: num("capital"),
        premiums: form.premiums.value };
      try { const r = await api("/backtest/structure", { method: "POST", body }); $("#bt-structures").append($("#bt-result")); renderBacktest(r); offerDeploy("structure", body, r); }
      catch (err) { showError("#sbt-error", err); }
      busy(button, false);
    });
    let lastPayoff = null;
    const payoff = async (button, premiums, labels) => {
      showError("#sbt-error", null); busy(button, true, "Working…");
      const opt = (name) => (form[name].value === "" ? null : Number(form[name].value));
      const body = { structure: form.structure.value, underlying: form.underlying.value, expiry_kind: form.expiry_kind.value,
        k: Number(form.k.value || 0), w: Number(form.w.value || 1), lots: Number(form.lots.value || 1), margin_pct: Number(form.margin_pct.value || 12),
        iv_scale: Number(form.iv_scale.value || 1), spot: opt("spot"), iv_pct: opt("iv_pct"), days: opt("days"), far_days: opt("far_days"), premiums, labels };
      if (premiums && lastPayoff) Object.assign(body, { spot: lastPayoff.spot, iv_pct: lastPayoff.iv_pct, days: lastPayoff.days, far_days: lastPayoff.far_days });
      try { lastPayoff = await api("/options/payoff", { method: "POST", body }); renderPayoff(lastPayoff); }
      catch (err) { showError("#sbt-error", err); }
      busy(button, false);
    };
    $("#sbt-payoff").addEventListener("click", (event) => payoff(event.currentTarget, null));
    $("#sbt-payoff-again").addEventListener("click", (event) => {
      const boxes = $$("#sbt-payoff-inputs input[name=premium]"), legs = lastPayoff ? lastPayoff.legs : [];
      const values = [], labels = [];
      boxes.forEach((el, n) => {
        const leg = legs[n] || {}, same = el.value !== "" && Number(el.value) === leg.premium;
        if (el.value === "" || (same && leg.source === "model")) { values.push(null); labels.push(null); return; }   // modelled
        values.push(Number(el.value));
        labels.push(same ? leg.source : "typed");
      });
      payoff(event.currentTarget, values, labels);
    });
  }

  function renderPayoff(r) {
    $("#sbt-payoff-result").hidden = false;
    const tile = (k, v, s, cls) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (cls || ""), text: v }), s ? h("div", { class: "s", text: s }) : null);
    const far = r.far_days != null ? ` · far ${r.far_days.toFixed(1)} days` : "";
    $("#sbt-payoff-title").textContent = `${r.name} · ${r.underlying} ${inr2.format(r.spot)} · IV ${r.iv_pct}% · ${r.days.toFixed(1)} days to expiry${far}`;
    $("#sbt-payoff-tiles").replaceChildren(
      tile(r.credit ? "Net credit" : "Net debit", rupees(Math.abs(r.net_rupees)), `${inr2.format(Math.abs(r.net_premium))} per unit × ${r.quantity}`, r.credit ? "pos" : "neg"),
      tile("Max profit", r.unlimited_profit ? "Unlimited" : rupees(r.max_profit), "at the near expiry", "pos"),
      tile("Max loss", r.unlimited_loss ? "Unlimited" : rupees(r.max_loss), r.reward_risk != null ? `reward : risk ${r.reward_risk}` : null, "neg"),
      tile("Breakeven" + (r.breakevens.length === 1 ? "" : "s"), r.breakevens.length ? r.breakevens.map((b) => inr.format(b)).join(" / ") : "–",
        r.breakevens.length ? r.breakevens.map((b) => pct((b / r.spot - 1) * 100)).join(" / ") + " from spot" : "none in range"),
      tile("Chance of profit", r.pop_pct == null ? "–" : r.pop_pct + "%", "lognormal at this IV"),
      tile("Margin (estimate)", rupees(r.margin), "check the broker's calculator"),
      tile("Delta", inr2.format(r.greeks.delta), "₹ per 1 point move"),
      tile("Theta", rupees(r.greeks.theta), "₹ per day", r.greeks.theta >= 0 ? "pos" : "neg"),
      tile("Vega", rupees(r.greeks.vega), "₹ per 1% IV"),
      tile("Gamma", r.greeks.gamma.toFixed(3), "delta change per point"));
    drawPayoff($("#sbt-payoff-chart"), $("#sbt-payoff-legend"), r);
    $("#sbt-payoff-legs").replaceChildren(table([
      { label: "Side", get: (g) => g.side, tone: (g) => (g.side === "BUY" ? "pos" : "neg") },
      { label: "Lots", num: true, get: (g) => g.lots }, { label: "Qty", num: true, get: (g) => g.quantity },
      { label: "Contract", get: (g) => (g.type === "FUT" ? "Futures" : `${inr.format(g.strike)} ${g.type}`) },
      { label: "Expiry", get: (g) => `${g.expiry === "far" ? "Far" : "Near"} (${g.days.toFixed(1)} d)` },
      { label: "Premium", num: true, get: (g) => inr2.format(g.premium) },
      { label: "Price from", get: (g) => premiumSource(g.source) },
      { label: "IV %", num: true, get: (g) => (g.iv_pct == null ? "–" : g.iv_pct.toFixed(2) + (g.iv_implied || g.source === "model" ? "" : "*")) },
      { label: "Delta", num: true, get: (g) => g.delta.toFixed(3) }, { label: "Gamma", num: true, get: (g) => g.gamma.toFixed(5) },
      { label: "Theta/day", num: true, get: (g) => inr2.format(g.theta) }, { label: "Vega", num: true, get: (g) => inr2.format(g.vega) },
    ], r.legs, "No legs."));
    $("#sbt-payoff-inputs").replaceChildren(...r.legs.map((g) => {
      const name = `${g.side === "BUY" ? "Buy" : "Sell"} ${g.type === "FUT" ? "Futures" : `${inr.format(g.strike)} ${g.type}`}${g.expiry === "far" ? " (far)" : ""}`;
      return h("label", { text: name + " " }, h("input", { name: "premium", type: "number", min: "0", step: "0.05", value: g.premium, style: "width:7em" }));
    }));
    const unpriced = r.legs.some((g) => g.iv_pct != null && !g.iv_implied && g.source !== "model")
      ? ["* No volatility gives that premium, so that leg uses the IV in the title."] : [];
    $("#sbt-payoff-note").textContent = [...(r.sources || []).map((x) => x[0].toUpperCase() + x.slice(1) + "."), ...r.notes, ...unpriced].join(" ");
    $("#sbt-payoff-result").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function premiumSource(source) {
    return { "Kite live": "Kite, live", "Kite last price": "Kite, last trade", "Kite previous close": "Kite, previous close",
      model: "Model", given: "Typed", typed: "Typed" }[source] || source;
  }

  function drawPayoff(host, legend, r) {
    const NS = "http://www.w3.org/2000/svg";
    const W = 1000, H = 340, pad = { l: 78, r: 16, t: 12, b: 26 };
    const svg = (tag, attrs) => { const el = document.createElementNS(NS, tag); for (const [k, v] of Object.entries(attrs)) el.setAttribute(k, v); return el; };
    const pts = r.payoff, xs = pts.map((p) => p.spot), all = pts.flatMap((p) => [p.expiry, p.today]);
    let lo = Math.min(0, ...all), hi = Math.max(0, ...all);
    const span = hi - lo || 1; lo -= span * 0.06; hi += span * 0.06;
    const x0 = xs[0], x1 = xs[xs.length - 1];
    const x = (v) => pad.l + ((v - x0) / (x1 - x0 || 1)) * (W - pad.l - pad.r);
    const y = (v) => pad.t + (1 - (v - lo) / (hi - lo)) * (H - pad.t - pad.b);
    const compact = (v) => { const a = Math.abs(v), sgn = v < 0 ? "−" : ""; return sgn + "₹" + (a >= 1e7 ? (a / 1e7).toFixed(2) + " Cr" : a >= 1e5 ? (a / 1e5).toFixed(2) + " L" : inr.format(a)); };
    const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, role: "img", "aria-label": `Profit and loss of ${r.name} against the index level, at expiry and today` });
    for (let i = 0; i <= 4; i++) {
      const value = lo + ((hi - lo) * i) / 4, yy = y(value);
      root.append(svg("line", { class: "grid", x1: pad.l, x2: W - pad.r, y1: yy, y2: yy }));
      const label = svg("text", { x: pad.l - 8, y: yy + 4, "text-anchor": "end" }); label.textContent = compact(value); root.append(label);
    }
    for (let i = 0; i <= 5; i++) {
      const v = x0 + ((x1 - x0) * i) / 5;
      const label = svg("text", { x: x(v), y: H - 6, "text-anchor": i === 0 ? "start" : i === 5 ? "end" : "middle" }); label.textContent = inr.format(v); root.append(label);
    }
    // Profit shaded green, loss red, under the expiry line.
    const area = (keep, color) => {
      let d = `M${x(x0).toFixed(1)},${y(0).toFixed(1)}`;
      pts.forEach((p) => { d += `L${x(p.spot).toFixed(1)},${y(keep(p.expiry)).toFixed(1)}`; });
      d += `L${x(x1).toFixed(1)},${y(0).toFixed(1)}Z`;
      root.append(svg("path", { d, fill: color, opacity: 0.14, stroke: "none" }));
    };
    area((v) => Math.max(v, 0), "var(--good)"); area((v) => Math.min(v, 0), "var(--bad)");
    root.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y(0), y2: y(0), stroke: "var(--muted)", "stroke-width": 1 }));
    root.append(svg("line", { x1: x(r.spot), x2: x(r.spot), y1: pad.t, y2: H - pad.b, stroke: "var(--s4)", "stroke-width": 1, "stroke-dasharray": "4 3" }));
    r.breakevens.forEach((b) => root.append(svg("circle", { cx: x(b), cy: y(0), r: 4, fill: "var(--surface)", stroke: "var(--text2)", "stroke-width": 2 })));
    const line = (key, color, dash) => {
      const d = pts.map((p, i) => `${i ? "L" : "M"}${x(p.spot).toFixed(1)},${y(p[key]).toFixed(1)}`).join("");
      root.append(svg("path", { d, fill: "none", stroke: color, "stroke-width": 2, "stroke-dasharray": dash || "none", "stroke-linejoin": "round" }));
    };
    line("today", "var(--s2)", "6 4"); line("expiry", "var(--s1)");
    const cross = svg("line", { class: "cross", y1: pad.t, y2: H - pad.b, visibility: "hidden" });
    root.append(cross);
    const tip = $("#tooltip");
    root.addEventListener("pointermove", (event) => {
      const box = root.getBoundingClientRect();
      const px = ((event.clientX - box.left) / box.width) * W, at = x0 + ((px - pad.l) / (W - pad.l - pad.r)) * (x1 - x0);
      let i = 0; pts.forEach((p, n) => { if (Math.abs(p.spot - at) < Math.abs(pts[i].spot - at)) i = n; });
      cross.setAttribute("x1", x(pts[i].spot)); cross.setAttribute("x2", x(pts[i].spot)); cross.setAttribute("visibility", "visible");
      tip.replaceChildren(h("div", {}, h("strong", { text: `${r.underlying} ${inr2.format(pts[i].spot)} (${pct((pts[i].spot / r.spot - 1) * 100)})` })),
        h("div", {}, h("i", { style: "background:var(--s1)" }), `At expiry: ${rupees(pts[i].expiry)}`),
        h("div", {}, h("i", { style: "background:var(--s2)" }), `Today: ${rupees(pts[i].today)}`));
      tip.hidden = false;
      const tw = tip.offsetWidth, left = event.clientX + 14 + tw > window.innerWidth ? event.clientX - tw - 14 : event.clientX + 14;
      tip.style.left = left + "px"; tip.style.top = Math.max(8, event.clientY - 20) + "px";
    });
    root.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); tip.hidden = true; });
    host.replaceChildren(root);
    legend.replaceChildren(h("span", {}, h("i", { style: "background:var(--s1)" }), "At expiry"), h("span", {}, h("i", { style: "background:var(--s2)" }), "Today (T+0)"),
      h("span", {}, h("i", { style: "background:var(--s4)" }), "Spot now"), h("span", {}, "○ Breakeven"));
  }

  function startPortfolios() {
    const form = $("#form-pbt"), list = state.app.portfolios;
    form.portfolio.replaceChildren(...list.map((p) => h("option", { value: p.key, text: p.name })), h("option", { value: "", text: "Custom…" }));
    const total = () => { const sum = $$("#pbt-weights .wrow").reduce((a, row) => a + Number($("input[name=weight]", row).value || 0), 0);
      $("#pbt-total").textContent = `Total ${sum.toFixed(1)}% ${Math.abs(sum - 100) < 0.01 ? "✓" : "(must be 100%)"}`; };
    const row = (symbol, weight) => { const el = h("div", { class: "row wrow" },
      h("label", {}, "ETF or stock", h("input", { name: "symbol", value: symbol, placeholder: "NIFTYBEES or CASH", required: true })),
      h("label", {}, "Weight %", h("input", { name: "weight", type: "number", min: 0, max: 100, step: 0.5, value: weight, required: true })),
      h("button", { type: "button", text: "Remove", onclick: () => { el.remove(); total(); } }));
      $("input[name=weight]", el).addEventListener("input", total); return el; };
    const load = () => {
      const p = list.find((x) => x.key === form.portfolio.value);
      if (p) { $("#pbt-weights").replaceChildren(...Object.entries(p.weights).map(([sym, w]) => row(sym.replace(/\.NS$/, ""), w)));
        $("#pbt-desc").replaceChildren(h("span", { text: p.about }), marketBadges(p.markets)); }
      else $("#pbt-desc").textContent = "Your own mix: any NSE ETFs or stocks, plus CASH. Weights must add up to 100%.";
      total();
    };
    form.portfolio.addEventListener("change", load); load();
    $("#pbt-add").addEventListener("click", () => { $("#pbt-weights").append(row("", 0)); form.portfolio.value = ""; total(); });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form); showError("#pbt-error", null); busy(button, true, "Running…");
      const weights = {}; $$("#pbt-weights .wrow").forEach((r) => { weights[$("input[name=symbol]", r).value.trim().toUpperCase()] = Number($("input[name=weight]", r).value); });
      const preset = list.find((x) => x.key === form.portfolio.value);
      const same = preset && Object.keys(weights).length === Object.keys(preset.weights).length && Object.entries(preset.weights).every(([k, v]) => weights[k.replace(/\.NS$/, "")] === v);
      const body = { portfolio: form.portfolio.value, rebalance: form.rebalance.value, capital: Number(form.capital.value),
        cash_rate_pct: Number(form.cash_rate_pct.value), cost_pct: Number(form.cost_pct.value), start: form.start.value || null, end: form.end.value || null,
        ...(same ? {} : { weights }) };
      try { const r = await api("/backtest/portfolio", { method: "POST", body }); $("#bt-portfolio").append($("#bt-result")); renderBacktest(r); }
      catch (err) { showError("#pbt-error", err); }
      busy(button, false);
    });
  }

  function startScreens() {
    const form = $("#form-screen"), list = state.app.screens;
    form.screen.replaceChildren(...list.map((x) => h("option", { value: x.key, text: x.name })));
    const universes = ["nifty50", "nifty100", "nifty200", "nifty500", "niftymidcap150", "niftysmallcap250", "niftymicrocap250", "niftytotalmarket"];
    form.universe.replaceChildren(...universes.map((id) => h("option", { value: id, text: id.replace("nifty", "Nifty ").replace("smallcap", "Smallcap ").replace("midcap", "Midcap ").replace("microcap", "Microcap ").replace("totalmarket", "Total Market") })));
    const sync = () => { const x = list.find((y) => y.key === form.screen.value); $("#screen-desc").textContent = x.about; form.universe.value = x.universe; };
    form.screen.addEventListener("change", sync); sync();
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form); showError("#screen-error", null); busy(button, true, "Screening…");
      try {
        const r = await api("/screens/run", { method: "POST", body: { screen: form.screen.value, universe: form.universe.value } });
        const pctv = (v) => (v == null ? "–" : v.toFixed(1));
        $("#screen-result").replaceChildren(
          h("p", { class: "hint", text: `${r.passed} of ${r.checked} ${r.universe_name} stocks pass (${r.missing} without figures yet). Rules: ${r.rules.join(" · ")}` }),
          r.note ? h("div", { class: "warnbox", text: r.note }) : "",
          table([{ label: "Stock", get: (x) => h("button", { type: "button", class: "linkish", text: x.symbol.replace(/\.NS$/, ""), onclick: () => openStock(x.symbol) }) },
            { label: "Company", get: (x) => x.name }, { label: "Industry", get: (x) => x.industry },
            { label: "Mkt cap", num: true, get: (x) => crore(x.market_cap) }, { label: "P/E", num: true, get: (x) => pctv(x.pe) },
            { label: "P/B", num: true, get: (x) => pctv(x.pb) }, { label: "ROE %", num: true, get: (x) => pctv(x.roe) },
            { label: "Debt/Eq", num: true, get: (x) => (x.debt_to_equity == null ? "–" : x.debt_to_equity.toFixed(2)) },
            { label: "Profit growth %", num: true, get: (x) => pctv(x.earnings_growth) }, { label: "PEG", num: true, get: (x) => (x.peg == null ? "–" : x.peg.toFixed(2)) },
            ...(r.screen === "greenblatt" ? [{ label: "Magic rank", num: true, get: (x) => x.magic }] : [])], r.stocks, "No stock passes every rule."));
      } catch (err) { showError("#screen-error", err); }
      busy(button, false);
    });
  }

  function startOptionsBacktest() {
    const form = $("#form-obt");
    strategyOptions(form.strategy); intervalOptions(form.interval); form.strategy.value = "supertrend";
    const today = state.app.today, from = new Date(today); from.setFullYear(from.getFullYear() - 2);
    form.start.value = from.toISOString().slice(0, 10); form.end.value = today;
    const params = () => {
      const strategy = state.app.strategies.find((x) => x.key === form.strategy.value);
      const desc = $("#obt-desc"); if (desc) desc.replaceChildren(strategyBlurb(strategy));
      $("#obt-params").replaceChildren(...strategy.params.map((p) => h("label", {}, p.label, h("input", { name: "p_" + p.name, type: "number", min: p.minimum, max: p.maximum, step: p.integer ? 1 : 0.1, value: p.default, required: true }))));
    };
    form.strategy.addEventListener("change", params); params();
    form.underlying.addEventListener("change", () => { form.expiry_weekday.value = ["SENSEX", "BANKEX", "SENSEX50"].includes(form.underlying.value) ? "3" : "1"; });
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form); showError("#obt-error", null); syncSharedDates(); busy(button, true, "Running…");
      const strategyParams = {}; $$("#obt-params input").forEach((input) => { strategyParams[input.name.slice(2)] = Number(input.value); });
      const body = { underlying: form.underlying.value, start: form.start.value, end: form.end.value, interval: form.interval.value, strategy: form.strategy.value,
        strategy_params: strategyParams, side: form.side.value, moneyness: Number(form.moneyness.value), lots: Number(form.lots.value),
        min_days: Number(form.min_days.value), expiry_weekday: Number(form.expiry_weekday.value), stop_pct: Number(form.stop_pct.value || 0),
        target_pct: Number(form.target_pct.value || 0), iv_scale: Number(form.iv_scale.value), slippage_pct: Number(form.slippage_pct.value), capital: Number(form.capital.value) };
      try { const r = await api("/backtest/options", { method: "POST", body }); $("#bt-options").append($("#bt-result")); renderBacktest(r); }
      catch (err) { showError("#obt-error", err); }
      busy(button, false);
    });
  }

  // -- option chain and options trading ------------------------------------------------------------

  const oc = { underlyings: [], chain: null, timer: null, contract: null, chart: null, interval: "5m", token: null };

  async function startOptionChain() {
    $("#oc-load").addEventListener("click", () => loadChain(true));
    $("#oc-underlying").addEventListener("change", () => { fillExpiries(); loadChain(true); });
    $("#oc-expiry").addEventListener("change", () => loadChain(true));
    $("#oc-width").addEventListener("change", () => loadChain(true));
    $("#oc-pos-refresh").addEventListener("click", refreshOptionPositions);
    $$("#oc-interval button").forEach((b) => b.addEventListener("click", () => { oc.interval = b.dataset.interval; $$("#oc-interval button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); drawContractChart(); }));
    $("#form-oc-order").addEventListener("submit", (event) => { event.preventDefault(); reviewOptionOrder(); });
    $("#form-oc-order").addEventListener("input", () => { $("#oc-review").hidden = true; });
    clearInterval(oc.timer);
    oc.timer = setInterval(() => { if (!$("#tab-fno").hidden && $("#oc-live").checked && state.app.session.open && oc.chain && !document.hidden) loadChain(false); }, 5000);
  }
  async function loadUnderlyings() {
    try { oc.underlyings = (await api("/options/underlyings")).underlyings; } catch (err) { showError("#oc-error", err); return; }
    const sel = $("#oc-underlying"), keep = sel.value;
    sel.replaceChildren(...oc.underlyings.map((u) => h("option", { value: u.name, text: `${u.name} · lot ${u.lot_size}` })));
    if (keep) sel.value = keep;
    fillExpiries();
    if (!oc.underlyings.length) showError("#oc-error", new Error("No index options loaded. Connect Kite on the Trade tab, then press Refresh from Kite below."));
  }
  function fillExpiries() {
    const u = oc.underlyings.find((x) => x.name === $("#oc-underlying").value);
    const sel = $("#oc-expiry"), keep = sel.value;
    sel.replaceChildren(...(u ? u.expiries : []).map((e) => h("option", { value: e, text: e })));
    if (u && u.expiries.includes(keep)) sel.value = keep;
  }
  async function loadChain(showErrors) {
    if (!$("#oc-underlying").value) { await loadUnderlyings(); if (!$("#oc-underlying").value) return; }
    try {
      const q = new URLSearchParams({ underlying: $("#oc-underlying").value, expiry: $("#oc-expiry").value, width: $("#oc-width").value });
      oc.chain = await api("/options/chain?" + q); showError("#oc-error", null); renderChain();
    } catch (err) { if (showErrors) showError("#oc-error", err); }
  }
  function renderChain() {
    const c = oc.chain, tile = (k, v, sub) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v", text: v }), sub ? h("div", { class: "s", text: sub }) : null);
    $("#oc-tiles").replaceChildren(tile(`${c.underlying} spot`, inr2.format(c.spot), c.spot_instrument), tile("Days to expiry", c.days_left.toFixed(1), c.expiry),
      tile("PCR (OI)", c.pcr == null ? "–" : c.pcr.toFixed(2), "put OI ÷ call OI"), tile("Max pain", c.max_pain ? inr.format(c.max_pain) : "–", "where option buyers collect least"),
      tile("ATM straddle", c.straddle ? rupees(c.straddle, 2) : "–", c.expected_move ? `implied move ±${inr2.format(c.expected_move)}` : null), tile("ATM IV", c.atm_iv ? c.atm_iv + "%" : "–", `lot ${c.lot_size}`));
    const side = (x, kind) => {
      if (!x) return Array(6).fill(0).map(() => h("td", { text: "" }));
      const cls = x.itm ? "num itm" : "num";
      const ltp = h("td", { class: cls }, h("button", { type: "button", class: "ltp", text: x.ltp == null ? "–" : inr2.format(x.ltp), title: "Chart and trade", onclick: () => openContract(x, kind) }));
      const cells = [h("td", { class: cls, text: x.oi == null ? "–" : inr.format(x.oi) }), h("td", { class: cls + " " + tone(x.oi_change), text: x.oi_change == null ? "–" : (x.oi_change > 0 ? "+" : "") + inr.format(x.oi_change) }),
        h("td", { class: cls, text: x.volume == null ? "–" : inr.format(x.volume) }), h("td", { class: cls, text: x.iv == null ? "–" : x.iv.toFixed(1) }),
        h("td", { class: cls, text: x.delta == null ? "–" : x.delta.toFixed(2) })];
      const chg = h("td", { class: cls + " " + tone(x.change_pct), text: x.change_pct == null ? "–" : pct(x.change_pct) });
      return kind === "CE" ? [...cells, chg, ltp] : [ltp, chg, ...cells.reverse()];
    };
    const head = ["OI", "OI chg", "Volume", "IV %", "Delta", "Chg", "LTP"];
    $("#oc-table").replaceChildren(h("table", {},
      h("thead", {}, h("tr", {}, h("th", { colspan: 7, class: "num", text: "CALLS" }), h("th", { text: "" }), h("th", { colspan: 7, text: "PUTS" })),
        h("tr", {}, ...head.map((t) => h("th", { class: "num", text: t })), h("th", { text: "Strike" }), ...head.slice().reverse().map((t) => h("th", { class: "num", text: t })))),
      h("tbody", {}, c.rows.map((r) => h("tr", { class: r.atm ? "atm" : null }, ...side(r.CE, "CE"), h("td", { class: "strike", text: inr.format(r.strike) }), ...side(r.PE, "PE"))))));
  }
  function openContract(x, kind) {
    oc.contract = { ...x, kind };
    $("#oc-contract").hidden = false;
    $("#oc-contract-title").textContent = `${x.tradingsymbol} · lot ${x.lot_size}`;
    const form = $("#form-oc-order");
    form.side.value = "BUY"; form.lots.value = 1;
    const buffer = state.app.settings.limit_buffer_pct / 100;
    form.price.value = x.ltp ? (Math.round(x.ltp * (1 + Math.max(buffer, 0.005)) / 0.05) * 0.05).toFixed(2) : "";
    $("#oc-review").hidden = true; showError("#oc-order-error", null);
    drawContractChart();
    $("#oc-contract").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  // -- index futures --------------------------------------------------------------------------
  const fut = { rows: [], contract: null, chart: null, interval: "15m", timer: null };
  async function loadFutures(showErrors) {
    try {
      const r = await api("/futures");
      fut.rows = r.futures; showError("#fut-error", null);
      if (!r.futures.length) { $("#fut-table").replaceChildren(h("p", { class: "hint", text: r.note || "No index futures." })); return; }
      $("#fut-table").replaceChildren(table([
        { label: "Contract", get: (x) => h("button", { type: "button", class: "linkish", text: x.tradingsymbol, onclick: () => openFuture(x) }) },
        { label: "Expiry", get: (x) => `${x.expiry} · ${x.days}d` }, { label: "Lot", num: true, get: (x) => x.lot_size },
        { label: "LTP", num: true, get: (x) => (x.ltp == null ? "–" : inr2.format(x.ltp)) },
        { label: "Today", num: true, get: (x) => pct(x.change_pct), tone: (x) => tone(x.change_pct) },
        { label: "Index", num: true, get: (x) => (x.spot == null ? "–" : inr2.format(x.spot)) },
        { label: "Basis", num: true, get: (x) => (x.basis == null ? "–" : inr2.format(x.basis)), tone: (x) => tone(x.basis) },
        { label: "Basis % p.a.", num: true, get: (x) => (x.basis_annual_pct == null ? "–" : x.basis_annual_pct + "%") },
        { label: "OI", num: true, get: (x) => (x.oi == null ? "–" : inr.format(x.oi)) },
        { label: "Volume", num: true, get: (x) => (x.volume == null ? "–" : inr.format(x.volume)) }], r.futures, ""));
    } catch (err) { if (showErrors) showError("#fut-error", err); }
  }
  function openFuture(x) {
    fut.contract = x; $("#fut-contract").hidden = false;
    $("#fut-title").textContent = `${x.tradingsymbol} · ${x.underlying} futures · lot ${x.lot_size}`;
    drawFutureChart(); $("#fut-contract").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  async function drawFutureChart() {
    if (!fut.contract) return;
    const host = $("#fut-chart");
    if (fut.chart) { fut.chart.remove(); fut.chart = null; }
    host.replaceChildren();
    try {
      const r = await api(`/chart?instrument=${encodeURIComponent(fut.contract.instrument)}&interval=${fut.interval}&overlays=sma20,sma50`);
      fut.chart = LightweightCharts.createChart(host, { ...chartOptions(380), timeScale: { borderColor: cssVar("--line"), timeVisible: fut.interval !== "1d" } });
      const series = candleSeries(fut.chart); series.setData(r.candles);
      fut.chart.timeScale().setVisibleLogicalRange({ from: r.candles.length - 150, to: r.candles.length + 2 });
      fut.chart.subscribeCrosshairMove((param) => { const c = param.seriesData && param.seriesData.get(series); $("#fut-ohlc").textContent = c ? `O ${inr2.format(c.open)}   H ${inr2.format(c.high)}   L ${inr2.format(c.low)}   C ${inr2.format(c.close)}` : ""; });
      $("#fut-note").textContent = `${r.candles.length} ${fut.interval === "1d" ? "day candles (continuous)" : fut.interval + " candles"} · Kite`;
    } catch (err) { host.replaceChildren(h("p", { class: "hint", text: err.message })); $("#fut-note").textContent = ""; }
  }
  $$("#fut-interval button").forEach((b) => b.addEventListener("click", () => {
    fut.interval = b.dataset.interval; $$("#fut-interval button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); drawFutureChart();
  }));
  $("#fut-refresh").addEventListener("click", () => loadFutures(true));
  fut.timer = setInterval(() => { if (!$("#tab-fno").hidden && $("#fut-live").checked && state.app.session.open && !document.hidden) loadFutures(false); }, 10000);

  async function drawContractChart() {
    if (!oc.contract) return;
    const host = $("#oc-chart");
    if (oc.chart) { oc.chart.remove(); oc.chart = null; }
    try {
      const r = await api(`/chart?instrument=${encodeURIComponent(oc.contract.instrument)}&interval=${oc.interval}`);
      oc.chart = LightweightCharts.createChart(host, { ...chartOptions(280), timeScale: { borderColor: cssVar("--line"), timeVisible: oc.interval !== "1d" } });
      const series = candleSeries(oc.chart); series.setData(r.candles);
      oc.chart.timeScale().setVisibleLogicalRange({ from: r.candles.length - 120, to: r.candles.length + 2 });
      oc.chart.subscribeCrosshairMove((param) => { const c = param.seriesData && param.seriesData.get(series); $("#oc-ohlc").textContent = c ? `O ${inr2.format(c.open)}   H ${inr2.format(c.high)}   L ${inr2.format(c.low)}   C ${inr2.format(c.close)}` : ""; });
    } catch (err) { host.replaceChildren(h("p", { class: "hint", text: err.message })); }
  }
  async function reviewOptionOrder(prefill) {
    const form = $("#form-oc-order"), x = oc.contract;
    showError("#oc-order-error", null);
    const order = prefill || { exchange: x.instrument.split(":")[0], tradingsymbol: x.tradingsymbol, side: form.side.value,
      quantity: Number(form.lots.value) * x.lot_size, product: form.product.value, order_type: "LIMIT", price: Number(form.price.value), variety: "regular" };
    try {
      const review = await api("/order/preview", { method: "POST", body: { mode: state.mode, order } });
      const box = $("#oc-review"); box.hidden = false;
      const live = review.mode === "live";
      const parts = [h("h3", { text: `${live ? "LIVE" : "Paper"} order: ${order.side} ${order.quantity} ${order.tradingsymbol}` }),
        h("dl", { class: "kv" }, h("dt", { text: "Limit" }), h("dd", { text: rupees(order.price, 2) }), h("dt", { text: "Premium" }), h("dd", { text: rupees(review.value, 2) }),
          h("dt", { text: "Last price" }), h("dd", { text: review.last_price ? `${rupees(review.last_price, 2)} (${review.last_price_source})` : "–" }),
          h("dt", { text: "Est. charges" }), h("dd", { text: review.charges ? rupees(review.charges.total, 2) : "–" }))];
      review.warnings.forEach((w) => parts.push(h("div", { class: "warnbox", text: w })));
      if (!review.ok) { parts.push(h("div", { class: "error", text: "Cannot be placed:\n• " + review.errors.join("\n• ") })); box.replaceChildren(h("div", { class: "confirm" + (live ? " live" : "") }, parts)); return; }
      let token = review.token;
      const input = h("input", { type: "text", autocomplete: "off", placeholder: review.phrase });
      const send = h("button", { class: live ? "danger" : "primary", text: live ? "Place LIVE order" : "Place paper order", disabled: true });
      const result = h("div", {});
      input.addEventListener("input", () => { send.disabled = input.value.trim().toUpperCase().replace(/\s+/g, " ") !== review.phrase; });
      send.addEventListener("click", async () => {
        if (!token) return; const use = token; token = null; send.disabled = true; input.disabled = true;
        try { const placed = await api("/order/place", { method: "POST", body: { token: use, phrase: input.value } });
          result.replaceChildren(h("div", { class: "warnbox", text: live ? `Sent to Kite, order ${placed.order_id}. Check that it filled.` : `Paper order ${placed.order_id} filled at ${rupees(placed.fill_price, 2)}.` })); }
        catch (err) { result.replaceChildren(h("div", { class: "error", text: "Not placed: " + err.message })); }
        refreshOptionPositions();
      });
      parts.push(h("p", {}, "Type ", h("code", { text: review.phrase }), " to confirm."), h("div", { class: "row" }, input, send), result);
      box.replaceChildren(h("div", { class: "confirm" + (live ? " live" : "") }, parts));
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) { const reasons = err.payload && err.payload.errors; showError("#oc-order-error", reasons ? new Error(reasons.join("\n")) : err); }
  }
  async function refreshOptionPositions() {
    $("#oc-pos-mode").textContent = state.mode === "live" ? "(live, from Kite)" : "(paper)";
    let rows = [];
    try { rows = (await api("/options/positions?mode=" + state.mode)).positions; } catch (err) { $("#oc-positions").replaceChildren(h("p", { class: "hint", text: err.message })); return; }
    $("#oc-positions").replaceChildren(rows.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Contract", "Qty", "Avg ₹", "Last ₹", "P&L", "Expiry", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.map((r) => h("tr", {}, h("td", { text: r.tradingsymbol }), h("td", { class: "num", text: r.quantity }), h("td", { class: "num", text: r.average == null ? "–" : inr2.format(r.average) }),
        h("td", { class: "num", text: r.last == null ? "–" : inr2.format(r.last) }), h("td", { class: "num " + tone(r.pnl), text: r.pnl == null ? "–" : rupees(r.pnl, 2) }),
        h("td", { text: r.contract ? r.contract.expiry : "–" }),
        h("td", {}, h("button", { type: "button", text: "Exit", onclick: () => {
          const buffer = Math.max(state.app.settings.limit_buffer_pct / 100, 0.005);
          oc.contract = { instrument: r.instrument, tradingsymbol: r.tradingsymbol, lot_size: (r.contract && r.contract.lot_size) || 1, ltp: r.last };
          $("#oc-contract").hidden = false; $("#oc-contract-title").textContent = `${r.tradingsymbol} · exit`; drawContractChart();
          reviewOptionOrder({ exchange: r.exchange, tradingsymbol: r.tradingsymbol, side: "SELL", quantity: r.quantity, product: r.product || "NRML", order_type: "LIMIT",
            price: r.last ? Math.max(0.05, Math.round(r.last * (1 - buffer) / 0.05) * 0.05) : 0.05, variety: "regular" });
        } }))))))
      : h("p", { class: "hint", text: "No option positions." }));
  }

  // -- MCX gold and silver ------------------------------------------------------------------

  const mcxs = { view: null, contract: null, chart: null, interval: "15m", chain: null };
  const mcxTick = (x) => (x && x.tick_size) || 1;
  const roundTo = (v, tick, up) => { const steps = v / tick; return Number(((up ? Math.ceil(steps - 1e-9) : Math.floor(steps + 1e-9)) * tick).toFixed(2)); };
  async function loadMcx(showErrors, reload) {
    try {
      if (reload) await api("/fno/refresh", { method: "POST" });
      const v = await api("/mcx"); mcxs.view = v; showError("#mcx-error", null);
      const s = v.session, pill = $("#mcx-session");
      pill.textContent = s.open ? `MCX open · closes ${s.closes}` : "MCX closed"; pill.className = "pill " + (s.open ? "good" : ""); pill.title = s.note;
      $("#mcx-note").textContent = [v.note, `Delivery cut-off: ${v.exit_days} days before expiry. New MCX positions ${v.trade_window[0]}–${v.trade_window[1]} IST. Contracts listed ${v.fetched || "never"}.`].filter(Boolean).join(" ");
      const rows = v.metals.flatMap((m) => m.futures.map((f) => ({ ...f, spec: m })));
      $("#mcx-futures").replaceChildren(table([
        { label: "Contract", get: (x) => h("button", { type: "button", class: "linkish", text: x.tradingsymbol, onclick: () => openMcx(x, x.spec) }) },
        { label: "Lot", get: (x) => `${x.spec.lot} · ${x.spec.quote}` },
        { label: "Expiry", get: (x) => h("span", {}, `${x.expiry} · ${x.days}d `, x.cutoff ? h("span", { class: "pill warn", text: "delivery cut-off" }) : null) },
        { label: "LTP", num: true, get: (x) => (x.ltp == null ? "–" : inr2.format(x.ltp)) },
        { label: "Today", num: true, get: (x) => pct(x.change_pct), tone: (x) => tone(x.change_pct) },
        { label: "₹ per lot", num: true, get: (x) => rupees(x.value_per_lot) },
        { label: "Margin / lot (est.)", num: true, get: (x) => rupees(x.margin_per_lot) },
        { label: "OI", num: true, get: (x) => (x.oi == null ? "–" : inr.format(x.oi)) },
        { label: "Volume", num: true, get: (x) => (x.volume == null ? "–" : inr.format(x.volume)) }], rows,
        v.connected ? "No MCX contracts loaded. Press Reload contracts from Kite." : "Connect Kite on the Trade tab to load MCX gold and silver."));
      const sel = $("#mcx-oc-name"), keep = sel.value;
      const withOptions = v.metals.filter((m) => m.option_expiries.length);
      sel.replaceChildren(...withOptions.map((m) => h("option", { value: m.name, text: `${m.name} · ${m.lot}` })));
      if (keep && withOptions.some((m) => m.name === keep)) sel.value = keep;
      fillMcxExpiries();
    } catch (err) { if (showErrors) showError("#mcx-error", err); }
  }
  function fillMcxExpiries() {
    const m = mcxs.view && mcxs.view.metals.find((x) => x.name === $("#mcx-oc-name").value);
    const sel = $("#mcx-oc-expiry"), keep = sel.value;
    sel.replaceChildren(...(m ? m.option_expiries : []).map((e) => h("option", { value: e, text: e })));
    if (m && m.option_expiries.includes(keep)) sel.value = keep;
  }
  function openMcx(x, spec, side) {
    mcxs.contract = { ...x, spec };
    $("#mcx-ticket").hidden = false;
    $("#mcx-ticket-title").textContent = `${x.tradingsymbol} · ${spec.label}`;
    $("#mcx-spec").textContent = `One lot is ${spec.lot}, quoted ${spec.quote}: ₹1 on the price is ₹${spec.units_per_lot} a lot. Tick ₹${mcxTick(x)}.` +
      (x.expiry ? ` Expires ${x.expiry}.` : "") + (x.cutoff ? " Inside the delivery cut-off: only closing orders are accepted." : "");
    const form = $("#form-mcx-order"), tick = mcxTick(x), buffer = Math.max(state.app.settings.limit_buffer_pct / 100, 0.002);
    form.side.value = side || "BUY"; form.lots.value = 1; form.price.step = tick; form.price.min = tick;
    form.price.value = x.ltp ? roundTo(x.ltp * (form.side.value === "BUY" ? 1 + buffer : 1 - buffer), tick, form.side.value === "BUY") : "";
    $("#mcx-review").hidden = true; showError("#mcx-order-error", null);
    drawMcxChart(); $("#mcx-ticket").scrollIntoView({ behavior: "smooth", block: "start" });
  }
  async function drawMcxChart() {
    if (!mcxs.contract) return;
    const host = $("#mcx-chart");
    if (mcxs.chart) { mcxs.chart.remove(); mcxs.chart = null; }
    host.replaceChildren();
    try {
      const r = await api(`/chart?instrument=${encodeURIComponent(mcxs.contract.instrument)}&interval=${mcxs.interval}`);
      mcxs.chart = LightweightCharts.createChart(host, { ...chartOptions(300), timeScale: { borderColor: cssVar("--line"), timeVisible: mcxs.interval !== "1d" } });
      const series = candleSeries(mcxs.chart); series.setData(r.candles);
      mcxs.chart.timeScale().setVisibleLogicalRange({ from: r.candles.length - 150, to: r.candles.length + 2 });
      mcxs.chart.subscribeCrosshairMove((param) => { const c = param.seriesData && param.seriesData.get(series); $("#mcx-ohlc").textContent = c ? `O ${inr2.format(c.open)}   H ${inr2.format(c.high)}   L ${inr2.format(c.low)}   C ${inr2.format(c.close)}` : ""; });
    } catch (err) { host.replaceChildren(h("p", { class: "hint", text: err.message })); }
  }
  async function reviewMcxOrder(prefill) {
    const form = $("#form-mcx-order"), x = mcxs.contract;
    showError("#mcx-order-error", null);
    const order = prefill || { exchange: "MCX", tradingsymbol: x.tradingsymbol, side: form.side.value, quantity: Number(form.lots.value),
      product: form.product.value, order_type: "LIMIT", price: Number(form.price.value), variety: "regular" };
    try {
      const review = await api("/order/preview", { method: "POST", body: { mode: state.mode, order } });
      const box = $("#mcx-review"); box.hidden = false;
      const live = review.mode === "live", future = order.tradingsymbol.endsWith("FUT");
      const parts = [h("h3", { text: `${live ? "LIVE" : "Paper"} MCX order: ${order.side} ${order.quantity} lot(s) ${order.tradingsymbol}` }),
        h("dl", { class: "kv" }, h("dt", { text: "Limit" }), h("dd", { text: rupees(order.price, 2) }),
          h("dt", { text: future ? "Contract value" : "Premium" }), h("dd", { text: rupees(review.value, 0) }),
          ...(future ? [h("dt", { text: "Margin (est.)" }), h("dd", { text: rupees(review.value * state.app.settings.mcx_margin_pct / 100, 0) })] : []),
          h("dt", { text: "Last price" }), h("dd", { text: review.last_price ? `${rupees(review.last_price, 2)} (${review.last_price_source})` : "–" }),
          h("dt", { text: "Est. charges" }), h("dd", { text: review.charges ? rupees(review.charges.total, 2) : "–" }))];
      review.warnings.forEach((w) => parts.push(h("div", { class: "warnbox", text: w })));
      if (!review.ok) { parts.push(h("div", { class: "error", text: "Cannot be placed:\n• " + review.errors.join("\n• ") })); box.replaceChildren(h("div", { class: "confirm" + (live ? " live" : "") }, parts)); return; }
      let token = review.token;
      const input = h("input", { type: "text", autocomplete: "off", placeholder: review.phrase });
      const send = h("button", { class: live ? "danger" : "primary", text: live ? "Place LIVE order" : "Place paper order", disabled: true });
      const result = h("div", {});
      input.addEventListener("input", () => { send.disabled = input.value.trim().toUpperCase().replace(/\s+/g, " ") !== review.phrase; });
      send.addEventListener("click", async () => {
        if (!token) return; const use = token; token = null; send.disabled = true; input.disabled = true;
        try { const placed = await api("/order/place", { method: "POST", body: { token: use, phrase: input.value } });
          result.replaceChildren(h("div", { class: "warnbox", text: live ? `Sent to Kite, order ${placed.order_id}. Check that it filled.` : `Paper order ${placed.order_id} filled at ${rupees(placed.fill_price, 2)}.` })); }
        catch (err) { result.replaceChildren(h("div", { class: "error", text: "Not placed: " + err.message })); }
        refreshMcxPositions();
      });
      parts.push(h("p", {}, "Type ", h("code", { text: review.phrase }), " to confirm."), h("div", { class: "row" }, input, send), result);
      box.replaceChildren(h("div", { class: "confirm" + (live ? " live" : "") }, parts));
      box.scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) { const reasons = err.payload && err.payload.errors; showError("#mcx-order-error", reasons ? new Error(reasons.join("\n")) : err); }
  }
  async function loadMcxChain() {
    if (!$("#mcx-oc-name").value) { showError("#mcx-oc-error", new Error("No MCX options loaded. Reload contracts from Kite above.")); return; }
    try {
      const q = new URLSearchParams({ name: $("#mcx-oc-name").value, expiry: $("#mcx-oc-expiry").value, width: 10 });
      const c = await api("/mcx/chain?" + q); mcxs.chain = c; showError("#mcx-oc-error", null);
      const tile = (k, v, sub) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v", text: v }), sub ? h("div", { class: "s", text: sub }) : null);
      $("#mcx-oc-tiles").replaceChildren(tile("Underlying future", inr2.format(c.spot), c.future.tradingsymbol), tile("Days to expiry", c.days_left.toFixed(1), c.expiry + (c.cutoff ? " · in cut-off" : "")),
        tile("PCR (OI)", c.pcr == null ? "–" : c.pcr.toFixed(2), "put OI ÷ call OI"), tile("ATM straddle", c.straddle ? rupees(c.straddle, 2) : "–", c.straddle ? `₹${inr.format(c.straddle * c.multiplier)} a lot` : null),
        tile("ATM IV", c.atm_iv ? c.atm_iv + "%" : "–", "Black-76"), tile("Lot", c.spec.lot, `₹1 = ₹${c.multiplier} a lot`));
      const side = (x, kind) => {
        if (!x) return Array(4).fill(0).map(() => h("td", { text: "" }));
        const cls = x.itm ? "num itm" : "num";
        const open = () => openMcx({ ...x, expiry: c.expiry, cutoff: c.cutoff }, c.spec);
        const cells = [h("td", { class: cls, text: x.oi == null ? "–" : inr.format(x.oi) }), h("td", { class: cls, text: x.iv == null ? "–" : x.iv.toFixed(1) }),
          h("td", { class: cls, text: x.delta == null ? "–" : x.delta.toFixed(2) }),
          h("td", { class: cls }, h("button", { type: "button", class: "ltp", text: x.ltp == null ? "–" : inr2.format(x.ltp), title: "Chart and trade", onclick: open }))];
        return kind === "CE" ? cells : cells.reverse();
      };
      const head = ["OI", "IV", "Δ", "LTP"];
      $("#mcx-oc-table").replaceChildren(h("table", {},
        h("thead", {}, h("tr", {}, h("th", { colspan: 4, text: "Calls" }), h("th", { text: "" }), h("th", { colspan: 4, text: "Puts" })),
          h("tr", {}, ...head.map((t) => h("th", { class: "num", text: t })), h("th", { text: "Strike" }), ...head.slice().reverse().map((t) => h("th", { class: "num", text: t })))),
        h("tbody", {}, c.rows.map((r) => h("tr", { class: r.atm ? "atm" : null }, ...side(r.CE, "CE"), h("td", { class: "strike", text: inr.format(r.strike) }), ...side(r.PE, "PE"))))));
    } catch (err) { showError("#mcx-oc-error", err); }
  }
  async function refreshMcxPositions() {
    $("#mcx-pos-mode").textContent = state.mode === "live" ? "(live, from Kite)" : "(paper)";
    let rows = [];
    try { rows = (await api("/mcx/positions?mode=" + state.mode)).positions; } catch (err) { $("#mcx-positions").replaceChildren(h("p", { class: "hint", text: err.message })); return; }
    $("#mcx-positions").replaceChildren(table([
      { label: "Contract", get: (r) => h("span", {}, r.tradingsymbol, " ", r.cutoff ? h("span", { class: "pill warn", text: "close: delivery cut-off" }) : null, r.warning ? h("span", { class: "pill bad", title: r.warning, text: "check" }) : null) },
      { label: "Lots", num: true, get: (r) => r.quantity }, { label: "Avg ₹", num: true, get: (r) => (r.average == null ? "–" : inr2.format(r.average)) },
      { label: "Last ₹", num: true, get: (r) => (r.last == null ? "–" : inr2.format(r.last)) },
      { label: "P&L", num: true, get: (r) => (r.pnl == null ? "–" : rupees(r.pnl, 2)), tone: (r) => tone(r.pnl) },
      { label: "Expiry", get: (r) => (r.contract ? r.contract.expiry : "–") },
      { label: "", get: (r) => (r.quantity > 0 ? h("button", { type: "button", text: "Exit", onclick: () => {
        const x = { ...(r.contract || {}), instrument: r.instrument, tradingsymbol: r.tradingsymbol, ltp: r.last };
        const spec = (mcxs.view && mcxs.view.metals.find((m) => r.tradingsymbol.startsWith(m.name) && /^\d/.test(r.tradingsymbol.slice(m.name.length)))) || { label: "", lot: "", quote: "", units_per_lot: r.multiplier };
        openMcx(x, spec, "SELL");
        const tick = mcxTick(x), buffer = Math.max(state.app.settings.limit_buffer_pct / 100, 0.002);
        reviewMcxOrder({ exchange: "MCX", tradingsymbol: r.tradingsymbol, side: "SELL", quantity: r.quantity, product: r.product || "NRML", order_type: "LIMIT",
          price: r.last ? Math.max(tick, roundTo(r.last * (1 - buffer), tick, false)) : tick, variety: "regular" });
      } }) : "short: closed by its algo") }], rows, "No MCX positions."));
  }
  $("#form-mcx-order").addEventListener("submit", (event) => { event.preventDefault(); reviewMcxOrder(); });
  $("#form-mcx-order").addEventListener("input", () => { $("#mcx-review").hidden = true; });
  $$("#mcx-interval button").forEach((b) => b.addEventListener("click", () => {
    mcxs.interval = b.dataset.interval; $$("#mcx-interval button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); drawMcxChart();
  }));
  $("#mcx-refresh").addEventListener("click", () => loadMcx(true));
  $("#mcx-reload").addEventListener("click", async (event) => { busy(event.target, true, "Loading…"); await loadMcx(true, true); busy(event.target, false); });
  $("#mcx-oc-name").addEventListener("change", fillMcxExpiries);
  $("#mcx-oc-load").addEventListener("click", loadMcxChain);
  $("#mcx-pos-refresh").addEventListener("click", refreshMcxPositions);
  setInterval(() => { if (!$("#tab-mcx").hidden && $("#mcx-live").checked && mcxs.view && mcxs.view.session.open && !document.hidden) loadMcx(false); }, 10000);

  // -- tax P&L and exports -----------------------------------------------------------------

  async function refreshTax() {
    const mode = state.mode;
    let r; try { r = await api("/tax?mode=" + mode); } catch (err) { $("#tax-years").replaceChildren(h("p", { class: "hint", text: err.message })); return; }
    const money = (v) => (v == null ? "–" : (v < 0 ? "−" : "") + "₹" + inr.format(Math.abs(Math.round(v))));
    $("#tax-years").replaceChildren(table([
      { label: "Financial year", get: (y) => "FY " + y.fy }, { label: "Short-term gains", num: true, get: (y) => money(y.STCG), tone: (y) => tone(y.STCG) },
      { label: "Long-term gains", num: true, get: (y) => money(y.LTCG), tone: (y) => tone(y.LTCG) },
      { label: "Intraday (speculative)", num: true, get: (y) => money(y["Speculative (intraday)"]), tone: (y) => tone(y["Speculative (intraday)"]) },
      { label: "F&O (business)", num: true, get: (y) => money(y["F&O (non-speculative)"]), tone: (y) => tone(y["F&O (non-speculative)"]) },
      { label: "F&O turnover", num: true, get: (y) => money(y.fno_turnover) }, { label: "Charges", num: true, get: (y) => money(y.charges) },
      { label: "STCG tax @20% (est.)", num: true, get: (y) => money(y.stcg_tax_estimate) }, { label: "LTCG tax @12.5% (est.)", num: true, get: (y) => money(y.ltcg_tax_estimate) },
      { label: "Lots", num: true, get: (y) => y.trades }], r.years, "No closed trades on this account yet."));
  }

  // -- strategy builder ---------------------------------------------------------------

  const builder = { editing: null };

  function operandControls(side, allowNumber) {
    const wrap = h("span", { class: "rule" });
    const select = h("select", { "aria-label": "Indicator" });
    if (allowNumber) select.append(h("option", { value: "value", text: "a number" }));
    const group = (label, items) => h("optgroup", { label }, items.map((o) => h("option", { value: o.key, text: o.label })));
    const pivot = (o) => o.key.startsWith("pivot");
    const numbers = state.app.indicators.filter((o) => (o.kind || "number") === "number");
    select.append(group("Price and price levels", numbers.filter((o) => o.overlay && !pivot(o))),
      group("Pivots (traditional: daily, weekly, monthly)", numbers.filter(pivot)),
      group("Oscillators and volume", numbers.filter((o) => !o.overlay)));
    if (!allowNumber) select.append(group("Time and day", state.app.indicators.filter((o) => (o.kind || "number") !== "number")));
    const extra = h("span", { class: "rule" });
    const draw = (values) => {
      if (select.value === "value") {
        extra.replaceChildren(h("input", { type: "number", step: "any", "data-role": "value", value: values && values.value != null ? values.value : "", required: true, "aria-label": "Number" }));
        return;
      }
      const operand = state.app.indicators.find((o) => o.key === select.value);
      extra.replaceChildren(...operand.params.map((p) => h("input", { type: "number", "data-param": p.name, min: p.minimum, max: p.maximum,
        step: p.integer ? 1 : 0.1, value: values && values.params && values.params[p.name] != null ? values.params[p.name] : p.default, title: p.label, "aria-label": p.label, required: true })));
    };
    select.value = side && side.indicator ? side.indicator : (allowNumber ? "value" : "close");
    select.addEventListener("change", () => draw(null));
    draw(side);
    wrap.append(select, extra);
    wrap.kind = () => ((state.app.indicators.find((o) => o.key === select.value) || {}).kind || "number");
    wrap.onChange = (fn) => select.addEventListener("change", fn);
    wrap.read = () => {
      if (select.value === "value") return { value: Number($("[data-role=value]", extra).value) };
      const params = {};
      $$("[data-param]", extra).forEach((input) => { params[input.dataset.param] = Number(input.value); });
      return { indicator: select.value, params };
    };
    return wrap;
  }

  const DAYS = [[1, "Mon"], [2, "Tue"], [3, "Wed"], [4, "Thu"], [5, "Fri"]];
  const hhmm = (n) => `${String(Math.floor(n / 100)).padStart(2, "0")}:${String(n % 100).padStart(2, "0")}`;

  function ruleRow(rule) {
    const left = operandControls(rule && rule.left, false);
    const op = h("select", { "aria-label": "Comparison" });
    const host = h("span", { class: "rule" });
    let right = null;
    const draw = (r) => {
      const kind = left.kind();
      const ops = kind === "time" ? state.app.rule_time_ops : kind === "weekday" ? { is_one_of: state.app.rule_ops.is_one_of }
        : Object.fromEntries(Object.entries(state.app.rule_ops).filter(([key]) => key !== "is_one_of"));
      op.replaceChildren(...Object.entries(ops).map(([key, label]) => h("option", { value: key, text: label })));
      if (r && r.op in ops) op.value = r.op;
      if (kind === "time") {
        const input = h("input", { type: "time", min: "09:00", max: "23:59", required: true, "aria-label": "Time", value: r && r.right && r.right.value != null ? hhmm(r.right.value) : "09:30" });
        right = h("span", {}, input);
        right.read = () => { const [hh, mm] = input.value.split(":").map(Number); return { value: hh * 100 + mm }; };
      } else if (kind === "weekday") {
        const chosen = r && r.right && r.right.days ? r.right.days : [1, 2, 3, 4, 5];
        right = h("span", { class: "days" }, DAYS.map(([n, label]) => h("label", { class: "check" }, h("input", { type: "checkbox", value: n, checked: chosen.includes(n) }), " " + label)));
        right.read = () => ({ days: $$("input:checked", right).map((c) => Number(c.value)) });
      } else {
        right = operandControls(r && r.right && !r.right.days ? r.right : { value: "" }, true);
      }
      host.replaceChildren(right);
    };
    left.onChange(() => draw(null));
    draw(rule);
    const row = h("div", { class: "rule" }, left, op, host, h("button", { type: "button", text: "Remove", onclick: () => row.remove() }));
    row.read = () => ({ left: left.read(), op: op.value, right: right.read() });
    return row;
  }

  function rulesEditor() {
    const matchSelect = (first) => h("select", {}, first === "all"
      ? [h("option", { value: "all", text: "ALL of these are true" }), h("option", { value: "any", text: "ANY of these is true" })]
      : [h("option", { value: "any", text: "ANY of these is true" }), h("option", { value: "all", text: "ALL of these are true" })]);
    const entryMatch = matchSelect("all"), exitMatch = matchSelect("any");
    const entry = h("div"), exit = h("div");
    const message = h("span", { class: "hint" });
    const el = h("div", { class: "rulegroups" },
      h("div", { class: "rulegroup" }, h("h4", {}, "Entry when (buy) ", entryMatch), entry,
        h("button", { type: "button", text: "+ Add rule", onclick: () => entry.append(ruleRow(null)) })),
      h("div", { class: "rulegroup" }, h("h4", {}, "Exit when (sell) ", exitMatch), exit,
        h("button", { type: "button", text: "+ Add rule", onclick: () => exit.append(ruleRow(null)) })),
      h("div", { class: "row" }, h("button", { type: "button", text: "Save these rules as a strategy", onclick: async (event) => {
        const name = window.prompt("Name for this strategy (it will appear in Backtest and Algos):");
        if (!name) return;
        busy(event.target, true, "Saving…");
        try { const saved = await api("/strategies", { method: "POST", body: { name, ...el.read() } }); await reloadStrategies(); message.textContent = `Saved “${saved.name}”.`; }
        catch (err) { message.textContent = err.message; }
        busy(event.target, false);
      } }), message));
    el.read = () => ({ entry: { match: entryMatch.value, rules: $$(":scope > .rule", entry).map((row) => row.read()) },
      exit: { match: exitMatch.value, rules: $$(":scope > .rule", exit).map((row) => row.read()) } });
    el.load = (spec) => {
      entryMatch.value = spec.entry.match; exitMatch.value = spec.exit.match;
      entry.replaceChildren(...spec.entry.rules.map(ruleRow)); exit.replaceChildren(...spec.exit.rules.map(ruleRow));
    };
    el.load({ entry: { match: "all", rules: [{ left: { indicator: "close" }, op: "crosses_above", right: { indicator: "ema", params: { period: 20 } } }] },
      exit: { match: "any", rules: [{ left: { indicator: "close" }, op: "crosses_below", right: { indicator: "ema", params: { period: 20 } } }] } });
    return el;
  }

  function loadBuilder(spec) {
    const form = $("#form-strategy");
    builder.editing = spec ? spec.id : null;
    form.name.value = spec ? spec.name : "";
    form.entry_match.value = spec ? spec.entry.match : "all"; form.exit_match.value = spec ? spec.exit.match : "any";
    $("#rules-entry").replaceChildren(...(spec ? spec.entry.rules : [null]).map(ruleRow));
    $("#rules-exit").replaceChildren(...(spec ? spec.exit.rules : [null]).map(ruleRow));
    $("button[type=submit]", form).textContent = spec ? "Save changes" : "Save strategy";
    $("#strategy-new").hidden = !spec; $("#strategy-msg").textContent = spec ? `Editing “${spec.name}”` : "";
    showError("#strategy-error", null);
  }

  async function reloadStrategies() {
    const data = await api("/strategies");
    state.app.strategies = data.strategies;
    btStrategyOptions(); strategyOptions($("#form-algo").strategy, (s) => s.key !== "buy_hold"); strategyOptions($("#form-fno").strategy, (s) => s.key !== "buy_hold" && !s.intraday_only);
    return data;
  }

  async function refreshStrategyList() {
    let data;
    try { data = await reloadStrategies(); } catch (err) { showError("#strategy-error", err); return; }
    $("#strategy-list").replaceChildren(...(data.custom.length ? data.custom.map((c) => h("div", { class: "algo" },
      h("div", { class: "head" }, h("strong", { text: c.name }), h("span", { class: "actions" },
        h("button", { type: "button", class: "primary", text: "Backtest", onclick: () => { $("#bt-what").value = "strategy"; showBacktestPane("strategy"); $("#form-bt").strategy.value = c.key; renderStrategyParams(); selectTab("backtest"); } }),
        h("button", { type: "button", text: "Edit", onclick: () => { loadBuilder(c); window.scrollTo({ top: 0, behavior: "smooth" }); } }),
        h("button", { type: "button", text: "Delete", onclick: async () => {
          if (!window.confirm(`Delete the strategy “${c.name}”?`)) return;
          try { await api("/strategies/" + c.id, { method: "DELETE" }); if (builder.editing === c.id) loadBuilder(null); } catch (err) { showError("#strategy-error", err); }
          refreshStrategyList(); } }))),
      h("dl", { class: "kv" }, h("dt", { text: "Buy when" }), h("dd", { text: c.entry_text }), h("dt", { text: "Sell when" }), h("dd", { text: c.exit_text }))))
      : [h("p", { class: "hint", text: "None yet. Build one above: for example, buy when RSI(14) is below 35 AND Close is above SMA(200); sell when RSI(14) is above 60." })]));
    $("#builtin-list").replaceChildren(table([{ label: "Strategy", get: (x) => x.name }, { label: "What it does", get: (x) => x.description },
      { label: "Candles", get: (x) => (x.intraday_only ? "intraday only" : "any") }, { label: "After", get: (x) => x.source || "standard" }],
    state.app.strategies.filter((x) => !x.custom), ""));
  }

  function startBuilder() {
    const form = $("#form-strategy");
    $("#add-entry").addEventListener("click", () => $("#rules-entry").append(ruleRow(null)));
    $("#add-exit").addEventListener("click", () => $("#rules-exit").append(ruleRow(null)));
    $("#strategy-new").addEventListener("click", () => loadBuilder(null));
    loadBuilder(null);
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form); showError("#strategy-error", null);
      const body = { name: form.name.value,
        entry: { match: form.entry_match.value, rules: $$("#rules-entry > .rule").map((row) => row.read()) },
        exit: { match: form.exit_match.value, rules: $$("#rules-exit > .rule").map((row) => row.read()) } };
      busy(button, true, "Saving…");
      try {
        const saved = await api(builder.editing ? "/strategies/" + builder.editing : "/strategies", { method: builder.editing ? "PUT" : "POST", body });
        busy(button, false); loadBuilder(null); $("#strategy-msg").textContent = `Saved “${saved.name}”. It is now in Backtest and Algos.`;
        await refreshStrategyList();
      } catch (err) { busy(button, false); showError("#strategy-error", err); }
    });
  }

  // -- scanner ----------------------------------------------------------------------------

  const SCAN_FIELDS = ["lookback", "volume_multiple", "tolerance_pct", "max_age", "stop_loss_pct", "target_pct"];
  const STATE_TEXT = { new: "new", buy_active: "buy GTT waiting", holding: "bought, stoploss placed", closed: "closed", cancelled: "cancelled",
    holding_unprotected: "bought, NO stoploss" };

  function renderScanner(view) {
    const form = $("#form-scanner");
    if (document.activeElement !== form.universe) form.universe.value = view.universe.map((s) => s.replace(/\.NS$/, "")).join(" ");
    SCAN_FIELDS.forEach((k) => { if (document.activeElement !== form[k]) form[k].value = view.params[k]; });
    $("#scanner-count").textContent = `${view.universe.length} symbols`;
    $("#scanner-live").checked = view.live;
    const scan = view.last_scan;
    $("#scanner-when").textContent = scan ? `Last scan ${scan.time_ist} IST: ${scan.scanned} stocks, ${scan.signals.length} signal${scan.signals.length === 1 ? "" : "s"}${scan.failed.length ? `, ${scan.failed.length} without data` : ""}.` : "Not scanned yet. The first scan downloads each stock's history and takes a few minutes.";
    const fresh = scan ? scan.signals.filter((x) => x.state === "new") : [];
    const place = $("#scanner-place");
    place.disabled = !fresh.length;
    place.textContent = fresh.length ? `Place ${fresh.length} buy GTT${fresh.length === 1 ? "" : "s"} (${state.mode})` : "Place buy GTTs";
    place.className = state.mode === "live" ? "danger" : "primary";
    $("#scanner-signals").replaceChildren(scan ? table([
      { label: "Stock", get: (x) => x.symbol.replace(/\.NS$/, "") }, { label: "Breakout", get: (x) => x.breakout_date },
      { label: "Age (days)", num: true, get: (x) => x.age }, { label: "Volume ×", num: true, get: (x) => x.volume_multiple.toFixed(1) },
      { label: "Breakout high", num: true, get: (x) => inr2.format(x.breakout_high) }, { label: "Last close", num: true, get: (x) => inr2.format(x.last_close) },
      { label: "Buy trigger", num: true, get: (x) => inr2.format(x.buy_trigger) }, { label: "Stoploss", num: true, get: (x) => (x.stop_loss ? inr2.format(x.stop_loss) : "–") },
      { label: "Target", num: true, get: (x) => (x.target ? inr2.format(x.target) : "–") }, { label: "Status", get: (x) => STATE_TEXT[x.state] || x.state.replace(/_/g, " ") },
    ], scan.signals, "No stock in the list has a breakout that is still holding. That is normal on most days.") : h("p", { class: "hint", text: "Run a scan to see signals." }));
    if (scan && scan.failed.length) $("#scanner-signals").append(h("p", { class: "hint", text: "No price data for: " + scan.failed.map((f) => f.symbol.replace(/\.NS$/, "")).join(", ") + ". Remove or correct these symbols." }));
    const rows = view.trades;
    $("#scanner-trades").replaceChildren(rows.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["Stock", "Breakout", "Mode", "Qty", "Buy trigger", "Stoploss", "Status", "Note", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.map((t) => h("tr", {}, h("td", { text: t.symbol.replace(/\.NS$/, "") }), h("td", { text: t.breakout_date }), h("td", { text: t.mode }),
        h("td", { class: "num", text: t.quantity }), h("td", { class: "num", text: inr2.format(t.buy_trigger) }), h("td", { class: "num", text: t.stop_loss ? inr2.format(t.stop_loss) : "–" }),
        h("td", { text: STATE_TEXT[t.state] || t.state.replace(/_/g, " ") }), h("td", { style: "white-space:normal;min-width:220px", text: t.note }),
        h("td", {}, ["buy_active", "holding"].includes(t.state)
          ? h("button", { type: "button", text: "Cancel", onclick: () => scannerAction("/scanner/cancel", t, `Cancel the open GTTs for ${t.symbol}? Shares already bought are not sold.`) })
          : h("button", { type: "button", text: "Remove", onclick: () => scannerAction("/scanner/forget", t) }))))))
      : h("p", { class: "hint", text: "Nothing placed yet." }));
  }

  async function scannerAction(path, trade, question) {
    if (question && !window.confirm(question)) return;
    try { renderScanner(await api(path, { method: "POST", body: { key: trade.key } })); } catch (err) { showError("#scanner-error", err); }
  }

  async function refreshScanner() {
    try { renderScanner(await api("/scanner")); } catch (err) { showError("#scanner-error", err); }
  }

  function startScanner() {
    const form = $("#form-scanner");
    form.addEventListener("submit", async (event) => {
      event.preventDefault();
      const button = $("button[type=submit]", form); showError("#scanner-error", null);
      const params = {}; SCAN_FIELDS.forEach((k) => { params[k] = Number(form[k].value); });
      busy(button, true, "Scanning…");
      try {
        await api("/scanner", { method: "PUT", body: { universe: form.universe.value, params } });
        const { job } = await api("/scanner/scan", { method: "POST" });
        $("#scanner-job").hidden = false; $("#scanner-log").textContent = "";
        watchJob(job.id, { title: $("#scanner-title"), status: $("#scanner-status"), log: $("#scanner-log") }, () => { busy(button, false); refreshScanner(); });
      } catch (err) { showError("#scanner-error", err); busy(button, false); }
    });
    $("#scanner-reset").addEventListener("click", async () => {
      try { form.universe.blur(); renderScanner(await api("/scanner", { method: "PUT", body: { reset_universe: true } })); } catch (err) { showError("#scanner-error", err); }
    });
    $("#scanner-live").addEventListener("change", async (event) => {
      try { renderScanner(await api("/scanner/live", { method: "POST", body: { live: event.target.checked } })); } catch (err) { showError("#scanner-error", err); }
    });
    $("#scanner-place").addEventListener("click", async (event) => {
      showError("#scanner-error", null);
      const body = { mode: state.mode };
      if (state.mode === "live") {
        const typed = window.prompt("This places real GTT buy orders on Zerodha for every new signal, sized by your Settings.\n\nType  PLACE LIVE GTT  to continue.");
        if (typed == null) return;
        body.phrase = typed;
      }
      busy(event.target, true, "Placing…");
      try {
        const out = await api("/scanner/place", { method: "POST", body });
        $("#scanner-result").replaceChildren(h("div", { class: "warnbox", text: out.results.map((r) => `${r.symbol.replace(/\.NS$/, "")}: ${r.outcome}${r.gtt ? ` (GTT ${r.gtt}, qty ${r.quantity})` : ""}${r.reason ? ` — ${r.reason}` : ""}`).join("\n") || "Nothing to place.", style: "white-space:pre-wrap" }));
      } catch (err) { showError("#scanner-error", err); }
      busy(event.target, false); refreshScanner();
    });
    $$("#mode button").forEach((b) => b.addEventListener("click", () => { if (!$("#tab-scanner").hidden) refreshScanner(); if (!$("#tab-trade").hidden) refreshGtts(); if (!$("#tab-pnl").hidden) refreshPnl(); if (!$("#tab-fno").hidden) refreshOptionPositions(); if (!$("#tab-mcx").hidden) refreshMcxPositions(); }));
    setInterval(() => { if (!$("#tab-scanner").hidden && $("#scanner-live").checked) refreshScanner(); }, 20000);
  }

  // -- GTT ---------------------------------------------------------------------------------

  async function refreshGtts() {
    $("#gtt-mode").textContent = state.mode === "live" ? "on Zerodha" : "paper";
    let rows = [];
    try { if (state.mode === "paper" || state.app.kite.connected) rows = (await api("/gtt?mode=" + state.mode)).gtts; }
    catch (err) { $("#gtt-list").replaceChildren(h("p", { class: "hint", text: err.message })); return; }
    const mode = state.mode;
    $("#gtt-list").replaceChildren(rows.length ? h("table", {},
      h("thead", {}, h("tr", {}, ["GTT", "Stock", "Side", "Qty", "Triggers at", "Status", ""].map((t) => h("th", { text: t })))),
      h("tbody", {}, rows.map((g) => {
        const levels = g.triggers ? g.triggers.map((v) => inr2.format(v)).join(" / ")
          : g.kind === "oco" ? `stop ${inr2.format(g.stop)} / target ${inr2.format(g.target)}` : inr2.format(g.trigger);
        return h("tr", {}, h("td", { text: g.id }), h("td", { text: g.tradingsymbol }), h("td", { text: g.side }), h("td", { class: "num", text: g.quantity }),
          h("td", { class: "num", text: levels }), h("td", { text: g.status + (g.leg ? ` (${g.leg})` : "") }),
          h("td", {}, g.status === "active" ? h("button", { type: "button", text: "Cancel", onclick: async () => {
            if (!window.confirm(`Cancel GTT ${g.id}?`)) return;
            try { await api("/gtt/cancel", { method: "POST", body: { mode, id: g.id } }); } catch (err) { showError("#gtt-error", err); }
            refreshGtts(); } }) : null));
      }))) : h("p", { class: "hint", text: "No GTTs." }));
  }

  function renderGttReview(review) {
    const box = $("#gtt-review"); box.hidden = false;
    const g = review.gtt, live = review.mode === "live";
    const parts = [h("h3", { text: `${live ? "LIVE" : "Paper"} GTT: ${g.side} ${g.quantity} ${g.exchange}:${g.tradingsymbol}` }),
      h("dl", { class: "kv" },
        h("dt", { text: "Triggers" }), h("dd", { text: g.kind === "oco" ? `target ${rupees(g.target, 2)}, stoploss ${rupees(g.stop, 2)} (one cancels the other)` : `at ${rupees(g.trigger, 2)}, limit ${rupees(g.limit, 2)}` }),
        h("dt", { text: "Last price" }), h("dd", { text: review.last_price ? `${rupees(review.last_price, 2)} (${review.last_price_source})` : review.last_price_source }),
        h("dt", { text: "Order value" }), h("dd", { text: rupees(review.value, 2) }))];
    review.warnings.forEach((w) => parts.push(h("div", { class: "warnbox", text: w })));
    if (!review.ok) {
      parts.push(h("div", { class: "error", text: "This GTT cannot be placed:\n• " + review.errors.join("\n• ") }));
      box.replaceChildren(h("div", { class: "confirm" + (live ? " live" : "") }, parts)); return;
    }
    let token = review.token;
    const input = h("input", { type: "text", autocomplete: "off", placeholder: review.phrase, "aria-label": "Confirmation phrase" });
    const send = h("button", { class: live ? "danger" : "primary", text: live ? "Place LIVE GTT" : "Place paper GTT", disabled: true });
    const result = h("div", {});
    input.addEventListener("input", () => { send.disabled = input.value.trim().toUpperCase().replace(/\s+/g, " ") !== review.phrase; });
    send.addEventListener("click", async () => {
      if (!token) return;
      const use = token; token = null; send.disabled = true; input.disabled = true;
      try {
        const placed = await api("/order/place", { method: "POST", body: { token: use, phrase: input.value } });
        result.replaceChildren(h("div", { class: "warnbox", text: `GTT ${placed.order_id} placed. It waits for its trigger; see the list below.` }));
      } catch (err) { result.replaceChildren(h("div", { class: "error", text: "Not placed: " + err.message })); }
      refreshGtts(); refreshJournal();
    });
    parts.push(h("p", {}, "Type ", h("code", { text: review.phrase }), ` to confirm. Valid for ${review.expires_in} seconds.`), h("div", { class: "row" }, input, send), result);
    box.replaceChildren(h("div", { class: "confirm" + (live ? " live" : "") }, parts));
  }

  async function reviewGtt() {
    const form = $("#form-gtt");
    showError("#gtt-error", null); $("#gtt-review").hidden = true;
    try {
      const resolved = await api("/symbol?q=" + encodeURIComponent(form.symbol.value));
      if (!resolved.kite) throw new Error(`${resolved.symbol} is not an NSE/BSE equity.`);
      const oco = form.kind.value === "oco";
      renderGttReview(await api("/gtt/preview", { method: "POST", body: { mode: state.mode, gtt: {
        exchange: resolved.kite.exchange, tradingsymbol: resolved.kite.tradingsymbol, side: oco ? "SELL" : form.side.value,
        quantity: Number(form.quantity.value), kind: form.kind.value, trigger: Number(form.trigger.value || 0), limit: Number(form.limit.value || 0),
        target: Number(form.target.value || 0), stop: Number(form.stop.value || 0) } } }));
    } catch (err) {
      const reasons = err.payload && err.payload.errors;
      showError("#gtt-error", reasons ? new Error(reasons.join("\n")) : err);
    }
  }

  function startGtt() {
    const form = $("#form-gtt");
    const shape = () => { const oco = form.kind.value === "oco"; $("#gtt-single").hidden = oco; $("#gtt-oco").hidden = !oco; form.side.disabled = oco; if (oco) form.side.value = "SELL"; };
    form.kind.addEventListener("change", shape); shape();
    form.addEventListener("input", () => { $("#gtt-review").hidden = true; });
    form.addEventListener("submit", (event) => { event.preventDefault(); reviewGtt(); });
  }

  // After a delivery buy with a target or stoploss asked for: prepare the GTT for confirmation.
  function protectAfter(order, price) {
    const ticket = $("#form-order"), target = Number(ticket.target_pct.value || 0), stop = Number(ticket.stop_loss_pct.value || 0);
    if (order.side !== "BUY" || order.product !== "CNC" || !(target || stop) || !price) return;
    const round = (v) => Math.round(v / 0.05) * 0.05;
    const form = $("#form-gtt");
    form.symbol.value = order.tradingsymbol + (order.exchange === "BSE" ? ".BO" : ""); form.quantity.value = order.quantity;
    if (target && stop) { form.kind.value = "oco"; form.target.value = round(price * (1 + target / 100)).toFixed(2); form.stop.value = round(price * (1 - stop / 100)).toFixed(2); }
    else { form.kind.value = "single"; form.side.value = "SELL"; form.trigger.value = round(price * (target ? 1 + target / 100 : 1 - stop / 100)).toFixed(2); form.limit.value = ""; }
    form.kind.dispatchEvent(new Event("change"));
    reviewGtt().then(() => $("#form-gtt").scrollIntoView({ behavior: "smooth", block: "center" }));
  }

  // -- trade journal and your results ------------------------------------------------------

  const signedInr = (v) => (v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + "₹" + inr.format(Math.abs(v)));
  const journalView = { data: null, group: "setup" };

  async function refreshJournalPanel() {
    let d;
    try { d = await api("/journal?mode=" + state.mode); }
    catch (err) { showError("#journal-error", err); return; }
    showError("#journal-error", null);
    journalView.data = d;
    $("#setup-list").replaceChildren(...d.setups.map((s) => h("option", { value: s })));
    renderResults(); renderJournal();
  }

  function renderResults() {
    const d = journalView.data, o = d.results.overall, min = d.results.min_trades;
    const tile = (k, v, sub, cls) => h("div", { class: "tile" }, h("div", { class: "k", text: k }), h("div", { class: "v " + (cls || ""), text: v }), sub ? h("div", { class: "s", text: sub }) : null);
    let sentence;
    if (!o.trades) sentence = "No closed trades on this account yet. Your results appear here once trades are bought and sold.";
    else {
      sentence = `${o.trades} closed trade${o.trades === 1 ? "" : "s"}: ${signedInr(o.expectancy)} a trade on average, ${o.win_rate}% of them winners.`;
      const range = o.range95 ? `${signedInr(o.range95[0])} to ${signedInr(o.range95[1])}` : null;
      if (o.trades < min) sentence += ` That is too few to tell skill from luck: ${min - o.trades} more before reading much into it.`;
      else if (o.edge === "positive") sentence += ` The likely range of the average (95%), ${range}, is above zero: so far this looks like a real edge.`;
      else if (o.edge === "negative") sentence += ` The likely range of the average (95%), ${range}, is below zero: on this record the way you trade loses money.`;
      else if (range) sentence += ` The likely range of the average (95%), ${range}, includes zero: the trades so far cannot tell an edge from luck.`;
    }
    $("#results-sentence").textContent = sentence;
    $("#results-tiles").replaceChildren(...(o.trades ? [
      tile("Trades", String(o.trades), o.enough ? "closed" : `closed · ${min} needed to judge`),
      tile("Win rate", o.win_rate + "%", "trades in profit"),
      tile("Average win", signedInr(o.avg_win), null, "pos"),
      tile("Average loss", signedInr(o.avg_loss), o.payoff ? `wins are ${o.payoff}× the losses` : null, "neg"),
      tile("Expectancy", signedInr(o.expectancy), "average result a trade", tone(o.expectancy)),
      tile("Profit factor", o.profit_factor == null ? "–" : o.profit_factor.toFixed(2), "won ÷ lost"),
      tile("Expectancy in R", o.expectancy_r == null ? "–" : o.expectancy_r.toFixed(2) + "R", o.r_trades ? `${o.r_trades} trades with a planned stoploss` : "write stoplosses in the journal", tone(o.expectancy_r)),
      tile("Worst losing streak", String(o.losing_streak), "losses in a row"),
      tile("Largest fall", signedInr(o.max_drawdown), "from a peak, trade by trade", tone(o.max_drawdown))] : []));
    const select = $("#results-group");
    if (!select.options.length) {
      select.replaceChildren(...Object.entries(d.results.groups).map(([k, label]) => h("option", { value: k, text: label })));
      select.value = journalView.group;
    }
    const rows = d.results.by[journalView.group] || [];
    $("#results-table").replaceChildren(o.trades ? table([
      { label: d.results.groups[journalView.group], get: (r) => (journalView.group === "source" ? sourceLabel(r.name) : r.name) },
      { label: "Trades", num: true, get: (r) => r.trades }, { label: "Win rate", num: true, get: (r) => r.win_rate + "%" },
      { label: "Average win", num: true, get: (r) => signedInr(r.avg_win) }, { label: "Average loss", num: true, get: (r) => signedInr(r.avg_loss) },
      { label: "Expectancy", num: true, get: (r) => signedInr(r.expectancy), tone: (r) => tone(r.expectancy) },
      { label: "Total", num: true, get: (r) => signedInr(r.total), tone: (r) => tone(r.total) },
      { label: "Enough trades?", get: (r) => (r.enough ? "Yes" : `No, ${r.trades} of ${min}`), tone: (r) => (r.enough ? "" : "muted") }],
      rows, "Nothing to group yet.") : h("p", { class: "hint", text: "" }));
    const other = d.other, name = other.mode === "live" ? "Live" : "Paper";
    $("#results-other").textContent = other.trades
      ? `${name} account for comparison: ${other.trades} closed trades, ${other.win_rate}% winners, ${signedInr(other.expectancy)} a trade on average.`
      : `No closed trades on the ${name.toLowerCase()} account to compare with.`;
  }
  $("#results-group").addEventListener("change", (event) => { journalView.group = event.target.value; renderResults(); });

  function renderJournal() {
    const d = journalView.data;
    const review = (t) => {
      const n = t.note;
      if (!n) return h("span", { class: "pill", text: "No note" });
      if (n.followed_plan || n.lesson || (n.mistakes || []).length) return h("span", { class: "pill " + (n.followed_plan === "no" || (n.mistakes || []).length ? "warn" : "good"), text: "Reviewed" });
      return h("span", { class: "pill", text: t.status === "Open" ? "Plan written" : "Review it" });
    };
    const reviewed = d.trades.filter((t) => t.note && (t.note.followed_plan || t.note.lesson)).length;
    $("#journal-count").textContent = d.trades.length ? `${reviewed} of ${d.trades.length} trades reviewed` : "";
    $("#journal-trades").replaceChildren(table([
      { label: "Opened (IST)", get: (t) => t.opened_at }, { label: "Stock / contract", get: (t) => t.symbol },
      { label: "From", get: (t) => sourceLabel(t.source) }, { label: "Side", get: (t) => (t.side === "SHORT" ? "Short" : "Long") },
      { label: "Qty", num: true, get: (t) => t.quantity }, { label: "Entry ₹", num: true, get: (t) => inr2.format(t.entry) },
      { label: "Exit ₹", num: true, get: (t) => (t.exit == null ? "open" : inr2.format(t.exit)) },
      { label: "Profit/Loss", num: true, get: (t) => signedInr(t.pnl), tone: (t) => tone(t.pnl) },
      { label: "R", num: true, get: (t) => (t.r == null ? "–" : t.r.toFixed(2) + "R"), tone: (t) => tone(t.r) },
      { label: "Setup", get: (t) => (t.note && t.note.setup) || "–" }, { label: "Review", get: review },
      { label: "", get: (t) => h("button", { type: "button", text: t.note ? "Edit note" : "Write note", onclick: () => openNote(t) }) }],
      d.trades, "No trades on this account yet."));
    $("#journal-pending-box").hidden = !d.pending.length;
    $("#journal-pending").replaceChildren(table([
      { label: "Written (IST)", get: (n) => n.created }, { label: "Stock / contract", get: (n) => n.symbol }, { label: "Setup", get: (n) => n.setup || "–" },
      { label: "Stoploss ₹", num: true, get: (n) => (n.stop ? inr2.format(n.stop) : "–") }, { label: "Why", get: (n) => n.reason || "–" },
      { label: "", get: (n) => h("button", { type: "button", text: "Delete", onclick: async () => {
        if (!window.confirm(`Delete the plan for ${n.symbol}?`)) return;
        try { await api("/journal/" + encodeURIComponent(n.id), { method: "DELETE" }); refreshJournalPanel(); } catch (err) { showError("#journal-error", err); } } }) }],
      d.pending, ""));
  }

  const noteView = { trade: null };
  function openNote(trade) {
    noteView.trade = trade;
    const form = $("#form-note"), n = trade.note || {};
    $("#nd-title").textContent = (n.id ? "Edit note: " : "Write a note: ") + trade.symbol;
    $("#nd-trade").textContent = `${trade.side === "SHORT" ? "Sold" : "Bought"} ${trade.quantity} ${trade.symbol} at ₹${inr2.format(trade.entry)} on ${trade.opened_at}`
      + (trade.exit != null ? `, closed at ₹${inr2.format(trade.exit)} for ${signedInr(trade.pnl)}.` : ". Still open.");
    form.setup.value = n.setup || ""; form.reason.value = n.reason || ""; form.stop.value = n.stop || ""; form.target.value = n.target || "";
    form.followed_plan.value = n.followed_plan || ""; form.lesson.value = n.lesson || "";
    $("#nd-mistakes").replaceChildren(...journalView.data.mistakes.map((m) => h("label", { class: "check" },
      h("input", { type: "checkbox", value: m, checked: (n.mistakes || []).includes(m) }), m)));
    $("#nd-delete").hidden = !n.id; showError("#nd-error", null);
    $("#note-dialog").showModal();
  }
  $("#nd-close").addEventListener("click", () => $("#note-dialog").close());
  $("#form-note").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.target, t = noteView.trade, n = t.note || {}, button = $("button[type=submit]", form);
    const body = { setup: form.setup.value, reason: form.reason.value, stop: Number(form.stop.value || 0) || null,
      target: Number(form.target.value || 0) || null, followed_plan: form.followed_plan.value, lesson: form.lesson.value,
      mistakes: $$("#nd-mistakes input:checked").map((b) => b.value) };
    busy(button, true, "Saving…");
    try {
      if (n.id) await api("/journal/" + encodeURIComponent(n.id), { method: "PUT", body });
      else await api("/journal", { method: "POST", body: { ...body, mode: t.mode, exchange: t.exchange, symbol: t.symbol, source: t.source, key: t.key } });
      $("#note-dialog").close(); refreshJournalPanel();
    } catch (err) { showError("#nd-error", err); }
    busy(button, false);
  });
  $("#nd-delete").addEventListener("click", async () => {
    const n = noteView.trade.note;
    if (!n || !window.confirm("Delete this note? The trade stays; only your note is removed.")) return;
    try { await api("/journal/" + encodeURIComponent(n.id), { method: "DELETE" }); $("#note-dialog").close(); refreshJournalPanel(); }
    catch (err) { showError("#nd-error", err); }
  });

  // -- size by risk on the order ticket ---------------------------------------------------------

  async function ticketEntry(form) {
    const price = Number(form.price.value || 0);
    if (price) return price;
    const q = await api("/quote?symbol=" + encodeURIComponent(form.symbol.value));
    return q.last_price || null;
  }

  // The stoploss price the ticket plans: typed as a price, or worked out from "Then stoploss %".
  function ticketStop(form, entry) {
    const typed = Number(form.stop_price.value || 0);
    if (typed) return typed;
    const pctStop = Number(form.stop_loss_pct.value || 0);
    return pctStop && entry ? Math.round(entry * (1 - pctStop / 100) / 0.05) * 0.05 : null;
  }

  $("#order-size").addEventListener("click", async (event) => {
    const form = $("#form-order"), out = $("#order-size-out");
    showError("#order-error", null); out.textContent = "Working it out…"; busy(event.target, true, "Working…");
    try {
      const resolved = await api("/symbol?q=" + encodeURIComponent(form.symbol.value));
      if (!resolved.kite) throw new Error(`${resolved.symbol} is not an NSE/BSE equity that can be ordered.`);
      const entry = await ticketEntry(form);
      if (!entry) throw new Error("No price to size from. Enter a limit price.");
      const r = await api("/order/size", { method: "POST", body: { entry, stop: Number(form.stop_price.value || 0) || null,
        target: Number(form.target_price.value || 0) || null, risk_pct: Number(form.risk_pct.value || 0) || null, side: "BUY",
        exchange: resolved.kite.exchange, tradingsymbol: resolved.kite.tradingsymbol, product: form.product.value } });
      if (!r.quantity) { out.textContent = r.note; busy(event.target, false); return; }
      form.side.value = "BUY"; form.quantity.value = r.quantity; form.stop_loss_pct.value = r.stop_loss_pct;
      if (r.target_pct) form.target_pct.value = r.target_pct;
      clearReview();
      let text = `Buy ${inr.format(r.quantity)} at about ₹${inr2.format(entry)} (₹${inr.format(r.value)}). If the stoploss ₹${inr2.format(Number(form.stop_price.value))} is hit, you lose about ₹${inr.format(r.risk)}: ${r.risk_of_capital_pct}% of ₹${inr.format(r.capital)}, charges included.`;
      if (r.limited_by.length) text += ` Fewer than the ${inr.format(r.by_risk)} the risk allows, because of ${r.limited_by.join(" and ")}.`;
      if (r.reward != null) text += ` At the target you gain about ₹${inr.format(r.reward)}, ${r.reward_to_risk} times the risk: this pays over many trades only if more than ${r.breakeven_win_rate_pct}% of them reach the target.`;
      out.textContent = text + " A price that gaps through the stoploss loses more.";
    } catch (err) { out.textContent = ""; showError("#order-error", err); }
    busy(event.target, false);
  });

  // After a confirmed buy from the ticket: keep its plan in the trade journal.
  async function savePlan(order, mode, entry) {
    const form = $("#form-order");
    if (order.side !== "BUY") return null;
    const stop = ticketStop(form, entry), targetPct = Number(form.target_pct.value || 0);
    const target = Number(form.target_price.value || 0) || (targetPct && entry ? Math.round(entry * (1 + targetPct / 100) / 0.05) * 0.05 : null);
    if (!form.setup.value.trim() && !form.reason.value.trim() && !stop) return null;
    await api("/journal", { method: "POST", body: { mode, exchange: order.exchange, symbol: order.tradingsymbol, source: "manual",
      setup: form.setup.value, reason: form.reason.value, stop, target } });
    return true;
  }

  // -- explainers: a "?" on every tile the glossary knows -----------------------------------------

  const tileNumber = (text) => { const v = parseFloat(String(text).replace(/[₹,%R×\s+]/g, "").replace("−", "-")); return Number.isFinite(v) ? v : null; };
  const lakh = (v) => "₹" + inr.format(Math.round(v));
  const MANUAL = { backtest: "/manual#reading-the-result", results: "/manual#learn-from-your-trades", pnl: "/manual#pl-and-alerts", risk: "/manual#risk-per-trade" };
  const EXPLAIN = {
    "total return": { t: (v) => `The gain or loss over the whole period, after charges: ${v}. Compare it with Buy & hold under it. If simply holding did better, the strategy added work, not money.`, link: MANUAL.backtest },
    "cagr": { t: (v, n) => `Growth per year, compounded.` + (n != null && n > -100 ? ` At ${v} a year, ₹1,00,000 becomes ${lakh(100000 * Math.pow(1 + n / 100, 5))} in 5 years.` : ""), link: MANUAL.backtest },
    "max drawdown": { t: (v, n) => `The worst fall from a peak to a low: ${v}.` + (n != null && Math.abs(n) < 100 ? ` To get back from a fall of ${Math.abs(n).toFixed(1)}% needs a gain of ${((1 / (1 - Math.abs(n) / 100) - 1) * 100).toFixed(1)}%. Ask yourself whether you would have kept trading through it.` : ""), link: MANUAL.backtest },
    "sharpe": { t: (v, n) => `Return above the risk-free rate (Settings) for each unit of ups and downs, per year.` + (n == null ? "" : n < 0 ? ` ${v} is below 0: a safe deposit did better.` : n < 1 ? ` ${v}: some return, but a lot of swing for it.` : n < 2 ? ` ${v}: a good return for the swing.` : ` ${v} is unusually high: check for too few trades or settings fitted to this period.`) + " Sortino counts only the falls.", link: MANUAL.backtest },
    "win rate": { t: (v) => `${v} of trades made money. On its own it says little: winning 40% of trades pays if the average win is well above the average loss, and winning 80% loses money if one loss wipes out many wins. Read it with Profit factor and the average win and loss.`, link: MANUAL.results },
    "profit factor": { t: (v, n) => (n == null ? "Money won ÷ money lost." : `₹${n.toFixed(2)} won for every ₹1 lost.`) + " Under 1 loses money. About 1.5 or more after charges is healthy; very high values from a few trades are usually luck.", link: MANUAL.backtest },
    "final value": { t: (v) => `What the capital grew (or shrank) to: ${v}, after every charge.`, link: MANUAL.backtest },
    "charges paid": { t: (v) => `Brokerage, STT, stamp duty, exchange fees, SEBI fee, GST and DP charges: ${v}. They are certain, the profit is not. Many small trades make this large.`, link: MANUAL.backtest },
    "re-entries": { t: () => "Buys made again after a stoploss closed the trade, as your exit rules allow.", link: "/manual#exit-rules-stoploss-target-trailing-re-entry" },
    "trades": { t: (v, n) => `Closed trades counted: ${v}.` + (n != null && n < 30 ? " Under about 30 the averages can be luck: a few big trades decide them." : " Enough to read the averages, if they cover rising and falling markets."), link: MANUAL.results },
    "average win": { t: (v) => `The average profit of a winning trade: ${v}. Compare it with the average loss: the bigger the gap, the lower the win rate you need.`, link: MANUAL.results },
    "average loss": { t: (v) => `The average loss of a losing trade: ${v}. A stoploss that you keep holds this steady; a stoploss you move makes it grow.`, link: MANUAL.results },
    "expectancy": { t: (v, n) => `The average result of one trade: ${v}.` + (n != null ? ` Over 100 such trades that is about ${signedInr(n * 100)}.` : "") + " It is the number that decides whether a way of trading pays: win rate × average win minus loss rate × average loss.", link: MANUAL.results },
    "expectancy in r": { t: (v, n) => `The average result in units of the risk you planned (R: entry to stoploss).` + (n != null ? ` ${v} means each ₹1,000 risked came back as about ${signedInr(n * 1000)} on average.` : "") + " It compares trades of different sizes fairly.", link: MANUAL.results },
    "worst losing streak": { t: (v) => `The most losses in a row: ${v}. Even with half your trades winning, a run of 6 or 7 losses somewhere in 100 trades is normal. Size each trade so that such a run does not stop you.`, link: MANUAL.risk },
    "largest fall": { t: (v) => `The biggest drop from a high point of your booked profit/loss: ${v}. It is what you have to sit through.`, link: MANUAL.pnl },
    "total profit/loss": { t: () => "Booked profit/loss from closed trades plus the open profit/loss of what you still hold, at current prices.", link: MANUAL.pnl },
    "booked (closed trades)": { t: () => "Profit/loss of trades bought and sold, after all charges. This is money made or lost.", link: MANUAL.pnl },
    "still holding": { t: () => "Profit/loss of positions not closed yet, at the current price. It changes until you sell.", link: MANUAL.pnl },
    "today": { t: () => "Booked today plus the change today in positions opened today. The daily loss limit in Settings watches this number.", link: MANUAL.pnl },
    "best day": { t: (v) => `The best single day of booked profit/loss: ${v}.`, link: MANUAL.pnl },
    "worst day": { t: (v) => `The worst single day of booked profit/loss: ${v}. If one day can undo many good ones, trades are too big.`, link: MANUAL.pnl },
    "daily loss limit": { t: () => "When today's loss on an account reaches this, the kill switch turns on and intraday algos close their positions. Set it in Settings.", link: "/manual#daily-loss-limit" },
  };

  const pop = h("div", { class: "xp-pop", id: "xp-pop", popover: "auto", role: "dialog" });
  document.body.append(pop);
  function showExplainer(button, entry, label, value) {
    pop.replaceChildren(h("strong", { text: label }), h("div", { text: entry.t(value, tileNumber(value)) }),
      entry.link ? h("a", { href: entry.link, target: "_blank", rel: "noopener", text: "Learn more in the manual" }) : null);
    const r = button.getBoundingClientRect();
    Object.assign(pop.style, { position: "fixed", margin: "0", inset: "auto", left: Math.max(16, Math.min(r.left, window.innerWidth - 436)) + "px", top: Math.min(r.bottom + 6, window.innerHeight - 200) + "px" });
    if (pop.showPopover) { try { pop.showPopover(); } catch (err) { /* already open */ } } else pop.hidden = false;
  }
  function addExplainers() {
    $$(".tile:not([data-xp])").forEach((tile) => {
      tile.dataset.xp = "1";
      const k = $(".k", tile);
      if (!k) return;
      const label = k.textContent.trim(), entry = EXPLAIN[label.toLowerCase()];
      if (!entry) return;
      k.append(h("button", { type: "button", class: "xp", text: "?", "aria-label": "What " + label + " means",
        onclick: (event) => { event.stopPropagation(); showExplainer(event.currentTarget, entry, label, ($(".v", tile) || {}).textContent || ""); } }));
    });
  }
  let xpQueued = false;
  new MutationObserver(() => { if (!xpQueued) { xpQueued = true; requestAnimationFrame(() => { xpQueued = false; addExplainers(); }); } })
    .observe(document.body, { childList: true, subtree: true });

  // -- backtest: can you trust it? -------------------------------------------------------------

  function renderChecks(r) {
    const box = $("#bt-checks"), c = r.metrics && r.metrics.checks;
    box.hidden = !c;
    if (!c) return;
    const item = (cls, text) => h("li", { class: cls, text });
    const capital = r.params && r.params.capital, final = r.metrics.final_value;
    const items = [
      c.few_trades
        ? item("warn", `Only ${c.trades} closed trade${c.trades === 1 ? "" : "s"}. Under ${c.min_trades}, the result can be luck: test a longer period or other stocks before trusting it.`)
        : item("ok", `${c.trades} closed trades: enough to read the averages, if the period has both rising and falling markets.`),
      c.beats_safe
        ? item("ok", `Beat a safe ${c.safe_rate_pct}% a year (the risk-free rate in Settings). ${rupees(capital)} at that rate would be ${rupees(c.safe_final)}; this ended at ${rupees(final)}.`)
        : item("warn", `Did not beat a safe ${c.safe_rate_pct}% a year (the risk-free rate in Settings). ${rupees(capital)} at that rate would be ${rupees(c.safe_final)}; this ended at ${rupees(final)}. The risk bought nothing.`)];
    const mc = c.monte_carlo;
    if (mc) {
      items.push(item(mc.loss_chance_pct >= 20 ? "warn" : "",
        `The same trades in ${inr.format(mc.runs)} random orders: the worst fall was ${Math.abs(mc.drawdown_pct.median).toFixed(1)}% in a typical run and ${Math.abs(mc.drawdown_pct.bad).toFixed(1)}% or deeper in 1 run in 20. `
        + `The final value ranged from ${rupees(mc.final.low)} to ${rupees(mc.final.high)} (9 runs in 10). ${mc.loss_chance_pct}% of runs lost money; ${mc.below_safe_pct}% ended below the safe rate.`));
    }
    items.push(item("", "Survivorship: a list of stocks chosen today leaves out companies that dropped out of the index or were delisted, so a test on today's index members looks better than trading through those years would have been."));
    box.replaceChildren(h("h3", { text: "Can you trust this result?" }), h("ul", { class: "checklist" }, items),
      h("p", { class: "hint", text: "Random orders: the closed trades are drawn again at random, with repeats, keeping each trade's rupee result. It shows how much of the result depends on the order the trades happened to come in. It cannot show what the strategy would do in a market unlike this period." }));
  }

  // -- start -------------------------------------------------------------------

  (async () => {
    try { await loadState(); }
    catch (err) { $("#banner").textContent = err.message; $("#banner").hidden = false; return; }
    const today = state.app.today, back = new Date(today); back.setFullYear(back.getFullYear() - 3);
    const iso = (d) => d.toISOString().slice(0, 10);
    $("#form-analyze").date.value = today; $("#form-analyze").date.max = today;
    const bt = $("#form-bt");
    $("#bt-from").value = iso(back); $("#bt-to").value = today; $("#bt-to").max = today; $("#bt-capital").value = state.app.settings.paper_capital;
    btStrategyOptions(); intervalOptions(bt.interval);
    bt.strategy.value = "supertrend";
    $("#bt-rules").replaceWith(Object.assign(rulesEditor(), { id: "bt-rules", hidden: true }));
    $("#bt-exits").replaceWith(Object.assign(exitRulesBox(false), { id: "bt-exits" }));
    ["trade_start", "trade_end", "square_off"].forEach((k) => { bt[k].value = state.app.settings[k]; });
    bt.strategy.addEventListener("change", renderStrategyParams); renderStrategyParams();
    bt.interval.addEventListener("change", () => {       // intraday history is short: start where the data starts
      const kite = state.app.kite.connected;      // Kite keeps years of intraday candles; the free feed, days
      const days = (kite ? { "1d": 365 * 3, "1h": 365 * 2, "30m": 365, "15m": 365, "5m": 180, "4m": 60, "3m": 90, "2m": 60, "1m": 30 }
        : { "1d": 365 * 3, "1h": 365, "30m": 55, "15m": 55, "5m": 55, "4m": 6, "3m": 6, "2m": 55, "1m": 6 })[bt.interval.value];
      const from = new Date(today); from.setDate(from.getDate() - days); $("#bt-from").value = iso(from); syncSharedDates();
      syncStockForm();
    });
    syncStockForm();
    const bta = $("#form-bta"), month = new Date(today); month.setDate(month.getDate() - 60);
    bta.start.value = iso(month); bta.end.value = today; bta.end.max = today;
    $("#form-order").product.value = state.app.settings.product;
    $("#form-order").risk_pct.value = state.app.settings.risk_per_trade_pct;
    fillSettings(); setMode("paper"); startSectors(); startCharts(); startSecurity(); startOptimiser(); startOptionsBacktest(); startFuturesBacktest(); startStructures(); startPortfolios(); startScreens(); startOptionChain(); startAlgos(); startFnoAlgos(); startBuilder(); startScanner(); startGtt(); startMarketTools(); renderTheme();
    syncSharedDates(); showBacktestPane($("#bt-what").value);
    if (state.app.hosted) { $("#logout").hidden = false; $("#logout-2").hidden = false; $("#logout").title = "Signed in as " + state.app.user; }
    const wanted = location.hash.slice(1);        // an app shortcut or a reload opens the same section
    if (wanted && $(`.tabs button[data-tab="${wanted}"]`) && !$(`.tabs button[data-tab="${wanted}"]`).hidden) selectTab(wanted);
    if (state.app.llm_problem) showError("#analyze-error", new Error(state.app.llm_problem));
    setInterval(() => loadState().catch(() => {}), 30000);
  })();
})();
