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

# Sampling temperature is still supported on claude-sonnet-4-6; if you point
# MODEL at a 4.7+/5-family model, drop the temperature kwargs in agents/.
MODEL: str = os.getenv("MODEL", "claude-sonnet-4-6")

DB_PATH: str = os.getenv("TRUST_LAYER_DB", "trust_layer.db")

# Zwapgrid API.1 (Accounting API) — invoice interchange, see app/tools/zwapgrid_real.py.
# The Consent is created once during onboarding (buyer connects Fortnox/Xero/etc via
# Zwapgrid's Client Portal flow) — this app only polls an already-ACTIVE consent.
ZWAPGRID_BASE_URL: str = os.getenv("ZWAPGRID_BASE_URL", "https://apione.zwapgrid.com/accounting/api/v1")
ZWAPGRID_API_KEY: str | None = os.getenv("ZWAPGRID_API_KEY")
ZWAPGRID_CONSENT_ID: str | None = os.getenv("ZWAPGRID_CONSENT_ID")

# A hung LLM call must never freeze the demo: hard per-request timeout.
LLM_TIMEOUT_SECONDS: float = float(os.getenv("LLM_TIMEOUT_SECONDS", "90"))
LLM_MAX_TOKENS: int = int(os.getenv("LLM_MAX_TOKENS", "2000"))

# Minimum spacing between heartbeats per agent so the UI pulses visibly.
HEARTBEAT_MIN_INTERVAL: float = 0.15
