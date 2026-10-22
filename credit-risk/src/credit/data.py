"""Deterministic synthetic loan applications with a latent default process.

The generator produces bureau-style attributes plus a protected attribute that is used
ONLY for fair-lending testing. The protected group never enters the default process
directly; it shifts the distribution of income and of region of residence, so region
acts as a geographic proxy (a classic redlining risk) and income carries a structural gap.

The numeric coefficients inside ``generate_applicants`` *are* the simulated world (the
data-generating process), not tunable model settings; the few that carry business meaning
(reference income, loan bounds, terms) are named below. Output is a pure function of
``(n, seed, months)``.
"""

from __future__ import annotations

from typing import Final

import numpy as np
import pandas as pd

from credit.config import DEV_MONTHS, N_APPLICANTS, N_MONTHS, SEED

PROTECTED: Final[str] = "protected_group"
TARGET: Final[str] = "bad"
MONTH: Final[str] = "application_month"

NUMERIC_FEATURES: Final[list[str]] = [
    "annual_income",
    "dti",
    "revolving_utilization",
    "delinq_2y",
    "inquiries_6m",
    "credit_history_months",
    "employment_years",
    "loan_amount",
    "term_months",
]
CATEGORICAL_FEATURES: Final[list[str]] = ["purpose", "region"]
ALL_FEATURES: Final[list[str]] = NUMERIC_FEATURES + CATEGORICAL_FEATURES
PROXY_FEATURES: Final[list[str]] = ["region"]

REGIONS: Final[list[str]] = ["R1", "R2", "R3", "R4", "R5", "R6"]
PURPOSES: Final[list[str]] = [
    "debt_consolidation",
    "credit_card",
    "home_improvement",
    "auto",
    "medical",
    "small_business",
    "other",
]

# Regional economic stress (log-odds shift). Unobserved by the lender except via region.
_REGION_STRESS: Final[np.ndarray] = np.array([-0.30, -0.15, 0.0, 0.05, 0.30, 0.40])
_REGION_P_REF: Final[np.ndarray] = np.array([0.24, 0.22, 0.20, 0.18, 0.10, 0.06])
_REGION_P_PROT: Final[np.ndarray] = np.array([0.05, 0.07, 0.11, 0.14, 0.29, 0.34])
_PURPOSE_P: Final[np.ndarray] = np.array([0.42, 0.20, 0.10, 0.08, 0.06, 0.05, 0.09])
_PURPOSE_EFFECT: Final[np.ndarray] = np.array([0.0, -0.15, -0.20, -0.10, 0.25, 0.55, 0.15])
_SMALL_BUSINESS: Final[int] = PURPOSES.index("small_business")

_PROTECTED_SHARE: Final[float] = 0.30
_REFERENCE_INCOME: Final[int] = 68_000  # median annual income; centre of the income effect
_INCOME_BOUNDS: Final[tuple[int, int]] = (12_000, 600_000)
LOAN_BOUNDS: Final[tuple[int, int]] = (1_000, 40_000)
SHORT_TERM: Final[int] = 36
LONG_TERM: Final[int] = 60
_LOAN_ROUNDING: Final[int] = 500


def generate_applicants(
    n: int = N_APPLICANTS, seed: int = SEED, months: int = N_MONTHS
) -> pd.DataFrame:
    """Return ``n`` synthetic applications with features, protected group and default flag."""
    # One generator, drawn in a fixed order. Adding or reordering a draw changes every
    # dataset produced from the seed, so the order below is part of the contract.
    rng = np.random.default_rng(seed)
    # Sorted month: rows are in application-time order, so OOT is a contiguous tail.
    month = np.sort(rng.integers(0, months, n))
    # Same boundary split_dev_oot uses, so the generated drift and the split agree.
    oot = month >= DEV_MONTHS

    # The protected group acts through the region mix (the proxy channel) and through
    # income below; it has no direct term in the default process.
    prot = rng.random(n) < _PROTECTED_SHARE
    region_idx = np.where(
        prot,
        rng.choice(len(REGIONS), n, p=_REGION_P_PROT),
        rng.choice(len(REGIONS), n, p=_REGION_P_REF),
    )

    # Income: lognormal with a structural gap for the protected group and by region.
    log_inc = (
        np.log(_REFERENCE_INCOME)
        - 0.14 * prot
        - 0.05 * (region_idx - 2.5) / 2.5
        + rng.normal(0, 0.48, n)
    )
    # Round to the nearest $100, then clip to the plausible income range.
    income = np.clip(np.round(np.exp(log_inc), -2), *_INCOME_BOUNDS).astype(np.int64)

    age_factor = rng.gamma(4.0, 1.0, n)  # shared driver of history and tenure
    credit_history_months = np.clip(np.round(age_factor * 32 + rng.normal(0, 18, n)), 3, 480)
    employment_years = np.clip(np.round(age_factor * 1.6 + rng.normal(0, 1.5, n)), 0, 10)

    # Debt-to-income is lower for higher-income applicants, all else equal.
    dti = np.clip(rng.normal(19, 8, n) - 3 * (log_inc - np.log(_REFERENCE_INCOME)), 0, 55).round(1)
    # Utilization: a Beta base, nudged up by debt-to-income.
    util_base = rng.beta(2.0, 3.0, n) * 1.1 + 0.004 * (dti - 19)
    # Out-of-time drift: utilization and inquiries creep up (late-cycle consumer).
    util = np.clip(util_base + 0.04 * oot, 0, 1.2).round(3)
    # Inquiries and delinquencies are Poisson, with rates rising in utilization, so the
    # bureau signals move together.
    inquiries = rng.poisson(np.where(oot, 1.25, 1.0) * np.exp(0.6 * (util - 0.45)))
    delinq = rng.poisson(0.22 * np.exp(1.2 * (util - 0.45) + 0.02 * (dti - 19)))

    purpose_idx = rng.choice(len(PURPOSES), n, p=_PURPOSE_P)
    # Small-business loans are more often long-term, and long terms raise the amount.
    long_term_p = 0.28 + 0.15 * (purpose_idx == _SMALL_BUSINESS)
    term = np.where(rng.random(n) < long_term_p, LONG_TERM, SHORT_TERM)
    loan = income * rng.uniform(0.06, 0.32, n) * np.where(term == LONG_TERM, 1.3, 1.0)
    # Round to whole $500 and clip to the bounds, after the 1.3x long-term uplift above.
    loan_amount = np.clip(np.round(loan / _LOAN_ROUNDING) * _LOAN_ROUNDING, *LOAN_BOUNDS)
    loan_amount = loan_amount.astype(np.int64)

    frailty = rng.normal(0, 0.55, n)  # unobserved heterogeneity caps achievable AUC
    # Latent default process: part of the simulated world, not model settings. Income,
    # DTI, utilization, delinquency, inquiries, history, employment, loan-to-income,
    # term, purpose, region, the OOT shift and frailty all enter the logit.
    logit = (
        -2.70
        - 0.55 * (log_inc - np.log(_REFERENCE_INCOME))
        + 0.030 * (dti - 19)
        + 1.70 * (util - 0.45)
        + 0.42 * delinq
        + 0.16 * inquiries
        - 0.0045 * (credit_history_months - 128)
        - 0.05 * (employment_years - 5)
        + 0.25 * (term == LONG_TERM)
        + 0.60 * (loan_amount / income - 0.2)
        + _PURPOSE_EFFECT[purpose_idx]
        + _REGION_STRESS[region_idx]
        + 0.12 * oot  # macro deterioration in the OOT window
        + frailty
    )
    # Bernoulli draw from the latent PD. Only this flag is observed, never the PD itself.
    pd_true = 1.0 / (1.0 + np.exp(-logit))
    bad = (rng.random(n) < pd_true).astype(np.int8)

    return pd.DataFrame(
        {
            "application_id": np.arange(1, n + 1),
            MONTH: month,
            "annual_income": income,
            "dti": dti,
            "revolving_utilization": util,
            "delinq_2y": delinq.astype(np.int64),
            "inquiries_6m": inquiries.astype(np.int64),
            "credit_history_months": credit_history_months.astype(np.int64),
            "employment_years": employment_years.astype(np.int64),
            "loan_amount": loan_amount,
            "term_months": term.astype(np.int64),
            "purpose": np.array(PURPOSES, dtype=object)[purpose_idx],
            "region": np.array(REGIONS, dtype=object)[region_idx],
            PROTECTED: np.where(prot, "B", "A"),
            TARGET: bad,
        }
    )


def split_dev_oot(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Out-of-time split: development window vs the most recent six months."""
    dev = df[df[MONTH] < DEV_MONTHS].reset_index(drop=True)
    oot = df[df[MONTH] >= DEV_MONTHS].reset_index(drop=True)
    return dev, oot
