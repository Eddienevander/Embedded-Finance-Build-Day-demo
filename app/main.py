"""FastAPI app: invoice intake, scenario triggers, case queue, human decision,
and the /ws heartbeat stream that drives the dashboard."""

import asyncio
from contextlib import asynccontextmanager
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
from app.orchestrator import CASES, get_registry, load_persisted_cases, rerun_case
from app.replay import available_recordings, has_recording, replay_scenario
from app.seed import SCENARIOS, make_scenario_invoices

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

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


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "mock_mode": config.MOCK_MODE, "model": config.MODEL,
            "recordings": available_recordings()}


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
