// Every chart indicator and drawing helper. Run: node --test "tests/js/*.test.mjs"
import assert from "node:assert/strict";
import { test } from "node:test";

import { CATALOG, GROUPS, cleanParams, compute, defaultsOf, pivotLevels, pivotValues, search } from "../../desk/static/chartdesk/indicators.js";
import { TOOLS, distanceToLine, newPosition, normalise, positionPlan } from "../../desk/static/chartdesk/drawings.js";

// A seeded random walk with real-looking candles: gaps between candles, wicks past the body, varying volume.
function walk(n, seed = 7, step = 300, start = 1_763_000_000 - (1_763_000_000 % 86400) + 33300) {
  let s = seed;
  const rnd = () => { s = (s * 1103515245 + 12345) % 2147483648; return s / 2147483648; };
  const bars = [];
  let c = 100;
  for (let i = 0; i < n; i++) {
    const o = c * (1 + (rnd() - 0.5) * 0.004);
    c = o * Math.exp((rnd() - 0.5) * 0.03);
    const hi = Math.max(o, c) * (1 + rnd() * 0.006), lo = Math.min(o, c) * (1 - rnd() * 0.006);
    bars.push({ time: start + i * step, open: o, high: hi, low: lo, close: c, volume: Math.round(100 + rnd() * 900) });
  }
  return bars;
}
const ctx = { seconds: 300, sessionOffset: 0, market: "india" };

test("there are about eighty indicators, each fully described", () => {
  const kinds = Object.keys(CATALOG);
  assert.ok(kinds.length >= 80, `only ${kinds.length}`);
  for (const [kind, c] of Object.entries(CATALOG)) {
    assert.ok(c.name && c.short && c.about, kind);
    assert.ok(c.group in GROUPS, `${kind} group`);
    assert.ok(c.pane === "price" || c.pane === "own", `${kind} pane`);
    assert.ok(c.outputs.length >= 1, `${kind} outputs`);
    assert.equal(new Set(c.outputs.map((o) => o.key)).size, c.outputs.length, `${kind} output keys`);
    for (const [k, p] of Object.entries(c.params)) {
      if (p.options) assert.ok(p.value in p.options, `${kind}.${k}`);
      else assert.ok(p.min <= p.value && p.value <= p.max, `${kind}.${k} default in range`);
    }
    if (c.fill) c.fill.forEach((k) => assert.ok(c.outputs.some((o) => o.key === k), `${kind} fill ${k}`));
  }
});

test("every indicator runs on real-looking, empty and one-candle data", () => {
  const short = walk(400), long = walk(1500);
  for (const kind of Object.keys(CATALOG)) {
    const bars = kind === "pivots" ? long : short;      // pivots need a whole earlier day
    const r = compute(kind, bars, defaultsOf(kind), ctx);
    for (const o of CATALOG[kind].outputs) {
      const pts = r[o.key];
      assert.ok(Array.isArray(pts), `${kind}.${o.key}`);
      assert.ok(pts.every((p) => Number.isFinite(p.value)), `${kind}.${o.key} finite`);
      for (let i = 1; i < pts.length; i++) assert.ok(pts[i].time > pts[i - 1].time, `${kind}.${o.key} times rise`);
    }
    assert.ok(Object.values(r).some((pts) => pts.length > 0), `${kind} drew nothing on 400 candles`);
    assert.doesNotThrow(() => compute(kind, [], defaultsOf(kind), ctx), kind);
    assert.doesNotThrow(() => compute(kind, bars.slice(0, 1), defaultsOf(kind), ctx), kind);
  }
});

test("indicators that do not repaint use past candles only", () => {
  // The chart updates only the newest point on a live tick; that is right only if no earlier value moves.
  const bars = walk(360, 11);
  for (const [kind, c] of Object.entries(CATALOG)) {
    if (c.repaints) continue;
    const full = compute(kind, bars, defaultsOf(kind), ctx);
    const part = compute(kind, bars.slice(0, 300), defaultsOf(kind), ctx), cut = bars[299].time;
    for (const o of c.outputs) {
      if (o.back) continue;
      const at = new Map(full[o.key].map((p) => [p.time, p.value]));
      part[o.key].filter((p) => p.time <= cut).forEach((p) => {
        assert.ok(at.has(p.time), `${kind}.${o.key} lost a point`);
        assert.ok(Math.abs(at.get(p.time) - p.value) <= 1e-9 * Math.max(1, Math.abs(p.value)), `${kind}.${o.key} changed at ${p.time}`);
      });
    }
  }
});

test("lines drawn ahead of price get times after the last candle", () => {
  const bars = walk(200);
  const r = compute("ichimoku", bars, defaultsOf("ichimoku"), ctx), last = bars[bars.length - 1].time;
  assert.equal(r.spanA[r.spanA.length - 1].time, last + 25 * 300);
  assert.equal(r.lagging[r.lagging.length - 1].time, bars[bars.length - 26].time);
  const a = compute("alligator", bars, defaultsOf("alligator"), ctx);
  assert.equal(a.jaw[a.jaw.length - 1].time, last + 8 * 300);
});

test("settings are checked: unknown dropped, numbers kept in range, options from the list", () => {
  assert.deepEqual(cleanParams("sma", { period: 9999, source: "nonsense", extra: 1 }), { period: 500, source: "close" });
  assert.deepEqual(cleanParams("sma", { period: "12.6", source: "hl2" }), { period: 13, source: "hl2" });
  assert.equal(cleanParams("bollinger", { width: 2.25 }).width, 2.25);
  assert.deepEqual(cleanParams("rsi", null), { period: 14 });
});

test("search finds indicators by name, short name and group", () => {
  assert.ok(search("rsi").includes("rsi") && search("rsi").includes("stochrsi"));
  assert.ok(search("moving average").includes("sma"));
  assert.ok(search("VOLUME").includes("obv"));
  assert.deepEqual(search("no such thing"), []);
  assert.equal(search("").length, Object.keys(CATALOG).length);
});

test("known values: WMA, Williams %R, OBV, ROC, CMO", () => {
  const bar = (time, close, volume = 1) => ({ time, open: close, high: close, low: close, close, volume });
  const bars = [1, 2, 3, 4, 5].map((c, i) => bar(i, c));
  assert.deepEqual(compute("wma", bars, { period: 3 }).line.map((p) => p.value), [14 / 6, 20 / 6, 26 / 6]);
  assert.deepEqual(compute("roc", bars, { period: 2 }).line.map((p) => +p.value.toFixed(6)), [200, 100, 66.666667]);
  assert.deepEqual(compute("obv", [bar(0, 1, 5), bar(1, 2, 3), bar(2, 1, 2), bar(3, 1, 9)], {}).line.map((p) => p.value), [0, 3, 1, 1]);
  const wr = compute("willr", [{ time: 0, open: 5, high: 10, low: 0, close: 5, volume: 1 }, { time: 1, open: 5, high: 10, low: 0, close: 10, volume: 1 }], { period: 2 }).line;
  assert.deepEqual(wr.map((p) => p.value), [0]);
  assert.deepEqual(compute("cmo", [1, 2, 1, 3].map((c, i) => bar(i, c)), { period: 3 }).line.map((p) => p.value), [100 * (3 - 1) / 4]);
});

test("pivot kinds follow their formulas", () => {
  const [r3, r2, r1, p, s1, s2, s3] = pivotLevels("traditional", 110, 90, 100);
  assert.equal(p, 100); assert.equal(r1, 110); assert.equal(s1, 90); assert.equal(r2, 120); assert.equal(s2, 80); assert.equal(r3, 130); assert.equal(s3, 70);
  assert.deepEqual(pivotLevels("fibonacci", 110, 90, 100).map((v) => +v.toFixed(2)), [120, 112.36, 107.64, 100, 92.36, 87.64, 80]);
  assert.equal(pivotLevels("woodie", 110, 90, 104)[3], 102);
  assert.equal(+pivotLevels("camarilla", 110, 90, 100)[2].toFixed(4), +(100 + 22 / 12).toFixed(4));
  // daily pivots on 5-minute candles: the first day sets the second day's levels
  const day = 86400, bars = [];
  for (let d = 0; d < 2; d++) for (let i = 0; i < 3; i++) bars.push({ time: 4 * day + d * day + 33300 + i * 300, open: 100, high: 100 + i + d * 10, low: 90 - i, close: 95 + i, volume: 1 });
  const v = pivotValues(bars, "D", "traditional");
  assert.equal(v.p[0], null);
  assert.equal(v.p[3], (102 + 88 + 97) / 3);
});

test("a position keeps entry, target and stoploss the right way round and works out the money", () => {
  const long = { id: "a", kind: "long", points: newPosition("long", { time: 100, price: 200 }, 60), qty: 10 };
  assert.deepEqual(long.points.map((p) => +p.price.toFixed(6)), [200, 204, 198]);
  assert.equal(long.points[1].time, 100 + 20 * 60);
  const plan = positionPlan(long, 201);
  assert.equal(plan.side, "BUY"); assert.equal(plan.quantity, 10);
  assert.ok(Math.abs(plan.reward - 40) < 1e-9 && Math.abs(plan.risk - 20) < 1e-9 && Math.abs(plan.riskReward - 2) < 1e-9);
  assert.ok(Math.abs(plan.now - 10) < 1e-9);
  const short = { id: "b", kind: "short", points: newPosition("short", { time: 0, price: 100 }, 60) };
  const sp = positionPlan(short, 99);
  assert.equal(sp.side, "SELL"); assert.ok(sp.target < sp.entry && sp.stoploss > sp.entry); assert.ok(sp.now > 0);
  // a target dragged under a long's entry is put back above it
  const bad = normalise({ ...long, points: [{ time: 100, price: 200 }, { time: 50, price: 150 }, { time: 300, price: 250 }] });
  assert.ok(bad.points[1].price > 200 && bad.points[2].price < 200);
  assert.equal(bad.points[2].time, 100); assert.ok(bad.points[1].time > 100);
});

test("every drawing tool says how many taps and points it takes", () => {
  for (const [k, t] of Object.entries(TOOLS)) { assert.ok(t.clicks >= 1 && t.points >= t.clicks, k); assert.ok(t.label && t.hint, k); }
  assert.equal(distanceToLine({ x: 5, y: 5 }, { x: 0, y: 0 }, { x: 10, y: 0 }), 5);
  assert.equal(distanceToLine({ x: 20, y: 0 }, { x: 0, y: 0 }, { x: 10, y: 0 }), 10);
  assert.equal(distanceToLine({ x: 20, y: 0 }, { x: 0, y: 0 }, { x: 10, y: 0 }, [false, true]), 0);
});

test("chart helpers: classic answers, Heikin Ashi, padding, saved indicators", async () => {
  globalThis.window = globalThis.window || {};
  const cd = await import("../../desk/static/chartdesk/chartdesk.js");
  const info = cd.classicInfo({ candles: [{ time: "2026-11-20", open: 1, high: 2, low: 1, close: 2 }, { time: "2026-11-21", open: 2, high: 3, low: 2, close: 2.5 }],
    panels: [{ key: "volume", points: [{ time: "2026-11-21", value: 70 }] }], live_price: 3.5, session: { open: true }, source: "Kite", symbol: "TCS", note: null, truncated: true }, "1d");
  assert.equal(info.candles[0].time, Date.UTC(2026, 10, 20) / 1000);
  assert.equal(info.candles[1].volume, 70); assert.equal(info.candles[0].volume, 0);
  assert.equal(info.candles[1].close, 3.5); assert.equal(info.candles[1].high, 3.5);
  assert.equal(info.seconds, 86400); assert.equal(info.live.kind, "refetch");
  assert.deepEqual(info.notes, ["Showing the most recent 1,500 candles."]);
  assert.equal(cd.secondsOf("3m"), 180); assert.equal(cd.secondsOf("1w"), 604800);
  const ha = cd.heikinAshi([{ time: 0, open: 10, high: 12, low: 9, close: 11, volume: 1 }, { time: 1, open: 11, high: 13, low: 10, close: 12, volume: 1 }]);
  assert.equal(ha[0].open, 10.5); assert.equal(ha[0].close, 10.5); assert.equal(ha[1].open, 10.5); assert.equal(ha[1].close, 11.5);
  assert.deepEqual(cd.padded([{ time: 1 }, { time: 2 }], [{ time: 2, value: 5 }, { time: 9, value: 6 }]), [{ time: 1 }, { time: 2, value: 5 }, { time: 9, value: 6 }]);
  assert.equal(cd.cleanActive({ kind: "nope" }), null);
  assert.deepEqual(cd.cleanActive({ kind: "ema", params: { period: 3 }, colors: { line: "#AABBCC", bad: "#000000", x: "red" }, width: 9, hidden: 1 }),
    { kind: "ema", params: { period: 3, source: "close" }, colors: { line: "#AABBCC" }, width: null, hidden: true });
  assert.equal(cd.valueText(1234567), "1,234,567"); assert.equal(cd.valueText(0.12345), "0.1235"); assert.equal(cd.valueText(null), "–");
});
