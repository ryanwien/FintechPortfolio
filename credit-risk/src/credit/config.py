"""Single home for run configuration: seeds, sample sizes, scaling, economics and policy.

Everything a model-risk reviewer might want to challenge (score scaling, unit economics,
policy tolerances, cutoff grids, fair-lending groups) is declared here once and imported
by the modules that use it. Changing a value here changes the documented results, which
are regenerated from code by ``python -m credit.run``.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path
from typing import Final

# --- Reproducibility and sample ---------------------------------------------------------
SEED: Final[int] = 20261013
N_APPLICANTS: Final[int] = 60_000
N_MONTHS: Final[int] = 24
DEV_MONTHS: Final[int] = 18  # months 0..17 are development, 18..23 out-of-time
HOLDOUT_FRACTION: Final[float] = 0.30  # share of development held out as in-time test
CHALLENGER_SEED: Final[int] = 7

# --- Score scaling (points-to-double-odds) ----------------------------------------------
BASE_SCORE: Final[float] = 600.0
BASE_ODDS: Final[float] = 30.0  # good:bad odds at BASE_SCORE
PDO: Final[float] = 20.0  # points to double the odds

# --- WoE binning and variable selection -------------------------------------------------
MAX_BINS: Final[int] = 10
MIN_BIN_FRAC: Final[float] = 0.05  # numeric bins: minimum population share per bin
CATEGORICAL_MIN_BIN_FRAC: Final[float] = 0.02  # rarer levels are pooled into __OTHER__
MIN_IV: Final[float] = 0.01  # candidates below this Information Value are excluded

# --- Unit economics (documented assumptions, not a pricing model) -----------------------
LIFETIME_MARGIN: Final[Decimal] = Decimal("0.09")
LGD: Final[Decimal] = Decimal("0.70")
EAD_FRACTION: Final[Decimal] = Decimal("0.75")

# --- Policy thresholds (documented in the model card) -----------------------------------
PROXY_AUC_TOLERANCE: Final[float] = 0.015  # max OOT AUC given up to remove a geographic proxy
CHALLENGER_MIN_UPLIFT: Final[float] = 0.020  # OOT AUC uplift to justify a less transparent model
FOUR_FIFTHS: Final[float] = 0.80  # adverse impact ratio screening threshold
TOP_N_REASONS: Final[int] = 4  # Reg B principal reasons disclosed per decline

# --- Cutoff grids -----------------------------------------------------------------------
SEARCH_CUTOFFS: Final[tuple[int, ...]] = tuple(range(480, 701, 5))
TABLE_CUTOFFS: Final[tuple[int, ...]] = tuple(range(500, 621, 10))
APPROVE_ALL_CUTOFF: Final[int] = -10_000  # below any attainable score: approve everyone

# --- Fair-lending groups ----------------------------------------------------------------
REFERENCE_GROUP: Final[str] = "A"
PROTECTED_GROUP: Final[str] = "B"

# --- Outputs ----------------------------------------------------------------------------
N_SAMPLE_NOTICES: Final[int] = 3
RESULTS_DIR: Final[Path] = Path(__file__).resolve().parents[2] / "results"
