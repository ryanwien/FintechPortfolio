/* Per-system content, exposed as window.CATALOG: short symbols, display names,
   the plain-English sentences, chart colors, and the JD-coverage matrix.
   Metrics and reports are NOT here — they come from assets/data.js, which is
   generated from each project's real run. */
(() => {
  "use strict";

  const REPO = "https://github.com/ryanwien/FintechPortfolio";

  /** Three-letter ticker shown in the sidebar, table and matrix header. */
  const SYM = { ledger: "LDG", fraud: "FRD", credit: "CRD", exchange: "XCH", aml: "AML" };

  /** Short display name used where the full project title is too long. */
  const NAMES = {
    ledger: "Ledger & payments",
    fraud: "Fraud scoring",
    credit: "Credit risk",
    exchange: "Matching engine",
    aml: "AML monitoring",
  };

  /** One sentence per system that a non-technical recruiter can read cold.
      tools/build_onepager.py mirrors these on the PDF. */
  const PLAIN = {
    ledger:
      "The bookkeeping engine behind a payments app: it moves money between accounts without " +
      "ever losing a cent, even when the same request is sent twice or a thousand arrive at once.",
    fraud:
      "Decides in under a millisecond whether a card payment looks stolen, and prices the " +
      "decision in dollars: block too little and fraud walks, block too much and real " +
      "customers get declined.",
    credit:
      "Decides who qualifies for a loan the way a regulated lender has to: provably fair " +
      "across groups, with the exact reasons every declined applicant is legally owed.",
    exchange:
      "A miniature stock exchange: it matches buyers with sellers at 150,000+ orders per " +
      "second, the core of how markets like NASDAQ work.",
    aml:
      "The compliance watchdog banks are required to run: it spots patterns criminals use to " +
      "launder money and keeps a tamper-proof evidence trail for regulators.",
  };

  /** One chart color per system, in data.js order. */
  const COLORS = ["#b6ef5a", "#7cb3ff", "#f5b84a", "#ff7b86", "#c084fc"];

  /** JD-coverage matrix: requirement -> slugs of the systems that demonstrate it. */
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
    ["CI on every push, three Python versions", ["ledger", "fraud", "credit", "exchange", "aml"]],
  ];

  window.CATALOG = { REPO, SYM, NAMES, PLAIN, COLORS, COVER };
})();
