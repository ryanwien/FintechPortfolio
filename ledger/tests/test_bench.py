from __future__ import annotations

from pathlib import Path

import pytest

from ledger.bench import pct, transfer_load


@pytest.mark.parametrize("p", [50, 95, 99, 100])
def test_nearest_rank_percentile_of_one_to_hundred_is_p(p: int) -> None:
    xs = [float(i) for i in range(1, 101)]
    assert pct(xs, p) == float(p)


def test_percentile_of_single_value_is_that_value() -> None:
    assert pct([3.0], 99) == 3.0


def test_transfer_load_smoke_conserves_money(tmp_path: Path) -> None:
    r = transfer_load(tmp_path / "b.db", threads=2, total_ops=40, synchronous="NORMAL")
    assert r["transfers"] == 40
    assert r["invariants_ok"]
    assert r["money_conserved"]
    assert r["latency_ms"]["p50"] <= r["latency_ms"]["p99"]
