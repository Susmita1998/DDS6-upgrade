"""Dashboard and query API for the stand-in collector."""

import bisect
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse

from collector.db import get_db

DASHBOARD = Path(__file__).resolve().parent / "dashboard" / "index.html"
TOKEN_METRIC = "claude_code.token.usage"
COST_METRIC = "claude_code.cost.usage"
DAILY_COST_LIMIT = 50.0
DAY_NS = 86400 * 10**9

app = FastAPI(title="Stand-in collector API")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:8100", "http://localhost:8100"],  # the console's status dot polls /api/status
    allow_methods=["GET"],
)
db = get_db()


def percentile(sorted_vals, p):
    return sorted_vals[int(p * (len(sorted_vals) - 1))] if sorted_vals else 0.0


def build_tree(spans):
    by_id = {s["span_id"]: s for s in spans}

    def depth(span):
        parent = span["parent_span_id"]
        return 0 if not parent or parent not in by_id else 1 + depth(by_id[parent])

    return {s["span_id"]: depth(s) for s in spans}


def daily_increase(name, days, group_key=None):
    """Per-day increase of a cumulative counter, summed per group, computed from the stored points."""
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    starts = [int((today - timedelta(days=days - 1 - i)).timestamp() * 1e9) for i in range(days)]
    series = defaultdict(list)
    for row in db.query("SELECT ts_ns, value, attributes FROM metrics WHERE name = ? ORDER BY ts_ns", (name,)):
        series[row["attributes"]].append((row["ts_ns"], row["value"]))

    groups = defaultdict(lambda: [0.0] * days)
    for attributes, points in series.items():
        ts = [p[0] for p in points]
        vals = [p[1] for p in points]
        labels = json.loads(attributes)
        group = str(labels.get(group_key, "total")) if group_key else "total"
        for i, start in enumerate(starts):
            end = start + DAY_NS
            idx_end = bisect.bisect_left(ts, end)
            idx_start = bisect.bisect_left(ts, start)
            end_v = vals[idx_end - 1] if idx_end else 0.0
            start_v = vals[idx_start - 1] if idx_start else 0.0
            # A counter that went backwards was reset, so the whole end value is new.
            groups[group][i] += end_v - start_v if end_v >= start_v else end_v
    return groups


@app.get("/")
def dashboard():
    return FileResponse(DASHBOARD)


@app.get("/api/status")
def status():
    counts = db.query(
        "SELECT (SELECT COUNT(DISTINCT trace_id) FROM spans) AS traces, "
        "(SELECT COUNT(*) FROM logs) AS logs, (SELECT COUNT(*) FROM metrics) AS metric_points, "
        "(SELECT COUNT(*) FROM spans) AS spans"
    )[0]
    services = [r["service"] for r in db.query("SELECT DISTINCT service FROM spans UNION SELECT DISTINCT service FROM metrics")]
    return {**counts, "services": services}


@app.get("/api/traces")
def traces(limit: int = 50):
    rows = db.query(
        """SELECT trace_id, MIN(start_ns) AS start_ns, MAX(end_ns) AS end_ns, COUNT(*) AS span_count,
                  SUM(CASE WHEN status = 'ERROR' THEN 1 ELSE 0 END) AS errors,
                  COALESCE(
                    (SELECT r.name FROM spans r WHERE r.trace_id = s.trace_id AND r.parent_span_id = '' LIMIT 1),
                    (SELECT f.name FROM spans f WHERE f.trace_id = s.trace_id ORDER BY f.start_ns LIMIT 1)
                  ) AS root
           FROM spans s GROUP BY trace_id ORDER BY start_ns DESC LIMIT ?""",
        (limit,),
    )
    for r in rows:
        r["duration_ms"] = (r["end_ns"] - r["start_ns"]) / 1e6
    return rows


@app.get("/api/traces/{trace_id}")
def trace_detail(trace_id: str):
    spans = db.query("SELECT * FROM spans WHERE trace_id = ? ORDER BY start_ns", (trace_id,))
    if not spans:
        raise HTTPException(404, "trace not found")
    t0 = spans[0]["start_ns"]
    t1 = max(s["end_ns"] for s in spans)
    depths = build_tree(spans)
    out = [
        {
            "span_id": s["span_id"],
            "parent_span_id": s["parent_span_id"],
            "name": s["name"],
            "service": s["service"],
            "kind": s["kind"],
            "status": s["status"],
            "depth": depths[s["span_id"]],
            "start_ms": (s["start_ns"] - t0) / 1e6,
            "duration_ms": (s["end_ns"] - s["start_ns"]) / 1e6,
            "attributes": json.loads(s["attributes"]),
        }
        for s in spans
    ]
    return {
        "trace_id": trace_id,
        "duration_ms": (t1 - t0) / 1e6,
        "spans": out,
        "logs": db.query(
            "SELECT ts_ns, severity, body, trace_id, span_id, attributes FROM logs WHERE trace_id = ? ORDER BY ts_ns",
            (trace_id,),
        ),
    }


@app.get("/api/logs")
def logs(trace_id: str | None = None, limit: int = 200):
    if trace_id:
        return db.query(
            "SELECT ts_ns, service, severity, body, trace_id, span_id FROM logs WHERE trace_id = ? ORDER BY ts_ns",
            (trace_id,),
        )
    return db.query(
        "SELECT ts_ns, service, severity, body, trace_id, span_id FROM logs ORDER BY ts_ns DESC LIMIT ?", (limit,)
    )


@app.get("/api/summary")
def summary():
    rows = db.query(
        "SELECT name, (end_ns - start_ns) / 1e6 AS ms FROM spans "
        "WHERE kind = 'SERVER' OR name LIKE 'rag.%' OR name LIKE 'db.%'"
    )
    by_name = defaultdict(list)
    for r in rows:
        by_name[r["name"]].append(r["ms"])
    out = []
    for name, vals in by_name.items():
        vals.sort()
        out.append(
            {
                "name": name,
                "count": len(vals),
                "p50": percentile(vals, 0.5),
                "p95": percentile(vals, 0.95),
                "max": vals[-1],
            }
        )
    return sorted(out, key=lambda r: -r["p95"])


@app.get("/api/metrics/daily")
def metrics_daily(days: int = 7):
    tokens = daily_increase(TOKEN_METRIC, days, "type")
    cost = daily_increase(COST_METRIC, days).get("total", [0.0] * days)
    today = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    labels = [(today - timedelta(days=days - 1 - i)).date().isoformat() for i in range(days)]
    synthetic = bool(db.query("SELECT 1 FROM metrics WHERE resource LIKE '%demo.synthetic%' LIMIT 1"))
    return {
        "labels": labels,
        "tokens": tokens,
        "cost": cost,
        "cost_limit": DAILY_COST_LIMIT,
        "alert": {
            "name": "claude_code_daily_cost_over_50",
            "state": "FIRING" if cost[-1] > DAILY_COST_LIMIT else "OK",
            "evaluated": "today (UTC, so far)",
            "today": cost[-1],
        },
        "synthetic": synthetic,
    }


def prom_name(name, unit, mtype):
    base = re.sub(r"[^a-zA-Z0-9_]", "_", name)
    unit_part = f"_{re.sub(r'[^a-zA-Z0-9_]', '_', unit)}" if unit and unit != "1" and mtype != "histogram" else ""
    if mtype == "sum":
        return f"{base}{unit_part}_total"
    return f"{base}{unit_part}"


@app.get("/metrics", response_class=PlainTextResponse)
def prometheus():
    rows = db.query(
        """SELECT m.service, m.name, m.unit, m.mtype, m.value, m.attributes
           FROM metrics m
           JOIN (SELECT service, name, attributes, MAX(ts_ns) AS ts FROM metrics GROUP BY service, name, attributes) latest
             ON m.service = latest.service AND m.name = latest.name
            AND m.attributes = latest.attributes AND m.ts_ns = latest.ts
           ORDER BY m.name"""
    )
    lines, typed = [], set()
    for r in rows:
        pname = prom_name(r["name"], r["unit"], r["mtype"])
        if pname not in typed:
            typed.add(pname)
            kind = "counter" if r["mtype"] == "sum" else "gauge"
            lines.append(f"# TYPE {pname} {kind}")
        labels = {"service_name": r["service"], **json.loads(r["attributes"])}
        label_str = ",".join(f'{k}="{str(v).replace(chr(34), chr(92) + chr(34))}"' for k, v in sorted(labels.items()))
        lines.append(f"{pname}{{{label_str}}} {r['value']}")
    return "\n".join(lines) + "\n"
