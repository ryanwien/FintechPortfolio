/* SVG charts, exposed as window.Charts: a tiny sparkline and the main line chart
   with hover inspection. No library - the whole site is hand-written and has no
   build step.

   lineChart draws at the container's real pixel width so axis text stays legible,
   and Charts.redrawAll re-renders every mounted chart (a ResizeObserver keeps them
   matched to their container afterwards). */
(() => {
  "use strict";
  const { el, mk } = window.UI;

  /** y-axis label: 1234567 -> "1.23M", 56789 -> "56.8k", 0.1234 -> "0.123". */
  const fmtAxis = (v) => {
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
    if (a >= 100) return v.toFixed(0);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(3);
  };

  /** x-axis label: integers as-is, long decimals to 3 significant figures, text untouched. */
  const fmtTick = (x) => {
    const n = Number(x);
    if (typeof x === "string" && !Number.isFinite(n)) return x;
    return Number.isInteger(n) ? n.toLocaleString() : String(Number(n.toPrecision(3)));
  };

  let gradSeq = 0; // unique gradient ids when several charts are mounted

  /** Minimal 100x30 trend line for table rows and KPI tiles. */
  function sparkline(series, color) {
    const svg = mk("svg", { viewBox: "0 0 100 30", preserveAspectRatio: "none", "aria-hidden": "true" });
    const ys = (series?.y || []).map(Number).filter(Number.isFinite);
    if (ys.length < 2) return svg;
    const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const pts = ys.map((y, i) => `${(i / (ys.length - 1)) * 100},${28 - ((y - lo) / span) * 26}`);
    svg.append(mk("polyline", { points: pts.join(" "), fill: "none", stroke: color, "stroke-width": 1.6, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
    return svg;
  }

  /** Full line chart with gridlines, smart axis labels, gradient fill and a hover tip. */
  function lineChart(host, series) {
    host.replaceChildren();
    host._series = series; // remembered so redrawAll/ResizeObserver can re-render
    host.append(el("p", "chart__name", series.name));
    const W = Math.max(280, Math.round(host.clientWidth)), H = W < 600 ? 200 : 260;
    const R = 12, T = 12, B = 26;
    const ys = series.y.map(Number);
    const xsRaw = series.x && series.x.length === ys.length ? series.x : ys.map((_, i) => i + 1);
    const numeric = xsRaw.every((x) => Number.isFinite(Number(x)));
    const xs = numeric ? xsRaw.map(Number) : xsRaw.map((_, i) => i);
    const xlo = Math.min(...xs), xhi = Math.max(...xs);
    let lo = Math.min(...ys), hi = Math.max(...ys);
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.1;
    // Never pad a non-negative series (rates, counts, money) below zero.
    lo = lo >= 0 ? Math.max(0, lo - pad) : lo - pad; hi += pad;
    const yTicks = [0, 1, 2, 3, 4].map((i) => lo + ((hi - lo) * i) / 4);
    // Left gutter fits the widest y label (11px mono is ~6.7px per char) plus the 8px gap.
    const L = Math.max(36, Math.ceil(Math.max(...yTicks.map((v) => fmtAxis(v).length)) * 6.7) + 12);
    const X = (x) => L + ((x - xlo) / (xhi - xlo || 1)) * (W - L - R);
    const Y = (y) => T + (1 - (y - lo) / (hi - lo)) * (H - T - B);
    const color = getComputedStyle(document.documentElement).getPropertyValue("--chart").trim() || "#4caf32";
    const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": series.name });

    // gradient area fill
    const id = `g${++gradSeq}`;
    const defs = mk("defs", {});
    const g = mk("linearGradient", { id, x1: 0, x2: 0, y1: 0, y2: 1 });
    g.append(mk("stop", { offset: "0%", "stop-color": color, "stop-opacity": 0.35 }), mk("stop", { offset: "100%", "stop-color": color, "stop-opacity": 0 }));
    defs.append(g); svg.append(defs);

    // horizontal gridlines + y labels
    for (const v of yTicks) {
      const y = Y(v);
      svg.append(mk("line", { x1: L, x2: W - R, y1: y, y2: y, class: "grid" }));
      const t = mk("text", { x: L - 8, y: y + 4, "text-anchor": "end" }); t.textContent = fmtAxis(v); svg.append(t);
    }

    // x ticks at the left edge, middle and right edge of the plot, using the data point
    // nearest each spot (x may be uneven or descending). A tick too close to one already
    // placed is dropped so labels never overlap.
    const MIN_GAP = 90;
    const placed = [];
    [L, (L + W - R) / 2, W - R].forEach((target) => {
      let best = 0;
      for (let i = 1; i < xs.length; i++) if (Math.abs(X(xs[i]) - target) < Math.abs(X(xs[best]) - target)) best = i;
      const px = X(xs[best]);
      if (placed.some((q) => Math.abs(q - px) < MIN_GAP)) return;
      placed.push(px);
      const anchor = px <= L + 1 ? "start" : px >= W - R - 1 ? "end" : "middle";
      const t = mk("text", { x: px, y: H - 8, "text-anchor": anchor });
      t.textContent = fmtTick(xsRaw[best]); svg.append(t);
    });

    // the series itself: filled area under a stroked line
    const d = ys.map((y, i) => `${i ? "L" : "M"}${X(xs[i]).toFixed(1)},${Y(y).toFixed(1)}`).join("");
    svg.append(mk("path", { d: `${d}L${X(xs[xs.length - 1]).toFixed(1)},${H - B}L${X(xs[0]).toFixed(1)},${H - B}Z`, fill: `url(#${id})` }));
    svg.append(mk("path", { d, fill: "none", stroke: color, "stroke-width": 2.2, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));

    // hover inspection: guide line, dot on the nearest point, tooltip with the value
    const guide = mk("line", { y1: T, y2: H - B, class: "guide", opacity: 0 });
    const dot = mk("circle", { r: 5, class: "dotp", opacity: 0, "vector-effect": "non-scaling-stroke" });
    svg.append(guide, dot);
    host.append(svg);
    const tip = el("div", "tip"); host.append(tip);
    const move = (clientX) => {
      const r = svg.getBoundingClientRect();
      const vx = ((clientX - r.left) / r.width) * W;
      let best = 0;
      for (let i = 1; i < xs.length; i++) if (Math.abs(X(xs[i]) - vx) < Math.abs(X(xs[best]) - vx)) best = i;
      const px = X(xs[best]), py = Y(ys[best]);
      guide.setAttribute("x1", px); guide.setAttribute("x2", px); guide.setAttribute("opacity", 1);
      dot.setAttribute("cx", px); dot.setAttribute("cy", py); dot.setAttribute("opacity", 1);
      tip.replaceChildren(el("b", null, ys[best].toLocaleString(undefined, { maximumFractionDigits: 4 })), document.createTextNode(String(xsRaw[best])));
      tip.style.left = `${(px / W) * r.width}px`;
      tip.style.top = `${(py / H) * r.height}px`;
      tip.style.opacity = 1;
    };
    svg.addEventListener("pointermove", (e) => move(e.clientX));
    svg.addEventListener("pointerleave", () => { tip.style.opacity = 0; guide.setAttribute("opacity", 0); dot.setAttribute("opacity", 0); });
  }

  // Redraw a chart whenever its own box changes width: window resizes, but also late font
  // loads and grid reflows that move the layout without firing a window resize.
  const chartWidths = new WeakMap();
  const observer = new ResizeObserver((entries) => {
    for (const { target } of entries) {
      const w = Math.round(target.clientWidth);
      if (target._series && chartWidths.get(target) !== w) {
        chartWidths.set(target, w);
        lineChart(target, target._series);
      }
    }
  });

  /** Re-render every mounted .chart; called by the router after each view mounts,
      because views are built detached and only know their width once in the DOM. */
  function redrawAll() {
    document.querySelectorAll(".chart").forEach((h) => {
      if (!h._series) return;
      chartWidths.set(h, Math.round(h.clientWidth));
      lineChart(h, h._series);
      observer.observe(h);
    });
  }

  window.Charts = { sparkline, lineChart, redrawAll };
})();
