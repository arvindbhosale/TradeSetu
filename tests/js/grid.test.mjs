// Several charts in one window: arrangements, resize handles and saved sizes (chartdesk/grid.js).
import assert from "node:assert/strict";
import { test } from "node:test";
import { readFileSync } from "node:fs";

globalThis.window = globalThis.window || {};
const { LAYOUTS, MAX_PANES, cleanGrid, handlesOf, placeOf, resize, sizesOf, tracks } = await import("../../desk/static/chartdesk/grid.js");

test("every arrangement fills its grid exactly once", () => {
  for (const [key, l] of Object.entries(LAYOUTS)) {
    assert.ok(l.cells.length >= 1 && l.cells.length <= MAX_PANES, key);
    const seen = new Set();
    l.cells.forEach(([c0, c1, r0, r1]) => {
      assert.ok(c0 >= 0 && c1 <= l.cols && c0 < c1 && r0 >= 0 && r1 <= l.rows && r0 < r1, key);
      for (let c = c0; c < c1; c++) for (let r = r0; r < r1; r++) { assert.ok(!seen.has(`${c},${r}`), `${key} overlaps`); seen.add(`${c},${r}`); }
    });
    assert.equal(seen.size, l.cols * l.rows, `${key} leaves a gap`);
  }
});

test("the server knows the same arrangements", () => {
  const py = readFileSync(new URL("../../desk/chartstore.py", import.meta.url), "utf8");
  const m = py.match(/GRID_LAYOUTS = \{([^}]*)\}/);
  assert.ok(m);
  const server = Object.fromEntries([...m[1].matchAll(/"(\w+)": \((\d), (\d)\)/g)].map((x) => [x[1], [Number(x[2]), Number(x[3])]]));
  assert.deepEqual(server, Object.fromEntries(Object.entries(LAYOUTS).map(([k, l]) => [k, [l.cols, l.rows]])));
});

test("cells and handles sit on the grid's lines, gutters between", () => {
  assert.equal(tracks([1, 2]), "minmax(0, 1fr) 8px minmax(0, 2fr)");
  assert.deepEqual(placeOf([0, 1, 0, 2]), { column: "1 / 2", row: "1 / 4" });
  assert.deepEqual(placeOf([1, 2, 1, 2]), { column: "3 / 4", row: "3 / 4" });
  // one large chart on the left: its column line runs the full height, the row line only on the right
  assert.deepEqual(handlesOf("3"), [
    { axis: "col", at: 1, column: "2 / 3", row: "1 / 4" },
    { axis: "row", at: 1, column: "3 / 4", row: "2 / 3" },
  ]);
  assert.equal(handlesOf("1").length, 0);
  assert.equal(handlesOf("6").length, 3);
});

test("dragging moves one line and keeps every chart a usable size", () => {
  assert.deepEqual(resize([1, 1], 1, 100, 1000), [0.6, 0.4]);
  const squeezed = resize([1, 1, 1], 1, -5000, 900);
  assert.ok(squeezed[0] >= 0.12 - 1e-9);
  assert.ok(Math.abs(squeezed[0] + squeezed[1] - 2 / 3) < 1e-3);
  assert.ok(Math.abs(squeezed[2] - 1 / 3) < 1e-3);
  assert.deepEqual(resize([1, 1], 1, 900, 1000, 300), [0.7, 0.3]);     // a pixel floor
  assert.deepEqual(resize([1, 1], 1, 900, 400, 300), [0.5, 0.5]);      // too small for it: an even split
});

test("saved settings are checked", () => {
  assert.deepEqual(cleanGrid(null), { layout: "2v", link: false, sizes: {} });
  const g = cleanGrid({ layout: "4", link: true, sizes: { 4: { cols: [2, 1], rows: [1, 1] }, 6: { cols: [1], rows: [1, 1] }, x: {} } });
  assert.deepEqual(g, { layout: "4", link: true, sizes: { 4: { cols: [2, 1], rows: [1, 1] } } });
  assert.equal(cleanGrid({ layout: "9" }).layout, "2v");
  assert.deepEqual(sizesOf("6", {}), { cols: [1, 1, 1], rows: [1, 1] });
});
