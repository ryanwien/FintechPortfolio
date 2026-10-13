"""Cost model, band optimiser and band evaluation."""

from __future__ import annotations

import itertools

import numpy as np
import pytest

from fraud.costs import (
    Bands,
    CostConfig,
    action_costs,
    candidate_thresholds,
    evaluate_bands,
    optimize_bands,
)

CFG = CostConfig(
    review_cost_cents=300,
    review_catch_bps=9_000,
    review_friction_cents=100,
    decline_margin_bps=300,
    decline_friction_cents=1_000,
    max_review_rate=1.0,
)


def test_action_costs_are_exact_integer_cents():
    y = np.array([1, 0, 1, 0])
    amount = np.array([10_000, 10_000, 333, 333])
    c = action_costs(y, amount, CFG)
    assert c.dtype == np.int64
    # approve: fraud costs the full amount, good costs nothing
    assert c[:, 0].tolist() == [10_000, 0, 333, 0]
    # review: 300 analyst + 10% leakage on fraud | + 100 friction on good
    assert c[:, 1].tolist() == [300 + 1_000, 300 + 100, 300 + 33, 300 + 100]
    # decline: good customers lose 3% margin + 1000 friction; fraud costs nothing
    assert c[:, 2].tolist() == [0, 300 + 1_000, 0, 9 + 1_000]


@pytest.mark.parametrize(
    ("score", "expected"),
    [(0.0, "approve"), (0.0499, "approve"), (0.05, "review"), (0.4999, "review"), (0.5, "decline")],
)
def test_bands_decide_uses_inclusive_lower_edges(score, expected):
    bands = Bands(review_at=0.05, decline_at=0.5)
    assert bands.decide(score) == expected
    assert bands.decide_many(np.array([score])).tolist() == [
        ["approve", "review", "decline"].index(expected)
    ]


def _loss_bruteforce(scores, y, amount, cfg, r, d):
    return evaluate_bands(scores, y, amount, Bands(r, d), cfg)["expected_loss_cents"]


def test_optimizer_matches_exhaustive_search():
    rng = np.random.default_rng(3)
    n = 400
    y = (rng.random(n) < 0.08).astype(int)
    scores = np.clip(rng.normal(0.2 + 0.5 * y, 0.2), 0, 1).round(2)  # ties on purpose
    amount = rng.integers(100, 200_000, n)
    bands, loss = optimize_bands(scores, y, amount, CFG)
    cands = candidate_thresholds(scores)
    best = min(
        _loss_bruteforce(scores, y, amount, CFG, r, d)
        for r, d in itertools.product(cands, cands)
        if r <= d
    )
    assert loss == best
    assert _loss_bruteforce(scores, y, amount, CFG, bands.review_at, bands.decline_at) == loss


def test_review_capacity_constraint_is_respected():
    rng = np.random.default_rng(4)
    n = 2_000
    y = (rng.random(n) < 0.05).astype(int)
    scores = np.clip(rng.normal(0.3 + 0.4 * y, 0.2), 0, 1)
    amount = rng.integers(5_000, 50_000, n)
    cfg = CostConfig(review_catch_bps=10_000, review_cost_cents=1, max_review_rate=0.02)
    bands, _ = optimize_bands(scores, y, amount, cfg)
    out = evaluate_bands(scores, y, amount, bands, cfg)
    assert out["review"] <= 0.02 * n


def test_perfect_model_declines_all_fraud_and_nothing_else():
    y = np.array([0] * 90 + [1] * 10)
    scores = y.astype(float)
    amount = np.full(100, 50_000)
    bands, loss = optimize_bands(scores, y, amount, CFG)
    out = evaluate_bands(scores, y, amount, bands, CFG)
    assert loss == 0
    assert out["decline"] == 10
    assert out["false_declines"] == 0


def test_expensive_declines_push_policy_to_approve():
    y = np.array([0] * 95 + [1] * 5)
    scores = np.linspace(0, 1, 100)
    amount = np.full(100, 100)  # $1 fraud vs $10,000 friction per false decline
    cfg = CostConfig(decline_friction_cents=1_000_000, review_cost_cents=1_000_000)
    bands, loss = optimize_bands(scores, y, amount, cfg)
    out = evaluate_bands(scores, y, amount, bands, cfg)
    assert out["false_declines"] == 0
    assert loss <= 5 * 100
