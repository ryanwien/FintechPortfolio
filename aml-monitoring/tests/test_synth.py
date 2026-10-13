"""Synthetic world: seeded determinism, ground truth and look-alike bookkeeping."""

import pytest

from aml.synth import TYPOLOGIES, UnknownSegmentError, WorldBuilder, gen_business, generate_world


@pytest.fixture(scope="module")
def small_world():
    return generate_world(seed=17, n_customers=400)


def test_same_seed_gives_identical_world(small_world):
    again = generate_world(seed=17, n_customers=400)
    assert again.txns == small_world.txns
    assert again.customers == small_world.customers
    assert again.instances == small_world.instances


def test_different_seed_gives_different_world(small_world):
    assert generate_world(seed=18, n_customers=400).txns != small_world.txns


def test_transactions_are_time_ordered_and_labels_match_instances(small_world):
    keys = [(t.ts, t.txn_id) for t in small_world.txns]
    assert keys == sorted(keys)
    labels = {t.label for t in small_world.txns if t.label}
    assert labels <= set(TYPOLOGIES)
    assert labels == {i.typology for i in small_world.instances}


def test_lookalike_buckets_are_sorted_and_benign(small_world):
    truth = small_world.truth()
    assert small_world.lookalikes
    for members in small_world.lookalikes.values():
        assert members == sorted(members)
    intercompany = small_world.lookalikes["intercompany_group"]
    assert not set(intercompany) & set(truth)


def test_unknown_business_segment_is_rejected():
    with pytest.raises(UnknownSegmentError, match="unknown segment"):
        gen_business(WorldBuilder(1), "casino")
