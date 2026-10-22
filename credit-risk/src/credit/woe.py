"""Weight-of-Evidence binning with monotonicity enforcement and Information Value.

Convention: WoE = ln(%goods / %bads). Higher WoE means lower risk. Numeric features start
from quantile fine-classing and adjacent bins are merged until (a) every bin holds at least
``min_bin_frac`` of the population and (b) WoE is monotonic in the direction implied by the
feature's rank correlation with default. Categorical features keep one bin per level, with
sparse levels pooled into ``__OTHER__``.

Invariants: numeric bins cover the whole real line, so every value maps to a bin; unseen
categorical levels map to ``__OTHER__`` when it exists, otherwise to index -1 (neutral WoE).
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field
from typing import Final, Literal

import numpy as np
import pandas as pd

from credit.config import CATEGORICAL_MIN_BIN_FRAC, MAX_BINS, MIN_BIN_FRAC

OTHER: Final[str] = "__OTHER__"
_EPS: Final[float] = 0.5  # Laplace-style smoothing so empty cells do not produce infinite WoE

# Siddiqi rule-of-thumb Information Value bands: (upper bound, label), checked in order.
_IV_BANDS: Final[tuple[tuple[float, str], ...]] = (
    (0.02, "unpredictive"),
    (0.1, "weak"),
    (0.3, "medium"),
    (0.5, "strong"),
)
_IV_SUSPICIOUS: Final[str] = "suspicious (check for leakage)"

BinKind = Literal["numeric", "categorical"]


def woe_iv(goods: np.ndarray, bads: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Return per-bin WoE and IV contributions for arrays of good and bad counts."""
    goods = np.asarray(goods, dtype=float) + _EPS
    bads = np.asarray(bads, dtype=float) + _EPS
    # Each side becomes a share of its class total; WoE compares the two distributions.
    dist_g = goods / goods.sum()
    dist_b = bads / bads.sum()
    woe = np.log(dist_g / dist_b)
    # IV sums (share_good - share_bad) * WoE over bins. Each term is >= 0 because the
    # two factors always share a sign, so IV is non-negative.
    iv = (dist_g - dist_b) * woe
    return woe, iv


@dataclass(frozen=True, slots=True)
class FeatureBinning:
    """Fitted bins for one characteristic, with per-bin counts, WoE and IV contributions."""

    feature: str
    kind: BinKind
    labels: list[str]
    woe: np.ndarray
    iv_parts: np.ndarray
    counts: np.ndarray
    bads: np.ndarray
    edges: np.ndarray | None = None  # numeric: interior cut points, bins are (e_{i-1}, e_i]
    category_map: dict[str, int] = field(default_factory=dict)  # categorical: level -> bin
    direction: int = 0  # +1: risk rises with x (WoE falls), -1: risk falls with x

    @property
    def iv(self) -> float:
        """Total Information Value of the characteristic."""
        return float(self.iv_parts.sum())

    def bin_index(self, x: pd.Series) -> np.ndarray:
        """Bin index per value; -1 for an unseen category when no ``__OTHER__`` bin exists."""
        if self.kind == "numeric":
            # side='left' puts a value equal to an edge in the lower bin, matching the
            # (e_{i-1}, e_i] interval labels.
            return np.searchsorted(self.edges, np.asarray(x, dtype=float), side="left")
        other = self.category_map.get(OTHER, -1)
        return np.array([self.category_map.get(str(v), other) for v in x], dtype=np.int64)

    def transform(self, x: pd.Series) -> np.ndarray:
        """WoE value per observation (unseen categories get neutral WoE 0)."""
        idx = self.bin_index(x)
        out = np.zeros(len(idx))
        known = idx >= 0
        out[known] = self.woe[idx[known]]
        return out  # unseen categories get neutral WoE 0

    def table(self) -> pd.DataFrame:
        """Per-bin characteristic analysis table."""
        n = self.counts.sum()
        return pd.DataFrame(
            {
                "feature": self.feature,
                "bin": self.labels,
                "count": self.counts,
                "pct": self.counts / n,
                "bad_rate": self.bads / np.maximum(self.counts, 1),
                "woe": self.woe,
                "iv": self.iv_parts,
            }
        )


def _is_monotonic(woe: np.ndarray, direction: int) -> bool:
    """Whether WoE moves strictly in the direction ``direction`` across adjacent bins.

    Direction +1 requires WoE to fall at every step (risk rising with the feature), and
    -1 requires it to rise. Equal neighbours count as a violation.
    """
    diffs = np.diff(woe)
    return bool(np.all(diffs < 0)) if direction > 0 else bool(np.all(diffs > 0))


def bin_numeric(
    x: pd.Series,
    y: pd.Series,
    feature: str,
    max_bins: int = MAX_BINS,
    min_bin_frac: float = MIN_BIN_FRAC,
) -> FeatureBinning:
    """Quantile fine-classing, then merge adjacent bins until sized and WoE-monotonic."""
    xv = np.asarray(x, dtype=float)
    yv = np.asarray(y, dtype=int)
    n = len(xv)
    # Start from up to max_bins quantile cuts so bins begin roughly equal in population.
    qs = np.quantile(xv, np.linspace(0, 1, max_bins + 1)[1:-1])
    # Repeated quantiles (heavy ties) collapse here, so no zero-width cut is created.
    edges = list(np.unique(qs))

    # Direction is the sign of the rank correlation with bad. A value >= 0 means risk
    # rises with x, so WoE has to fall as x rises.
    rank_corr = pd.Series(xv).rank().corr(pd.Series(yv).astype(float))
    # A constant feature gives NaN, which is treated as 0 and therefore direction +1.
    direction = 1 if (rank_corr if not math.isnan(rank_corr) else 0) >= 0 else -1

    def stats(e: list[float]) -> tuple[np.ndarray, np.ndarray]:
        """Good and bad counts per bin for the cut points ``e``.

        Bin i holds values in (e[i-1], e[i]], with open outer bins, so there are
        len(e) + 1 bins."""
        idx = np.searchsorted(np.asarray(e), xv, side="left")
        cnt = np.bincount(idx, minlength=len(e) + 1)
        bad = np.bincount(idx, weights=yv, minlength=len(e) + 1)
        return cnt, bad

    # Repair loop, one cut removed per pass. Checks run in order: no empty bins, then
    # every bin large enough, then monotone WoE. With no cuts left there is one bin.
    while edges:
        cnt, bad = stats(edges)
        # Drop empty bins produced by ties.
        # An empty bin means a cut has no observations on one side (common with ties).
        if np.any(cnt == 0):
            k = int(np.argmax(cnt == 0))
            edges.pop(min(k, len(edges) - 1))
            continue
        # Size floor. The first undersized bin merges into its smaller neighbour; the two
        # outer bins have only one neighbour, so they merge into it directly.
        small = np.where(cnt < min_bin_frac * n)[0]
        if len(small):
            k = int(small[0])
            # merge with the smaller neighbour
            if k == 0:
                edges.pop(0)
            elif k == len(cnt) - 1:
                edges.pop(-1)
            else:
                edges.pop(k - 1 if cnt[k - 1] <= cnt[k + 1] else k)
            continue
        # Reached only when every bin is populated and large enough. Goods are the count
        # minus bads.
        woe, _ = woe_iv(cnt - bad, bad)
        if _is_monotonic(woe, direction):
            break
        # Monotonicity repair: among the violating adjacent pairs, merge the one whose WoE
        # values are closest, so the merge moves the bin WoE values the least.
        diffs = np.diff(woe)
        bad_pairs = np.where(diffs >= 0)[0] if direction > 0 else np.where(diffs <= 0)[0]
        # merge the violating adjacent pair whose WoE values are closest
        k = int(bad_pairs[np.argmin(np.abs(diffs[bad_pairs]))])
        edges.pop(k)

    # Final counts for the surviving cuts; WoE and IV are stored from this pass.
    cnt, bad = stats(edges)
    woe, iv = woe_iv(cnt - bad, bad)
    e = np.asarray(edges, dtype=float)
    return FeatureBinning(
        feature=feature,
        kind="numeric",
        labels=_interval_labels(edges),
        woe=woe,
        iv_parts=iv,
        counts=cnt.astype(np.int64),
        bads=bad.astype(np.int64),
        edges=e,
        direction=direction,
    )


def _interval_labels(edges: list[float]) -> list[str]:
    """Human-readable ``(lo, hi]`` labels for the bins defined by interior ``edges``."""
    labels = []
    for lo, hi in itertools.pairwise([-np.inf, *edges, np.inf]):
        lo_s = "-inf" if np.isinf(lo) else f"{lo:g}"
        hi_s = "inf" if np.isinf(hi) else f"{hi:g}"
        labels.append(f"({lo_s}, {hi_s}]")
    return labels


def bin_categorical(
    x: pd.Series, y: pd.Series, feature: str, min_bin_frac: float = CATEGORICAL_MIN_BIN_FRAC
) -> FeatureBinning:
    """One bin per level (riskiest first), with levels rarer than ``min_bin_frac`` pooled."""
    # Strings on both sides, so level keys match the category_map used at transform time.
    xs = pd.Series(np.asarray(x, dtype=object)).astype(str)
    freq = xs.value_counts(normalize=True)
    # Rare levels are pooled into __OTHER__ before WoE is computed, so a near-empty
    # level cannot produce an extreme WoE.
    sparse = set(freq[freq < min_bin_frac].index)
    grouped = xs.where(~xs.isin(sparse), OTHER)
    yv = pd.Series(np.asarray(y, dtype=int))
    agg = pd.DataFrame({"g": grouped, "y": yv}).groupby("g")["y"].agg(["count", "sum"])
    woe, iv = woe_iv((agg["count"] - agg["sum"]).to_numpy(), agg["sum"].to_numpy())
    order = np.argsort(woe)  # riskiest first for readability
    levels = [str(v) for v in agg.index.to_numpy()[order]]
    bin_of_level = {lvl: i for i, lvl in enumerate(levels)}
    # Every observed level maps to a bin; pooled levels map to the __OTHER__ bin, so
    # transform sees exactly the bins that fit produced.
    category_map = dict(bin_of_level)
    for lvl in sparse:
        category_map[lvl] = bin_of_level[OTHER]
    return FeatureBinning(
        feature=feature,
        kind="categorical",
        labels=levels,
        woe=woe[order],
        iv_parts=iv[order],
        counts=agg["count"].to_numpy()[order].astype(np.int64),
        bads=agg["sum"].to_numpy()[order].astype(np.int64),
        category_map=category_map,
    )


class WoEBinner:
    """Fits and applies a ``FeatureBinning`` per characteristic."""

    def __init__(self, max_bins: int = MAX_BINS, min_bin_frac: float = MIN_BIN_FRAC) -> None:
        self.max_bins = max_bins
        self.min_bin_frac = min_bin_frac
        self.binnings: dict[str, FeatureBinning] = {}

    def fit(
        self, df: pd.DataFrame, y: pd.Series, numeric: list[str], categorical: list[str]
    ) -> WoEBinner:
        """Bin every characteristic: numeric via ``bin_numeric``, categorical via
        ``bin_categorical``.

        ``y`` is the bad flag (1 = bad). Results are stored in ``binnings`` keyed by
        feature name."""
        for f in numeric:
            self.binnings[f] = bin_numeric(df[f], y, f, self.max_bins, self.min_bin_frac)
        for f in categorical:
            self.binnings[f] = bin_categorical(df[f], y, f)
        return self

    def transform(self, df: pd.DataFrame) -> pd.DataFrame:
        """WoE-encode every fitted characteristic of ``df``."""
        return pd.DataFrame({f: b.transform(df[f]) for f, b in self.binnings.items()})

    def iv_table(self) -> pd.DataFrame:
        """Characteristics ranked by Information Value."""
        rows = [{"feature": f, "iv": b.iv, "bins": len(b.labels)} for f, b in self.binnings.items()]
        return pd.DataFrame(rows).sort_values("iv", ascending=False).reset_index(drop=True)


def iv_strength(iv: float) -> str:
    """Conventional Siddiqi rule-of-thumb labels for Information Value."""
    for upper, label in _IV_BANDS:
        if iv < upper:
            return label
    return _IV_SUSPICIOUS
