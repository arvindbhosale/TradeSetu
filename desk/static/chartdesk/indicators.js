// @ts-check
// Chart indicators, computed in the browser so they move with every live price. Written from each
// indicator's published definition (they are standard, public-domain formulas), not copied from any charting
// product. Where the desk's server already has an indicator (desk/indicators.py) the formula is the same, and a
// test checks the two agree: EMAs seed with the first value and show once `period` values are in (pandas ewm
// adjust=False, min_periods), RSI/ATR/ADX use Wilder's smoothing, standard deviations are the population one,
// VWAP restarts each trading day.
//
// Every value on bar i uses bars up to i only, except where an entry says `repaints` (ZigZag, fractals, the
// regression channel) or `future` (Ichimoku and Alligator lines drawn ahead of the last bar).

/** @typedef {{ time: number, open: number, high: number, low: number, close: number, volume: number }} Bar */
/** @typedef {{ time: number, value: number }} Point */
/** @typedef {(number|null)[]} Values */
/** @typedef {{ sessionOffset?: number, seconds?: number, market?: string }} Ctx */

// -- building blocks on value arrays (null = no value yet) ---------------------------------------------

/** @param {number} n @returns {Values} */
const blank = (n) => new Array(n).fill(null);
/** @param {number|null|undefined} v */
const ok = (v) => v != null && Number.isFinite(v);

/** Runs `fn(start, end)` over each window of `p` values in a row that are all present.
 * @param {Values} values @param {number} p @param {(s: number, e: number) => number|null} fn @returns {Values} */
function rolling(values, p, fn) {
  const out = blank(values.length);
  let run = 0;
  for (let i = 0; i < values.length; i++) {
    if (!ok(values[i])) { run = 0; continue; }
    run++;
    if (run >= p) { const v = fn(i - p + 1, i); out[i] = ok(v) ? v : null; }
  }
  return out;
}

/** @param {Values} values @param {number} period @returns {Values} */
export function smaValues(values, period) {
  /** @type {Values} */ const out = new Array(values.length).fill(null);
  let sum = 0, count = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v == null) { sum = 0; count = 0; continue; }
    sum += v; count++;
    if (count > period) { sum -= /** @type {number} */ (values[i - period]); count = period; }
    if (count === period) out[i] = sum / period;
  }
  return out;
}

/** Exponential average with weight `alpha`, seeded with the first value, shown from the `minPeriods`th.
 * @param {Values} values @param {number} alpha @param {number} minPeriods @returns {Values} */
export function ewmValues(values, alpha, minPeriods) {
  /** @type {Values} */ const out = new Array(values.length).fill(null);
  /** @type {number|null} */ let level = null;
  let count = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (v == null || !Number.isFinite(v)) { if (level != null && count >= minPeriods) out[i] = level; continue; }
    level = level == null ? v : alpha * v + (1 - alpha) * level;
    count++;
    if (count >= minPeriods) out[i] = level;
  }
  return out;
}

/** @param {Values} values @param {number} period */
export const emaValues = (values, period) => ewmValues(values, 2 / (period + 1), period);
/** Wilder's smoothing (RMA). @param {Values} values @param {number} period */
export const rmaValues = (values, period) => ewmValues(values, 1 / period, period);

/** @param {Values} v @param {number} p */
const wma = (v, p) => rolling(v, p, (s, e) => { let a = 0; for (let j = s; j <= e; j++) a += /** @type {number} */ (v[j]) * (j - s + 1); return a / (p * (p + 1) / 2); });
/** @param {Values} v @param {number} p */
const sum = (v, p) => rolling(v, p, (s, e) => { let a = 0; for (let j = s; j <= e; j++) a += /** @type {number} */ (v[j]); return a; });
/** @param {Values} v @param {number} p */
const highest = (v, p) => rolling(v, p, (s, e) => { let a = -Infinity; for (let j = s; j <= e; j++) a = Math.max(a, /** @type {number} */ (v[j])); return a; });
/** @param {Values} v @param {number} p */
const lowest = (v, p) => rolling(v, p, (s, e) => { let a = Infinity; for (let j = s; j <= e; j++) a = Math.min(a, /** @type {number} */ (v[j])); return a; });
/** Population standard deviation. @param {Values} v @param {number} p */
const stdev = (v, p) => rolling(v, p, (s, e) => {
  let m = 0; for (let j = s; j <= e; j++) m += /** @type {number} */ (v[j]); m /= p;
  let q = 0; for (let j = s; j <= e; j++) q += (/** @type {number} */ (v[j]) - m) ** 2;
  return Math.sqrt(q / p);
});
/** Least-squares line through the last `p` values, read `offset` bars back from the newest. @param {Values} v @param {number} p @param {number} [offset] */
function linreg(v, p, offset = 0) {
  return rolling(v, p, (s, e) => {
    let sx = 0, sy = 0, sxy = 0, sxx = 0;
    for (let j = s; j <= e; j++) { const x = j - s, y = /** @type {number} */ (v[j]); sx += x; sy += y; sxy += x * y; sxx += x * x; }
    const d = p * sxx - sx * sx, slope = d ? (p * sxy - sx * sy) / d : 0, icpt = (sy - slope * sx) / p;
    return icpt + slope * (p - 1 - offset);
  });
}
/** @param {Values} a @param {Values} b @param {(x: number, y: number) => number|null} fn @returns {Values} */
const zip = (a, b, fn) => a.map((x, i) => { const y = b[i]; if (!ok(x) || !ok(y)) return null; const v = fn(/** @type {number} */ (x), /** @type {number} */ (y)); return ok(v) ? v : null; });
/** @param {Values} a @param {(x: number, i: number) => number|null} fn @returns {Values} */
const map = (a, fn) => a.map((x, i) => { if (!ok(x)) return null; const v = fn(/** @type {number} */ (x), i); return ok(v) ? v : null; });
/** v[i] - v[i-n]. @param {Values} v @param {number} n */
const change = (v, n = 1) => v.map((x, i) => (i >= n && ok(x) && ok(v[i - n]) ? /** @type {number} */ (x) - /** @type {number} */ (v[i - n]) : null));
/** Percent change over n bars. @param {Values} v @param {number} n */
const roc = (v, n) => v.map((x, i) => (i >= n && ok(x) && ok(v[i - n]) && v[i - n] !== 0 ? 100 * (/** @type {number} */ (x) / /** @type {number} */ (v[i - n]) - 1) : null));
/** v moved n bars later (v[i-n] at i). @param {Values} v @param {number} n */
const lag = (v, n) => v.map((_, i) => (i >= n ? v[i - n] : null));
/** @param {Values} v */
const cum = (v) => { let a = 0, seen = false; return v.map((x) => { if (!ok(x)) return seen ? a : null; seen = true; a += /** @type {number} */ (x); return a; }); };
/** (x + 2x[1] + 2x[2] + x[3]) / 6 @param {Values} v */
const swma = (v) => rolling(v, 4, (s, e) => (/** @type {number} */ (v[s]) + 2 * /** @type {number} */ (v[s + 1]) + 2 * /** @type {number} */ (v[s + 2]) + /** @type {number} */ (v[e])) / 6);

/** @param {Bar[]} bars @param {string} [name] @returns {Values} */
function source(bars, name = "close") {
  switch (name) {
    case "open": return bars.map((b) => b.open);
    case "high": return bars.map((b) => b.high);
    case "low": return bars.map((b) => b.low);
    case "hl2": return bars.map((b) => (b.high + b.low) / 2);
    case "hlc3": return bars.map((b) => (b.high + b.low + b.close) / 3);
    case "ohlc4": return bars.map((b) => (b.open + b.high + b.low + b.close) / 4);
    default: return bars.map((b) => b.close);
  }
}
/** True range; the first bar's is its high minus low. @param {Bar[]} bars @returns {Values} */
const trueRange = (bars) => bars.map((b, i) => (i ? Math.max(b.high - b.low, Math.abs(b.high - bars[i - 1].close), Math.abs(b.low - bars[i - 1].close)) : b.high - b.low));
/** @param {Bar[]} bars @param {number} p */
export const atrValues = (bars, p) => rmaValues(trueRange(bars), p);

/** A moving average of the kind a "Type" setting names. @param {string} type @param {Values} v @param {number} p */
function maOf(type, v, p) {
  if (type === "sma") return smaValues(v, p);
  if (type === "wma") return wma(v, p);
  if (type === "rma") return rmaValues(v, p);
  return emaValues(v, p);
}

/** @param {Bar[]} bars @param {Values} values @returns {Point[]} */
function points(bars, values) {
  /** @type {Point[]} */ const out = [];
  for (let i = 0; i < bars.length; i++) {
    const v = values[i];
    if (v != null && Number.isFinite(v)) out.push({ time: bars[i].time, value: v });
  }
  return out;
}

const closes = (/** @type {Bar[]} */ bars) => bars.map((b) => b.close);

// -- the indicators the desk's server also has (kept as named exports; tests compare them) ---------------

/** @param {Bar[]} bars @param {{ period: number, source?: string }} p */
export function sma(bars, { period, source: src = "close" }) { return { line: points(bars, smaValues(source(bars, src), period)) }; }

/** @param {Bar[]} bars @param {{ period: number, source?: string }} p */
export function ema(bars, { period, source: src = "close" }) { return { line: points(bars, emaValues(source(bars, src), period)) }; }

/** @param {Bar[]} bars @param {{ period: number, width: number }} p */
function bollingerValues(bars, { period, width }) {
  const close = closes(bars), mid = smaValues(close, period), sd = stdev(close, period);
  return { upper: zip(mid, sd, (m, s) => m + width * s), middle: mid, lower: zip(mid, sd, (m, s) => m - width * s) };
}
/** @param {Bar[]} bars @param {{ period: number, width: number }} p */
export function bollinger(bars, p) {
  const r = bollingerValues(bars, p);
  return { upper: points(bars, r.upper), middle: points(bars, r.middle), lower: points(bars, r.lower) };
}

/** @param {Values} close @param {number} period */
function rsiValues(close, period) {
  /** @type {Values} */ const gains = [null];
  /** @type {Values} */ const losses = [null];
  for (let i = 1; i < close.length; i++) {
    const a = close[i], b = close[i - 1];
    if (!ok(a) || !ok(b)) { gains.push(null); losses.push(null); continue; }
    const d = /** @type {number} */ (a) - /** @type {number} */ (b);
    gains.push(Math.max(d, 0)); losses.push(Math.max(-d, 0));
  }
  const g = rmaValues(gains, period), l = rmaValues(losses, period);
  return g.map((gain, i) => {
    const loss = l[i];
    if (gain == null || loss == null) return null;
    return loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  });
}
/** @param {Bar[]} bars @param {{ period: number }} p */
export function rsi(bars, { period }) { return { line: points(bars, rsiValues(closes(bars), period)) }; }

/** @param {Bar[]} bars @param {{ fast: number, slow: number, signal: number }} p */
function macdValues(bars, { fast, slow, signal }) {
  const close = closes(bars), f = emaValues(close, fast), s = emaValues(close, slow);
  const line = zip(f, s, (a, b) => a - b), sig = emaValues(line, signal);
  return { macd: line, signal: sig, histogram: zip(line, sig, (a, b) => a - b) };
}
/** @param {Bar[]} bars @param {{ fast: number, slow: number, signal: number }} p */
export function macd(bars, p) {
  const r = macdValues(bars, p);
  return { macd: points(bars, r.macd), signal: points(bars, r.signal), histogram: points(bars, r.histogram) };
}

/** @param {Bar[]} bars @param {number} sessionOffset */
function vwapValues(bars, sessionOffset) {
  /** @type {Values} */ const out = [];
  let day = NaN, pv = 0, vol = 0;
  for (const b of bars) {
    const d = Math.floor((b.time - sessionOffset) / 86400);
    if (d !== day) { day = d; pv = 0; vol = 0; }
    pv += ((b.high + b.low + b.close) / 3) * b.volume; vol += b.volume;
    out.push(vol > 0 ? pv / vol : null);
  }
  return out;
}
/** VWAP restarting each trading day. Bar times are on the chart clock; `sessionOffset` seconds after midnight
 * on that clock is when the market's day starts (0 for NSE/BSE, 19800 for crypto's 00:00 UTC).
 * @param {Bar[]} bars @param {{ sessionOffset?: number }} p */
export function vwap(bars, { sessionOffset = 0 } = {}) { return { line: points(bars, vwapValues(bars, sessionOffset)) }; }

/** Supertrend (as desk/indicators.py): the band under price while the trend is up, over it while down.
 * @param {Bar[]} bars @param {number} period @param {number} mult */
export function supertrendValues(bars, period, mult) {
  const a = atrValues(bars, period), n = bars.length;
  /** @type {Values} */ const line = blank(n), up = blank(n), down = blank(n);
  /** @type {number|null} */ let upper = null, lower = null;
  let long = false;
  for (let i = 0; i < n; i++) {
    const at = a[i];
    if (at == null) continue;
    const mid = (bars[i].high + bars[i].low) / 2, ub = mid + mult * at, lb = mid - mult * at, c = bars[i].close;
    if (upper == null || lower == null) { upper = ub; lower = lb; long = c > ub; }
    else {
      upper = ub < upper || bars[i - 1].close > upper ? ub : upper;
      lower = lb > lower || bars[i - 1].close < lower ? lb : lower;
      long = long ? c >= lower : c > upper;
    }
    line[i] = long ? lower : upper;
    if (long) up[i] = lower; else down[i] = upper;
  }
  return { line, up, down };
}

/** Wilder's parabolic SAR (as desk/indicators.py). @param {Bar[]} bars @param {number} step @param {number} maximum */
export function psarValues(bars, step, maximum) {
  const n = bars.length, sar = blank(n);
  if (n < 2) return sar;
  let long = bars[1].high >= bars[0].high, extreme = long ? bars[1].high : bars[1].low, value = long ? bars[0].low : bars[0].high, factor = step;
  sar[1] = value;
  for (let i = 2; i < n; i++) {
    value = value + factor * (extreme - value);
    if (long) {
      value = Math.min(value, bars[i - 1].low, bars[i - 2].low);
      if (bars[i].low < value) { long = false; value = extreme; extreme = bars[i].low; factor = step; }
      else if (bars[i].high > extreme) { extreme = bars[i].high; factor = Math.min(factor + step, maximum); }
    } else {
      value = Math.max(value, bars[i - 1].high, bars[i - 2].high);
      if (bars[i].high > value) { long = true; value = extreme; extreme = bars[i].high; factor = step; }
      else if (bars[i].low < extreme) { extreme = bars[i].low; factor = Math.min(factor + step, maximum); }
    }
    sar[i] = value;
  }
  return sar;
}

/** ADX with +DI and −DI (Wilder; as desk/indicators.py). @param {Bar[]} bars @param {number} p */
export function adxValues(bars, p) {
  /** @type {Values} */ const pdm = [0], mdm = [0];
  for (let i = 1; i < bars.length; i++) {
    const up = bars[i].high - bars[i - 1].high, down = bars[i - 1].low - bars[i].low;
    pdm.push(up > down && up > 0 ? up : 0); mdm.push(down > up && down > 0 ? down : 0);
  }
  const tr = rmaValues(trueRange(bars), p);
  const plus = zip(rmaValues(pdm, p), tr, (a, t) => (t ? 100 * a / t : null)), minus = zip(rmaValues(mdm, p), tr, (a, t) => (t ? 100 * a / t : null));
  const dx = zip(plus, minus, (a, b) => (a + b ? 100 * Math.abs(a - b) / (a + b) : null));
  return { adx: rmaValues(dx, p), plus, minus };
}

/** Raw %K and its average %D (as desk/indicators.py when smoothing is 1). @param {Bar[]} bars @param {number} p */
function stochK(bars, p) {
  const hh = highest(source(bars, "high"), p), ll = lowest(source(bars, "low"), p);
  return bars.map((b, i) => { const h = hh[i], l = ll[i]; return h == null || l == null || h === l ? null : 100 * (b.close - l) / (h - l); });
}

/** Commodity channel index on the typical price (as desk/indicators.py). @param {Bar[]} bars @param {number} p */
export function cciValues(bars, p) {
  const tp = source(bars, "hlc3");
  return rolling(tp, p, (s, e) => {
    let m = 0; for (let j = s; j <= e; j++) m += /** @type {number} */ (tp[j]); m /= p;
    let d = 0; for (let j = s; j <= e; j++) d += Math.abs(/** @type {number} */ (tp[j]) - m); d /= p;
    return d ? (/** @type {number} */ (tp[e]) - m) / (0.015 * d) : null;
  });
}

/** @param {Bar[]} bars @param {number} fast @param {number} slow */
const aoValues = (bars, fast, slow) => { const m = source(bars, "hl2"); return zip(smaValues(m, fast), smaValues(m, slow), (a, b) => a - b); };

// -- pivot points -----------------------------------------------------------------------------------------

const PIVOT_KEYS = ["r3", "r2", "r1", "p", "s1", "s2", "s3"];
/** @param {string} type @param {number} h @param {number} l @param {number} c @returns {number[]} R3, R2, R1, P, S1, S2, S3 */
export function pivotLevels(type, h, l, c) {
  const r = h - l;
  if (type === "fibonacci") { const p = (h + l + c) / 3; return [p + r, p + 0.618 * r, p + 0.382 * r, p, p - 0.382 * r, p - 0.618 * r, p - r]; }
  if (type === "woodie") { const p = (h + l + 2 * c) / 4; return [h + 2 * (p - l), p + r, 2 * p - l, p, 2 * p - h, p - r, l - 2 * (h - p)]; }
  if (type === "camarilla") { const p = (h + l + c) / 3, k = 1.1 * r; return [c + k / 4, c + k / 6, c + k / 12, p, c - k / 12, c - k / 6, c - k / 4]; }
  const p = (h + l + c) / 3;
  return [h + 2 * (p - l), p + r, 2 * p - l, p, 2 * p - h, p - r, l - 2 * (h - p)];
}

/** Which day, week (Monday to Sunday) or month a chart-clock time falls in. @param {number} t @param {string} freq */
function periodOf(t, freq) {
  const day = Math.floor(t / 86400);
  if (freq === "D") return day;
  if (freq === "W") return Math.floor((day + 3) / 7);          // 1970-01-01 was a Thursday
  const d = new Date(t * 1000);
  return d.getUTCFullYear() * 12 + d.getUTCMonth();
}

/** Weekdays from `fromDay` up to (not including) `toDay`, both as days since 1970. @param {number} fromDay @param {number} toDay */
function weekdaysBetween(fromDay, toDay) {
  let n = 0;
  for (let d = fromDay; d < toDay; d++) { const w = (d + 4) % 7; if (w !== 0 && w !== 6) n++; }   // 0 Sunday, 6 Saturday
  return n;
}

/** For each bar, the pivots of its day, week or month set by the period before, as desk/indicators.py's
 * period_pivots: a first period the data does not wholly cover sets no levels.
 * @param {Bar[]} bars @param {string} freq D, W or M @param {string} type @param {number} offset session start */
export function pivotValues(bars, freq, type, offset = 0) {
  const n = bars.length, t = bars.map((b) => b.time - offset);
  /** @type {Record<string, Values>} */ const out = Object.fromEntries(PIVOT_KEYS.map((k) => [k, blank(n)]));
  if (!n) return out;
  const keys = t.map((x) => periodOf(x, freq));
  /** @type {{ key: number, h: number, l: number, c: number }[]} */ const ranges = [];
  bars.forEach((b, i) => {
    const last = ranges[ranges.length - 1];
    if (last && last.key === keys[i]) { last.h = Math.max(last.h, b.high); last.l = Math.min(last.l, b.low); last.c = b.close; }
    else ranges.push({ key: keys[i], h: b.high, l: b.low, c: b.close });
  });
  let firstComplete = true;
  const intraday = t.some((x) => x % 86400 !== 0);
  if (intraday) {
    /** @type {Map<number, number>} */ const opens = new Map();
    t.forEach((x) => { const d = Math.floor(x / 86400), s = x - d * 86400; if (!opens.has(d) || s < /** @type {number} */ (opens.get(d))) opens.set(d, s); });
    const list = [...opens.values()];
    if (list.length > 1 && list[0] > Math.min(...list.slice(1))) firstComplete = false;
  }
  if (firstComplete && freq !== "D") {
    const firstDay = Math.floor(t[0] / 86400);
    let start;
    if (freq === "W") start = firstDay - ((firstDay + 3) % 7);
    else { const d = new Date(t[0] * 1000); start = Math.floor(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), 1) / 86400000); }
    firstComplete = weekdaysBetween(start, firstDay) === 0;
  }
  /** @type {Map<number, number[]|null>} */ const levels = new Map();
  ranges.forEach((r, i) => {
    if (i === 0 || (i === 1 && !firstComplete)) { levels.set(r.key, null); return; }
    const prev = ranges[i - 1];
    levels.set(r.key, pivotLevels(type, prev.h, prev.l, prev.c));
  });
  keys.forEach((k, i) => { const lv = levels.get(k); if (lv) PIVOT_KEYS.forEach((name, j) => { out[name][i] = lv[j]; }); });
  return out;
}

// -- the catalogue ----------------------------------------------------------------------------------------

/** @param {string} label @param {number} value @param {number} min @param {number} max @param {number} [step] */
const num = (label, value, min, max, step) => ({ label, value, min, max, ...(step ? { step } : {}) });
/** @param {string} label @param {string} value @param {Record<string, string>} options */
const pick = (label, value, options) => ({ label, value, options });
const SOURCES = { close: "Close", open: "Open", high: "High", low: "Low", hl2: "(High + Low) / 2", hlc3: "(High + Low + Close) / 3", ohlc4: "(Open + High + Low + Close) / 4" };
const src = (value = "close") => pick("Price used", value, SOURCES);
const MA_TYPES = { ema: "Exponential", sma: "Simple", wma: "Weighted", rma: "Smoothed (Wilder)" };
/** @param {number} [value] */ const period = (value = 14, max = 500) => num("Period", value, 1, max);

/**
 * @typedef {{ key: string, label: string, type?: "line"|"histogram"|"dots"|"step", dashed?: boolean, width?: number,
 *   colour?: number, sparse?: boolean, signed?: boolean, updown?: boolean, back?: boolean }} Output
 * @typedef {{ label: string, value: number|string, min?: number, max?: number, step?: number, options?: Record<string, string> }} Param
 * @typedef {{ name: string, short: string, group: string, pane: "price"|"own", params: Record<string, Param>,
 *   outputs: Output[], guides?: number[], intraday?: boolean, repaints?: boolean, future?: number, fill?: [string, string],
 *   about: string, calc: (bars: Bar[], p: Record<string, any>, ctx: Ctx) => Record<string, Values> }} Entry
 */

/** @param {string} key @param {string} label @param {Partial<Output>} [style] @returns {Output} */
const out = (key, label, style = {}) => ({ key, label, ...style });
const LINE = [out("line", "Line")];

export const GROUPS = { ma: "Moving averages", bands: "Bands, channels and levels", trend: "Trend", momentum: "Momentum and oscillators", volatility: "Volatility", volume: "Volume" };

/** @param {string} name @param {string} short @param {string} about @param {(v: Values, p: number, bars: Bar[], q: Record<string, any>) => Values} fn @param {number} value */
function movingAverage(name, short, about, fn, value = 20) {
  return /** @type {Entry} */ ({ name, short, group: "ma", pane: "price", about, params: { period: period(value), source: src() }, outputs: LINE,
    calc: (bars, p) => ({ line: fn(source(bars, p.source), p.period, bars, p) }) });
}

/** @type {Record<string, Entry>} */
export const CATALOG = {
  // Moving averages
  sma: movingAverage("Simple moving average", "SMA", "The average of the last N prices.", smaValues, 20),
  ema: movingAverage("Exponential moving average", "EMA", "An average that weights recent prices more.", emaValues, 50),
  wma: movingAverage("Weighted moving average", "WMA", "Weights the newest price N, the one before N − 1, and so on.", wma, 20),
  dema: movingAverage("Double exponential moving average", "DEMA", "2 × EMA − EMA of the EMA: less lag than an EMA.", (v, p) => { const e1 = emaValues(v, p), e2 = emaValues(e1, p); return zip(e1, e2, (a, b) => 2 * a - b); }, 20),
  tema: movingAverage("Triple exponential moving average", "TEMA", "3 × EMA − 3 × EMA² + EMA³: even less lag.", (v, p) => {
    const e1 = emaValues(v, p), e2 = emaValues(e1, p), e3 = emaValues(e2, p);
    return e1.map((a, i) => (ok(a) && ok(e2[i]) && ok(e3[i]) ? 3 * /** @type {number} */ (a) - 3 * /** @type {number} */ (e2[i]) + /** @type {number} */ (e3[i]) : null));
  }, 20),
  hma: movingAverage("Hull moving average", "HMA", "A weighted average of weighted averages that follows price closely.", (v, p) => {
    const half = wma(v, Math.max(1, Math.floor(p / 2))), full = wma(v, p);
    return wma(zip(half, full, (a, b) => 2 * a - b), Math.max(1, Math.floor(Math.sqrt(p))));
  }, 9),
  vwma: movingAverage("Volume-weighted moving average", "VWMA", "An average where high-volume candles count more.", (v, p, bars) => {
    const pv = sum(v.map((x, i) => (ok(x) ? /** @type {number} */ (x) * bars[i].volume : null)), p), vol = sum(bars.map((b) => b.volume), p);
    return zip(pv, vol, (a, b) => (b ? a / b : null));
  }, 20),
  smma: movingAverage("Smoothed moving average (Wilder)", "SMMA", "Wilder's smoothing, the average RSI and ATR use.", rmaValues, 14),
  alma: { name: "Arnaud Legoux moving average", short: "ALMA", group: "ma", pane: "price", about: "A Gaussian-weighted average that is smooth with little lag.",
    params: { period: period(9), offset: num("Offset (0 to 1)", 0.85, 0, 1, 0.05), sigma: num("Sigma", 6, 1, 20, 0.5), source: src() }, outputs: LINE,
    calc: (bars, p) => {
      const v = source(bars, p.source), m = p.offset * (p.period - 1), s = p.period / p.sigma;
      const w = Array.from({ length: p.period }, (_, j) => Math.exp(-((j - m) ** 2) / (2 * s * s))), total = w.reduce((a, b) => a + b, 0);
      return { line: rolling(v, p.period, (st, e) => { let a = 0; for (let j = st; j <= e; j++) a += w[j - st] * /** @type {number} */ (v[j]); return a / total; }) };
    } },
  kama: { name: "Kaufman adaptive moving average", short: "KAMA", group: "ma", pane: "price", about: "Speeds up in trends and slows down in sideways markets.",
    params: { period: period(10), fast: num("Fast period", 2, 1, 50), slow: num("Slow period", 30, 2, 200), source: src() }, outputs: LINE,
    calc: (bars, p) => {
      const v = source(bars, p.source), n = v.length, res = blank(n), fsc = 2 / (p.fast + 1), ssc = 2 / (p.slow + 1);
      let prev = null;
      for (let i = p.period; i < n; i++) {
        const c = /** @type {number} */ (v[i]);
        let noise = 0; for (let j = i - p.period + 1; j <= i; j++) noise += Math.abs(/** @type {number} */ (v[j]) - /** @type {number} */ (v[j - 1]));
        const er = noise ? Math.abs(c - /** @type {number} */ (v[i - p.period])) / noise : 0, sc = (er * (fsc - ssc) + ssc) ** 2;
        prev = prev == null ? /** @type {number} */ (v[i - 1]) + sc * (c - /** @type {number} */ (v[i - 1])) : prev + sc * (c - prev);
        res[i] = prev;
      }
      return { line: res };
    } },
  zlema: movingAverage("Zero-lag exponential moving average", "ZLEMA", "An EMA of price plus its own recent change, to cut lag.", (v, p) => {
    const l = Math.floor((p - 1) / 2);
    return emaValues(v.map((x, i) => (i >= l && ok(x) && ok(v[i - l]) ? 2 * /** @type {number} */ (x) - /** @type {number} */ (v[i - l]) : null)), p);
  }, 20),
  trima: movingAverage("Triangular moving average", "TRIMA", "An average of an average: weights the middle of the window most.", (v, p) => smaValues(smaValues(v, Math.floor(p / 2) + 1), Math.ceil(p / 2)), 20),
  lsma: movingAverage("Least squares moving average", "LSMA", "The end of a straight line fitted to the last N prices.", (v, p) => linreg(v, Math.max(2, p)), 25),
  mcginley: movingAverage("McGinley dynamic", "McGinley", "An average that adjusts its speed to how fast price moves.", (v, p) => {
    const seed = emaValues(v, p), res = blank(v.length);
    /** @type {number|null} */ let md = null;
    for (let i = 0; i < v.length; i++) {
      const x = v[i];
      if (!ok(x)) continue;
      if (md == null) { if (ok(seed[i])) md = /** @type {number} */ (seed[i]); }
      else md = md + (/** @type {number} */ (x) - md) / (p * (/** @type {number} */ (x) / md) ** 4);
      res[i] = md;
    }
    return res;
  }, 14),
  t3: { name: "T3 moving average (Tillson)", short: "T3", group: "ma", pane: "price", about: "Six chained EMAs blended for a very smooth line.",
    params: { period: period(5), factor: num("Volume factor", 0.7, 0, 1, 0.05), source: src() }, outputs: LINE,
    calc: (bars, p) => {
      const gd = (/** @type {Values} */ x) => { const e1 = emaValues(x, p.period), e2 = emaValues(e1, p.period); return zip(e1, e2, (a, b) => a * (1 + p.factor) - b * p.factor); };
      return { line: gd(gd(gd(source(bars, p.source)))) };
    } },
  ma_cross: { name: "Moving average cross", short: "MA cross", group: "ma", pane: "price", about: "A fast and a slow average: their crossings are the classic trend signal.",
    params: { fast: num("Fast period", 9, 1, 500), slow: num("Slow period", 21, 2, 500), type: pick("Type", "ema", MA_TYPES), source: src() },
    outputs: [out("fast", "Fast"), out("slow", "Slow", { colour: 1 })],
    calc: (bars, p) => { const v = source(bars, p.source); return { fast: maOf(p.type, v, p.fast), slow: maOf(p.type, v, p.slow) }; } },
  gmma: { name: "Guppy multiple moving averages", short: "GMMA", group: "ma", pane: "price", about: "Six short and six long EMAs: traders' and investors' trends.",
    params: { source: src() },
    outputs: [3, 5, 8, 10, 12, 15].map((n) => out(`s${n}`, `EMA ${n}`, { colour: 0, width: 1 })).concat([30, 35, 40, 45, 50, 60].map((n) => out(`l${n}`, `EMA ${n}`, { colour: 1, width: 1 }))),
    calc: (bars, p) => { const v = source(bars, p.source); return Object.fromEntries([3, 5, 8, 10, 12, 15, 30, 35, 40, 45, 50, 60].map((n) => [`${n <= 15 ? "s" : "l"}${n}`, emaValues(v, n)])); } },
  alligator: { name: "Williams alligator", short: "Alligator", group: "ma", pane: "price", future: 8, about: "Three smoothed averages drawn ahead of price (jaw, teeth, lips).",
    params: { jaw: num("Jaw period", 13, 1, 200), teeth: num("Teeth period", 8, 1, 200), lips: num("Lips period", 5, 1, 200) },
    outputs: [out("jaw", "Jaw", { colour: 0 }), out("teeth", "Teeth", { colour: 4 }), out("lips", "Lips", { colour: 2 })],
    calc: (bars, p) => { const m = source(bars, "hl2"), ahead = (/** @type {Values} */ v, n) => blank(n).concat(v);
      return { jaw: ahead(rmaValues(m, p.jaw), 8), teeth: ahead(rmaValues(m, p.teeth), 5), lips: ahead(rmaValues(m, p.lips), 3) }; } },

  // Bands, channels and levels
  bollinger: { name: "Bollinger Bands", short: "BB", group: "bands", pane: "price", fill: ["upper", "lower"], about: "An average with bands a set number of standard deviations above and below.",
    params: { period: num("Period", 20, 2, 500), width: num("Width (standard deviations)", 2, 0.5, 5, 0.5) },
    outputs: [out("upper", "Upper", { width: 1 }), out("middle", "Middle", { width: 1, dashed: true }), out("lower", "Lower", { width: 1 })],
    calc: (bars, p) => bollingerValues(bars, /** @type {any} */ (p)) },
  keltner: { name: "Keltner channels", short: "KC", group: "bands", pane: "price", fill: ["upper", "lower"], about: "An EMA with bands a multiple of the average true range away.",
    params: { period: num("Period", 20, 1, 500), mult: num("Multiplier", 2, 0.5, 10, 0.5), atr: num("ATR period", 10, 1, 200) },
    outputs: [out("upper", "Upper", { width: 1 }), out("middle", "Middle", { width: 1, dashed: true }), out("lower", "Lower", { width: 1 })],
    calc: (bars, p) => { const mid = emaValues(closes(bars), p.period), a = atrValues(bars, p.atr);
      return { upper: zip(mid, a, (m, x) => m + p.mult * x), middle: mid, lower: zip(mid, a, (m, x) => m - p.mult * x) }; } },
  donchian: { name: "Donchian channel", short: "DC", group: "bands", pane: "price", fill: ["upper", "lower"], about: "The highest high and lowest low of the N candles before this one, as the desk's breakout rules use.",
    params: { period: period(20) },
    outputs: [out("upper", "High", { type: "step", width: 1 }), out("middle", "Middle", { width: 1, dashed: true }), out("lower", "Low", { type: "step", width: 1 })],
    calc: (bars, p) => { const hh = lag(highest(source(bars, "high"), p.period), 1), ll = lag(lowest(source(bars, "low"), p.period), 1);
      return { upper: hh, middle: zip(hh, ll, (a, b) => (a + b) / 2), lower: ll }; } },
  envelope: { name: "Moving average envelope", short: "Envelope", group: "bands", pane: "price", fill: ["upper", "lower"], about: "Lines a fixed percentage above and below an average.",
    params: { period: period(20), percent: num("Distance (%)", 2.5, 0.1, 50, 0.1), type: pick("Type", "sma", MA_TYPES) },
    outputs: [out("upper", "Upper", { width: 1 }), out("middle", "Middle", { width: 1, dashed: true }), out("lower", "Lower", { width: 1 })],
    calc: (bars, p) => { const mid = maOf(p.type, closes(bars), p.period);
      return { upper: map(mid, (m) => m * (1 + p.percent / 100)), middle: mid, lower: map(mid, (m) => m * (1 - p.percent / 100)) }; } },
  vwap: { name: "VWAP (volume-weighted average price)", short: "VWAP", group: "bands", pane: "price", intraday: true, about: "The day's average price weighted by volume. Restarts every trading day.",
    params: {}, outputs: LINE, calc: (bars, p, ctx) => ({ line: vwapValues(bars, ctx.sessionOffset || 0) }) },
  pivots: { name: "Pivot points", short: "Pivots", group: "bands", pane: "price", about: "Support (S) and resistance (R) levels from the previous day, week or month's high, low and close.",
    params: { type: pick("Kind", "traditional", { traditional: "Traditional (floor)", fibonacci: "Fibonacci", woodie: "Woodie", camarilla: "Camarilla" }),
      timeframe: pick("Levels from the previous", "auto", { auto: "Auto (day on intraday charts, month on day charts)", D: "Day", W: "Week", M: "Month" }) },
    outputs: PIVOT_KEYS.map((k, i) => out(k, k.toUpperCase(), { type: "step", width: 1, dashed: k !== "p", colour: i < 3 ? 2 : i === 3 ? 0 : 1 })),
    calc: (bars, p, ctx) => {
      const freq = p.timeframe === "auto" ? ((ctx.seconds || 86400) < 86400 ? "D" : "M") : p.timeframe;
      return pivotValues(bars, freq, p.type, ctx.sessionOffset || 0);
    } },
  linreg_channel: { name: "Linear regression channel", short: "LR channel", group: "bands", pane: "price", repaints: true, fill: ["upper", "lower"], about: "A straight line fitted to the last N candles with bands at a number of standard deviations. Redrawn as new candles come.",
    params: { period: num("Candles", 100, 10, 1000), width: num("Width (standard deviations)", 2, 0.5, 5, 0.5) },
    outputs: [out("upper", "Upper", { width: 1 }), out("middle", "Middle", { width: 1 }), out("lower", "Lower", { width: 1 })],
    calc: (bars, p) => {
      const n = bars.length, res = { upper: blank(n), middle: blank(n), lower: blank(n) }, len = Math.min(p.period, n);
      if (len < 2) return res;
      const s = n - len; let sx = 0, sy = 0, sxy = 0, sxx = 0;
      for (let j = s; j < n; j++) { const x = j - s, y = bars[j].close; sx += x; sy += y; sxy += x * y; sxx += x * x; }
      const d = len * sxx - sx * sx, slope = d ? (len * sxy - sx * sy) / d : 0, icpt = (sy - slope * sx) / len;
      let q = 0; for (let j = s; j < n; j++) q += (bars[j].close - (icpt + slope * (j - s))) ** 2;
      const sd = Math.sqrt(q / len);
      for (let j = s; j < n; j++) { const m = icpt + slope * (j - s); res.middle[j] = m; res.upper[j] = m + p.width * sd; res.lower[j] = m - p.width * sd; }
      return res;
    } },
  ichimoku: { name: "Ichimoku cloud", short: "Ichimoku", group: "trend", pane: "price", future: 26, fill: ["spanA", "spanB"], about: "Conversion and base lines, a cloud drawn ahead of price, and the close drawn back.",
    params: { conversion: num("Conversion line period", 9, 1, 200), base: num("Base line period", 26, 1, 200), spanB: num("Leading span B period", 52, 1, 400), shift: num("Displacement", 26, 1, 100) },
    outputs: [out("conversion", "Conversion", { colour: 0, width: 1 }), out("base", "Base", { colour: 1, width: 1 }), out("spanA", "Span A", { colour: 2, width: 1 }),
      out("spanB", "Span B", { colour: 4, width: 1 }), out("lagging", "Lagging", { colour: 5, width: 1, back: true })],
    calc: (bars, p) => {
      const h = source(bars, "high"), l = source(bars, "low"), mid = (/** @type {number} */ n) => zip(highest(h, n), lowest(l, n), (a, b) => (a + b) / 2);
      const conv = mid(p.conversion), base = mid(p.base), d = Math.max(0, Math.round(p.shift) - 1);
      const lead = (/** @type {Values} */ v) => blank(d).concat(v), close = closes(bars);
      return { conversion: conv, base, spanA: lead(zip(conv, base, (a, b) => (a + b) / 2)), spanB: lead(mid(p.spanB)),
        lagging: close.map((_, i) => (i + d < close.length ? close[i + d] : null)) };
    } },
  supertrend: { name: "Supertrend", short: "Supertrend", group: "trend", pane: "price", about: "A trailing line under price in an uptrend and over it in a downtrend (ATR based).",
    params: { period: num("ATR period", 10, 1, 200), mult: num("Multiplier", 3, 0.5, 10, 0.5) },
    outputs: [out("up", "Uptrend", { colour: -1 }), out("down", "Downtrend", { colour: -2 })],
    calc: (bars, p) => { const r = supertrendValues(bars, p.period, p.mult); return { up: r.up, down: r.down }; } },
  psar: { name: "Parabolic SAR", short: "SAR", group: "trend", pane: "price", about: "Dots that trail price and flip sides when the trend turns.",
    params: { step: num("Step", 0.02, 0.001, 0.2, 0.001), maximum: num("Maximum", 0.2, 0.01, 1, 0.01) }, outputs: [out("line", "SAR", { type: "dots" })],
    calc: (bars, p) => ({ line: psarValues(bars, p.step, p.maximum) }) },
  chandelier: { name: "Chandelier exit", short: "Chandelier", group: "trend", pane: "price", about: "A stop a multiple of ATR below the highest high (long) or above the lowest low (short).",
    params: { period: period(22), mult: num("Multiplier", 3, 0.5, 10, 0.5) },
    outputs: [out("long", "Long stop", { colour: -1 }), out("short", "Short stop", { colour: -2 })],
    calc: (bars, p) => {
      const a = atrValues(bars, p.period), hh = highest(source(bars, "high"), p.period), ll = lowest(source(bars, "low"), p.period), n = bars.length;
      const res = { long: blank(n), short: blank(n) };
      /** @type {number|null} */ let ls = null, ss = null; let dir = 1;
      for (let i = 0; i < n; i++) {
        if (!ok(a[i]) || !ok(hh[i]) || !ok(ll[i])) continue;
        let l = /** @type {number} */ (hh[i]) - p.mult * /** @type {number} */ (a[i]), s = /** @type {number} */ (ll[i]) + p.mult * /** @type {number} */ (a[i]);
        if (ls != null && i && bars[i - 1].close > ls) l = Math.max(l, ls);
        if (ss != null && i && bars[i - 1].close < ss) s = Math.min(s, ss);
        if (ss != null && bars[i].close > ss) dir = 1; else if (ls != null && bars[i].close < ls) dir = -1;
        ls = l; ss = s;
        if (dir === 1) res.long[i] = l; else res.short[i] = s;
      }
      return res;
    } },
  volstop: { name: "Volatility stop", short: "Vol stop", group: "trend", pane: "price", about: "A stop that trails the close by a multiple of ATR and flips when price crosses it.",
    params: { period: period(20), mult: num("Multiplier", 2, 0.5, 10, 0.5) },
    outputs: [out("up", "Uptrend", { colour: -1 }), out("down", "Downtrend", { colour: -2 })],
    calc: (bars, p) => {
      const a = atrValues(bars, p.period), n = bars.length, res = { up: blank(n), down: blank(n) };
      let up = true, max = NaN, min = NaN; /** @type {number|null} */ let stop = null;
      for (let i = 0; i < n; i++) {
        const c = bars[i].close, at = a[i];
        if (!ok(at)) continue;
        if (stop == null) { max = c; min = c; stop = c - p.mult * /** @type {number} */ (at); }
        max = Math.max(max, c); min = Math.min(min, c);
        stop = up ? Math.max(stop, max - p.mult * /** @type {number} */ (at)) : Math.min(stop, min + p.mult * /** @type {number} */ (at));
        if (up && c < stop) { up = false; min = c; max = c; stop = c + p.mult * /** @type {number} */ (at); }
        else if (!up && c > stop) { up = true; max = c; min = c; stop = c - p.mult * /** @type {number} */ (at); }
        if (up) res.up[i] = stop; else res.down[i] = stop;
      }
      return res;
    } },
  zigzag: { name: "ZigZag", short: "ZigZag", group: "trend", pane: "price", repaints: true, about: "Joins swing highs and lows that are at least a set percentage apart. The last leg moves until the swing is confirmed.",
    params: { percent: num("Smallest swing (%)", 5, 0.1, 50, 0.1) }, outputs: [out("line", "ZigZag", { sparse: true })],
    calc: (bars, p) => {
      const n = bars.length, res = blank(n), up = 1 + p.percent / 100, down = 1 - p.percent / 100;
      if (!n) return { line: res };
      let dir = 0, ext = 0, hi = 0, lo = 0;
      for (let i = 1; i < n; i++) {
        if (dir === 0) {
          if (bars[i].high >= bars[hi].high) hi = i;
          if (bars[i].low <= bars[lo].low) lo = i;
          if (hi > lo && bars[hi].high >= bars[lo].low * up) { res[lo] = bars[lo].low; dir = 1; ext = hi; }
          else if (lo > hi && bars[lo].low <= bars[hi].high * down) { res[hi] = bars[hi].high; dir = -1; ext = lo; }
        } else if (dir === 1) {
          if (bars[i].high >= bars[ext].high) ext = i;
          else if (bars[i].low <= bars[ext].high * down) { res[ext] = bars[ext].high; dir = -1; ext = i; }
        } else if (bars[i].low <= bars[ext].low) ext = i;
        else if (bars[i].high >= bars[ext].low * up) { res[ext] = bars[ext].low; dir = 1; ext = i; }
      }
      if (dir !== 0) res[ext] = dir === 1 ? bars[ext].high : bars[ext].low;      // the swing still forming
      return { line: res };
    } },
  fractals: { name: "Williams fractals", short: "Fractals", group: "trend", pane: "price", repaints: true, about: "Marks a high with two lower highs each side, and a low with two higher lows each side. Shown two candles late.",
    params: { size: num("Candles each side", 2, 1, 10) }, outputs: [out("up", "Fractal high", { type: "dots", colour: -1, sparse: true }), out("down", "Fractal low", { type: "dots", colour: -2, sparse: true })],
    calc: (bars, p) => {
      const n = bars.length, k = p.size, res = { up: blank(n), down: blank(n) };
      for (let i = k; i < n - k; i++) {
        let up = true, down = true;
        for (let j = 1; j <= k; j++) {
          if (!(bars[i].high > bars[i - j].high && bars[i].high > bars[i + j].high)) up = false;
          if (!(bars[i].low < bars[i - j].low && bars[i].low < bars[i + j].low)) down = false;
        }
        if (up) res.up[i] = bars[i].high; if (down) res.down[i] = bars[i].low;
      }
      return res;
    } },

  // Trend strength (own pane)
  adx: { name: "ADX and DMI (directional movement)", short: "ADX", group: "trend", pane: "own", guides: [25], about: "How strong the trend is (ADX), and whether buyers (+DI) or sellers (−DI) lead.",
    params: { period: period(14, 200) }, outputs: [out("adx", "ADX", { colour: 5 }), out("plus", "+DI", { colour: -1, width: 1 }), out("minus", "−DI", { colour: -2, width: 1 })],
    calc: (bars, p) => adxValues(bars, p.period) },
  aroon: { name: "Aroon", short: "Aroon", group: "trend", pane: "own", guides: [70, 30], about: "How recently the highest high (up) and lowest low (down) of the period came, from 0 to 100.",
    params: { period: period(14, 200) }, outputs: [out("up", "Up", { colour: -1 }), out("down", "Down", { colour: -2 })],
    calc: (bars, p) => {
      const n = bars.length, k = p.period, res = { up: blank(n), down: blank(n) };
      for (let i = k; i < n; i++) {
        let hi = i, lo = i;
        for (let j = i - k; j <= i; j++) { if (bars[j].high >= bars[hi].high) hi = j; if (bars[j].low <= bars[lo].low) lo = j; }
        res.up[i] = 100 * (k - (i - hi)) / k; res.down[i] = 100 * (k - (i - lo)) / k;
      }
      return res;
    } },
  vortex: { name: "Vortex indicator", short: "VI", group: "trend", pane: "own", guides: [1], about: "Upward (VI+) against downward (VI−) movement; a crossing hints at a new trend.",
    params: { period: period(14, 200) }, outputs: [out("plus", "VI+", { colour: -1 }), out("minus", "VI−", { colour: -2 })],
    calc: (bars, p) => {
      const vp = bars.map((b, i) => (i ? Math.abs(b.high - bars[i - 1].low) : null)), vm = bars.map((b, i) => (i ? Math.abs(b.low - bars[i - 1].high) : null));
      const tr = sum(trueRange(bars).map((x, i) => (i ? x : null)), p.period);
      return { plus: zip(sum(vp, p.period), tr, (a, t) => (t ? a / t : null)), minus: zip(sum(vm, p.period), tr, (a, t) => (t ? a / t : null)) };
    } },
  chop: { name: "Choppiness index", short: "CHOP", group: "trend", pane: "own", guides: [61.8, 38.2], about: "High means a sideways, choppy market; low means a trending one.",
    params: { period: period(14, 200) }, outputs: LINE,
    calc: (bars, p) => { const s = sum(trueRange(bars), p.period), r = zip(highest(source(bars, "high"), p.period), lowest(source(bars, "low"), p.period), (a, b) => a - b);
      return { line: zip(s, r, (a, b) => (b > 0 && a > 0 ? 100 * Math.log10(a / b) / Math.log10(p.period) : null)) }; } },
  mass: { name: "Mass index", short: "Mass", group: "trend", pane: "own", guides: [27, 26.5], about: "Widening ranges: a rise above 27 then below 26.5 warns of a reversal.",
    params: { period: num("Sum period", 25, 2, 200), ema: num("EMA period", 9, 1, 100) }, outputs: LINE,
    calc: (bars, p) => { const r = bars.map((b) => b.high - b.low), e1 = emaValues(r, p.ema), e2 = emaValues(e1, p.ema);
      return { line: sum(zip(e1, e2, (a, b) => (b ? a / b : null)), p.period) }; } },
  stc: { name: "Schaff trend cycle", short: "STC", group: "trend", pane: "own", guides: [75, 25], about: "A MACD run through a stochastic twice: a fast trend signal from 0 to 100.",
    params: { fast: num("Fast period", 23, 1, 200), slow: num("Slow period", 50, 2, 400), cycle: num("Cycle", 10, 2, 100), factor: num("Smoothing (0 to 1)", 0.5, 0.05, 1, 0.05) }, outputs: LINE,
    calc: (bars, p) => {
      const c = closes(bars), m = zip(emaValues(c, p.fast), emaValues(c, p.slow), (a, b) => a - b);
      const stage = (/** @type {Values} */ x) => {
        const lo = lowest(x, p.cycle), hi = highest(x, p.cycle), res = blank(x.length);
        /** @type {number|null} */ let f = null, pf = null;
        for (let i = 0; i < x.length; i++) {
          if (!ok(x[i]) || !ok(lo[i]) || !ok(hi[i])) continue;
          const range = /** @type {number} */ (hi[i]) - /** @type {number} */ (lo[i]);
          f = range > 0 ? 100 * (/** @type {number} */ (x[i]) - /** @type {number} */ (lo[i])) / range : (f == null ? 0 : f);
          pf = pf == null ? f : pf + p.factor * (f - pf);
          res[i] = pf;
        }
        return res;
      };
      return { line: stage(stage(m)) };
    } },

  // Momentum and oscillators
  rsi: { name: "RSI (relative strength index)", short: "RSI", group: "momentum", pane: "own", guides: [70, 30], about: "Recent gains against losses, 0 to 100. Over 70 is often called overbought, under 30 oversold.",
    params: { period: num("Period", 14, 2, 200) }, outputs: LINE, calc: (bars, p) => ({ line: rsiValues(closes(bars), p.period) }) },
  stochastic: { name: "Stochastic", short: "Stoch", group: "momentum", pane: "own", guides: [80, 20], about: "Where the close sits in the period's range, 0 to 100 (%K), with its average (%D).",
    params: { period: num("%K period", 14, 1, 200), smoothK: num("%K smoothing", 1, 1, 50), smooth: num("%D period", 3, 1, 50) },
    outputs: [out("k", "%K"), out("d", "%D", { colour: 1 })],
    calc: (bars, p) => { const k = smaValues(stochK(bars, p.period), p.smoothK); return { k, d: smaValues(k, p.smooth) }; } },
  stochrsi: { name: "Stochastic RSI", short: "Stoch RSI", group: "momentum", pane: "own", guides: [80, 20], about: "The stochastic of RSI: where RSI sits in its own recent range.",
    params: { rsi: num("RSI period", 14, 2, 200), period: num("Stochastic period", 14, 1, 200), smoothK: num("%K smoothing", 3, 1, 50), smooth: num("%D period", 3, 1, 50) },
    outputs: [out("k", "%K"), out("d", "%D", { colour: 1 })],
    calc: (bars, p) => {
      const r = rsiValues(closes(bars), p.rsi), hi = highest(r, p.period), lo = lowest(r, p.period);
      const raw = r.map((x, i) => (ok(x) && ok(hi[i]) && ok(lo[i]) ? (hi[i] === lo[i] ? null : 100 * (/** @type {number} */ (x) - /** @type {number} */ (lo[i])) / (/** @type {number} */ (hi[i]) - /** @type {number} */ (lo[i]))) : null));
      const k = smaValues(raw, p.smoothK); return { k, d: smaValues(k, p.smooth) };
    } },
  macd: { name: "MACD", short: "MACD", group: "momentum", pane: "own", guides: [0], about: "The gap between a fast and a slow EMA, its signal line and the histogram between them.",
    params: { fast: num("Fast period", 12, 1, 200), slow: num("Slow period", 26, 2, 400), signal: num("Signal period", 9, 1, 200) },
    outputs: [out("histogram", "Histogram", { type: "histogram", signed: true }), out("macd", "MACD", { colour: 0 }), out("signal", "Signal", { colour: 1 })],
    calc: (bars, p) => macdValues(bars, /** @type {any} */ (p)) },
  cci: { name: "CCI (commodity channel index)", short: "CCI", group: "momentum", pane: "own", guides: [100, -100], about: "How far the typical price is from its average, in units of its average deviation.",
    params: { period: period(20, 200) }, outputs: LINE, calc: (bars, p) => ({ line: cciValues(bars, p.period) }) },
  willr: { name: "Williams %R", short: "%R", group: "momentum", pane: "own", guides: [-20, -80], about: "Where the close sits below the period's high, 0 to −100.",
    params: { period: period(14, 200) }, outputs: LINE, calc: (bars, p) => ({ line: map(stochK(bars, p.period), (k) => k - 100) }) },
  momentum: { name: "Momentum", short: "MOM", group: "momentum", pane: "own", guides: [0], about: "Today's price minus the price N candles ago.",
    params: { period: period(10, 200), source: src() }, outputs: LINE, calc: (bars, p) => ({ line: change(source(bars, p.source), p.period) }) },
  roc: { name: "Rate of change", short: "ROC", group: "momentum", pane: "own", guides: [0], about: "The percentage change over the last N candles.",
    params: { period: period(9, 200), source: src() }, outputs: LINE, calc: (bars, p) => ({ line: roc(source(bars, p.source), p.period) }) },
  ao: { name: "Awesome oscillator", short: "AO", group: "momentum", pane: "own", guides: [0], about: "A 5-candle against a 34-candle average of the midpoint price.",
    params: { fast: num("Fast period", 5, 1, 100), slow: num("Slow period", 34, 2, 200) }, outputs: [out("line", "AO", { type: "histogram", signed: true })],
    calc: (bars, p) => ({ line: aoValues(bars, p.fast, p.slow) }) },
  ac: { name: "Accelerator oscillator", short: "AC", group: "momentum", pane: "own", guides: [0], about: "The awesome oscillator minus its 5-candle average: whether momentum is speeding up.",
    params: {}, outputs: [out("line", "AC", { type: "histogram", signed: true })],
    calc: (bars) => { const a = aoValues(bars, 5, 34); return { line: zip(a, smaValues(a, 5), (x, y) => x - y) }; } },
  uo: { name: "Ultimate oscillator", short: "UO", group: "momentum", pane: "own", guides: [70, 30], about: "Buying pressure over three periods at once, 0 to 100.",
    params: { fast: num("Short period", 7, 1, 100), mid: num("Middle period", 14, 1, 200), slow: num("Long period", 28, 1, 400) }, outputs: LINE,
    calc: (bars, p) => {
      const bp = bars.map((b, i) => (i ? b.close - Math.min(b.low, bars[i - 1].close) : null)), tr = trueRange(bars).map((x, i) => (i ? x : null));
      const avg = (/** @type {number} */ n) => zip(sum(bp, n), sum(tr, n), (a, b) => (b ? a / b : null));
      const a = avg(p.fast), b = avg(p.mid), c = avg(p.slow);
      return { line: a.map((x, i) => (ok(x) && ok(b[i]) && ok(c[i]) ? 100 * (4 * /** @type {number} */ (x) + 2 * /** @type {number} */ (b[i]) + /** @type {number} */ (c[i])) / 7 : null)) };
    } },
  trix: { name: "TRIX", short: "TRIX", group: "momentum", pane: "own", guides: [0], about: "The percentage change of a triple-smoothed EMA.",
    params: { period: period(18, 200), signal: num("Signal period", 9, 1, 100) }, outputs: [out("line", "TRIX"), out("signal", "Signal", { colour: 1 })],
    calc: (bars, p) => { const e = emaValues(emaValues(emaValues(closes(bars), p.period), p.period), p.period), t = roc(e, 1); return { line: t, signal: emaValues(t, p.signal) }; } },
  tsi: { name: "True strength index", short: "TSI", group: "momentum", pane: "own", guides: [0], about: "Double-smoothed price change against double-smoothed absolute change.",
    params: { long: num("Long period", 25, 1, 200), short: num("Short period", 13, 1, 200), signal: num("Signal period", 13, 1, 100) }, outputs: [out("line", "TSI"), out("signal", "Signal", { colour: 1 })],
    calc: (bars, p) => {
      const pc = change(closes(bars), 1), num_ = emaValues(emaValues(pc, p.long), p.short), den = emaValues(emaValues(map(pc, Math.abs), p.long), p.short);
      const t = zip(num_, den, (a, b) => (b ? 100 * a / b : null)); return { line: t, signal: emaValues(t, p.signal) };
    } },
  ppo: { name: "Percentage price oscillator", short: "PPO", group: "momentum", pane: "own", guides: [0], about: "MACD as a percentage of the slow EMA, so stocks at different prices compare.",
    params: { fast: num("Fast period", 12, 1, 200), slow: num("Slow period", 26, 2, 400), signal: num("Signal period", 9, 1, 200) },
    outputs: [out("histogram", "Histogram", { type: "histogram", signed: true }), out("line", "PPO", { colour: 0 }), out("signal", "Signal", { colour: 1 })],
    calc: (bars, p) => { const c = closes(bars), s = emaValues(c, p.slow), l = zip(emaValues(c, p.fast), s, (a, b) => (b ? 100 * (a - b) / b : null)), sig = emaValues(l, p.signal);
      return { line: l, signal: sig, histogram: zip(l, sig, (a, b) => a - b) }; } },
  dpo: { name: "Detrended price oscillator", short: "DPO", group: "momentum", pane: "own", guides: [0], about: "Price against an older average, to show cycles without the trend.",
    params: { period: period(21, 200) }, outputs: LINE,
    calc: (bars, p) => { const c = closes(bars); return { line: zip(c, lag(smaValues(c, p.period), Math.floor(p.period / 2) + 1), (a, b) => a - b) }; } },
  cmo: { name: "Chande momentum oscillator", short: "CMO", group: "momentum", pane: "own", guides: [50, -50], about: "Sum of gains minus sum of losses over their total, −100 to 100.",
    params: { period: period(9, 200) }, outputs: LINE,
    calc: (bars, p) => { const d = change(closes(bars), 1), up = sum(map(d, (x) => Math.max(x, 0)), p.period), dn = sum(map(d, (x) => Math.max(-x, 0)), p.period);
      return { line: zip(up, dn, (a, b) => (a + b ? 100 * (a - b) / (a + b) : null)) }; } },
  crsi: { name: "Connors RSI", short: "CRSI", group: "momentum", pane: "own", guides: [90, 10], about: "The average of a short RSI, the RSI of the up/down streak, and the rank of today's change.",
    params: { rsi: num("RSI period", 3, 2, 50), streak: num("Streak RSI period", 2, 2, 50), rank: num("Rank period", 100, 10, 500) }, outputs: LINE,
    calc: (bars, p) => {
      const c = closes(bars), streak = [0];
      for (let i = 1; i < c.length; i++) { const s = streak[i - 1]; streak.push(c[i] > c[i - 1] ? (s > 0 ? s + 1 : 1) : c[i] < c[i - 1] ? (s < 0 ? s - 1 : -1) : 0); }
      const r1 = rsiValues(c, p.rsi), r2 = rsiValues(streak, p.streak), chg = roc(c, 1);
      const rank = chg.map((x, i) => {
        if (!ok(x) || i < p.rank) return null;
        let below = 0; for (let j = i - p.rank; j < i; j++) if (ok(chg[j]) && /** @type {number} */ (chg[j]) < /** @type {number} */ (x)) below++;
        return 100 * below / p.rank;
      });
      return { line: r1.map((a, i) => (ok(a) && ok(r2[i]) && ok(rank[i]) ? (/** @type {number} */ (a) + /** @type {number} */ (r2[i]) + /** @type {number} */ (rank[i])) / 3 : null)) };
    } },
  fisher: { name: "Fisher transform", short: "Fisher", group: "momentum", pane: "own", guides: [0], about: "Turns the price's place in its range into sharp turning points.",
    params: { period: period(9, 200) }, outputs: [out("line", "Fisher"), out("trigger", "Trigger", { colour: 1 })],
    calc: (bars, p) => {
      const m = source(bars, "hl2"), hi = highest(m, p.period), lo = lowest(m, p.period), n = bars.length, res = { line: blank(n), trigger: blank(n) };
      let v = 0, f = 0, started = false;
      for (let i = 0; i < n; i++) {
        if (!ok(hi[i]) || !ok(lo[i])) continue;
        const range = /** @type {number} */ (hi[i]) - /** @type {number} */ (lo[i]);
        v = 0.66 * (range ? (/** @type {number} */ (m[i]) - /** @type {number} */ (lo[i])) / range - 0.5 : 0) + 0.67 * v;
        v = Math.max(-0.999, Math.min(0.999, v));
        const prev = f;
        f = 0.5 * Math.log((1 + v) / (1 - v)) + 0.5 * f;
        res.line[i] = f; if (started) res.trigger[i] = prev; started = true;
      }
      return res;
    } },
  coppock: { name: "Coppock curve", short: "Coppock", group: "momentum", pane: "own", guides: [0], about: "A weighted average of two long rates of change; a turn up from below zero is its buy signal.",
    params: { period: num("Average period", 10, 1, 100), long: num("Long change period", 14, 1, 100), short: num("Short change period", 11, 1, 100) }, outputs: LINE,
    calc: (bars, p) => { const c = closes(bars); return { line: wma(zip(roc(c, p.long), roc(c, p.short), (a, b) => a + b), p.period) }; } },
  kst: { name: "Know sure thing", short: "KST", group: "momentum", pane: "own", guides: [0], about: "Four smoothed rates of change added together, with a signal line.",
    params: { signal: num("Signal period", 9, 1, 100) }, outputs: [out("line", "KST"), out("signal", "Signal", { colour: 1 })],
    calc: (bars, p) => {
      const c = closes(bars), part = (/** @type {number} */ r, /** @type {number} */ s) => smaValues(roc(c, r), s);
      const a = part(10, 10), b = part(15, 10), d = part(20, 10), e = part(30, 15);
      const k = a.map((x, i) => (ok(x) && ok(b[i]) && ok(d[i]) && ok(e[i]) ? /** @type {number} */ (x) + 2 * /** @type {number} */ (b[i]) + 3 * /** @type {number} */ (d[i]) + 4 * /** @type {number} */ (e[i]) : null));
      return { line: k, signal: smaValues(k, p.signal) };
    } },
  rvgi: { name: "Relative vigor index", short: "RVGI", group: "momentum", pane: "own", guides: [0], about: "Close minus open against high minus low: whether candles close strong.",
    params: { period: period(10, 200) }, outputs: [out("line", "RVGI"), out("signal", "Signal", { colour: 1 })],
    calc: (bars, p) => { const r = zip(sum(swma(bars.map((b) => b.close - b.open)), p.period), sum(swma(bars.map((b) => b.high - b.low)), p.period), (a, b) => (b ? a / b : null));
      return { line: r, signal: swma(r) }; } },
  smi: { name: "Stochastic momentum index", short: "SMI", group: "momentum", pane: "own", guides: [40, -40], about: "Where the close sits against the middle of the range, double-smoothed, −100 to 100.",
    params: { period: num("%K period", 10, 1, 200), smooth: num("Smoothing", 3, 1, 50), signal: num("Signal period", 3, 1, 50) }, outputs: [out("line", "SMI"), out("signal", "Signal", { colour: 1 })],
    calc: (bars, p) => {
      const hh = highest(source(bars, "high"), p.period), ll = lowest(source(bars, "low"), p.period);
      const rel = bars.map((b, i) => (ok(hh[i]) && ok(ll[i]) ? b.close - (/** @type {number} */ (hh[i]) + /** @type {number} */ (ll[i])) / 2 : null));
      const range = zip(hh, ll, (a, b) => a - b), e2 = (/** @type {Values} */ v) => emaValues(emaValues(v, p.smooth), p.smooth);
      const s = zip(e2(rel), e2(range), (a, b) => (b ? 200 * a / b : null)); return { line: s, signal: emaValues(s, p.signal) };
    } },
  bbp: { name: "Bull and bear power (Elder)", short: "BBP", group: "momentum", pane: "own", guides: [0], about: "High plus low against twice the 13-candle EMA: above zero buyers lead.",
    params: { period: period(13, 200) }, outputs: [out("line", "Power", { type: "histogram", signed: true })],
    calc: (bars, p) => { const e = emaValues(closes(bars), p.period); return { line: map(e, (x, i) => bars[i].high + bars[i].low - 2 * x) }; } },
  bop: { name: "Balance of power", short: "BOP", group: "momentum", pane: "own", guides: [0], about: "Close minus open over high minus low, averaged: who controlled the candles.",
    params: { period: num("Average period", 14, 1, 200) }, outputs: LINE,
    calc: (bars, p) => ({ line: smaValues(bars.map((b) => (b.high > b.low ? (b.close - b.open) / (b.high - b.low) : 0)), p.period) }) },
  rci: { name: "Rank correlation index", short: "RCI", group: "momentum", pane: "own", guides: [80, -80], about: "How closely the last N closes rise in time order, −100 to 100.",
    params: { period: period(9, 200) }, outputs: LINE,
    calc: (bars, p) => {
      const c = closes(bars), k = p.period;
      return { line: rolling(c, k, (s, e) => {
        const idx = Array.from({ length: k }, (_, j) => s + j).sort((a, b) => c[b] - c[a] || b - a);
        let d = 0; idx.forEach((j, rank) => { d += ((e - j + 1) - (rank + 1)) ** 2; });
        return 100 * (1 - 6 * d / (k * (k * k - 1)));
      }) };
    } },

  // Volatility
  atr: { name: "Average true range", short: "ATR", group: "volatility", pane: "own", about: "The average size of a candle including gaps: how much price moves.",
    params: { period: period(14, 200) }, outputs: LINE, calc: (bars, p) => ({ line: atrValues(bars, p.period) }) },
  stdev: { name: "Standard deviation", short: "StdDev", group: "volatility", pane: "own", about: "How spread out the last N closes are.",
    params: { period: period(20, 500), source: src() }, outputs: LINE, calc: (bars, p) => ({ line: stdev(source(bars, p.source), p.period) }) },
  hv: { name: "Historical volatility", short: "HV", group: "volatility", pane: "own", about: "The yearly percentage volatility of the closes over the last N candles.",
    params: { period: period(10, 500) }, outputs: LINE,
    calc: (bars, p, ctx) => {
      const c = closes(bars), r = c.map((x, i) => (i && c[i - 1] > 0 && x > 0 ? Math.log(x / c[i - 1]) : null)), s = ctx.seconds || 86400;
      const crypto = ctx.market === "crypto", days = crypto ? 365 : 252, perDay = s >= 86400 ? 1 : (crypto ? 86400 : 22500) / s;
      const perYear = s >= 604800 ? 52 : days * perDay, sd = rolling(r, p.period, (a, e) => {
        let m = 0; for (let j = a; j <= e; j++) m += /** @type {number} */ (r[j]); m /= p.period;
        let q = 0; for (let j = a; j <= e; j++) q += (/** @type {number} */ (r[j]) - m) ** 2;
        return p.period > 1 ? Math.sqrt(q / (p.period - 1)) : 0;
      });
      return { line: map(sd, (x) => 100 * x * Math.sqrt(perYear)) };
    } },
  bbpct: { name: "Bollinger Bands %B", short: "%B", group: "volatility", pane: "own", guides: [1, 0], about: "Where price sits inside the Bollinger Bands: 1 at the upper band, 0 at the lower.",
    params: { period: num("Period", 20, 2, 500), width: num("Width (standard deviations)", 2, 0.5, 5, 0.5) }, outputs: LINE,
    calc: (bars, p) => { const b = bollingerValues(bars, /** @type {any} */ (p)); return { line: b.upper.map((u, i) => (ok(u) && ok(b.lower[i]) && u !== b.lower[i] ? (bars[i].close - /** @type {number} */ (b.lower[i])) / (/** @type {number} */ (u) - /** @type {number} */ (b.lower[i])) : null)) }; } },
  bbw: { name: "Bollinger BandWidth", short: "BBW", group: "volatility", pane: "own", about: "The width of the Bollinger Bands as a percentage of the middle: low values come before big moves.",
    params: { period: num("Period", 20, 2, 500), width: num("Width (standard deviations)", 2, 0.5, 5, 0.5) }, outputs: LINE,
    calc: (bars, p) => { const b = bollingerValues(bars, /** @type {any} */ (p)); return { line: b.upper.map((u, i) => (ok(u) && ok(b.middle[i]) && b.middle[i] ? 100 * (/** @type {number} */ (u) - /** @type {number} */ (b.lower[i])) / /** @type {number} */ (b.middle[i]) : null)) }; } },
  ulcer: { name: "Ulcer index", short: "Ulcer", group: "volatility", pane: "own", about: "How deep and how long price has been below its recent high.",
    params: { period: period(14, 200) }, outputs: LINE,
    calc: (bars, p) => { const c = closes(bars), hh = highest(c, p.period), dd = zip(c, hh, (a, b) => (b ? (100 * (a - b) / b) ** 2 : null));
      return { line: map(smaValues(dd, p.period), Math.sqrt) }; } },
  chaikin_vol: { name: "Chaikin volatility", short: "CV", group: "volatility", pane: "own", guides: [0], about: "The percentage change of the average high-low range.",
    params: { period: period(10, 200), change: num("Change period", 10, 1, 200) }, outputs: LINE,
    calc: (bars, p) => ({ line: roc(emaValues(bars.map((b) => b.high - b.low), p.period), p.change) }) },
  rvi: { name: "Relative volatility index", short: "RVI", group: "volatility", pane: "own", guides: [80, 20], about: "Like RSI, but on the standard deviation of closes on up and down candles.",
    params: { period: num("Standard deviation period", 10, 2, 200), smooth: num("Smoothing period", 14, 1, 200) }, outputs: LINE,
    calc: (bars, p) => {
      const c = closes(bars), sd = stdev(c, p.period), d = change(c, 1);
      const up = emaValues(sd.map((s, i) => (ok(s) && ok(d[i]) ? (/** @type {number} */ (d[i]) > 0 ? s : 0) : null)), p.smooth);
      const dn = emaValues(sd.map((s, i) => (ok(s) && ok(d[i]) ? (/** @type {number} */ (d[i]) <= 0 ? s : 0) : null)), p.smooth);
      return { line: zip(up, dn, (a, b) => (a + b ? 100 * a / (a + b) : null)) };
    } },

  // Volume
  vol_ma: { name: "Volume with average", short: "Volume", group: "volume", pane: "own", about: "Volume bars in their own pane with an average line.",
    params: { period: num("Average period", 20, 1, 500) }, outputs: [out("volume", "Volume", { type: "histogram", updown: true }), out("average", "Average", { colour: 1 })],
    calc: (bars, p) => { const v = bars.map((b) => b.volume); return { volume: v, average: smaValues(v, p.period) }; } },
  obv: { name: "On-balance volume", short: "OBV", group: "volume", pane: "own", about: "A running total adding volume on up closes and taking it away on down closes.",
    params: {}, outputs: LINE, calc: (bars) => ({ line: cum(bars.map((b, i) => (i ? Math.sign(b.close - bars[i - 1].close) * b.volume : 0))) }) },
  ad: { name: "Accumulation/distribution", short: "A/D", group: "volume", pane: "own", about: "A running total of volume weighted by where each close sits in its candle.",
    params: {}, outputs: LINE, calc: (bars) => ({ line: cum(bars.map((b) => (b.high > b.low ? ((2 * b.close - b.low - b.high) / (b.high - b.low)) * b.volume : 0))) }) },
  cmf: { name: "Chaikin money flow", short: "CMF", group: "volume", pane: "own", guides: [0], about: "Volume weighted by where closes sit in their candles, over N candles, −1 to 1.",
    params: { period: period(20, 200) }, outputs: LINE,
    calc: (bars, p) => { const mfv = bars.map((b) => (b.high > b.low ? ((2 * b.close - b.low - b.high) / (b.high - b.low)) * b.volume : 0));
      return { line: zip(sum(mfv, p.period), sum(bars.map((b) => b.volume), p.period), (a, b) => (b ? a / b : null)) }; } },
  chaikin_osc: { name: "Chaikin oscillator", short: "Chaikin", group: "volume", pane: "own", guides: [0], about: "A fast minus a slow EMA of accumulation/distribution.",
    params: { fast: num("Fast period", 3, 1, 100), slow: num("Slow period", 10, 2, 200) }, outputs: LINE,
    calc: (bars, p) => { const a = cum(bars.map((b) => (b.high > b.low ? ((2 * b.close - b.low - b.high) / (b.high - b.low)) * b.volume : 0)));
      return { line: zip(emaValues(a, p.fast), emaValues(a, p.slow), (x, y) => x - y) }; } },
  mfi: { name: "Money flow index", short: "MFI", group: "volume", pane: "own", guides: [80, 20], about: "RSI that counts volume: money flowing in against out, 0 to 100.",
    params: { period: period(14, 200) }, outputs: LINE,
    calc: (bars, p) => {
      const tp = source(bars, "hlc3"), pos = tp.map((x, i) => (i ? (/** @type {number} */ (x) > /** @type {number} */ (tp[i - 1]) ? /** @type {number} */ (x) * bars[i].volume : 0) : null));
      const neg = tp.map((x, i) => (i ? (/** @type {number} */ (x) < /** @type {number} */ (tp[i - 1]) ? /** @type {number} */ (x) * bars[i].volume : 0) : null));
      return { line: zip(sum(pos, p.period), sum(neg, p.period), (a, b) => (a + b ? 100 * a / (a + b) : null)) };
    } },
  eom: { name: "Ease of movement", short: "EOM", group: "volume", pane: "own", guides: [0], about: "How far price moves for its volume: high when it rises on little volume.",
    params: { period: period(14, 200), divisor: num("Divisor", 10000, 1, 1e9) }, outputs: LINE,
    calc: (bars, p) => { const m = source(bars, "hl2"), d = change(m, 1);
      return { line: smaValues(d.map((x, i) => (ok(x) && bars[i].volume ? p.divisor * /** @type {number} */ (x) * (bars[i].high - bars[i].low) / bars[i].volume : (ok(x) ? 0 : null))), p.period) }; } },
  force: { name: "Force index (Elder)", short: "EFI", group: "volume", pane: "own", guides: [0], about: "Price change times volume, smoothed: the force behind a move.",
    params: { period: period(13, 200) }, outputs: LINE, calc: (bars, p) => ({ line: emaValues(map(change(closes(bars), 1), (x, i) => x * bars[i].volume), p.period) }) },
  klinger: { name: "Klinger oscillator", short: "KVO", group: "volume", pane: "own", guides: [0], about: "Fast minus slow EMA of volume signed by the typical price's direction, with a signal.",
    params: { fast: num("Fast period", 34, 1, 200), slow: num("Slow period", 55, 2, 400), signal: num("Signal period", 13, 1, 100) }, outputs: [out("line", "KVO"), out("signal", "Signal", { colour: 1 })],
    calc: (bars, p) => { const d = change(source(bars, "hlc3"), 1), sv = map(d, (x, i) => (x >= 0 ? bars[i].volume : -bars[i].volume));
      const k = zip(emaValues(sv, p.fast), emaValues(sv, p.slow), (a, b) => a - b); return { line: k, signal: emaValues(k, p.signal) }; } },
  pvt: { name: "Price volume trend", short: "PVT", group: "volume", pane: "own", about: "A running total of volume times the percentage price change.",
    params: {}, outputs: LINE, calc: (bars) => ({ line: cum(bars.map((b, i) => (i && bars[i - 1].close ? (b.close / bars[i - 1].close - 1) * b.volume : 0))) }) },
  vo: { name: "Volume oscillator", short: "VO", group: "volume", pane: "own", guides: [0], about: "A fast against a slow EMA of volume, in percent.",
    params: { fast: num("Fast period", 5, 1, 200), slow: num("Slow period", 10, 2, 400) }, outputs: LINE,
    calc: (bars, p) => { const v = bars.map((b) => b.volume); return { line: zip(emaValues(v, p.fast), emaValues(v, p.slow), (a, b) => (b ? 100 * (a - b) / b : null)) }; } },
  nvi: { name: "Negative volume index", short: "NVI", group: "volume", pane: "own", about: "Moves only on days volume falls: what the quiet money does. Starts at 1,000.",
    params: { period: num("Average period", 255, 1, 500) }, outputs: [out("line", "NVI"), out("average", "Average", { colour: 1 })],
    calc: (bars, p) => { let x = 1000; const v = bars.map((b, i) => { if (i && b.volume < bars[i - 1].volume && bars[i - 1].close) x *= b.close / bars[i - 1].close; return x; });
      return { line: v, average: emaValues(v, p.period) }; } },
  pvi: { name: "Positive volume index", short: "PVI", group: "volume", pane: "own", about: "Moves only on days volume rises: what the crowd does. Starts at 1,000.",
    params: { period: num("Average period", 255, 1, 500) }, outputs: [out("line", "PVI"), out("average", "Average", { colour: 1 })],
    calc: (bars, p) => { let x = 1000; const v = bars.map((b, i) => { if (i && b.volume > bars[i - 1].volume && bars[i - 1].close) x *= b.close / bars[i - 1].close; return x; });
      return { line: v, average: emaValues(v, p.period) }; } },
  net_volume: { name: "Net volume", short: "Net vol", group: "volume", pane: "own", guides: [0], about: "Volume counted as plus on up closes and minus on down closes.",
    params: {}, outputs: [out("line", "Net volume", { type: "histogram", signed: true })],
    calc: (bars) => ({ line: bars.map((b, i) => (i ? Math.sign(b.close - bars[i - 1].close) * b.volume : null)) }) },
};

/** The default settings of an indicator. @param {string} kind @returns {Record<string, number|string>} */
export function defaultsOf(kind) {
  return Object.fromEntries(Object.entries(CATALOG[kind].params).map(([k, p]) => [k, p.value]));
}

/** Settings checked against the catalogue: unknown keys dropped, numbers clamped, options from the list.
 * @param {string} kind @param {any} given @returns {Record<string, number|string>} */
export function cleanParams(kind, given) {
  const out = defaultsOf(kind), spec = CATALOG[kind].params;
  if (!given || typeof given !== "object") return out;
  for (const [k, p] of Object.entries(spec)) {
    const v = given[k];
    if (p.options) { if (typeof v === "string" && v in p.options) out[k] = v; continue; }
    const x = Number(v);
    if (v == null || v === "" || !Number.isFinite(x)) continue;
    const clamped = Math.min(/** @type {number} */ (p.max), Math.max(/** @type {number} */ (p.min), x));
    out[k] = p.step ? clamped : Math.round(clamped);
  }
  return out;
}

/** The indicators whose names, short names, group or description match a search. @param {string} query */
export function search(query) {
  const q = String(query || "").toLowerCase().trim(), words = q.split(/\s+/).filter(Boolean);
  const scored = Object.entries(CATALOG).map(([kind, c], order) => {
    const name = `${c.name} ${c.short}`.toLowerCase(), text = `${kind} ${name} ${GROUPS[/** @type {keyof GROUPS} */ (c.group)]} ${c.about}`.toLowerCase();
    if (!words.every((w) => text.includes(w))) return null;
    // the best matches first: the short name itself, then names that start with or hold the words
    const score = !q ? 0 : c.short.toLowerCase() === q || kind === q ? 0 : name.startsWith(q) ? 1 : words.every((w) => name.includes(w)) ? 2 : 3;
    return { kind, score, order };
  }).filter((x) => x != null);
  return /** @type {{kind: string, score: number, order: number}[]} */ (scored).sort((a, b) => a.score - b.score || a.order - b.order).map((x) => x.kind);
}

/** An indicator's lines as chart points. Values past the last bar (lines drawn ahead) get times one candle
 * apart. @param {string} kind @param {Bar[]} bars @param {Record<string, any>} params @param {Ctx} [ctx]
 * @returns {Record<string, Point[]>} */
export function compute(kind, bars, params, ctx = {}) {
  const c = CATALOG[kind], p = cleanParams(kind, params), seconds = ctx.seconds || 60;
  const raw = c.calc(bars, p, ctx), n = bars.length;
  /** @type {Record<string, Point[]>} */ const res = {};
  for (const o of c.outputs) {
    const v = raw[o.key] || [];
    /** @type {Point[]} */ const pts = [];
    for (let i = 0; i < v.length; i++) {
      const x = v[i];
      if (x == null || !Number.isFinite(x)) continue;
      if (!n) break;
      const time = i < n ? bars[i].time : bars[n - 1].time + (i - n + 1) * seconds;
      pts.push({ time, value: x });
    }
    res[o.key] = pts;
  }
  return res;
}
