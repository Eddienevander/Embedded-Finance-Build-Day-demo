"""Replay of recorded heartbeat streams — the demo's insurance policy.

A recorded run re-emits the exact heartbeats of a real pipeline execution at
close to its original pacing (long waits on the API are capped, see MAX_GAP_S),
and needs no network at all. If the venue wifi dies or the API is unreachable,
the demo still runs.

Recording format (recordings/<scenario>.jsonl):
  line 1  {"type": "meta", "scenario": ..., "model": ..., "invoices": [...]}
  line n  {"type": "hb", "offset_ms": <float>, "hb": {...heartbeat...}}
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.bus import bus
from app.db import Database
from app.models import Heartbeat, Invoice, VerificationCase

RECORDINGS_DIR = Path(__file__).resolve().parent.parent / "recordings"


def available_recordings() -> list[str]:
    if not RECORDINGS_DIR.is_dir():
        return []
    return sorted(p.stem for p in RECORDINGS_DIR.glob("*.jsonl"))


def has_recording(scenario: str) -> bool:
    return (RECORDINGS_DIR / f"{scenario}.jsonl").is_file()


def load_recording(scenario: str) -> tuple[dict, list[dict]]:
    path = RECORDINGS_DIR / f"{scenario}.jsonl"
    meta: dict = {}
    events: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("type") == "meta":
            meta = rec
        else:
            events.append(rec)
    return meta, events


# A live run spends whole seconds waiting on the API. Faithful replay would
# reproduce that silence on stage, so gaps are capped: pacing stays natural,
# dead air does not.
MAX_GAP_S = 2.5


async def replay_scenario(scenario: str, db: Database, speed: float = 1.0,
                          max_gap: float = MAX_GAP_S) -> int:
    """Re-emit a recorded run onto the live bus. Timestamps are rewritten to now
    so the dashboard clock reads correctly; case ids are stable, so replaying
    twice updates the same queue card instead of cluttering it."""
    meta, events = load_recording(scenario)

    # Restore the scenario's invoices so /cases/{id}/rerun can go live later.
    for raw in meta.get("invoices", []):
        db.insert_invoice(Invoice.model_validate(raw), status="under_review")

    await bus.emit("replay", "system", "thinking",
                   f"▶ Replaying recorded run of '{scenario}' "
                   f"({len(events)} heartbeats, no API calls)")

    loop = asyncio.get_running_loop()
    start = loop.time()
    wall = datetime.now(timezone.utc)
    recorded_prev = 0.0
    elapsed = 0.0

    for rec in events:
        recorded = float(rec["offset_ms"]) / 1000.0
        elapsed += min(recorded - recorded_prev, max_gap)
        recorded_prev = recorded
        offset = elapsed / max(speed, 0.01)
        delay = (start + offset) - loop.time()
        if delay > 0:
            await asyncio.sleep(delay)
        hb = Heartbeat.model_validate(rec["hb"])
        hb.ts = wall + timedelta(seconds=offset)
        bus.publish(hb)

        # Keep the case store in sync so the queue, verdict card and the
        # human decision endpoint all work on a replayed case.
        if hb.payload and hb.payload.get("case"):
            from app.orchestrator import CASES  # late import: avoids a cycle
            case = VerificationCase.model_validate(hb.payload["case"])
            CASES[case.id] = case
            db.save_case(case)

    return len(events)
