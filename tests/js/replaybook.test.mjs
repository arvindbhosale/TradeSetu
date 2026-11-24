// Unit tests for the replay's practice book (replaybook.js). Run: node --test "tests/js/*.test.mjs"
import assert from "node:assert/strict";
import { test } from "node:test";

import { advance, cancel, exitAtNextOpen, finish, newBook, place, setLevels, totals } from "../../desk/static/chartdesk/replaybook.js";

const DAY = 86400;
const at = (day, h, m) => day * DAY + h * 3600 + m * 60;
const bar = (time, open, high, low, close) => ({ time, open, high, low, close, volume: 1 });
const rules = { squareOff: true };
const delivery = { squareOff: false };

test("an order at next open fills at the next candle's open, not at the price on the chart", () => {
  const b = newBook(), last = bar(at(1, 10, 0), 100, 101, 99, 100);
  assert.equal(place(b, { side: "long", type: "market", qty: 10, stop: 95, target: 110, product: "intraday" }, last, { shortOk: true }), "");
  advance(b, bar(at(1, 10, 5), 102, 103, 101, 102.5), rules);
  assert.equal(b.position.entry, 102);
  assert.match(place(b, { side: "long", type: "market", qty: 1, product: "intraday" }, last, { shortOk: true }), /one trade at a time/);
});

test("stoploss and target: a gap fills at the open, both in one candle counts the stoploss", () => {
  const b = newBook(), last = bar(at(1, 10, 0), 100, 101, 99, 100);
  place(b, { side: "long", type: "market", qty: 10, stop: 98, target: 105, product: "delivery" }, last, { shortOk: true });
  advance(b, bar(at(1, 10, 5), 100, 100.5, 99.5, 100), delivery);
  let ev = advance(b, bar(at(2, 9, 15), 96, 97, 95, 96.5), delivery);       // gaps under the stoploss
  assert.equal(ev.at(-1).trade.exit, 96); assert.equal(ev.at(-1).trade.reason, "Stoploss"); assert.equal(ev.at(-1).trade.gross, -40);
  place(b, { side: "long", type: "market", qty: 10, stop: 94, target: 100, product: "delivery" }, bar(at(2, 9, 15), 96, 97, 95, 96.5), { shortOk: true });
  advance(b, bar(at(2, 9, 20), 97, 97, 96, 96.5), delivery);
  ev = advance(b, bar(at(2, 9, 25), 97, 101, 93, 98), delivery);           // touches both
  assert.equal(ev.at(-1).trade.reason, "Stoploss"); assert.equal(ev.at(-1).trade.exit, 94);
});

test("a short sale gains when the price falls and hits its target", () => {
  const b = newBook(), last = bar(at(1, 10, 0), 100, 101, 99, 100);
  assert.match(place(b, { side: "short", type: "market", qty: 5, stop: 99, target: 95, product: "intraday" }, last, { shortOk: true }), /stoploss must be above/);
  assert.match(place(b, { side: "short", type: "market", qty: 5, product: "delivery" }, last, { shortOk: false }), /Short selling/);
  place(b, { side: "short", type: "market", qty: 5, stop: 103, target: 95, product: "intraday" }, last, { shortOk: true });
  advance(b, bar(at(1, 10, 5), 100, 100, 98, 98), rules);
  const ev = advance(b, bar(at(1, 10, 10), 97, 97.5, 94, 95), rules);
  assert.equal(ev.at(-1).trade.reason, "Target"); assert.equal(ev.at(-1).trade.gross, 25);
});

test("a limit buy fills at its price inside a candle (only the stoploss checked there) or at a lower open", () => {
  const b = newBook(), last = bar(at(1, 10, 0), 100, 101, 99, 100);
  place(b, { side: "long", type: "limit", price: 98, qty: 1, stop: 96, target: 99, product: "delivery" }, last, { shortOk: true });
  advance(b, bar(at(1, 10, 5), 99.5, 100, 98.5, 99), delivery);
  assert.ok(b.pending && !b.position);
  advance(b, bar(at(1, 10, 10), 99, 99.5, 97.5, 99.2), delivery);     // dips to 98 then rises past 99: target not counted on this candle
  assert.equal(b.position.entry, 98); assert.equal(b.trades.length, 0);
  advance(b, bar(at(1, 10, 15), 99.1, 99.5, 98.8, 99.3), delivery);   // opens past the target: fills at the open
  assert.equal(b.trades[0].exit, 99.1);
  const c = newBook();
  place(c, { side: "long", type: "limit", price: 98, qty: 1, product: "delivery" }, last, { shortOk: true });
  advance(c, bar(at(2, 9, 15), 97, 97.5, 96.5, 97.2), delivery);
  assert.equal(c.position.entry, 97);
});

test("intraday trades are squared off at 3:20 PM, or at the day's last close when no 3:20 PM candle comes", () => {
  const b = newBook();
  place(b, { side: "long", type: "market", qty: 2, product: "intraday" }, bar(at(1, 15, 10), 100, 100, 100, 100), { shortOk: true });
  advance(b, bar(at(1, 15, 15), 101, 101, 101, 101), rules);
  const ev = advance(b, bar(at(1, 15, 20), 103, 104, 102, 103), rules);
  assert.equal(ev.at(-1).trade.reason, "Square-off"); assert.equal(ev.at(-1).trade.exit, 103);
  const h = newBook();
  place(h, { side: "long", type: "market", qty: 1, product: "intraday" }, bar(at(1, 13, 15), 100, 100, 100, 100), { shortOk: true });
  advance(h, bar(at(1, 14, 15), 101, 102, 100.5, 101.5), rules);
  advance(h, bar(at(2, 9, 15), 110, 111, 109, 110), rules);              // next morning's gap is not the trade's
  assert.equal(h.trades[0].exit, 101.5); assert.equal(h.trades[0].reason, "Square-off");
  const late = newBook();
  place(late, { side: "long", type: "market", qty: 1, product: "intraday" }, bar(at(1, 15, 15), 100, 100, 100, 100), { shortOk: true });
  advance(late, bar(at(1, 15, 20), 100, 100, 100, 100), rules);
  assert.ok(!late.position && !late.pending && !late.trades.length);
  const carry = newBook();
  place(carry, { side: "long", type: "market", qty: 1, product: "delivery" }, bar(at(1, 15, 15), 100, 100, 100, 100), { shortOk: true });
  advance(carry, bar(at(1, 15, 20), 100, 100, 100, 100), rules);
  advance(carry, bar(at(2, 9, 15), 105, 105, 105, 105), rules);
  assert.ok(carry.position);                                               // delivery is held overnight
});

test("exit at next open, moving the stoploss, cancelling, finishing and totals", () => {
  const b = newBook(), last = bar(at(1, 10, 0), 100, 101, 99, 100);
  place(b, { side: "long", type: "limit", price: 99, qty: 1, product: "delivery" }, last, { shortOk: true });
  assert.match(setLevels(b, { stop: 100 }, 100), /below the price/);
  assert.equal(setLevels(b, { stop: 97 }, 100), "");
  assert.ok(cancel(b) && !b.pending);
  place(b, { side: "long", type: "market", qty: 1, product: "delivery" }, last, { shortOk: true });
  advance(b, bar(at(1, 10, 5), 100, 101, 99, 100), delivery);
  assert.ok(exitAtNextOpen(b));
  advance(b, bar(at(1, 10, 10), 104, 105, 103, 104), delivery);
  assert.equal(b.trades[0].reason, "Exit"); assert.equal(b.trades[0].gross, 4);
  place(b, { side: "long", type: "market", qty: 1, product: "delivery" }, last, { shortOk: true });
  advance(b, bar(at(1, 10, 15), 104, 104, 100, 101), delivery);
  assert.equal(finish(b).exit, 101);
  const t = totals(b.trades);
  assert.equal(t.trades, 2); assert.equal(t.wins, 1); assert.equal(t.net, 1); assert.equal(t.maxDrawdown, -3);
});

test("an intraday order is refused once the next candle starts after 3:20 PM", () => {
  const b = newBook();
  const late = bar(at(1, 15, 15), 100, 100, 100, 100);
  assert.match(place(b, { side: "long", type: "market", qty: 1, product: "intraday" }, late, { shortOk: true, squareOff: true, seconds: 900 }), /3:20 PM/);
  assert.equal(place(b, { side: "long", type: "market", qty: 1, product: "delivery" }, late, { shortOk: true, squareOff: true, seconds: 900 }), "");
  const c = newBook();
  assert.equal(place(c, { side: "long", type: "market", qty: 1, product: "intraday" }, bar(at(1, 15, 10), 100, 100, 100, 100), { shortOk: true, squareOff: true, seconds: 300 }), "");
});
