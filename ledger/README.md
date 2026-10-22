# Double-entry ledger & payments API

An append-only, double-entry ledger with an idempotent payments API, authorization
holds, a payment state machine, and an automated bank reconciliation job, built on
FastAPI and SQLite. It is tested with property-based and multi-threaded tests.

## Why this matters in fintech

Every wallet, card program, marketplace payout and lending product sits on a ledger.
When a ledger has bugs, the outcomes are some of the worst in the industry: money
created or destroyed, double-charged customers after a client retry, overdrafts on
accounts that were never allowed to go negative, and month-end books that nobody can
reconcile with the bank. This project covers the guarantees that payments
infrastructure teams (Stripe, Modern Treasury, neobank core-banking teams) are
accountable for:

- **Conservation.** Every entry balances per currency, so the trial balance is always zero.
- **Immutability.** History is never edited. Corrections are reversing entries.
- **Exactly-once effects.** Retries with an `Idempotency-Key` cannot move money twice.
- **No unauthorized overdrafts.** This holds even when concurrent requests race on one account.
- **Explainable reconciliation.** Every cent of difference between ledger and bank is attributed to a classified break.

## Architecture

```
            HTTP (FastAPI, pydantic StrictInt money, Idempotency-Key header)
                                   │
   POST /v1/accounts   POST /v1/entries   POST /v1/payments   …/capture …/fail …/reverse
   GET  …/balance      GET  /v1/entries   POST /v1/entries/{id}/reverse   GET /v1/ledger/verify
                                   │
                                   ▼
 ┌──────────────────────── Ledger facade (service.py) ─────────────────────────────────┐
 │  one BEGIN IMMEDIATE transaction per operation:                                     │
 │   1. idempotency lookup (same key+body → stored response; different body → 409)    │
 │   2. validate: integer minor units, per-currency balance, account currency          │
 │   3. payment state machine (lifecycle.py)  pending ─▶ posted ─▶ reversed            │
 │                                              └──▶ failed                            │
 │   4. append entry + postings, or place/release a hold                               │
 │   5. cached balances: overdraft check on *available*, CAS on lock_version           │
 │   6. store idempotency record, COMMIT, all in one atomic transaction                │
 └─────────────────────────────────────────────────────────────────────────────────────┘
                                   │
                                   ▼
 SQLite (WAL, synchronous=FULL)
   accounts(cached posted/pending debits+credits, lock_version)   payments   payment_events
   entries ──< postings   (UPDATE/DELETE blocked by triggers; reverses_entry_id UNIQUE)
   idempotency_keys
                                   │
                                   ▼
 recon.py: cash-account activity ──▶ matcher ◀── bank statement (synthetic, injected breaks)
           pass 1 reference · pass 2 amount+date · pass 3 leftovers ──▶ classified report
```

**Module layout** (`src/ledger/`):

| module | owns |
|---|---|
| `service.py` | `Ledger` facade: input validation, transaction boundary per operation, composition |
| `storage.py` | connections, `BEGIN IMMEDIATE` transactions, in-process write mutex |
| `idempotency.py` | key validation, request fingerprint, stored-response replay |
| `journal.py` | posting validation, balanced-entry writes, reversals |
| `balances.py` | cached totals, available/pending/posted, overdraft + `lock_version` CAS update |
| `payments.py` | payment lifecycle: holds, capture, fail, reverse, `payment_events` audit |
| `lifecycle.py` | payment state machine |
| `integrity.py` | `verify()`: trial balance, cache vs. journal re-derivation, overdrafts |
| `repository.py` | SQL row loaders, inserts and JSON views |
| `domain.py`, `schema.py`, `clock.py` | domain types and errors, SQLite schema and triggers, time source |
| `api.py` | FastAPI HTTP layer |
| `recon.py`, `statement.py` | reconciliation matcher and scoring; synthetic bank statement generator |
| `scenario.py`, `config.py` | deterministic settlement workload; benchmark seeds, sizes and rates |
| `bench.py`, `report.py` | benchmark orchestration; `metrics.json` / `REPORT.md` / `site.json` rendering |

**Balances.** Each account caches four running totals: posted debits, posted credits,
pending debits and pending credits. The ledger exposes three numbers in the account's
normal direction:

| balance | definition | used for |
|---|---|---|
| `posted` | settled journal postings | statements, reconciliation |
| `pending` | posted ± every open hold | showing the customer what is in flight |
| `available` | posted − open *outbound* holds | the overdraft rule |

`verify()` re-derives all four totals from `postings` and pending `payments`, compares
them with the cache, and checks the trial balance and overdraft rules on one consistent
snapshot. The `/v1/ledger/verify` endpoint exposes the same check.

## Run it

```bash
cd ledger
pip install -r requirements.txt -e .   # from the repo's virtualenv
ruff check . && pytest
python -m ledger.bench             # ~1 min; regenerates everything in results/
python -m ledger.api               # API on http://127.0.0.1:8000/docs
```

```bash
curl -s -X POST localhost:8000/v1/payments \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: order-1042' \
  -d '{"source_account_id":"acct_…","destination_account_id":"acct_…","amount":1250,"currency":"USD","capture":false}'
```

## Results

These figures come from `results/metrics.json` and `results/REPORT.md`, produced by
`python -m ledger.bench` on Windows 11, Python 3.14.0 and SQLite 3.50.4. Storage is a
file-backed SQLite database in WAL mode with `synchronous=FULL`. Latency is measured
around `create_payment` in the service layer. Each transfer writes 1 payment, 1 entry,
2 postings, 2 payment events, 1 idempotency record and 2 balance updates in a single
transaction.

**Transfers (4,000 per level, 200 wallets, random pairs, all with idempotency keys)**

| threads | transfers/s | postings/s | p50 ms | p95 ms | p99 ms | invariants + money conserved |
|---|---|---|---|---|---|---|
| 1 | 556.2 | 1112.5 | 1.49 | 2.327 | 14.752 | yes |
| 2 | 428.2 | 856.4 | 3.981 | 8.389 | 19.163 | yes |
| 4 | 426.1 | 852.2 | 8.045 | 20.566 | 25.858 | yes |
| 8 | 420.2 | 840.4 | 16.877 | 31.592 | 36.322 | yes |
| 16 | 425.3 | 850.6 | 34.5 | 50.377 | 54.722 | yes |

With `synchronous=NORMAL` instead, the same single-thread load ran at 796.1
transfers/s (p99 15.962 ms). Throughput does not scale with threads because SQLite
allows only one writer, and that is the main limitation of this storage choice (see
below). Latency rises with queue depth, but it rises predictably.

**Lifecycle and idempotency (1,000 each)**

- authorize (place a hold): p50 1.094 ms, p99 2.245 ms. Capture: p50 1.387 ms, p99 6.088 ms.
- idempotent replay: p50 0.073 ms, against p50 1.667 ms for a first write. All 1000/1000 retries were served from the stored response.
- overdraft guard: of 10 holds that were each 1/7 of a wallet's balance, 7 were accepted and 3 were rejected with `insufficient_funds`.

**Reconciliation** (30 days, 1,633 cash-account movements against 1,632 bank lines)

| break type | injected | detected | correct | precision | recall |
|---|---|---|---|---|---|
| timing | 49 | 49 | 49 | 1.0 | 1.0 |
| amount_mismatch | 24 | 24 | 24 | 1.0 | 1.0 |
| duplicate | 16 | 16 | 16 | 1.0 | 1.0 |
| missing_in_bank | 33 | 33 | 33 | 1.0 | 1.0 |
| missing_in_ledger | 16 | 16 | 16 | 1.0 | 1.0 |

- The auto-match rate was 93.51%, including reference-less lines matched on amount and date.
- The bank-minus-ledger difference was 3,732,848 cents. Classified breaks explain all of it, so the unexplained difference is 0.
- Matching took 9.42 ms.

**Stress case.** This run used four times the break rate. 35% of bank memos lost their
reference, and those lines settled 0 to 3 days late. Precision fell to **0.5394** and
recall to **0.7008**:

- A reference-less line that settled outside the 1-day tolerance is a real timing break.
- The matcher cannot see that, so it reports the break as a `missing_in_bank` plus a `missing_in_ledger`.
- As a result, timing recall falls to 0.4527.
- Even here, every cent of the difference is still attributed to a break (unexplained difference 0).
- This is the failure mode that a fuzzy, many-to-one matcher would address next.

## Design decisions

- **Integer minor units everywhere.**
  - `StrictInt` at the API, so `10.5` and `"10"` are rejected with a 422.
  - `isinstance(int)` and a cap of 10^15 in the service.
  - `CHECK (typeof(amount) = 'integer' AND amount > 0)` in SQLite.
- **The database protects itself.**
  - Triggers reject `UPDATE`/`DELETE` on `entries`, `postings` and `payment_events`.
  - A trigger rejects postings whose currency differs from the account's currency.
  - `reverses_entry_id UNIQUE` makes double reversal impossible even if the service has a bug.
  - Each entry's balance is re-checked with SQL over the rows actually inserted, as well as in Python.
- **Pessimistic and optimistic concurrency.**
  - Pessimistic: every write runs in `BEGIN IMMEDIATE`, so an overdraft check and the update that follows cannot interleave with another writer.
  - Optimistic: every cached-balance update is a compare-and-set on `lock_version`. Payment status changes are a compare-and-set on `status`.
  - Clients can pass `expected_lock_versions` to get a 409 when an account changed after they read it.
  - An in-process mutex sits in front of `BEGIN IMMEDIATE`. SQLite's busy handler polls with sleeps, which pushed p99 at 8 threads above 400 ms in early runs; the mutex hands the lock straight to the next writer. `BEGIN IMMEDIATE` still guards against writers in other processes.
- **Idempotency records commit in the same transaction as the effect.**
  - A crash cannot leave money moved without the key recorded, or the reverse.
  - A concurrent duplicate waits for the write lock and then replays the stored response.
  - Failed requests are not cached, so a client can fix the cause (for example, fund the account) and retry with the same key.
  - The fingerprint is a hash of (operation, canonical parameters). The same key with a different body, or a different operation, returns 409.
- **Holds are not journal entries.**
  - An authorization changes `pending` and `available` balances only.
  - Capture releases the hold and posts the entry in one transaction, so `available` is unchanged and a held payment can always settle.
  - Failing a payment releases the hold and writes nothing to the journal.
  - `payment_events` is an append-only audit trail of every transition.
- **Reversals go through the state machine.** Reversing a payment's entry through `/v1/entries/{id}/reverse` also moves the payment to `reversed`, so the journal and the payment always agree. Reversals obey the overdraft rule: if the recipient already spent the money, the reversal is rejected rather than overdrawing them.
- **Every reconciliation result is scored.** The statement generator records the breaks it injects, so the matcher's output is measured against ground truth. The `explained_by_breaks == difference` identity serves as a proof that the classification is complete.

**Testing.**

- Unit tests cover the domain rules and an exhaustive 4×4 state-transition table.
- Service tests cover tampering detection (a hand-edited cached balance is caught by `verify`).
- FastAPI `TestClient` tests cover the HTTP surface.
- A Hypothesis `RuleBasedStateMachine` runs random deposits, payments, holds, captures, failures, reversals, withdrawals and deliberately unbalanced entries across two currencies. After every step it checks:
  - the trial balance is zero per currency;
  - no disallowed overdraft;
  - cache equals derivation;
  - every balance equals an independent model;
  - rejected operations leave every balance unchanged.
- Disabling the overdraft check makes the state machine fail within seconds; this was verified by hand.
- A threaded test sends 600 payment requests 1 to 3 times each, shuffled across 8 threads, then settles the holds concurrently with duplicate deliveries. It asserts:
  - one payment per key;
  - conservation of money;
  - no negative balances;
  - exactly one winner when 8 threads race to reverse the same payment.

## Limitations

- **Single writer.** SQLite serializes all writes, so throughput stays around 420 to 560 transfers/s no matter how many threads. A Postgres version with `SELECT … FOR UPDATE` on the touched accounts (already locked in sorted id order) would scale with the number of disjoint account pairs.
- **The reconciliation data is synthetic.** The statement generator and the matcher were written together, so the 100% score on the standard run is an upper bound. The stress run shows how quickly that falls once references are missing. Real bank files (BAI2, camt.053) carry more ambiguity, such as batched settlements and many-to-one matches.
- **Simplified scope.**
  - Transfers are single-currency. FX works only as a hand-built multi-leg entry.
  - There is no partial capture, hold expiry, or authentication/tenancy.
  - Idempotency keys never expire.

## What I'd do next

1. Port storage to Postgres with row-level locks and `SERIALIZABLE` retry, and benchmark throughput as concurrency rises across disjoint accounts.
2. Add hold expiry (a sweeper that fails stale `pending` payments), partial and multiple captures, and idempotency-key TTLs.
3. Make the reconciliation matcher many-to-one (batched payouts) and add scored fuzzy matching (amount, date distance, counterparty similarity). Add a BAI2/camt.053 parser.
4. Publish ledger events through a transactional outbox so downstream systems (notifications, GL export) get exactly-once delivery.
5. Add balance snapshots per effective date, for as-of balance queries without a full scan.
