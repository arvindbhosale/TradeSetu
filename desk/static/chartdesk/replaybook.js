// @ts-check
// The practice book of a chart replay: one trade at a time, filled against the replay's candles only. It never
// sends an order anywhere; closed trades are saved as practice results, apart from paper and live trading.
//
// Fill rules (the same honest ones each time, so a practice score means something):
// - An order "at next open" fills at the next candle's open, never at a price already on the chart.
// - A limit order fills when a candle trades through its price: at the open if the candle opens past it (a gap),
//   else at the limit price.
// - A stoploss or target is checked from the open: a candle that opens past it fills at the open (a gap through a
//   stoploss loses more than planned). When one candle touches both, the stoploss is counted, since a candle
//   does not say which came first. On the candle a limit order filled, only the stoploss is checked.
// - Intraday trades (NSE/BSE) are squared off at 3:20 PM, or at the last close of the day if the candle size
//   skips 3:20 PM. Intraday orders not filled by then are cancelled.

/** @typedef {{ time: number, open: number, high: number, low: number, close: number, volume: number }} Bar */
/** @typedef {"long"|"short"} Side */
/** @typedef {"intraday"|"delivery"} Product */
/** @typedef {{ side: Side, qty: number, entry: number, time: number, stop: number|null, target: number|null, product: Product }} Position */
/** @typedef {{ side: Side, type: "market"|"limit", price: number|null, qty: number, stop: number|null, target: number|null, product: Product, placed: number }} Pending */
/** @typedef {{ side: Side, qty: number, entry: number, exit: number, entry_time: number, exit_time: number, reason: string, product: Product, gross: number, charges?: number, net?: number }} Trade */
/** @typedef {{ position: Position|null, pending: Pending|null, exitNext: boolean, trades: Trade[], last: Bar|null }} Book */

export const SQUARE_OFF = 15 * 3600 + 20 * 60;     // 3:20 PM on the India clock, seconds into the day
const DAY = 86400;
const say = (/** @type {number} */ v) => String(Number(v.toPrecision(10)));

/** The day number of a bar time (bar times are India wall-clock seconds). @param {number} t */
export const dayOf = (t) => Math.floor(t / DAY);
/** Seconds into the day of a bar time. @param {number} t */
export const clockOf = (t) => ((t % DAY) + DAY) % DAY;

/** @returns {Book} */
export function newBook() { return { position: null, pending: null, exitNext: false, trades: [], last: null }; }

/** Profit (above zero) or loss of a trade before charges. @param {Side} side @param {number} qty @param {number} entry @param {number} exit */
export function pnl(side, qty, entry, exit) { return (side === "long" ? exit - entry : entry - exit) * qty; }

/** The open trade's profit or loss at a price. @param {Book} book @param {number} price */
export function openPnl(book, price) {
  const p = book.position;
  return p ? pnl(p.side, p.qty, p.entry, price) : 0;
}

/** Checks a stoploss and target against the price they are measured from. Returns an error text or "".
 * @param {Side} side @param {number} ref @param {number|null} stop @param {number|null} target */
export function checkLevels(side, ref, stop, target) {
  if (stop != null && !(stop > 0)) return "Stoploss must be a price above zero.";
  if (target != null && !(target > 0)) return "Target must be a price above zero.";
  if (side === "long") {
    if (stop != null && stop >= ref) return "For a buy the stoploss must be below the price.";
    if (target != null && target <= ref) return "For a buy the target must be above the price.";
  } else {
    if (stop != null && stop <= ref) return "For a short sale the stoploss must be above the price.";
    if (target != null && target >= ref) return "For a short sale the target must be below the price.";
  }
  return "";
}

/**
 * Places a practice order to open a trade. ``last`` is the newest candle on the chart.
 * ``rules.squareOff`` with ``rules.seconds`` (the candle length) refuses an intraday order once the next candle would
 * start after 3:20 PM: it could not fill that day.
 * @param {Book} book @param {{ side: Side, type: "market"|"limit", price?: number|null, qty: number, stop?: number|null,
 *   target?: number|null, product: Product }} order @param {Bar} last @param {{ shortOk: boolean, squareOff?: boolean, seconds?: number }} rules
 * @returns {string} an error text, or "" when placed
 */
export function place(book, order, last, rules) {
  if (book.position) return "Close the open practice trade first: one trade at a time.";
  if (book.pending) return "Cancel the waiting order first.";
  if (!(order.qty > 0)) return "Quantity must be above zero.";
  if (order.side === "short" && !rules.shortOk) return "Short selling is for NSE/BSE intraday only.";
  if (order.product === "intraday" && rules.squareOff && clockOf(last.time + (rules.seconds || 0)) >= SQUARE_OFF) {
    return "Intraday orders stop at 3:20 PM. Play on to the next day, or choose Delivery.";
  }
  const type = order.type === "limit" ? "limit" : "market";
  const price = type === "limit" ? Number(order.price) : null;
  if (type === "limit" && !(price && price > 0)) return "Enter the limit price.";
  const stop = order.stop ?? null, target = order.target ?? null;
  const err = checkLevels(order.side, type === "limit" ? /** @type {number} */ (price) : last.close, stop, target);
  if (err) return err;
  book.pending = { side: order.side, type, price, qty: order.qty, stop, target, product: order.product, placed: last.time };
  book.last = book.last || last;
  return "";
}

/** Cancels the waiting order. @param {Book} book */
export function cancel(book) { const had = !!book.pending; book.pending = null; return had; }

/** Closes the open trade at the next candle's open. @param {Book} book */
export function exitAtNextOpen(book) { if (!book.position) return false; book.exitNext = true; return true; }

/** Moves the open trade's stoploss or target (checked against the last price). Returns an error text or "".
 * @param {Book} book @param {{ stop?: number|null, target?: number|null }} levels @param {number} price */
export function setLevels(book, levels, price) {
  const p = book.position || book.pending;
  if (!p) return "There is no practice trade to change.";
  const stop = "stop" in levels ? levels.stop ?? null : p.stop, target = "target" in levels ? levels.target ?? null : p.target;
  const o = book.pending;
  const ref = book.position || !o ? price : (o.type === "limit" ? /** @type {number} */ (o.price) : price);
  const err = checkLevels(p.side, ref, stop, target);
  if (err) return err;
  p.stop = stop; p.target = target;
  return "";
}

/** @param {Book} book @param {number} price @param {number} time @param {string} reason @returns {Trade} */
function close(book, price, time, reason) {
  const p = /** @type {Position} */ (book.position);
  const t = { side: p.side, qty: p.qty, entry: p.entry, exit: price, entry_time: p.time, exit_time: time, reason, product: p.product,
    gross: pnl(p.side, p.qty, p.entry, price) };
  book.trades.push(t);
  book.position = null; book.exitNext = false;
  return t;
}

/** Where the open trade's stoploss or target fills on a candle, if it does. @param {Position} p @param {Bar} bar
 * @param {boolean} fromOpen whether the candle's open counts (false on the candle a limit order filled mid-way)
 * @returns {{ price: number, reason: string }|null} */
function exitOn(p, bar, fromOpen) {
  const long = p.side === "long";
  const pastStop = (/** @type {number} */ x) => p.stop != null && (long ? x <= p.stop : x >= p.stop);
  const pastTarget = (/** @type {number} */ x) => p.target != null && (long ? x >= p.target : x <= p.target);
  if (fromOpen) {
    if (pastStop(bar.open)) return { price: bar.open, reason: "Stoploss" };
    if (pastTarget(bar.open)) return { price: bar.open, reason: "Target" };
  }
  const stopHit = p.stop != null && (long ? bar.low <= p.stop : bar.high >= p.stop);
  if (stopHit) return { price: /** @type {number} */ (p.stop), reason: "Stoploss" };
  const targetHit = fromOpen && p.target != null && (long ? bar.high >= p.target : bar.low <= p.target);
  if (targetHit) return { price: /** @type {number} */ (p.target), reason: "Target" };
  return null;
}

/**
 * Plays one new candle through the book. Returns what happened, in words, for the replay's log.
 * @param {Book} book @param {Bar} bar @param {{ squareOff: boolean }} rules  squareOff: NSE/BSE intraday candles
 * @returns {{ text: string, trade?: Trade }[]}
 */
export function advance(book, bar, rules) {
  /** @type {{ text: string, trade?: Trade }[]} */ const events = [];
  const prev = book.last;
  book.last = bar;
  const newDay = !!prev && dayOf(bar.time) !== dayOf(prev.time);
  const after = rules.squareOff && clockOf(bar.time) >= SQUARE_OFF;
  const closeAt = (/** @type {number} */ price, /** @type {number} */ time, /** @type {string} */ reason) => {
    const t = close(book, price, time, reason);
    events.push({ text: `${reason}: ${t.side === "long" ? "sold" : "bought back"} ${t.qty} at ${say(price)}`, trade: t });
  };
  // the day ended without a 3:20 PM candle (hourly or bigger): square off at the day's last close
  if (rules.squareOff && newDay && prev) {
    if (book.position && book.position.product === "intraday" && dayOf(book.position.time) !== dayOf(bar.time)) closeAt(prev.close, prev.time, "Square-off");
    if (book.pending && book.pending.product === "intraday") { book.pending = null; events.push({ text: "The intraday order was not filled that day and is cancelled." }); }
  }
  if (book.position && book.exitNext) closeAt(bar.open, bar.time, "Exit");
  if (book.position && book.position.product === "intraday" && after) closeAt(bar.open, bar.time, "Square-off");
  let filledMidway = false;
  const o = book.pending;
  if (o) {
    if (o.product === "intraday" && after) {
      book.pending = null;
      events.push({ text: "Intraday orders stop at 3:20 PM: the order is cancelled." });
    } else {
      let price = null;
      if (o.type === "market") price = bar.open;
      else {
        const limit = /** @type {number} */ (o.price);
        if (o.side === "long") { if (bar.open <= limit) price = bar.open; else if (bar.low <= limit) { price = limit; filledMidway = true; } }
        else if (bar.open >= limit) price = bar.open; else if (bar.high >= limit) { price = limit; filledMidway = true; }
      }
      if (price != null) {
        book.position = { side: o.side, qty: o.qty, entry: price, time: bar.time, stop: o.stop, target: o.target, product: o.product };
        book.pending = null;
        events.push({ text: `${o.side === "long" ? "Bought" : "Short sold"} ${o.qty} at ${say(price)}` });
      }
    }
  }
  if (book.position) {
    const hit = exitOn(book.position, bar, !filledMidway);
    if (hit) closeAt(hit.price, bar.time, hit.reason);
  }
  return events;
}

/** Closes what is open at the last candle's close when a replay ends. @param {Book} book */
export function finish(book) {
  book.pending = null;
  if (!book.position || !book.last) return null;
  return close(book, book.last.close, book.last.time, "End of replay");
}

/** Totals for the replay's own panel (the desk recomputes them with charges when the session is saved).
 * @param {Trade[]} trades */
export function totals(trades) {
  const net = (/** @type {Trade} */ t) => (t.net != null ? t.net : t.gross);
  const wins = trades.filter((t) => net(t) > 0).length;
  let run = 0, peak = 0, worst = 0;
  trades.forEach((t) => { run += net(t); peak = Math.max(peak, run); worst = Math.min(worst, run - peak); });
  return { trades: trades.length, wins, winRate: trades.length ? (100 * wins) / trades.length : null,
    gross: trades.reduce((s, t) => s + t.gross, 0), charges: trades.reduce((s, t) => s + (t.charges || 0), 0), net: run, maxDrawdown: worst };
}
