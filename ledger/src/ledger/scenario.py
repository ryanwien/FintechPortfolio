"""Deterministic synthetic workload for the benchmark and the reconciliation run.

A month of customer deposits, payouts (some captured immediately, some held then
captured or failed) and monthly fees, all settling through one bank (cash)
account. The same seed always produces the same sequence of ledger calls.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Final

from ledger.config import SCENARIO_SEED
from ledger.domain import Direction, InsufficientFundsError, PostingSpec
from ledger.service import Ledger

DEFAULT_START: Final = date(2026, 9, 1)
DEPOSIT_SHARE: Final = 0.55  # roll < this: deposit
PAYOUT_SHARE_CUMULATIVE: Final = 0.92  # roll < this (and not a deposit): payout; else fee
MIN_DEPOSIT: Final = 10_00
MAX_DEPOSIT: Final = 5_000_00
MAX_CENTS_JITTER: Final = 99
MIN_PAYOUT: Final = 1_00
MAX_PAYOUT: Final = 3_000_00
IMMEDIATE_CAPTURE_PROBABILITY: Final = 0.7
CAPTURE_AFTER_HOLD_PROBABILITY: Final = 0.85  # otherwise the held payout fails
MONTHLY_FEE: Final = 2_50


@dataclass(frozen=True, slots=True)
class Scenario:
    """Ids and period of a generated scenario, plus counts of what was written."""

    cash_account_id: str
    wallet_ids: list[str]
    fee_account_id: str
    start: str
    end: str
    deposits: int
    payouts_posted: int
    payouts_failed: int
    fees: int


def build_settlement_scenario(
    ledger: Ledger,
    *,
    customers: int = 40,
    days: int = 30,
    movements_per_day: int = 40,
    start: date = DEFAULT_START,
    seed: int = SCENARIO_SEED,
    currency: str = "USD",
) -> Scenario:
    """Populate ``ledger`` with a deterministic month of settlement activity."""
    rng = random.Random(seed)
    cash = ledger.create_account("Operating cash @ Partner Bank", "asset", currency)["id"]
    fees = ledger.create_account("Fee revenue", "revenue", currency)["id"]
    wallets = [
        ledger.create_account(f"Customer wallet {i:03d}", "liability", currency)["id"]
        for i in range(customers)
    ]
    seq = deposits = payouts = failed = fee_count = 0
    for day in range(days):
        d = (start + timedelta(days=day)).isoformat()
        for _ in range(movements_per_day):
            seq += 1
            wallet = rng.choice(wallets)
            roll = rng.random()
            # Day 0 always deposits, so wallets have money for the payouts later in the month.
            if roll < DEPOSIT_SHARE or day == 0:
                # The cents jitter keeps amounts from clustering on round numbers, which would look
                # artificial.
                amount = rng.randint(MIN_DEPOSIT, MAX_DEPOSIT) + rng.randint(0, MAX_CENTS_JITTER)
                ledger.post_entry(
                    [
                        PostingSpec(cash, Direction.DEBIT, amount, currency),
                        PostingSpec(wallet, Direction.CREDIT, amount, currency),
                    ],
                    description="Customer deposit (ACH credit)",
                    effective_date=d,
                    external_ref=f"DEP-{seq:06d}",
                )
                deposits += 1
            elif roll < PAYOUT_SHARE_CUMULATIVE:
                # A payout is sized against available, not posted, so funds already under an open
                # hold cannot be spent twice.
                available = ledger.get_balance(wallet)["available"]
                # An empty or near-empty wallet just skips this movement. The rest of the month
                # still runs.
                if available < MIN_PAYOUT:
                    continue
                amount = rng.randint(MIN_PAYOUT, min(available, MAX_PAYOUT))
                # Some payouts are authorized first and captured or failed later, so the hold
                # lifecycle gets exercised.
                capture_now = rng.random() < IMMEDIATE_CAPTURE_PROBABILITY
                try:
                    pay = ledger.create_payment(
                        wallet,
                        cash,
                        amount,
                        currency,
                        capture=capture_now,
                        description="Customer payout",
                        external_ref=f"WD-{seq:06d}",
                        effective_date=d,
                    )
                # Skip this movement rather than abort the whole month over one refused payout.
                except InsufficientFundsError:
                    continue
                # Held payouts settle on a later step: most are captured, and the rest fail and
                # release their hold.
                if not capture_now:
                    if rng.random() < CAPTURE_AFTER_HOLD_PROBABILITY:
                        ledger.capture_payment(pay["id"], effective_date=d)
                    else:
                        ledger.fail_payment(pay["id"], reason="beneficiary bank rejected")
                        failed += 1
                        continue
                payouts += 1
            else:
                # The last branch is the monthly fee. It is charged only when the wallet can cover
                # it, so the fee call never raises.
                available = ledger.get_balance(wallet)["available"]
                if available >= MONTHLY_FEE:
                    ledger.create_payment(
                        wallet,
                        fees,
                        MONTHLY_FEE,
                        currency,
                        description="Monthly fee",
                        external_ref=f"FEE-{seq:06d}",
                        effective_date=d,
                    )
                    fee_count += 1
    end = (start + timedelta(days=days - 1)).isoformat()
    return Scenario(
        cash, wallets, fees, start.isoformat(), end, deposits, payouts, failed, fee_count
    )
