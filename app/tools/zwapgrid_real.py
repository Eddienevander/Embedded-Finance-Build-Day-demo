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

Correction to an earlier version of this note: there IS a write path for
payments, just not on the invoice resource itself — it's the separate
POST /consents/{consentId}/supplierinvoices/{id}/payments (see
create_invoice_payment), confirmed live against the sandbox (201, and
Fortnox enforces it internally: it rejected a duplicate payment on an
already-settled invoice with "Leverantörsfaktura X är redan slutbetald").
What still doesn't work, also confirmed live rather than assumed: the
invoice's own `paymentStatus` field (GET .../supplierinvoices/{id}) does
NOT update from a payment registered this way — a real gap in the Fortnox
connector's field mapping, not something fixable by more request tweaking.
So "is this invoice paid" should be computed from get_invoice_payments'
sum, not get_invoice_payment_status.
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


def _invoice_amount(totals: dict) -> dict:
    """Prefer payableAmount (the docs' example has it populated and matching
    the invoice total), but confirmed live against the real Fortnox-via-
    Zwapgrid sandbox: this connector leaves payableAmount AND
    totalBalanceAmount at 0.0 and puts the real total only in
    taxInclusiveAmount. Fall back to that rather than silently reporting a
    0 SEK invoice."""
    payable = totals["payableAmount"]
    if payable.get("amount"):
        return payable
    tax_inclusive = totals.get("taxInclusiveAmount")
    if tax_inclusive and tax_inclusive.get("amount"):
        return tax_inclusive
    return payable  # genuinely 0 — don't fabricate a number


def _to_invoice(item: dict) -> Invoice:
    supplier_party = item["accountingSupplierParty"]
    party = supplier_party["party"]
    notes = item.get("notes", [])
    payable = _invoice_amount(item["legalMonetaryTotal"])

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

    async def fetch_incoming_invoices(self, limit: int | None = None) -> list[Invoice]:
        """Poll supplier invoices for the connected Consent. Pass `limit` to
        stop after roughly that many (one page, sized to the limit, instead
        of paginating through everything) — the sandbox has a real rate
        limit (429, confirmed live) and a live consent can hold dozens of
        rows, most of it placeholder/seed data not worth an API call to see."""
        if not config.ZWAPGRID_CONSENT_ID:
            raise RuntimeError("ZWAPGRID_CONSENT_ID is not set")

        invoices: list[Invoice] = []
        page = 1
        page_size = min(limit, 100) if limit else 100
        async with httpx.AsyncClient(timeout=30) as client:
            while True:
                resp = await client.get(
                    f"{config.ZWAPGRID_BASE_URL}/consents/{config.ZWAPGRID_CONSENT_ID}/supplierinvoices",
                    headers=self._headers(),
                    params={"Count": page_size, "CurrentPage": page},
                )
                resp.raise_for_status()
                body = resp.json()
                invoices.extend(_to_invoice(item) for item in body["data"])
                if limit and len(invoices) >= limit:
                    return invoices[:limit]
                if page >= body["meta"]["totalPages"]:
                    break
                page += 1

        return invoices

    async def get_invoice_payment_status(self, invoice_id: str) -> dict | None:
        """GET the single supplier invoice and read back its paymentStatus —
        the accounting system's own view of whether it's paid, not ours.
        Returns None if the id isn't a real Zwapgrid invoice — e.g. a
        scripted demo invoice that never came from Zwapgrid (confirmed live:
        Fortnox returns 400 for an id it doesn't recognise, not the 404 the
        API spec's docs suggest) — or if the connected accounting system
        doesn't support this operation (501, per the API spec)."""
        if not config.ZWAPGRID_CONSENT_ID:
            raise RuntimeError("ZWAPGRID_CONSENT_ID is not set")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{config.ZWAPGRID_BASE_URL}/consents/{config.ZWAPGRID_CONSENT_ID}"
                f"/supplierinvoices/{invoice_id}",
                headers=self._headers(),
            )
            if resp.status_code in (400, 404, 501):
                return None
            resp.raise_for_status()
            status = resp.json().get("paymentStatus") or {}
            return {
                "status": status.get("status"),
                "settlement_date": status.get("settlementDate"),
            }

    async def get_invoice_payments(self, invoice_id: str) -> list[dict] | None:
        """List payments registered against a supplier invoice
        (GET .../supplierinvoices/{id}/payments). Returns None using the same
        convention as get_invoice_payment_status (400/404/501 = not a real
        Zwapgrid invoice or unsupported). Prefer this over
        get_invoice_payment_status for "is this actually paid": confirmed
        live that Fortnox's paymentStatus field on the invoice itself does
        NOT reflect payments registered here, even though Fortnox internally
        tracks and enforces them (it rejected a duplicate payment on an
        already-settled invoice with "Leverantörsfaktura X är redan
        slutbetald" while paymentStatus stayed null) — a real connector gap,
        not something more request tweaking fixes."""
        if not config.ZWAPGRID_CONSENT_ID:
            raise RuntimeError("ZWAPGRID_CONSENT_ID is not set")
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(
                f"{config.ZWAPGRID_BASE_URL}/consents/{config.ZWAPGRID_CONSENT_ID}"
                f"/supplierinvoices/{invoice_id}/payments",
                headers=self._headers(),
            )
            if resp.status_code in (400, 404, 501):
                return None
            resp.raise_for_status()
            return resp.json().get("data") or []

    async def create_invoice_payment(
        self, invoice_id: str, amount: float, currency: str, reference: str, paid_date: str
    ) -> None:
        """Register a real payment against a supplier invoice — verified
        live (201, Fortnox even enforces "already fully paid" internally on
        a duplicate). This is the closest thing to "mark paid" the Accounting
        API actually exposes; it's additive (a payment record), not an
        update to the invoice itself, since no such endpoint exists."""
        if not config.ZWAPGRID_CONSENT_ID:
            raise RuntimeError("ZWAPGRID_CONSENT_ID is not set")
        payload = {
            "reference": reference,
            "receivedDate": paid_date,
            "paidDate": paid_date,
            "bookedIndicator": True,
            "bookedDate": paid_date,
            "amount": amount,
            "documentCurrencyCode": {"currencyId": currency},
        }
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{config.ZWAPGRID_BASE_URL}/consents/{config.ZWAPGRID_CONSENT_ID}"
                f"/supplierinvoices/{invoice_id}/payments",
                headers={**self._headers(), "content-type": "application/json"},
                json=payload,
            )
            resp.raise_for_status()


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
