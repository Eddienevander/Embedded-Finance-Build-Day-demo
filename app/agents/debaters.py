"""Skeptic + Advocate: one class, two stances. Exactly two streamed LLM calls
per debater (opening + one rebuttal) — demo time budget."""

import json
from typing import Literal

from app.agents.base import BaseAgent, extract_json, truncate
from app.models import Argument, Claim, Evidence

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
Respond with ONLY this JSON, no prose, no code fences:
{{"points": ["<one sentence>", "..."], "strongest_point": "<your single best argument>"}}
Use 3-5 points.
"""


def _fallback_argument(stance: Literal["skeptic", "advocate"], text: str) -> Argument:
    line = truncate(text.replace("\n", " "), 200) or f"({stance} produced no parseable argument)"
    return Argument(stance=stance, points=[line], strongest_point=line)


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

    def _parse(self, text: str) -> Argument:
        try:
            data = extract_json(text)
            return Argument(
                stance=self.stance,
                points=[str(p) for p in data.get("points", [])][:5] or ["(no points)"],
                strongest_point=str(data.get("strongest_point", ""))
                or str(data.get("points", ["(none)"])[0]),
            )
        except Exception:
            return _fallback_argument(self.stance, text)

    async def opening(self, claim: Claim, evidence: list[Evidence]) -> Argument:
        try:
            await self.beat("arguing", "Composing opening argument…")
            text = await self.stream_text(
                self._system(),
                [{"role": "user",
                  "content": self._case_block(claim, evidence)
                  + "\n\nMake your opening argument now (JSON only)."}],
            )
            arg = self._parse(text)
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
            text = await self.stream_text(
                self._system(),
                [{"role": "user",
                  "content": self._case_block(claim, evidence)
                  + f"\n\nYOUR OPENING ARGUMENT:\n{own.model_dump_json(indent=2)}"
                  + f"\n\nOPPONENT'S ARGUMENT:\n{opponent.model_dump_json(indent=2)}"
                  + "\n\nOne rebuttal round: address the opponent's strongest point and, "
                    "if warranted, revise your strongest_point. Respond with the same "
                    "JSON shape only."}],
            )
            arg = self._parse(text)
            await self.beat("done", f"Rebuttal: {truncate(arg.strongest_point, 110)}",
                            payload={"argument": arg.model_dump(mode="json"), "round": 2})
            return arg
        except Exception as e:
            await self.beat("error", f"{type(e).__name__}: {truncate(str(e), 160)}")
            raise
