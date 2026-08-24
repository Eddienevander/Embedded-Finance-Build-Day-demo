"""Arbiter: weighs evidence + both debaters and issues a strict Verdict.
On parse failure: one retry with the validation error, then verify_manually."""

import json

from pydantic import ValidationError

from app.agents.base import BaseAgent, extract_json, truncate
from app.models import Argument, Claim, Evidence, SupplierBaseline, Verdict

SYSTEM = """\
You are the ARBITER of an adversarial verification debate about a flagged B2B
invoice. You receive the claim, the investigator's evidence bundle, and the
arguments from both the skeptic (fraud) and the advocate (legitimate), including
one rebuttal round each. Weigh them and decide.

Respond with ONLY this JSON, no prose, no code fences:
{"decision": "approve" | "block" | "verify_manually",
 "confidence": <0.0-1.0>,
 "key_evidence": ["<verbatim Evidence.finding strings that drove the decision>"],
 "reasoning": "<2-4 sentences>",
 "recommended_action": "<one concrete next step for the payments team>"}

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
"""


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
            + "\n\nIssue your verdict now (JSON only)."
        )
        try:
            await self.beat("deciding", "Weighing evidence and both arguments…")
            messages = [{"role": "user", "content": user}]
            text = await self.stream_text(SYSTEM, messages, state="deciding")
            try:
                verdict = Verdict.model_validate(extract_json(text))
            except (ValidationError, ValueError) as first_err:
                await self.beat("deciding",
                                f"Verdict failed validation, retrying once: {truncate(str(first_err), 80)}")
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user",
                     "content": "Your JSON failed validation:\n"
                                f"{first_err}\n\nOutput ONLY the corrected JSON verdict."},
                ]
                try:
                    text = await self.stream_text(SYSTEM, messages, state="deciding")
                    verdict = Verdict.model_validate(extract_json(text))
                except (ValidationError, ValueError):
                    verdict = Verdict(
                        decision="verify_manually", confidence=0.0, key_evidence=[],
                        reasoning="Arbiter output could not be validated after one retry; "
                                  "defaulting to manual verification.",
                        recommended_action="Route to manual review and contact the supplier "
                                           "via the previously known contact channel.",
                    )
            await self.beat("done",
                            f"Verdict: {verdict.decision.upper()} ({verdict.confidence:.0%})",
                            payload={"verdict": verdict.model_dump(mode="json")})
            return verdict
        except Exception as e:
            await self.beat("error", f"{type(e).__name__}: {truncate(str(e), 160)}")
            raise
