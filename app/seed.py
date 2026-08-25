"""Synthetic data generator + scripted attack invoices.

Fictional buyer: Bergström Bygg AB. Eight suppliers with 12 months of
invoice + payment history. The scenario constants below are also the fixture
keys for the mock tools in app/tools/ — keep them in sync.

Run: MOCK_MODE=true uv run python -m app.seed
"""

import random
import uuid
from datetime import date, timedelta

from app.db import Database, get_db
from app.models import Invoice, SupplierBaseline

BUYER_NAME = "Bergström Bygg AB"

# name, orgnr, bankgiro, contact_email, avg amount SEK, terms days, invoices/12mo
# (bankgiro numbers are synthetic but carry VALID mod-10 check digits, so the
#  bankgirot validation tool treats legitimate suppliers as legitimate)
SUPPLIERS: list[dict] = [
    {"name": "Nordisk Ställning AB", "orgnr": "556677-8899", "account": "BG 123-4566",
     "email": "ekonomi@nordiskstallning.se", "avg": 180_000, "terms": 30, "n": 30},
    {"name": "Svea Kontorsmaterial AB", "orgnr": "556234-1122", "account": "BG 234-5676",
     "email": "faktura@sveakontor.se", "avg": 14_500, "terms": 30, "n": 12},
    {"name": "Mälardalens El & Automation AB", "orgnr": "556891-3344", "account": "BG 345-6787",
     "email": "ekonomi@malardalenel.se", "avg": 92_000, "terms": 30, "n": 14},
    {"name": "Götaland Grus & Schakt AB", "orgnr": "556455-7788", "account": "BG 456-7897",
     "email": "faktura@gotalandgrus.se", "avg": 240_000, "terms": 45, "n": 11},
    {"name": "Björk & Söner Snickeri AB", "orgnr": "556712-9911", "account": "BG 567-8909",
     "email": "info@bjorksoner.se", "avg": 65_000, "terms": 30, "n": 13},
    {"name": "Lappland Logistik AB", "orgnr": "556388-2255", "account": "BG 678-9010",
     "email": "ekonomi@lapplandlogistik.se", "avg": 38_000, "terms": 20, "n": 15},
    {"name": "Kungsholmen Kontorsservice AB", "orgnr": "556990-4433", "account": "BG 789-0122",
     "email": "faktura@kungsholmenkontor.se", "avg": 8_200, "terms": 30, "n": 12},
    {"name": "Öresund Verktygsuthyrning AB", "orgnr": "556533-6677", "account": "BG 890-1233",
     "email": "ekonomi@oresundverktyg.se", "avg": 450_000, "terms": 60, "n": 10},
]

# --- scenario constants (fixture keys for the mock tools) --------------------

# account_swap: real supplier, fake account. The IBAN belongs to a private
# person and was opened 19 days ago (per the mock account registry).
ATTACK_SUPPLIER = SUPPLIERS[0]  # Nordisk Ställning AB, 30 prior payments
ATTACK_IBAN = "SE45 5000 0000 0583 9825 7466"
ATTACK_CONTACT = "ekonomi@nordisk-stallning.com"  # lookalike of the real domain

# ghost_supplier: fabricated company, orgnr unknown at Bolagsverket.
GHOST_ORGNR = "559999-1234"
GHOST_NAME = "Skandinavisk Byggpartner AB"
GHOST_ACCOUNT = "BG 902-6642"
GHOST_CONTACT = "faktura@skandinaviskbyggpartner.se"

# legit_bank_change: real supplier really changed banks; the registry confirms
# ownership and web_intel finds a dated announcement.
LEGIT_SUPPLIER = SUPPLIERS[1]  # Svea Kontorsmaterial AB
LEGIT_NEW_ACCOUNT = "BG 5678-9019"
LEGIT_ANNOUNCEMENT_DATE = "2026-08-01"

CLEAN_SUPPLIER = SUPPLIERS[2]  # Mälardalens El & Automation AB
DOUBLE_SUPPLIER = SUPPLIERS[3]  # Götaland Grus & Schakt AB

SCENARIOS = ["clean", "account_swap", "ghost_supplier", "legit_bank_change", "double_finance"]


def _new_id(prefix: str = "INV") -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8].upper()}"


def seed(db: Database) -> None:
    """Rebuild suppliers + 12 months of invoice/payment history + baselines."""
    rng = random.Random(42)
    db.reset()
    today = date.today()

    for s in SUPPLIERS:
        first_seen = today - timedelta(days=365)
        n = s["n"]
        terms_seen: list[int] = []
        amounts: list[float] = []
        for i in range(n):
            issued = today - timedelta(days=int(365 * (n - i) / n) + rng.randint(0, 6))
            terms = s["terms"] + rng.choice([0, 0, 0, 0, -5, 5])
            due = issued + timedelta(days=terms)
            amount = round(s["avg"] * rng.uniform(0.7, 1.3), 2)
            inv = Invoice(
                id=_new_id(),
                supplier_orgnr=s["orgnr"], supplier_name=s["name"],
                amount_sek=amount, bank_account=s["account"],
                reference=f"F{issued.year}-{rng.randint(1000, 9999)}",
                due_date=due, issued_date=issued, contact_email=s["email"],
            )
            db.insert_invoice(inv, status="paid")
            # occasional lateness
            paid_at = due + timedelta(days=rng.choice([0, 0, 0, 1, 2, -1, 8]))
            db.add_payment(s["orgnr"], inv.id, amount, s["account"], paid_at)
            terms_seen.append(terms)
            amounts.append(amount)

        terms_seen.sort()
        db.upsert_supplier(SupplierBaseline(
            orgnr=s["orgnr"], name=s["name"], known_accounts=[s["account"]],
            payment_count=n, avg_amount_sek=round(sum(amounts) / len(amounts), 2),
            typical_terms_days=terms_seen[len(terms_seen) // 2],
            first_seen=first_seen, contact_email=s["email"],
        ))


def make_scenario_invoices(name: str) -> list[Invoice]:
    """Craft the invoice(s) for a scripted scenario. Fresh ids per call so the
    demo buttons can be clicked repeatedly."""
    today = date.today()

    if name == "clean":
        s = CLEAN_SUPPLIER
        return [Invoice(
            id=_new_id(), supplier_orgnr=s["orgnr"], supplier_name=s["name"],
            amount_sek=94_300.0, bank_account=s["account"],
            reference=f"F{today.year}-{uuid.uuid4().hex[:4].upper()}",
            issued_date=today, due_date=today + timedelta(days=s["terms"]),
            contact_email=s["email"],
        )]

    if name == "account_swap":
        s = ATTACK_SUPPLIER
        return [Invoice(
            id=_new_id(), supplier_orgnr=s["orgnr"], supplier_name=s["name"],
            amount_sek=187_400.0, bank_account=ATTACK_IBAN,
            reference=f"F{today.year}-{uuid.uuid4().hex[:4].upper()}",
            issued_date=today, due_date=today + timedelta(days=s["terms"]),
            contact_email=ATTACK_CONTACT,
            raw_note="Vi har bytt bank – vänligen använd vårt nya konto för alla framtida betalningar.",
        )]

    if name == "ghost_supplier":
        return [Invoice(
            id=_new_id(), supplier_orgnr=GHOST_ORGNR, supplier_name=GHOST_NAME,
            amount_sek=96_500.0, bank_account=GHOST_ACCOUNT,
            reference=f"F{today.year}-{uuid.uuid4().hex[:4].upper()}",
            issued_date=today, due_date=today + timedelta(days=10),
            contact_email=GHOST_CONTACT,
            raw_note="Slutfaktura enligt överenskommelse med er projektledare.",
        )]

    if name == "legit_bank_change":
        s = LEGIT_SUPPLIER
        return [Invoice(
            id=_new_id(), supplier_orgnr=s["orgnr"], supplier_name=s["name"],
            amount_sek=15_100.0, bank_account=LEGIT_NEW_ACCOUNT,
            reference=f"F{today.year}-{uuid.uuid4().hex[:4].upper()}",
            issued_date=today, due_date=today + timedelta(days=s["terms"]),
            contact_email=s["email"],
            raw_note="Vi har bytt bankförbindelse till SEB, se nyheten på vår hemsida.",
        )]

    if name == "double_finance":
        s = DOUBLE_SUPPLIER
        shared_ref = f"DF{today.year}-{uuid.uuid4().hex[:4].upper()}"
        common = dict(
            supplier_orgnr=s["orgnr"], supplier_name=s["name"],
            amount_sek=243_000.0, bank_account=s["account"], reference=shared_ref,
            issued_date=today, due_date=today + timedelta(days=s["terms"]),
            contact_email=s["email"],
        )
        return [
            Invoice(id=_new_id(), **common),
            Invoice(id=_new_id(), **common,
                    raw_note="Påminnelse: översänder fakturan på nytt för omgående betalning."),
        ]

    raise ValueError(f"unknown scenario: {name!r} (choose from {SCENARIOS})")


def main() -> None:
    db = get_db()
    seed(db)
    print(f"Seeded {len(SUPPLIERS)} suppliers with 12 months of history for {BUYER_NAME}.")
    print(f"Scenarios ready: {', '.join(SCENARIOS)}")


if __name__ == "__main__":
    main()
