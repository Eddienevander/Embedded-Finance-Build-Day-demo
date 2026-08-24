"""Runs a VerificationCase end-to-end and emits heartbeats.

Each case runs as its own asyncio task so HTTP requests return immediately
while the pipeline streams to the dashboard.
"""

import asyncio
from datetime import datetime, timezone

from app import config
from app.agents.arbiter import Arbiter
from app.agents.base import truncate
from app.agents.debaters import Debater
from app.agents.investigator import Investigator
from app.bus import bus
from app.db import Database
from app.models import CaseStatus, Invoice, SupplierBaseline, VerificationCase
from app.tools.base import ToolRegistry, build_registry

CASES: dict[str, VerificationCase] = {}
_TASKS: set[asyncio.Task] = set()
_registry: ToolRegistry | None = None


def get_registry(db: Database) -> ToolRegistry:
    global _registry
    if _registry is None:
        _registry = build_registry(db, mock=config.MOCK_MODE)
    return _registry


def load_persisted_cases(db: Database) -> None:
    for case in db.list_cases():
        CASES.setdefault(case.id, case)


def start_case(case: VerificationCase, invoice: Invoice,
               baseline: SupplierBaseline | None, db: Database) -> None:
    CASES[case.id] = case
    db.save_case(case)
    task = asyncio.create_task(run_case(case, invoice, baseline, db))
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)


async def _transition(case: VerificationCase, status: CaseStatus,
                      detail: str, db: Database, payload: dict | None = None) -> None:
    case.status = status
    db.save_case(case)
    await bus.emit(case.id, "system", "done" if status == CaseStatus.DONE else "thinking",
                   detail, payload)


async def run_case(case: VerificationCase, invoice: Invoice,
                   baseline: SupplierBaseline | None, db: Database) -> None:
    registry = get_registry(db)
    case.started_at = datetime.now(timezone.utc)
    try:
        await _transition(case, CaseStatus.INVESTIGATING,
                          f"{case.id}: {case.claim.summary} → investigating", db,
                          payload={"case": case.model_dump(mode="json")})

        investigator = Investigator(case.id)
        await investigator.announce()
        case.evidence = await investigator.investigate(case.claim, invoice, baseline, registry)
        db.save_case(case)

        await _transition(case, CaseStatus.DEBATING,
                          f"{case.id}: evidence complete → skeptic vs advocate", db)
        skeptic, advocate = Debater("skeptic", case.id), Debater("advocate", case.id)
        await asyncio.gather(skeptic.announce(), advocate.announce())
        s_open, a_open = await asyncio.gather(
            skeptic.opening(case.claim, case.evidence),
            advocate.opening(case.claim, case.evidence),
        )
        s_reb, a_reb = await asyncio.gather(
            skeptic.rebuttal(case.claim, case.evidence, s_open, a_open),
            advocate.rebuttal(case.claim, case.evidence, a_open, s_open),
        )
        case.arguments = [s_open, a_open, s_reb, a_reb]
        db.save_case(case)

        await _transition(case, CaseStatus.ARBITRATING,
                          f"{case.id}: debate closed → arbiter deciding", db)
        arbiter = Arbiter(case.id)
        await arbiter.announce()
        case.verdict = await arbiter.decide(case.claim, case.evidence,
                                            case.arguments, baseline)

        case.finished_at = datetime.now(timezone.utc)
        await _transition(
            case, CaseStatus.DONE,
            f"{case.id}: verdict {case.verdict.decision.upper()} "
            f"({case.verdict.confidence:.0%}) — awaiting human decision",
            db, payload={"case": case.model_dump(mode="json")},
        )
    except Exception as e:
        case.status = CaseStatus.ERROR
        case.finished_at = datetime.now(timezone.utc)
        db.save_case(case)
        await bus.emit(case.id, "system", "error",
                       f"{case.id} failed: {type(e).__name__}: {truncate(str(e), 160)}",
                       payload={"case": case.model_dump(mode="json")})
