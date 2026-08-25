"""Environment-driven configuration. Everything has a demo-safe default."""

import os

from dotenv import load_dotenv

load_dotenv()


def _env_bool(name: str, default: str) -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


MOCK_MODE: bool = _env_bool("MOCK_MODE", "true")

# The anthropic SDK also resolves credentials from an `ant auth login` profile,
# so an unset ANTHROPIC_API_KEY is not necessarily fatal.
ANTHROPIC_API_KEY: str | None = os.getenv("ANTHROPIC_API_KEY")

# Sampling params were removed on the 4.7+/5 generation, so `temperature` is only
# sent for models that still accept it (see agents/base.py: sampling_body).
MODEL: str = os.getenv("MODEL", "claude-opus-4-7")

DB_PATH: str = os.getenv("TRUST_LAYER_DB", "trust_layer.db")

# Zwapgrid API.1 (Accounting API) — invoice interchange, see app/tools/zwapgrid_real.py.
# The Consent is created once during onboarding (buyer connects Fortnox/Xero/etc via
# Zwapgrid's Client Portal flow) — this app only polls an already-ACTIVE consent.
ZWAPGRID_BASE_URL: str = os.getenv("ZWAPGRID_BASE_URL", "https://apione.zwapgrid.com/accounting/api/v1")
ZWAPGRID_API_KEY: str | None = os.getenv("ZWAPGRID_API_KEY")
ZWAPGRID_CONSENT_ID: str | None = os.getenv("ZWAPGRID_CONSENT_ID")

# Cap on how many invoices a single sync pulls (rate-limit-friendly — the
# sandbox has returned a real 429) and how often /demo/zwapgrid-sync will
# actually call out, in seconds (repeat clicks are refused with 429 instead
# of hammering Zwapgrid again).
ZWAPGRID_SYNC_LIMIT: int = int(os.getenv("ZWAPGRID_SYNC_LIMIT", "10"))
ZWAPGRID_SYNC_COOLDOWN_SECONDS: float = float(os.getenv("ZWAPGRID_SYNC_COOLDOWN_SECONDS", "30"))

# Startup default for the dashboard's "real integrations" toggle (see
# app/orchestrator.py: set_real_integrations) — swaps in adapters actually
# wired to a live API (Zwapgrid today) without needing MOCK_MODE=false.
ZWAPGRID_LIVE_PAYMENT_HISTORY: bool = _env_bool("ZWAPGRID_LIVE_PAYMENT_HISTORY", "false")

# Open Payments Europe (openpayments.io) — PSD2 Payment Initiation Service (PIS),
# see app/tools/openpayments_real.py. NOT the same "Open Payments" as the
# Interledger/GNAP standard (openpayments.dev) — different company, different
# protocol. Sandbox-only for now: the redirect/BankID flow is production-only.
OPENPAYMENTS_AUTH_BASE_URL: str = os.getenv(
    "OPENPAYMENTS_AUTH_BASE_URL", "https://auth.sandbox.openbankingplatform.com"
)
OPENPAYMENTS_API_BASE_URL: str = os.getenv(
    "OPENPAYMENTS_API_BASE_URL", "https://api.sandbox.openbankingplatform.com"
)
OPENPAYMENTS_CLIENT_ID: str | None = os.getenv("OPENPAYMENTS_CLIENT_ID")
OPENPAYMENTS_CLIENT_SECRET: str | None = os.getenv("OPENPAYMENTS_CLIENT_SECRET")
OPENPAYMENTS_REDIRECT_URI: str | None = os.getenv("OPENPAYMENTS_REDIRECT_URI")

# Sandbox has no bank/PSU picker — these are the docs' own worked examples
# (docs.openpayments.io/docs/quickstart_pis, /docs/credentials), not a guess:
# ESSESESS is SEB's BIC, and neither PSU-ID nor PSU-Corporate-ID trigger any of
# the documented negative-test scenarios (Corporate-ID must end in an even
# digit or sandbox returns an invalid-KYC status).
OPENPAYMENTS_BIC: str = os.getenv("OPENPAYMENTS_BIC", "ESSESESS")
OPENPAYMENTS_PSU_ID: str = os.getenv("OPENPAYMENTS_PSU_ID", "199002092386")
OPENPAYMENTS_PSU_CORPORATE_ID: str = os.getenv("OPENPAYMENTS_PSU_CORPORATE_ID", "5560160680")
# Our own (buyer/debtor) sandbox test account — same IBAN docs.openpayments.io's
# own examples use throughout the PIS guide.
OPENPAYMENTS_DEBTOR_IBAN: str = os.getenv("OPENPAYMENTS_DEBTOR_IBAN", "SE4550000000058398257466")

# A hung LLM call must never freeze the demo: hard per-request timeout.
LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2000"))

# Minimum spacing between heartbeats per agent so the UI pulses visibly.
HEARTBEAT_MIN_INTERVAL: float = 0.15
