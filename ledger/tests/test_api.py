from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def test_healthz(client: TestClient) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_account_and_balance(client: TestClient, api_wallets: dict[str, str]) -> None:
    alice = api_wallets["alice"]
    r = client.get(f"/v1/accounts/{alice}/balance")
    assert r.status_code == 200
    assert r.json()["posted"] == 10_000
    assert r.json()["currency"] == "USD"
    assert client.get(f"/v1/accounts/{alice}").json()["normal_balance"] == "credit"


def test_create_account_idempotent(client: TestClient) -> None:
    body = {"name": "Ops", "type": "asset", "currency": "USD"}
    r1 = client.post("/v1/accounts", json=body, headers={"Idempotency-Key": "acct-1"})
    r2 = client.post("/v1/accounts", json=body, headers={"Idempotency-Key": "acct-1"})
    assert r1.json()["id"] == r2.json()["id"]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/accounts/acct_x",
        "/v1/accounts/acct_x/balance",
        "/v1/entries/entry_x",
        "/v1/payments/pay_x",
    ],
)
def test_missing_resource_returns_404_error_envelope(client: TestClient, path: str) -> None:
    r = client.get(path)
    assert r.status_code == 404
    assert r.json()["error"]["type"] == "not_found"


@pytest.mark.parametrize("amount", [10.5, 10.0, "10", -5, 0])
def test_non_positive_or_non_integer_amount_rejected(
    client: TestClient, api_wallets: dict[str, str], amount: object
) -> None:
    alice, bob = api_wallets["alice"], api_wallets["bob"]
    r = client.post(
        "/v1/payments",
        json={
            "source_account_id": alice,
            "destination_account_id": bob,
            "amount": amount,
            "currency": "USD",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "validation_error"


def test_unknown_fields_rejected(client: TestClient) -> None:
    r = client.post(
        "/v1/accounts", json={"name": "x", "type": "asset", "currency": "USD", "balance": 100}
    )
    assert r.status_code == 422


def test_unbalanced_entry_422(client: TestClient, api_wallets: dict[str, str]) -> None:
    bank, alice = api_wallets["bank"], api_wallets["alice"]
    r = client.post(
        "/v1/entries",
        json={
            "postings": [
                {"account_id": bank, "direction": "debit", "amount": 5, "currency": "USD"},
                {"account_id": alice, "direction": "credit", "amount": 4, "currency": "USD"},
            ]
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "unbalanced_entry"


def test_payment_idempotency_over_http(client: TestClient, api_wallets: dict[str, str]) -> None:
    alice, bob = api_wallets["alice"], api_wallets["bob"]
    body = {
        "source_account_id": alice,
        "destination_account_id": bob,
        "amount": 1_234,
        "currency": "USD",
    }
    r1 = client.post("/v1/payments", json=body, headers={"Idempotency-Key": "abc"})
    r2 = client.post("/v1/payments", json=body, headers={"Idempotency-Key": "abc"})
    assert r1.status_code == r2.status_code == 201
    assert "Idempotent-Replayed" not in r1.headers
    assert r2.headers["Idempotent-Replayed"] == "true"
    assert r1.json() == r2.json()
    assert client.get(f"/v1/accounts/{bob}/balance").json()["posted"] == 1_234

    r3 = client.post(
        "/v1/payments", json={**body, "amount": 999}, headers={"Idempotency-Key": "abc"}
    )
    assert r3.status_code == 409
    assert r3.json()["error"]["type"] == "idempotency_key_reused"
    assert client.get(f"/v1/accounts/{bob}/balance").json()["posted"] == 1_234


def test_insufficient_funds_422(client: TestClient, api_wallets: dict[str, str]) -> None:
    alice, bob = api_wallets["alice"], api_wallets["bob"]
    r = client.post(
        "/v1/payments",
        json={
            "source_account_id": bob,
            "destination_account_id": alice,
            "amount": 1,
            "currency": "USD",
        },
    )
    assert r.status_code == 422
    assert r.json()["error"]["type"] == "insufficient_funds"


def test_payment_lifecycle_over_http(client: TestClient, api_wallets: dict[str, str]) -> None:
    alice, bob = api_wallets["alice"], api_wallets["bob"]
    r = client.post(
        "/v1/payments",
        json={
            "source_account_id": alice,
            "destination_account_id": bob,
            "amount": 4_000,
            "currency": "USD",
            "capture": False,
        },
    )
    pay = r.json()
    assert pay["status"] == "pending"
    bal = client.get(f"/v1/accounts/{alice}/balance").json()
    assert (bal["posted"], bal["available"]) == (10_000, 6_000)

    r = client.post(f"/v1/payments/{pay['id']}/capture", headers={"Idempotency-Key": "cap-1"})
    assert r.status_code == 200
    assert r.json()["status"] == "posted"
    r = client.post(f"/v1/payments/{pay['id']}/capture", headers={"Idempotency-Key": "cap-1"})
    assert r.status_code == 200
    assert r.headers["Idempotent-Replayed"] == "true"
    r = client.post(f"/v1/payments/{pay['id']}/capture")  # a new attempt without the key
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "illegal_state_transition"
    r = client.post(f"/v1/payments/{pay['id']}/fail", json={"reason": "late"})
    assert r.status_code == 409

    r = client.post(f"/v1/payments/{pay['id']}/reverse")
    assert r.status_code == 200
    assert r.json()["status"] == "reversed"
    events = client.get(f"/v1/payments/{pay['id']}").json()["events"]
    assert [e["to_status"] for e in events] == ["pending", "posted", "reversed"]
    assert client.get(f"/v1/accounts/{alice}/balance").json()["posted"] == 10_000


def test_fail_pending_payment(client: TestClient, api_wallets: dict[str, str]) -> None:
    alice, bob = api_wallets["alice"], api_wallets["bob"]
    pay = client.post(
        "/v1/payments",
        json={
            "source_account_id": alice,
            "destination_account_id": bob,
            "amount": 4_000,
            "currency": "USD",
            "capture": False,
        },
    ).json()
    r = client.post(f"/v1/payments/{pay['id']}/fail", json={"reason": "issuer declined"})
    assert r.json()["status"] == "failed"
    assert client.get(f"/v1/accounts/{alice}/balance").json()["available"] == 10_000


def test_reverse_entry_and_list(client: TestClient, api_wallets: dict[str, str]) -> None:
    alice, bob = api_wallets["alice"], api_wallets["bob"]
    r = client.post(
        "/v1/entries",
        json={
            "postings": [
                {"account_id": alice, "direction": "debit", "amount": 100, "currency": "USD"},
                {"account_id": bob, "direction": "credit", "amount": 100, "currency": "USD"},
            ]
        },
    )
    entry = r.json()
    r = client.post(
        f"/v1/entries/{entry['id']}/reverse", json={"description": "keyed to wrong customer"}
    )
    assert r.status_code == 201
    assert r.json()["reverses_entry_id"] == entry["id"]
    r = client.post(f"/v1/entries/{entry['id']}/reverse")
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "already_reversed"

    listing = client.get("/v1/entries", params={"account_id": bob, "limit": 1}).json()
    assert len(listing["data"]) == 1
    assert listing["has_more"]
    nxt = client.get(
        "/v1/entries", params={"account_id": bob, "limit": 1, "cursor": listing["next_cursor"]}
    ).json()
    assert nxt["data"][0]["id"] == entry["id"]
    assert client.get("/v1/entries", params={"limit": 0}).status_code == 422


def test_stale_lock_version_409(client: TestClient, api_wallets: dict[str, str]) -> None:
    alice, bob = api_wallets["alice"], api_wallets["bob"]
    v = client.get(f"/v1/accounts/{alice}/balance").json()["lock_version"]
    body = {
        "postings": [
            {"account_id": alice, "direction": "debit", "amount": 1, "currency": "USD"},
            {"account_id": bob, "direction": "credit", "amount": 1, "currency": "USD"},
        ],
        "expected_lock_versions": {alice: v},
    }
    assert client.post("/v1/entries", json=body).status_code == 201
    r = client.post("/v1/entries", json=body)
    assert r.status_code == 409
    assert r.json()["error"]["type"] == "lock_version_mismatch"


@pytest.mark.usefixtures("api_wallets")
def test_trial_balance_and_verify(client: TestClient) -> None:
    assert client.get("/v1/ledger/trial-balance").json()["USD"]["net"] == 0
    v = client.get("/v1/ledger/verify").json()
    assert v["ok"]
    assert v["accounts_checked"] == 3
