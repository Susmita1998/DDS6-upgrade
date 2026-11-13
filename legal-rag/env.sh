# Claude Code OpenTelemetry settings for the local collector (stand-in for SigNoz on :4317).
# Usage: source env.sh   (then start `claude` from this shell)
export CLAUDE_CODE_ENABLE_TELEMETRY=1
export OTEL_METRICS_EXPORTER=otlp
export OTEL_LOGS_EXPORTER=otlp
export OTEL_EXPORTER_OTLP_PROTOCOL=grpc
export OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317
