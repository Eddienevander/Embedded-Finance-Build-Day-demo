# Trust Layer

Adversarial multi-agent verification for B2B payment fraud, built at Embedded Finance
Build Day in Stockholm. Incoming invoices (live over Zwapgrid's API) are checked against
each supplier's history; any change, like a new bank account or a duplicate receivable,
is argued out by AI agents (Investigator, Skeptic vs Advocate, Arbiter) before a human
decides. Approved invoices are paid for real through Open Payments.

**Presentation:** _link coming soon_ <!-- TODO: replace with the presentation URL -->

## Requirements

- [uv](https://docs.astral.sh/uv/)
- `ANTHROPIC_API_KEY` set in the environment (agents run on `claude-opus-4-7`)
- Optional, for live Zwapgrid and Open Payments integrations: a `.env` with the keys
  listed in `.env.example`

## Run

```bash
export ANTHROPIC_API_KEY=sk-ant-...
./run.sh        # http://127.0.0.1:8000/
```

Tests: `make test`
