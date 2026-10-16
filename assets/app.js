(() => {
  "use strict";
  const DATA = window.PORTFOLIO || [];
  const META = window.PORTFOLIO_META || {};
  const REPO = "https://github.com/ryanwien/FintechPortfolio";
  const SYM = { ledger: "LDG", fraud: "FRD", credit: "CRD", exchange: "XCH", aml: "AML" };
  const COLORS = ["#b6ef5a", "#7cb3ff", "#f5b84a", "#ff7b86", "#c084fc"];
  const NAMES = { ledger: "Ledger & payments", fraud: "Fraud scoring", credit: "Credit risk", exchange: "Matching engine", aml: "AML monitoring" };
  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const fmtInt = (n) => Number(n).toLocaleString();

  // ---------- theme ----------
  const root = document.documentElement;
  const applyTheme = (t) => {
    root.dataset.theme = t;
    $("#theme .theme__txt").textContent = t === "dark" ? "Dark mode" : "Light mode";
  };
  let saved = null;
  try { saved = localStorage.getItem("theme"); } catch (_) { /* private mode */ }
  applyTheme(saved || "dark");
  $("#theme").addEventListener("click", () => {
    const t = root.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(t);
    try { localStorage.setItem("theme", t); } catch (_) { /* ignore */ }
  });

  // ---------- mobile nav ----------
  const side = $("#side"), scrim = $("#scrim"), burger = $("#burger");
  const closeNav = () => { side.classList.remove("open"); scrim.hidden = true; burger.setAttribute("aria-expanded", "false"); };
  burger.addEventListener("click", () => {
    const open = !side.classList.contains("open");
    side.classList.toggle("open", open); scrim.hidden = !open; burger.setAttribute("aria-expanded", String(open));
  });
  scrim.addEventListener("click", closeNav);

  // ---------- sidebar project links ----------
  const navHost = $("#nav-projects");
  DATA.forEach((p) => {
    const a = el("a", "nav__item");
    a.href = `#/${p.slug}`; a.dataset.route = `/${p.slug}`;
    a.append(el("span", "nav__sym", SYM[p.slug] || p.slug.slice(0, 3).toUpperCase()), document.createTextNode(shortTitle(p)));
    a.append(el("span", "nav__v", `${p.tests}t`));
    a.addEventListener("click", closeNav);
    navHost.append(a);
  });
  function shortTitle(p) { return NAMES[p.slug] || p.title; }

  // ---------- charts ----------
  const NS = "http://www.w3.org/2000/svg";
  const mk = (tag, attrs) => { const n = document.createElementNS(NS, tag); for (const k in attrs) n.setAttribute(k, attrs[k]); return n; };
  const fmt = (v) => {
    const a = Math.abs(v);
    if (a >= 1e6) return (v / 1e6).toFixed(2) + "M";
    if (a >= 1e4) return (v / 1e3).toFixed(1) + "k";
    if (a >= 100) return v.toFixed(0);
    if (a >= 1) return v.toFixed(2);
    return v.toFixed(3);
  };
  let gradSeq = 0;

  function sparkline(series, color) {
    const svg = mk("svg", { viewBox: "0 0 100 30", preserveAspectRatio: "none", "aria-hidden": "true" });
    const ys = (series?.y || []).map(Number).filter(Number.isFinite);
    if (ys.length < 2) return svg;
    const lo = Math.min(...ys), hi = Math.max(...ys), span = hi - lo || 1;
    const pts = ys.map((y, i) => `${(i / (ys.length - 1)) * 100},${28 - ((y - lo) / span) * 26}`);
    svg.append(mk("polyline", { points: pts.join(" "), fill: "none", stroke: color, "stroke-width": 1.6, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
    return svg;
  }

  // Drawn at the container's real pixel size so axis text stays legible; redrawn on resize.
  function lineChart(host, series) {
    host.replaceChildren();
    host._series = series;
    host.append(el("p", "chart__name", series.name));
    const W = Math.max(280, Math.round(host.clientWidth)), H = W < 600 ? 200 : 260;
    const L = 46, R = 10, T = 12, B = 26;
    const ys = series.y.map(Number);
    const xsRaw = series.x && series.x.length === ys.length ? series.x : ys.map((_, i) => i + 1);
    const numeric = xsRaw.every((x) => Number.isFinite(Number(x)));
    const xs = numeric ? xsRaw.map(Number) : xsRaw.map((_, i) => i);
    const xlo = Math.min(...xs), xhi = Math.max(...xs);
    let lo = Math.min(...ys), hi = Math.max(...ys);
    const pad = (hi - lo || Math.abs(hi) || 1) * 0.1; lo -= pad; hi += pad;
    const X = (x) => L + ((x - xlo) / (xhi - xlo || 1)) * (W - L - R);
    const Y = (y) => T + (1 - (y - lo) / (hi - lo)) * (H - T - B);
    const color = getComputedStyle(root).getPropertyValue("--chart").trim() || "#4caf32";
    const svg = mk("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H, role: "img", "aria-label": series.name });
    const id = `g${++gradSeq}`;
    const defs = mk("defs", {});
    const g = mk("linearGradient", { id, x1: 0, x2: 0, y1: 0, y2: 1 });
    g.append(mk("stop", { offset: "0%", "stop-color": color, "stop-opacity": 0.35 }), mk("stop", { offset: "100%", "stop-color": color, "stop-opacity": 0 }));
    defs.append(g); svg.append(defs);
    for (let i = 0; i <= 4; i++) {
      const v = lo + ((hi - lo) * i) / 4, y = Y(v);
      svg.append(mk("line", { x1: L, x2: W - R, y1: y, y2: y, class: "grid" }));
      const t = mk("text", { x: L - 8, y: y + 4, "text-anchor": "end" }); t.textContent = fmt(v); svg.append(t);
    }
    const ticks = [0, Math.floor((xs.length - 1) / 2), xs.length - 1];
    ticks.forEach((i, k) => {
      const t = mk("text", { x: X(xs[i]), y: H - 8, "text-anchor": k === 0 ? "start" : k === 2 ? "end" : "middle" });
      t.textContent = String(xsRaw[i]); svg.append(t);
    });
    const d = ys.map((y, i) => `${i ? "L" : "M"}${X(xs[i]).toFixed(1)},${Y(y).toFixed(1)}`).join("");
    svg.append(mk("path", { d: `${d}L${X(xs[xs.length - 1]).toFixed(1)},${H - B}L${X(xs[0]).toFixed(1)},${H - B}Z`, fill: `url(#${id})` }));
    svg.append(mk("path", { d, fill: "none", stroke: color, "stroke-width": 2.2, "vector-effect": "non-scaling-stroke", "stroke-linejoin": "round" }));
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

  // ---------- shared blocks ----------
  function kpiTile(m, i, series) {
    const c = el("article", "card kpi");
    c.append(el("div", "kpi__l", m.label));
    const v = el("div", "kpi__v", m.value);
    if (m.unit) v.append(el("small", null, m.unit));
    c.append(v);
    if (m.note) c.append(el("div", "kpi__n", m.note));
    if (series) { const s = el("div", "kpi__spark"); s.append(sparkline(series, COLORS[i % COLORS.length])); c.append(s); }
    return c;
  }
  function pillFor(text) {
    const p = el("span", "pill", text);
    if (/^\+|pass|green|ok/i.test(text)) p.classList.add("pill--up");
    else if (/^-|fail|miss/i.test(text)) p.classList.add("pill--down");
    return p;
  }
  function segControl(host, items, onPick, initial = 0) {
    host.replaceChildren();
    const btns = items.map((label, i) => {
      const b = el("button", null, label); b.type = "button"; b.setAttribute("role", "tab");
      b.setAttribute("aria-selected", String(i === initial));
      b.addEventListener("click", () => { btns.forEach((x) => x.setAttribute("aria-selected", "false")); b.setAttribute("aria-selected", "true"); onPick(i); });
      host.append(b); return b;
    });
    onPick(initial);
  }

  // ---------- views ----------
  function viewOverview() {
    const v = $("#t-overview").content.cloneNode(true);
    $("[data-big]", v).textContent = fmtInt(META.tests ?? 0);
    $("[data-big-pill]", v).textContent = "run in CI";
    $("[data-loc]", v).textContent = fmtInt(META.loc ?? 0);
    $("[data-count]", v).textContent = String(DATA.length);
    const chart = $("[data-chart]", v);
    const withSeries = DATA.filter((p) => p.series && p.series.y && p.series.y.length > 1);
    segControl($("[data-seg]", v), withSeries.map((p) => SYM[p.slug]), (i) => lineChart(chart, withSeries[i].series));

    const totalLoc = DATA.reduce((a, p) => a + p.loc, 0) || 1;
    const alloc = $("[data-alloc]", v), legend = $("[data-legend]", v);
    DATA.forEach((p, i) => {
      const s = el("span"); s.style.width = `${(p.loc / totalLoc) * 100}%`; s.style.background = COLORS[i]; s.title = `${p.title}: ${fmtInt(p.loc)} lines`; alloc.append(s);
      const li = el("li"); const sw = el("i"); sw.style.background = COLORS[i];
      li.append(sw, el("span", null, shortTitle(p)), el("span", "pct", `${((p.loc / totalLoc) * 100).toFixed(0)}%`), el("span", "val", fmtInt(p.loc)));
      legend.append(li);
    });
    const kv = $("[data-kv]", v);
    [["Test cases (pytest)", fmtInt(META.tests ?? 0)], ["Python versions in CI", "3.12 · 3.13 · 3.14"], ["External services needed", "none"], ["Money represented as", "integer cents"]]
      .forEach(([k, val]) => { const li = el("li"); li.append(el("span", null, k), el("b", null, val)); kv.append(li); });

    const kpis = $("[data-kpis]", v);
    DATA.forEach((p, i) => { const m = p.metrics[0]; if (m) kpis.append(kpiTile({ ...m, label: `${shortTitle(p)} · ${m.label}` }, i, p.series)); });

    $("[data-sys-count]", v).textContent = `${DATA.length} systems`;
    const tb = $("[data-table] tbody", v);
    DATA.forEach((p, i) => {
      const tr = el("tr"); tr.tabIndex = 0;
      const go = () => { location.hash = `#/${p.slug}`; };
      tr.addEventListener("click", go); tr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      const td1 = el("td"); const sys = el("div", "sys");
      sys.append(el("span", "sys__ic sys__ic--on", SYM[p.slug]));
      const txt = el("div", "txt"); txt.append(el("b", null, p.title), el("small", null, p.tagline)); sys.append(txt); td1.append(sys);
      const td2 = el("td", "mono hide-sm", p.lane);
      const m = p.metrics[0]; const td3 = el("td"); td3.append(pillFor(`${m.value}${m.unit ? " " + m.unit : ""}`), document.createTextNode(" "), el("span", "dim", m.label));
      const td4 = el("td", "r mono", String(p.tests));
      const td5 = el("td", "r hide-sm"); const sp = el("div", "spark"); sp.append(sparkline(p.series, COLORS[i])); td5.append(sp);
      tr.append(td1, td2, td3, td4, td5); tb.append(tr);
    });

    const act = $("[data-activity]", v);
    (META.commits || []).slice(0, 8).forEach((c) => {
      const li = el("li");
      li.append(el("span", "ic", c.sha.slice(0, 5)), el("span", "msg", c.msg), el("span", "when", c.date));
      act.append(li);
    });
    if (!(META.commits || []).length) act.append(el("li", "empty", "No commits yet."));
    return v;
  }

  function viewProject(p) {
    const v = $("#t-project").content.cloneNode(true);
    const m0 = p.metrics[0] || { label: "", value: "—" };
    $("[data-lane]", v).textContent = p.lane;
    $("[data-big]", v).textContent = m0.value;
    $("[data-big-unit]", v).textContent = m0.unit || "";
    $("[data-big-pill]", v).textContent = m0.label;
    $("[data-big-note]", v).textContent = m0.note || "";
    $("[data-title]", v).textContent = p.title;
    $("[data-tagline]", v).textContent = p.tagline;
    $("[data-src]", v).href = `${REPO}/tree/main/${p.folder}`;
    $("[data-report-link]", v).href = `${REPO}/blob/main/${p.folder}/results/REPORT.md`;
    p.stack.forEach((s) => $("[data-stack]", v).append(el("span", "chip", s)));
    const facts = $("[data-facts]", v);
    [["Tests", fmtInt(p.tests)], ["Lines of Python", fmtInt(p.loc)], ["Run", `python -m ${p.run || "…"}`], ["Folder", p.folder + "/"]]
      .forEach(([k, val]) => { const li = el("li"); li.append(el("span", null, k), el("b", null, val)); facts.append(li); });

    const chart = $("[data-chart]", v), seg = $("[data-seg]", v);
    const all = [p.series, ...(p.extra_series || [])].filter((s) => s && s.y && s.y.length > 1);
    if (all.length) segControl(seg, all.map((s) => s.short || s.name.split(" ").slice(0, 2).join(" ")), (i) => lineChart(chart, all[i]));
    else { chart.append(el("p", "empty", "No series for this project.")); seg.remove(); }

    const kpis = $("[data-kpis]", v);
    p.metrics.slice(1).forEach((m, i) => kpis.append(kpiTile(m, i + 1)));
    p.highlights.forEach((h) => $("[data-highlights]", v).append(el("li", null, h)));
    p.limitations.forEach((h) => $("[data-limits]", v).append(el("li", null, h)));
    const rep = $("[data-report]", v);
    // Reports are generated by this repo's own run scripts, not user input.
    if (window.marked && p.report) rep.innerHTML = window.marked.parse(p.report);
    else rep.append(el("pre", null, p.report || "No report."));
    return v;
  }

  const COVER = [
    ["Double-entry accounting, integer money", ["ledger"]],
    ["Idempotent APIs and safe retries", ["ledger"]],
    ["Concurrency and transactional integrity", ["ledger", "exchange"]],
    ["Reconciliation and break management", ["ledger"]],
    ["Real-time scoring with p99 latency budgets", ["fraud", "exchange"]],
    ["Point-in-time features, no leakage", ["fraud", "credit"]],
    ["Cost-sensitive decisions in dollars", ["fraud", "credit"]],
    ["Drift monitoring (PSI) and alerting", ["fraud", "credit"]],
    ["Model risk documentation, model card", ["credit"]],
    ["Fair lending and adverse-action reasons", ["credit"]],
    ["Low-latency data structures and benchmarks", ["exchange"]],
    ["Backtesting with fees and slippage", ["exchange"]],
    ["BSA/AML typologies and SAR narratives", ["aml"]],
    ["Graph analytics on transaction networks", ["aml"]],
    ["Tamper-evident audit trails", ["aml", "ledger"]],
    ["Property-based testing (Hypothesis)", ["ledger", "exchange"]],
    ["FastAPI services", ["ledger", "fraud"]],
    ["CI on every push, three Python versions", DATA.map((p) => p.slug)],
  ];
  function viewCoverage() {
    const v = $("#t-coverage").content.cloneNode(true);
    const t = $("[data-matrix]", v);
    const thead = el("thead"), hr = el("tr"); hr.append(el("th", null, "Requirement"));
    DATA.forEach((p) => hr.append(el("th", null, SYM[p.slug]))); thead.append(hr);
    const tb = el("tbody");
    COVER.forEach(([req, has]) => {
      const tr = el("tr"); tr.append(el("td", null, req));
      DATA.forEach((p) => {
        const on = has.includes(p.slug); const td = el("td", on ? "on" : "off");
        if (on) { const s = mk("svg", { viewBox: "0 0 20 20", "aria-label": "yes" }); s.append(mk("path", { d: "M4 10l4 4 8-8", fill: "none", stroke: "currentColor", "stroke-width": 2.4, "stroke-linecap": "round", "stroke-linejoin": "round" })); td.append(s); }
        else td.textContent = "–";
        tr.append(td);
      });
      tb.append(tr);
    });
    t.append(thead, tb);
    return v;
  }

  function redrawCharts() {
    document.querySelectorAll(".chart").forEach((h) => { if (h._series) lineChart(h, h._series); });
  }

  // ---------- router ----------
  const main = $("#main"), title = $("#top-title");
  function render() {
    const route = (location.hash.replace(/^#/, "") || "/");
    document.querySelectorAll(".nav__item[data-route]").forEach((a) => {
      if (a.dataset.route === route) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current");
    });
    const p = DATA.find((x) => `/${x.slug}` === route);
    let frag, t;
    if (!DATA.length) { frag = el("p", "empty", "No data yet — run each project, then python tools/build_site.py."); t = "Dashboard"; }
    else if (route === "/coverage") { frag = viewCoverage(); t = "JD coverage"; }
    else if (p) { frag = viewProject(p); t = p.title; }
    else { frag = viewOverview(); t = "Dashboard"; }
    main.replaceChildren(frag); title.textContent = t; document.title = `${t} — Ryan Wien`;
    redrawCharts(); // views are built detached, so charts only know their width once mounted
    window.scrollTo({ top: 0 });
  }
  window.addEventListener("hashchange", render);
  let resizeTimer = 0;
  window.addEventListener("resize", () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(redrawCharts, 150);
  });
  render();

  // ---------- search ----------
  $("#search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) return;
    const hit = DATA.find((p) => [p.title, p.tagline, p.lane, ...p.stack, ...p.highlights, ...p.metrics.map((m) => m.label)].join(" ").toLowerCase().includes(q));
    if (hit && location.hash !== `#/${hit.slug}`) location.hash = `#/${hit.slug}`;
  });
})();
