"""Shared fixtures: in-memory and file-backed ledgers, an API client, funded accounts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from ledger.api import create_app
from ledger.domain import Direction, PostingSpec
from ledger.service import Ledger

Fund = Callable[..., dict[str, Any]]


def _fund(
    ledger: Ledger, bank: str, wallet: str, amount: int, currency: str = "USD"
) -> dict[str, Any]:
    """Customer deposit: cash arrives at the bank (debit asset), we owe the customer
    (credit liability)."""
    return ledger.post_entry(
        [
            PostingSpec(bank, Direction.DEBIT, amount, currency),
            PostingSpec(wallet, Direction.CREDIT, amount, currency),
        ],
        description="deposit",
    )


@pytest.fixture
def fund() -> Fund:
    """``fund(ledger, bank, wallet, amount, currency="USD")`` posts a deposit entry."""
    return _fund


@pytest.fixture
def ledger() -> Iterator[Ledger]:
    lg = Ledger(":memory:")
    yield lg
    lg.close()


@pytest.fixture
def file_ledger(tmp_path: Path) -> Iterator[Ledger]:
    lg = Ledger(tmp_path / "ledger.db", synchronous="NORMAL")
    yield lg
    lg.close()


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    lg = Ledger(tmp_path / "api.db", synchronous="NORMAL")
    with TestClient(create_app(lg)) as c:
        yield c
    lg.close()


@pytest.fixture
def accounts(ledger: Ledger) -> dict[str, str]:
    """A bank account plus wallets for Alice (funded with 10,000) and Bob (empty)."""
    bank = ledger.create_account("Bank", "asset", "USD")["id"]
    alice = ledger.create_account("Alice", "liability", "USD")["id"]
    bob = ledger.create_account("Bob", "liability", "USD")["id"]
    _fund(ledger, bank, alice, 10_000)
    return {"bank": bank, "alice": alice, "bob": bob}


def _api_account(client: TestClient, name: str, type_: str = "liability") -> str:
    r = client.post("/v1/accounts", json={"name": name, "type": type_, "currency": "USD"})
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def api_wallets(client: TestClient) -> dict[str, str]:
    """Same layout as ``accounts``, created through the HTTP API."""
    bank = _api_account(client, "Bank", "asset")
    alice = _api_account(client, "Alice")
    bob = _api_account(client, "Bob")
    r = client.post(
        "/v1/entries",
        json={
            "description": "deposit",
            "postings": [
                {"account_id": bank, "direction": "debit", "amount": 10_000, "currency": "USD"},
                {"account_id": alice, "direction": "credit", "amount": 10_000, "currency": "USD"},
            ],
        },
    )
    assert r.status_code == 201, r.text
    return {"bank": bank, "alice": alice, "bob": bob}
