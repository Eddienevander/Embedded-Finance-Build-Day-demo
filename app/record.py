"""Record a live pipeline run to recordings/<scenario>.jsonl.

    uv run python -m app.record                 # record every scenario
    uv run python -m app.record account_swap    # record one

Recording runs against its own database file and reseeds before each scenario,
so a recording is always made from the same clean baseline.
"""

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from app import config
from app.bus import bus
from app.db import Database
from app.ingest import process_invoice
from app.replay import RECORDINGS_DIR
from app.seed import SCENARIOS, make_scenario_invoices, seed

RECORD_DB = "trust_layer_record.db"
CASE_TIMEOUT_S = 300.0
DRAIN_S = 1.0


async def _drain(queue, events, start, seconds: float) -> None:
    loop = asyncio.get_running_loop()
    end = loop.time() + seconds
    while True:
        remaining = end - loop.time()
        if remaining <= 0:
            return
        try:
            hb = await asyncio.wait_for(queue.get(), timeout=remaining)
        except asyncio.TimeoutError:
            return
        events.append({
            "type": "hb",
            "offset_ms": round((loop.time() - start) * 1000, 1),
            "hb": json.loads(hb.model_dump_json()),
        })


async def record_scenario(scenario: str, db: Database) -> Path:
    seed(db)
    sub_id, queue = bus.subscribe()
    loop = asyncio.get_running_loop()
    events: list[dict] = []
    invoices = make_scenario_invoices(scenario)
    start = loop.time()
    try:
        pending: set[str] = set()
        for invoice in invoices:
            cases = await process_invoice(invoice, db)
            pending.update(c.id for c in cases)

        finished: set[str] = set()
        deadline = start + CASE_TIMEOUT_S
        while pending - finished and loop.time() < deadline:
            try:
                hb = await asyncio.wait_for(queue.get(), timeout=deadline - loop.time())
            except asyncio.TimeoutError:
                break
            events.append({
                "type": "hb",
                "offset_ms": round((loop.time() - start) * 1000, 1),
                "hb": json.loads(hb.model_dump_json()),
            })
            case = (hb.payload or {}).get("case")
            if case and case.get("status") in ("done", "error"):
                finished.add(case["id"])

        await _drain(queue, events, start, DRAIN_S)
    finally:
        bus.unsubscribe(sub_id)

    meta = {
        "type": "meta",
        "scenario": scenario,
        "model": config.MODEL,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "invoices": [json.loads(i.model_dump_json()) for i in invoices],
    }
    RECORDINGS_DIR.mkdir(exist_ok=True)
    path = RECORDINGS_DIR / f"{scenario}.jsonl"
    with path.open("w") as fh:
        fh.write(json.dumps(meta, ensure_ascii=False) + "\n")
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


async def main_async(scenarios: list[str]) -> None:
    db = Database(RECORD_DB)
    for scenario in scenarios:
        print(f"recording {scenario} …", flush=True)
        path = await record_scenario(scenario, db)
        lines = len(path.read_text().splitlines()) - 1
        verdicts = [
            json.loads(line)["hb"]["detail"]
            for line in path.read_text().splitlines()[1:]
            if '"agent":"arbiter"' in line and '"state":"done"' in line
        ]
        print(f"  → {path} ({lines} heartbeats) {verdicts[-1] if verdicts else ''}")


def main() -> None:
    scenarios = sys.argv[1:] or SCENARIOS
    unknown = [s for s in scenarios if s not in SCENARIOS]
    if unknown:
        raise SystemExit(f"unknown scenario(s): {unknown}; choose from {SCENARIOS}")
    asyncio.run(main_async(scenarios))


if __name__ == "__main__":
    main()
