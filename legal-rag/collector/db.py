"""SQLite storage for OTLP spans, logs and metrics received by the stand-in collector."""

import os
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

DB_PATH = Path(os.getenv("COLLECTOR_DB", Path(__file__).resolve().parent / "data" / "telemetry.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS spans (
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    parent_span_id TEXT NOT NULL,
    name TEXT NOT NULL,
    service TEXT NOT NULL,
    kind TEXT NOT NULL,
    start_ns INTEGER NOT NULL,
    end_ns INTEGER NOT NULL,
    status TEXT NOT NULL,
    attributes TEXT NOT NULL,
    resource TEXT NOT NULL,
    PRIMARY KEY (trace_id, span_id)
);
CREATE INDEX IF NOT EXISTS idx_spans_start ON spans(start_ns);
CREATE TABLE IF NOT EXISTS logs (
    id INTEGER PRIMARY KEY,
    ts_ns INTEGER NOT NULL,
    service TEXT NOT NULL,
    severity TEXT NOT NULL,
    body TEXT NOT NULL,
    trace_id TEXT NOT NULL,
    span_id TEXT NOT NULL,
    attributes TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_logs_trace ON logs(trace_id);
CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY,
    ts_ns INTEGER NOT NULL,
    service TEXT NOT NULL,
    name TEXT NOT NULL,
    unit TEXT NOT NULL,
    mtype TEXT NOT NULL,
    monotonic INTEGER NOT NULL,
    value REAL NOT NULL,
    attributes TEXT NOT NULL,
    resource TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_metric_point ON metrics(service, name, attributes, ts_ns);
CREATE INDEX IF NOT EXISTS idx_metrics_name ON metrics(name, ts_ns);
"""


class TelemetryDB:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(SCHEMA)

    def _write(self, sql, rows):
        if not rows:
            return
        with self._lock:
            self._conn.executemany(sql, rows)
            self._conn.commit()

    def insert_spans(self, rows):
        self._write("INSERT OR REPLACE INTO spans VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)

    def insert_logs(self, rows):
        self._write("INSERT INTO logs (ts_ns, service, severity, body, trace_id, span_id, attributes) VALUES (?, ?, ?, ?, ?, ?, ?)", rows)

    def insert_metrics(self, rows):
        self._write(
            "INSERT OR IGNORE INTO metrics (ts_ns, service, name, unit, mtype, monotonic, value, attributes, resource) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )

    def query(self, sql, params=()):
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]


@lru_cache(maxsize=1)
def get_db():
    return TelemetryDB(DB_PATH)
