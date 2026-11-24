// @ts-check
// Candle patterns on the chart, what followed them on this stock, and a range of likely prices ahead.
// The rules are the desk's (desk/candles.py, which explains them); a test checks the two give the same
// answers. A pattern is known only once its last candle has closed, and the odds use only the candles shown,
// so a replay never sees past its cut.

import { atrValues, smaValues } from "./indicators.js";

/** @typedef {import("./indicators.js").Bar} Bar */
/** @typedef {{ cases: number, up_pct: number|null, avg_pct: number|null, median_pct: number|null, worst_pct: number|null, best_pct: number|null }} Stats */
/** @typedef {{ key: string, text: string, z: number|null }} Verdict */
/** @typedef {{ trend: "any"|"up"|"down", place: "any"|"low"|"high", volume: boolean, near: number }} Filters */

/** key: [name, textbook signal, candles, what it looks like]. Same as desk/candles.py. */
export const PATTERNS = /** @type {Record<string, [string, "up"|"down"|"neutral", number, string]>} */ ({
  doji: ["Doji", "neutral", 1, "Open and close almost equal: buyers and sellers ended level."],
  hammer: ["Hammer", "up", 1, "After a fall: small body at the top, a lower wick at least twice the body."],
  inverted_hammer: ["Inverted Hammer", "up", 1, "After a fall: small body at the bottom, a long upper wick."],
  hanging_man: ["Hanging Man", "down", 1, "After a rise: the Hammer's shape, small body on a long lower wick."],
  shooting_star: ["Shooting Star", "down", 1, "After a rise: small body at the bottom, a long upper wick."],
  bullish_marubozu: ["Bullish Marubozu", "up", 1, "A large green candle with almost no wicks."],
  bearish_marubozu: ["Bearish Marubozu", "down", 1, "A large red candle with almost no wicks."],
  bullish_engulfing: ["Bullish Engulfing", "up", 2, "A green body that covers the whole red body before it."],
  bearish_engulfing: ["Bearish Engulfing", "down", 2, "A red body that covers the whole green body before it."],
  bullish_harami: ["Bullish Harami", "up", 2, "A small green body inside a large red body before it."],
  bearish_harami: ["Bearish Harami", "down", 2, "A small red body inside a large green body before it."],
  piercing_line: ["Piercing Line", "up", 2, "Opens below a large red candle's close, closes above its middle."],
  dark_cloud_cover: ["Dark Cloud Cover", "down", 2, "Opens above a large green candle's close, closes below its middle."],
  tweezer_bottom: ["Tweezer Bottom", "up", 2, "After a fall: a red then a green candle with the same low."],
  tweezer_top: ["Tweezer Top", "down", 2, "After a rise: a green then a red candle with the same high."],
  morning_star: ["Morning Star", "up", 3, "A large red candle, a small candle at or below its close, then a green candle closing above the red one's middle."],
  evening_star: ["Evening Star", "down", 3, "A large green candle, a small candle at or above its close, then a red candle closing below the green one's middle."],
  three_white_soldiers: ["Three White Soldiers", "up", 3, "Three green candles, each opening inside the one before and closing higher, with small upper wicks."],
  three_black_crows: ["Three Black Crows", "down", 3, "Three red candles, each opening inside the one before and closing lower, with small lower wicks."],
});
export const KEYS = Object.keys(PATTERNS);
export const HORIZONS = [1, 5, 10];
export const MIN_CASES = 30;
const AVG = 10, TREND_BACK = 5, FAST = 20, SLOW = 50, LEVEL_BARS = 20, VOLUME_BARS = 20, ATR_BARS = 14, VOL_BARS = 20;
export const DEFAULT_FILTERS = /** @type {Filters} */ ({ trend: "any", place: "any", volume: false, near: 1 });

/** @param {any} raw @returns {Filters} */
export function cleanFilters(raw) {
  const r = raw && typeof raw === "object" ? raw : {};
  const near = Number(r.near);
  return { trend: ["up", "down"].includes(r.trend) ? r.trend : "any", place: ["low", "high"].includes(r.place) ? r.place : "any",
    volume: r.volume === true, near: Number.isFinite(near) && near >= 0.1 && near <= 5 ? near : 1 };
}

/** Mean of the `n` values before each index (not the index itself). @param {number[]} v @param {number} n */
function priorMean(v, n) {
  const m = smaValues(v, n);
  return v.map((_, i) => (i ? m[i - 1] : null));
}

/** Which patterns close on each candle: an array, one list of pattern keys per candle.
 * @param {Bar[]} bars @returns {string[][]} */
export function detect(bars) {
  const n = bars.length;
  const o = bars.map((b) => b.open), hi = bars.map((b) => b.high), lo = bars.map((b) => b.low), c = bars.map((b) => b.close);
  const body = c.map((x, i) => Math.abs(x - o[i])), rng = hi.map((x, i) => x - lo[i]);
  const top = c.map((x, i) => Math.max(o[i], x)), bottom = c.map((x, i) => Math.min(o[i], x));
  const upper = hi.map((x, i) => x - top[i]), lower = lo.map((x, i) => bottom[i] - x);
  const avgBody = priorMean(body, AVG), avgRange = priorMean(rng, AVG);
  const green = (/** @type {number} */ i) => i >= 0 && c[i] > o[i], red = (/** @type {number} */ i) => i >= 0 && c[i] < o[i];
  const fell = (/** @type {number} */ i) => i - 1 - TREND_BACK >= 0 && c[i - 1] < c[i - 1 - TREND_BACK];
  const rose = (/** @type {number} */ i) => i - 1 - TREND_BACK >= 0 && c[i - 1] > c[i - 1 - TREND_BACK];
  const ab = (/** @type {number} */ i) => (i >= 0 && avgBody[i] != null ? /** @type {number} */ (avgBody[i]) : NaN);
  const ok = (/** @type {number} */ i) => i >= 0 && rng[i] > 0 && avgBody[i] != null && i - 1 - TREND_BACK >= 0;
  const smallLow = (/** @type {number} */ i) => lower[i] >= 2 * body[i] && upper[i] <= 0.15 * rng[i] && body[i] > 0.02 * rng[i];
  const smallHigh = (/** @type {number} */ i) => upper[i] >= 2 * body[i] && lower[i] <= 0.15 * rng[i] && body[i] > 0.02 * rng[i];
  const soldier = (/** @type {number} */ i) => i >= 1 && green(i) && upper[i] <= 0.5 * body[i] && body[i] >= 0.5 * ab(i) && c[i] > c[i - 1] && o[i] > o[i - 1] && o[i] < c[i - 1];
  const crow = (/** @type {number} */ i) => i >= 1 && red(i) && lower[i] <= 0.5 * body[i] && body[i] >= 0.5 * ab(i) && c[i] < c[i - 1] && o[i] < o[i - 1] && o[i] > c[i - 1];
  /** @type {Record<string, (i: number) => boolean>} */
  const rules = {
    doji: (i) => body[i] <= 0.1 * rng[i],
    hammer: (i) => smallLow(i) && fell(i),
    inverted_hammer: (i) => smallHigh(i) && fell(i),
    hanging_man: (i) => smallLow(i) && rose(i),
    shooting_star: (i) => smallHigh(i) && rose(i),
    bullish_marubozu: (i) => green(i) && body[i] >= 0.9 * rng[i] && body[i] >= 1.3 * ab(i),
    bearish_marubozu: (i) => red(i) && body[i] >= 0.9 * rng[i] && body[i] >= 1.3 * ab(i),
    bullish_engulfing: (i) => red(i - 1) && green(i) && o[i] <= c[i - 1] && c[i] >= o[i - 1] && body[i] > body[i - 1],
    bearish_engulfing: (i) => green(i - 1) && red(i) && o[i] >= c[i - 1] && c[i] <= o[i - 1] && body[i] > body[i - 1],
    bullish_harami: (i) => red(i - 1) && body[i - 1] >= ab(i - 1) && green(i) && top[i] <= o[i - 1] && bottom[i] >= c[i - 1] && body[i] <= 0.5 * body[i - 1],
    bearish_harami: (i) => green(i - 1) && body[i - 1] >= ab(i - 1) && red(i) && top[i] <= c[i - 1] && bottom[i] >= o[i - 1] && body[i] <= 0.5 * body[i - 1],
    piercing_line: (i) => red(i - 1) && body[i - 1] >= ab(i - 1) && green(i) && o[i] < c[i - 1] && c[i] > (o[i - 1] + c[i - 1]) / 2 && c[i] < o[i - 1],
    dark_cloud_cover: (i) => green(i - 1) && body[i - 1] >= ab(i - 1) && red(i) && o[i] > c[i - 1] && c[i] < (o[i - 1] + c[i - 1]) / 2 && c[i] > o[i - 1],
    tweezer_bottom: (i) => red(i - 1) && green(i) && Math.abs(lo[i] - lo[i - 1]) <= 0.05 * /** @type {number} */ (avgRange[i] ?? NaN) && fell(i - 1),
    tweezer_top: (i) => green(i - 1) && red(i) && Math.abs(hi[i] - hi[i - 1]) <= 0.05 * /** @type {number} */ (avgRange[i] ?? NaN) && rose(i - 1),
    morning_star: (i) => red(i - 2) && body[i - 2] >= ab(i - 2) && body[i - 1] <= 0.5 * ab(i - 2) && top[i - 1] <= c[i - 2] && green(i) && c[i] > (o[i - 2] + c[i - 2]) / 2,
    evening_star: (i) => green(i - 2) && body[i - 2] >= ab(i - 2) && body[i - 1] <= 0.5 * ab(i - 2) && bottom[i - 1] >= c[i - 2] && red(i) && c[i] < (o[i - 2] + c[i - 2]) / 2,
    three_white_soldiers: (i) => soldier(i) && soldier(i - 1) && green(i - 2) && upper[i - 2] <= 0.5 * body[i - 2] && body[i - 2] >= 0.5 * ab(i - 2),
    three_black_crows: (i) => crow(i) && crow(i - 1) && red(i - 2) && lower[i - 2] <= 0.5 * body[i - 2] && body[i - 2] >= 0.5 * ab(i - 2),
  };
  /** @type {string[][]} */ const out = [];
  for (let i = 0; i < n; i++) {
    /** @type {string[]} */ const here = [];
    if (ok(i)) for (const k of KEYS) { if (ok(i - PATTERNS[k][2] + 1) && rules[k](i)) here.push(k); }
    out.push(here);
  }
  return out;
}

/** The filters on each candle (see desk/candles.py). @param {Bar[]} bars @param {number} [near]
 * @returns {{ trend: string[], nearLow: boolean[], nearHigh: boolean[], highVolume: boolean[] }} */
export function context(bars, near = 1) {
  const n = bars.length, c = bars.map((b) => b.close);
  const fast = smaValues(c, FAST), slow = smaValues(c, SLOW), atr = atrValues(bars, ATR_BARS);
  const raw = c.map((x, i) => {
    const s = slow[i], f = fast[i];
    if (s == null) return "";
    if (f != null && x > s && f > s) return "up";
    if (f != null && x < s && f < s) return "down";
    return "side";
  });
  const trend = raw.map((_, i) => (i ? raw[i - 1] : ""));
  const nearLow = new Array(n).fill(false), nearHigh = new Array(n).fill(false), highVolume = new Array(n).fill(false);
  const vol = bars.map((b) => Number(b.volume) || 0), avgVol = priorMean(vol, VOLUME_BARS);
  for (let i = 0; i < n; i++) {
    const a = atr[i];
    if (i >= LEVEL_BARS + 2 && a != null) {
      let low3 = Infinity, high3 = -Infinity, refLow = Infinity, refHigh = -Infinity;
      for (let k = i - 2; k <= i; k++) { low3 = Math.min(low3, bars[k].low); high3 = Math.max(high3, bars[k].high); }
      for (let k = i - 2 - LEVEL_BARS; k < i - 2; k++) { refLow = Math.min(refLow, bars[k].low); refHigh = Math.max(refHigh, bars[k].high); }
      nearLow[i] = low3 - refLow <= near * a;
      nearHigh[i] = refHigh - high3 <= near * a;
    }
    const av = avgVol[i];
    highVolume[i] = av != null && av > 0 && vol[i] > av;
  }
  return { trend, nearLow, nearHigh, highVolume };
}

/** @param {ReturnType<typeof context>} ctx @param {Filters} f @param {number} i */
const passes = (ctx, f, i) => (f.trend === "any" || ctx.trend[i] === f.trend)
  && (f.place === "any" || (f.place === "low" ? ctx.nearLow[i] : ctx.nearHigh[i])) && (!f.volume || ctx.highVolume[i]);

const round = (/** @type {number} */ v, /** @type {number} */ dp) => Math.round(v * 10 ** dp) / 10 ** dp;

/** @param {number[]} moves @returns {Stats} */
export function stats(moves) {
  if (!moves.length) return { cases: 0, up_pct: null, avg_pct: null, median_pct: null, worst_pct: null, best_pct: null };
  const s = moves.slice().sort((a, b) => a - b), n = s.length;
  const mid = n % 2 ? s[(n - 1) / 2] : (s[n / 2 - 1] + s[n / 2]) / 2;
  return { cases: n, up_pct: round(moves.filter((m) => m > 0).length / n * 100, 1), avg_pct: round(moves.reduce((a, b) => a + b, 0) / n * 100, 2),
    median_pct: round(mid * 100, 2), worst_pct: round(s[0] * 100, 2), best_pct: round(s[n - 1] * 100, 2) };
}

/** Plain words for a pattern against the normal candle (desk/candles.py verdict).
 * @param {number} cases @param {number|null} up @param {number|null} normal @param {number} [minCases] @returns {Verdict} */
export function verdict(cases, up, normal, minCases = MIN_CASES) {
  if (cases < minCases || up == null || normal == null) return { key: "few", text: "Too few cases", z: null };
  const p0 = Math.min(Math.max(normal / 100, 0.01), 0.99);
  const z = (up / 100 - p0) / Math.sqrt(p0 * (1 - p0) / cases), zr = round(z, 2);
  if (z >= 3) return { key: "up", text: "Up more often than normal", z: zr };
  if (z <= -3) return { key: "down", text: "Down more often than normal", z: zr };
  if (z >= 2) return { key: "maybe_up", text: "A little more up, could be luck", z: zr };
  if (z <= -2) return { key: "maybe_down", text: "A little more down, could be luck", z: zr };
  return { key: "none", text: "No clear edge", z: zr };
}

/** What followed each pattern on these candles. Only the first `closed` count (the rest are still forming).
 * @param {Bar[]} all @param {{ closed?: number, filters?: any, horizons?: number[], minCases?: number, found?: string[][] }} [opts] */
export function odds(all, opts = {}) {
  const f = cleanFilters(opts.filters), horizons = opts.horizons || HORIZONS, minCases = opts.minCases || MIN_CASES;
  const bars = opts.closed == null ? all : all.slice(0, opts.closed);
  const found = (opts.found || detect(all)).slice(0, bars.length), ctx = context(bars, f.near);
  const n = bars.length, mask = bars.map((_, i) => passes(ctx, f, i));
  /** @type {Record<string, Stats>} */ const normal = {};
  /** @type {Record<string, any>} */ const rows = {};
  KEYS.forEach((k) => { rows[k] = { key: k, name: PATTERNS[k][0], signal: PATTERNS[k][1], seen: found.filter((ks, i) => mask[i] && ks.includes(k)).length, after: {} }; });
  for (const hz of horizons) {
    /** @type {number[]} */ const base = [];
    /** @type {Record<string, number[]>} */ const per = Object.fromEntries(KEYS.map((k) => [k, []]));
    for (let i = 0; i + hz < n; i++) {
      if (!mask[i]) continue;
      const m = bars[i + hz].close / bars[i].close - 1;
      base.push(m);
      found[i].forEach((k) => per[k].push(m));
    }
    normal[hz] = stats(base);
    KEYS.forEach((k) => { const s = stats(per[k]); rows[k].after[hz] = { ...s, verdict: verdict(s.cases, s.up_pct, normal[hz].up_pct, minCases) }; });
  }
  return { patterns: KEYS.map((k) => rows[k]), normal, filters: f, bars: n, tested: KEYS.length * horizons.length };
}

/** numpy's default quantile (linear between closest ranks) of sorted values. @param {number[]} s @param {number} q */
export function quantile(s, q) {
  const pos = (s.length - 1) * q, lo = Math.floor(pos), hi = Math.min(lo + 1, s.length - 1);
  return s[lo] + (s[hi] - s[lo]) * (pos - lo);
}

/** Where the price ended up 50% and 80% of the time over the next 1..horizon candles, from this stock's own
 * moves scaled to today's volatility (desk/candles.py cone). Null with too little history.
 * @param {Bar[]} all @param {number} horizon @param {{ closed?: number }} [opts] */
export function cone(all, horizon, opts = {}) {
  const close = (opts.closed == null ? all : all.slice(0, opts.closed)).map((b) => b.close), n = close.length;
  if (n < VOL_BARS + horizon + 60) return null;
  const logc = close.map(Math.log), r1 = logc.slice(1).map((v, i) => v - logc[i]);
  const sig = new Array(n).fill(NaN);
  for (let i = VOL_BARS; i < n; i++) {
    let mean = 0;
    for (let k = i - VOL_BARS; k < i; k++) mean += r1[k];
    mean /= VOL_BARS;
    let sq = 0;
    for (let k = i - VOL_BARS; k < i; k++) sq += (r1[k] - mean) ** 2;
    sig[i] = Math.sqrt(sq / (VOL_BARS - 1));
  }
  const now = sig[n - 1];
  if (!(now > 0)) return null;
  const zs = (/** @type {number} */ h, /** @type {number} */ upto) => {
    /** @type {number[]} */ const z = [];
    for (let i = VOL_BARS; i < upto - h; i++) if (sig[i] > 0) z.push((logc[i + h] - logc[i]) / (sig[i] * Math.sqrt(h)));
    return z.sort((a, b) => a - b);
  };
  const steps = [];
  for (let h = 1; h <= horizon; h++) {
    const z = zs(h, n), scale = now * Math.sqrt(h), last = close[n - 1];
    const at = (/** @type {number} */ q) => last * Math.exp(quantile(z, q) * scale);
    steps.push({ ahead: h, q10: at(0.1), q25: at(0.25), q50: at(0.5), q75: at(0.75), q90: at(0.9) });
  }
  const split = VOL_BARS + Math.floor((n - VOL_BARS) * 2 / 3);
  const train = zs(horizon, split);
  let held = null, checks = 0, inside = 0;
  if (train.length >= 30) {
    const lo = quantile(train, 0.1), hi = quantile(train, 0.9);
    for (let i = split; i < n - horizon; i++) {
      if (!(sig[i] > 0)) continue;
      const z = (logc[i + horizon] - logc[i]) / (sig[i] * Math.sqrt(horizon));
      checks++; if (z >= lo && z <= hi) inside++;
    }
    if (checks >= 20) held = { pct: round(inside / checks * 100, 1), checks };
  }
  return { from: close[n - 1], steps, held, samples: zs(horizon, n).length };
}
