"""The invoice archive must never feed suspect invoices back as 'history'."""

import asyncio
from datetime import date, timedelta

from app.db import Database
from app.models import Invoice
from app.tools.invoice_archive import InvoiceArchiveTool

ORGNR = "556677-8899"
TODAY = date(2026, 8, 24)


def _invoice(inv_id: str, account: str) -> Invoice:
    return Invoice(
        id=inv_id, supplier_orgnr=ORGNR, supplier_name="Nordisk Ställning AB",
        amount_sek=180_000.0, bank_account=account, reference=f"F-{inv_id}",
        issued_date=TODAY, due_date=TODAY + timedelta(days=30),
        contact_email="ekonomi@nordiskstallning.se",
    )


def test_archive_hides_under_review_and_blocked_invoices(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.insert_invoice(_invoice("INV-PAID", "BG 123-4566"), status="paid")
    db.insert_invoice(_invoice("INV-AUTO", "BG 123-4566"), status="auto_approved")
    db.insert_invoice(_invoice("INV-SUSPECT", "SE45 FAKE"), status="under_review")
    db.insert_invoice(_invoice("INV-BLOCKED", "SE45 FAKE"), status="blocked")

    result = asyncio.run(InvoiceArchiveTool(db).lookup(orgnr=ORGNR))

    returned = {row["id"] for row in result["last_5_invoices"]}
    assert returned == {"INV-PAID", "INV-AUTO"}
    assert result["invoice_count_on_file"] == 2


def test_archive_still_excludes_the_invoice_under_investigation(tmp_path):
    db = Database(str(tmp_path / "t.db"))
    db.insert_invoice(_invoice("INV-PAID", "BG 123-4566"), status="paid")
    db.insert_invoice(_invoice("INV-CURRENT", "BG 123-4566"), status="paid")

    result = asyncio.run(InvoiceArchiveTool(db).lookup(orgnr=ORGNR, invoice_id="INV-CURRENT"))

    assert {row["id"] for row in result["last_5_invoices"]} == {"INV-PAID"}
