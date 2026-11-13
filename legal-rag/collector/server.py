"""Stand-in OpenTelemetry collector for local SigNoz-style testing.

Receives OTLP/gRPC traces, logs and metrics on :4317, stores them in SQLite, and
serves the dashboard and query API on :8080.

Run from the legal-rag directory:
    python -m collector.server
"""

import json
import logging
import os
from concurrent import futures

import grpc
import uvicorn
from opentelemetry.proto.collector.logs.v1 import logs_service_pb2, logs_service_pb2_grpc
from opentelemetry.proto.collector.metrics.v1 import metrics_service_pb2, metrics_service_pb2_grpc
from opentelemetry.proto.collector.trace.v1 import trace_service_pb2, trace_service_pb2_grpc

from collector.api import app as api_app
from collector.db import get_db

log = logging.getLogger("collector")

SPAN_KIND = {0: "UNSPECIFIED", 1: "INTERNAL", 2: "SERVER", 3: "CLIENT", 4: "PRODUCER", 5: "CONSUMER"}
STATUS = {0: "UNSET", 1: "OK", 2: "ERROR"}
AGG_CUMULATIVE = 2


def any_value(v):
    kind = v.WhichOneof("value")
    if kind is None:
        return None
    if kind == "kvlist_value":
        return {kv.key: any_value(kv.value) for kv in v.kvlist_value.values}
    if kind == "array_value":
        return [any_value(x) for x in v.array_value.values]
    if kind == "bytes_value":
        return v.bytes_value.hex()
    return getattr(v, kind)


def attrs(kvs):
    return {kv.key: any_value(kv.value) for kv in kvs}


class TraceService(trace_service_pb2_grpc.TraceServiceServicer):
    def __init__(self, db):
        self.db = db

    def Export(self, request, context):
        rows = []
        for rs in request.resource_spans:
            resource = attrs(rs.resource.attributes)
            service = resource.get("service.name", "unknown")
            for ss in rs.scope_spans:
                for s in ss.spans:
                    rows.append(
                        (
                            s.trace_id.hex(),
                            s.span_id.hex(),
                            s.parent_span_id.hex(),
                            s.name,
                            service,
                            SPAN_KIND.get(s.kind, "UNSPECIFIED"),
                            s.start_time_unix_nano,
                            s.end_time_unix_nano,
                            STATUS.get(s.status.code, "UNSET"),
                            json.dumps(attrs(s.attributes), sort_keys=True),
                            json.dumps(resource, sort_keys=True),
                        )
                    )
        self.db.insert_spans(rows)
        log.info("received %d spans", len(rows))
        return trace_service_pb2.ExportTraceServiceResponse()


class LogsService(logs_service_pb2_grpc.LogsServiceServicer):
    def __init__(self, db):
        self.db = db

    def Export(self, request, context):
        rows = []
        for rl in request.resource_logs:
            service = attrs(rl.resource.attributes).get("service.name", "unknown")
            for sl in rl.scope_logs:
                for lr in sl.log_records:
                    rows.append(
                        (
                            lr.time_unix_nano or lr.observed_time_unix_nano,
                            service,
                            lr.severity_text or "INFO",
                            str(any_value(lr.body)),
                            lr.trace_id.hex(),
                            lr.span_id.hex(),
                            json.dumps(attrs(lr.attributes), sort_keys=True),
                        )
                    )
        self.db.insert_logs(rows)
        log.info("received %d log records", len(rows))
        return logs_service_pb2.ExportLogsServiceResponse()


class MetricsService(metrics_service_pb2_grpc.MetricsServiceServicer):
    def __init__(self, db):
        self.db = db

    def Export(self, request, context):
        rows = []
        for rm in request.resource_metrics:
            resource = attrs(rm.resource.attributes)
            service = resource.get("service.name", "unknown")
            resource_json = json.dumps(resource, sort_keys=True)
            for sm in rm.scope_metrics:
                for m in sm.metrics:
                    kind = m.WhichOneof("data")
                    if kind == "sum":
                        monotonic = int(m.sum.is_monotonic)
                        for dp in m.sum.data_points:
                            rows.append(self._number_row(dp, service, m.name, m.unit, "sum", monotonic, resource_json))
                    elif kind == "gauge":
                        for dp in m.gauge.data_points:
                            rows.append(self._number_row(dp, service, m.name, m.unit, "gauge", 0, resource_json))
                    elif kind == "histogram":
                        for dp in m.histogram.data_points:
                            labels = json.dumps(attrs(dp.attributes), sort_keys=True)
                            ts = dp.time_unix_nano
                            rows.append((ts, service, f"{m.name}_count", m.unit, "histogram", 1, dp.count, labels, resource_json))
                            rows.append((ts, service, f"{m.name}_sum", m.unit, "histogram", 1, dp.sum, labels, resource_json))
        self.db.insert_metrics([r for r in rows if r])
        log.info("received %d metric points", len(rows))
        return metrics_service_pb2.ExportMetricsServiceResponse()

    @staticmethod
    def _number_row(dp, service, name, unit, mtype, monotonic, resource_json):
        kind = dp.WhichOneof("value")
        if kind is None:
            return None
        labels = json.dumps(attrs(dp.attributes), sort_keys=True)
        return (dp.time_unix_nano, service, name, unit, mtype, monotonic, getattr(dp, kind), labels, resource_json)


def serve(port, db):
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    trace_service_pb2_grpc.add_TraceServiceServicer_to_server(TraceService(db), server)
    logs_service_pb2_grpc.add_LogsServiceServicer_to_server(LogsService(db), server)
    metrics_service_pb2_grpc.add_MetricsServiceServicer_to_server(MetricsService(db), server)
    server.add_insecure_port(f"[::]:{port}")
    server.start()
    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    grpc_port = int(os.getenv("COLLECTOR_GRPC_PORT", "4317"))
    http_port = int(os.getenv("COLLECTOR_HTTP_PORT", "8080"))
    grpc_server = serve(grpc_port, get_db())
    log.info("OTLP/gRPC receiver on :%d, dashboard on http://localhost:%d", grpc_port, http_port)
    try:
        uvicorn.run(api_app, host="127.0.0.1", port=http_port, log_level="warning")
    finally:
        grpc_server.stop(grace=2)
