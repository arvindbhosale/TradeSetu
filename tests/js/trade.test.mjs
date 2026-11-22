// Unit tests for trading from the chart (trade.js). Run: node --test "tests/js/*.test.mjs"
import assert from "node:assert/strict";
import { test } from "node:test";

import { defaultLevels, nearest, outcome, roundPrice } from "../../desk/static/chartdesk/trade.js";

test("a dragged rupee price lands on the 0.05 tick, a coin on its decimals", () => {
  assert.equal(roundPrice("india", 1234.5678, 2), 1234.55);
  assert.equal(roundPrice("india", 99.976, 2), 100);
  assert.equal(roundPrice("crypto", 0.123456789, 6), 0.123457);
  assert.equal(roundPrice("crypto", 65432.129, 2), 65432.13);
  assert.equal(roundPrice("india", -3, 2), 0);
});

test("profit and loss of a buy at its stoploss and target", () => {
  assert.equal(outcome(10, 1000, 980), -200);
  assert.equal(outcome(10, 1000, 1050), 500);
});

test("the starting lines sit 1% under and 2% over the price", () => {
  const { stop, target } = defaultLevels(1000);
  assert.ok(Math.abs(stop - 990) < 1e-9 && Math.abs(target - 1020) < 1e-9);
});

test("a press picks the nearest line within reach and ignores lines off the chart", () => {
  assert.equal(nearest([{ y: 100 }, { y: 108 }, { y: null }], 105), 1);
  assert.equal(nearest([{ y: 100 }, { y: 200 }], 150), -1);
  assert.equal(nearest([{ y: null }], 0), -1);
});
