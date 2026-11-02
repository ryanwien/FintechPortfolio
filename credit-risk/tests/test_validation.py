import pytest

from credit.validation import feature_iv_rows
from credit.woe import OTHER


def csi_by_feature(fitted, oot):
    rows = feature_iv_rows(fitted.scorecard, fitted.dev, oot)
    return {r["feature"]: r["csi_dev_vs_oot"] for r in rows}


@pytest.fixture
def oot_with_unseen_purpose(fitted):
    """OOT sample whose bin-0 purpose level is renamed to one development never saw.

    Folding unseen levels into bin 0 (the old clip(0) counting) puts the renamed rows
    straight back where they came from, leaving the counts - and the CSI - unchanged:
    the silent distortion the dedicated unseen bin exists to expose.
    """
    binning = fitted.scorecard.binner.binnings["purpose"]
    # No level is rare enough to pool, so an unseen level maps to -1, not __OTHER__.
    assert OTHER not in binning.category_map
    bin0_level = binning.labels[0]
    oot = fitted.oot.copy()
    assert (oot["purpose"] == bin0_level).any()
    oot.loc[oot["purpose"] == bin0_level, "purpose"] = "yacht_refit"
    return oot


def test_unseen_oot_level_raises_purpose_csi(fitted, oot_with_unseen_purpose):
    baseline = csi_by_feature(fitted, fitted.oot)
    shifted = csi_by_feature(fitted, oot_with_unseen_purpose)
    # A whole level moving to one development never saw is a significant shift,
    # past the conventional PSI red line of 0.25.
    assert shifted["purpose"] > baseline["purpose"]
    assert shifted["purpose"] > 0.25


def test_unseen_level_leaves_other_characteristics_untouched(fitted, oot_with_unseen_purpose):
    baseline = csi_by_feature(fitted, fitted.oot)
    shifted = csi_by_feature(fitted, oot_with_unseen_purpose)
    for f, csi in baseline.items():
        if f != "purpose":
            assert shifted[f] == csi
