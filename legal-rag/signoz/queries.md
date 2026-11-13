# SigNoz queries for the legal-rag telemetry

Use these in SigNoz (Metrics Explorer, Logs Explorer, Traces Explorer, or the ClickHouse SQL view).
Metric names follow SigNoz's OTLP-to-Prometheus normalisation: dots become underscores,
the unit is appended, and counters get `_total`. Confirm the exact names in Metrics Explorer
on your SigNoz version before saving dashboards.

## 1. Daily token consumption by type (PromQL)

```promql
sum by (type) (increase(claude_code_token_usage_tokens_total[1d]))
```

Series: `input`, `output`, `cacheRead`, `cacheCreation`.

## 2. Daily cost (PromQL)

```promql
sum(increase(claude_code_cost_usage_USD_total[1d]))
```

## 3. Cost alert: fire when a day's cost exceeds $50

```promql
sum(increase(claude_code_cost_usage_USD_total[1d])) > 50
```

Rule definition is in `alert_cost.yaml`.

## 4. Trace-to-log: every log line for one trace (Logs Explorer)

Filter: `trace_id = '<TRACE_ID>'`, ordered by timestamp.

ClickHouse form (table name varies by SigNoz version; check `signoz_logs` in your install):

```sql
SELECT timestamp, severity_text, span_id, body
FROM signoz_logs.distributed_logs_v2
WHERE trace_id = '<TRACE_ID>'
ORDER BY timestamp;
```

The same query runs against the stand-in collector:

```bash
curl -s "http://127.0.0.1:8080/api/logs?trace_id=<TRACE_ID>"
```

## 5. Slowest operations in a trace (Traces Explorer)

Filter: `traceID = '<TRACE_ID>'`, sort by duration. For the slow `/search?slow=1` requests, the
dominant span is `db.query audit_log` (about 490 ms of a 497 ms request).

## 6. Find the slow DB spans across traces

```
name = 'db.query audit_log' AND durationNano > 100000000
```

## 7. Root-cause check: query plan for the slow statement (run against the app database)

```sql
EXPLAIN QUERY PLAN
SELECT COUNT(*), MAX(ts) FROM audit_log
WHERE doc_id = ? AND payload LIKE '%view clause 4.%';
-- SCAN audit_log   <- full table scan of 2,000,000 rows
```

Index experiment on a copy of the database (measured, best of 3):

| Variant | Query time | Plan |
|---|---|---|
| As shipped (no index) | ~346 ms | `SCAN audit_log` |
| Index on `doc_id` only, same `LIKE` | ~398 ms | index search, but still reads most of the document's rows |
| `clause` column plus `(doc_id, clause, ts)` index | ~109 ms | covering index search |

A single-column index on `doc_id` does not fix the problem, because the leading-wildcard `LIKE`
still has to inspect almost every row for the document. The fix is to store the clause as its
own indexed column and filter on it.
