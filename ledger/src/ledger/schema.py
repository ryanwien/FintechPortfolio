"""SQLite schema.

The database enforces what it can on its own (types, signs, currency agreement,
append-only journal) so a bug in the service layer cannot silently corrupt the
books. Statements are idempotent (``IF NOT EXISTS``) and run on every open.
"""

from typing import Final

SCHEMA: Final = """
CREATE TABLE IF NOT EXISTS accounts (
    id               TEXT PRIMARY KEY,
    name             TEXT NOT NULL,
    type             TEXT NOT NULL
                     CHECK (type IN ('asset','liability','equity','revenue','expense')),
    currency         TEXT NOT NULL CHECK (length(currency) = 3 AND currency = upper(currency)),
    allow_negative   INTEGER NOT NULL DEFAULT 0 CHECK (allow_negative IN (0, 1)),
    -- cached running totals; always re-derivable from postings + payments
    posted_debits    INTEGER NOT NULL DEFAULT 0
                     CHECK (typeof(posted_debits) = 'integer' AND posted_debits >= 0),
    posted_credits   INTEGER NOT NULL DEFAULT 0
                     CHECK (typeof(posted_credits) = 'integer' AND posted_credits >= 0),
    pending_debits   INTEGER NOT NULL DEFAULT 0
                     CHECK (typeof(pending_debits) = 'integer' AND pending_debits >= 0),
    pending_credits  INTEGER NOT NULL DEFAULT 0
                     CHECK (typeof(pending_credits) = 'integer' AND pending_credits >= 0),
    lock_version     INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entries (
    seq               INTEGER PRIMARY KEY AUTOINCREMENT,
    id                TEXT NOT NULL UNIQUE,
    description       TEXT NOT NULL DEFAULT '',
    effective_date    TEXT NOT NULL,
    created_at        TEXT NOT NULL,
    external_ref      TEXT,
    payment_id        TEXT,
    -- UNIQUE: an entry can be reversed at most once, enforced by the database
    reverses_entry_id TEXT UNIQUE REFERENCES entries(id)
);
CREATE INDEX IF NOT EXISTS ix_entries_external_ref ON entries(external_ref);
CREATE INDEX IF NOT EXISTS ix_entries_effective_date ON entries(effective_date);

CREATE TABLE IF NOT EXISTS postings (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    entry_id    TEXT NOT NULL REFERENCES entries(id),
    account_id  TEXT NOT NULL REFERENCES accounts(id),
    direction   TEXT NOT NULL CHECK (direction IN ('debit','credit')),
    amount      INTEGER NOT NULL CHECK (typeof(amount) = 'integer' AND amount > 0),
    currency    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_postings_account ON postings(account_id, id);
CREATE INDEX IF NOT EXISTS ix_postings_entry ON postings(entry_id);

CREATE TABLE IF NOT EXISTS payments (
    id                     TEXT PRIMARY KEY,
    source_account_id      TEXT NOT NULL REFERENCES accounts(id),
    destination_account_id TEXT NOT NULL REFERENCES accounts(id),
    amount                 INTEGER NOT NULL CHECK (typeof(amount) = 'integer' AND amount > 0),
    currency               TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN ('pending','posted','failed','reversed')),
    description            TEXT NOT NULL DEFAULT '',
    external_ref           TEXT,
    entry_id               TEXT REFERENCES entries(id),
    reversal_entry_id      TEXT REFERENCES entries(id),
    failure_reason         TEXT,
    created_at             TEXT NOT NULL,
    updated_at             TEXT NOT NULL,
    CHECK (source_account_id <> destination_account_id)
);
CREATE INDEX IF NOT EXISTS ix_payments_status ON payments(status);
CREATE INDEX IF NOT EXISTS ix_payments_source ON payments(source_account_id, status);
CREATE INDEX IF NOT EXISTS ix_payments_destination ON payments(destination_account_id, status);

CREATE TABLE IF NOT EXISTS payment_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payment_id  TEXT NOT NULL REFERENCES payments(id),
    from_status TEXT,
    to_status   TEXT NOT NULL,
    entry_id    TEXT REFERENCES entries(id),
    detail      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_payment_events_payment ON payment_events(payment_id, id);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    fingerprint  TEXT NOT NULL,
    response     TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

-- Posting currency must match the account's currency.
CREATE TRIGGER IF NOT EXISTS postings_currency_matches_account
BEFORE INSERT ON postings
WHEN NEW.currency <> (SELECT currency FROM accounts WHERE id = NEW.account_id)
BEGIN
    SELECT RAISE(ABORT, 'posting currency does not match account currency');
END;

-- The journal is append-only. Corrections are reversing entries, never edits.
CREATE TRIGGER IF NOT EXISTS entries_immutable_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'journal is append-only: entries cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS entries_immutable_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'journal is append-only: entries cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS postings_immutable_update BEFORE UPDATE ON postings
BEGIN SELECT RAISE(ABORT, 'journal is append-only: postings cannot be updated'); END;
CREATE TRIGGER IF NOT EXISTS postings_immutable_delete BEFORE DELETE ON postings
BEGIN SELECT RAISE(ABORT, 'journal is append-only: postings cannot be deleted'); END;
CREATE TRIGGER IF NOT EXISTS payment_events_immutable_update BEFORE UPDATE ON payment_events
BEGIN SELECT RAISE(ABORT, 'payment_events is append-only'); END;
CREATE TRIGGER IF NOT EXISTS payment_events_immutable_delete BEFORE DELETE ON payment_events
BEGIN SELECT RAISE(ABORT, 'payment_events is append-only'); END;
"""
