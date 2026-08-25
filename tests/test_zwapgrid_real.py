"""Mapping unit tests for the Zwapgrid adapter, driven by the example payload
from Zwapgrid's own docs (docs.zwapgrid.com/api-guide/accounting-api-guide/
supplier-invoices) so the mapping is checked against a real response shape,
not just a shape we imagined."""

from datetime import date

from app.tools.zwapgrid_real import _extract_bank_account, _format_orgnr, _to_invoice

EXAMPLE_ITEM = {
    "id": "INV-2024-001",
    "reference": "FX-98765",
    "issueDate": "2024-01-15",
    "dueDate": "2024-02-15",
    "createdDateTime": "2024-01-15T10:30:00Z",
    "modifiedDateTime": "2024-01-15T10:30:00Z",
    "accountingSupplierParty": {
        "customerAssignedAccountId": {"id": "5560123456", "schemeId": "SE:ORGNR"},
        "party": {
            "partyName": {"name": "Acme Supply Corp", "languageId": "ENG"},
            "postalAddress": {
                "streetName": "Main Street",
                "cityName": "Stockholm",
                "postalZone": "10001",
                "country": {"identificationCode": "SE", "name": "Sweden"},
            },
            "contact": {"name": "John Doe", "telephone": "+46812345678", "email": "contact@acme.se"},
        },
    },
    "notes": [{"text": "Payment via bank transfer to account ending in 1234", "languageId": "ENG"}],
    "totalBalanceAmount": {"amount": 15000.00, "currencyId": "SEK"},
    "legalMonetaryTotal": {
        "taxInclusiveAmount": {"amount": 18750.00, "currencyId": "SEK"},
        "prepaidAmount": {"amount": 0.00, "currencyId": "SEK"},
        "payableAmount": {"amount": 18750.00, "currencyId": "SEK"},
        "lineExtensionAmount": {"amount": 15000.00, "currencyId": "SEK"},
        "taxExclusiveAmount": {"amount": 15000.00, "currencyId": "SEK"},
    },
    "paymentStatus": {"status": "UNPAID", "settlementDate": None},
}


def test_format_orgnr_inserts_dash():
    assert _format_orgnr("5560123456") == "556012-3456"


def test_format_orgnr_leaves_already_dashed_alone():
    assert _format_orgnr("556012-3456") == "556012-3456"


def test_extract_bank_account_has_no_structured_field_in_the_example():
    # The example payload's note is prose, not a parseable account number —
    # this documents the real gap, it isn't a bug in the regex.
    assert _extract_bank_account(EXAMPLE_ITEM["notes"]) is None


def test_to_invoice_maps_the_documented_example():
    invoice = _to_invoice(EXAMPLE_ITEM)

    assert invoice.id == "INV-2024-001"
    assert invoice.supplier_orgnr == "556012-3456"
    assert invoice.supplier_name == "Acme Supply Corp"
    assert invoice.amount_sek == 18750.00
    assert invoice.currency == "SEK"
    assert invoice.bank_account == "UNKNOWN"
    assert invoice.reference == "FX-98765"
    assert invoice.issued_date == date(2024, 1, 15)
    assert invoice.due_date == date(2024, 2, 15)
    assert invoice.contact_email == "contact@acme.se"
    assert invoice.raw_note == "Payment via bank transfer to account ending in 1234"
