"""Deliberately small demo service that emits correlated metrics, logs, and traces."""
import logging
import os
import random
import time
from contextlib import ExitStack
from contextlib import contextmanager

import psutil
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, TimeoutError as SQLAlchemyTimeoutError
from sqlalchemy.orm import sessionmaker
from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

SERVICE = "payment-api"
OTLP_ENDPOINT = os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://otel-collector:4318")
resource = Resource.create({"service.name": SERVICE, "deployment.environment": "incidentiq-demo"})
trace_provider = TracerProvider(resource=resource)
trace_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{OTLP_ENDPOINT}/v1/traces")))
trace.set_tracer_provider(trace_provider)
meter_provider = MeterProvider(resource=resource, metric_readers=[PeriodicExportingMetricReader(
    OTLPMetricExporter(endpoint=f"{OTLP_ENDPOINT}/v1/metrics"), export_interval_millis=5000)])
metrics.set_meter_provider(meter_provider)
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter(endpoint=f"{OTLP_ENDPOINT}/v1/logs")))
set_logger_provider(logger_provider)
logger = logging.getLogger("payment-api")
logger.setLevel(logging.INFO)
logger.addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))

meter = metrics.get_meter(SERVICE)
requests_total = meter.create_counter("payment_http_requests_total", unit="{request}")
errors_total = meter.create_counter("payment_http_errors_total", unit="{request}")
duration = meter.create_histogram("payment_http_request_duration_seconds", unit="s")
active = meter.create_up_down_counter("payment_active_requests", unit="{request}")
process_cpu = meter.create_observable_gauge("payment_process_cpu_utilization_ratio",
    callbacks=[lambda options: [metrics.Observation(min(psutil.cpu_percent(interval=None) / 100, 1), {"service_name": SERVICE})]], unit="1")
process_memory = meter.create_observable_gauge("payment_process_memory_rss_bytes",
    callbacks=[lambda options: [metrics.Observation(psutil.Process().memory_info().rss, {"service_name": SERVICE})]], unit="By")

engine = create_engine("sqlite:////tmp/payment-demo.db", pool_size=5, max_overflow=0, pool_timeout=2,
                       connect_args={"check_same_thread": False})
Session = sessionmaker(bind=engine)
with engine.begin() as connection:
    connection.execute(text("CREATE TABLE IF NOT EXISTS payments (id INTEGER PRIMARY KEY AUTOINCREMENT, amount_cents INTEGER NOT NULL, currency TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)"))

DEMO_FAILURES_ENABLED = os.getenv("DEMO_FAILURES_ENABLED", "false").lower() == "true"
app = FastAPI(title="IncidentIQ Payment API", version="1.0.0")
FastAPIInstrumentor.instrument_app(app, tracer_provider=trace_provider)
prom_requests = Counter("payment_http_requests_total", "Payment API HTTP requests", ["service_name", "method", "route", "status_code"])
prom_errors = Counter("payment_http_errors_total", "Payment API HTTP errors", ["service_name", "method", "route", "status_code"])
prom_duration = Histogram("payment_http_request_duration_seconds", "Payment API HTTP request duration", ["service_name", "method", "route"])
prom_active = Gauge("payment_active_requests", "Payment API active requests", ["service_name"])
prom_cpu = Gauge("payment_process_cpu_utilization_ratio", "Payment API process CPU utilization ratio", ["service_name"])
prom_memory = Gauge("payment_process_memory_rss_bytes", "Payment API process RSS memory", ["service_name"])
prom_db_pool = Gauge("payment_db_pool_utilization_ratio", "Payment API SQLite connection pool utilization", ["service_name"])


class PaymentCreate(BaseModel):
    amount_cents: int = Field(gt=0, le=10_000_000)
    currency: str = Field(default="USD", min_length=3, max_length=3, pattern="^[A-Z]{3}$")
    scenario: str | None = Field(default=None, pattern="^(latency|http_500|slow_query|db_pool_saturation)$")


@app.middleware("http")
async def measure(request: Request, call_next):
    started = time.perf_counter()
    route = request.url.path
    if route.startswith("/payments/"):
        route = "/payments/{payment_id}"
    attrs = {"service_name": SERVICE, "method": request.method, "route": route[:100]}
    active.add(1, attrs)
    prom_active.labels(SERVICE).inc()
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        errors_total.add(1, {**attrs, "status_code": "500"})
        prom_errors.labels(SERVICE, request.method, route, "500").inc()
        raise
    finally:
        active.add(-1, attrs)
        elapsed = time.perf_counter() - started
        duration.record(elapsed, attrs)
        prom_active.labels(SERVICE).dec()
        prom_duration.labels(SERVICE, request.method, route).observe(elapsed)
        prom_cpu.labels(SERVICE).set(psutil.cpu_percent(interval=None) / 100)
        prom_memory.labels(SERVICE).set(psutil.Process().memory_info().rss)
        pool = engine.pool
        prom_db_pool.labels(SERVICE).set(min(pool.checkedout() / max(1, pool.size() + pool._max_overflow), 1))
    requests_total.add(1, {**attrs, "status_code": str(response.status_code)})
    prom_requests.labels(SERVICE, request.method, route, str(response.status_code)).inc()
    if response.status_code >= 500:
        errors_total.add(1, {**attrs, "status_code": str(response.status_code)})
        prom_errors.labels(SERVICE, request.method, route, str(response.status_code)).inc()
    return response


def pool_observation(_options):
    pool = engine.pool
    capacity = max(1, pool.size() + pool._max_overflow)
    return [metrics.Observation(min(pool.checkedout() / capacity, 1), {"service_name": SERVICE})]


meter.create_observable_gauge("payment_db_pool_utilization_ratio", callbacks=[pool_observation], unit="1")


@app.get("/health")
def health():
    return {"status": "ok", "service": SERVICE}


@app.get("/metrics")
def prometheus_metrics():
    # OTel metrics also go to the collector; Prometheus scrapes this endpoint for immediate series.
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/payments")
def list_payments(limit: int = 20):
    with Session() as db:
        rows = db.execute(text("SELECT id, amount_cents, currency, created_at FROM payments ORDER BY id DESC LIMIT :limit"), {"limit": max(1, min(limit, 100))}).mappings().all()
    return {"items": [dict(row) for row in rows]}


@app.post("/payments", status_code=201)
def create_payment(payload: PaymentCreate):
    if payload.scenario and not DEMO_FAILURES_ENABLED:
        raise HTTPException(403, "Controlled failure scenarios are disabled")
    if payload.scenario == "latency":
        time.sleep(random.uniform(0.8, 1.8))
    if payload.scenario == "http_500":
        logger.error("controlled payment failure", extra={"scenario": payload.scenario})
        raise HTTPException(500, "Controlled demo failure")
    if payload.scenario == "slow_query":
        time.sleep(1.5)
    if payload.scenario == "db_pool_saturation":
        try:
            with ExitStack() as connections:
                for _ in range(engine.pool.size()):
                    connections.enter_context(engine.connect())
                with Session() as db:
                    db.execute(text("SELECT 1"))
        except (OperationalError, SQLAlchemyTimeoutError):
            logger.error("controlled database pool saturation", extra={"scenario": payload.scenario})
            raise HTTPException(503, "Controlled demo database pool saturation") from None
    try:
        with Session() as db:
            result = db.execute(text("INSERT INTO payments(amount_cents, currency) VALUES (:amount, :currency) RETURNING id, amount_cents, currency, created_at"),
                                {"amount": payload.amount_cents, "currency": payload.currency}).mappings().one()
            db.commit()
    except (OperationalError, SQLAlchemyTimeoutError):
        logger.exception("payment database operation failed")
        raise HTTPException(503, "Payment store temporarily unavailable") from None
    logger.info("payment created", extra={"payment_id": result["id"], "amount_cents": payload.amount_cents,
                                           "currency": payload.currency})
    return dict(result)


@app.get("/payments/{payment_id}")
def get_payment(payment_id: int):
    with Session() as db:
        row = db.execute(text("SELECT id, amount_cents, currency, created_at FROM payments WHERE id=:id"), {"id": payment_id}).mappings().one_or_none()
    if row is None:
        raise HTTPException(404, "Payment not found")
    return dict(row)
