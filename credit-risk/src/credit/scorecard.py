"""Logistic-regression scorecard on WoE features with points-to-double-odds scaling.

Score = offset + factor * ln(odds_good), with factor = PDO / ln 2 and
offset = base_score - factor * ln(base_odds). With the defaults a score of 600 means
30:1 good:bad odds and every 20 points doubles the odds.

Because logit(PD_bad) = b0 + sum_i b_i * WoE_i, the score decomposes additively into
per-attribute points: points_ij = -factor * b_i * WoE_ij + (offset - factor * b0) / k.

Invariant: the protected attribute is rejected as a model input (``ProtectedAttributeError``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

import numpy as np
import pandas as pd
from sklearn.linear_model import LogisticRegression

from credit.config import BASE_ODDS, BASE_SCORE, MAX_BINS, MIN_BIN_FRAC, MIN_IV, PDO
from credit.data import PROTECTED
from credit.woe import WoEBinner

# Effectively unpenalised: a scorecard's coefficients should be the MLE on WoE inputs.
_LOGIT_C: Final[float] = 1e6
_LOGIT_MAX_ITER: Final[int] = 2000


@dataclass(frozen=True, slots=True)
class Scaling:
    """Linear map between ln(good:bad odds) and score points."""

    base_score: float = BASE_SCORE
    base_odds: float = BASE_ODDS  # good:bad
    pdo: float = PDO

    @property
    def factor(self) -> float:
        """Score points per unit of ln(good:bad odds): PDO / ln 2, so PDO points double odds."""
        return self.pdo / math.log(2)

    @property
    def offset(self) -> float:
        """Score at 1:1 odds (ln odds = 0). Chosen so base_odds maps exactly to base_score."""
        return self.base_score - self.factor * math.log(self.base_odds)

    def score_from_odds(self, odds_good: np.ndarray | float) -> np.ndarray | float:
        """Score for good:bad odds."""
        return self.offset + self.factor * np.log(odds_good)

    def odds_from_score(self, score: np.ndarray | float) -> np.ndarray | float:
        """Good:bad odds implied by a score (inverse of ``score_from_odds``)."""
        return np.exp((np.asarray(score) - self.offset) / self.factor)

    def pd_from_score(self, score: np.ndarray | float) -> np.ndarray | float:
        """Probability of default (bad) implied by a score."""
        return 1.0 / (1.0 + self.odds_from_score(score))


class ProtectedAttributeError(ValueError):
    """Raised when a protected attribute is offered as a model input."""


def assert_no_protected(features: Sequence[str]) -> None:
    """Raise ``ProtectedAttributeError`` if any protected attribute is among ``features``."""
    hit = {PROTECTED}.intersection(features)
    if hit:
        raise ProtectedAttributeError(f"protected attribute(s) not allowed as inputs: {hit}")


class Scorecard:
    """WoE logistic-regression scorecard with additive per-attribute points."""

    def __init__(
        self,
        numeric: Sequence[str],
        categorical: Sequence[str],
        scaling: Scaling | None = None,
        max_bins: int = MAX_BINS,
        min_bin_frac: float = MIN_BIN_FRAC,
        min_iv: float = MIN_IV,
    ) -> None:
        assert_no_protected([*numeric, *categorical])
        self.min_iv = min_iv
        self.numeric = list(numeric)
        self.categorical = list(categorical)
        self.scaling = scaling or Scaling()
        self.binner = WoEBinner(max_bins=max_bins, min_bin_frac=min_bin_frac)
        self.model = LogisticRegression(C=_LOGIT_C, max_iter=_LOGIT_MAX_ITER)
        # Populated by ``fit``.
        self.dropped_for_iv: list[str] = []
        self.dropped_for_sign: list[str] = []
        self.coef_: dict[str, float] = {}
        self.intercept_: float = 0.0
        self.points: dict[str, np.ndarray] = {}
        self.points_table: pd.DataFrame = pd.DataFrame()
        self.max_points: dict[str, float] = {}

    @property
    def features(self) -> list[str]:
        """Characteristics the model uses, numeric then categorical.

        Before ``fit`` this is the constructor input. Afterwards it excludes characteristics
        dropped for low IV or for a wrong-signed coefficient."""
        return self.numeric + self.categorical

    def fit(self, df: pd.DataFrame, y: pd.Series) -> Scorecard:
        """Bin, drop candidates with IV below ``min_iv``, then fit.

        Features whose coefficient has the wrong sign are dropped one at a time (most
        positive first) and the model refit. A positive coefficient on a WoE
        feature reverses the univariate risk ordering, typically through collinearity, and
        would produce counter-intuitive points and adverse action reasons."""
        self.binner.fit(df, y, self.numeric, self.categorical)
        ivs = {f: b.iv for f, b in self.binner.binnings.items()}
        self.dropped_for_iv = [f for f in self.features if ivs[f] < self.min_iv]
        self.numeric = [f for f in self.numeric if f not in self.dropped_for_iv]
        self.categorical = [f for f in self.categorical if f not in self.dropped_for_iv]
        woe = self.binner.transform(df)
        yv = np.asarray(y, dtype=int)
        self.dropped_for_sign = []
        while True:
            self.model.fit(woe[self.features].to_numpy(), yv)
            coefs = dict(zip(self.features, self.model.coef_[0], strict=True))
            wrong = {f: c for f, c in coefs.items() if c >= 0}
            if not wrong:
                break
            worst = max(wrong, key=wrong.get)
            self.dropped_for_sign.append(worst)
            self.numeric = [f for f in self.numeric if f != worst]
            self.categorical = [f for f in self.categorical if f != worst]
        self.coef_ = coefs
        self.intercept_ = float(self.model.intercept_[0])
        self._build_points()
        return self

    def _intercept_share(self) -> float:
        """Points each characteristic receives from the intercept (neutral WoE 0)."""
        s = self.scaling
        return (s.offset - s.factor * self.intercept_) / len(self.features)

    def _build_points(self) -> None:
        """Convert fitted coefficients into per-bin points and cache the results.

        A bin's points are ``-factor * coef * WoE`` plus that characteristic's equal share
        of the intercept, so summing one bin per characteristic yields the scaled score.
        """
        s = self.scaling
        shift = self._intercept_share()
        self.points = {}
        rows = []
        for f in self.features:
            b = self.binner.binnings[f]
            pts = -s.factor * self.coef_[f] * b.woe + shift
            self.points[f] = pts
            for label, w, p in zip(b.labels, b.woe, pts, strict=True):
                rows.append({"feature": f, "bin": label, "woe": w, "points": p})
        self.points_table = pd.DataFrame(rows)
        self.max_points = {f: float(p.max()) for f, p in self.points.items()}

    def points_matrix(self, df: pd.DataFrame) -> pd.DataFrame:
        """Points per applicant (rows) and characteristic (columns)."""
        # Unseen category -> neutral WoE 0 -> only the intercept share of points.
        neutral = self._intercept_share()
        out = {}
        for f in self.features:
            idx = self.binner.binnings[f].bin_index(df[f])
            pts = np.full(len(idx), neutral)
            known = idx >= 0
            pts[known] = self.points[f][idx[known]]
            out[f] = pts
        return pd.DataFrame(out, index=df.index)

    def score(self, df: pd.DataFrame) -> np.ndarray:
        """Total score per applicant (sum of characteristic points)."""
        return self.points_matrix(df).sum(axis=1).to_numpy()

    def predict_pd(self, df: pd.DataFrame) -> np.ndarray:
        """Probability of default implied by each applicant's score."""
        return np.asarray(self.scaling.pd_from_score(self.score(df)), dtype=float)

    def coefficients_all_negative(self) -> bool:
        """WoE scorecards should have negative coefficients (higher WoE -> lower PD)."""
        return all(c < 0 for c in self.coef_.values())
