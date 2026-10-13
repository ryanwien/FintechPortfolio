"""HTTP API (FastAPI) over :class:`ledger.service.Ledger`.

All POST endpoints accept an ``Idempotency-Key`` header. A retried request with
the same key and body returns the original response (with
``Idempotent-Replayed: true``); the same key with a different body is a 409.
Every error uses one envelope: ``{"error": {"type": <code>, "message": ...}}``.

Run: ``python -m ledger.api`` (serves on 127.0.0.1:8000, database at
``$LEDGER_DB`` or ``ledger.db``).
"""

import os
import sqlite3
from typing import Annotated, Any, Final, Literal

import uvicorn
from fastapi import Depends, FastAPI, Header, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from ledger.domain import (
    MAX_ACCOUNT_NAME_LENGTH,
    MAX_AMOUNT,
    MAX_DESCRIPTION_LENGTH,
    MAX_EXTERNAL_REF_LENGTH,
    MAX_IDEMPOTENCY_KEY_LENGTH,
    IdempotencyKey,
    LedgerError,
)
from ledger.service import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, Ledger

DEFAULT_DB_PATH: Final = "ledger.db"
HOST: Final = "127.0.0.1"
PORT: Final = 8000
MAX_POSTINGS_PER_ENTRY: Final = 100
HTTP_CREATED: Final = 201
HTTP_OK: Final = 200
HTTP_CONFLICT: Final = 409
HTTP_UNPROCESSABLE: Final = 422

ISO_DATE_PATTERN: Final = r"^\d{4}-\d{2}-\d{2}$"
Currency = Annotated[str, Field(pattern=r"^[A-Z]{3}$", examples=["USD"])]
# StrictInt: a JSON float such as 10.5 (or "10") is rejected, never coerced.
Amount = Annotated[
    StrictInt, Field(gt=0, le=MAX_AMOUNT, description="Integer minor units, e.g. cents")
]
Description = Annotated[str, Field(max_length=MAX_DESCRIPTION_LENGTH)]
ExternalRef = Annotated[str | None, Field(max_length=MAX_EXTERNAL_REF_LENGTH)]
EffectiveDate = Annotated[str | None, Field(pattern=ISO_DATE_PATTERN)]
IdemHeader = Annotated[
    str | None, Header(alias="Idempotency-Key", max_length=MAX_IDEMPOTENCY_KEY_LENGTH)
]


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CreateAccount(_Model):
    name: Annotated[str, Field(min_length=1, max_length=MAX_ACCOUNT_NAME_LENGTH)]
    type: Literal["asset", "liability", "equity", "revenue", "expense"]
    currency: Currency
    allow_negative: StrictBool = False


class PostingIn(_Model):
    account_id: str
    direction: Literal["debit", "credit"]
    amount: Amount
    currency: Currency


class CreateEntry(_Model):
    postings: Annotated[list[PostingIn], Field(min_length=2, max_length=MAX_POSTINGS_PER_ENTRY)]
    description: Description = ""
    effective_date: EffectiveDate = None
    external_ref: ExternalRef = None
    expected_lock_versions: dict[str, StrictInt] | None = None


class ReverseEntry(_Model):
    description: Annotated[str | None, Field(max_length=MAX_DESCRIPTION_LENGTH)] = None


class CreatePayment(_Model):
    source_account_id: str
    destination_account_id: str
    amount: Amount
    currency: Currency
    capture: StrictBool = True
    description: Description = ""
    external_ref: ExternalRef = None
    effective_date: EffectiveDate = None


class FailPayment(_Model):
    reason: Description = ""


def _respond(
    body: dict[str, Any], idem: IdempotencyKey | None, status: int = HTTP_CREATED
) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if idem is not None and idem.replayed else {}
    return JSONResponse(body, status_code=status, headers=headers)


def _error(error_type: str, message: str, status: int, **extra: Any) -> JSONResponse:
    return JSONResponse(
        {"error": {"type": error_type, "message": message, **extra}}, status_code=status
    )


def _idempotency(key: str | None) -> IdempotencyKey | None:
    return IdempotencyKey(key) if key else None


def create_app(ledger: Ledger | None = None) -> FastAPI:
    """Build the app around ``ledger`` (default: a file ledger at ``$LEDGER_DB``)."""
    ledger = ledger if ledger is not None else Ledger(os.environ.get("LEDGER_DB", DEFAULT_DB_PATH))
    app = FastAPI(title="Ledger & Payments API", version="1.0.0")
    app.state.ledger = ledger

    def get_ledger(request: Request) -> Ledger:
        return request.app.state.ledger

    L = Annotated[Ledger, Depends(get_ledger)]  # dependency-injected ledger

    @app.exception_handler(LedgerError)
    async def ledger_error(_: Request, exc: LedgerError) -> JSONResponse:
        return _error(exc.code, str(exc), exc.http_status)

    @app.exception_handler(RequestValidationError)
    async def request_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _error(
            "validation_error",
            "request failed validation",
            HTTP_UNPROCESSABLE,
            details=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(sqlite3.IntegrityError)
    async def integrity_error(_: Request, exc: sqlite3.IntegrityError) -> JSONResponse:
        return _error("constraint_violation", str(exc), HTTP_CONFLICT)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -- accounts
    @app.post("/v1/accounts", status_code=HTTP_CREATED)
    def create_account(body: CreateAccount, lg: L, key: IdemHeader = None) -> JSONResponse:
        k = _idempotency(key)
        account = lg.create_account(
            body.name,
            body.type,
            body.currency,
            allow_negative=body.allow_negative,
            idempotency=k,
        )
        return _respond(account, k)

    @app.get("/v1/accounts/{account_id}")
    def get_account(account_id: str, lg: L) -> dict[str, Any]:
        return lg.get_account(account_id)

    @app.get("/v1/accounts/{account_id}/balance")
    def get_balance(account_id: str, lg: L) -> dict[str, Any]:
        return lg.get_balance(account_id)

    # -- journal
    @app.post("/v1/entries", status_code=HTTP_CREATED)
    def create_entry(body: CreateEntry, lg: L, key: IdemHeader = None) -> JSONResponse:
        k = _idempotency(key)
        entry = lg.post_entry(
            [p.model_dump() for p in body.postings],
            description=body.description,
            effective_date=body.effective_date,
            external_ref=body.external_ref,
            expected_lock_versions=body.expected_lock_versions,
            idempotency=k,
        )
        return _respond(entry, k)

    @app.get("/v1/entries")
    def list_entries(
        lg: L,
        account_id: str | None = None,
        limit: Annotated[int, Query(ge=1, le=MAX_PAGE_SIZE)] = DEFAULT_PAGE_SIZE,
        cursor: int | None = None,
    ) -> dict[str, Any]:
        return lg.list_entries(account_id=account_id, limit=limit, cursor=cursor)

    @app.get("/v1/entries/{entry_id}")
    def get_entry(entry_id: str, lg: L) -> dict[str, Any]:
        return lg.get_entry(entry_id)

    @app.post("/v1/entries/{entry_id}/reverse", status_code=HTTP_CREATED)
    def reverse_entry(
        entry_id: str, lg: L, body: ReverseEntry | None = None, key: IdemHeader = None
    ) -> JSONResponse:
        k = _idempotency(key)
        description = body.description if body else None
        return _respond(lg.reverse_entry(entry_id, description=description, idempotency=k), k)

    # -- payments
    @app.post("/v1/payments", status_code=HTTP_CREATED)
    def create_payment(body: CreatePayment, lg: L, key: IdemHeader = None) -> JSONResponse:
        k = _idempotency(key)
        pay = lg.create_payment(
            body.source_account_id,
            body.destination_account_id,
            body.amount,
            body.currency,
            capture=body.capture,
            description=body.description,
            external_ref=body.external_ref,
            effective_date=body.effective_date,
            idempotency=k,
        )
        return _respond(pay, k)

    @app.get("/v1/payments/{payment_id}")
    def get_payment(payment_id: str, lg: L) -> dict[str, Any]:
        return lg.get_payment(payment_id)

    @app.post("/v1/payments/{payment_id}/capture")
    def capture_payment(payment_id: str, lg: L, key: IdemHeader = None) -> JSONResponse:
        k = _idempotency(key)
        return _respond(lg.capture_payment(payment_id, idempotency=k), k, HTTP_OK)

    @app.post("/v1/payments/{payment_id}/fail")
    def fail_payment(
        payment_id: str, lg: L, body: FailPayment | None = None, key: IdemHeader = None
    ) -> JSONResponse:
        k = _idempotency(key)
        reason = body.reason if body else ""
        return _respond(lg.fail_payment(payment_id, reason=reason, idempotency=k), k, HTTP_OK)

    @app.post("/v1/payments/{payment_id}/reverse")
    def reverse_payment(payment_id: str, lg: L, key: IdemHeader = None) -> JSONResponse:
        k = _idempotency(key)
        return _respond(lg.reverse_payment(payment_id, idempotency=k), k, HTTP_OK)

    # -- integrity
    @app.get("/v1/ledger/trial-balance")
    def trial_balance(lg: L) -> dict[str, Any]:
        return lg.trial_balance()

    @app.get("/v1/ledger/verify")
    def verify(lg: L) -> dict[str, Any]:
        return lg.verify()

    return app


def main() -> None:  # pragma: no cover
    uvicorn.run(create_app(), host=HOST, port=PORT)


if __name__ == "__main__":  # pragma: no cover
    main()
