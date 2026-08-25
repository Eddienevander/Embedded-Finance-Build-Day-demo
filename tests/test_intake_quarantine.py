"""Placeholder quarantine: a soft default for automatic intake.

force=True (a human deliberately reviewing) bypasses it, and a supplier we
already hold a baseline for skips the orgnr-format check (the Zwapgrid
sandbox's real supplier is keyed by a bare Fortnox number, not an orgnr).
"""

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
        bank_account="BG 123-4566",
        reference="F2026-1001",
        issued_date=TODAY,
        due_date=TODAY + timedelta(days=30),
        contact_email="ekonomi@nordiskstallning.se",
    )
    values.update(overrides)
    return Invoice(**values)


def test_zwapgrid_seed_placeholder_is_flagged():
    inv = make_invoice(supplier_orgnr="1", amount_sek=0.0, bank_account="UNKNOWN")
    assert placeholder_reason(inv) is not None


def test_each_placeholder_field_is_caught_individually():
    assert "amount" in placeholder_reason(make_invoice(amount_sek=0.0))
    assert "orgnr" in placeholder_reason(make_invoice(supplier_orgnr="1"))
    assert "bank account" in placeholder_reason(make_invoice(bank_account="UNKNOWN"))


def test_known_supplier_skips_the_orgnr_format_check():
    # Cloudlane: real sandbox supplier, Fortnox number "1", has a baseline
    inv = make_invoice(supplier_orgnr="1", supplier_name="Cloudlane Systems AB - Seed",
                       amount_sek=44_900.0, bank_account="543-2109")
    assert placeholder_reason(inv, known_supplier=False) is not None
    assert placeholder_reason(inv, known_supplier=True) is None


def test_real_invoice_is_not_flagged():
    assert placeholder_reason(make_invoice()) is None


def test_no_scenario_invoice_is_flagged():
    for name in ("clean", "account_swap", "ghost_supplier",
                 "legit_bank_change", "double_finance"):
        for inv in make_scenario_invoices(name):
            assert placeholder_reason(inv) is None, name
