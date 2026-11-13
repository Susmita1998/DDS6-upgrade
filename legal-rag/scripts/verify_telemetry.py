"""Verify the telemetry setup end to end. Exit code 0 means every check passed.

Checks:
  1. Claude Code telemetry env vars are set in .claude/settings.json (and match the expected values)
  2. The OTLP receiver (:4317) and the collector API (:8080) accept connections
  3. legal-rag spans, logs and metrics have arrived
  4. Claude Code claude_code.* metrics have arrived
  5. A log line carries the trace_id of a span from the same trace (trace-to-log correlation)

Usage (from legal-rag):  python scripts/verify_telemetry.py
"""

import json
import socket
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
SETTINGS = ROOT / ".claude" / "settings.json"
COLLECTOR = "http://127.0.0.1:8080"
EXPECTED_ENV = {
    "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
    "OTEL_METRICS_EXPORTER": "otlp",
    "OTEL_LOGS_EXPORTER": "otlp",
    "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
}

results = []


def check(name, ok, detail=""):
    results.append(ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def port_open(port):
    with socket.socket() as s:
        s.settimeout(2)
        return s.connect_ex(("127.0.0.1", port)) == 0


def main():
    env = json.loads(SETTINGS.read_text()).get("env", {})
    for key, expected in EXPECTED_ENV.items():
        actual = env.get(key)
        check(f"settings.json env {key}", actual == expected, f"got {actual!r}, want {expected!r}")

    check("OTLP gRPC receiver listening on :4317", port_open(4317))
    check("collector API listening on :8080", port_open(8080))
    if not all(results):
        return 1

    client = httpx.Client(base_url=COLLECTOR, timeout=10)
    status = client.get("/api/status").json()
    check("legal-rag spans arrived", "legal-rag" in status["services"] and status["spans"] > 0, f"{status['spans']} spans")
    check("legal-rag logs arrived", status["logs"] > 0, f"{status['logs']} logs")
    check("metric points arrived", status["metric_points"] > 0, f"{status['metric_points']} points")

    daily = client.get("/api/metrics/daily?days=7").json()
    token_types = sorted(daily["tokens"])
    check(
        "claude_code token metric present",
        bool(token_types),
        f"types={token_types}" + (" (synthetic)" if daily["synthetic"] else " (real Claude Code)"),
    )

    traces = client.get("/api/traces?limit=50").json()
    correlated = False
    for t in traces:
        detail = client.get(f"/api/traces/{t['trace_id']}").json()
        if any(log["trace_id"] == t["trace_id"] for log in detail["logs"]):
            correlated = True
            break
    check("log line shares trace_id with a span", correlated)

    passed = sum(results)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
