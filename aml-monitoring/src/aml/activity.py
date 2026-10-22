"""Per-customer activity index shared by the rules engine and graph analytics.

Inflows and outflows are kept in world (time) order with prefix sums, so any window sum
is two bisections. Own-account transfers (same customer on both sides) are excluded.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from .model import Txn, World


def _cumsum(txns: list[Txn]) -> list[int]:
    """Prefix sums with a leading zero, so ``cum[j] - cum[i]`` sums ``txns[i:j]``."""
    out = [0]
    for t in txns:
        out.append(out[-1] + t.amount_cents)
    return out


class Activity:
    """Per-customer inflows/outflows with prefix sums for O(log n) window sums.

    Append to ``inflows`` / ``outflows`` in time order, then call ``finalize`` once
    before querying.
    """

    def __init__(self) -> None:
        self.inflows: list[Txn] = []
        self.outflows: list[Txn] = []
        self.in_ts: list[int] = []
        self.out_ts: list[int] = []
        self._in_cum: list[int] = [0]
        self._out_cum: list[int] = [0]

    def finalize(self) -> None:
        """Build the timestamp index and prefix sums; call once after all flows are appended."""
        self.in_ts = [t.ts for t in self.inflows]
        self.out_ts = [t.ts for t in self.outflows]
        self._in_cum = _cumsum(self.inflows)
        self._out_cum = _cumsum(self.outflows)

    def in_sum(self, lo: int, hi: int) -> int:
        """Total inflow with ``lo <= ts <= hi``."""
        i, j = bisect_left(self.in_ts, lo), bisect_right(self.in_ts, hi)
        return self._in_cum[j] - self._in_cum[i]

    def out_sum(self, lo: int, hi: int) -> int:
        """Total outflow with ``lo <= ts <= hi``."""
        i, j = bisect_left(self.out_ts, lo), bisect_right(self.out_ts, hi)
        return self._out_cum[j] - self._out_cum[i]

    def ins_between(self, lo: int, hi: int) -> list[Txn]:
        """Inflows with ``lo <= ts <= hi``, in time order."""
        return self.inflows[bisect_left(self.in_ts, lo) : bisect_right(self.in_ts, hi)]

    def outs_between(self, lo: int, hi: int) -> list[Txn]:
        """Outflows with ``lo <= ts <= hi``, in time order."""
        return self.outflows[bisect_left(self.out_ts, lo) : bisect_right(self.out_ts, hi)]


def index_activity(world: World) -> dict[str, Activity]:
    """Index every customer's activity (customers in id order, flows in time order)."""
    acts: dict[str, Activity] = {cid: Activity() for cid in sorted(world.customers)}
    for t in world.txns:
        src, dst = world.owner(t.src), world.owner(t.dst)
        if src is not None and src == dst:
            continue  # own-account transfer
        if dst is not None:
            acts[dst].inflows.append(t)
        if src is not None:
            acts[src].outflows.append(t)
    for a in acts.values():
        a.finalize()
    return acts
