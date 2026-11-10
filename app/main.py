import json
import logging
import os
import random
import time
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, Request, Response
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

LOG_PATH = "/var/log/app/app.jsonl"
os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

class JsonFileHandler(logging.Handler):
    def emit(self, record):
        span = trace.get_current_span()
        ctx = span.get_span_context()
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "message": record.getMessage(),
            "service": "demo-api",
            "trace_id": format(ctx.trace_id, "032x") if ctx.is_valid else None,
            "span_id": format(ctx.span_id, "016x") if ctx.is_valid else None,
        }
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload) + "\n")

logger = logging.getLogger("demo-api")
logger.setLevel(logging.INFO)
logger.addHandler(JsonFileHandler())

resource = Resource.create({"service.name": "demo-api"})
provider = TracerProvider(resource=resource)
provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
trace.set_tracer_provider(provider)
tracer = trace.get_tracer(__name__)

REQUESTS = Counter("http_requests_total", "Total HTTP requests", ["method", "endpoint", "status"])
LATENCY = Histogram("http_request_duration_seconds", "HTTP request latency", ["endpoint"])
ERRORS = Counter("app_errors_total", "Application errors", ["endpoint"])

app = FastAPI(title="Observability Demo API")
FastAPIInstrumentor.instrument_app(app)

@app.middleware("http")
async def metrics_and_logging(request: Request, call_next):
    start = time.perf_counter()
    request_id = str(uuid.uuid4())
    try:
        response = await call_next(request)
        status = response.status_code
    except Exception:
        status = 500
        ERRORS.labels(request.url.path).inc()
        logger.exception("request_failed path=%s request_id=%s", request.url.path, request_id)
        raise
    finally:
        elapsed = time.perf_counter() - start
        endpoint = request.url.path
        LATENCY.labels(endpoint).observe(elapsed)
        REQUESTS.labels(request.method, endpoint, str(status)).inc()
        logger.info("request_complete method=%s path=%s status=%s duration_ms=%.2f request_id=%s",
                    request.method, endpoint, status, elapsed * 1000, request_id)
    response.headers["X-Request-ID"] = request_id
    return response

@app.get("/")
def root():
    logger.info("root_endpoint_called")
    return {"message": "Observability demo API", "try": ["/work", "/error", "/metrics"]}

@app.get("/work")
def work():
    with tracer.start_as_current_span("business-work") as span:
        delay = random.uniform(0.05, 0.6)
        span.set_attribute("work.delay_ms", delay * 1000)
        time.sleep(delay)
        logger.info("work_completed delay_ms=%.2f", delay * 1000)
        return {"status": "ok", "simulated_delay_ms": round(delay * 1000, 2)}

@app.get("/error")
def error():
    logger.error("simulated_application_error")
    raise ValueError("This is an intentional demo error")

@app.get("/health")
def health():
    return {"status": "healthy"}

@app.get("/metrics")
def metrics():
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
