"""Property-based tests: Hypothesis drives random sequences of ledger operations
and checks, after every step, that

* the trial balance nets to zero in every currency,
* no account that disallows negatives has a negative available balance,
* cached balances equal balances re-derived from the journal and open holds,
* every balance equals an independent in-test model of what it should be.

Operations that the ledger rejects (overdraft, illegal transition, ...) must
leave every balance exactly as it was.
"""

from __future__ import annotations

from collections import defaultdict

import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, precondition, rule

from ledger.balances import compute_balances
from ledger.domain import Direction, LedgerError, PostingSpec
from ledger.lifecycle import PaymentStatus
from ledger.service import Ledger

CURRENCIES = ("USD", "EUR")
amounts = st.integers(min_value=1, max_value=50_000)


class LedgerMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.ledger = Ledger(":memory:")
        # model: account -> [posted_debits, posted_credits, pending_debits, pending_credits]
        self.model: dict[str, list[int]] = defaultdict(lambda: [0, 0, 0, 0])
        self.ccy: dict[str, str] = {}
        self.wallets: dict[str, list[str]] = defaultdict(list)
        self.payments: dict[str, tuple[str, str, int, PaymentStatus]] = {}
        self.manual_entries: list[str] = []
        self.banks: dict[str, str] = {}
        self.rejections = 0

    @initialize()
    def setup(self) -> None:
        for c in CURRENCIES:
            bank = self.ledger.create_account(f"bank-{c}", "asset", c)["id"]
            self.ccy[bank] = c
            self.banks[c] = bank
            for i in range(3):
                w = self.ledger.create_account(
                    f"w{i}-{c}", "liability", c, allow_negative=(i == 2)
                )["id"]  # one credit-line account
                self.ccy[w] = c
                self.wallets[c].append(w)

    # ------------------------------------------------------------ helpers

    def _snapshot(self) -> dict[str, dict]:
        return {a: self.ledger.get_balance(a) for a in self.ccy}

    def _attempt(self, fn, *args, **kwargs):
        before = self._snapshot()
        try:
            return fn(*args, **kwargs)
        except LedgerError:
            self.rejections += 1
            after = self._snapshot()
            assert {
                a: {k: v for k, v in b.items() if k != "lock_version"} for a, b in before.items()
            } == {a: {k: v for k, v in b.items() if k != "lock_version"} for a, b in after.items()}
            return None

    # -------------------------------------------------------------- rules

    @rule(c=st.sampled_from(CURRENCIES), i=st.integers(0, 2), amount=amounts)
    def deposit(self, c: str, i: int, amount: int) -> None:
        w = self.wallets[c][i]
        self.ledger.post_entry(
            [
                PostingSpec(self.banks[c], Direction.DEBIT, amount, c),
                PostingSpec(w, Direction.CREDIT, amount, c),
            ]
        )
        self.model[self.banks[c]][0] += amount
        self.model[w][1] += amount

    @rule(
        c=st.sampled_from(CURRENCIES),
        i=st.integers(0, 2),
        j=st.integers(0, 2),
        amount=amounts,
        capture=st.booleans(),
    )
    def pay(self, c: str, i: int, j: int, amount: int, capture: bool) -> None:
        if i == j:
            return
        src, dst = self.wallets[c][i], self.wallets[c][j]
        p = self._attempt(self.ledger.create_payment, src, dst, amount, c, capture=capture)
        if p is None:
            return
        if capture:
            self.model[src][0] += amount
            self.model[dst][1] += amount
            self.payments[p["id"]] = (src, dst, amount, PaymentStatus.POSTED)
        else:
            self.model[src][2] += amount
            self.model[dst][3] += amount
            self.payments[p["id"]] = (src, dst, amount, PaymentStatus.PENDING)

    @rule(c=st.sampled_from(CURRENCIES), i=st.integers(0, 2), amount=amounts)
    def withdraw(self, c: str, i: int, amount: int) -> None:
        """Payout to the outside world: debit wallet, credit bank (bank must not go negative)."""
        w = self.wallets[c][i]
        e = self._attempt(
            self.ledger.post_entry,
            [
                PostingSpec(w, Direction.DEBIT, amount, c),
                PostingSpec(self.banks[c], Direction.CREDIT, amount, c),
            ],
        )
        if e is not None:
            self.model[w][0] += amount
            self.model[self.banks[c]][1] += amount
            self.manual_entries.append(e["id"])

    @precondition(lambda self: self.payments)
    @rule(data=st.data(), action=st.sampled_from(["capture", "fail", "reverse"]))
    def advance_payment(self, data: st.DataObject, action: str) -> None:
        # Draw by creation order, not sorted(): ids are random UUIDs, so a sorted order would
        # differ when Hypothesis replays an example and make data generation non-reproducible.
        pid = data.draw(st.sampled_from(list(self.payments)))
        src, dst, amt, status = self.payments[pid]
        fn = {
            "capture": self.ledger.capture_payment,
            "fail": self.ledger.fail_payment,
            "reverse": self.ledger.reverse_payment,
        }[action]
        res = self._attempt(fn, pid)
        legal = {
            ("capture", PaymentStatus.PENDING),
            ("fail", PaymentStatus.PENDING),
            ("reverse", PaymentStatus.POSTED),
        }
        if (action, status) not in legal:
            assert res is None, f"illegal {action} from {status} was accepted"
            return
        if res is None:  # legal transition but rejected (reversal would overdraw destination)
            assert action == "reverse"
            return
        if action == "capture":
            self.model[src][2] -= amt
            self.model[dst][3] -= amt
            self.model[src][0] += amt
            self.model[dst][1] += amt
        elif action == "fail":
            self.model[src][2] -= amt
            self.model[dst][3] -= amt
        else:
            self.model[src][1] += amt
            self.model[dst][0] += amt
        self.payments[pid] = (src, dst, amt, PaymentStatus(res["status"]))

    @precondition(lambda self: self.manual_entries)
    @rule(data=st.data())
    def reverse_manual(self, data: st.DataObject) -> None:
        eid = data.draw(st.sampled_from(self.manual_entries))
        rev = self._attempt(self.ledger.reverse_entry, eid)
        if rev is not None:
            for p in rev["postings"]:
                self.model[p["account_id"]][0 if p["direction"] == "debit" else 1] += p["amount"]
            self.manual_entries.remove(eid)

    @rule(c=st.sampled_from(CURRENCIES), amount=amounts)
    def unbalanced_is_rejected(self, c: str, amount: int) -> None:
        res = self._attempt(
            self.ledger.post_entry,
            [
                PostingSpec(self.banks[c], Direction.DEBIT, amount + 1, c),
                PostingSpec(self.wallets[c][0], Direction.CREDIT, amount, c),
            ],
        )
        assert res is None

    # --------------------------------------------------------- invariants

    @invariant()
    def books_balance(self) -> None:
        if not self.ccy:
            return
        report = self.ledger.verify()
        assert report["unbalanced_currencies"] == []
        assert all(v["net"] == 0 for v in report["trial_balance"].values())
        assert report["disallowed_overdrafts"] == []
        assert report["cache_mismatches"] == []

    @invariant()
    def matches_model(self) -> None:
        for acct in self.ccy:
            got = self.ledger.get_account(acct)
            expected = compute_balances(got["type"], *self.model[acct])
            assert got["balances"] == expected, acct

    def teardown(self) -> None:
        self.ledger.close()


LedgerMachine.TestCase.settings = settings(
    max_examples=60,
    stateful_step_count=40,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
TestLedgerStateMachine = LedgerMachine.TestCase


@settings(max_examples=200, deadline=None)
@given(
    legs=st.lists(
        st.tuples(st.sampled_from(CURRENCIES), st.sampled_from(list(Direction)), amounts),
        min_size=1,
        max_size=8,
    ),
    make_balanced=st.booleans(),
)
def test_entry_accepted_iff_balanced_per_currency(
    legs: list[tuple[str, Direction, int]], make_balanced: bool
) -> None:
    if make_balanced:  # half the examples: append the legs that balance each currency
        net0: dict[str, int] = defaultdict(int)
        for c, d, a in legs:
            net0[c] += a if d is Direction.DEBIT else -a
        legs = legs + [
            (c, Direction.CREDIT if n > 0 else Direction.DEBIT, abs(n))
            for c, n in net0.items()
            if n
        ]
    lg = Ledger(":memory:")
    try:
        acct = {
            c: lg.create_account(f"a-{c}", "asset", c, allow_negative=True)["id"]
            for c in CURRENCIES
        }
        net: dict[str, int] = defaultdict(int)
        for c, d, a in legs:
            net[c] += a if d is Direction.DEBIT else -a
        balanced = len(legs) >= 2 and all(v == 0 for v in net.values())
        try:
            lg.post_entry([PostingSpec(acct[c], d, a, c) for c, d, a in legs])
            accepted = True
        except LedgerError:
            accepted = False
        assert accepted == balanced
        assert all(v["net"] == 0 for v in lg.trial_balance().values())
    finally:
        lg.close()
