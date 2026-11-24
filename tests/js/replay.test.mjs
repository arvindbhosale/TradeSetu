// Unit tests for the replay's helpers (replay.js). Run: node --test "tests/js/*.test.mjs"
import assert from "node:assert/strict";
import { test } from "node:test";

import { dayNumbers, startText, whenText } from "../../desk/static/chartdesk/replay.js";

const DAY = 86400;
const bar = (t) => ({ time: t, open: 1, high: 1, low: 1, close: 1, volume: 0 });

test("start text for the desk: a date for day candles, date and time otherwise", () => {
  const t = Date.UTC(2021, 2, 12, 10, 15) / 1000;            // bar times are India wall clock read as UTC
  assert.equal(startText(t, false), "2021-03-12T10:15");
  assert.equal(startText(t, true), "2021-03-12");
  assert.equal(whenText(t, false), "12 Mar 2021, 10:15");
});

test("blind days count from the replay's start, also after the candle size changes", () => {
  const d0 = 18000;
  const bars = [bar(d0 * DAY + 50000), bar((d0 + 1) * DAY + 34000), bar((d0 + 3) * DAY + 34000), bar((d0 + 4) * DAY + 34000)];
  const days = dayNumbers(bars, 2);
  assert.equal(days.get(d0 + 3), 1); assert.equal(days.get(d0 + 4), 2); assert.equal(days.get(d0 + 1), 0); assert.equal(days.get(d0), -1);
  const kept = dayNumbers(bars.slice(1), 3, (d0 + 3) * DAY + 33300);
  assert.equal(kept.get(d0 + 3), 1); assert.equal(kept.get(d0 + 1), 0);
});
