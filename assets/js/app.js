/* The dashboard application: theme, navigation, the three views (overview,
   project, JD coverage) and the hash router. Depends on util.js (UI),
   catalog.js (CATALOG), charts.js (Charts) and the generated data.js. */
(() => {
  "use strict";
  const { $, el, mk, fmtInt } = window.UI;
  const { REPO, SYM, NAMES, PLAIN, COLORS, COVER } = window.CATALOG;
  const { sparkline, lineChart, redrawAll } = window.Charts;
  const DATA = window.PORTFOLIO || [];
  const META = window.PORTFOLIO_META || {};

  const shortTitle = (p) => NAMES[p.slug] || p.title;

  // ---------- theme (persisted per visitor) ----------
  const rootEl = document.documentElement;
  const applyTheme = (t) => {
    rootEl.dataset.theme = t;
    $("#theme .theme__txt").textContent = t === "dark" ? "Dark mode" : "Light mode";
  };
  let savedTheme = null;
  try { savedTheme = localStorage.getItem("theme"); } catch (_) { /* private mode */ }
  applyTheme(savedTheme || "dark");
  $("#theme").addEventListener("click", () => {
    const t = rootEl.dataset.theme === "dark" ? "light" : "dark";
    applyTheme(t);
    try { localStorage.setItem("theme", t); } catch (_) { /* ignore */ }
  });

  // ---------- mobile nav drawer ----------
  const side = $("#side"), scrim = $("#scrim"), burger = $("#burger");
  const closeNav = () => { side.classList.remove("open"); scrim.hidden = true; burger.setAttribute("aria-expanded", "false"); };
  burger.addEventListener("click", () => {
    const open = !side.classList.contains("open");
    side.classList.toggle("open", open); scrim.hidden = !open; burger.setAttribute("aria-expanded", String(open));
  });
  scrim.addEventListener("click", closeNav);

  // ---------- sidebar: one link per system ----------
  const navHost = $("#nav-projects");
  DATA.forEach((p) => {
    const a = el("a", "nav__item");
    a.href = `#/${p.slug}`; a.dataset.route = `/${p.slug}`;
    a.append(el("span", "nav__sym", SYM[p.slug] || p.slug.slice(0, 3).toUpperCase()), document.createTextNode(shortTitle(p)));
    a.append(el("span", "nav__v", `${p.tests}t`));
    a.addEventListener("click", closeNav);
    navHost.append(a);
  });

  // ---------- small reusable blocks ----------
  /** KPI tile: label, big value with unit, optional note and sparkline. */
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

  /** Colored pill: green for gains/passes, red for losses/failures. */
  function pillFor(text) {
    const p = el("span", "pill", text);
    if (/^\+|pass|green|ok/i.test(text)) p.classList.add("pill--up");
    else if (/^-|fail|miss/i.test(text)) p.classList.add("pill--down");
    return p;
  }

  /** Segmented tab control; calls onPick(index) on selection and once initially. */
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

  /** The ▶ button that opens a system's interactive demo (defined in demos.js). */
  function demoButton(p, cls, label) {
    const b = el("button", cls, label);
    b.type = "button";
    b.setAttribute("aria-label", `Open the ${shortTitle(p)} live demo`);
    b.addEventListener("click", (e) => { e.stopPropagation(); window.openDemo?.(p.slug); });
    return b;
  }

  // ---------- view: overview dashboard ----------
  function viewOverview() {
    const v = $("#t-overview").content.cloneNode(true);

    // hero: headline number, copy, chart with one tab per system
    $("[data-big]", v).textContent = fmtInt(META.tests ?? 0);
    $("[data-big-pill]", v).textContent = "all passing";
    $("[data-loc]", v).textContent = fmtInt(META.loc ?? 0);
    $("[data-count]", v).textContent = String(DATA.length);
    const chart = $("[data-chart]", v);
    const withSeries = DATA.filter((p) => p.series && p.series.y && p.series.y.length > 1);
    segControl($("[data-seg]", v), withSeries.map((p) => SYM[p.slug]), (i) => lineChart(chart, withSeries[i].series));

    // allocation bar + legend, by lines of code
    const totalLoc = DATA.reduce((a, p) => a + p.loc, 0) || 1;
    const alloc = $("[data-alloc]", v), legend = $("[data-legend]", v);
    DATA.forEach((p, i) => {
      const s = el("span"); s.style.width = `${(p.loc / totalLoc) * 100}%`; s.style.background = COLORS[i]; s.title = `${p.title}: ${fmtInt(p.loc)} lines`; alloc.append(s);
      const li = el("li"); const sw = el("i"); sw.style.background = COLORS[i];
      li.append(sw, el("span", null, shortTitle(p)), el("span", "pct", `${((p.loc / totalLoc) * 100).toFixed(0)}%`), el("span", "val", fmtInt(p.loc)));
      legend.append(li);
    });

    // coverage facts
    const kv = $("[data-kv]", v);
    [["Test cases (pytest)", fmtInt(META.tests ?? 0)], ["Python versions in CI", "3.12 · 3.13 · 3.14"], ["External services needed", "none"], ["Money represented as", "integer cents"]]
      .forEach(([k, val]) => { const li = el("li"); li.append(el("span", null, k), el("b", null, val)); kv.append(li); });

    // one headline KPI tile per system
    const kpis = $("[data-kpis]", v);
    DATA.forEach((p, i) => { const m = p.metrics[0]; if (m) kpis.append(kpiTile({ ...m, label: `${shortTitle(p)} · ${m.label}` }, i, p.series)); });

    // systems table: plain-English description, headline, tests, trend, demo button
    $("[data-sys-count]", v).textContent = `${DATA.length} systems`;
    const tb = $("[data-table] tbody", v);
    DATA.forEach((p, i) => {
      const tr = el("tr"); tr.tabIndex = 0;
      const go = () => { location.hash = `#/${p.slug}`; };
      tr.addEventListener("click", go); tr.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
      const td1 = el("td"); const sys = el("div", "sys");
      sys.append(el("span", "sys__ic sys__ic--on", SYM[p.slug]));
      const txt = el("div", "txt"); txt.append(el("b", null, p.title), el("small", null, PLAIN[p.slug] || p.tagline)); sys.append(txt); td1.append(sys);
      const td2 = el("td", "mono hide-sm", p.lane);
      const m = p.metrics[0]; const td3 = el("td"); td3.append(pillFor(`${m.value}${m.unit ? " " + m.unit : ""}`), document.createTextNode(" "), el("span", "dim", m.label));
      const td4 = el("td", "r mono", String(p.tests));
      const td5 = el("td", "r hide-sm"); const sp = el("div", "spark"); sp.append(sparkline(p.series, COLORS[i])); td5.append(sp);
      const td6 = el("td", "r"); td6.append(demoButton(p, "dbtn dbtn--go", "▶ Demo"));
      tr.append(td1, td2, td3, td4, td5, td6); tb.append(tr);
    });

    // recent git activity
    const act = $("[data-activity]", v);
    (META.commits || []).slice(0, 8).forEach((c) => {
      const li = el("li");
      li.append(el("span", "ic", c.sha.slice(0, 5)), el("span", "msg", c.msg), el("span", "when", c.date));
      act.append(li);
    });
    if (!(META.commits || []).length) act.append(el("li", "empty", "No commits yet."));
    return v;
  }

  // ---------- view: one project ----------
  function viewProject(p) {
    const v = $("#t-project").content.cloneNode(true);

    // hero: lane, headline metric, chart tabs
    const m0 = p.metrics[0] || { label: "", value: "—" };
    $("[data-lane]", v).textContent = p.lane;
    $("[data-big]", v).textContent = m0.value;
    $("[data-big-unit]", v).textContent = m0.unit || "";
    $("[data-big-pill]", v).textContent = m0.label;
    $("[data-big-note]", v).textContent = m0.note || "";
    const chart = $("[data-chart]", v), seg = $("[data-seg]", v);
    const all = [p.series, ...(p.extra_series || [])].filter((s) => s && s.y && s.y.length > 1);
    if (all.length) segControl(seg, all.map((s) => s.short || s.name.split(" ").slice(0, 2).join(" ")), (i) => lineChart(chart, all[i]));
    else { chart.append(el("p", "empty", "No series for this project.")); seg.remove(); }

    // about card: plain-English line, title, tagline, stack, actions, facts
    $("[data-plain]", v).textContent = PLAIN[p.slug] || "";
    $("[data-title]", v).textContent = p.title;
    $("[data-tagline]", v).textContent = p.tagline;
    p.stack.forEach((s) => $("[data-stack]", v).append(el("span", "chip", s)));
    $("[data-demo]", v).addEventListener("click", () => window.openDemo?.(p.slug));
    $("[data-src]", v).href = `${REPO}/tree/main/${p.folder}`;
    $("[data-report-link]", v).href = `${REPO}/blob/main/${p.folder}/results/REPORT.md`;
    const facts = $("[data-facts]", v);
    [["Tests", fmtInt(p.tests)], ["Lines of Python", fmtInt(p.loc)], ["Run", `python -m ${p.run || "…"}`], ["Folder", p.folder + "/"]]
      .forEach(([k, val]) => { const li = el("li"); li.append(el("span", null, k), el("b", null, val)); facts.append(li); });

    // remaining metrics, highlights, limitations, generated run report
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

  // ---------- view: JD coverage matrix ----------
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

  // ---------- hash router: "#/" overview, "#/<slug>" project, "#/coverage" matrix ----------
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
    title.title = t; // full title on hover when the header truncates it
    redrawAll(); // views are built detached, so charts only know their width once mounted
    window.scrollTo({ top: 0 });
  }
  window.addEventListener("hashchange", render);
  render();

  // ---------- search: jump to the first system matching the query ----------
  $("#search").addEventListener("input", (e) => {
    const q = e.target.value.trim().toLowerCase();
    if (!q) return;
    const hit = DATA.find((p) => [p.title, p.tagline, p.lane, ...p.stack, ...p.highlights, ...p.metrics.map((m) => m.label)].join(" ").toLowerCase().includes(q));
    if (hit && location.hash !== `#/${hit.slug}`) location.hash = `#/${hit.slug}`;
  });
})();
