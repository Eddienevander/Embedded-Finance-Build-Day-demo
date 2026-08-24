"""In-process async pub/sub. Agents emit Heartbeats; every open WebSocket
connection owns a queue that the /ws endpoint drains to the browser."""

import asyncio
import logging
from datetime import datetime, timezone

from app.config import HEARTBEAT_MIN_INTERVAL
from app.models import AgentName, AgentState, Heartbeat

log = logging.getLogger("trust_layer.bus")

QUEUE_MAXSIZE = 2000


class EventBus:
    def __init__(self) -> None:
        self._subscribers: dict[int, asyncio.Queue[Heartbeat]] = {}
        self._next_id = 0
        # (case_id, agent) -> earliest monotonic time the next event may be emitted
        self._next_allowed: dict[tuple[str, str], float] = {}

    def subscribe(self) -> tuple[int, "asyncio.Queue[Heartbeat]"]:
        self._next_id += 1
        q: asyncio.Queue[Heartbeat] = asyncio.Queue(maxsize=QUEUE_MAXSIZE)
        self._subscribers[self._next_id] = q
        return self._next_id, q

    def unsubscribe(self, sub_id: int) -> None:
        self._subscribers.pop(sub_id, None)

    def publish(self, hb: Heartbeat) -> None:
        for q in self._subscribers.values():
            try:
                q.put_nowait(hb)
            except asyncio.QueueFull:
                # A stalled browser must never block the pipeline: drop for that subscriber.
                log.warning("subscriber queue full, dropping heartbeat")

    async def emit(
        self,
        case_id: str,
        agent: AgentName,
        state: AgentState,
        detail: str,
        payload: dict | None = None,
    ) -> None:
        """Throttled emit: >=150ms between events per (case, agent) so the UI
        pulses visibly instead of blurring."""
        loop = asyncio.get_running_loop()
        key = (case_id, agent)
        now = loop.time()
        allowed = self._next_allowed.get(key, 0.0)
        if now < allowed:
            await asyncio.sleep(allowed - now)
            now = loop.time()
        self._next_allowed[key] = now + HEARTBEAT_MIN_INTERVAL

        self.publish(
            Heartbeat(
                ts=datetime.now(timezone.utc),
                case_id=case_id,
                agent=agent,
                state=state,
                detail=detail[:300],
                payload=payload,
            )
        )


bus = EventBus()
