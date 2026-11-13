# Enable & Verify Telemetry

Scope: OpenTelemetry for the legal RAG API and for Claude Code, sent to a collector on
`localhost:4317`. Real SigNoz could not run in this environment (no Docker), so a stand-in
collector receives the data. Its queries and dashboards use SigNoz-compatible names, so the
same configuration works against SigNoz by changing only the endpoint.

## 0. Run book

```bash
cd /home/labuser/Downloads/DD6/legal-rag
pip install -r requirements.txt

# 1. Collector: OTLP/gRPC on :4317, dashboard and API on :8080
python -m collector.server

# 2. RAG API (port 8100; 8000 was already in use on this machine)
python -m uvicorn app.main:app --port 8100

# 3. Traffic, synthetic Claude Code metrics, verification
python scripts/load.py --rounds 3
python scripts/gen_claude_metrics.py
python scripts/verify_telemetry.py     # expect: 11/11 checks passed
```

Open the dashboard at http://127.0.0.1:8080.

---

## 1. Enable Claude Code OpenTelemetry

Set these four variables (plus the protocol), either in the project or in the shell:

| Variable | Value |
|---|---|
| `CLAUDE_CODE_ENABLE_TELEMETRY` | `1` |
| `OTEL_METRICS_EXPORTER` | `otlp` |
| `OTEL_LOGS_EXPORTER` | `otlp` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | `http://localhost:4317` |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `grpc` |

Where they live:
- `.claude/settings.json` (`env` block). Claude Code applies these to sessions started in this
  project. This is the preferred option.
- `env.sh`. Run `source env.sh` before starting `claude` from a shell.

Confirm:

```bash
python scripts/verify_telemetry.py
```

The first block of checks compares `.claude/settings.json` against the expected values, and checks
that `:4317` and `:8080` accept connections. Current result: all pass.

Notes:
- The variables in this shell are unset. That is expected, because the project settings are read
  by Claude Code, not by this shell.
- Real `claude_code.*` metrics appear after a Claude Code session in this project runs with the
  settings above. This plan has no live session, so `scripts/gen_claude_metrics.py` sends
  synthetic points with the same names and a `demo.synthetic=true` resource label.

---

## 2. Instrument the FastAPI service

Packages (`requirements.txt`):
`opentelemetry-sdk`, `opentelemetry-exporter-otlp-proto-grpc`,
`opentelemetry-instrumentation-fastapi`, `opentelemetry-instrumentation-sqlite3`, `grpcio`.

Files:
- `app/telemetry.py`: sets up the tracer, meter, and logger providers. Each exports over OTLP/gRPC
  to `OTEL_EXPORTER_OTLP_ENDPOINT` (default `http://localhost:4317`). Its log handler adds the
  active `trace_id` and `span_id` to every log record.
- `app/main.py`: `FastAPIInstrumentor.instrument_app(app)` gives an automatic server span for each
  HTTP request, plus HTTP metrics. `SQLite3Instrumentor` covers database calls automatically.
- `app/store.py`: custom spans for database calls. Each query runs in a span named
  `db.query <table>` (or `db.write <table>`) with `db.system`, `db.operation`, `db.name`, and
  `db.statement`. The `rag.db.calls` counter records each call.
- `app/rag.py`: spans for each pipeline stage (`rag.retrieve`, `rag.sparse_search`,
  `rag.dense_search`, `rag.fuse`, `rag.rerank`, `rag.generate`) and the
  `rag.retrieval.duration` histogram.

Verify:

```bash
curl -s -X POST localhost:8100/ask -H 'content-type: application/json' \
  -d '{"question":"What is the liability cap and what are the carve-outs?"}'
curl -s "http://127.0.0.1:8080/api/traces?limit=5"
```

Each trace has a `POST /ask` root span with `rag.ask`, `rag.retrieve`, `rag.rerank`,
`db.query parent_clause`, and `rag.generate` below it. Check the latency table on the dashboard.

Known gap: the plan listed `opentelemetry-instrumentation-logging`. It was not used, because the
SDK log handler in `app/telemetry.py` already stamps `trace_id` and `span_id`.

---

## 3. Correlate a trace to its logs

Given a `trace_id`, every log line that shares it:

SigNoz Logs Explorer: `trace_id = '<TRACE_ID>'`

Stand-in collector:

```bash
curl -s "http://127.0.0.1:8080/api/logs?trace_id=<TRACE_ID>"
```

ClickHouse form (check the table name for your SigNoz version):

```sql
SELECT timestamp, severity_text, span_id, body
FROM signoz_logs.distributed_logs_v2
WHERE trace_id = '<TRACE_ID>'
ORDER BY timestamp;
```

The `<paste trace_id here>` placeholder was not filled in, so this section uses a real trace from
a slow request: **`7df5a52e984e6d8574bfdfb8da455fa2`** (`GET /search?slow=1`, 497 ms). Replace it with
your own ID to reproduce.

Span tree for that trace:

```
GET /search                       497.4 ms   SERVER
  rag.retrieve                      0.5 ms
  db.query audit_log              493.2 ms   CLIENT   <- 99% of the request
  GET /search http send             0.0 ms
```

Correlated logs (same `trace_id`, same span):

```
INFO  audit report doc=msa_acme_vendor views=60645 in 493 ms
WARN  audit report slow: 493 ms for doc=msa_acme_vendor (full table scan on audit_log)
```

**Root cause of the elevated latency:** `audit_log` has 2,000,000 rows and no index that covers the
filter. The statement `WHERE doc_id = ? AND payload LIKE '%view clause 4.%'` runs as a full table
scan (`EXPLAIN QUERY PLAN` → `SCAN audit_log`). The leading `%` in the `LIKE` also prevents index
use on `payload`. The flamegraph shows that the DB span accounts for almost the whole request, while
retrieval takes under 1 ms.

Fix tested on a copy of the database (best of 3 runs):

| Variant | Time | Plan |
|---|---|---|
| As shipped | ~346 ms | `SCAN audit_log` |
| Index on `doc_id` only | ~398 ms | still reads most rows for the document (no gain) |
| `clause` column plus `(doc_id, clause, ts)` index | ~109 ms | covering index search |

The single-column index does not help. The fix needs the clause as its own indexed column, plus
a query that filters on it. The app was left unchanged, so the slow path still demonstrates the
problem.

---

## 4. Cost visibility dashboard and alert

Queries (PromQL, also in `signoz/queries.md`):

```promql
sum by (type) (increase(claude_code_token_usage_tokens_total[1d]))   # daily tokens: input, output, cacheRead, cacheCreation
sum(increase(claude_code_cost_usage_USD_total[1d]))                  # daily cost
sum(increase(claude_code_cost_usage_USD_total[1d])) > 50             # alert condition
```

Alert rule: `signoz/alert_cost.yaml`. Create it in the SigNoz UI with the same expression, condition
"above 50", evaluated every 5 minutes.

Dashboard:
- SigNoz: import `signoz/dashboard.json`. The schema is best-effort, so if import fails, recreate
  the three panels from `signoz/queries.md`.
- Interactive local dashboard: http://127.0.0.1:8080. It shows daily tokens by type with toggles,
  daily cost against the $50 line, the alert state, a trace list with a flamegraph and logs per
  trace, and latency by operation. It refreshes every 5 seconds.

Current state (synthetic demo data, seven days ending today):

| Day (UTC) | Cost |
|---|---|
| 10-20 | $38.55 |
| 10-21 | $46.35 |
| 10-22 | $30.23 |
| 10-23 | $52.80 (over $50, historical) |
| 10-24 | $37.73 |
| 10-25 | $42.15 |
| 10-26 (today, so far) | $71.93 → **alert FIRING** |

The alert state is computed from today's partial total. Checking the OK branch: with the threshold
raised to $100, the same data returns `OK`.

---

## 5. Verification checklist

| # | Check | How | Expected |
|---|---|---|---|
| 1 | Settings present | `verify_telemetry.py` | 4 env checks pass |
| 2 | Collector reachable | `verify_telemetry.py` | `:4317` and `:8080` pass |
| 3 | App spans arrive | `GET /api/status` | `legal-rag` in services, spans > 0 |
| 4 | App logs arrive | `GET /api/status` | logs > 0 |
| 5 | Claude metrics arrive | `GET /api/metrics/daily` | token types present, `synthetic: true` |
| 6 | Trace-to-log correlation | `verify_telemetry.py` | log shares a `trace_id` with a span |
| 7 | Daily cost math | `/api/metrics/daily` | matches `gen_claude_metrics.py` output within rounding |
| 8 | Alert fires / clears | `/api/metrics/daily` | `FIRING` at $50; `OK` if the limit is raised |
| 9 | Dashboard renders | open http://127.0.0.1:8080 | panels render; no console errors |
| 10 | Prometheus endpoint | `curl localhost:8080/metrics` | `claude_code_cost_usage_USD_total` and token series |

Result of this run: checks 1–7, 8, and 10 pass; check 9 was done by screenshot only. The dashboard
rendered in headless Chrome and looked correct. Browser console errors were not checked, and
clicking a trace in the browser was not tested. The flamegraph path is checked through the API
response only.

---

## 6. Console: three tabs

Open http://127.0.0.1:8100/ui. The header shows whether the RAG API and the collector are up.

| Tab | What it does | Backed by |
|---|---|---|
| 1 · Hybrid RAG Live | Ask a question. Shows the answer, citations, each retrieval stage (BM25, dense, RRF, rerank) with scores and timings, and the clauses sent to the generator. "Open trace" jumps to the trace in tab 2. | `POST /api/rag/query` |
| 2 · Live Telemetry | The collector dashboard (tokens, cost, alert, traces, flamegraph, latency), embedded. `?trace=<id>` opens a trace directly. | collector on :8080 (iframe) |
| 3 · GoldenSet Evaluation & KPIs | Runs all 20 golden cases in a background thread. Results stream into the table as they finish. Shows KPI tiles, per-type breakdown, case details, and run history. Each run is saved to `eval/runs/<run_id>.json`. | `POST /api/eval/run`, polled `GET /api/eval/status` |

Deep links: `/ui?q=<question>#rag` runs a question on load. `/ui#eval` opens the evaluation tab.

Evaluation traces: each run is one trace, `eval.run`, with one `eval.case` span per case. The
pipeline spans for each case nest underneath, so a failing case can be opened as a flamegraph.

Run recorded in this session (`20261026T043737Z`, extractive mode):

| Metric | Value |
|---|---|
| Pass rate | 50% (10 / 20) |
| Context recall | 78% |
| Context precision | 25% |
| Faithfulness (lexical) | 87% |
| Answer recall (lexical) | 67% |
| Answer relevancy (lexical) | 45% |
| Negative refusal | 0% (0 / 3) |
| Latency p95 | 8.3 ms |

Why cases fail:
- **No stemming.** "grossly negligent" does not match "gross negligence" (TC_008), and "excluded" does
  not match "Exclusions" (TC_017). Both get 0% context recall. Adding a stemmer to `rag.tokenize` is
  the first fix.
- **Negative cases are not refused.** The three out-of-scope questions score 0.5–0.7 in the reranker,
  well above the 0.25 "not found" threshold. Generic words such as "within" and "days" give partial
  coverage. Raising the threshold, or requiring coverage of the rarest query term, would fix this.
- **Low precision.** Five parent clauses are always returned, so most are irrelevant to the question.
  Returning fewer clauses when the score drops would raise precision.
- **Section bonus is not scoped.** A query for "Section 4.1" also boosts the NDA's 4.1.

The evaluation is deliberately lexical. Its results show the retrieval gaps above. They do not
measure answer quality the way an LLM judge would. Re-run with `ANTHROPIC_API_KEY` set to get
generated answers, and then add an LLM-judged faithfulness score.

## 7. Limits of this setup

- This is a stand-in for SigNoz, not SigNoz itself. The SigNoz dashboard and alert files were
  written by hand against SigNoz's documented query names. They have not been imported into a
  running SigNoz instance.
- Embeddings are a hashed lexical stand-in (`app/rag.py`), not a neural model. Retrieval quality
  is a demonstration, not a benchmark.
- The `ANTHROPIC_API_KEY` path in `rag.generate` is written but untested here (no key). Without
  the key, answers are extractive and cite clauses.
- Real Claude Code telemetry has not been observed. Those checks need a Claude Code session started
  in this project after `.claude/settings.json` is in place.
- The reranker's section-number bonus is not scoped to a document. A query for "Section 4.1" also
  boosts the NDA's section 4.1. Scope the match to the requested document once the app has a doc
  filter.
