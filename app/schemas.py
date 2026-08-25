"""Structured-output schemas for the agents.

The Anthropic API guarantees responses matching these schemas (json_schema
output format), so agents parse rather than scrape: no code fences, no repair
prompts. Kept separate from models.py because the wire schema must stay
JSON-Schema-friendly (no tuples, no free-form dicts, no constraints the API
rejects) while models.py stays the domain model.
"""

from typing import Literal

from pydantic import BaseModel


class EvidenceItem(BaseModel):
    tool: str
    query: str
    finding: str
    supports: Literal["fraud", "legit", "neutral"]
    confidence: float


class EvidenceBundle(BaseModel):
    evidence: list[EvidenceItem]


class ArgumentOut(BaseModel):
    points: list[str]
    strongest_point: str


class VerdictOut(BaseModel):
    decision: Literal["approve", "block", "verify_manually"]
    confidence: float
    key_evidence: list[str]
    reasoning: str
    recommended_action: str


def strict_schema(model: type[BaseModel]) -> dict:
    """Pydantic JSON schema, made API-strict: every object must explicitly set
    additionalProperties=false (including nested $defs) or the API 400s."""
    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                node["additionalProperties"] = False
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)
        return node

    return walk(model.model_json_schema())


def clamp01(value: float) -> float:
    """Confidences are free-form floats on the wire; the domain models constrain
    them to 0..1. Clamp rather than fail a case on a stray 1.2."""
    return max(0.0, min(1.0, float(value)))
