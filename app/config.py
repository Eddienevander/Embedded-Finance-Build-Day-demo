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

# Startup default for the dashboard's "real integrations" toggle (see
# app/orchestrator.py: set_real_integrations) — swaps in adapters actually
# wired to a live API (Zwapgrid today) without needing MOCK_MODE=false.
ZWAPGRID_LIVE_PAYMENT_HISTORY: bool = _env_bool("ZWAPGRID_LIVE_PAYMENT_HISTORY", "false")

# A hung LLM call must never freeze the demo: hard per-request timeout.
LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2000"))

# Minimum spacing between heartbeats per agent so the UI pulses visibly.
HEARTBEAT_MIN_INTERVAL: float = 0.15
