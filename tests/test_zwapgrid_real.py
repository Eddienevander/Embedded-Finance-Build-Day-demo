"""Mapping unit tests for the Zwapgrid adapter, driven by the example payload
from Zwapgrid's own docs (docs.zwapgrid.com/api-guide/accounting-api-guide/
supplier-invoices) so the mapping is checked against a real response shape,
not just a shape we imagined."""

import copy
from datetime import date

import pytest

from app.tools.zwapgrid_real import (
    ZwapgridPaymentHistoryTool,
    _extract_bank_account,
    _format_orgnr,
    _to_invoice,
)

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


class _StubZwapgridClient:
    """Stands in for ZwapgridRealTool so the history tool is testable without
    hitting the network."""

    def __init__(self, invoices):
        self._invoices = invoices
        self.calls = 0

    async def fetch_incoming_invoices(self):
        self.calls += 1
        return self._invoices


def _invoice_for(orgnr: str, account_note: str, issue_date: str, due_date: str):
    item = copy.deepcopy(EXAMPLE_ITEM)
    item["accountingSupplierParty"]["customerAssignedAccountId"]["id"] = orgnr
    item["notes"] = [{"text": account_note, "languageId": "ENG"}]
    item["issueDate"] = issue_date
    item["dueDate"] = due_date
    return _to_invoice(item)


ACME = "5560123456"  # -> 556012-3456
OTHER = "5569876543"  # -> 556987-6543


@pytest.mark.asyncio
async def test_payment_history_filters_by_orgnr_and_counts_accounts():
    invoices = [
        _invoice_for(ACME, "Pay to SE4550000000058398257466", "2024-01-15", "2024-02-15"),
        _invoice_for(ACME, "Pay to SE4550000000058398257466", "2024-03-01", "2024-04-01"),
        _invoice_for(ACME, "Pay to 123-4567", "2024-06-01", "2024-07-01"),
        _invoice_for(OTHER, "Pay to 999-9999", "2024-05-01", "2024-06-01"),
    ]
    tool = ZwapgridPaymentHistoryTool(client=_StubZwapgridClient(invoices))

    result = await tool.lookup(orgnr="556012-3456")

    assert result["orgnr"] == "556012-3456"
    assert result["payment_count"] == 3
    assert result["first_payment"] == "2024-01-15"
    assert result["last_payment"] == "2024-06-01"
    assert result["accounts"] == [
        {"account": "SE4550000000058398257466", "payments": 2},
        {"account": "123-4567", "payments": 1},
    ]


@pytest.mark.asyncio
async def test_payment_history_reports_zero_for_unseen_account():
    tool = ZwapgridPaymentHistoryTool(client=_StubZwapgridClient([]))

    result = await tool.lookup(orgnr="556012-3456", account="SE99UNKNOWN")

    assert result["payment_count"] == 0
    assert result["accounts"] == [{"account": "SE99UNKNOWN", "payments": 0}]
    assert result["first_payment"] is None


@pytest.mark.asyncio
async def test_payment_history_caches_within_ttl():
    stub = _StubZwapgridClient([_invoice_for(ACME, "no account here", "2024-01-15", "2024-02-15")])
    tool = ZwapgridPaymentHistoryTool(client=stub)

    await tool.lookup(orgnr="556012-3456")
    await tool.lookup(orgnr="556012-3456")

    assert stub.calls == 1
