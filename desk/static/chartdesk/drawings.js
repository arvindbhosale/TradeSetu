// @ts-check
// Drawings on the price chart: lines, shapes, Fibonacci levels, a measuring tool and long/short position
// tools that show profit and loss for an entry, a stoploss and a target. Each is stored as points of
// (bar time, price), so it stays where it was drawn when you change the candle size or reload. Drawn with the
// chart library's plugin interface (a series primitive) on the price pane's canvas. Drawings can be picked,
// dragged by a handle or moved as a whole, recoloured and deleted.

/** @typedef {{ time: number, price: number }} Pt */
/** @typedef {{ id: string, kind: string, points: Pt[], color?: string, qty?: number }} Drawing */
/** @typedef {{ x: number, y: number }} XY */

// `clicks` is how many taps place it; `points` how many points it keeps (a position keeps three from one tap).
export const TOOLS = {
  trend: { label: "Trend line", group: "Lines", clicks: 2, points: 2, hint: "Tap the start, then the end." },
  ray: { label: "Ray", group: "Lines", clicks: 2, points: 2, hint: "Tap the start, then a point it runs through." },
  extended: { label: "Extended line", group: "Lines", clicks: 2, points: 2, hint: "Tap two points; the line runs past both." },
  hline: { label: "Horizontal line", group: "Lines", clicks: 1, points: 1, hint: "Tap the chart at the price." },
  hray: { label: "Horizontal ray", group: "Lines", clicks: 1, points: 1, hint: "Tap where it starts; it runs to the right." },
  vline: { label: "Vertical line", group: "Lines", clicks: 1, points: 1, hint: "Tap the candle." },
  rect: { label: "Rectangle", group: "Shapes", clicks: 2, points: 2, hint: "Tap one corner, then the opposite corner." },
  channel: { label: "Parallel channel", group: "Shapes", clicks: 3, points: 3, hint: "Tap two points for the first line, then a third for its parallel." },
  fib: { label: "Fibonacci", group: "Shapes", clicks: 2, points: 2, hint: "Tap the swing start, then the swing end." },
  measure: { label: "Measure", group: "Shapes", clicks: 2, points: 2, hint: "Tap two points to see the price change, % and candles between them." },
  long: { label: "Long position", group: "Positions", clicks: 1, points: 3, hint: "Tap the entry price. Then drag the green (target) and red (stoploss) edges." },
  short: { label: "Short position", group: "Positions", clicks: 1, points: 3, hint: "Tap the entry price. Then drag the green (target) and red (stoploss) edges." },
};
export const FIB_LEVELS = [0, 0.236, 0.382, 0.5, 0.618, 0.786, 1];

/** Bar index (fractional between bars, extended past either end) for a time. @param {{time:number}[]} bars
 * @param {number} time @param {number} seconds @returns {number} */
export function logicalOf(bars, time, seconds) {
  const n = bars.length;
  if (!n) return 0;
  if (time <= bars[0].time) return (time - bars[0].time) / seconds;
  if (time >= bars[n - 1].time) return n - 1 + (time - bars[n - 1].time) / seconds;
  let lo = 0, hi = n - 1;
  while (hi - lo > 1) { const mid = (lo + hi) >> 1; if (bars[mid].time <= time) lo = mid; else hi = mid; }
  const span = bars[hi].time - bars[lo].time;
  return lo + (span > 0 ? (time - bars[lo].time) / span : 0);
}

/** The time at a bar index: the bar's own time inside the data, extended by the candle size outside it.
 * @param {{time:number}[]} bars @param {number} logical @param {number} seconds */
export function timeOf(bars, logical, seconds) {
  const n = bars.length, i = Math.round(logical);
  if (!n) return 0;
  if (i < 0) return bars[0].time + i * seconds;
  if (i > n - 1) return bars[n - 1].time + (i - (n - 1)) * seconds;
  return bars[i].time;
}

/** Fibonacci levels between a first and a second point: 0% at the first, 100% at the second.
 * @param {number} from @param {number} to */
export function fibLevels(from, to) { return FIB_LEVELS.map((r) => ({ ratio: r, price: from + (to - from) * r })); }

/** A new long or short position at a tapped entry: stoploss 1% away, target 2% away (reward twice the risk),
 * 20 candles wide. Points are [entry, target (with the right edge's time), stoploss].
 * @param {"long"|"short"} kind @param {Pt} at @param {number} seconds @returns {Pt[]} */
export function newPosition(kind, at, seconds) {
  const e = at.price, dir = kind === "long" ? 1 : -1;
  return [{ time: at.time, price: e }, { time: at.time + 20 * seconds, price: e * (1 + 0.02 * dir) }, { time: at.time, price: e * (1 - 0.01 * dir) }];
}

/** What a position drawing means as an order: side, entry, stoploss, target, quantity and the money at stake.
 * This is the shape the chart's order ticket reads.
 * @param {Drawing} d @param {number|null} [last] the latest price, for the profit or loss right now */
export function positionPlan(d, last = null) {
  const [entry, target, stop] = d.points.map((p) => p.price), dir = d.kind === "long" ? 1 : -1, qty = d.qty && d.qty > 0 ? d.qty : 1;
  const reward = (target - entry) * dir * qty, risk = (entry - stop) * dir * qty;
  return { side: d.kind === "long" ? "BUY" : "SELL", entry, stoploss: stop, target, quantity: qty,
    reward, risk, rewardPct: ((target - entry) / entry) * 100 * dir, riskPct: ((entry - stop) / entry) * 100 * dir,
    riskReward: risk > 0 ? reward / risk : null, now: last != null && last > 0 ? (last - entry) * dir * qty : null };
}

/** Keeps a position the right way round: for a long the target is above the entry and the stoploss below it
 * (the other way for a short), and the stoploss's time is the entry's. Other drawings are returned as they are.
 * @param {Drawing} d @returns {Drawing} */
export function normalise(d) {
  if (d.kind !== "long" && d.kind !== "short") return d;
  const [e, t, s] = d.points, dir = d.kind === "long" ? 1 : -1, tick = e.price * 0.0005;
  const target = (t.price - e.price) * dir > tick ? t.price : e.price + tick * dir * 2;
  const stop = (e.price - s.price) * dir > tick ? s.price : e.price - tick * dir * 2;
  const end = t.time > e.time ? t.time : e.time + 1;
  return { ...d, points: [{ ...e }, { time: end, price: target }, { time: e.time, price: stop > 0 ? stop : e.price / 2 }] };
}

/** Distance from p to the segment a-b (or the infinite line when `extend` says which ends run on).
 * @param {XY} p @param {XY} a @param {XY} b @param {[boolean, boolean]} [extend] */
export function distanceToLine(p, a, b, extend = [false, false]) {
  const dx = b.x - a.x, dy = b.y - a.y, len = dx * dx + dy * dy;
  let t = len ? ((p.x - a.x) * dx + (p.y - a.y) * dy) / len : 0;
  if (!extend[0]) t = Math.max(t, 0);
  if (!extend[1]) t = Math.min(t, 1);
  if (!len) t = 0;
  return Math.hypot(p.x - (a.x + t * dx), p.y - (a.y + t * dy));
}

/** Where the line through a and b leaves a box of the given width (for rays and extended lines).
 * @param {XY} a @param {XY} b @param {number} width @param {boolean} back also run to the left */
function stretch(a, b, width, back) {
  const dx = b.x - a.x, dy = b.y - a.y;
  if (Math.abs(dx) < 1e-9) return { from: back ? { x: a.x, y: -1e5 } : a, to: { x: a.x, y: dy >= 0 ? 1e5 : -1e5 } };
  const at = (/** @type {number} */ x) => ({ x, y: a.y + (dy / dx) * (x - a.x) });
  const right = dx > 0 ? at(width + 10) : at(-10);
  return { from: back ? (dx > 0 ? at(-10) : at(width + 10)) : a, to: right };
}

export class DrawingLayer {
  /** @param {{ bars: {time:number}[], seconds: number, colors: string[], good: string, bad: string, text: string, surface: string,
   *   format: (p: number) => string, money: (v: number) => string, last: () => number|null }} opts */
  constructor(opts) {
    this.opts = opts;
    /** @type {Drawing[]} */ this.items = [];
    /** @type {Drawing|null} */ this.preview = null;
    /** @type {string|null} */ this.selected = null;
    /** @type {any} */ this.chart = null;
    /** @type {any} */ this.series = null;
    /** @type {null|(() => void)} */ this.request = null;
  }

  /** @param {{ chart: any, series: any, requestUpdate: () => void }} p */
  attached({ chart, series, requestUpdate }) { this.chart = chart; this.series = series; this.request = requestUpdate; }
  detached() { this.chart = this.series = this.request = null; }
  updateAllViews() { /* positions are worked out when drawing */ }
  update() { if (this.request) this.request(); }
  paneViews() { return [{ zOrder: () => "top", renderer: () => ({ draw: (/** @type {any} */ target) => this.draw(target) }) }]; }

  /** @param {number} time */
  x(time) { return this.chart.timeScale().logicalToCoordinate(logicalOf(this.opts.bars, time, this.opts.seconds)); }
  /** @param {number} price */
  y(price) { return this.series.priceToCoordinate(price); }
  /** @param {Pt} p @returns {XY|null} */
  xy(p) { const x = this.x(p.time), y = this.y(p.price); return x == null || y == null ? null : { x, y }; }

  /** @param {Drawing} d @param {number} n */
  color(d, n) {
    if (d.color) return d.color;
    if (d.kind === "long" || d.kind === "short") return this.opts.text;
    return this.opts.colors[n % this.opts.colors.length];
  }

  /** The drawing under a point, newest first, and which handle (point index) if one is close; -1 is the body.
   * @param {number} x @param {number} y @param {number} [tol] @returns {{ id: string, handle: number }|null} */
  hit(x, y, tol = 7) {
    if (!this.chart || !this.series) return null;
    const p = { x, y }, width = this.chart.timeScale().width();
    for (let n = this.items.length - 1; n >= 0; n--) {
      const d = this.items[n], pts = d.points.map((q) => this.xy(q));
      if (pts.some((q) => !q)) continue;
      const xy = /** @type {XY[]} */ (pts);
      const handles = this.handles(d, xy);
      const h = handles.findIndex((q) => Math.hypot(q.x - x, q.y - y) <= tol + 3);
      if (h >= 0) return { id: d.id, handle: h };
      if (this.near(d, xy, p, width, tol)) return { id: d.id, handle: -1 };
    }
    return null;
  }

  /** Where a drawing's grab handles are, one per stored point. @param {Drawing} d @param {XY[]} xy @returns {XY[]} */
  handles(d, xy) {
    if (d.kind === "hline") return [{ x: Math.min(40, xy[0].x), y: xy[0].y }];
    if (d.kind === "vline") return [{ x: xy[0].x, y: 30 }];
    if (d.kind === "long" || d.kind === "short") return [xy[0], { x: xy[1].x, y: xy[1].y }, { x: xy[0].x, y: xy[2].y }];
    return xy;
  }

  /** @param {Drawing} d @param {XY[]} xy @param {XY} p @param {number} width @param {number} tol */
  near(d, xy, p, width, tol) {
    const [a, b, c] = xy;
    switch (d.kind) {
      case "hline": return Math.abs(p.y - a.y) <= tol;
      case "hray": return Math.abs(p.y - a.y) <= tol && p.x >= a.x - tol;
      case "vline": return Math.abs(p.x - a.x) <= tol;
      case "trend": case "measure": return distanceToLine(p, a, b) <= tol || (d.kind === "measure" && inside(p, a, b));
      case "ray": return distanceToLine(p, a, b, [false, true]) <= tol;
      case "extended": return distanceToLine(p, a, b, [true, true]) <= tol;
      case "rect": return inside(p, a, b, tol);
      case "fib": return p.x >= Math.min(a.x, b.x) - tol && p.y >= Math.min(a.y, b.y) - tol && p.y <= Math.max(a.y, b.y) + tol;
      case "channel": {
        const off = this.channelOffset(d), a2 = this.xy({ time: d.points[0].time, price: d.points[0].price + off }), b2 = this.xy({ time: d.points[1].time, price: d.points[1].price + off });
        if (!a2 || !b2) return false;
        return distanceToLine(p, a, b) <= tol || distanceToLine(p, a2, b2) <= tol || inQuad(p, [a, b, b2, a2]);
      }
      case "long": case "short": return p.x >= a.x - tol && p.x <= b.x + tol && p.y >= Math.min(b.y, c.y) - tol && p.y <= Math.max(b.y, c.y) + tol;
      default: return false;
    }
  }

  /** How far (in price) the channel's parallel line is from its first line. @param {Drawing} d */
  channelOffset(d) {
    const [a, b, c] = d.points, la = logicalOf(this.opts.bars, a.time, this.opts.seconds), lb = logicalOf(this.opts.bars, b.time, this.opts.seconds);
    const lc = logicalOf(this.opts.bars, c.time, this.opts.seconds), at = lb === la ? a.price : a.price + (b.price - a.price) * (lc - la) / (lb - la);
    return c.price - at;
  }

  /** @param {any} target */
  draw(target) {
    if (!this.chart || !this.series) return;
    const all = this.preview ? [...this.items, this.preview] : this.items;
    target.useBitmapCoordinateSpace((/** @type {any} */ scope) => {
      const ctx = scope.context, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio;
      const width = scope.bitmapSize.width / hr, height = scope.bitmapSize.height / vr;
      ctx.save();
      ctx.scale(hr, vr);
      ctx.font = "11px system-ui, sans-serif";
      ctx.textBaseline = "alphabetic";
      all.forEach((d, n) => {
        const pts = d.points.map((q) => this.xy(q));
        if (d.points.length && pts.some((q) => !q)) return;
        const xy = /** @type {XY[]} */ (pts), color = this.color(d, n), isPreview = d === this.preview;
        ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = 1.5; ctx.setLineDash([]);
        ctx.globalAlpha = isPreview ? 0.65 : 1;
        this.paint(ctx, d, xy, color, width, height);
        if (d.id === this.selected && !isPreview) {
          ctx.globalAlpha = 1; ctx.setLineDash([]);
          this.handles(d, xy).forEach((h) => {
            ctx.beginPath(); ctx.arc(h.x, h.y, 5, 0, Math.PI * 2);
            ctx.fillStyle = this.opts.surface; ctx.fill(); ctx.strokeStyle = color; ctx.lineWidth = 2; ctx.stroke();
          });
        }
      });
      ctx.restore();
    });
  }

  /** @param {CanvasRenderingContext2D} ctx @param {Drawing} d @param {XY[]} xy @param {string} color @param {number} width @param {number} height */
  paint(ctx, d, xy, color, width, height) {
    const line = (/** @type {XY} */ a, /** @type {XY} */ b) => { ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.stroke(); };
    const dot = (/** @type {XY} */ a) => { ctx.beginPath(); ctx.arc(a.x, a.y, 3, 0, Math.PI * 2); ctx.fill(); };
    const label = (/** @type {string} */ text, /** @type {number} */ x, /** @type {number} */ y, align = "left") => {
      ctx.textAlign = /** @type {CanvasTextAlign} */ (align); ctx.fillText(text, x, y);
    };
    const fmt = this.opts.format;
    const [a, b, c] = xy;
    switch (d.kind) {
      case "hline": line({ x: 0, y: a.y }, { x: width, y: a.y }); label(fmt(d.points[0].price), 6, a.y - 4); return;
      case "hray": line(a, { x: width, y: a.y }); dot(a); label(fmt(d.points[0].price), a.x + 6, a.y - 4); return;
      case "vline": line({ x: a.x, y: 0 }, { x: a.x, y: height }); return;
      case "trend": line(a, b); dot(a); dot(b); return;
      case "ray": { const s = stretch(a, b, width, false); line(s.from, s.to); dot(a); dot(b); return; }
      case "extended": { const s = stretch(a, b, width, true); line(s.from, s.to); dot(a); dot(b); return; }
      case "rect": {
        const x = Math.min(a.x, b.x), y = Math.min(a.y, b.y), w = Math.abs(b.x - a.x), h = Math.abs(b.y - a.y);
        ctx.globalAlpha *= 0.15; ctx.fillRect(x, y, w, h); ctx.globalAlpha /= 0.15; ctx.strokeRect(x, y, w, h); return;
      }
      case "channel": {
        if (!c) { line(a, b); return; }
        const off = this.channelOffset(d), a2 = this.xy({ time: d.points[0].time, price: d.points[0].price + off }), b2 = this.xy({ time: d.points[1].time, price: d.points[1].price + off });
        if (!a2 || !b2) return;
        ctx.globalAlpha *= 0.12; ctx.beginPath(); ctx.moveTo(a.x, a.y); ctx.lineTo(b.x, b.y); ctx.lineTo(b2.x, b2.y); ctx.lineTo(a2.x, a2.y); ctx.closePath(); ctx.fill(); ctx.globalAlpha /= 0.12;
        line(a, b); line(a2, b2);
        ctx.setLineDash([4, 4]); line({ x: a.x, y: (a.y + a2.y) / 2 }, { x: b.x, y: (b.y + b2.y) / 2 }); ctx.setLineDash([]);
        return;
      }
      case "fib": {
        const left = Math.min(a.x, b.x);
        fibLevels(d.points[0].price, d.points[1].price).forEach((level, i) => {
          const y = this.y(level.price);
          if (y == null) return;
          const base = ctx.globalAlpha;
          ctx.globalAlpha = base * (i === 0 || i === FIB_LEVELS.length - 1 ? 1 : 0.75);
          line({ x: left, y }, { x: width, y });
          label(`${(level.ratio * 100).toFixed(1)}%  ${fmt(level.price)}`, left + 4, y - 3);
          ctx.globalAlpha = base;
        });
        ctx.setLineDash([4, 4]); line(a, b); ctx.setLineDash([]);
        return;
      }
      case "measure": {
        const [p0, p1] = d.points, diff = p1.price - p0.price, pct = (diff / p0.price) * 100;
        const bars = Math.round(logicalOf(this.opts.bars, p1.time, this.opts.seconds) - logicalOf(this.opts.bars, p0.time, this.opts.seconds));
        const tone = diff >= 0 ? this.opts.good : this.opts.bad;
        ctx.fillStyle = tone; ctx.strokeStyle = tone;
        ctx.globalAlpha *= 0.15; ctx.fillRect(Math.min(a.x, b.x), Math.min(a.y, b.y), Math.abs(b.x - a.x), Math.abs(b.y - a.y)); ctx.globalAlpha /= 0.15;
        line(a, b); dot(b);
        const text = `${diff >= 0 ? "+" : "−"}${fmt(Math.abs(diff))}  (${diff >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%)  ·  ${Math.abs(bars)} candle${Math.abs(bars) === 1 ? "" : "s"}`;
        pill(ctx, text, (a.x + b.x) / 2, Math.min(a.y, b.y) - 8, tone, this.opts.surface);
        return;
      }
      case "long": case "short": this.paintPosition(ctx, d, xy); return;
      default:
    }
  }

  /** @param {CanvasRenderingContext2D} ctx @param {Drawing} d @param {XY[]} xy */
  paintPosition(ctx, d, xy) {
    const [e, t, s] = xy, x0 = Math.min(e.x, t.x), w = Math.max(Math.abs(t.x - e.x), 24), plan = positionPlan(d, this.opts.last());
    const fmt = this.opts.format, money = this.opts.money, base = ctx.globalAlpha;
    const box = (/** @type {number} */ y1, /** @type {number} */ y2, /** @type {string} */ col) => {
      ctx.fillStyle = col; ctx.globalAlpha = base * 0.18; ctx.fillRect(x0, Math.min(y1, y2), w, Math.abs(y2 - y1));
      ctx.globalAlpha = base; ctx.strokeStyle = col; ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(x0, y2); ctx.lineTo(x0 + w, y2); ctx.stroke();
    };
    box(e.y, t.y, this.opts.good); box(e.y, s.y, this.opts.bad);
    ctx.strokeStyle = this.opts.text; ctx.lineWidth = 1.5; ctx.beginPath(); ctx.moveTo(x0, e.y); ctx.lineTo(x0 + w, e.y); ctx.stroke();
    const cx = x0 + w / 2, up = t.y < s.y;
    const tText = `Target ${fmt(plan.target)} (${plan.rewardPct >= 0 ? "+" : ""}${plan.rewardPct.toFixed(2)}%)  ${signed(money, plan.reward)}`;
    const sText = `Stoploss ${fmt(plan.stoploss)} (−${Math.abs(plan.riskPct).toFixed(2)}%)  ${signed(money, -plan.risk)}`;
    const ink = this.opts.surface;
    pill(ctx, tText, cx, up ? t.y - 6 : t.y + 18, this.opts.good, ink);
    pill(ctx, sText, cx, up ? s.y + 18 : s.y - 6, this.opts.bad, ink);
    const rr = plan.riskReward == null ? "–" : plan.riskReward.toFixed(2);
    // the entry's label sits left of the box on the entry line, clear of the target and stoploss labels
    const entryText = `${d.kind === "long" ? "Long" : "Short"} ${plan.quantity.toLocaleString("en-IN")} at ${fmt(plan.entry)}  ·  1 : ${rr}`;
    pill(ctx, entryText, x0 - 6 - (ctx.measureText(entryText).width + 12) / 2, e.y + 4, this.opts.text, ink);
    ctx.globalAlpha = base;
  }
}

/** @param {(v: number) => string} money @param {number} v */
function signed(money, v) { return (v >= 0 ? "+" : "−") + money(Math.abs(v)); }

/** A label on a filled rounded box, centred on x with its baseline at y. @param {CanvasRenderingContext2D} ctx
 * @param {string} text @param {number} x @param {number} y @param {string} color @param {string} ink the text colour */
function pill(ctx, text, x, y, color, ink) {
  const w = ctx.measureText(text).width + 12, base = ctx.globalAlpha;
  const room = ctx.canvas.width / (ctx.getTransform().a || 1);          // keep the label on the chart
  x = Math.max(w / 2 + 2, Math.min(x, room - w / 2 - 2));
  ctx.globalAlpha = base * 0.92; ctx.fillStyle = color;
  const left = x - w / 2, top = y - 13;
  ctx.beginPath();
  if (typeof ctx.roundRect === "function") ctx.roundRect(left, top, w, 17, 4); else ctx.rect(left, top, w, 17);
  ctx.fill();
  ctx.globalAlpha = base; ctx.fillStyle = ink; ctx.textAlign = "center"; ctx.fillText(text, x, y);
  ctx.textAlign = "left";
}

/** @param {XY} p @param {XY} a @param {XY} b @param {number} [tol] */
function inside(p, a, b, tol = 0) {
  return p.x >= Math.min(a.x, b.x) - tol && p.x <= Math.max(a.x, b.x) + tol && p.y >= Math.min(a.y, b.y) - tol && p.y <= Math.max(a.y, b.y) + tol;
}

/** Whether p is inside a four-cornered shape (corners in order). @param {XY} p @param {XY[]} q */
function inQuad(p, q) {
  let inside_ = false;
  for (let i = 0, j = q.length - 1; i < q.length; j = i++) {
    if ((q[i].y > p.y) !== (q[j].y > p.y) && p.x < ((q[j].x - q[i].x) * (p.y - q[i].y)) / (q[j].y - q[i].y) + q[i].x) inside_ = !inside_;
  }
  return inside_;
}
