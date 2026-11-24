// Unit tests for candle patterns (patterns.js) and the panel's helpers (patternpanel.js).
// Run: node --test "tests/js/*.test.mjs". The desk's own copy is compared with these in tests/test_desk_patterns.py.
import assert from "node:assert/strict";
import { test } from "node:test";

import { KEYS, cone, detect, odds, quantile, verdict } from "../../desk/static/chartdesk/patterns.js";
import { cleanSettings, closedCount } from "../../desk/static/chartdesk/patternpanel.js";

const DAY = 86400;
/** Twelve quiet candles drifting down, then the given (open, high, low, close) rows. */
function scripted(rows) {
  const base = Array.from({ length: 12 }, (_, i) => [110 - i, 110.6 - i, 109 - i, 109.4 - i]);
  return [...base, ...rows].map(([open, high, low, close], i) => ({ time: (20000 + i) * DAY, open, high, low, close, volume: 1000 }));
}
const lastFound = (bars) => detect(bars).at(-1);

test("textbook shapes are found on the candle they close on", () => {
  assert.ok(lastFound(scripted([[98, 98.3, 95, 98.2]])).includes("hammer"));
  assert.ok(!lastFound(scripted([[98, 98.3, 95, 98.2]])).includes("hanging_man"));
  assert.ok(lastFound(scripted([[98.4, 98.6, 97, 97.2], [97, 99.4, 96.9, 99]])).includes("bullish_engulfing"));
  const star = scripted([[98.4, 98.5, 95.4, 95.6], [95.4, 95.7, 94.9, 95.3], [95.5, 97.6, 95.4, 97.5]]);
  assert.ok(lastFound(star).includes("morning_star"));
  assert.deepEqual(detect(star.slice(0, -1)), detect(star).slice(0, -1));       // nothing earlier changes when a candle is added
});

test("few cases and luck are called what they are", () => {
  assert.equal(verdict(10, 90, 50).key, "few");
  assert.equal(verdict(100, 66, 50).key, "up");
  assert.equal(verdict(100, 61, 50).key, "maybe_up");
  assert.equal(verdict(100, 50, 50).key, "none");
});

test("odds leave out candles that have not closed, and the range widens with time", () => {
  let x = 100;
  const bars = Array.from({ length: 400 }, (_, i) => {
    const open = x; x *= Math.exp(Math.sin(i * 1.7) * 0.012); const close = x;
    return { time: (20000 + i) * DAY, open, high: Math.max(open, close) * 1.004, low: Math.min(open, close) * 0.995, close, volume: 100 + (i % 7) };
  });
  assert.deepEqual(odds(bars, { closed: 399 }), odds(bars.slice(0, 399)));
  assert.equal(odds(bars).patterns.length, KEYS.length);
  const c = cone(bars, 10);
  assert.ok(c && c.steps.length === 10);
  const w = c.steps.map((s) => s.q90 - s.q10);
  assert.ok(w[9] > w[0]);
  assert.equal(cone(bars.slice(0, 50), 10), null);
  assert.equal(quantile([1, 2, 3, 4], 0.5), 2.5);
});

test("panel settings fall back to defaults and the forming candle is not counted", () => {
  const s = cleanSettings({ on: true, ahead: 7, horizon: 20, trend: "up", hide: ["doji", "nope"], minCases: 2 });
  assert.equal(s.on, true); assert.equal(s.ahead, 5); assert.equal(s.horizon, 20); assert.equal(s.trend, "up");
  assert.deepEqual(s.hide, ["doji"]); assert.equal(s.minCases, 30);
  assert.deepEqual(cleanSettings(null).hide, ["doji"]);
  assert.equal(closedCount(100, { replaying: false, live: true }), 99);
  assert.equal(closedCount(100, { replaying: true, live: true }), 100);
  assert.equal(closedCount(100, { replaying: false, live: false }), 100);
});
