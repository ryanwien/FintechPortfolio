"""End-to-end run: world -> detection -> cases -> analyst queue -> evaluation -> reports.

    python -m aml.run [--seed N] [--customers N] [--config overrides.json] [--out results]

Every file written to ``--out`` is deterministic for a given seed, size and config; the
wall-clock duration is only printed.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .audit import AuditLog
from .cases import build_cases, work_cases
from .config import DEFAULT_CUSTOMERS, DEFAULT_SEED, load_config
from .detect import run_detection
from .evaluate import evaluate, line_sweep, rule_sweeps
from .report import build_site, pct, pick_sample_case, render_report, render_sample_case
from .synth import generate_world


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> dict[str, Any]:
    """Run the pipeline, write the results files and return the metrics dict."""
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--customers", type=int, default=DEFAULT_CUSTOMERS)
    ap.add_argument("--config", default=None)
    ap.add_argument("--out", default="results")
    args = ap.parse_args(argv)

    t0 = time.perf_counter()
    cfg = load_config(args.config)
    world = generate_world(args.seed, args.customers)
    det = run_detection(world, cfg)
    cases = build_cases(world, det.alerts, det.network_of(), cfg.scoring)
    audit = AuditLog()
    work_cases(world, cases, cfg, audit)
    verified = audit.verify()

    metrics: dict[str, Any] = {"config": cfg.tag(), "seed": args.seed}
    metrics.update(evaluate(world, det, cases, cfg))
    metrics["audit"] = {
        "entries": verified.entries,
        "verified": verified.ok,
        "head_hash": audit.head,
    }
    metrics["line_sweep"] = line_sweep(world, cases)
    metrics["rule_sweeps"] = rule_sweeps(world, det, cfg)
    metrics["degree_thresholds"] = det.degree_thresholds
    sample = pick_sample_case(cases)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "metrics.json", metrics)
    (out / "REPORT.md").write_text(render_report(metrics), encoding="utf-8")
    (out / "sample_case.md").write_text(render_sample_case(world, sample, audit), encoding="utf-8")
    _write_json(out / "site.json", build_site(metrics))

    de, al = metrics["detection"], metrics["alerts"]
    print(
        f"{len(world.customers)} customers, {len(world.txns)} txns, {al['total']} alerts "
        f"({al['per_1k_customers']}/1k), precision {pct(al['precision'])}, "
        f"detection at line {pct(de['instance_detection_rate_at_line'])}, "
        f"{metrics['cases']['above_line']} cases above line; audit ok={verified.ok}; "
        f"sample {sample.case_id}; {time.perf_counter() - t0:.1f}s"
    )
    return metrics


if __name__ == "__main__":
    main()
