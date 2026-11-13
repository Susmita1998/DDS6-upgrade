"""Send SYNTHETIC Claude Code token and cost metrics to the collector over OTLP/gRPC.

Real Claude Code emits `claude_code.token.usage` (attribute `type`: input | output |
cacheRead | cacheCreation) and `claude_code.cost.usage` (USD) once telemetry is enabled
and a session runs. This script fabricates seven days of cumulative counters with the
same names so the cost dashboard and alert can be exercised without spending money.
Every point is tagged with resource attribute demo.synthetic=true.

Usage (from legal-rag):  python scripts/gen_claude_metrics.py [--endpoint localhost:4317]
"""

import argparse
import time
from datetime import datetime, timedelta, timezone

import grpc
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.common.v1 import common_pb2
from opentelemetry.proto.metrics.v1 import metrics_pb2
from opentelemetry.proto.resource.v1 import resource_pb2

# Sonnet list prices in USD per million tokens, used to derive cost from token counts.
PRICE = {"input": 3.00, "output": 15.00, "cacheRead": 0.30, "cacheCreation": 3.75}
MODEL = "claude-sonnet-5-5"

# Daily token volumes for the six days before today, then today so far.
# Day 3 breaches $50 historically; today breaches it so the alert shows FIRING.
DAILY_TOKENS = [
    {"input": 3_100_000, "output": 900_000, "cacheRead": 35_000_000, "cacheCreation": 1_400_000},
    {"input": 4_000_000, "output": 1_000_000, "cacheRead": 42_000_000, "cacheCreation": 1_800_000},
    {"input": 2_400_000, "output": 700_000, "cacheRead": 28_000_000, "cacheCreation": 1_100_000},
    {"input": 3_600_000, "output": 1_200_000, "cacheRead": 55_000_000, "cacheCreation": 2_000_000},
    {"input": 2_900_000, "output": 800_000, "cacheRead": 38_000_000, "cacheCreation": 1_500_000},
    {"input": 3_300_000, "output": 950_000, "cacheRead": 40_000_000, "cacheCreation": 1_600_000},
    {"input": 4_600_000, "output": 1_600_000, "cacheRead": 80_000_000, "cacheCreation": 2_700_000},
]


def kv(key, value):
    return common_pb2.KeyValue(key=key, value=common_pb2.AnyValue(string_value=value))


def day_cost(tokens):
    return sum(tokens[t] / 1e6 * PRICE[t] for t in PRICE)


def build_request(now):
    base = now.replace(hour=12, minute=0, second=0, microsecond=0)
    # The last entry is today (a point at "now"); earlier entries are the six days before it.
    days = [base - timedelta(days=len(DAILY_TOKENS) - 1 - i) for i in range(len(DAILY_TOKENS))]
    # Points are cumulative, so each day's point is the running total through that day.
    # A zero baseline sits one day before the first day, so the first day's increase is measurable.
    baseline_ts = int((days[0] - timedelta(days=1)).timestamp() * 1e9)

    token_points = {t: [(baseline_ts, 0.0)] for t in PRICE}
    cost_points = [(baseline_ts, 0.0)]
    running = {t: 0.0 for t in PRICE}
    running_cost = 0.0

    for i, (day, tokens) in enumerate(zip(days, DAILY_TOKENS)):
        ts = int(now.timestamp() * 1e9) if i == len(days) - 1 else int(day.timestamp() * 1e9)
        for t in PRICE:
            running[t] += tokens[t]
            token_points[t].append((ts, running[t]))
        running_cost += day_cost(tokens)
        cost_points.append((ts, round(running_cost, 6)))
        print(f"{day.date()}  input={tokens['input']:>10,}  output={tokens['output']:>9,}  "
              f"cacheRead={tokens['cacheRead']:>11,}  cacheCreation={tokens['cacheCreation']:>10,}  "
              f"cost=${day_cost(tokens):6.2f}")

    token_metric = metrics_pb2.Metric(
        name="claude_code.token.usage",
        description="Synthetic token usage (demo)",
        unit="tokens",
        sum=metrics_pb2.Sum(
            aggregation_temporality=metrics_pb2.AGGREGATION_TEMPORALITY_CUMULATIVE,
            is_monotonic=True,
            data_points=[
                metrics_pb2.NumberDataPoint(
                    attributes=[kv("type", t), kv("model", MODEL)],
                    start_time_unix_nano=baseline_ts,
                    time_unix_nano=ts,
                    as_double=value,
                )
                for t, series in token_points.items()
                for ts, value in series
            ],
        ),
    )
    cost_metric = metrics_pb2.Metric(
        name="claude_code.cost.usage",
        description="Synthetic cost (demo)",
        unit="USD",
        sum=metrics_pb2.Sum(
            aggregation_temporality=metrics_pb2.AGGREGATION_TEMPORALITY_CUMULATIVE,
            is_monotonic=True,
            data_points=[
                metrics_pb2.NumberDataPoint(
                    attributes=[kv("model", MODEL)],
                    start_time_unix_nano=baseline_ts,
                    time_unix_nano=ts,
                    as_double=value,
                )
                for ts, value in cost_points
            ],
        ),
    )
    resource = resource_pb2.Resource(
        attributes=[kv("service.name", "claude-code"), kv("demo.synthetic", "true")]
    )
    scope = common_pb2.InstrumentationScope(name="claude-code-synthetic")
    return metrics_service_pb2.ExportMetricsServiceRequest(
        resource_metrics=[
            metrics_pb2.ResourceMetrics(
                resource=resource,
                scope_metrics=[metrics_pb2.ScopeMetrics(scope=scope, metrics=[token_metric, cost_metric])],
            )
        ]
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--endpoint", default="localhost:4317")
    args = parser.parse_args()

    request = build_request(datetime.now(timezone.utc))
    channel = grpc.insecure_channel(args.endpoint)
    stub = metrics_service_pb2_grpc.MetricsServiceStub(channel)
    stub.Export(request, timeout=10)
    channel.close()
    total = sum(day_cost(t) for t in DAILY_TOKENS)
    print(f"sent synthetic claude_code.* metrics to {args.endpoint}; 7-day total ${total:.2f}")


if __name__ == "__main__":
    main()
