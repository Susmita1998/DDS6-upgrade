"""Legal RAG API (Method 1), instrumented with OpenTelemetry, plus the three-tab console at /ui.

Run from the legal-rag directory:
    uvicorn app.main:app --port 8100
"""

import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.sqlite3 import SQLite3Instrumentor
from pydantic import BaseModel

from . import evaluation, pipeline, rag
from .store import Store
from .telemetry import setup_telemetry

BASE = Path(__file__).resolve().parent
DB_PATH = os.getenv("LEGAL_RAG_DB", str(BASE.parent / "data" / "legal_rag.db"))
CONTRACTS = BASE / "data" / "contracts"
UI = BASE / "ui" / "index.html"
AUDIT_SEED_ROWS = 2_000_000
SLOW_REPORT_MS = 100

log = logging.getLogger("legal-rag.api")
tracer = trace.get_tracer("legal-rag.api")
providers = setup_telemetry()
SQLite3Instrumentor().instrument()

store: Store
index: rag.HybridIndex


def ingest_document(doc_id, title, text):
    global index
    clauses = rag.parse_clauses(text)
    parents, children = rag.chunk_clauses(doc_id, title, clauses)
    store.save_document(doc_id, title, parents, children)
    index = rag.HybridIndex(store.all_children())
    log.info("ingested doc=%s parents=%d children=%d", doc_id, len(parents), len(children))
    return len(parents), len(children)


@asynccontextmanager
async def lifespan(app):
    global store, index
    store = Store(DB_PATH)
    if store.doc_count() == 0:
        for path in sorted(CONTRACTS.glob("*.txt")):
            text = path.read_text(encoding="utf-8")
            ingest_document(path.stem, text.splitlines()[0].strip().title(), text)
    store.seed_audit(AUDIT_SEED_ROWS)
    index = rag.HybridIndex(store.all_children())
    log.info("index ready: %d child chunks", len(index.children))
    yield
    for provider in providers:
        provider.shutdown()


app = FastAPI(title="Legal RAG (Method 1)", lifespan=lifespan)
# The console polls these routes every second or so; leave them out so they don't crowd real traces.
FastAPIInstrumentor.instrument_app(app, excluded_urls="health,api/eval/status")


class IngestRequest(BaseModel):
    doc_id: str
    title: str
    text: str


class AskRequest(BaseModel):
    question: str


@app.get("/health")
def health():
    return {"status": "ok", "child_chunks": len(index.children)}


@app.get("/ui")
def ui():
    return FileResponse(UI)


@app.post("/ingest")
def ingest(req: IngestRequest):
    parents, children = ingest_document(req.doc_id, req.title, req.text)
    return {"doc_id": req.doc_id, "parents": parents, "children": children}


@app.post("/ask")
def ask(req: AskRequest):
    result = pipeline.answer(req.question, index, store)
    return {"answer": result["answer"], "llm": result["llm"], "citations": result["citations"]}


@app.post("/api/rag/query")
def rag_query(req: AskRequest):
    """Full pipeline detail for the Hybrid RAG tab: per-stage results, timings, context, trace id."""
    return pipeline.answer(req.question, index, store)


@app.get("/search")
def search(q: str, slow: int = Query(0, ge=0, le=1)):
    hits = index.retrieve(q)
    report = None
    if slow:
        doc_id = hits[0].doc_id if hits else "none"
        started = time.perf_counter()
        report = store.audit_report(doc_id)
        elapsed_ms = (time.perf_counter() - started) * 1000
        log.info("audit report doc=%s views=%s in %.0f ms", doc_id, report["views"], elapsed_ms)
        if elapsed_ms > SLOW_REPORT_MS:
            log.warning("audit report slow: %.0f ms for doc=%s (full table scan on audit_log)", elapsed_ms, doc_id)
    return {"query": q, "hits": [asdict(h) for h in hits], "audit": report}


@app.get("/api/golden")
def golden():
    return evaluation.load_golden()


@app.post("/api/eval/run", status_code=202)
def eval_run():
    try:
        return {"run_id": evaluation.start(index, store)}
    except RuntimeError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/eval/status")
def eval_status():
    return evaluation.status()


@app.get("/api/eval/runs")
def eval_runs():
    return evaluation.list_runs()
