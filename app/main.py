"""FastAPI app: invoice intake, scenario triggers, case queue, human decision,
and the /ws heartbeat stream that drives the dashboard."""

import asyncio
import time
from contextlib import asynccontextmanager
from datetime import date
from pathlib import Path
from typing import Literal

import httpx
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
    cancel_running_cases,
    get_registry,
    load_persisted_cases,
    real_integrations_enabled,
    rerun_case,
    set_real_integrations,
)
from app.replay import available_recordings, has_recording, replay_scenario
from app.seed import SCENARIOS, make_scenario_invoices, seed
from app.tools.openpayments_real import (
    OpenPaymentsError,
    OpenPaymentsPISClient,
    SETTLED_STATUSES,
    parse_creditor_giro,
)
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
_last_zwapgrid_sync: float = 0.0  # monotonic seconds; guards the cooldown below


async def _stage_zwapgrid_invoices() -> dict:
    """Fetch (capped at ZWAPGRID_SYNC_LIMIT) invoices from Zwapgrid and stage
    any new ones as 'fetched' — no quarantine check, no agents. Shared by the
    manual sync endpoint and the one-shot startup sync."""
    db = get_db()
    invoices = await ZwapgridRealTool().fetch_incoming_invoices(limit=config.ZWAPGRID_SYNC_LIMIT)
    new_ids = []
    for invoice in invoices:
        if db.invoice_exists(invoice.id):
            continue
        db.insert_invoice(invoice, status="fetched")
        new_ids.append(invoice.id)
    return {
        "fetched": len(invoices),
        "new": len(new_ids),
        "skipped_already_seen": len(invoices) - len(new_ids),
        "invoice_ids": new_ids,
    }


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _last_zwapgrid_sync
    db = get_db()
    load_persisted_cases(db)
    get_registry(db)  # build mock/real tool registry at startup
    try:
        await _stage_zwapgrid_invoices()
        _last_zwapgrid_sync = time.monotonic()
    except Exception:
        pass  # not configured / sandbox unreachable at boot — manual sync still works
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
async def list_invoices(include_historical: bool = False) -> list[dict]:
    """The invoice list: everything except seeded backfill, newest first, with
    the ids of any verification cases spawned for each invoice. Invoices paid
    out through the app (status 'paid') are always included."""
    cases_by_invoice: dict[str, list[str]] = {}
    for case in CASES.values():
        cases_by_invoice.setdefault(case.claim.invoice_id, []).append(case.id)
    rows = get_db().list_invoices(exclude_statuses=() if include_historical else ("historical",))
    return [{**row, "case_ids": cases_by_invoice.get(row["id"], [])} for row in rows]


@app.post("/invoices/{invoice_id}/process")
async def process_fetched_invoice(invoice_id: str, force: bool = False) -> dict:
    """Run the intake pipeline (quarantine check, claim detection, and the
    agent pipeline if a claim is found) for exactly one invoice, on demand.
    Valid for an invoice in 'fetched' status; also valid for one already
    quarantined ('invalid') if force=true — the placeholder check is a
    default recommendation, not a hard block, and a human reviewing a
    specific invoice may want it run anyway. Once actually processed (not
    quarantined) this won't re-run it, to avoid a duplicate case."""
    db = get_db()
    status = db.get_invoice_status(invoice_id)
    if status is None:
        raise HTTPException(404, f"no such invoice: {invoice_id}")
    if status not in ("fetched", "invalid"):
        raise HTTPException(409, f"invoice {invoice_id} is already {status!r} — nothing to process")
    if status == "invalid" and not force:
        raise HTTPException(409, f"invoice {invoice_id} was quarantined as a placeholder — "
                                 "pass ?force=true to run it through the agents anyway")

    invoice = db.get_invoice(invoice_id)
    cases = await process_invoice(invoice, db, force=force)
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


@app.post("/demo/zwapgrid-sync")
async def sync_zwapgrid() -> dict:
    """Pull up to ZWAPGRID_SYNC_LIMIT invoices from the connected Zwapgrid
    consent and stage any we haven't seen yet as 'fetched' — visible in the
    inbox, but NOT run through the intake pipeline (quarantine check, claims,
    agents) here. Trigger verification per invoice on demand via
    POST /invoices/{id}/process. Rate-limited client-side too (the sandbox
    itself has returned a real 429) — repeat clicks within the cooldown are
    refused instead of hammering Zwapgrid again."""
    global _last_zwapgrid_sync
    elapsed = time.monotonic() - _last_zwapgrid_sync
    if elapsed < config.ZWAPGRID_SYNC_COOLDOWN_SECONDS:
        wait = config.ZWAPGRID_SYNC_COOLDOWN_SECONDS - elapsed
        raise HTTPException(429, f"synced recently — wait {wait:.0f}s before syncing again")
    _last_zwapgrid_sync = time.monotonic()

    await bus.emit("zwapgrid-sync", "system", "thinking",
                   "Zwapgrid: fetching supplier invoices from the connected consent…")
    try:
        summary = await _stage_zwapgrid_invoices()
    except RuntimeError as e:
        await bus.emit("zwapgrid-sync", "system", "error", f"Zwapgrid sync failed: {e}")
        raise HTTPException(400, str(e))
    except httpx.HTTPStatusError as e:
        detail = f"Zwapgrid returned {e.response.status_code}"
        if e.response.status_code == 429:
            detail += " (rate-limited — wait a bit before trying again)"
        await bus.emit("zwapgrid-sync", "system", "error", f"Zwapgrid sync failed: {detail}")
        raise HTTPException(502, detail)
    except httpx.HTTPError as e:
        await bus.emit("zwapgrid-sync", "system", "error", f"Zwapgrid sync failed: {e}")
        raise HTTPException(502, f"Zwapgrid request failed: {type(e).__name__}: {e}")

    await bus.emit("zwapgrid-sync", "system", "done",
                   f"Zwapgrid: {summary['fetched']} invoice(s) fetched, "
                   f"{summary['new']} new (staged for review — select one and run verification), "
                   f"{summary['skipped_already_seen']} already seen.")
    return summary


@app.post("/demo/reset")
async def reset_demo() -> dict:
    """Wipe invoices, cases and payments and reseed the supplier baselines,
    without restarting the server. Lets a rehearsal or stage run start from a
    blank slate and re-fire the same scenarios."""
    cancelled = cancel_running_cases()  # zombie pipelines must not outlive the reset
    seed(get_db())  # resets every table, then rebuilds 12 months of history
    CASES.clear()
    if cancelled:
        await bus.emit("reset", "system", "done",
                       f"Cancelled {cancelled} running verification case(s).")
    await bus.emit("reset", "system", "done",
                   "Demo data reset: invoices and cases cleared, supplier baselines reseeded.",
                   payload={"reset": True})
    return {"ok": True}


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


def _find_case(case_id: str):
    """CASES first, database second: a case saved by a task that outlived a
    reset (or a restart) is still decidable instead of a 404."""
    case = CASES.get(case_id)
    if case is None:
        case = get_db().get_case(case_id)
        if case is not None:
            CASES[case.id] = case
    return case


@app.get("/cases")
async def list_cases() -> list[dict]:
    return [c.model_dump(mode="json") for c in CASES.values()]


@app.get("/cases/{case_id}")
async def get_case(case_id: str) -> dict:
    case = _find_case(case_id)
    if case is None:
        raise HTTPException(404, f"no such case: {case_id}")
    return case.model_dump(mode="json")


class DecisionBody(BaseModel):
    decision: Literal["approve", "block"]


@app.post("/cases/{case_id}/decision")
async def record_decision(case_id: str, body: DecisionBody) -> dict:
    case = _find_case(case_id)
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


class PayBody(BaseModel):
    # Set when the invoice itself has no usable account (e.g. real Zwapgrid
    # data with an empty note — see parse_creditor_giro): a human confirming
    # the real bankgiro/plusgiro through another channel before paying,
    # not a fabricated fallback.
    bank_account: str | None = None


@app.post("/cases/{case_id}/pay")
async def pay_case(case_id: str, body: PayBody | None = None) -> dict:
    """Execute the case's payment via Open Payments Europe's PIS (sandbox
    decoupled flow — see app/tools/openpayments_real.py), only once a human
    has approved. On settlement, marks the invoice paid in our own DB and
    registers the payment back in Zwapgrid/Fortnox (best effort — only
    invoices booked at creation accept payment registrations; Fortnox
    rejects unbooked ones with "Fakturan är inte bokförd")."""
    case = _find_case(case_id)
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

    override = (body.bank_account or "").strip() if body else ""
    bank_account = override or invoice.bank_account
    try:
        parse_creditor_giro(bank_account)
    except ValueError:
        raise HTTPException(
            422,
            f"invoice {invoice.id} has no valid bankgiro/plusgiro account "
            f"(got {bank_account!r}) — can't execute a payment with no known destination. "
            "This is a real gap in the source data, not a bug: pass a confirmed "
            "`bank_account` in the request body to pay to it.",
        )
    if override and override != invoice.bank_account:
        db.set_invoice_bank_account(invoice.id, override)
        invoice = invoice.model_copy(update={"bank_account": override})

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

    # Best-effort: a scripted demo scenario's invoice never came from Zwapgrid
    # at all, so this legitimately fails for those — the Open Payments
    # settlement above is already the source of truth regardless.
    zwapgrid_note = ""
    try:
        await ZwapgridRealTool().create_invoice_payment(
            invoice.id, invoice.amount_sek, invoice.currency,
            reference=f"OpenPayments-{result['payment_id']}", paid_date=date.today().isoformat(),
        )
        zwapgrid_note = " and registered as a payment in Zwapgrid/Fortnox"
    except Exception as e:
        zwapgrid_note = f" (Zwapgrid payment registration skipped: {type(e).__name__}: {e})"

    await bus.emit(case.id, "system", "done",
                   f"Payment executed for {case.id} via Open Payments ({result['status']}){zwapgrid_note}",
                   payload={"case": case.model_dump(mode="json")})
    return case.model_dump(mode="json")


@app.get("/demo/payments")
async def list_executed_payments() -> dict:
    """Every payment we've executed, cross-checked live against two
    independent sources: Open Payments' own status endpoint (did the payment
    rail settle it), and Zwapgrid's payments-per-invoice list (did we
    register a matching payment against the invoice in the connected
    accounting system). Deliberately NOT Zwapgrid's invoice-level
    paymentStatus field — confirmed live that it doesn't reflect payments
    registered this way even when Fortnox itself considers the invoice
    fully settled, so it would just be misleading here."""
    db = get_db()
    op_client = OpenPaymentsPISClient()
    zwapgrid_client = ZwapgridRealTool()
    results = []
    for case in CASES.values():
        if not case.payment_id:
            continue
        invoice = db.get_invoice(case.claim.invoice_id)
        try:
            live_status = await op_client.get_payment_status(case.payment_id)
            error = None
        except Exception as e:
            live_status = None
            error = f"{type(e).__name__}: {e}"
        try:
            zwapgrid_payments = await zwapgrid_client.get_invoice_payments(case.claim.invoice_id)
        except Exception:
            zwapgrid_payments = None  # e.g. ZWAPGRID_CONSENT_ID not set — not this endpoint's concern

        zwapgrid_found = zwapgrid_payments is not None
        zwapgrid_total_paid = sum(p.get("amount") or 0 for p in zwapgrid_payments) if zwapgrid_payments else 0
        zwapgrid_paid = (
            zwapgrid_found and invoice is not None
            and zwapgrid_total_paid >= invoice.amount_sek - 0.01  # float tolerance
        )
        results.append({
            "case_id": case.id,
            "payment_id": case.payment_id,
            "recorded_status": case.payment_status,
            "live_status": live_status,
            "error": error,
            # zwapgrid_found=False: this invoice_id isn't a real Zwapgrid
            # invoice (e.g. a scripted demo scenario never synced from
            # Zwapgrid). zwapgrid_found=True: it is, and zwapgrid_paid says
            # whether its registered payments cover the invoice total.
            "zwapgrid_found": zwapgrid_found,
            "zwapgrid_paid": zwapgrid_paid,
            "zwapgrid_total_paid": zwapgrid_total_paid,
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
    case = _find_case(case_id)
    if case is None:
        raise HTTPException(404, f"no such case: {case_id}")
    db = get_db()
    invoice = db.get_invoice(case.claim.invoice_id)
    if invoice is None:
        raise HTTPException(409, "the invoice for this case is not on file "
                                 "(replayed case?): fire the live scenario instead")
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
