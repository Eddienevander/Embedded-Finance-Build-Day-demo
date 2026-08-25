"""Skeptic + Advocate: one class, two stances. Exactly two streamed LLM calls
per debater (opening + one rebuttal) — demo time budget."""

import json
from typing import Literal

from app.agents.base import BaseAgent, truncate
from app.models import Argument, Claim, Evidence
from app.schemas import ArgumentOut

STANCE_BRIEF = {
    "skeptic": ("You are the SKEPTIC. Argue that this claim indicates FRAUD and the "
                "payment should not be made. Attack weak evidence for legitimacy."),
    "advocate": ("You are the ADVOCATE. Argue that this claim has a LEGITIMATE "
                 "explanation and blocking would hurt a real supplier relationship. "
                 "Attack weak evidence for fraud."),
}

SYSTEM_TEMPLATE = """\
You are one side of an adversarial verification debate about a flagged B2B invoice.
{brief}
Ground every point in the evidence bundle you are given — cite concrete findings.
Give 3-5 points plus the single strongest argument you have.
"""


class Debater(BaseAgent):
    temperature = 0.7

    def __init__(self, stance: Literal["skeptic", "advocate"], case_id: str) -> None:
        super().__init__(case_id)
        self.stance: Literal["skeptic", "advocate"] = stance
        self.agent_name = stance

    def _system(self) -> str:
        return SYSTEM_TEMPLATE.format(brief=STANCE_BRIEF[self.stance])

    def _case_block(self, claim: Claim, evidence: list[Evidence]) -> str:
        return (
            f"CLAIM:\n{claim.model_dump_json(indent=2)}\n\n"
            "EVIDENCE BUNDLE:\n"
            + json.dumps([e.model_dump(mode="json") for e in evidence],
                         indent=2, ensure_ascii=False)
        )

    def _to_argument(self, out: ArgumentOut) -> Argument:
        return Argument(
            stance=self.stance,
            points=out.points[:5] or ["(no points)"],
            strongest_point=out.strongest_point or (out.points[0] if out.points else "(none)"),
        )

    async def opening(self, claim: Claim, evidence: list[Evidence]) -> Argument:
        try:
            await self.beat("arguing", "Composing opening argument…")
            out = await self.stream_structured(
                self._system(),
                [{"role": "user",
                  "content": self._case_block(claim, evidence)
                  + "\n\nMake your opening argument now."}],
                ArgumentOut,
            )
            arg = self._to_argument(out)
            await self.beat("done", f"Opening: {truncate(arg.strongest_point, 110)}",
                            payload={"argument": arg.model_dump(mode="json"), "round": 1})
            return arg
        except Exception as e:
            await self.beat("error", f"{type(e).__name__}: {truncate(str(e), 160)}")
            raise

    async def rebuttal(self, claim: Claim, evidence: list[Evidence],
                       own: Argument, opponent: Argument) -> Argument:
        try:
            await self.beat("arguing", "Rebutting the other side…")
            out = await self.stream_structured(
                self._system(),
                [{"role": "user",
                  "content": self._case_block(claim, evidence)
                  + f"\n\nYOUR OPENING ARGUMENT:\n{own.model_dump_json(indent=2)}"
                  + f"\n\nOPPONENT'S ARGUMENT:\n{opponent.model_dump_json(indent=2)}"
                  + "\n\nOne rebuttal round: address the opponent's strongest point and, "
                    "if warranted, revise your strongest point."}],
                ArgumentOut,
            )
            arg = self._to_argument(out)
            await self.beat("done", f"Rebuttal: {truncate(arg.strongest_point, 110)}",
                            payload={"argument": arg.model_dump(mode="json"), "round": 2})
            return arg
        except Exception as e:
            await self.beat("error", f"{type(e).__name__}: {truncate(str(e), 160)}")
            raise
