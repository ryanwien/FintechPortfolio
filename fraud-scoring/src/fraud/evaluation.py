"""Offline evaluation on the time split: ranking metrics, cost-based decisioning, drift.

Invariants:

* The split is by calendar month: warm-up (feature history only) | train | validation |
  test | shifted. Nothing is trained or tuned on a later month than it is evaluated on.
* Decision bands are optimised on the validation month only and applied unchanged to the
  test and shifted months.
* Evaluation uses matured labels (``is_fraud``); training used labels known at the cut-off.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Final

import numpy as np

from fraud.catalog import FRAUD_TYPES
from fraud.config import PSI_ALERT, PSI_WARN, TEST_PERIODS, TRAIN_PERIODS, CostConfig, SimConfig
from fraud.costs import approve_all_loss, decline_curve, evaluate_bands, optimize_bands
from fraud.drift import psi, psi_table, status
from fraud.features import FEATURES
from fraud.metrics import KEY_RECALL_PRIMARY, classification_report

MODELS: Final = ("hgb", "logreg", "rules")
PROBABILISTIC: Final = {"hgb": True, "logreg": True, "rules": False}
REPORT_SPLITS: Final = ("valid", "test", "shifted")
HOLDOUT_SPLITS: Final = ("test", "shifted")
DRIFT_COMPARISONS: Final = (
    "train_vs_test",
    "train_vs_shifted",
    "fraud_only_train_vs_test",
    "fraud_only_train_vs_shifted",
)


@dataclass(frozen=True, slots=True)
class TimeSplit:
    """Month indices of each split."""

    warmup: tuple[int, ...]
    train: tuple[int, ...]
    valid: tuple[int, ...]
    test: tuple[int, ...]
    shifted: int

    @classmethod
    def from_config(cls, cfg: SimConfig) -> TimeSplit:
        """Warm-up, then ``TRAIN_PERIODS`` train months, one validation month,
        ``TEST_PERIODS`` test months, ending right before the shifted month."""
        # Months run consecutively after warm-up: train, then one validation month, then test.
        train = tuple(range(cfg.warmup_periods, cfg.warmup_periods + TRAIN_PERIODS))
        valid = (train[-1] + 1,)
        test = tuple(range(valid[-1] + 1, valid[-1] + 1 + TEST_PERIODS))
        # Fail loudly if the layout leaves a gap or an overlap before the shifted month.
        if test[-1] + 1 != cfg.shift_period:
            raise ValueError(
                f"test months {test} must end right before shift_period={cfg.shift_period}"
            )
        return cls(tuple(range(cfg.warmup_periods)), train, valid, test, cfg.shift_period)


@dataclass(frozen=True, slots=True)
class SplitMasks:
    """Row masks over the transaction table for each evaluated split."""

    train: np.ndarray
    valid: np.ndarray
    test: np.ndarray
    shifted: np.ndarray

    @classmethod
    def from_periods(cls, period: np.ndarray, split: TimeSplit) -> SplitMasks:
        """Build one boolean mask per split from each transaction's calendar-month index.

        Train, validation and test are month sets; the shifted split is the single shifted month.
        """
        # Month sets use membership; the shifted split is one month, so equality is enough.
        return cls(
            train=np.isin(period, split.train),
            valid=np.isin(period, split.valid),
            test=np.isin(period, split.test),
            shifted=period == split.shifted,
        )

    def __getitem__(self, name: str) -> np.ndarray:
        """Return the mask for a split by name, so ``masks["test"]`` works."""
        return getattr(self, name)


def model_reports(
    scores: dict[str, np.ndarray], y: np.ndarray, masks: SplitMasks
) -> dict[str, dict[str, Any]]:
    """Classification report per model and split (valid, test, shifted)."""
    return {
        name: {
            split: classification_report(s[masks[split]], y[masks[split]], PROBABILISTIC[name])
            for split in REPORT_SPLITS
        }
        for name, s in scores.items()
    }


def _recall_by_type(s: np.ndarray, ftype: np.ndarray, n_alert: int) -> dict[str, float]:
    """Share of each fraud type's transactions inside the top ``n_alert`` scores."""
    # Mark the n_alert highest scores; the stable sort makes ties deterministic.
    alerted = np.zeros(len(s), dtype=bool)
    alerted[np.argsort(-s, kind="stable")[:n_alert]] = True
    # Recall per type: the share of that type's transactions that land in the alert set.
    return {t: float(alerted[ftype == t].mean()) for t in FRAUD_TYPES if (ftype == t).any()}


def recall_by_fraud_type(
    scores: dict[str, np.ndarray],
    ftype: np.ndarray,
    masks: SplitMasks,
    reports: dict[str, dict[str, Any]],
) -> dict[str, dict[str, dict[str, float]]]:
    """Recall by fraud type at the primary alert rate, on the hold-out splits."""
    return {
        name: {
            split: _recall_by_type(
                s[masks[split]],
                ftype[masks[split]],
                reports[name][split][KEY_RECALL_PRIMARY]["n_alerts"],
            )
            for split in HOLDOUT_SPLITS
        }
        for name, s in scores.items()
    }


def recall_ceiling(reports: dict[str, dict[str, Any]]) -> dict[str, float]:
    """Best possible recall at the primary alert rate: alerts / fraud count, capped at 1."""
    # A perfect ranker fills the alert quota with fraud, so the ceiling is alerts / fraud.
    hgb = reports["hgb"]
    return {
        split: min(1.0, hgb[split][KEY_RECALL_PRIMARY]["n_alerts"] / hgb[split]["n_fraud"])
        for split in HOLDOUT_SPLITS
    }


def decide_per_model(
    scores: dict[str, np.ndarray],
    y: np.ndarray,
    amount: np.ndarray,
    masks: SplitMasks,
    cost_cfg: CostConfig,
) -> dict[str, dict[str, Any]]:
    """Optimise bands on validation, then evaluate them frozen on test and shifted."""
    decisions: dict[str, dict[str, Any]] = {}
    # Bands are tuned on the validation month only; test and shifted just apply them.
    v, t, sh = masks.valid, masks.test, masks.shifted
    for name, s in scores.items():
        bands, v_loss = optimize_bands(s[v], y[v], amount[v], cost_cfg)
        decisions[name] = {
            "bands": asdict(bands),
            "valid_expected_loss_cents": v_loss,
            "test": evaluate_bands(s[t], y[t], amount[t], bands, cost_cfg),
            "shifted": evaluate_bands(s[sh], y[sh], amount[sh], bands, cost_cfg),
        }
    return decisions


def savings(
    decisions: dict[str, dict[str, Any]],
    y: np.ndarray,
    amount: np.ndarray,
    masks: SplitMasks,
    n_test_months: int,
) -> dict[str, Any]:
    """HGB's test-period expected-loss reduction vs rules, logistic and approve-all."""
    # Approve-all has no model or bands: its loss is the raw fraud dollars in test months.
    approve_all_test = approve_all_loss(y[masks.test], amount[masks.test])
    loss = {k: decisions[k]["test"]["expected_loss_cents"] for k in MODELS}
    return {
        "approve_all_loss_cents": approve_all_test,
        "hgb_vs_rules_cents": loss["rules"] - loss["hgb"],
        "hgb_vs_rules_pct": (loss["rules"] - loss["hgb"]) / loss["rules"],
        # Monthly figure: the test-period saving averaged over the number of test months.
        "hgb_vs_rules_cents_per_month": (loss["rules"] - loss["hgb"]) // n_test_months,
        "hgb_vs_logreg_cents": loss["logreg"] - loss["hgb"],
        "hgb_vs_approve_all_cents": approve_all_test - loss["hgb"],
        "test_months": n_test_months,
    }


def validation_decline_curve(
    hgb_scores: np.ndarray,
    y: np.ndarray,
    amount: np.ndarray,
    masks: SplitMasks,
    cost_cfg: CostConfig,
) -> dict[str, Any]:
    """Chart series: expected loss in USD vs a single decline threshold, validation month."""
    # The same validation month the bands were tuned on, so the chart shows that choice.
    v = masks.valid
    curve_x, curve_y = decline_curve(hgb_scores[v], y[v], amount[v], cost_cfg)
    return {
        "name": "Expected loss (USD) vs decline threshold, validation month, HGB",
        "x": [round(val, 6) for val in curve_x],
        "y": [round(val / 100, 2) for val in curve_y],
    }


def drift_report(
    X: np.ndarray, hgb_scores: np.ndarray, y: np.ndarray, masks: SplitMasks
) -> dict[str, Any]:
    """Feature PSI vs the training months (all traffic and fraud-only) plus score PSI."""
    # Fraud-only PSI uses matured labels of the fraud rows; usable only after chargebacks.
    fraud = y == 1
    tr, te, sh = masks.train, masks.test, masks.shifted
    drift: dict[str, Any] = {
        "thresholds": {"warn": PSI_WARN, "alert": PSI_ALERT},
        "train_vs_test": psi_table(X[tr], X[te], FEATURES),
        "train_vs_shifted": psi_table(X[tr], X[sh], FEATURES),
        # Label-based view: only possible once the shifted month's chargebacks arrive.
        "fraud_only_train_vs_test": psi_table(X[tr & fraud], X[te & fraud], FEATURES),
        "fraud_only_train_vs_shifted": psi_table(X[tr & fraud], X[sh & fraud], FEATURES),
        "score_psi_train_vs_test": psi(hgb_scores[tr], hgb_scores[te]),
        "score_psi_train_vs_shifted": psi(hgb_scores[tr], hgb_scores[sh]),
    }
    drift["score_status_shifted"] = status(drift["score_psi_train_vs_shifted"])
    # Pre-compute the ALERT and warn feature lists so the report and site can cite them.
    for key in DRIFT_COMPARISONS:
        drift[f"alerts_{key}"] = [r["feature"] for r in drift[key] if r["status"] == "ALERT"]
        drift[f"warns_{key}"] = [r["feature"] for r in drift[key] if r["status"] == "warn"]
    return drift
