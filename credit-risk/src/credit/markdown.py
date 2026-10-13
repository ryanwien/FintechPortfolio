"""Markdown building blocks shared by the validation report and the model card.

Pure formatting: every function reads the rounded ``metrics`` dict and returns text.
Tables that appear in both documents live here so the two can never disagree.
"""

from __future__ import annotations

from collections.abc import Sequence

from credit.config import PROTECTED_GROUP, REFERENCE_GROUP
from credit.serialize import Metrics


def pct(x: float, nd: int = 1) -> str:
    """Format a fraction as a percentage, e.g. ``0.1234 -> '12.3%'``."""
    return f"{100 * x:.{nd}f}%"


def pass_fail(passed: bool) -> str:
    """Four-fifths screen outcome as shown in tables."""
    return "pass" if passed else "FAIL"


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> str:
    """GitHub-flavoured Markdown table."""
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c) for c in r) + " |" for r in rows]
    return "\n".join(out)


def perf_table(m: Metrics) -> str:
    """AUC / Gini / KS for champion and challenger, in-time and out-of-time."""
    rows = []
    for name, d in (("Scorecard (champion)", m["champion"]), ("GBM (challenger)", m["challenger"])):
        for window, label in (("in_time", "in-time holdout"), ("oot", "out-of-time")):
            v = d[window]
            rows.append([name, label, f"{v['auc']:.4f}", f"{v['gini']:.4f}", f"{v['ks']:.4f}"])
    return table(["Model", "Sample", "AUC", "Gini", "KS"], rows)


def fair_table(m: Metrics) -> str:
    """Approval rates, AIR and EO gap for both scorecard candidates and the challenger."""
    f = m["fairness"]
    rows = [
        _fair_row(name, f[key], f"{f[key]['oot_auc']:.4f}", f[key]["cutoff"])
        for name, key in (
            ("With region (proxy)", "with_proxy"),
            ("Region removed", "proxy_removed"),
        )
    ]
    ch = m["challenger"]
    rows.append(
        _fair_row(
            "GBM challenger (matched approval)",
            ch["fairness_at_matched_approval"],
            f"{ch['oot']['auc']:.4f}",
            "n/a",
        )
    )
    return table(
        ["Model", "OOT AUC", "Cutoff", "Approve A", "Approve B", "AIR", "4/5 rule", "EO gap"],
        rows,
    )


def _fair_row(name: str, d: Metrics, auc: str, cutoff: object) -> list[object]:
    return [
        name,
        auc,
        cutoff,
        pct(d["approval_rate"][REFERENCE_GROUP]),
        pct(d["approval_rate"][PROTECTED_GROUP]),
        f"{d['adverse_impact_ratio']:.3f}",
        pass_fail(d["passes_four_fifths"]),
        f"{d['equal_opportunity_gap']:.4f}",
    ]


def adopted_fairness(m: Metrics) -> Metrics:
    """Fairness block of the scorecard variant that was adopted (with or without proxy)."""
    f = m["fairness"]
    return f["proxy_removed"] if f["mitigation_adopted"] else f["with_proxy"]
