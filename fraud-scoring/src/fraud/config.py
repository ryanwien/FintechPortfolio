"""Single home for every tunable: seeds, simulation sizes, cost model, model hyperparameters,
drift thresholds, evaluation and serving-benchmark settings, and filesystem locations.

Invariants:

* Every value here is a pure constant or a frozen dataclass default, so one run is fully
  determined by this module plus the code.
* Money is integer cents and rates are integer basis points (``BPS``), so per-transaction
  costs stay exact.
* Time is integer seconds since the simulation epoch.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Final

# ------------------------------------------------------------------------------- units
HOUR: Final = 3_600
DAY: Final = 86_400
WEEK: Final = 7 * DAY
BPS: Final = 10_000  # basis points in 100%

# ------------------------------------------------------------------------------- paths
PROJECT_ROOT: Final = Path(__file__).resolve().parents[2]
RESULTS_DIR: Final = PROJECT_ROOT / "results"
ARTIFACTS_DIR: Final = PROJECT_ROOT / "artifacts"  # git-ignored
SCORER_ARTIFACT: Final = ARTIFACTS_DIR / "scorer.pkl"


# ------------------------------------------------------------------------------- simulation
@dataclass(frozen=True, slots=True)
class SimConfig:
    """Size, seed and behaviour rates of the synthetic transaction stream."""

    seed: int = 7
    n_customers: int = 1_800
    n_merchants: int = 1_200
    n_periods: int = 9  # 30-day "months"
    period_days: int = 30
    warmup_periods: int = 2  # feature warm-up only; never trained or evaluated on
    late_joiner_share: float = 0.25  # customers who open their account mid-stream
    mean_txn_per_day: float = 0.75
    one_off_device_p: float = 0.03  # legit CNP txn from a never-seen browser/device
    big_ticket_per_month: float = 0.3  # legit out-of-pattern purchases per customer
    micro_burst_per_month: float = 0.25  # legit in-app micro-purchase bursts per customer
    trips_per_month: float = 0.3
    # Fraud episode rates per day.
    card_testing_per_day: float = 1.3
    ato_per_day: float = 2.0
    high_value_per_day: float = 2.0
    compromised_merchants_per_period: int = 3
    # First-party "friendly fraud": a genuine purchase later disputed as unauthorised.
    # Behaviourally identical to the customer's normal spend, so no model can catch it.
    friendly_fraud_p: float = 0.012  # of eligible CNP purchases >= $40
    shift_period: int = 8  # last period: seasonal + fraud-tactic shift

    @property
    def total_days(self) -> int:
        """Length of the simulated stream in days."""
        return self.n_periods * self.period_days

    @property
    def total_seconds(self) -> int:
        """Length of the simulated stream in seconds."""
        return self.total_days * DAY

    @property
    def period_seconds(self) -> int:
        """Length of one simulated period in seconds."""
        return self.period_days * DAY

    @property
    def shift_start_ts(self) -> int:
        """First second of the shifted period."""
        return self.shift_period * self.period_seconds


# ------------------------------------------------------------------------------- decisioning
@dataclass(frozen=True, slots=True)
class CostConfig:
    """Per-transaction cost model for approve / review / decline (see :mod:`fraud.costs`)."""

    review_cost_cents: int = 300  # analyst time per manual review
    review_catch_bps: int = 9_000  # reviewers stop 90% of the fraud they see
    review_friction_cents: int = 100  # good customer held for a review
    decline_margin_bps: int = 300  # lost margin on a declined good purchase (3%)
    decline_friction_cents: int = 1_000  # customer friction / attrition per false decline
    max_review_rate: float = 0.01  # analyst capacity: at most 1% of traffic

    def as_dict(self) -> dict[str, Any]:
        """Cost parameters as a plain dict, for the metrics output."""
        return asdict(self)


# ------------------------------------------------------------------------------- models
HGB_PARAMS: Final[dict[str, Any]] = {
    "learning_rate": 0.06,
    "max_iter": 300,
    "max_leaf_nodes": 31,
    "max_depth": 8,  # bounds the online tree walk to 8 steps
    "min_samples_leaf": 40,
    "l2_regularization": 1.0,
    "early_stopping": False,
    "random_state": 0,
}
LOGREG_C: Final = 1.0
LOGREG_MAX_ITER: Final = 3_000

# ------------------------------------------------------------------------------- evaluation
TRAIN_PERIODS: Final = 3  # months after warm-up used for training
TEST_PERIODS: Final = 2  # months after the validation month, before the shifted one
ALERT_RATE_PRIMARY: Final = 0.005  # analyst queue sized to 0.5% of traffic
ALERT_RATE_WIDE: Final = 0.02  # shows headroom beyond what 0.5% can cover

# ------------------------------------------------------------------------------- drift
PSI_WARN: Final = 0.10  # moderate shift: investigate
PSI_ALERT: Final = 0.25  # significant shift: page the model owner / consider retrain
PSI_BINS: Final = 10

# ------------------------------------------------------------------------------- serving bench
N_LATENCY: Final = 5_000  # test requests timed with the production setting (reasons on adverse)
N_LATENCY_ALWAYS: Final = 2_000  # next requests timed with reason codes forced on every request
N_HTTP: Final = 1_000  # shifted-month requests driven through the FastAPI app
REASON_TOP_K: Final = 3
