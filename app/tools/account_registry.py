"""Account -> owner (orgnr) ownership lookup. MOCK ONLY.

This API does not exist in Sweden today — a shared account-ownership registry
(who owns bankgiro/IBAN X?) is exactly the missing infrastructure this demo's
pitch argues for (cf. IMY's bank-data-sharing sandbox). The mock shows what
fraud detection looks like the day it exists.
"""

from pydantic import BaseModel, Field

from app import seed
from app.tools.base import EvidenceTool


class AccountRegistryInput(BaseModel):
    account: str = Field(description="Bankgiro or IBAN string exactly as printed on the invoice")


def _norm(account: str) -> str:
    return account.replace(" ", "").upper()


class MockAccountRegistryTool(EvidenceTool):
    name = "account_registry"
    description = (
        "Account ownership registry (FICTIONAL API — mock only): who owns a "
        "given bankgiro/IBAN? Returns owner orgnr/name, account age in days, "
        "and risk flags such as recently_opened."
    )
    input_model = AccountRegistryInput

    async def lookup(self, account: str) -> dict:
        fixtures: dict[str, dict] = {
            _norm(seed.ATTACK_IBAN): {
                "found": True, "account": account,
                "owner_orgnr": None,
                "owner_name": "E. Lindqvist (privatperson)",
                "owner_type": "private_individual",
                "account_age_days": 19,
                "flags": ["recently_opened", "private_individual"],
            },
            _norm(seed.LEGIT_NEW_ACCOUNT): {
                "found": True, "account": account,
                "owner_orgnr": seed.LEGIT_SUPPLIER["orgnr"],
                "owner_name": seed.LEGIT_SUPPLIER["name"],
                "owner_type": "company",
                "account_age_days": 24,
                "flags": [],
            },
        }
        hit = fixtures.get(_norm(account))
        if hit:
            return hit
        for s in seed.SUPPLIERS:
            if _norm(s["account"]) == _norm(account):
                return {
                    "found": True, "account": account,
                    "owner_orgnr": s["orgnr"], "owner_name": s["name"],
                    "owner_type": "company",
                    "account_age_days": 2600,
                    "flags": [],
                }
        return {
            "found": False, "account": account,
            "note": "Account not present in the ownership registry.",
            "flags": ["unknown_account"],
        }
