"""Pre-flight check: fail loudly at boot, not mid-demo.

run.sh calls this before starting uvicorn so a missing/expired credential is a
five-second failure with instructions, instead of a red case on stage twenty
seconds into the headline scenario.
"""

import asyncio
import sys
import time

from app import config
from app.replay import available_recordings


async def check() -> tuple[bool, str]:
    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(timeout=20.0, max_retries=0)
    started = time.monotonic()
    try:
        await client.messages.create(
            model=config.MODEL, max_tokens=8,
            messages=[{"role": "user", "content": "Reply with: ok"}],
        )
        return True, f"{config.MODEL} reachable in {time.monotonic() - started:.1f}s"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def main() -> None:
    ok, detail = asyncio.run(check())
    if ok:
        print(f"  ✓ pre-flight OK: {detail}")
        sys.exit(0)

    recordings = available_recordings()
    print(f"  ✗ PRE-FLIGHT FAILED: {detail}", file=sys.stderr)
    print("    Live agents will not run. Fix: export ANTHROPIC_API_KEY=sk-ant-…", file=sys.stderr)
    if recordings:
        print(f"    Recorded replays ARE available ({', '.join(recordings)}).", file=sys.stderr)
        print("    To demo from recordings anyway: SKIP_PREFLIGHT=true ./run.sh", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
