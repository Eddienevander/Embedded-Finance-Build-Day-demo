"""Prior invoices for this supplier, from our own SQLite. Always real."""

from datetime import date

from pydantic import BaseModel, Field

from app.db import Database
from app.tools.base import EvidenceTool


class InvoiceArchiveInput(BaseModel):
    orgnr: str = Field(description="Supplier organisationsnummer")
    invoice_id: str | None = Field(
        default=None, description="Optional: the incoming invoice id, to exclude it from the archive"
    )


class InvoiceArchiveTool(EvidenceTool):
    name = "invoice_archive"
    description = (
        "Our own invoice archive: the last 5 invoices from this supplier, with "
        "field-by-field values (amount, account, contact email, payment terms) "
        "to compare the new invoice against."
    )
    input_model = InvoiceArchiveInput

    def __init__(self, db: Database) -> None:
        self._db = db

    async def lookup(self, orgnr: str, invoice_id: str | None = None) -> dict:
        rows = [r for r in self._db.get_invoices_for(orgnr, limit=6) if r["id"] != invoice_id][:5]
        return {
            "orgnr": orgnr,
            "invoice_count_on_file": len(self._db.get_invoices_for(orgnr, limit=1000)),
            "last_5_invoices": [
                {
                    "id": r["id"],
                    "issued_date": r["issued_date"],
                    "amount_sek": r["amount_sek"],
                    "bank_account": r["bank_account"],
                    "reference": r["reference"],
                    "contact_email": r["contact_email"],
                    "terms_days": (date.fromisoformat(r["due_date"])
                                   - date.fromisoformat(r["issued_date"])).days,
                }
                for r in rows
            ],
        }
