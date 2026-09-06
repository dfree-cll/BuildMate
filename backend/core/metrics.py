"""Low-cardinality Prometheus metrics for API, tasks, RAG and Revit."""

try:
    from prometheus_client import Counter, Histogram, Gauge
except ImportError:  # Developer environments created before the metrics dependency was added.
    class _NoopMetric:
        def labels(self, *args, **kwargs):
            return self

        def inc(self, amount=1):
            return None

        def observe(self, value):
            return None

        def set(self, value):
            return None

    def Counter(*args, **kwargs):
        return _NoopMetric()

    def Histogram(*args, **kwargs):
        return _NoopMetric()

    def Gauge(*args, **kwargs):
        return _NoopMetric()

API_REQUESTS = Counter(
    "buildmate_api_requests_total", "API requests", ["method", "route", "status"]
)
API_LATENCY = Histogram(
    "buildmate_api_request_duration_seconds", "API request latency", ["method", "route"]
)
TASK_RESULTS = Counter(
    "buildmate_task_results_total", "Durable workflow results", ["workflow", "status", "failure_type"]
)
TASK_STEP_LATENCY = Histogram(
    "buildmate_task_step_duration_seconds", "Workflow step latency", ["workflow", "step"]
)
RAG_REQUESTS = Counter(
    "buildmate_rag_requests_total", "Knowledge retrievals", ["scope", "status"]
)
RAG_HITS = Histogram(
    "buildmate_rag_hits", "Knowledge hits returned", buckets=(0, 1, 2, 4, 8, 16, 32)
)
RAG_ABSTAINS = Counter(
    "buildmate_rag_abstains_total", "Grounded answers that abstained", ["scope"]
)
REVIT_BRIDGE_UP = Gauge(
    "buildmate_revit_bridge_up", "Whether the local Revit Bridge health endpoint is available"
)
