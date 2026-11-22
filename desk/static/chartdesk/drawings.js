// @ts-check
// Drawings on the price chart: trend line, horizontal line and Fibonacci retracement. Each is stored as
// points of (bar time, price), so it stays where it was drawn when you change the candle size or reload.
// Drawn with the chart library's plugin interface (a series primitive) on the price pane's canvas.

/** @typedef {{ time: number, price: number }} Pt */
/** @typedef {{ id: string, kind: "trend"|"hline"|"fib", points: Pt[] }} Drawing */

export const TOOLS = { trend: { label: "Trend line", points: 2 }, hline: { label: "Horizontal line", points: 1 }, fib: { label: "Fibonacci", points: 2 } };
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

export class DrawingLayer {
  /** @param {{ bars: {time:number}[], seconds: number, colors: string[], text: string, format: (p: number) => string }} opts */
  constructor(opts) {
    this.opts = opts;
    /** @type {Drawing[]} */ this.items = [];
    /** @type {Drawing|null} */ this.preview = null;
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

  /** @param {any} target */
  draw(target) {
    if (!this.chart || !this.series) return;
    const all = this.preview ? [...this.items, this.preview] : this.items;
    target.useBitmapCoordinateSpace((/** @type {any} */ scope) => {
      const ctx = scope.context, hr = scope.horizontalPixelRatio, vr = scope.verticalPixelRatio, width = scope.bitmapSize.width;
      ctx.save();
      ctx.font = `${Math.round(11 * vr)}px system-ui, sans-serif`;
      all.forEach((d, n) => {
        const color = this.opts.colors[n % this.opts.colors.length];
        ctx.strokeStyle = color; ctx.fillStyle = color; ctx.lineWidth = Math.max(1, Math.round(1.5 * hr));
        ctx.globalAlpha = d === this.preview ? 0.6 : 1;
        if (d.kind === "hline") {
          const y = this.y(d.points[0].price);
          if (y == null) return;
          ctx.beginPath(); ctx.moveTo(0, y * vr); ctx.lineTo(width, y * vr); ctx.stroke();
          ctx.fillText(this.opts.format(d.points[0].price), 6 * hr, y * vr - 4 * vr);
          return;
        }
        if (d.points.length < 2) return;
        const [a, b] = d.points, x1 = this.x(a.time), x2 = this.x(b.time);
        if (x1 == null || x2 == null) return;
        if (d.kind === "trend") {
          const y1 = this.y(a.price), y2 = this.y(b.price);
          if (y1 == null || y2 == null) return;
          ctx.beginPath(); ctx.moveTo(x1 * hr, y1 * vr); ctx.lineTo(x2 * hr, y2 * vr); ctx.stroke();
          [[x1, y1], [x2, y2]].forEach(([x, y]) => { ctx.beginPath(); ctx.arc(x * hr, y * vr, 3 * hr, 0, Math.PI * 2); ctx.fill(); });
          return;
        }
        const left = Math.min(x1, x2) * hr;
        fibLevels(a.price, b.price).forEach((level, i) => {
          const y = this.y(level.price);
          if (y == null) return;
          ctx.globalAlpha = (d === this.preview ? 0.5 : 1) * (i === 0 || i === FIB_LEVELS.length - 1 ? 1 : 0.75);
          ctx.beginPath(); ctx.moveTo(left, y * vr); ctx.lineTo(width, y * vr); ctx.stroke();
          ctx.fillText(`${(level.ratio * 100).toFixed(1)}%  ${this.opts.format(level.price)}`, left + 4 * hr, y * vr - 3 * vr);
        });
        ctx.setLineDash([4 * hr, 4 * hr]);
        const y1 = this.y(a.price), y2 = this.y(b.price);
        if (y1 != null && y2 != null) { ctx.beginPath(); ctx.moveTo(x1 * hr, y1 * vr); ctx.lineTo(x2 * hr, y2 * vr); ctx.stroke(); }
        ctx.setLineDash([]);
      });
      ctx.restore();
    });
  }
}
