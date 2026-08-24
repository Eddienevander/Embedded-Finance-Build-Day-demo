"""Company status/address/orgnr lookups — mock + real stub."""

from pydantic import BaseModel, Field

from app import seed
from app.tools.base import EvidenceTool


class BolagsverketInput(BaseModel):
    orgnr: str = Field(description="Swedish organisationsnummer, e.g. 556677-8899")


_DESCRIPTION = (
    "Look up a Swedish company at Bolagsverket by organisationsnummer. "
    "Returns registration status (Aktiv/Likvidation/not found), registered "
    "address, registration date and SNI industry code."
)

_ADDRESSES = {
    "556677-8899": "Ställningsvägen 4, 141 47 Huddinge",
    "556234-1122": "Kontorsgatan 12, 111 52 Stockholm",
    "556891-3344": "Industrigatan 8, 722 12 Västerås",
    "556455-7788": "Grusvägen 21, 441 39 Alingsås",
    "556712-9911": "Snickarvägen 3, 831 45 Östersund",
    "556388-2255": "Terminalvägen 15, 973 45 Luleå",
    "556990-4433": "Fleminggatan 30, 112 26 Stockholm",
    "556533-6677": "Verkstadsgatan 7, 211 24 Malmö",
}


class MockBolagsverketTool(EvidenceTool):
    name = "bolagsverket"
    description = _DESCRIPTION
    input_model = BolagsverketInput

    async def lookup(self, orgnr: str) -> dict:
        orgnr = orgnr.strip()
        for s in seed.SUPPLIERS:
            if s["orgnr"] == orgnr:
                return {
                    "found": True,
                    "orgnr": orgnr,
                    "name": s["name"],
                    "status": "Aktiv",
                    "registered_address": _ADDRESSES.get(orgnr, "Storgatan 1, 111 11 Stockholm"),
                    "registration_date": "2009-03-17",
                    "sni_code": "43.999 — Specialiserad bygg- och anläggningsverksamhet",
                }
        return {
            "found": False,
            "orgnr": orgnr,
            "status": "NOT FOUND",
            "note": "No company with this organisationsnummer is registered at Bolagsverket.",
        }


class BolagsverketRealTool(EvidenceTool):
    """Real adapter targeting Bolagsverket's free 'värdefulla datamängder' REST
    API (orgnr -> status/address).

    TODO(venue): wire this in.
      - Register for API access (onboarding required):
        https://bolagsverket.se/apierochoppnadata
      - GET https://gw.api.bolagsverket.se/vardefulla-datamangder/v1/organisationer/{orgnr}
      - Map response -> the same dict shape as MockBolagsverketTool.
    """

    name = "bolagsverket"
    description = _DESCRIPTION
    input_model = BolagsverketInput

    async def lookup(self, orgnr: str) -> dict:
        raise NotImplementedError("TODO: wire Bolagsverket värdefulla datamängder API")
