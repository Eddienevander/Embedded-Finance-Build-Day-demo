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
_registries: dict[bool, ToolRegistry] = {}
# Runtime toggle (the dashboard's "real integrations" button), separate from
# MOCK_MODE — lets a case use genuinely-wired live adapters (Zwapgrid today)
# without a restart. Seeded from the env var so an existing .env still works
# as the startup default.
_real_integrations: bool = config.ZWAPGRID_LIVE_PAYMENT_HISTORY


def get_registry(db: Database) -> ToolRegistry:
    """Registries are cached per mode (not just once) so each mode's tools —
    e.g. ZwapgridPaymentHistoryTool's invoice cache — keep their state across
    calls instead of resetting every time the toggle flips."""
    if _real_integrations not in _registries:
        _registries[_real_integrations] = build_registry(
            db, mock=config.MOCK_MODE, real_integrations=_real_integrations
        )
    return _registries[_real_integrations]


def real_integrations_enabled() -> bool:
    return _real_integrations


def set_real_integrations(enabled: bool) -> bool:
    global _real_integrations
    _real_integrations = enabled
    return _real_integrations


def cancel_running_cases() -> int:
    """Cancel every in-flight case task. Without this, a demo reset leaves
    zombie pipelines running: they keep calling the model, re-save their case
    into the fresh database, and their heartbeats resurrect the case in open
    browsers while the server no longer knows it (so Decide/Block 404s)."""
    cancelled = 0
    for task in list(_TASKS):
        if not task.done():
            task.cancel()
            cancelled += 1
    return cancelled


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


def rerun_case(case: VerificationCase, invoice: Invoice,
               baseline: SupplierBaseline | None, db: Database) -> None:
    """Re-run an existing case in place. Reusing the case id keeps the queue
    clean — a retry after a failure replaces the card instead of adding one."""
    case.status = CaseStatus.QUEUED
    case.evidence = []
    case.arguments = []
    case.verdict = None
    case.human_decision = None
    case.started_at = None
    case.finished_at = None
    db.clear_verdict(case.id)
    start_case(case, invoice, baseline, db)


async def _transition(case: VerificationCase, status: CaseStatus, detail: str,
                      db: Database, include_case: bool = False) -> None:
    case.status = status
    db.save_case(case)
    # Serialize AFTER the status is set, or the payload ships the previous
    # status and the dashboard's case state lags a step behind.
    payload = {"case": case.model_dump(mode="json")} if include_case else None
    await bus.emit(case.id, "system", "done" if status == CaseStatus.DONE else "thinking",
                   detail, payload)


async def run_case(case: VerificationCase, invoice: Invoice,
                   baseline: SupplierBaseline | None, db: Database) -> None:
    registry = get_registry(db)
    case.started_at = datetime.now(timezone.utc)
    try:
        await _transition(case, CaseStatus.INVESTIGATING,
                          f"{case.id}: {case.claim.summary} → investigating", db,
                          include_case=True)

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
        verdict_label = ("VERIFY_MANUALLY, a human decides"
                         if case.verdict.decision == "verify_manually"
                         else f"{case.verdict.decision.upper()} "
                              f"({case.verdict.confidence:.0%})")
        await _transition(
            case, CaseStatus.DONE,
            f"{case.id}: verdict {verdict_label}, awaiting human decision",
            db, include_case=True,
        )
    except Exception as e:
        case.status = CaseStatus.ERROR
        case.finished_at = datetime.now(timezone.utc)
        db.save_case(case)
        await bus.emit(case.id, "system", "error",
                       f"{case.id} failed: {type(e).__name__}: {truncate(str(e), 160)}",
                       payload={"case": case.model_dump(mode="json")})
