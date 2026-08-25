"""Push demo supplier invoices INTO the Zwapgrid sandbox, so the on-stage
"Sync from Zwapgrid" pulls a realistic batch through the real rail.

    uv run python -m app.zwapgrid_seed --dry-run   # show payloads, send nothing
    uv run python -m app.zwapgrid_seed --probe     # send ONE invoice, print response
    uv run python -m app.zwapgrid_seed             # push the full demo batch

The batch targets the one supplier that exists in the connected Fortnox
sandbox (Cloudlane, supplier number 1; Fortnox rejects invoices for unknown
suppliers, and Zwapgrid's API cannot create suppliers). Cloudlane has a local
baseline + history (app/seed.py), so what comes back through the sync lands
on a real baseline: routine invoices auto-approve, and the last one carries a
new IBAN plus a "vi har bytt bank" note, so the fraud case arrives over the
actual e-invoice rail instead of a demo button.

Payloads mirror what app/tools/zwapgrid_real.py:_to_invoice reads back:
orgnr in accountingSupplierParty.customerAssignedAccountId.id (bare digits,
the reader re-inserts the dash), amount in legalMonetaryTotal.payableAmount,
and the bank account as free text in notes (the reader regex-scrapes it,
because the schema has no structured account field; that gap is the pitch).
"""

import argparse
import asyncio
import json
import uuid
from datetime import date, timedelta

import httpx

from app import config, seed


def _party(name: str, orgnr: str, email: str) -> dict:
    return {
        "customerAssignedAccountId": {"id": orgnr.replace("-", ""), "schemeId": "SE:ORGNR"},
        "party": {"partyName": {"name": name}, "contact": {"email": email}},
    }


def _invoice_payload(supplier: dict, amount: float, reference: str,
                     note: str, terms_days: int) -> dict:
    today = date.today()
    return {
        "accountingSupplierParty": _party(supplier["name"], supplier["orgnr"], supplier["email"]),
        "accountingCustomerParty": _party(seed.BUYER_NAME, "556000-1234", "ekonomi@bergstrombygg.se"),
        "reference": reference,
        "issueDate": today.isoformat(),
        "dueDate": (today + timedelta(days=terms_days)).isoformat(),
        "documentCurrencyCode": {"currencyId": "SEK"},
        "notes": [{"text": note}],
        "legalMonetaryTotal": {
            "lineExtensionAmount": {"amount": amount, "currencyId": "SEK"},
            "taxExclusiveAmount": {"amount": amount, "currencyId": "SEK"},
            "taxInclusiveAmount": {"amount": amount, "currencyId": "SEK"},
            "payableAmount": {"amount": amount, "currencyId": "SEK"},
        },
        "totalBalanceAmount": {"amount": amount, "currencyId": "SEK"},
        "invoiceLines": [{
            "id": "1",
            "account": {"id": "4010"},
            "note": note,
            "invoicedQuantity": {"quantity": 1, "unitCode": "EA"},
            "lineExtensionAmount": {"amount": amount, "currencyId": "SEK"},
        }],
    }


def demo_batch() -> list[dict]:
    """Fortnox only accepts invoices for suppliers that exist in its ledger,
    and the sandbox has exactly one (Cloudlane, supplier number 1), so the
    whole batch is Cloudlane's. Locally it has a full baseline + history
    (app/seed.py), so on sync: routine invoices auto-approve, and the last one
    carries a new IBAN plus a bank-switch note, so the account-swap case
    arrives over the real rail instead of a demo button."""
    run = uuid.uuid4().hex[:4].upper()
    cl = seed.ZWAPGRID_SUPPLIER
    bg = cl["account"].removeprefix("BG ")
    return [
        _invoice_payload(cl, 44_900.0, f"ZG{run}-101",
                         f"Molntjänster juli. Betalning till bankgiro {bg}", cl["terms"]),
        _invoice_payload(cl, 47_300.0, f"ZG{run}-102",
                         f"Molntjänster augusti. Betalning till bankgiro {bg}", cl["terms"]),
        _invoice_payload(cl, 51_200.0, f"ZG{run}-103",
                         f"Lagringsutökning enligt avtal. Bankgiro {bg}", cl["terms"]),
        # the attack, arriving over the real rail: known supplier, new IBAN
        _invoice_payload(cl, 46_800.0, f"ZG{run}-104",
                         f"Vi har bytt bank, vänligen betala till {seed.ATTACK_IBAN}",
                         cl["terms"]),
    ]


async def push(payloads: list[dict]) -> None:
    if not (config.ZWAPGRID_API_KEY and config.ZWAPGRID_CONSENT_ID):
        raise SystemExit("ZWAPGRID_API_KEY / ZWAPGRID_CONSENT_ID not set")
    url = f"{config.ZWAPGRID_BASE_URL}/consents/{config.ZWAPGRID_CONSENT_ID}/supplierinvoices"
    async with httpx.AsyncClient(timeout=30) as client:
        for p in payloads:
            resp = await client.post(url, json=p, headers={
                "x-api-key": config.ZWAPGRID_API_KEY,
                "x-correlation-id": str(uuid.uuid4()),
            })
            supplier = p["accountingSupplierParty"]["party"]["partyName"]["name"]
            print(f"  {supplier:<32} {p['reference']:<12} -> HTTP {resp.status_code}")
            if resp.status_code >= 300:
                print("    ", resp.text[:400])
            else:
                body = resp.json() if resp.text else {}
                print(f"     created id: {body.get('id') or body.get('data', {}).get('id') or '(no id in response)'}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--probe", action="store_true", help="send only the first invoice")
    args = ap.parse_args()
    batch = demo_batch()
    if args.dry_run:
        print(json.dumps(batch[0], indent=2, ensure_ascii=False))
        print(f"({len(batch)} payloads in the batch)")
        return
    asyncio.run(push(batch[:1] if args.probe else batch))


if __name__ == "__main__":
    main()
