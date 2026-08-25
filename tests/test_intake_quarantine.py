"""Placeholder invoices from live feeds must be quarantined, not verified."""

from datetime import date, timedelta

from app.ingest import placeholder_reason
from app.models import Invoice
from app.seed import make_scenario_invoices

TODAY = date(2026, 8, 25)


def make_invoice(**overrides) -> Invoice:
    values = dict(
        id="INV-Q1",
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


def test_zwapgrid_sandbox_placeholder_is_quarantined():
    # the exact shape the live sandbox feed produced
    inv = make_invoice(supplier_orgnr="1", supplier_name="Cloudlane Systems AB - Seed",
                       amount_sek=0.0, bank_account="UNKNOWN", contact_email="")
    assert placeholder_reason(inv) is not None


def test_each_placeholder_field_is_caught_individually():
    assert "amount" in placeholder_reason(make_invoice(amount_sek=0.0))
    assert "orgnr" in placeholder_reason(make_invoice(supplier_orgnr="1"))
    assert "bank account" in placeholder_reason(make_invoice(bank_account="UNKNOWN"))
    assert "bank account" in placeholder_reason(make_invoice(bank_account=""))


def test_real_invoice_is_not_quarantined():
    assert placeholder_reason(make_invoice()) is None


def test_no_scenario_invoice_is_quarantined():
    # the ghost supplier has a fabricated but well-formed orgnr: it must reach
    # the pipeline (that scenario IS the pipeline's job), not the quarantine
    for name in ("clean", "account_swap", "ghost_supplier",
                 "legit_bank_change", "double_finance"):
        for inv in make_scenario_invoices(name):
            assert placeholder_reason(inv) is None, name
