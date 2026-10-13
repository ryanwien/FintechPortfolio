"""End-to-end: deterministic outputs, labels never used by detection."""

import json
import os
import subprocess
import sys
from pathlib import Path

from aml.config import MonitoringConfig
from aml.detect import run_detection
from aml.run import main
from aml.synth import generate_world


def test_detection_ignores_ground_truth_labels():
    cfg = MonitoringConfig()
    w = generate_world(seed=5, n_customers=500)
    a = run_detection(w, cfg).alerts
    b = run_detection(w.without_labels(), cfg).alerts
    key = [(x.rule_id, x.customer_id, x.ts_start, x.txn_ids, x.score) for x in a]
    assert key == [(x.rule_id, x.customer_id, x.ts_start, x.txn_ids, x.score) for x in b]


def test_run_is_deterministic_and_writes_artifacts(tmp_path):
    out1, out2 = tmp_path / "a", tmp_path / "b"
    main(["--customers", "500", "--seed", "9", "--out", str(out1)])
    main(["--customers", "500", "--seed", "9", "--out", str(out2)])
    for name in ("metrics.json", "REPORT.md", "sample_case.md", "site.json"):
        text = (out1 / name).read_text(encoding="utf-8")
        assert text == (out2 / name).read_text(encoding="utf-8")
    site = json.loads((out1 / "site.json").read_text(encoding="utf-8"))
    assert site["slug"] == "aml"
    assert site["lane"] == "Financial crime & compliance"
    assert len(site["series"]["x"]) == len(site["series"]["y"]) > 0
    metrics = json.loads((out1 / "metrics.json").read_text(encoding="utf-8"))
    assert metrics["audit"]["verified"] is True
    assert "Audit trail" in (out1 / "sample_case.md").read_text(encoding="utf-8")


def test_results_do_not_depend_on_string_hash_seed():
    code = (
        "import hashlib;from aml.config import MonitoringConfig;"
        "from aml.detect import run_detection;from aml.synth import generate_world;"
        "d=run_detection(generate_world(3,500),MonitoringConfig());"
        "print(hashlib.sha256(repr([(a.rule_id,a.customer_id,a.txn_ids,a.score)"
        " for a in d.alerts]+[n.members for n in d.networks]).encode()).hexdigest())"
    )
    src = str(Path(__file__).resolve().parents[1] / "src")
    digests = set()
    for seed in ("1", "2"):
        env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=src)
        out = subprocess.run(
            [sys.executable, "-c", code], env=env, capture_output=True, text=True, check=True
        )
        digests.add(out.stdout.strip())
    assert len(digests) == 1
