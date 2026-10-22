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

    # Wall-clock timer, used only in the final summary line; no written file depends on it.
    t0 = time.perf_counter()
    # Load the config first: the run tag and every threshold come from it, so nothing runs before
    # it.
    cfg = load_config(args.config)
    # The world is a pure function of seed and size, so every later stage sees identical data on
    # each run.
    world = generate_world(args.seed, args.customers)
    # Detection runs the rules over the world; the customer network it returns feeds case building.
    det = run_detection(world, cfg)
    # Alerts are grouped into cases and scored with the configured weights.
    cases = build_cases(world, det.alerts, det.network_of(), cfg.scoring)
    # Every case action goes into one hash-chained audit log.
    audit = AuditLog()
    # Simulated analyst work runs in time order and writes each step to the audit log.
    work_cases(world, cases, cfg, audit)
    # Verify the chain at the end, so the results record whether the log is intact.
    verified = audit.verify()

    # Metrics are built in stages: run identity first, then core evaluation, audit status and
    # sweeps.
    metrics: dict[str, Any] = {"config": cfg.tag(), "seed": args.seed}
    metrics.update(evaluate(world, det, cases, cfg))
    metrics["audit"] = {
        "entries": verified.entries,
        "verified": verified.ok,
        "head_hash": audit.head,
    }
    # Sweeps are extra analyses over the same data: the case line moves, and single rules re-run
    # with shifted thresholds.
    metrics["line_sweep"] = line_sweep(world, cases)
    metrics["rule_sweeps"] = rule_sweeps(world, det, cfg)
    metrics["degree_thresholds"] = det.degree_thresholds
    # Pick the sample case once, so the same case is written to sample_case.md on every run.
    sample = pick_sample_case(cases)

    # Everything goes under --out; the directory is created if it does not exist yet.
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # metrics.json is the machine-readable source; the other files are rendered from the metrics or
    # the case.
    _write_json(out / "metrics.json", metrics)
    (out / "REPORT.md").write_text(render_report(metrics), encoding="utf-8")
    (out / "sample_case.md").write_text(render_sample_case(world, sample, audit), encoding="utf-8")
    _write_json(out / "site.json", build_site(metrics))

    # The console summary reads the numbers just written, so it cannot disagree with the files.
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
