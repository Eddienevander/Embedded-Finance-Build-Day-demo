"""Arbiter: weighs evidence + both debaters and issues a strict Verdict.

The verdict schema is enforced by the API, so there is no repair prompt; the
verify_manually fallback remains as a last resort for a truncated response.
"""

import json

from app.agents.base import STYLE_RULES, BaseAgent, truncate
from app.models import Argument, Claim, Evidence, SupplierBaseline, Verdict
from app.schemas import VerdictOut, clamp01

SYSTEM = """\
You are the ARBITER of an adversarial verification debate about a flagged B2B
invoice. You receive the claim, the investigator's evidence bundle, and the
arguments from both the skeptic (fraud) and the advocate (legitimate), including
one rebuttal round each. Weigh them and decide: approve, block, or verify_manually.

key_evidence must quote the Evidence findings that drove the decision, verbatim.
reasoning is 2-4 SHORT sentences in plain language. recommended_action is one
concrete imperative instruction for the payments team.

CRITICAL RULE: if your decision is "block" on a bank_account_changed claim, the
recommended_action MUST be to contact the supplier via the PREVIOUSLY KNOWN
contact channel from the baseline (the old email/phone on file) to confirm —
NEVER via the contact details printed on the suspicious invoice, because a
fraudster controls those.

DOMAIN RULE: on a duplicate_financing claim, if the evidence confirms the same
receivable (same supplier, amount and reference) was already submitted or paid,
the decision MUST be "block" — paying the same receivable twice is never correct,
and blocking the duplicate costs nothing (the supplier can re-issue if a second
payment is genuinely owed). Reserve verify_manually for when the duplicate
detection itself appears mistaken.
""" + STYLE_RULES

FALLBACK = Verdict(
    decision="verify_manually", confidence=0.0, key_evidence=[],
    reasoning="The arbiter's response could not be read as a valid verdict; "
              "defaulting to manual verification.",
    recommended_action="Route to manual review and contact the supplier via the "
                       "previously known contact channel.",
)


class Arbiter(BaseAgent):
    agent_name = "arbiter"
    temperature = 0.2

    async def decide(
        self,
        claim: Claim,
        evidence: list[Evidence],
        arguments: list[Argument],
        baseline: SupplierBaseline | None,
    ) -> Verdict:
        user = (
            f"CLAIM:\n{claim.model_dump_json(indent=2)}\n\n"
            "EVIDENCE BUNDLE:\n"
            + json.dumps([e.model_dump(mode="json") for e in evidence],
                         indent=2, ensure_ascii=False)
            + "\n\nDEBATE (openings then rebuttals):\n"
            + json.dumps([a.model_dump(mode="json") for a in arguments],
                         indent=2, ensure_ascii=False)
            + "\n\nPREVIOUSLY KNOWN CONTACT CHANNEL (from baseline, safe to use): "
            + (f"{baseline.contact_email}" if baseline else "none on file")
            + "\n\nIssue your verdict now."
        )
        try:
            await self.beat("deciding", "Weighing evidence and both arguments…")
            try:
                out = await self.stream_structured(
                    SYSTEM, [{"role": "user", "content": user}], VerdictOut, state="deciding"
                )
                verdict = Verdict(
                    decision=out.decision, confidence=clamp01(out.confidence),
                    key_evidence=out.key_evidence, reasoning=out.reasoning,
                    recommended_action=out.recommended_action,
                )
            except ValueError as e:  # truncated / unparseable response
                await self.beat("deciding",
                                f"Verdict unreadable ({truncate(str(e), 60)}), manual review")
                verdict = FALLBACK
            label = ("VERIFY_MANUALLY (evidence cuts both ways, a human decides)"
                     if verdict.decision == "verify_manually"
                     else f"{verdict.decision.upper()} ({verdict.confidence:.0%})")
            await self.beat("done", f"Verdict: {label}",
                            payload={"verdict": verdict.model_dump(mode="json")})
            return verdict
        except Exception as e:
            await self.beat("error", f"{type(e).__name__}: {truncate(str(e), 160)}")
            raise
