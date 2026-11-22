// @ts-check
// Chart indicators, computed in the browser so they move with every live price.
// They match the desk's server-side formulas (desk/indicators.py): EMAs seed with the first value and show
// once `period` values are in (pandas ewm adjust=False, min_periods), RSI uses Wilder's smoothing, Bollinger
// Bands use the population standard deviation, VWAP restarts each trading day.

/** @typedef {{ time: number, open: number, high: number, low: number, close: number, volume: number }} Bar */
/** @typedef {{ time: number, value: number }} Point */
/** @typedef {(number|null)[]} Values */

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
    if (v == null) { if (level != null && count >= minPeriods) out[i] = level; continue; }
    level = level == null ? v : alpha * v + (1 - alpha) * level;
    count++;
    if (count >= minPeriods) out[i] = level;
  }
  return out;
}

/** @param {Values} values @param {number} period */
export const emaValues = (values, period) => ewmValues(values, 2 / (period + 1), period);

/** @param {Bar[]} bars @param {Values} values @returns {Point[]} */
function points(bars, values) {
  /** @type {Point[]} */ const out = [];
  for (let i = 0; i < bars.length; i++) {
    const v = values[i];
    if (v != null && Number.isFinite(v)) out.push({ time: bars[i].time, value: v });
  }
  return out;
}

/** @param {Bar[]} bars */
const closes = (bars) => bars.map((b) => b.close);

/** @param {Bar[]} bars @param {{ period: number }} p */
export function sma(bars, { period }) { return { line: points(bars, smaValues(closes(bars), period)) }; }

/** @param {Bar[]} bars @param {{ period: number }} p */
export function ema(bars, { period }) { return { line: points(bars, emaValues(closes(bars), period)) }; }

/** @param {Bar[]} bars @param {{ period: number, width: number }} p */
export function bollinger(bars, { period, width }) {
  const close = closes(bars), mid = smaValues(close, period);
  /** @type {Values} */ const upper = [], lower = [];
  for (let i = 0; i < close.length; i++) {
    const m = mid[i];
    if (m == null) { upper.push(null); lower.push(null); continue; }
    let sq = 0;
    for (let j = i - period + 1; j <= i; j++) sq += (close[j] - m) ** 2;
    const sd = Math.sqrt(sq / period);
    upper.push(m + width * sd); lower.push(m - width * sd);
  }
  return { upper: points(bars, upper), middle: points(bars, mid), lower: points(bars, lower) };
}

/** @param {Bar[]} bars @param {{ period: number }} p */
export function rsi(bars, { period }) {
  const close = closes(bars);
  /** @type {Values} */ const gains = [null];
  /** @type {Values} */ const losses = [null];
  for (let i = 1; i < close.length; i++) {
    const d = close[i] - close[i - 1];
    gains.push(Math.max(d, 0)); losses.push(Math.max(-d, 0));
  }
  const g = ewmValues(gains, 1 / period, period), l = ewmValues(losses, 1 / period, period);
  /** @type {Values} */ const out = g.map((gain, i) => {
    const loss = l[i];
    if (gain == null || loss == null) return null;
    return loss === 0 ? 100 : 100 - 100 / (1 + gain / loss);
  });
  return { line: points(bars, out) };
}

/** @param {Bar[]} bars @param {{ fast: number, slow: number, signal: number }} p */
export function macd(bars, { fast, slow, signal }) {
  const close = closes(bars), f = emaValues(close, fast), s = emaValues(close, slow);
  /** @type {Values} */ const line = f.map((v, i) => (v == null || s[i] == null ? null : v - /** @type {number} */ (s[i])));
  const sig = emaValues(line, signal);
  /** @type {Values} */ const hist = line.map((v, i) => (v == null || sig[i] == null ? null : v - /** @type {number} */ (sig[i])));
  return { macd: points(bars, line), signal: points(bars, sig), histogram: points(bars, hist) };
}

/** VWAP restarting each trading day. Bar times are on the chart clock; `sessionOffset` seconds after midnight
 * on that clock is when the market's day starts (0 for NSE/BSE, 19800 for crypto's 00:00 UTC).
 * @param {Bar[]} bars @param {{ sessionOffset?: number }} p */
export function vwap(bars, { sessionOffset = 0 } = {}) {
  /** @type {Values} */ const out = [];
  let day = NaN, pv = 0, vol = 0;
  for (const b of bars) {
    const d = Math.floor((b.time - sessionOffset) / 86400);
    if (d !== day) { day = d; pv = 0; vol = 0; }
    pv += ((b.high + b.low + b.close) / 3) * b.volume; vol += b.volume;
    out.push(vol > 0 ? pv / vol : null);
  }
  return { line: points(bars, out) };
}

// What the Indicators menu offers. `pane` "price" draws on the candles; "own" gets a pane of its own.
export const CATALOG = {
  sma: { name: "Simple moving average", short: "SMA", pane: "price", params: { period: { label: "Period", value: 20, min: 1, max: 500 } } },
  ema: { name: "Exponential moving average", short: "EMA", pane: "price", params: { period: { label: "Period", value: 50, min: 1, max: 500 } } },
  bollinger: { name: "Bollinger Bands", short: "BB", pane: "price", params: {
    period: { label: "Period", value: 20, min: 2, max: 500 }, width: { label: "Width (standard deviations)", value: 2, min: 0.5, max: 5, step: 0.5 } } },
  vwap: { name: "VWAP (volume-weighted average price)", short: "VWAP", pane: "price", intraday: true, params: {} },
  rsi: { name: "RSI (relative strength index)", short: "RSI", pane: "own", params: { period: { label: "Period", value: 14, min: 2, max: 200 } } },
  macd: { name: "MACD", short: "MACD", pane: "own", params: {
    fast: { label: "Fast period", value: 12, min: 1, max: 200 }, slow: { label: "Slow period", value: 26, min: 2, max: 400 },
    signal: { label: "Signal period", value: 9, min: 1, max: 200 } } },
};

export const COMPUTE = { sma, ema, bollinger, vwap, rsi, macd };
