"""SQLite persistence: suppliers, invoices, payments, cases, verdicts.

Plain sqlite3 behind a lock — calls are sub-millisecond, so blocking the event
loop briefly is fine for a demo.
"""

import json
import sqlite3
import threading
from datetime import date

from app import config
from app.models import Invoice, SupplierBaseline, VerificationCase

SCHEMA = """
CREATE TABLE IF NOT EXISTS suppliers (
    orgnr TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    known_accounts TEXT NOT NULL,      -- JSON list, most-used first
    payment_count INTEGER NOT NULL,
    avg_amount_sek REAL NOT NULL,
    typical_terms_days INTEGER NOT NULL,
    first_seen TEXT NOT NULL,
    contact_email TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY,
    supplier_orgnr TEXT NOT NULL,
    supplier_name TEXT NOT NULL,
    amount_sek REAL NOT NULL,
    currency TEXT NOT NULL,
    bank_account TEXT NOT NULL,
    reference TEXT NOT NULL,
    due_date TEXT NOT NULL,
    issued_date TEXT NOT NULL,
    contact_email TEXT NOT NULL,
    raw_note TEXT,
    status TEXT NOT NULL DEFAULT 'received'
);
CREATE TABLE IF NOT EXISTS payments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    supplier_orgnr TEXT NOT NULL,
    invoice_id TEXT,
    amount_sek REAL NOT NULL,
    bank_account TEXT NOT NULL,
    paid_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS cases (
    id TEXT PRIMARY KEY,
    invoice_id TEXT NOT NULL,
    status TEXT NOT NULL,
    case_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS verdicts (
    case_id TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    confidence REAL NOT NULL,
    reasoning TEXT NOT NULL,
    recommended_action TEXT NOT NULL,
    human_decision TEXT
);
"""


class Database:
    def __init__(self, path: str = config.DB_PATH) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def _exec(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur

    def reset(self) -> None:
        with self._lock:
            for table in ("suppliers", "invoices", "payments", "cases", "verdicts"):
                self._conn.execute(f"DELETE FROM {table}")
            self._conn.commit()

    # -- suppliers -----------------------------------------------------------

    def upsert_supplier(self, b: SupplierBaseline) -> None:
        self._exec(
            "INSERT OR REPLACE INTO suppliers VALUES (?,?,?,?,?,?,?,?)",
            (b.orgnr, b.name, json.dumps(b.known_accounts), b.payment_count,
             b.avg_amount_sek, b.typical_terms_days, b.first_seen.isoformat(), b.contact_email),
        )

    def get_baseline(self, orgnr: str) -> SupplierBaseline | None:
        row = self._exec("SELECT * FROM suppliers WHERE orgnr=?", (orgnr,)).fetchone()
        if row is None:
            return None
        return SupplierBaseline(
            orgnr=row["orgnr"], name=row["name"],
            known_accounts=json.loads(row["known_accounts"]),
            payment_count=row["payment_count"], avg_amount_sek=row["avg_amount_sek"],
            typical_terms_days=row["typical_terms_days"],
            first_seen=date.fromisoformat(row["first_seen"]),
            contact_email=row["contact_email"],
        )

    # -- invoices ------------------------------------------------------------

    def insert_invoice(self, inv: Invoice, status: str = "received") -> None:
        self._exec(
            "INSERT OR REPLACE INTO invoices VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (inv.id, inv.supplier_orgnr, inv.supplier_name, inv.amount_sek, inv.currency,
             inv.bank_account, inv.reference, inv.due_date.isoformat(),
             inv.issued_date.isoformat(), inv.contact_email, inv.raw_note, status),
        )

    def set_invoice_status(self, invoice_id: str, status: str) -> None:
        self._exec("UPDATE invoices SET status=? WHERE id=?", (status, invoice_id))

    def invoice_exists(self, invoice_id: str) -> bool:
        return self._exec("SELECT 1 FROM invoices WHERE id=?", (invoice_id,)).fetchone() is not None

    def get_invoice(self, invoice_id: str) -> Invoice | None:
        row = self._exec("SELECT * FROM invoices WHERE id=?", (invoice_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        data.pop("status", None)
        return Invoice.model_validate(data)

    def get_invoices_for(self, orgnr: str, limit: int = 100,
                         exclude_statuses: tuple[str, ...] = ()) -> list[dict]:
        sql = "SELECT * FROM invoices WHERE supplier_orgnr=?"
        params: list = [orgnr]
        if exclude_statuses:
            sql += f" AND status NOT IN ({','.join('?' * len(exclude_statuses))})"
            params += list(exclude_statuses)
        sql += " ORDER BY issued_date DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._exec(sql, tuple(params)).fetchall()]

    def list_invoices(self, exclude_statuses: tuple[str, ...] = ("paid",),
                      limit: int = 200) -> list[dict]:
        """The operational inbox: newest first, seeded history ('paid') excluded
        by default so real traffic isn't drowned by 100+ backfill rows."""
        sql = "SELECT * FROM invoices"
        params: list = []
        if exclude_statuses:
            sql += f" WHERE status NOT IN ({','.join('?' * len(exclude_statuses))})"
            params += list(exclude_statuses)
        sql += " ORDER BY issued_date DESC, rowid DESC LIMIT ?"
        params.append(limit)
        return [dict(r) for r in self._exec(sql, tuple(params)).fetchall()]

    def find_duplicate(self, orgnr: str, amount_sek: float, reference: str,
                       exclude_id: str) -> dict | None:
        row = self._exec(
            "SELECT * FROM invoices WHERE supplier_orgnr=? AND amount_sek=? AND reference=? AND id!=? LIMIT 1",
            (orgnr, amount_sek, reference, exclude_id),
        ).fetchone()
        return dict(row) if row else None

    # -- payments ------------------------------------------------------------

    def add_payment(self, orgnr: str, invoice_id: str | None, amount_sek: float,
                    bank_account: str, paid_at: date) -> None:
        self._exec(
            "INSERT INTO payments (supplier_orgnr, invoice_id, amount_sek, bank_account, paid_at) VALUES (?,?,?,?,?)",
            (orgnr, invoice_id, amount_sek, bank_account, paid_at.isoformat()),
        )

    def get_payments(self, orgnr: str) -> list[dict]:
        rows = self._exec(
            "SELECT * FROM payments WHERE supplier_orgnr=? ORDER BY paid_at", (orgnr,)
        ).fetchall()
        return [dict(r) for r in rows]

    # -- cases + verdicts ----------------------------------------------------

    def save_case(self, case: VerificationCase) -> None:
        self._exec(
            "INSERT OR REPLACE INTO cases VALUES (?,?,?,?)",
            (case.id, case.claim.invoice_id, case.status.value, case.model_dump_json()),
        )
        if case.verdict is not None:
            self._exec(
                "INSERT OR REPLACE INTO verdicts VALUES (?,?,?,?,?,?)",
                (case.id, case.verdict.decision, case.verdict.confidence,
                 case.verdict.reasoning, case.verdict.recommended_action, case.human_decision),
            )

    def clear_verdict(self, case_id: str) -> None:
        """Drop a stale verdict row so a re-run starts clean."""
        self._exec("DELETE FROM verdicts WHERE case_id=?", (case_id,))

    def get_case(self, case_id: str) -> VerificationCase | None:
        row = self._exec("SELECT case_json FROM cases WHERE id=?", (case_id,)).fetchone()
        return VerificationCase.model_validate_json(row["case_json"]) if row else None

    def list_cases(self) -> list[VerificationCase]:
        rows = self._exec("SELECT case_json FROM cases ORDER BY rowid").fetchall()
        return [VerificationCase.model_validate_json(r["case_json"]) for r in rows]


_db: Database | None = None


def get_db() -> Database:
    global _db
    if _db is None:
        _db = Database()
    return _db
