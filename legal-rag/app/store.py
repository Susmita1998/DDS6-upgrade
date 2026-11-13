"""SQLite store for parent clauses, child chunks and an audit log.

Every query runs inside a custom span named `db.query <table>` (or `db.write <table>`)
and increments the rag.db.calls counter.
"""

import random
import sqlite3
import threading
import time
from pathlib import Path

from opentelemetry import metrics, trace
from opentelemetry.trace import SpanKind

tracer = trace.get_tracer("legal-rag.store")
db_calls = metrics.get_meter("legal-rag").create_counter(
    "rag.db.calls", description="SQLite calls issued by the RAG store"
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id TEXT PRIMARY KEY,
    title TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS parent_clause (
    parent_id TEXT PRIMARY KEY,
    doc_id TEXT NOT NULL,
    clause_id TEXT NOT NULL,
    heading TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS child_chunk (
    child_id TEXT PRIMARY KEY,
    parent_id TEXT NOT NULL,
    doc_id TEXT NOT NULL,
    clause_id TEXT NOT NULL,
    title TEXT NOT NULL,
    heading TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_child_parent ON child_chunk(parent_id);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    doc_id TEXT NOT NULL,
    action TEXT NOT NULL,
    payload TEXT NOT NULL
);
"""


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def _query(self, table, sql, params=()):
        with tracer.start_as_current_span(f"db.query {table}", kind=SpanKind.CLIENT) as span:
            span.set_attribute("db.system", "sqlite")
            span.set_attribute("db.operation", "SELECT")
            span.set_attribute("db.name", table)
            span.set_attribute("db.statement", sql)
            db_calls.add(1, {"db.operation": "SELECT", "db.name": table})
            with self._lock:
                return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _write(self, table, sql, rows):
        with tracer.start_as_current_span(f"db.write {table}", kind=SpanKind.CLIENT) as span:
            span.set_attribute("db.system", "sqlite")
            span.set_attribute("db.operation", "INSERT")
            span.set_attribute("db.name", table)
            span.set_attribute("db.statement", sql)
            span.set_attribute("db.rows", len(rows))
            db_calls.add(1, {"db.operation": "INSERT", "db.name": table})
            with self._lock:
                self._conn.executemany(sql, rows)
                self._conn.commit()

    def save_document(self, doc_id, title, parents, children):
        self._write("documents", "INSERT OR REPLACE INTO documents VALUES (?, ?)", [(doc_id, title)])
        self._write(
            "parent_clause",
            "INSERT OR REPLACE INTO parent_clause VALUES (?, ?, ?, ?, ?)",
            [(p["parent_id"], p["doc_id"], p["clause_id"], p["heading"], p["text"]) for p in parents],
        )
        self._write(
            "child_chunk",
            "INSERT OR REPLACE INTO child_chunk VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (c["child_id"], c["parent_id"], c["doc_id"], c["clause_id"], c["title"], c["heading"], c["text"])
                for c in children
            ],
        )

    def doc_ids(self):
        return [r["doc_id"] for r in self._query("documents", "SELECT doc_id FROM documents")]

    def doc_count(self):
        return self._query("documents", "SELECT COUNT(*) AS n FROM documents")[0]["n"]

    def all_children(self):
        return self._query(
            "child_chunk",
            "SELECT child_id, parent_id, doc_id, clause_id, title, heading, text FROM child_chunk",
        )

    def get_parents(self, parent_ids):
        if not parent_ids:
            return []
        marks = ",".join("?" * len(parent_ids))
        return self._query(
            "parent_clause",
            f"SELECT parent_id, doc_id, clause_id, heading, text FROM parent_clause WHERE parent_id IN ({marks})",
            tuple(parent_ids),
        )

    def seed_audit(self, rows=2_000_000, chunk=100_000):
        """Fill audit_log with synthetic history once (only when empty), so the table is large enough to matter."""
        if self._query("audit_log", "SELECT COUNT(*) AS n FROM audit_log")[0]["n"] > 0:
            return
        rng = random.Random(7)
        docs = self.doc_ids() or ["unknown"]
        now = time.time()
        for _ in range(rows // chunk):
            data = [
                (
                    now - rng.randint(0, 90 * 86400),
                    rng.choice(docs),
                    "read",
                    f"user=u{rng.randrange(5000)} view clause {rng.randint(1, 11)}.{rng.randint(1, 4)}",
                )
                for _ in range(chunk)
            ]
            self._write("audit_log", "INSERT INTO audit_log (ts, doc_id, action, payload) VALUES (?, ?, ?, ?)", data)

    def record_access(self, doc_id, action, payload):
        self._write(
            "audit_log",
            "INSERT INTO audit_log (ts, doc_id, action, payload) VALUES (?, ?, ?, ?)",
            [(time.time(), doc_id, action, payload)],
        )

    def audit_report(self, doc_id):
        rows = self._query(
            "audit_log",
            "SELECT COUNT(*) AS views, MAX(ts) AS last_view FROM audit_log "
            "WHERE doc_id = ? AND payload LIKE '%view clause 4.%'",
            (doc_id,),
        )
        return rows[0]
