"""Bankgiro number verification — by parsing, because Bankgirot has no API.

Two layers, honest about which is which:

1. Check-digit validation (REAL, offline, every mode). Bankgiro numbers are
   7-8 digits whose last digit is a Luhn/mod-10 check digit. A number that
   fails the checksum was never issued by Bankgirot — a strong, free signal
   against typo-squatted or fabricated payment slips.

2. Owner lookup (MOCK here, parseable in principle). Bankgirot publishes a
   public number search on bankgirot.se (form-driven website, no API). The
   real adapter is a TODO(venue) HTML-parsing stub; the mock serves seeded
   fixtures so MOCK_MODE stays deterministic.

Pitch context: Norway's KAR (Konto- og adresseringsregister, run by the banks
via Bits) does account-to-owner verification as infrastructure. Sweden has no
equivalent for arbitrary accounts — but for bankgiro numbers specifically,
this tool shows a slice of it can be built today by parsing what Bankgirot
already publishes.
"""

import re

from pydantic import BaseModel, Field

from app import seed
from app.tools.base import EvidenceTool

_BANKGIRO_RE = re.compile(r"^(?:BG\s*)?(\d{3,4})-(\d{4})$", re.IGNORECASE)


def parse_bankgiro(account: str) -> str | None:
    """Digits of a bankgiro number, or None when the string isn't one
    (IBANs and free text fall through)."""
    m = _BANKGIRO_RE.match(account.strip())
    return (m.group(1) + m.group(2)) if m else None


def luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


class BankgirotInput(BaseModel):
    account: str = Field(description="The account string exactly as printed on the invoice")


class MockBankgirotTool(EvidenceTool):
    name = "bankgirot"
    description = (
        "Verify a bankgiro number: mod-10 check-digit validation (offline, always "
        "real) plus owner lookup (parsed from Bankgirot's public number search; "
        "mocked here). Only call this for bankgiro numbers, not IBANs."
    )
    input_model = BankgirotInput

    def _owners(self) -> dict[str, dict]:
        owners = {
            parse_bankgiro(s["account"]): {"owner_name": s["name"], "owner_orgnr": s["orgnr"]}
            for s in seed.SUPPLIERS
        }
        owners[parse_bankgiro(seed.LEGIT_NEW_ACCOUNT)] = {
            "owner_name": seed.LEGIT_SUPPLIER["name"],
            "owner_orgnr": seed.LEGIT_SUPPLIER["orgnr"],
        }
        return owners

    async def lookup(self, account: str) -> dict:
        digits = parse_bankgiro(account)
        if digits is None:
            return {
                "account": account, "is_bankgiro": False,
                "note": "Not a bankgiro number (IBAN or other format) — "
                        "Bankgirot cannot say anything about it.",
            }
        result = {
            "account": account, "is_bankgiro": True,
            "check_digit_valid": luhn_valid(digits),
        }
        if not result["check_digit_valid"]:
            result["note"] = ("Invalid mod-10 check digit: this number was never "
                              "issued by Bankgirot.")
            return result
        owner = self._owners().get(digits)
        if owner:
            result.update(owner)
            result["note"] = "Number registered; owner confirmed via Bankgirot's public search."
        else:
            result["note"] = ("Valid check digit, but no owner found in Bankgirot's "
                              "public number search.")
        return result


class BankgirotPublicLookup:
    """Real owner lookup by parsing Bankgirot's public number search.

    TODO(venue): wire this in.
      - Bankgirot exposes no API; the public search at
        https://www.bankgirot.se/sok-bankgironummer is a form POST returning
        HTML with the account holder's name for a valid bankgiro number.
      - Submit the form with httpx, parse the holder name out of the result
        table, map -> the same dict shape as MockBankgirotTool.lookup.
      - Rate-limit and cache aggressively; it is a public website, not an API.
    """

    async def lookup(self, account: str) -> dict:
        raise NotImplementedError("TODO: parse Bankgirot's public number search")
