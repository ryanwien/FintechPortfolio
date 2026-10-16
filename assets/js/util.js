/* Shared DOM and formatting helpers, exposed as window.UI.
   Loaded first; every other script on the page builds on these.

   The dashboard's data shape (produced by tools/build_site.py into assets/data.js):
     window.PORTFOLIO: Array<{
       slug, folder, run, title, tagline, lane, stack: string[],
       tests: number, loc: number, report: string,
       metrics: Array<{label, value, unit?, note?}>,
       highlights: string[], limitations: string[],
       series?: {name, x: Array<number|string>, y: number[]}
     }>
     window.PORTFOLIO_META: { tests, loc, commits: Array<{sha, date, msg}> } */
(() => {
  "use strict";

  /** querySelector shorthand, optionally scoped to a root node. */
  const $ = (sel, root = document) => root.querySelector(sel);

  /** Create an element with an optional class and text content. */
  const el = (tag, cls, text) => {
    const n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text != null) n.textContent = text;
    return n;
  };

  const SVG_NS = "http://www.w3.org/2000/svg";

  /** Create an SVG element with attributes. */
  const mk = (tag, attrs) => {
    const n = document.createElementNS(SVG_NS, tag);
    for (const k in attrs) n.setAttribute(k, attrs[k]);
    return n;
  };

  /** Integer with thousands separators. */
  const fmtInt = (n) => Number(n).toLocaleString();

  /** Integer cents as dollars, e.g. 1500 -> "$15.00". Money is cents everywhere. */
  const usd = (cents) => (cents / 100).toLocaleString(undefined, { style: "currency", currency: "USD" });

  window.UI = { $, el, mk, fmtInt, usd };
})();
