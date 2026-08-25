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

`app/tools/account_registry.py` (the tool that cracks the headline case) answers "who
owns this account?". That is not science fiction: **Norway already runs it as
infrastructure**. KAR (Konto- og adresseringsregister, operated by the banks through
Bits) lets a Norwegian bank verify that an account number actually belongs to the person
or company being paid, before the payment goes out. Sweden has no equivalent: here the
tool is a mock, and IMY's regulatory sandbox with SEB, Nordea, Swedbank and Handelsbanken
concluded that this kind of data sharing between banks needs legislative change
(IMY-2024-14275, May 2025). The demo shows what the day after that legislation looks like.

Two slices of it are real today:

- `app/tools/bankgirot.py` validates bankgiro numbers offline (mod-10 check digit: a
  number that fails was never issued by Bankgirot) and models the owner lookup on
  Bankgirot's public number search, which exposes account-holder names through a website
  but no API. Parsing beats waiting for an API that does not exist.
- Zwapgrid invoice interchange is implemented against the real Accounting API in
  `app/tools/zwapgrid_real.py`, and can serve live payment history via the dashboard's
  real-integrations toggle.

Bolagsverket and Open Payments have stubbed adapters behind the same `EvidenceTool`
interface, ready to wire in at the venue.

Note that even the real Zwapgrid feed carries no authoritative bank account field, which
is the same gap from the other direction: an account number off the wire is an unverified
claim, so it goes through this pipeline rather than around it.
