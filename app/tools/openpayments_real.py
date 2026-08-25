"""Open Payments Europe adapter (real): executes an approved invoice's
payment via the Payment Initiation Service (PIS).

This is NOT the Interledger/GNAP "Open Payments" standard (openpayments.dev)
— that's a different company with a different protocol (wallet addresses,
incoming/outgoing payment grants). This is Open Payments Europe's PSD2
Payment Initiation API for Nordic/EU banks (openpayments.io), verified
against https://docs.openpayments.io (Payments guide, Authorisations guide,
Access Tokens guide, Sandbox Credentials guide):

  - Auth: OAuth2 `client_credentials` against POST {auth}/connect/token,
    scope "paymentinitiation corporate". Tokens last 1h; cached with a
    safety margin and refetched on expiry (see `_access_token`).
  - Payment: POST {api}/psd2/paymentinitiation/v1/payments/swedish-giro — a
    Bankgiro/Plusgiro-routed domestic payment, the natural fit since every
    supplier account in this app is already a bankgiro string (see
    app/seed.py). Returns a paymentId.
  - Authorisation (SCA): sandbox only offers the *decoupled* flow — the docs
    confirm it auto-finalises within seconds with no real PSU/BankID action
    needed, unlike the interactive *redirect* flow (production only, needs a
    real human + callback). That's what makes this runnable as one
    synchronous backend call instead of a redirect/callback dance: create an
    authorisation, start it with the first offered SCA method, poll
    GET .../authorisations/{id} until scaStatus is "finalised" or "failed".
  - Status: GET {api}/.../swedish-giro/{paymentId}/status, poll until
    transactionStatus settles (ACSC = accepted/settled, RJCT = rejected).

Known gap, confirmed against the docs rather than guessed: there is no
"mark this invoice as paid" write-back to the supplier's own accounting
system here — that would be a separate integration (and Zwapgrid's
Accounting API can't do it either: it's GET/POST-only, no PATCH, verified
separately). Once a payment settles here, "paid" is recorded only in *our
own* database (see pay_invoice's caller), not pushed anywhere else.

TODO(venue):
  - X-BicFi/PSU-ID/PSU-Corporate-ID below are the docs' own worked sandbox
    examples (ESSESESS = SEB's BIC), not a guess — confirm whether
    production requires different, real values per bank/PSU.
  - Wire the *redirect* flow for production (real BankID, real human,
    a callback endpoint) — this module only implements the sandbox-only
    decoupled path.
"""

import asyncio
import re
import time
import uuid
from datetime import date

import httpx

from app import config
from app.models import Invoice

_GIRO_RE = re.compile(r"\b(\d{2,4}-\d{4,7})\b")

_POLL_INTERVAL_SECONDS = 1.0
_POLL_MAX_ATTEMPTS = 30  # sandbox finalises within seconds; this is a generous ceiling

SETTLED_STATUSES = {"ACSC", "ACCC"}
_REJECTED_STATUSES = {"RJCT"}


class OpenPaymentsError(RuntimeError):
    """A payment was created but did not reach a settled status."""


def parse_creditor_giro(bank_account: str) -> dict:
    """"BG 123-4567" / "PG 123-4567" -> {"giroNumber": "123-4567", "giroType": ...}.
    Defaults to BANKGIRO if the prefix is missing or unrecognised — every
    seeded supplier account in this app is a bankgiro."""
    match = _GIRO_RE.search(bank_account)
    if not match:
        raise ValueError(f"not a recognisable bankgiro/plusgiro account: {bank_account!r}")
    giro_type = "PLUSGIRO" if re.search(r"\bPG\b", bank_account, re.IGNORECASE) else "BANKGIRO"
    return {"giroNumber": match.group(1), "giroType": giro_type}


class OpenPaymentsPISClient:
    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    def _require_config(self) -> None:
        if not config.OPENPAYMENTS_CLIENT_ID or not config.OPENPAYMENTS_CLIENT_SECRET:
            raise RuntimeError("OPENPAYMENTS_CLIENT_ID / OPENPAYMENTS_CLIENT_SECRET are not set")

    async def _access_token(self, client: httpx.AsyncClient) -> str:
        if self._token and time.monotonic() < self._token_expires_at:
            return self._token
        self._require_config()
        resp = await client.post(
            f"{config.OPENPAYMENTS_AUTH_BASE_URL}/connect/token",
            headers={"content-type": "application/x-www-form-urlencoded"},
            data={
                "client_id": config.OPENPAYMENTS_CLIENT_ID,
                "client_secret": config.OPENPAYMENTS_CLIENT_SECRET,
                "grant_type": "client_credentials",
                "scope": "paymentinitiation corporate",
            },
        )
        resp.raise_for_status()
        body = resp.json()
        self._token = body["access_token"]
        # Refresh a little early rather than racing the server's 1h expiry.
        self._token_expires_at = time.monotonic() + body.get("expires_in", 3600) - 30
        return self._token

    async def _headers(self, client: httpx.AsyncClient) -> dict:
        token = await self._access_token(client)
        return {
            "Authorization": f"Bearer {token}",
            "X-Request-ID": str(uuid.uuid4()),
            "X-BicFi": config.OPENPAYMENTS_BIC,
            "PSU-ID": config.OPENPAYMENTS_PSU_ID,
            "PSU-Corporate-ID": config.OPENPAYMENTS_PSU_CORPORATE_ID,
            "TPP-Redirect-Preferred": "false",  # sandbox: decoupled only, no real redirect
            "PSU-IP-Address": "127.0.0.1",
            "PSU-User-Agent": "TrustLayer/1.0",
        }

    async def _create_payment(self, client: httpx.AsyncClient, invoice: Invoice) -> str:
        headers = await self._headers(client)
        body = {
            "instructedAmount": {"amount": f"{invoice.amount_sek:.2f}", "currency": invoice.currency},
            "debtorAccount": {"iban": config.OPENPAYMENTS_DEBTOR_IBAN, "currency": invoice.currency},
            "creditorGiro": parse_creditor_giro(invoice.bank_account),
            "creditorName": invoice.supplier_name,
            "requestedExecutionDate": date.today().isoformat(),
            "invoiceRef": invoice.reference,
        }
        resp = await client.post(
            f"{config.OPENPAYMENTS_API_BASE_URL}/psd2/paymentinitiation/v1/payments/swedish-giro",
            headers={**headers, "content-type": "application/json"},
            json=body,
        )
        resp.raise_for_status()
        return resp.json()["paymentId"]

    async def _authorise_and_wait(self, client: httpx.AsyncClient, payment_id: str) -> None:
        base = f"{config.OPENPAYMENTS_API_BASE_URL}/psd2/paymentinitiation/v1/payments/swedish-giro/{payment_id}"

        resp = await client.post(f"{base}/authorisations", headers=await self._headers(client))
        resp.raise_for_status()
        created = resp.json()
        authorisation_id = created["authorisationId"]
        sca_methods = created.get("scaMethods", [])
        if not sca_methods:
            raise OpenPaymentsError(f"no SCA methods offered for payment {payment_id}")

        resp = await client.put(
            f"{base}/authorisations/{authorisation_id}",
            headers={**await self._headers(client), "content-type": "application/json"},
            json={"authenticationMethodId": sca_methods[0]["authenticationMethodId"]},
        )
        resp.raise_for_status()

        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = await client.get(
                f"{base}/authorisations/{authorisation_id}", headers=await self._headers(client)
            )
            resp.raise_for_status()
            sca_status = resp.json().get("scaStatus")
            if sca_status == "finalised":
                return
            if sca_status == "failed":
                raise OpenPaymentsError(f"authorisation failed for payment {payment_id}")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        raise OpenPaymentsError(f"authorisation for payment {payment_id} did not finalise in time")

    async def _wait_for_settlement(self, client: httpx.AsyncClient, payment_id: str) -> str:
        url = (
            f"{config.OPENPAYMENTS_API_BASE_URL}/psd2/paymentinitiation/v1/"
            f"payments/swedish-giro/{payment_id}/status"
        )
        for _ in range(_POLL_MAX_ATTEMPTS):
            resp = await client.get(url, headers=await self._headers(client))
            resp.raise_for_status()
            status = resp.json()["transactionStatus"]
            if status in SETTLED_STATUSES:
                return status
            if status in _REJECTED_STATUSES:
                raise OpenPaymentsError(f"payment {payment_id} was rejected ({status})")
            await asyncio.sleep(_POLL_INTERVAL_SECONDS)
        raise OpenPaymentsError(f"payment {payment_id} did not settle in time")

    async def pay_invoice(self, invoice: Invoice) -> dict:
        """Create, authorise (sandbox decoupled — auto-finalises) and confirm
        settlement of a Swedish Giro payment for this invoice. Raises
        OpenPaymentsError if the payment is rejected or never finalises."""
        async with httpx.AsyncClient(timeout=30) as client:
            payment_id = await self._create_payment(client, invoice)
            await self._authorise_and_wait(client, payment_id)
            status = await self._wait_for_settlement(client, payment_id)
        return {"payment_id": payment_id, "status": status}

    async def get_payment_status(self, payment_id: str) -> str:
        """One-shot status check (no polling/waiting) — for cross-checking a
        payment we already executed against Open Payments' own record of it,
        not just trusting what we cached locally."""
        url = (
            f"{config.OPENPAYMENTS_API_BASE_URL}/psd2/paymentinitiation/v1/"
            f"payments/swedish-giro/{payment_id}/status"
        )
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(url, headers=await self._headers(client))
            resp.raise_for_status()
            return resp.json()["transactionStatus"]
