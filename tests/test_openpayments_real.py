"""Unit tests for the Open Payments Europe (PIS) adapter's pure mapping logic.
No network — the PSU-ID/BIC/polling behavior is exercised manually against
the real sandbox, not in CI."""

import pytest

from app.tools.openpayments_real import parse_creditor_giro


def test_parse_bankgiro_default():
    assert parse_creditor_giro("BG 123-4567") == {"giroNumber": "123-4567", "giroType": "BANKGIRO"}


def test_parse_plusgiro():
    assert parse_creditor_giro("PG 123-4567") == {"giroNumber": "123-4567", "giroType": "PLUSGIRO"}


def test_parse_bare_number_defaults_to_bankgiro():
    # Every seeded supplier account in app/seed.py is a bankgiro; an
    # unprefixed number should still be usable rather than rejected.
    assert parse_creditor_giro("123-4567") == {"giroNumber": "123-4567", "giroType": "BANKGIRO"}


def test_parse_unrecognisable_account_raises():
    with pytest.raises(ValueError):
        parse_creditor_giro("SE4550000000058398257466")
