// Unit tests for the Charts tab's browser code. Run: node --test "tests/js/*.test.mjs"
import assert from "node:assert/strict";
import { test } from "node:test";

import { bollinger, ema, macd, rsi, sma, smaValues, vwap } from "../../desk/static/chartdesk/indicators.js";
import { mergeBar, mergeTrade, startFeed } from "../../desk/static/chartdesk/feeds.js";

const bar = (time, close, volume = 1) => ({ time, open: close, high: close, low: close, close, volume });
const near = (a, b, eps = 1e-9) => assert.ok(Math.abs(a - b) < eps, `${a} != ${b}`);

test("SMA waits for a full window", () => {
  assert.deepEqual(smaValues([1, 2, 3, 4, 5], 3), [null, null, 2, 3, 4]);
  const out = sma([1, 2, 3, 4].map((c, i) => bar(i, c)), { period: 2 }).line;
  assert.deepEqual(out.map((p) => p.value), [1.5, 2.5, 3.5]);
  assert.equal(out[0].time, 1);
});

test("EMA seeds with the first close and shows from the period-th", () => {
  const bars = [10, 11, 12, 13].map((c, i) => bar(i, c));
  const out = ema(bars, { period: 3 }).line;    // alpha 0.5: 10, 10.5, 11.25, 12.125
  assert.deepEqual(out.map((p) => p.value), [11.25, 12.125]);
});

test("RSI is 100 with no losses and follows Wilder's smoothing", () => {
  const rising = [1, 2, 3, 4, 5, 6].map((c, i) => bar(i, c));
  assert.ok(rsi(rising, { period: 3 }).line.every((p) => p.value === 100));
  const mixed = [10, 11, 10, 12, 11].map((c, i) => bar(i, c));
  // deltas +1 -1 +2 -1; gains 1 0 2 0, losses 0 1 0 1; Wilder alpha 1/2 from the first delta
  // gain: 1, .5, 1.25, .625  loss: 0, .5, .25, .625 -> shown from the 2nd delta (period 2)
  const out = rsi(mixed, { period: 2 }).line.map((p) => p.value);
  near(out[0], 50); near(out[1], 100 - 100 / (1 + 1.25 / 0.25)); near(out[2], 50);
});

test("Bollinger uses the population standard deviation", () => {
  const bars = [1, 2, 3].map((c, i) => bar(i, c));
  const r = bollinger(bars, { period: 3, width: 2 });
  near(r.middle[0].value, 2); near(r.upper[0].value, 2 + 2 * Math.sqrt(2 / 3)); near(r.lower[0].value, 2 - 2 * Math.sqrt(2 / 3));
});

test("MACD line, signal and histogram line up", () => {
  const bars = Array.from({ length: 60 }, (_, i) => bar(i, 100 + Math.sin(i / 5) * 5));
  const r = macd(bars, { fast: 12, slow: 26, signal: 9 });
  assert.equal(r.macd[0].time, 25);
  assert.equal(r.signal[0].time, 33);
  const last = r.histogram[r.histogram.length - 1];
  near(last.value, r.macd[r.macd.length - 1].value - r.signal[r.signal.length - 1].value);
});

test("VWAP restarts when the trading day starts", () => {
  const day = 86400, offset = 19800;      // crypto: the day starts 05:30 on the chart clock
  const bars = [
    { time: offset + day - 60, open: 10, high: 10, low: 10, close: 10, volume: 1 },
    { time: offset + day, open: 20, high: 20, low: 20, close: 20, volume: 1 },
    { time: offset + day + 60, open: 40, high: 40, low: 40, close: 40, volume: 3 },
  ];
  const out = vwap(bars, { sessionOffset: offset }).line.map((p) => p.value);
  assert.deepEqual(out, [10, 20, 35]);
  assert.equal(vwap([bar(0, 5, 0)], {}).line.length, 0, "no volume, no VWAP");
});

test("a trade updates the forming bar or opens the next one", () => {
  const bars = [{ time: 0, open: 10, high: 10, low: 10, close: 10, volume: 1 }];
  assert.equal(mergeTrade(bars, 30, 12, 2, 60, 0), bars[0]);
  assert.deepEqual(bars[0], { time: 0, open: 10, high: 12, low: 10, close: 12, volume: 3 });
  mergeTrade(bars, 61, 9, 1, 60, 0);
  assert.equal(bars.length, 2); assert.equal(bars[1].time, 60); assert.equal(bars[1].open, 9);
  assert.equal(mergeTrade(bars, 10, 50, 1, 60, 0), null, "late trades are ignored");
  assert.equal(mergeTrade(bars, 200, 0, 1, 60, 0), null, "a zero price is never drawn");
  // 4-hour candles anchored at 09:15 (NSE)
  const nse = []; mergeTrade(nse, 33300 + 4 * 3600 + 5, 100, 0, 14400, 33300);
  assert.equal(nse[0].time, 33300 + 4 * 3600);
});

test("a streamed bar replaces the same-time bar and never an older one", () => {
  const bars = [bar(0, 1), bar(60, 2)];
  assert.equal(mergeBar(bars, bar(60, 3)), true); assert.equal(bars[1].close, 3);
  assert.equal(mergeBar(bars, bar(0, 9)), false);
  assert.equal(mergeBar(bars, bar(120, 4)), true); assert.equal(bars.length, 3);
});

test("a stream that cannot connect falls back to polling and reports it", async () => {
  class Dead { constructor() { setTimeout(() => this.onclose && this.onclose(), 0); } close() {} send() {} }
  const statuses = [];
  const stop = startFeed({ live: { kind: "binance", url: "wss://x" }, seconds: 60, anchor: 0 },
    { bars: [], onChange() {}, onBars() {}, onStatus: (s) => statuses.push(s.state), refetch: async () => ({ candles: [] }), quote: async () => ({}), WebSocketImpl: Dead });
  await new Promise((r) => setTimeout(r, 10));
  stop();
  assert.deepEqual(statuses, ["connecting", "poll"]);
});

test("binance kline messages update the chart", async () => {
  let sock;
  class Fake { constructor() { sock = this; setTimeout(() => this.onopen(), 0); } close() {} send() {} }
  const bars = [bar(19800, 1)], changes = [];
  const stop = startFeed({ live: { kind: "binance", url: "wss://x" }, seconds: 60, anchor: 0 },
    { bars, onChange: (b) => changes.push(b), onBars() {}, onStatus() {}, refetch: async () => ({ candles: [] }), quote: async () => ({}), WebSocketImpl: Fake });
  await new Promise((r) => setTimeout(r, 5));
  sock.onmessage({ data: JSON.stringify({ k: { t: 60000, o: "1", h: "3", l: "1", c: "2", v: "5" } }) });
  sock.onmessage({ data: JSON.stringify({ k: { t: 60000, o: "0", h: "3", l: "1", c: "2", v: "5" } }) });   // bad open: dropped
  stop();
  assert.equal(bars.length, 2); assert.equal(bars[1].time, 60 + 19800); assert.equal(changes.length, 1);
});
