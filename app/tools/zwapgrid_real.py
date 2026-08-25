"""Zwapgrid adapter (real): invoice interchange as the intake source.

In production, invoices arrive from Zwapgrid's Accounting API (API.1) instead
of the demo's POST /invoices endpoint. Verified against the public docs at
https://docs.zwapgrid.com (Accounting API Guide -> Supplier Invoices):

  - Auth: every request carries `x-api-key` (a key minted in Zwapgrid's Client
    Portal — Development or Production) and `x-correlation-id` (a fresh GUID
    per request, for tracing).
  - Access is scoped to a *Consent* — the link between our buyer and their
    connected accounting system (Fortnox, Xero, ...). Consents are created
    once via `POST /consents` during onboarding and go
    Created -> Accepted -> Active; they go Inactive after 30 days unused.
    This adapter does not create consents, only polls one that already
    exists (ZWAPGRID_CONSENT_ID).
  - Data access is polling-based (no webhooks): GET
    /consents/{consentId}/supplierinvoices, paginated via Count/CurrentPage.
  - The list schema is UBL-flavoured (accountingSupplierParty /
    legalMonetaryTotal), matching what an e-invoice interchange network
    actually hands you — not a flat REST resource.

Known gap, confirmed against the docs rather than guessed: the supplier
invoice list has no dedicated IBAN/bankgiro/OCR field. Payment routing shows
up, if at all, as free text in `notes[].text`. That is exactly the "no one
authoritative says which account belongs to which supplier" gap
app/tools/account_registry.py argues doesn't exist yet — so a bank_account
parsed here is itself an unverified claim, not ground truth. Feed it through
the same verification pipeline as a bank-account-changed claim; don't trust
it just because it came off the wire.

TODO(venue):
  - Confirm with Zwapgrid support whether a single-invoice GET (as opposed to
    the list) exposes a structured paymentMeans block; if so, prefer that
    over _extract_bank_account's notes-regex fallback.
  - Decide a polling cadence and call this on a schedule (or on-demand before
    financing decisions) — the API is polling-only, there's no push.
"""

import re
import time
import uuid

import httpx

from app import config
from app.models import Invoice
from app.tools.base import EvidenceTool
from app.tools.payment_history import PaymentHistoryInput

_BANK_ACCOUNT_RE = re.compile(r"\b(SE\d{2}[A-Z0-9]{15,30}|\d{3,4}-\d{4,7})\b")


def _format_orgnr(raw: str) -> str:
    """Zwapgrid returns a bare 10-digit orgnr (schemeId SE:ORGNR); the rest of
    this app uses the conventional dashed form, e.g. 556012-3456."""
    digits = raw.strip()
    if len(digits) == 10 and digits.isdigit():
        return f"{digits[:6]}-{digits[6:]}"
    return digits


def _extract_bank_account(notes: list[dict]) -> str | None:
    for note in notes:
        match = _BANK_ACCOUNT_RE.search(note.get("text", ""))
        if match:
            return match.group(0)
    return None


def _to_invoice(item: dict) -> Invoice:
    supplier_party = item["accountingSupplierParty"]
    party = supplier_party["party"]
    notes = item.get("notes", [])
    payable = item["legalMonetaryTotal"]["payableAmount"]

    return Invoice(
        id=item["id"],
        supplier_orgnr=_format_orgnr(supplier_party["customerAssignedAccountId"]["id"]),
        supplier_name=party["partyName"]["name"],
        amount_sek=payable["amount"],
        currency=payable["currencyId"],
        bank_account=_extract_bank_account(notes) or "UNKNOWN",
        reference=item["reference"],
        due_date=item["dueDate"],
        issued_date=item["issueDate"],
        contact_email=party.get("contact", {}).get("email") or "",
        raw_note=" | ".join(n["text"] for n in notes) or None,
    )


class ZwapgridRealTool:
    """Adapter around Zwapgrid's Accounting API (API.1): polls a connected
    Consent for supplier invoices and maps them to app.models.Invoice, ready
    for app.ingest.process_invoice()."""

    def _headers(self) -> dict:
        if not config.ZWAPGRID_API_KEY:
            raise RuntimeError("ZWAPGRID_API_KEY is not set")
        return {"x-api-key": config.ZWAPGRID_API_KEY, "x-correlation-id": str(uuid.uuid4())}

    async def fetch_incoming_invoices(self) -> list[Invoice]:
        """Poll every page of supplier invoices for the connected Consent."""
        if not config.ZWAPGRID_CONSENT_ID:
            raise RuntimeError("ZWAPGRID_CONSENT_ID is not set")

        invoices: list[Invoice] = []
        page = 1
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                resp = await client.get(
                    f"{config.ZWAPGRID_BASE_URL}/consents/{config.ZWAPGRID_CONSENT_ID}/supplierinvoices",
                    headers=self._headers(),
                    params={"Count": 100, "CurrentPage": page},
                )
                resp.raise_for_status()
                body = resp.json()
                invoices.extend(_to_invoice(item) for item in body["data"])
                if page >= body["meta"]["totalPages"]:
                    break
                page += 1

        return invoices


class ZwapgridPaymentHistoryTool(EvidenceTool):
    """Real 'payment_history' evidence tool, backed by Zwapgrid instead of Open
    Payments (app/tools/payment_history.py's OpenPaymentsRealTool needs OAuth2
    creds we don't have; this uses the Zwapgrid consent we do).

    Honest caveat: this is invoice history from the connected accounting
    system (Fortnox/Xero), not confirmed bank settlements — a supplier can be
    invoiced without being paid yet. It answers "how many invoices from this
    supplier have we booked, on which accounts" rather than true payment
    history. Good enough to spot an account that's never been used before;
    not proof money actually moved.

    The list endpoint has no per-supplier filter, so a lookup means fetching
    the whole consent's invoice list and filtering client-side — cached
    briefly so a single investigation (payment_history + repeat calls) doesn't
    re-poll Zwapgrid per tool call.
    """

    name = "payment_history"
    description = (
        "Invoice history from the connected accounting system via Zwapgrid: how "
        "many invoices this supplier has sent us, when, and to which bank accounts. "
        "Pass `account` to check how often a specific account was used. Reflects "
        "booked invoices, not confirmed bank settlements."
    )
    input_model = PaymentHistoryInput

    _CACHE_TTL_SECONDS = 60

    def __init__(self, client: ZwapgridRealTool | None = None) -> None:
        self._client = client or ZwapgridRealTool()
        self._cache: list[Invoice] | None = None
        self._cached_at: float = 0.0

    async def _all_invoices(self) -> list[Invoice]:
        now = time.monotonic()
        if self._cache is None or now - self._cached_at > self._CACHE_TTL_SECONDS:
            self._cache = await self._client.fetch_incoming_invoices()
            self._cached_at = now
        return self._cache

    async def lookup(self, orgnr: str, account: str | None = None) -> dict:
        invoices = [inv for inv in await self._all_invoices() if inv.supplier_orgnr == orgnr]
        per_account: dict[str, int] = {}
        for inv in invoices:
            per_account[inv.bank_account] = per_account.get(inv.bank_account, 0) + 1
        if account is not None and account not in per_account:
            per_account[account] = 0
        ordered = sorted(invoices, key=lambda inv: inv.issued_date)
        return {
            "orgnr": orgnr,
            "payment_count": len(invoices),
            "first_payment": ordered[0].issued_date.isoformat() if ordered else None,
            "last_payment": ordered[-1].issued_date.isoformat() if ordered else None,
            "accounts": [
                {"account": acct, "payments": n}
                for acct, n in sorted(per_account.items(), key=lambda kv: -kv[1])
            ],
            "note": "Sourced from Zwapgrid invoice history, not confirmed bank settlements.",
        }
