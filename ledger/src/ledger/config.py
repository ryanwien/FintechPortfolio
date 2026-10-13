"""Seeds, workload sizes and injection rates for the benchmark and scenarios.

Everything that determines the deterministic part of ``python -m ledger.bench``
lives here, so a reviewer can see in one place what a published number was
produced with. Changing any value changes the published results.
"""

from __future__ import annotations

from typing import Final

from ledger.statement import InjectionRates

# --------------------------------------------------------------------- seeds
BENCH_SEED: Final = 20261013
SCENARIO_SEED: Final = 11
RECON_STANDARD_SEED: Final = 7
RECON_STRESS_SEED: Final = 8

# ----------------------------------------------------------- transfer load
BENCH_CURRENCY: Final = "USD"
WALLETS: Final = 200
FUNDING: Final = 10_000_000_00  # $10M per wallet: transfers never hit the overdraft rule here
MAX_TRANSFER_AMOUNT: Final = 50_000
TRANSFERS_PER_LEVEL: Final = 4000
TRANSFERS_PER_LEVEL_QUICK: Final = 500
THREAD_LEVELS: Final = (1, 2, 4, 8, 16)
THREAD_LEVELS_QUICK: Final = (1, 2, 4, 8)

# ------------------------------------------------- lifecycle & idempotency
LIFECYCLE_WALLETS: Final = 20
LIFECYCLE_OPS: Final = 1000
LIFECYCLE_OPS_QUICK: Final = 200
MAX_LIFECYCLE_AMOUNT: Final = 10_000
REPLAY_AMOUNT: Final = 100
# Overdraft probe: place holds of 1/OVERDRAFT_HOLD_FRACTION of a balance, this many times.
OVERDRAFT_HOLD_FRACTION: Final = 7
OVERDRAFT_HOLD_ATTEMPTS: Final = 10

# ---------------------------------------------------------- reconciliation
RECON_DAYS: Final = 30
RECON_MOVEMENTS_PER_DAY: Final = 60
RECON_MOVEMENTS_PER_DAY_QUICK: Final = 10
STANDARD_RATES: Final = InjectionRates()
# 4x breaks; 35% of memos stripped of the reference, settling 0-3 days late, so some
# fall outside the 1-day matching tolerance with no reference to fall back on.
STRESS_RATES: Final = InjectionRates(
    missing_in_bank=0.08,
    amount_mismatch=0.06,
    timing=0.12,
    duplicate=0.04,
    missing_in_ledger=0.04,
    stripped_reference=0.35,
    stripped_max_lag_days=3,
)
