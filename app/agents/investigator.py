"""Investigator: gathers evidence via the tool registry. Never concludes."""

from app.agents.base import STYLE_RULES, BaseAgent, truncate
from app.models import Claim, Evidence, Invoice, SupplierBaseline
from app.schemas import EvidenceBundle, clamp01
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

If any account involved is a BANKGIRO number (NNN-NNNN or NNNN-NNNN, often
prefixed "BG"), also call bankgirot on it: it validates the check digit and can
confirm the registered owner. Skip it for IBANs — it cannot see those.

You may call several tools in parallel. When you have finished gathering, report
the evidence bundle: one entry per finding, each tagged as supporting "fraud",
"legit" or "neutral" with your confidence. Every finding must be a fact you
observed in a tool result, not an inference. Each finding is ONE short plain
sentence, at most 18 words, phrased as a business fact an accounts-payable
clerk can act on. Name sources in plain words (the company register, our
payment history, the supplier's website), never by tool name.
""" + STYLE_RULES


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
            f"{baseline.model_dump_json(indent=2) if baseline else 'none, supplier never seen before'}\n\n"
            "Gather evidence about this claim, then report the evidence bundle."
        )
        try:
            bundle = await self.run_tool_loop(
                SYSTEM, [{"role": "user", "content": user}], registry, EvidenceBundle
            )
            evidence = [
                Evidence(tool=i.tool, query=i.query, finding=i.finding,
                         supports=i.supports, confidence=clamp01(i.confidence))
                for i in bundle.evidence
            ]
            if not evidence:
                raise ValueError("investigator returned an empty evidence bundle")
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
