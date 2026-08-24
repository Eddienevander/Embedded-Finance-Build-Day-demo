"""Zwapgrid adapter (real stub): invoice interchange as the intake source.

In production, invoices would arrive from Zwapgrid's e-invoice network instead
of the demo's POST /invoices endpoint.

TODO(venue): wire this in.
  - Auth: Zwapgrid API key placeholder in env (ZWAPGRID_API_KEY).
  - Subscribe to / poll incoming invoices for the buyer company.
  - Map each Zwapgrid invoice payload -> app.models.Invoice and feed it to
    app.ingest.process_invoice().
"""

from app.models import Invoice


async def fetch_incoming_invoices() -> list[Invoice]:
    raise NotImplementedError("TODO: wire Zwapgrid invoice interchange adapter")
