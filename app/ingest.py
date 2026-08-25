"""Invoice intake + change-of-state claim detection. Deterministic — no LLM."""

import re
import uuid

from app.bus import bus
from app.db import Database
from app.models import Claim, ClaimType, Invoice, SupplierBaseline, VerificationCase


def detect_claims(
    invoice: Invoice,
    baseline: SupplierBaseline | None,
    prior_invoices: list[dict],
) -> list[Claim]:
    """Diff the invoice against the supplier baseline. Pure function so the
    tests can drive it with fixtures."""

    def claim(ctype: ClaimType, summary: str, fields: dict) -> Claim:
        return Claim(
            id=f"CLM-{uuid.uuid4().hex[:8].upper()}", type=ctype,
            invoice_id=invoice.id, supplier_orgnr=invoice.supplier_orgnr,
            summary=summary, detected_fields=fields,
        )

    claims: list[Claim] = []

    if baseline is None:
        return [claim(
            ClaimType.NEW_SUPPLIER,
            f"First invoice ever from {invoice.supplier_name} ({invoice.supplier_orgnr}): no payment history.",
            {"supplier_orgnr": (None, invoice.supplier_orgnr)},
        )]

    if invoice.bank_account not in baseline.known_accounts:
        old = baseline.known_accounts[0] if baseline.known_accounts else None
        claims.append(claim(
            ClaimType.BANK_ACCOUNT_CHANGED,
            f"Bank account changed from {old} to {invoice.bank_account} "
            f"after {baseline.payment_count} payments to the old account.",
            {"bank_account": (old, invoice.bank_account)},
        ))

    actual_terms = (invoice.due_date - invoice.issued_date).days
    if baseline.typical_terms_days > 0 and actual_terms > 2 * baseline.typical_terms_days:
        claims.append(claim(
            ClaimType.TERMS_CHANGED,
            f"Payment terms jumped from ~{baseline.typical_terms_days} to {actual_terms} days.",
            {"payment_terms_days": (str(baseline.typical_terms_days), str(actual_terms))},
        ))

    for prior in prior_invoices:
        if (prior["id"] != invoice.id
                and prior["amount_sek"] == invoice.amount_sek
                and prior["reference"] == invoice.reference):
            claims.append(claim(
                ClaimType.DUPLICATE_FINANCING,
                f"Same receivable already submitted as invoice {prior['id']} "
                f"(ref {invoice.reference}, {invoice.amount_sek:,.0f} SEK).",
                {"duplicate_of": (prior["id"], invoice.id)},
            ))
            break

    return claims


_ORGNR_RE = re.compile(r"^\d{6}-\d{4}$")


def placeholder_reason(invoice: Invoice, known_supplier: bool = False) -> str | None:
    """Why an invoice is a placeholder/test record rather than a real one.
    Live feeds (the Zwapgrid sandbox, for one) contain seed rows like
    amount 0.00 and account "UNKNOWN". Returns None for anything worth
    verifying. A supplier we already hold a baseline for skips the orgnr
    format check: the sandbox's real supplier is keyed by a bare Fortnox
    supplier number ("1"), not an organisationsnummer."""
    if invoice.amount_sek <= 0:
        return f"non-positive amount ({invoice.amount_sek:g} SEK)"
    if not known_supplier and not _ORGNR_RE.match(invoice.supplier_orgnr.strip()):
        return f"malformed orgnr ({invoice.supplier_orgnr!r})"
    if invoice.bank_account.strip().upper() in ("", "UNKNOWN"):
        return "no bank account on the invoice"
    return None


async def process_invoice(invoice: Invoice, db: Database, force: bool = False) -> list[VerificationCase]:
    """Intake path: quarantine placeholders, then detect claims and either
    auto-approve or spawn one verification case per claim. Returns the created
    cases (empty on auto-approve or quarantine).

    `force=True` skips the placeholder check: it is a default recommendation
    for automatic intake (sync, scripted scenarios), not an unoverridable
    block. A human deliberately reviewing a specific invoice can send it
    through anyway."""
    from app.orchestrator import start_case  # late import to avoid a cycle

    baseline = db.get_baseline(invoice.supplier_orgnr)
    reason = None if force else placeholder_reason(invoice,
                                                   known_supplier=baseline is not None)
    if reason is not None:
        db.insert_invoice(invoice, status="invalid")
        await bus.emit(invoice.id, "system", "done",
                       f"Invoice {invoice.id} from {invoice.supplier_name!r} quarantined "
                       f"as a placeholder: {reason}. Not sent to verification.",
                       payload={"invoice": invoice.model_dump(mode="json"),
                                "quarantined": True})
        return []

    priors = db.get_invoices_for(invoice.supplier_orgnr)

    await bus.emit(invoice.id, "detector", "thinking",
                   f"Diffing {invoice.id} from {invoice.supplier_name} against baseline…")
    claims = detect_claims(invoice, baseline, priors)

    if not claims:
        db.insert_invoice(invoice, status="auto_approved")
        await bus.emit(invoice.id, "detector", "done",
                       "No change-of-state claims: matches supplier baseline.")
        await bus.emit(invoice.id, "system", "done",
                       f"Invoice {invoice.id} ({invoice.amount_sek:,.0f} SEK to "
                       f"{invoice.supplier_name}) auto-approved: no claims detected.",
                       payload={"invoice": invoice.model_dump(mode="json"),
                                "auto_approved": True})
        return []

    db.insert_invoice(invoice, status="under_review")
    await bus.emit(invoice.id, "detector", "done",
                   f"{len(claims)} claim(s): " + "; ".join(c.type.value for c in claims))

    cases = []
    for c in claims:
        case = VerificationCase(id=f"CASE-{uuid.uuid4().hex[:8].upper()}", claim=c)
        cases.append(case)
        start_case(case, invoice, baseline, db)
    return cases
