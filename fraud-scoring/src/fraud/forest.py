"""Flat-array inference for a fitted HistGradientBoostingClassifier.

sklearn's ``predict_proba`` costs ~5 ms per call on small batches (input validation, the
categorical preprocessor and OpenMP start-up dominate). For online scoring we copy every tree
into a handful of NumPy arrays once and walk all trees for all rows in lock-step. The output
matches ``predict_proba`` to floating-point round-off (``tests/test_forest.py``).

Version pin (read before upgrading scikit-learn)
-----------------------------------------------
This module reads scikit-learn *private* attributes of a fitted estimator:

* ``_predictors``           - per-iteration ``TreePredictor`` objects (``nodes`` record array,
                              ``raw_left_cat_bitsets``, ``get_max_depth``)
* ``_preprocessor``         - the ``ColumnTransformer`` that ordinal-encodes categoricals
* ``_bin_mapper``           - ``make_known_categories_bitsets()``
* ``_baseline_prediction``  - the initial raw score

None of these are public API, so the code is pinned to :data:`SKLEARN_VERSION`, the exact
version in ``requirements.txt``/``pyproject.toml``. ``tests/test_forest.py`` asserts the
installed version matches the pin and that compiled output equals ``predict_proba`` to 1e-12
(including NaNs and unseen / negative categories). To upgrade scikit-learn: bump the pin,
run that test, and fix any attribute changes it exposes.
"""

from __future__ import annotations

from typing import Final

import numpy as np
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.preprocessing import OrdinalEncoder

SKLEARN_VERSION: Final = "1.9.1"  # private-attribute layout this module was written against
_CHUNK: Final = 4_096  # rows per lock-step walk; bounds the (rows x trees) node matrix
_N_CAT_CODES: Final = 256  # HGB categorical codes are uint8; column 256 = missing/negative
_BITSET_WORDS: Final = _N_CAT_CODES // 32


class CompiledForest:
    """A fitted binary ``HistGradientBoostingClassifier`` flattened into NumPy arrays.

    All trees are concatenated into one node table; leaves point to themselves so every row
    walks exactly ``depth`` steps. Categorical splits become a ``[node, code]`` lookup table.
    """

    def __init__(self, est: HistGradientBoostingClassifier) -> None:
        """Flatten the fitted estimator's trees, categorical maps and bitsets into NumPy arrays.

        Raises ``ValueError`` for a model that is not binary classification.
        """
        # One tree per boosting round; multiclass would need one tree per class per round.
        if est.n_trees_per_iteration_ != 1:
            raise ValueError("binary classification only")
        n_in = est.n_features_in_
        perm: list[int] = []
        cat_maps: dict[int, np.ndarray] = {}
        # sklearn moves categorical columns and ordinal-encodes them; rebuild that column order.
        pre = getattr(est, "_preprocessor", None)
        if pre is None:
            perm = list(range(n_in))
        else:
            # perm lists, for each estimator input slot, the caller column that feeds it.
            for name, trans, cols in pre.transformers_:
                if name == "remainder" and trans == "drop":
                    continue
                idx = np.flatnonzero(cols) if np.asarray(cols).dtype == bool else list(cols)
                if isinstance(trans, OrdinalEncoder):
                    for k in range(len(idx)):
                        cat_maps[len(perm) + k] = np.asarray(trans.categories_[k], dtype=float)
                perm.extend(int(i) for i in idx)
        self.perm = np.array(perm, dtype=np.int64)
        self.cat_maps = cat_maps

        # Categories seen in training, per feature: anything outside this set is an unseen code.
        known, f_idx_map = est._bin_mapper.make_known_categories_bitsets()
        self.known_bitsets = np.asarray(known, dtype=np.uint32)
        self.f_idx_map = np.asarray(f_idx_map, dtype=np.int64)

        # Each tree's node table is appended to these lists, then concatenated into one table.
        feat, thr, left, right, leaf, is_cat, miss_left, bidx, value = ([] for _ in range(9))
        bitsets, roots = [], []
        node_off = bit_off = 0
        for (pred,) in est._predictors:
            nodes = pred.nodes
            n = len(nodes)
            # A tree's root is the first row of its block in the flat table.
            roots.append(node_off)
            feat.append(nodes["feature_idx"].astype(np.int64))
            thr.append(nodes["num_threshold"].astype(np.float64))
            # sklearn child pointers are per tree; the offset makes them index the flat table.
            left.append(nodes["left"].astype(np.int64) + node_off)
            right.append(nodes["right"].astype(np.int64) + node_off)
            leaf.append(nodes["is_leaf"].astype(bool))
            is_cat.append(nodes["is_categorical"].astype(bool))
            miss_left.append(nodes["missing_go_to_left"].astype(bool))
            bidx.append(nodes["bitset_idx"].astype(np.int64) + bit_off)
            value.append(nodes["value"].astype(np.float64))
            # A categorical split stores its left-going codes as a bitset of uint32 words.
            raw_bits = np.asarray(pred.raw_left_cat_bitsets, dtype=np.uint32).reshape(
                -1, _BITSET_WORDS
            )
            bitsets.append(raw_bits)
            # Advance both offsets past this tree so the next tree's indices do not collide.
            node_off += n
            bit_off += len(raw_bits)
        # Concatenate the per-tree arrays into the flat tables that inference indexes.
        self.feat = np.concatenate(feat)
        self.thr = np.concatenate(thr)
        self.left = np.concatenate(left)
        self.right = np.concatenate(right)
        is_leaf = np.concatenate(leaf)
        is_cat = np.concatenate(is_cat)
        self.miss_left = np.concatenate(miss_left)
        bidx = np.concatenate(bidx)
        self.value = np.concatenate(value)
        raw_bits = np.concatenate(bitsets) if bit_off else np.zeros((1, _BITSET_WORDS), np.uint32)
        self.roots = np.array(roots, dtype=np.int64)
        self.baseline = float(np.asarray(est._baseline_prediction).ravel()[0])

        # Leaves loop to themselves, so every row can take the same number of steps.
        ids = np.arange(len(self.feat))
        self.left[is_leaf] = ids[is_leaf]
        self.right[is_leaf] = ids[is_leaf]
        self.feat[is_leaf] = 0
        self.thr[is_leaf] = np.inf
        # The lock-step walk runs as deep as the deepest tree; shallower trees wait on leaves.
        self.depth = int(max(p.get_max_depth() for (p,) in est._predictors))

        # Categorical splits -> lookup table [cat node, code]; last column = missing/negative.
        cat_nodes = np.flatnonzero(is_cat & ~is_leaf)
        self.cat_id = np.full(len(self.feat), -1, dtype=np.int64)
        self.cat_id[cat_nodes] = np.arange(len(cat_nodes))
        # Precompute each categorical split's answer for every code, so inference is a lookup.
        table = np.zeros((max(1, len(cat_nodes)), _N_CAT_CODES + 1), dtype=bool)
        codes = np.arange(_N_CAT_CODES)
        for k, nd in enumerate(cat_nodes):
            # Read each code's bit from the split's left-set bitset (32 codes per word).
            in_left = (raw_bits[bidx[nd], codes >> 5] >> (codes & 31).astype(np.uint32)) & 1
            # Same bit trick on the training-time known set: separates seen codes from unseen ones.
            kb = self.known_bitsets[self.f_idx_map[self.feat[nd]], codes >> 5]
            known = (kb >> (codes & 31).astype(np.uint32)) & 1
            # Unseen codes and the reserved missing slot both follow this node's missing direction.
            ml = bool(self.miss_left[nd])
            table[k, :_N_CAT_CODES] = np.where(in_left == 1, True, np.where(known == 1, False, ml))
            table[k, _N_CAT_CODES] = ml
        self.cat_table = table
        self.has_cat = len(cat_nodes) > 0

    def _transform(self, X: np.ndarray) -> np.ndarray:
        """Reorder columns as the estimator saw them and ordinal-encode categoricals."""
        # Fancy indexing copies, so the caller's array is untouched, in estimator column order.
        z = np.asarray(X, dtype=np.float64)[:, self.perm]
        for pos, cats in self.cat_maps.items():
            v = z[:, pos]
            # Position of a value in the sorted category list is its ordinal code.
            code = np.searchsorted(cats, v)
            safe = np.minimum(code, len(cats) - 1)
            # A value is known only if it equals the category at that position; else it is missing.
            ok = (code < len(cats)) & (cats[safe] == v)
            z[:, pos] = np.where(ok, code, np.nan)  # unknown category -> missing
        return z

    def _raw_chunk(self, z: np.ndarray) -> np.ndarray:
        """Walk all trees over one chunk of rows in lock-step and return raw logit scores.

        ``z`` must already be column-ordered and ordinal-encoded by :meth:`_transform`.
        """
        b = len(z)
        # Every row starts at the root of every tree: a (rows x trees) matrix of node ids.
        node = np.broadcast_to(self.roots, (b, len(self.roots))).copy()
        rows = np.arange(b)[:, None]
        # One level per step for all rows and trees; leaves self-loop, so extra steps do nothing.
        for _ in range(self.depth):
            # The feature value each (row, tree) pair tests at its current node.
            v = z[rows, self.feat[node]]
            # Numeric split; NaN compares False here and is corrected below.
            go_left = v <= self.thr[node]
            if self.has_cat:
                # Categorical nodes skip the threshold and use their precomputed table instead.
                cid = self.cat_id[node]
                cat = cid >= 0
                if cat.any():
                    vc = v[cat]
                    # Codes outside 0..255, and NaN, go to the reserved last column (missing slot).
                    code = np.where((vc >= 0) & (vc < _N_CAT_CODES), vc, _N_CAT_CODES)
                    code = np.nan_to_num(code, nan=_N_CAT_CODES).astype(np.int64)
                    go_left[cat] = self.cat_table[cid[cat], code]
            # Missing values follow the direction learned for each node during training.
            nan = np.isnan(v)
            if nan.any():
                go_left = np.where(nan, self.miss_left[node], go_left)
            # Advance every (row, tree) pair to its chosen child in one vectorised step.
            node = np.where(go_left, self.left[node], self.right[node])
        # Sum the reached leaves over trees and add the initial score: the raw logit, as sklearn.
        return self.baseline + self.value[node].sum(axis=1)

    def raw(self, X: np.ndarray) -> np.ndarray:
        """Raw (logit) scores, identical to the estimator's ``decision_function``."""
        z = self._transform(X)
        # Chunk large batches so the (rows x trees) node matrix stays bounded in size.
        if len(z) <= _CHUNK:
            return self._raw_chunk(z)
        return np.concatenate(
            [self._raw_chunk(z[i : i + _CHUNK]) for i in range(0, len(z), _CHUNK)]
        )

    def predict_proba1(self, X: np.ndarray) -> np.ndarray:
        """P(class 1) for each row."""
        return 1.0 / (1.0 + np.exp(-self.raw(X)))
