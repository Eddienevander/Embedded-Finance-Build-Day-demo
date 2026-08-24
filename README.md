# Trust Layer

Adversarial multi-agent verification for B2B payment fraud. Incoming invoices are
diffed against the supplier baseline; every "change-of-state claim" (new bank
account, new supplier, changed terms, duplicate receivable) is run through an
adversarial pipeline — **Investigator → Skeptic vs Advocate → Arbiter** — before a
human approves payment. The dashboard shows every agent's heartbeat live.

## Quickstart (clean machine: only `uv` + an API key)

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./run.sh                       # uv sync + seed + serve → http://127.0.0.1:8000/
```

Or `make demo`. Runs fully offline except calls to `api.anthropic.com`
(`MOCK_MODE=true` is the default; all data-source tools are seeded mocks).
Tests: `make test`.

## Demo script (in this order)

1. **Clean invoice** — known supplier, matches baseline. Auto-approved in <1s. Nothing to verify.
2. **⚠ Account swap** (headline) — real supplier, 30 prior payments, but a new IBAN and
   "Vi har bytt bank". Watch: registry says the account belongs to a *private person,
   opened 19 days ago*; no announcement online. Skeptic wins → **BLOCK**, and the
   recommended action is to confirm via the *old* contact channel — never the details
   on the suspicious invoice.
3. **Ghost supplier** — fabricated orgnr, not found at Bolagsverket, zero history → **BLOCK**.
4. **Legit bank change** — the nuance case: the registry confirms the new account belongs
   to the supplier and there is a dated announcement on their site → approve / low-friction
   manual verify. The system is not a paranoid rubber-stamp.
5. **Double financing** — the same receivable arrives twice; the first sails through,
   the second is caught.

## The pitch

`app/tools/account_registry.py` — the tool that cracks the headline case — is a
**fictional API**. No shared "who owns this bankgiro/IBAN?" registry exists today.
That is the missing piece of infrastructure this demo argues for (cf. IMY's
bank-data-sharing sandbox). Everything else has a real counterpart: Bolagsverket
(company status), Open Payments (payment history), Zwapgrid (invoice interchange) —
real adapters are stubbed with TODOs in `app/tools/*_real.py` and behind the same
`EvidenceTool` interface, ready to wire in at the venue.
