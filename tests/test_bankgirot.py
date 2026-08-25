"""Bankgiro parsing + mod-10 validation (the real, offline layer)."""

import asyncio

from app import seed
from app.tools.bankgirot import MockBankgirotTool, luhn_valid, parse_bankgiro

TOOL = MockBankgirotTool()


def _lookup(account: str) -> dict:
    return asyncio.run(TOOL.lookup(account=account))


def test_parse_accepts_bankgiro_shapes_and_rejects_ibans():
    assert parse_bankgiro("BG 123-4566") == "1234566"
    assert parse_bankgiro("5678-9019") == "56789019"
    assert parse_bankgiro("SE45 5000 0000 0583 9825 7466") is None
    assert parse_bankgiro("UNKNOWN") is None


def test_all_seeded_accounts_carry_valid_check_digits():
    for s in seed.SUPPLIERS:
        assert luhn_valid(parse_bankgiro(s["account"])), s["account"]
    assert luhn_valid(parse_bankgiro(seed.LEGIT_NEW_ACCOUNT))
    assert luhn_valid(parse_bankgiro(seed.GHOST_ACCOUNT))


def test_tampered_check_digit_is_flagged():
    result = _lookup("BG 123-4567")  # last digit off by one
    assert result["is_bankgiro"] is True
    assert result["check_digit_valid"] is False
    assert "never issued" in result["note"]


def test_owner_lookup_confirms_seeded_supplier():
    result = _lookup(seed.SUPPLIERS[0]["account"])
    assert result["check_digit_valid"] is True
    assert result["owner_orgnr"] == seed.SUPPLIERS[0]["orgnr"]


def test_legit_new_account_maps_to_the_right_supplier():
    result = _lookup(seed.LEGIT_NEW_ACCOUNT)
    assert result["owner_orgnr"] == seed.LEGIT_SUPPLIER["orgnr"]


def test_iban_is_out_of_scope_not_an_error():
    result = _lookup("SE45 5000 0000 0583 9825 7466")
    assert result["is_bankgiro"] is False
