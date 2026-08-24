"""Supplier website / announcement check — mock returns canned pages."""

from pydantic import BaseModel, Field

from app import seed
from app.tools.base import EvidenceTool


class WebIntelInput(BaseModel):
    supplier_name: str = Field(description="Supplier company name to search for")
    query: str = Field(description="What to look for, e.g. 'bank account change announcement'")


_DESCRIPTION = (
    "Search the supplier's public web presence (website, news, press releases) "
    "for announcements — e.g. did they publicly announce a change of bank?"
)


class MockWebIntelTool(EvidenceTool):
    name = "web_intel"
    description = _DESCRIPTION
    input_model = WebIntelInput

    async def lookup(self, supplier_name: str, query: str) -> dict:
        needle = supplier_name.lower()

        if "svea" in needle:  # legit_bank_change scenario: dated announcement exists
            return {
                "supplier_name": supplier_name, "query": query,
                "results": [{
                    "title": "Svea Kontorsmaterial byter bankförbindelse",
                    "url": "https://sveakontor.se/nyheter/vi-byter-bank",
                    "date": seed.LEGIT_ANNOUNCEMENT_DATE,
                    "snippet": ("Från och med 1 augusti sker alla betalningar till vårt nya "
                                f"bankgiro {seed.LEGIT_NEW_ACCOUNT} hos SEB. Fakturor ställda "
                                "till vårt gamla bankgiro betalas som vanligt till 30 september."),
                }],
                "summary": "Dated bank-change announcement found on the supplier's own website.",
            }

        if "skandinavisk byggpartner" in needle:  # ghost supplier: no web presence
            return {
                "supplier_name": supplier_name, "query": query,
                "results": [],
                "summary": "No website, no news mentions, no directory listings found.",
            }

        for s in seed.SUPPLIERS:
            if s["name"].lower() == needle:
                return {
                    "supplier_name": supplier_name, "query": query,
                    "results": [{
                        "title": s["name"],
                        "url": "https://" + s["email"].split("@")[1],
                        "date": None,
                        "snippet": "Company website with contact details and service pages. "
                                   "No announcements matching the query.",
                    }],
                    "summary": "Website exists; no announcement matching the query was found.",
                }

        return {
            "supplier_name": supplier_name, "query": query,
            "results": [],
            "summary": "Nothing relevant found.",
        }


class WebIntelRealTool(EvidenceTool):
    """Real adapter: web search / site scrape for supplier announcements.

    TODO(venue): wire a real search backend (e.g. a search API + fetch of the
    supplier's news page) and map -> the same dict shape as MockWebIntelTool.
    """

    name = "web_intel"
    description = _DESCRIPTION
    input_model = WebIntelInput

    async def lookup(self, supplier_name: str, query: str) -> dict:
        raise NotImplementedError("TODO: wire real web intel backend")
