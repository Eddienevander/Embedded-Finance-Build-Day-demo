# Trust Layer

Adversarial multi-agent verification for B2B payment fraud. Incoming invoices are
diffed against the supplier baseline; every "change-of-state claim" (new bank
account, new supplier, changed terms, duplicate receivable) is run through an
adversarial pipeline (**Investigator → Skeptic vs Advocate → Arbiter**) before a
human approves payment. The dashboard shows every agent's heartbeat live.

## Quickstart (clean machine: only `uv` + an API key)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./run.sh                       # uv sync + seed + pre-flight + serve → http://127.0.0.1:8000/
```

Or `make demo`. Runs offline except calls to `api.anthropic.com` (`MOCK_MODE=true`
is the default; all data-source tools are seeded mocks). Agents run on
`claude-opus-4-7`; override with `MODEL=...`. Tests: `make test`.

`run.sh` makes one tiny model call before serving, so a missing or expired
credential fails at boot with instructions instead of failing on stage.

## Demo script (in this order)

1. **Clean invoice**: known supplier, matches baseline. Auto-approved in <1s, nothing to verify.
2. **⚠ Account swap** (headline): real supplier, 30 prior payments, but a new IBAN and
   "Vi har bytt bank". Watch the registry report a *private person's account, opened 19
   days ago*, and no announcement online. Skeptic wins → **BLOCK**, and the recommended
   action is to confirm via the *old* contact channel, never the details on the invoice.
3. **Ghost supplier**: fabricated orgnr, not found at Bolagsverket, zero history → **BLOCK**.
4. **Legit bank change**: the nuance case. The registry confirms the new account belongs to
   the supplier and there is a dated announcement on their site, so this one is not blocked.
   The system is not a paranoid rubber-stamp.
5. **Double financing**: the same receivable arrives twice; the first sails through, the
   second is caught.

## Demo insurance

**Replay mode.** Every scenario can be replayed from a recorded run with no network at
all: flip the dashboard's Scenarios panel from "Live agents" to "Replay recorded".
Replays re-emit the real heartbeats with their original pacing (gaps capped at 2.5s so
there is no dead air on stage). If the venue wifi dies, the demo still runs.

```bash
make record                              # re-record every scenario (costs API calls)
uv run python -m app.record account_swap # re-record one
```

Recordings live in `recordings/*.jsonl` and are committed, so a fresh clone can demo
immediately. A replay announces itself in the event log, so nobody mistakes it for a
live run.

**Re-run.** Any finished or failed case has a "re-run this case" button, which re-runs it
in place under the same case id. A red case on stage is one click from a retry, and the
queue does not fill up with duplicates.

## The pitch

`app/tools/account_registry.py` (the tool that cracks the headline case) is a
**fictional API**. No shared "who owns this bankgiro/IBAN?" registry exists today. That
is the missing infrastructure this demo argues for (cf. IMY's bank-data-sharing sandbox).
Everything else has a real counterpart: Zwapgrid invoice interchange is implemented
against the real Accounting API in `app/tools/zwapgrid_real.py` (needs credentials),
and Bolagsverket and Open Payments have stubbed adapters behind the same `EvidenceTool`
interface, ready to wire in at the venue.

Note that even the real Zwapgrid feed carries no authoritative bank account field, which
is the same gap from the other direction: an account number off the wire is an unverified
claim, so it goes through this pipeline rather than around it.
