"""FastAPI app: invoice intake, scenario triggers, case queue, human decision,
and the /ws heartbeat stream that drives the dashboard."""

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
from app.orchestrator import CASES, get_registry, load_persisted_cases
from app.seed import SCENARIOS, make_scenario_invoices

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


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


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "mock_mode": config.MOCK_MODE, "model": config.MODEL}


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
