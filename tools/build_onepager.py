"""Build the one-page PDF summary from the same data that feeds the dashboard.

Run from the repo root after `python tools/build_site.py`:
    python tools/build_onepager.py
Writes RyanWien-Fintech-Portfolio.pdf at the repo root. Core-font (Helvetica) only,
so the text must stay latin-1-safe.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

from fpdf import FPDF

ROOT: Final = Path(__file__).resolve().parent.parent
OUT: Final = ROOT / "RyanWien-Fintech-Portfolio.pdf"
SITE: Final = "ryanwien.github.io/FintechPortfolio"
LINKEDIN: Final = "linkedin.com/in/ryanwien3d"
GITHUB: Final = "github.com/ryanwien/FintechPortfolio"

INK = (17, 20, 17)
MUTE = (92, 100, 91)
ACCENT = (122, 200, 27)
LINE = (214, 218, 206)
BG_HEAD = (12, 13, 12)

# One sentence per system that a non-technical reader can take cold,
# mirroring the site's plain-English layer.
PLAIN: Final = {
    "ledger": "The bookkeeping engine behind a payments app: moves money between accounts "
    "without ever losing a cent, even when the same request is sent twice or a "
    "thousand arrive at once.",
    "fraud": "Decides in under a millisecond whether a card payment looks stolen, and prices "
    "the decision in dollars: block too little and fraud walks, block too much and "
    "real customers get declined.",
    "credit": "Decides who qualifies for a loan the way a regulated lender has to: provably "
    "fair across groups, with the exact reasons every declined applicant is legally owed.",
    "exchange": "A miniature stock exchange: matches buyers with sellers at 150,000+ orders "
    "per second, the core of how markets like NASDAQ work.",
    "aml": "The compliance watchdog banks are required to run: spots the patterns criminals "
    "use to launder money and keeps a tamper-proof evidence trail for regulators.",
}


def load() -> tuple[list[dict], dict]:
    raw = (ROOT / "assets" / "data.js").read_text(encoding="utf-8")
    body = raw.split("window.PORTFOLIO = ", 1)[1]
    projects = json.loads(body.split(";\nwindow.PORTFOLIO_META", 1)[0])
    meta = json.loads(raw.split("window.PORTFOLIO_META = ", 1)[1].rstrip().rstrip(";"))
    return projects, meta


def latin(s: str) -> str:
    """Keep core-font output safe: swap the few non-latin-1 characters the data uses."""
    return (
        s.replace("—", "-")
        .replace("–", "-")
        .replace("→", "->")
        .replace("µ", "u")
        .replace("…", "...")
        .replace("’", "'")
        .encode("latin-1", "replace")
        .decode("latin-1")
    )


def main() -> None:
    projects, meta = load()
    pdf = FPDF(format="letter", unit="mm")
    pdf.set_auto_page_break(False)
    pdf.set_margins(14, 10, 14)
    pdf.add_page()
    w = pdf.w - 28

    # header band
    pdf.set_fill_color(*BG_HEAD)
    pdf.rect(0, 0, pdf.w, 26, "F")
    pdf.set_xy(14, 7)
    pdf.set_text_color(255, 255, 255)
    pdf.set_font("helvetica", "B", 17)
    pdf.cell(w / 2, 7, "Ryan Wien")
    pdf.set_font("helvetica", "", 9.5)
    pdf.set_xy(14, 14.5)
    pdf.set_text_color(*ACCENT)
    pdf.cell(w / 2, 5, "Fintech portfolio - five working systems, live demos included")
    pdf.set_font("helvetica", "", 9)
    pdf.set_text_color(230, 230, 230)
    links = [("Live site + demos", SITE), ("Source", GITHUB), ("LinkedIn", LINKEDIN)]
    for i, (label, url) in enumerate(links):
        pdf.set_xy(pdf.w / 2 + 4, 6 + i * 5.6)
        pdf.cell(32, 5, label + ":")
        pdf.set_text_color(*ACCENT)
        pdf.cell(w / 2 - 36, 5, url, link=f"https://{url}")
        pdf.set_text_color(230, 230, 230)

    # intro
    pdf.set_xy(14, 30)
    pdf.set_text_color(*INK)
    pdf.set_font("helvetica", "", 9.6)
    pdf.multi_cell(
        w,
        4.6,
        latin(
            f"Five production-style systems covering the jobs fintech companies hire for: "
            f"moving money, catching fraud, approving loans, running markets, and compliance. "
            f"{meta['tests']} automated tests pass on every change across {meta['loc']:,} lines "
            f"of Python. Every number below is produced by the code itself, each project states "
            f"its own limitations, and each one has an interactive demo on the site."
        ),
    )

    y = pdf.get_y() + 2.5
    for p in projects:
        pdf.set_xy(14, y)
        # accent spine
        pdf.set_fill_color(*ACCENT)
        pdf.rect(14, y + 1, 1.2, 20.5, "F")
        pdf.set_xy(18, y)
        pdf.set_font("helvetica", "B", 10.5)
        pdf.cell(110, 5.4, latin(p["title"]))
        pdf.set_font("helvetica", "", 8.2)
        pdf.set_text_color(*MUTE)
        pdf.cell(w - 114, 5.4, latin(f"{p['lane']}  ·  {p['tests']} tests"), align="R")
        pdf.set_xy(18, y + 5.6)
        pdf.set_text_color(*INK)
        pdf.set_font("helvetica", "", 9)
        pdf.multi_cell(w - 4, 4.1, latin(PLAIN[p["slug"]]))
        my = pdf.get_y() + 0.8
        pdf.set_xy(18, my)
        pdf.set_font("helvetica", "B", 8.4)
        bits = [
            f"{m['value']}{(' ' + m['unit']) if m.get('unit') else ''} {m['label']}"
            for m in p["metrics"][:3]
        ]
        pdf.set_text_color(60, 98, 20)
        pdf.multi_cell(w - 4, 4.0, latin("   |   ".join(bits)))
        y = pdf.get_y() + 1.6
        pdf.set_draw_color(*LINE)
        if p is not projects[-1]:
            pdf.line(14, y, 14 + w, y)
        y += 2.2

    # footer
    pdf.set_xy(14, y + 0.5)
    pdf.set_text_color(*MUTE)
    pdf.set_font("helvetica", "", 8.6)
    pdf.multi_cell(
        w,
        4.2,
        latin(
            "Stack: Python · FastAPI · scikit-learn · pandas · SQLite · networkx · Hypothesis · "
            "pytest · GitHub Actions (CI on Python 3.12-3.14). Money is never a float; "
            "no lookahead; deterministic synthetic data, so everything reruns with one command."
        ),
    )
    if pdf.get_y() > pdf.h - 10:
        raise SystemExit(f"overflowed the page: ended at y={pdf.get_y():.1f}")
    pdf.output(str(OUT))
    print(f"wrote {OUT.name} ({OUT.stat().st_size // 1024} KB), ends y={pdf.get_y():.0f}mm")


if __name__ == "__main__":
    main()
