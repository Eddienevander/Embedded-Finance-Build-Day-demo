"""Claim detector unit tests — pure Python, no LLM, no DB."""

from datetime import date, timedelta

from app.ingest import detect_claims
from app.models import ClaimType, Invoice, SupplierBaseline

TODAY = date(2026, 8, 24)


def make_invoice(**overrides) -> Invoice:
    values = dict(
        id="INV-TEST0001",
        supplier_orgnr="556677-8899",
        supplier_name="Nordisk Ställning AB",
        amount_sek=180_000.0,
        bank_account="BG 123-4567",
        reference="F2026-1001",
        issued_date=TODAY,
        due_date=TODAY + timedelta(days=30),
        contact_email="ekonomi@nordiskstallning.se",
    )
    values.update(overrides)
    return Invoice(**values)


def make_baseline(**overrides) -> SupplierBaseline:
    values = dict(
        orgnr="556677-8899",
        name="Nordisk Ställning AB",
        known_accounts=["BG 123-4567"],
        payment_count=30,
        avg_amount_sek=180_000.0,
        typical_terms_days=30,
        first_seen=TODAY - timedelta(days=365),
        contact_email="ekonomi@nordiskstallning.se",
    )
    values.update(overrides)
    return SupplierBaseline(**values)


def test_clean_invoice_yields_zero_claims():
    assert detect_claims(make_invoice(), make_baseline(), []) == []


def test_new_supplier_when_no_baseline():
    claims = detect_claims(make_invoice(supplier_orgnr="559999-1234"), None, [])
    assert [c.type for c in claims] == [ClaimType.NEW_SUPPLIER]
    assert claims[0].detected_fields["supplier_orgnr"] == (None, "559999-1234")


def test_bank_account_changed_uses_most_used_known_account_as_old():
    baseline = make_baseline(known_accounts=["BG 123-4567", "BG 999-0000"])
    invoice = make_invoice(bank_account="SE45 5000 0000 0583 9825 7466")
    claims = detect_claims(invoice, baseline, [])
    assert [c.type for c in claims] == [ClaimType.BANK_ACCOUNT_CHANGED]
    old, new = claims[0].detected_fields["bank_account"]
    assert old == "BG 123-4567"
    assert new == "SE45 5000 0000 0583 9825 7466"


def test_terms_changed_when_terms_exceed_double_typical():
    invoice = make_invoice(due_date=TODAY + timedelta(days=61))  # typical 30 → >2x
    claims = detect_claims(invoice, make_baseline(), [])
    assert [c.type for c in claims] == [ClaimType.TERMS_CHANGED]

    # exactly 2x is still fine
    invoice_ok = make_invoice(due_date=TODAY + timedelta(days=60))
    assert detect_claims(invoice_ok, make_baseline(), []) == []


def test_duplicate_financing_on_same_receivable_with_different_id():
    prior = {"id": "INV-ORIGINAL", "amount_sek": 180_000.0, "reference": "F2026-1001"}
    claims = detect_claims(make_invoice(), make_baseline(), [prior])
    assert [c.type for c in claims] == [ClaimType.DUPLICATE_FINANCING]
    assert claims[0].detected_fields["duplicate_of"] == ("INV-ORIGINAL", "INV-TEST0001")

    # the same invoice id re-submitted is not a duplicate claim
    same = {"id": "INV-TEST0001", "amount_sek": 180_000.0, "reference": "F2026-1001"}
    assert detect_claims(make_invoice(), make_baseline(), [same]) == []


def test_account_change_and_duplicate_can_both_fire():
    prior = {"id": "INV-ORIGINAL", "amount_sek": 180_000.0, "reference": "F2026-1001"}
    invoice = make_invoice(bank_account="SE45 5000 0000 0583 9825 7466")
    types = {c.type for c in detect_claims(invoice, make_baseline(), [prior])}
    assert types == {ClaimType.BANK_ACCOUNT_CHANGED, ClaimType.DUPLICATE_FINANCING}
