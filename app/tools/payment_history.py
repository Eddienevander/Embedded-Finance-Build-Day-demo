"""Open Payments-shaped payment history — mock (reads our SQLite) + real stub."""

from pydantic import BaseModel, Field

from app.db import Database
from app.tools.base import EvidenceTool


class PaymentHistoryInput(BaseModel):
    orgnr: str = Field(description="Supplier organisationsnummer")
    account: str | None = Field(
        default=None,
        description="Optional: a specific account to check payment counts for "
                    "(e.g. the NEW account on a suspicious invoice)",
    )


_DESCRIPTION = (
    "Payment history from the buyer's bank (Open Payments-shaped): how many "
    "times this supplier has been paid, when, and to which accounts. "
    "Pass `account` to check how often a specific account was used."
)


class MockPaymentHistoryTool(EvidenceTool):
    name = "payment_history"
    description = _DESCRIPTION
    input_model = PaymentHistoryInput

    def __init__(self, db: Database) -> None:
        self._db = db

    async def lookup(self, orgnr: str, account: str | None = None) -> dict:
        payments = self._db.get_payments(orgnr)
        per_account: dict[str, int] = {}
        for p in payments:
            per_account[p["bank_account"]] = per_account.get(p["bank_account"], 0) + 1
        if account is not None and account not in per_account:
            per_account[account] = 0
        return {
            "orgnr": orgnr,
            "payment_count": len(payments),
            "first_payment": payments[0]["paid_at"] if payments else None,
            "last_payment": payments[-1]["paid_at"] if payments else None,
            "accounts": [
                {"account": acct, "payments": n}
                for acct, n in sorted(per_account.items(), key=lambda kv: -kv[1])
            ],
        }


class OpenPaymentsRealTool(EvidenceTool):
    """Real adapter for the Open Payments platform (account-to-account history).

    TODO(venue): wire this in.
      - Auth: OAuth2 client credentials against the Open Payments sandbox
        (client_id / client_secret placeholders in env).
      - List payment initiations / account transactions per counterparty and
        map -> the same dict shape as MockPaymentHistoryTool.
    """

    name = "payment_history"
    description = _DESCRIPTION
    input_model = PaymentHistoryInput

    async def lookup(self, orgnr: str, account: str | None = None) -> dict:
        raise NotImplementedError("TODO: wire Open Payments adapter (OAuth2 placeholders)")
