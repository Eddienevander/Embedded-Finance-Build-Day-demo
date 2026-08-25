"""FastAPI app: invoice intake, scenario triggers, case queue, human decision,
and the /ws heartbeat stream that drives the dashboard."""

import asyncio
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config
from app.bus import bus
from app.db import get_db
from app.ingest import process_invoice
from app.models import Invoice
from app.orchestrator import (
    CASES,
    get_registry,
    load_persisted_cases,
    real_integrations_enabled,
    rerun_case,
    set_real_integrations,
)
from app.replay import available_recordings, has_recording, replay_scenario
from app.seed import SCENARIOS, make_scenario_invoices
from app.tools.openpayments_real import OpenPaymentsError, OpenPaymentsPISClient, SETTLED_STATUSES
from app.tools.zwapgrid_real import ZwapgridRealTool

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

"""
TODO:
- Add Email functionality, optional for users, they select it.
In case they select email func, we mail details of the invoice to the user (for now hardcode one of our Froda emails).

- Add Agents for various scenarios, like: double checking claims that are not obvious, such as bankgiro claims, etc. In case of high risk claims, we can use this agents to double check the claims.
- Add funcitonality for simple checks like does this this invoice exists in other ERP systems, etc.
- Add real functionality for Zwapgrid + OpenPayments

Presentation layer:
- Improve UI and design
- Nice presentation slides.
"""

_BACKGROUND: set[asyncio.Task] = set()
# Overlapping replays of the same recording would apply stale case states on top
# of newer ones (status marching backwards), so one at a time per scenario.
_ACTIVE_REPLAYS: set[str] = set()


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = get_db()
    load_persisted_cases(db)
    get_registry(db)  # build mock/real tool registry at startup
    yield


app = FastAPI(title="Trust Layer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.post("/invoices")
async def submit_invoice(invoice: Invoice) -> dict:
    cases = await process_invoice(invoice, get_db())
    return {
        "invoice_id": invoice.id,
        "auto_approved": not cases,
        "case_ids": [c.id for c in cases],
    }


@app.get("/invoices")
async def list_invoices(include_paid: bool = False) -> list[dict]:
    """The invoice inbox: everything except seeded history, newest first, with
    the ids of any verification cases spawned for each invoice."""
    cases_by_invoice: dict[str, list[str]] = {}
    for case in CASES.values():
        cases_by_invoice.setdefault(case.claim.invoice_id, []).append(case.id)
    rows = get_db().list_invoices(exclude_statuses=() if include_paid else ("paid",))
    return [{**row, "case_ids": cases_by_invoice.get(row["id"], [])} for row in rows]


@app.post("/demo/scenario/{name}")
async def run_scenario(name: str) -> dict:
    if name not in SCENARIOS:
        raise HTTPException(404, f"unknown scenario {name!r}; choose from {SCENARIOS}")
    db = get_db()
    results = []
    for invoice in make_scenario_invoices(name):
        cases = await process_invoice(invoice, db)
        results.append({
            "invoice_id": invoice.id,
            "auto_approved": not cases,
            "case_ids": [c.id for c in cases],
        })
    return {"scenario": name, "invoices": results}


@app.post("/demo/zwapgrid-sync")
async def sync_zwapgrid() -> dict:
    """Pull real invoices from the connected Zwapgrid consent and run any
    we haven't seen yet through the same intake path as /invoices. Lets the
    real Fortnox/Xero-connected sandbox data feed real cases, instead of only
    the scripted fictional scenarios."""
    db = get_db()
    await bus.emit("zwapgrid-sync", "system", "thinking",
                   "Zwapgrid: fetching supplier invoices from the connected consent…")
    try:
        invoices = await ZwapgridRealTool().fetch_incoming_invoices()
    except RuntimeError as e:
        await bus.emit("zwapgrid-sync", "system", "error", f"Zwapgrid sync failed: {e}")
        raise HTTPException(400, str(e))

    results = []
    for invoice in invoices:
        if db.invoice_exists(invoice.id):
            continue
        cases = await process_invoice(invoice, db)
        results.append({
            "invoice_id": invoice.id,
            "supplier_name": invoice.supplier_name,
            "auto_approved": not cases,
            "case_ids": [c.id for c in cases],
        })

    summary = {
        "fetched": len(invoices),
        "new": len(results),
        "skipped_already_seen": len(invoices) - len(results),
        "invoices": results,
    }
    await bus.emit("zwapgrid-sync", "system", "done",
                   f"Zwapgrid: {summary['fetched']} invoice(s) fetched, "
                   f"{summary['new']} new, {summary['skipped_already_seen']} already seen.")
    return summary


@app.get("/demo/recordings")
async def list_recordings() -> dict:
    """Which scenarios can be replayed without touching the network."""
    return {"recordings": available_recordings()}


@app.post("/demo/replay/{name}")
async def replay_recorded(name: str, speed: float = 1.0) -> dict:
    if not has_recording(name):
        raise HTTPException(404, f"no recording for {name!r}; "
                                 f"record one with `uv run python -m app.record {name}`")
    if name in _ACTIVE_REPLAYS:
        # A fat-fingered double click on stage should be a no-op, not an error.
        return {"scenario": name, "mode": "replay", "status": "already_running"}

    async def run() -> int:
        try:
            return await replay_scenario(name, get_db(), speed)
        finally:
            _ACTIVE_REPLAYS.discard(name)

    _ACTIVE_REPLAYS.add(name)
    task = asyncio.create_task(run())
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return {"scenario": name, "mode": "replay", "speed": speed, "status": "started"}


@app.get("/cases")
async def list_cases() -> list[dict]:
    return [c.model_dump(mode="json") for c in CASES.values()]


@app.get("/cases/{case_id}")
async def get_case(case_id: str) -> dict:
    case = CASES.get(case_id)
    if case is None:
        raise HTTPException(404, f"no such case: {case_id}")
    return case.model_dump(mode="json")


class DecisionBody(BaseModel):
    decision: Literal["approve", "block"]


@app.post("/cases/{case_id}/decision")
async def record_decision(case_id: str, body: DecisionBody) -> dict:
    case = CASES.get(case_id)
    if case is None:
        raise HTTPException(404, f"no such case: {case_id}")
    db = get_db()
    case.human_decision = body.decision
    db.save_case(case)
    db.set_invoice_status(case.claim.invoice_id,
                          "approved" if body.decision == "approve" else "blocked")
    await bus.emit(case.id, "system", "done",
                   f"Human decision on {case.id}: {body.decision.upper()}",
                   payload={"case": case.model_dump(mode="json")})
    return case.model_dump(mode="json")


@app.post("/cases/{case_id}/pay")
async def pay_case(case_id: str) -> dict:
    """Execute the case's payment via Open Payments Europe's PIS (sandbox
    decoupled flow — see app/tools/openpayments_real.py), only once a human
    has approved. On settlement, marks the invoice paid in our own DB —
    Zwapgrid has no write-back for this (verified: its Accounting API is
    GET/POST-only, no PATCH on supplier invoices)."""
    case = CASES.get(case_id)
    if case is None:
        raise HTTPException(404, f"no such case: {case_id}")
    if case.human_decision != "approve":
        raise HTTPException(409, "case has not been approved — approve it before paying")
    if case.payment_status in SETTLED_STATUSES:
        return case.model_dump(mode="json")  # already paid — no-op, not an error

    db = get_db()
    invoice = db.get_invoice(case.claim.invoice_id)
    if invoice is None:
        raise HTTPException(409, "the invoice for this case is not on file "
                                 "(replayed case?) — can't execute payment")

    try:
        result = await OpenPaymentsPISClient().pay_invoice(invoice)
    except RuntimeError as e:
        raise HTTPException(400, str(e))
    except OpenPaymentsError as e:
        case.payment_status = "failed"
        db.save_case(case)
        await bus.emit(case.id, "system", "error",
                       f"Payment failed for {case.id}: {e}",
                       payload={"case": case.model_dump(mode="json")})
        return case.model_dump(mode="json")

    case.payment_id = result["payment_id"]
    case.payment_status = result["status"]
    db.save_case(case)
    db.set_invoice_status(invoice.id, "paid")
    db.add_payment(invoice.supplier_orgnr, invoice.id, invoice.amount_sek, invoice.bank_account, date.today())
    await bus.emit(case.id, "system", "done",
                   f"Payment executed for {case.id} via Open Payments ({result['status']})",
                   payload={"case": case.model_dump(mode="json")})
    return case.model_dump(mode="json")


@app.get("/demo/payments")
async def list_executed_payments() -> dict:
    """Every payment we've executed, cross-checked live against Open
    Payments' own status endpoint — proof it actually exists there, not just
    a status string cached in our local case record."""
    db = get_db()
    client = OpenPaymentsPISClient()
    results = []
    for case in CASES.values():
        if not case.payment_id:
            continue
        invoice = db.get_invoice(case.claim.invoice_id)
        try:
            live_status = await client.get_payment_status(case.payment_id)
            error = None
        except Exception as e:
            live_status = None
            error = f"{type(e).__name__}: {e}"
        results.append({
            "case_id": case.id,
            "payment_id": case.payment_id,
            "recorded_status": case.payment_status,
            "live_status": live_status,
            "error": error,
            "invoice_id": case.claim.invoice_id,
            "supplier_name": invoice.supplier_name if invoice else None,
            "amount_sek": invoice.amount_sek if invoice else None,
            "bank_account": invoice.bank_account if invoice else None,
        })
    results.sort(key=lambda r: r["case_id"], reverse=True)
    return {"payments": results}


@app.post("/cases/{case_id}/rerun")
async def rerun(case_id: str) -> dict:
    """Re-run a finished or failed case through the pipeline, in place."""
    case = CASES.get(case_id)
    if case is None:
        raise HTTPException(404, f"no such case: {case_id}")
    db = get_db()
    invoice = db.get_invoice(case.claim.invoice_id)
    if invoice is None:
        raise HTTPException(409, "the invoice for this case is not on file "
                                 "(replayed case?) — fire the live scenario instead")
    rerun_case(case, invoice, db.get_baseline(case.claim.supplier_orgnr), db)
    return case.model_dump(mode="json")


class RealIntegrationsBody(BaseModel):
    enabled: bool


@app.get("/demo/real-integrations")
async def get_real_integrations() -> dict:
    return {"real_integrations": real_integrations_enabled()}


@app.post("/demo/real-integrations")
async def toggle_real_integrations(body: RealIntegrationsBody) -> dict:
    """Flip which evidence tools are genuinely wired to a live API (currently
    just Zwapgrid, for payment_history) vs seeded/mocked, for every case from
    here on — takes effect immediately, no restart."""
    return {"real_integrations": set_real_integrations(body.enabled)}


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "mock_mode": config.MOCK_MODE, "model": config.MODEL,
            "recordings": available_recordings(),
            "real_integrations": real_integrations_enabled()}


@app.websocket("/ws")
async def heartbeat_stream(ws: WebSocket) -> None:
    await ws.accept()
    sub_id, queue = bus.subscribe()
    try:
        while True:
            hb = await queue.get()
            await ws.send_text(hb.model_dump_json())
    except WebSocketDisconnect:
        pass
    finally:
        bus.unsubscribe(sub_id)
