"""Investigator: gathers evidence via the tool registry. Never concludes."""

import json

from app.agents.base import BaseAgent, extract_json, truncate
from app.models import Claim, Evidence, Invoice, SupplierBaseline
from app.tools.base import ToolRegistry

SYSTEM = """\
You are a financial crime investigator at a B2B payments platform. A deterministic
detector flagged a change-of-state claim on an incoming invoice. Your only job is
to GATHER EVIDENCE about this claim using the tools available. Do not conclude,
judge, or recommend — a separate debate and arbiter stage does that.

You MUST call each of these tools at least once before finishing:
- bolagsverket: look up the supplier's orgnr
- payment_history: the supplier's orgnr, AND pass the invoice's bank account as `account`
- account_registry: the bank account printed on the NEW invoice (exactly as printed)
- web_intel: check for public announcements relevant to the claim (e.g. a bank change)
- invoice_archive: field-by-field comparison against this supplier's prior invoices

You may call several tools in parallel. When you have finished gathering, output
ONLY a JSON array of evidence objects and nothing else — no prose, no code fences:
[
  {"tool": "<tool name>", "query": "<what you looked up>",
   "finding": "<one factual sentence>",
   "supports": "fraud" | "legit" | "neutral",
   "confidence": <0.0-1.0>}
]
Every finding must be a fact you observed in a tool result, not an inference.
"""


class Investigator(BaseAgent):
    agent_name = "investigator"
    temperature = 0.2

    async def investigate(
        self,
        claim: Claim,
        invoice: Invoice,
        baseline: SupplierBaseline | None,
        registry: ToolRegistry,
    ) -> list[Evidence]:
        user = (
            f"CLAIM:\n{claim.model_dump_json(indent=2)}\n\n"
            f"INCOMING INVOICE:\n{invoice.model_dump_json(indent=2)}\n\n"
            f"SUPPLIER BASELINE (from our records):\n"
            f"{baseline.model_dump_json(indent=2) if baseline else 'none — supplier never seen before'}\n\n"
            "Gather evidence about this claim, then output the JSON evidence array."
        )
        try:
            final_text = await self.run_tool_loop(
                SYSTEM, [{"role": "user", "content": user}], registry
            )
            items = extract_json(final_text)
            if not isinstance(items, list):
                raise ValueError("investigator output was not a JSON array")
            evidence: list[Evidence] = []
            for item in items:
                try:
                    item.pop("raw", None)
                    evidence.append(Evidence(**item))
                except Exception:
                    continue  # drop malformed items rather than fail the case
            if not evidence:
                raise ValueError("no valid evidence objects in investigator output")
            await self.beat(
                "done",
                f"Collected {len(evidence)} pieces of evidence "
                f"({sum(1 for e in evidence if e.supports == 'fraud')} fraud / "
                f"{sum(1 for e in evidence if e.supports == 'legit')} legit)",
                payload={"evidence_list": [e.model_dump(mode='json') for e in evidence]},
            )
            return evidence
        except Exception as e:
            await self.beat("error", f"{type(e).__name__}: {truncate(str(e), 160)}")
            raise
