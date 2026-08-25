"""All Pydantic models. Frozen first — everything else depends on these."""

from datetime import date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field


class ClaimType(str, Enum):
    BANK_ACCOUNT_CHANGED = "bank_account_changed"
    NEW_SUPPLIER = "new_supplier"
    TERMS_CHANGED = "terms_changed"
    DUPLICATE_FINANCING = "duplicate_financing"


class Invoice(BaseModel):
    id: str
    supplier_orgnr: str
    supplier_name: str
    amount_sek: float
    currency: str = "SEK"
    bank_account: str  # bankgiro or IBAN string
    reference: str
    due_date: date
    issued_date: date
    contact_email: str
    raw_note: str | None = None  # e.g. "We have switched banks"


class SupplierBaseline(BaseModel):
    orgnr: str
    name: str
    known_accounts: list[str]  # most-used first
    payment_count: int
    avg_amount_sek: float
    typical_terms_days: int
    first_seen: date
    contact_email: str


class Claim(BaseModel):
    id: str
    type: ClaimType
    invoice_id: str
    supplier_orgnr: str
    summary: str  # human sentence: "Bank account changed from X to Y"
    detected_fields: dict[str, tuple[str | None, str | None]]  # field -> (old, new)


class Evidence(BaseModel):
    tool: str
    query: str
    finding: str
    supports: Literal["fraud", "legit", "neutral"]
    confidence: float = Field(ge=0.0, le=1.0)  # set by the investigator
    raw: dict | None = None


class Argument(BaseModel):
    stance: Literal["skeptic", "advocate"]
    points: list[str]
    strongest_point: str


class Verdict(BaseModel):
    decision: Literal["approve", "block", "verify_manually"]
    confidence: float = Field(ge=0.0, le=1.0)
    key_evidence: list[str]  # references Evidence.finding strings
    reasoning: str  # 2-4 sentences
    recommended_action: str  # e.g. "Email previously known contact at <old address> to confirm"


class CaseStatus(str, Enum):
    QUEUED = "queued"
    INVESTIGATING = "investigating"
    DEBATING = "debating"
    ARBITRATING = "arbitrating"
    DONE = "done"
    ERROR = "error"


class VerificationCase(BaseModel):
    id: str
    claim: Claim
    status: CaseStatus = CaseStatus.QUEUED
    evidence: list[Evidence] = []
    arguments: list[Argument] = []
    verdict: Verdict | None = None
    human_decision: str | None = None  # approve / block, set via UI
    payment_status: str | None = None  # set once /pay executes via Open Payments
    payment_id: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


AgentName = Literal["system", "detector", "investigator", "skeptic", "advocate", "arbiter"]

AgentState = Literal[
    "idle", "spawned", "thinking", "tool_call", "tool_result",
    "streaming", "arguing", "deciding", "done", "error",
]


class Heartbeat(BaseModel):
    ts: datetime
    case_id: str
    agent: AgentName
    state: AgentState
    detail: str  # short human line: "Calling bolagsverket(orgnr=556677-8899)"
    payload: dict | None = None  # optional: evidence item, argument, verdict — UI renders inline
