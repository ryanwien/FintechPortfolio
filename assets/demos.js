/* Interactive in-browser demos, one per system. Each is a small model of the real
   project's core mechanism so a visitor can poke it; every published metric still
   comes from the Python runs in each project's results/. */
(() => {
  "use strict";
  const $ = (sel, root = document) => root.querySelector(sel);
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };
  const usd = (cents) => (cents / 100).toLocaleString(undefined, { style: "currency", currency: "USD" });

  // ---------- sound effects ----------
  // All cues are synthesized live with the Web Audio API — no audio files, nothing
  // licensed, nothing to attribute. They only ever play in response to a click.
  let audioCtx = null, muted = false;
  try { muted = localStorage.getItem("demo-muted") === "1"; } catch (_) { /* private mode */ }
  function tone(freq, ms, { type = "sine", gain = 0.25, at = 0, slide = 0 } = {}) {
    if (muted) return;
    try {
      audioCtx ||= new (window.AudioContext || window.webkitAudioContext)();
      // Browsers hand out suspended contexts outside (or sometimes even inside) a
      // gesture; resuming on every cue keeps the first click audible.
      if (audioCtx.state === "suspended") audioCtx.resume();
      const t0 = audioCtx.currentTime + at;
      const osc = audioCtx.createOscillator();
      const g = audioCtx.createGain();
      osc.type = type;
      osc.frequency.setValueAtTime(freq, t0);
      if (slide) osc.frequency.exponentialRampToValueAtTime(Math.max(40, freq + slide), t0 + ms / 1000);
      g.gain.setValueAtTime(0.0001, t0);
      g.gain.exponentialRampToValueAtTime(gain, t0 + 0.012);
      g.gain.exponentialRampToValueAtTime(0.0001, t0 + ms / 1000);
      osc.connect(g).connect(audioCtx.destination);
      osc.start(t0);
      osc.stop(t0 + ms / 1000 + 0.02);
    } catch (_) { /* no audio device — demos work silently */ }
  }
  const sfx = {
    open: () => tone(520, 120, { type: "triangle", gain: 0.18 }),
    good: () => { tone(660, 120, { type: "triangle" }); tone(990, 170, { type: "triangle", at: 0.08 }); },
    bad: () => tone(200, 260, { type: "square", gain: 0.16, slide: -70 }),
    tick: () => tone(1250, 55, { type: "square", gain: 0.1 }),
    warn: () => tone(430, 200, { type: "sawtooth", gain: 0.14, slide: -60 }),
    alarm: () => { tone(880, 150, { type: "square", gain: 0.16 }); tone(880, 150, { type: "square", gain: 0.16, at: 0.2 }); },
  };
  function muteButton() {
    const b = el("button", "demo__close demo__mute", muted ? "🔇" : "🔊");
    b.type = "button";
    b.setAttribute("aria-label", muted ? "Unmute demo sounds" : "Mute demo sounds");
    b.setAttribute("aria-pressed", String(muted));
    b.addEventListener("click", () => {
      muted = !muted;
      try { localStorage.setItem("demo-muted", muted ? "1" : "0"); } catch (_) { /* ignore */ }
      b.textContent = muted ? "🔇" : "🔊";
      b.setAttribute("aria-pressed", String(muted));
      b.setAttribute("aria-label", muted ? "Unmute demo sounds" : "Mute demo sounds");
      if (!muted) sfx.open();
    });
    return b;
  }

  // ---------- modal plumbing ----------
  let overlay = null, lastFocus = null, teardown = null;
  function closeDemo() {
    if (teardown) { teardown(); teardown = null; }
    overlay?.remove(); overlay = null;
    document.body.style.overflow = "";
    lastFocus?.focus?.();
  }
  function openDemo(slug) {
    const build = DEMOS[slug];
    if (!build) return;
    closeDemo();
    lastFocus = document.activeElement;
    overlay = el("div", "demo-overlay");
    overlay.addEventListener("click", (e) => { if (e.target === overlay) closeDemo(); });
    const box = el("div", "demo");
    box.setAttribute("role", "dialog");
    box.setAttribute("aria-modal", "true");
    box.setAttribute("aria-label", `${build.title} demo`);
    const head = el("div", "demo__head");
    const ttl = el("div");
    ttl.append(el("h2", "demo__title", build.title), el("p", "demo__sub", build.sub));
    const x = el("button", "demo__close", "✕");
    x.setAttribute("aria-label", "Close demo");
    x.addEventListener("click", closeDemo);
    head.append(ttl, muteButton(), x);
    const body = el("div", "demo__body");
    box.append(head, body, el("p", "demo__note",
      "Interactive model running in your browser to show the mechanism. The metrics on the cards come from the real Python system in this repo."));
    overlay.append(box);
    document.body.append(overlay);
    document.body.style.overflow = "hidden";
    teardown = build.mount(body) || null;
    sfx.open();
    x.focus();
  }
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && overlay) closeDemo(); });
  window.openDemo = openDemo;

  // ---------- shared widgets ----------
  function btn(label, cls, fn) {
    const b = el("button", `dbtn${cls ? " " + cls : ""}`, label);
    b.type = "button";
    b.addEventListener("click", fn);
    return b;
  }
  function logPane(ariaLabel) {
    const pane = el("div", "dlog");
    pane.setAttribute("role", "log");
    pane.setAttribute("aria-label", ariaLabel);
    const add = (text, cls) => {
      pane.append(el("div", `dlog__line${cls ? " " + cls : ""}`, text));
      pane.scrollTop = pane.scrollHeight;
    };
    return [pane, add];
  }
  function slider(label, min, max, value, step, fmt) {
    const wrap = el("label", "dctl");
    const out = el("b", null, fmt(value));
    const top = el("span", "dctl__l");
    top.append(document.createTextNode(label + " "), out);
    const inp = document.createElement("input");
    Object.assign(inp, { type: "range", min, max, value, step });
    wrap.append(top, inp);
    return { wrap, inp, out, get: () => Number(inp.value), fmt };
  }
  const mulberry = (seed) => () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };

  const DEMOS = {};

  // ---------- 01 · ledger ----------
  DEMOS.ledger = {
    title: "Double-entry ledger",
    sub: "Every move is a balanced journal entry. Retries replay, overdrafts bounce, and the books always sum to zero.",
    mount(body) {
      const wallets = { "Wallet A": 5000, "Wallet B": 2000 }; // cents
      const seen = new Map(); // idempotency key -> response line
      let seq = 0, lastKey = null, lastArgs = null;
      const bal = el("div", "dgrid2");
      const cards = {};
      for (const w of Object.keys(wallets)) {
        const c = el("div", "dstat");
        c.append(el("span", "dstat__l", w), cards[w] = el("b", "dstat__v", usd(wallets[w])));
        bal.append(c);
      }
      const [pane, add] = logPane("Journal");
      const trial = el("div", "dtrial");
      const paint = () => {
        for (const w of Object.keys(wallets)) cards[w].textContent = usd(wallets[w]);
        trial.textContent = `trial balance: debits − credits = ${usd(0)} ✓ · money in system: ${usd(wallets["Wallet A"] + wallets["Wallet B"])}`;
      };
      function transfer(amount, key) {
        if (seen.has(key)) { sfx.tick(); add(`↻ ${key} replayed — stored response returned, nothing posted`, "is-dim"); return; }
        if (wallets["Wallet A"] < amount) {
          seen.set(key, "rejected");
          sfx.bad();
          add(`✗ ${key} rejected: insufficient_funds (Wallet A has ${usd(wallets["Wallet A"])}, needs ${usd(amount)})`, "is-bad");
          return;
        }
        wallets["Wallet A"] -= amount; wallets["Wallet B"] += amount;
        seen.set(key, "posted");
        sfx.good();
        add(`✓ entry ${++seq} posted — DR Wallet A ${usd(amount)} · CR Wallet B ${usd(amount)}  [${key}]`, "is-good");
        paint();
      }
      const amt = slider("Amount", 100, 9000, 1500, 100, (v) => usd(v));
      amt.inp.addEventListener("input", () => { amt.out.textContent = usd(amt.get()); });
      const row = el("div", "dbtns");
      row.append(
        btn("Send A → B", "dbtn--go", () => {
          lastKey = `req-${(Math.random() * 1e6 | 0).toString(36)}`; lastArgs = amt.get();
          transfer(lastArgs, lastKey);
        }),
        btn("Retry the same request", null, () => {
          if (!lastKey) { add("send something first, then retry it", "is-dim"); return; }
          transfer(lastArgs, lastKey);
        }),
        btn("Try to overdraw ($999)", null, () => transfer(99900, `od-${++seq}-${(Math.random() * 1e4 | 0)}`)),
      );
      body.append(bal, amt.wrap, row, pane, trial);
      add("ledger ready — two wallets funded by an opening entry");
      paint();
    },
  };

  // ---------- 02 · fraud ----------
  DEMOS.fraud = {
    title: "Fraud scorer",
    sub: "Slide a transaction toward risk and watch the score, the decision band and the reason codes move.",
    mount(body) {
      const ctls = [
        slider("Amount", 1, 3000, 60, 1, (v) => usd(v * 100)),
        slider("Distance from home", 0, 9000, 5, 5, (v) => `${v} km`),
        slider("Transactions in the last hour", 0, 12, 1, 1, (v) => `${v}`),
        slider("Local time", 0, 23, 14, 1, (v) => `${String(v).padStart(2, "0")}:00`),
      ];
      const newDev = el("label", "dchk");
      const devInp = document.createElement("input"); devInp.type = "checkbox";
      newDev.append(devInp, document.createTextNode(" First time seeing this device"));
      const gauge = el("div", "dgauge");
      const fill = el("i"); gauge.append(fill);
      const verdict = el("div", "dverdict");
      const reasons = el("ul", "dreasons");
      const FEATURES = [
        ["high amount vs typical spend", () => Math.max(0, Math.log10(ctls[0].get() / 40)) * 1.6],
        ["far from home geography", () => Math.min(1, ctls[1].get() / 2500) * 2.2],
        ["card-testing velocity", () => Math.max(0, ctls[2].get() - 2) * 0.55],
        ["unusual hour", () => { const h = ctls[3].get(); return (h >= 1 && h <= 5 ? 1 : 0) * 1.1; }],
        ["new device", () => (devInp.checked ? 1.4 : 0)],
      ];
      function score() {
        const parts = FEATURES.map(([name, f]) => [name, f()]).filter(([, v]) => v > 0.01);
        const z = parts.reduce((a, [, v]) => a + v, 0) - 3.2;
        const p = 1 / (1 + Math.exp(-z));
        fill.style.width = `${(p * 100).toFixed(1)}%`;
        fill.className = p < 0.18 ? "ok" : p < 0.58 ? "warn" : "bad";
        const d = p < 0.18 ? ["APPROVE", "ok"] : p < 0.58 ? ["REVIEW — route to an analyst", "warn"] : ["DECLINE", "bad"];
        if (verdict.dataset.band && verdict.dataset.band !== d[1]) ({ ok: sfx.good, warn: sfx.warn, bad: sfx.bad })[d[1]]();
        verdict.dataset.band = d[1];
        verdict.textContent = `score ${p.toFixed(3)} → ${d[0]}`;
        verdict.className = `dverdict is-${d[1]}`;
        reasons.replaceChildren(...(p < 0.18
          ? [el("li", "is-dim", "no adverse reason codes on approvals")]
          : parts.sort((a, b) => b[1] - a[1]).slice(0, 3).map(([name, v]) => el("li", null, `${name} (+${v.toFixed(2)})`))));
      }
      ctls.forEach((c) => c.inp.addEventListener("input", () => { c.out.textContent = c.fmt(c.get()); score(); }));
      devInp.addEventListener("change", score);
      const presets = el("div", "dbtns");
      const set = (a, d, v, h, nd) => { [a, d, v, h].forEach((val, i) => { ctls[i].inp.value = val; ctls[i].out.textContent = ctls[i].fmt(val); }); devInp.checked = nd; score(); };
      presets.append(
        btn("Normal coffee run", null, () => set(6, 2, 1, 9, false)),
        btn("Account takeover", "dbtn--go", () => set(1400, 7400, 3, 3, true)),
        btn("Card-testing burst", null, () => set(2, 10, 11, 2, true)),
      );
      body.append(...ctls.map((c) => c.wrap), newDev, presets, gauge, verdict, reasons);
      score();
    },
  };

  // ---------- 03 · credit ----------
  DEMOS.credit = {
    title: "Credit scorecard",
    sub: "A points-based scorecard: every factor adds or removes points, and a decline lists exactly why — the Reg B adverse-action reasons.",
    mount(body) {
      const BINS = {
        "Debt-to-income": { ctl: slider("Debt-to-income", 0, 65, 28, 1, (v) => `${v}%`), bins: [[20, 42], [35, 18], [50, -8], [Infinity, -30]], reason: "Debt-to-income ratio too high" },
        "Card utilization": { ctl: slider("Card utilization", 0, 100, 30, 1, (v) => `${v}%`), bins: [[10, 38], [30, 22], [60, -6], [90, -26], [Infinity, -44]], reason: "Proportion of revolving balances to credit limits too high" },
        "Late payments (24 mo)": { ctl: slider("Late payments (24 mo)", 0, 8, 0, 1, (v) => `${v}`), bins: [[0, 34], [1, 2], [2, -24], [Infinity, -52]], reason: "Delinquency on accounts" },
        "Credit history": { ctl: slider("Credit history", 0, 30, 7, 1, (v) => `${v} yrs`), bins: [[2, -28], [5, -8], [12, 14], [Infinity, 30]], reason: "Length of credit history insufficient" },
        "Hard inquiries (12 mo)": { ctl: slider("Hard inquiries (12 mo)", 0, 10, 1, 1, (v) => `${v}`), bins: [[0, 16], [2, 6], [4, -12], [Infinity, -28]], reason: "Too many recent credit inquiries" },
      };
      const CUTOFF = 555, BASE = 540;
      const tbl = el("table", "dtable");
      tbl.innerHTML = "<thead><tr><th>Factor</th><th>Value</th><th>Points</th></tr></thead>";
      const tb = el("tbody"); tbl.append(tb);
      const verdict = el("div", "dverdict");
      const reasons = el("ul", "dreasons");
      function scoreIt() {
        tb.replaceChildren();
        let total = BASE;
        const rows = [];
        for (const [name, cfg] of Object.entries(BINS)) {
          const v = cfg.ctl.get();
          const pts = cfg.bins.find(([edge]) => v <= edge)[1];
          const best = Math.max(...cfg.bins.map(([, p]) => p));
          total += pts;
          rows.push({ name, lost: best - pts, reason: cfg.reason });
          const tr = el("tr");
          tr.append(el("td", null, name), el("td", "num", cfg.ctl.fmt(v)), el("td", `num ${pts >= 0 ? "is-good" : "is-bad"}`, `${pts >= 0 ? "+" : ""}${pts}`));
          tb.append(tr);
        }
        const foot = el("tr", "dtable__total");
        foot.append(el("td", null, "Score (base 540)"), el("td"), el("td", "num", String(total)));
        tb.append(foot);
        const band = total >= CUTOFF ? "ok" : "bad";
        if (verdict.dataset.band && verdict.dataset.band !== band) (band === "ok" ? sfx.good : sfx.bad)();
        verdict.dataset.band = band;
        if (total >= CUTOFF) {
          verdict.textContent = `${total} ≥ cutoff ${CUTOFF} → APPROVED`;
          verdict.className = "dverdict is-ok";
          reasons.replaceChildren(el("li", "is-dim", "approvals carry no adverse-action notice"));
        } else {
          verdict.textContent = `${total} < cutoff ${CUTOFF} → DECLINED — principal reasons:`;
          verdict.className = "dverdict is-bad";
          reasons.replaceChildren(...rows.sort((a, b) => b.lost - a.lost).slice(0, 4)
            .map((r, i) => el("li", null, `${i + 1}. ${r.reason} (−${r.lost} pts vs best)`)));
        }
      }
      for (const cfg of Object.values(BINS)) {
        cfg.ctl.inp.addEventListener("input", () => { cfg.ctl.out.textContent = cfg.ctl.fmt(cfg.ctl.get()); scoreIt(); });
        body.append(cfg.ctl.wrap);
      }
      body.append(tbl, verdict, reasons);
      scoreIt();
    },
  };

  // ---------- 04 · exchange ----------
  DEMOS.exchange = {
    title: "Matching engine",
    sub: "A live limit order book with price-time priority. Fire orders at it and watch fills sweep the levels.",
    mount(body) {
      const rnd = mulberry(42);
      const TICK = 1; // cents
      const book = { bid: new Map(), ask: new Map() };
      let mid = 10000, lastPx = null, lastUp = null, fills = 0, traded = 0;
      for (let k = 1; k <= 10; k++) {
        book.bid.set(mid - k * TICK, 100 + ((rnd() * 300) | 0));
        book.ask.set(mid + k * TICK, 100 + ((rnd() * 300) | 0));
      }
      const best = (s) => (s === "bid" ? Math.max(...book.bid.keys()) : Math.min(...book.ask.keys()));
      const host = el("div", "dbook");
      const asksEl = el("div", "dbook__side"), spreadEl = el("div", "dbook__mid"), bidsEl = el("div", "dbook__side");
      host.append(asksEl, spreadEl, bidsEl);
      const [pane, add] = logPane("Executions");
      const stats = el("div", "dtrial");
      function render() {
        const paint = (hostEl, side) => {
          const ks = [...book[side].keys()].sort((a, b) => (side === "bid" ? b - a : a - b)).slice(0, 6);
          const rows = side === "ask" ? ks.reverse() : ks;
          const mx = Math.max(...ks.map((k) => book[side].get(k)), 1);
          hostEl.replaceChildren(...rows.map((px) => {
            const q = book[side].get(px);
            const r = el("div", `dlvl dlvl--${side}`);
            const bar = el("i"); bar.style.width = `${(q / mx) * 100}%`;
            r.append(bar, el("span", null, usd(px)), el("span", "num", q.toLocaleString()));
            return r;
          }));
        };
        paint(asksEl, "ask"); paint(bidsEl, "bid");
        const bb = best("bid"), ba = best("ask");
        spreadEl.textContent = `spread ${usd(ba - bb)}${lastPx ? ` · last ${usd(lastPx)} ${lastUp ? "▲" : "▼"}` : ""}`;
        stats.textContent = `${fills} fills · ${traded.toLocaleString()} shares traded · price-time priority, FIFO within each level`;
      }
      function market(buy, qty) {
        const side = buy ? "ask" : "bid";
        let left = qty;
        while (left > 0 && book[side].size > 2) {
          const px = best(side), avail = book[side].get(px), take = Math.min(avail, left);
          left -= take; fills++; traded += take; lastPx = px; lastUp = buy;
          sfx.tick();
          add(`fill ${take} @ ${usd(px)} (${buy ? "buy" : "sell"} aggressor${take === avail ? ", level cleared" : ""})`, buy ? "is-good" : "is-bad");
          if (take === avail) book[side].delete(px); else book[side].set(px, avail - take);
        }
        replenish(); render();
      }
      function passive() {
        const side = rnd() < 0.5 ? "bid" : "ask";
        const px = side === "bid" ? best("bid") + (rnd() < 0.3 ? TICK : 0) : best("ask") - (rnd() < 0.3 ? TICK : 0);
        const safe = side === "bid" ? Math.min(px, best("ask") - TICK) : Math.max(px, best("bid") + TICK);
        book[side].set(safe, (book[side].get(safe) || 0) + 50 + ((rnd() * 250) | 0));
        add(`passive ${side} ${usd(safe)} joins the queue (no trade — post, don't cross)`, "is-dim");
        render();
      }
      function replenish() {
        for (const s of ["bid", "ask"]) {
          while (book[s].size < 8) {
            const edge = s === "bid" ? Math.min(...book[s].keys()) - TICK : Math.max(...book[s].keys()) + TICK;
            book[s].set(edge, 100 + ((rnd() * 300) | 0));
          }
        }
      }
      const row = el("div", "dbtns");
      row.append(
        btn("Market BUY 300", "dbtn--go", () => market(true, 300)),
        btn("Market SELL 300", null, () => market(false, 300)),
        btn("Post a passive order", null, passive),
      );
      body.append(host, row, pane, stats);
      add("book seeded · deterministic RNG, same session every reload");
      render();
      const timer = setInterval(() => { rnd() < 0.55 ? passive() : market(rnd() < 0.5, 60 + ((rnd() * 200) | 0)); }, 1800);
      return () => clearInterval(timer);
    },
  };

  // ---------- 05 · aml ----------
  DEMOS.aml = {
    title: "AML monitor",
    sub: "Inject activity, run the rules, and try to tamper with the audit log — the hash chain catches it.",
    mount(body) {
      const txns = [];
      const audit = [];
      let chain = 7;
      const hash = (s) => { for (const ch of s) chain = (Math.imul(chain, 31) + ch.charCodeAt(0)) >>> 0; return chain.toString(16).padStart(8, "0").slice(-8); };
      const [tPane, tAdd] = logPane("Transactions");
      const [aPane, aAdd] = logPane("Audit trail");
      const verdict = el("div", "dverdict");
      let day = 1;
      function record(kind, amountC, note) {
        txns.push({ day, kind, amountC });
        tAdd(`day ${day} · ${kind} ${usd(amountC)} ${note || ""}`);
        day++;
      }
      function auditLine(text) {
        const h = hash(text);
        audit.push({ text, h });
        aAdd(`${h}  ${text}`);
      }
      function runRules() {
        const cash = txns.filter((t) => t.kind === "cash deposit" && t.amountC >= 800000 && t.amountC < 1000000);
        if (cash.length >= 3) {
          const total = cash.reduce((a, t) => a + t.amountC, 0);
          sfx.alarm();
          verdict.textContent = `ALERT: structuring — ${cash.length} cash deposits just under $10,000 (total ${usd(total)}) → case opened, SAR narrative drafted`;
          verdict.className = "dverdict is-bad";
          auditLine(`rule structuring_v1 fired on ${cash.length} txns; case CASE-1 opened`);
        } else {
          sfx.good();
          verdict.textContent = "No alert: activity is consistent with the customer's declared profile.";
          verdict.className = "dverdict is-ok";
          auditLine(`rules ran clean over ${txns.length} txns`);
        }
      }
      function tamper() {
        if (!audit.length) { verdict.textContent = "Nothing in the audit log to tamper with yet."; verdict.className = "dverdict"; return; }
        audit[0].text = audit[0].text.replace(/\d/, "9") + " (edited)";
        aPane.firstChild.classList.add("is-bad");
        aPane.firstChild.textContent = `${audit[0].h}  ${audit[0].text}`;
        verdict.textContent = "Entry 1 silently edited… now verify the chain.";
        verdict.className = "dverdict is-warn";
      }
      function verify() {
        chain = 7;
        for (let i = 0; i < audit.length; i++) {
          if (hash(audit[i].text) !== audit[i].h) {
            sfx.alarm();
            verdict.textContent = `TAMPER DETECTED at entry ${i + 1}: stored hash ${audit[i].h} no longer matches the chain. Every later entry is invalidated too.`;
            verdict.className = "dverdict is-bad";
            return;
          }
        }
        sfx.good();
        verdict.textContent = `audit chain verifies: ${audit.length} entries intact`;
        verdict.className = "dverdict is-ok";
      }
      const row1 = el("div", "dbtns");
      row1.append(
        btn("Inject structuring", "dbtn--go", () => {
          [940000, 970000, 985000].forEach((a) => record("cash deposit", a, "— different branch each day"));
          auditLine("ingested 3 cash deposits");
        }),
        btn("Inject benign payroll", null, () => {
          [412366, 412366].forEach((a) => record("ACH payroll", a, "— matches declared salary"));
          auditLine("ingested 2 payroll credits");
        }),
        btn("Run the rules", null, runRules),
      );
      const row2 = el("div", "dbtns");
      row2.append(btn("Tamper with the log", null, tamper), btn("Verify the chain", null, verify));
      body.append(row1, el("p", "dlbl", "Transactions"), tPane, verdict, el("p", "dlbl", "Hash-chained audit trail"), row2, aPane);
      auditLine("monitor armed; config fingerprint c0ffee42");
    },
  };
})();
