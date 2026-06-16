#!/usr/bin/env python3
"""Main API application"""

import logging
from fastapi import FastAPI, Request
from opentelemetry import trace
from starlette.middleware.base import BaseHTTPMiddleware
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import ConsoleSpanExporter, BatchSpanProcessor, SimpleSpanProcessor
from opentelemetry.sdk.trace.sampling import TraceIdRatioBased, ParentBased
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from app.routers.error_handlers import install_error_handlers
from app.routers.facility import facility
from app.routers.status import status
from app.routers.account import account
from app.routers.compute import compute
from app.routers.filesystem import filesystem
from app.routers.task import task

from . import config
from .request_context import set_api_url_base, set_auth_headers, _api_url_base

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)

# ------------------------------------------------------------------
# OpenTelemetry Tracing Configuration
# ------------------------------------------------------------------
if config.OPENTELEMETRY_ENABLED:
    resource = Resource.create({"service.name": "iri-facility-api", "service.version": config.API_VERSION, "service.endpoint": config.API_URL_ROOT})

    samplerate = "1.0" if config.OPENTELEMETRY_DEBUG else config.OTEL_SAMPLE_RATE
    provider = TracerProvider(resource=resource, sampler=ParentBased(TraceIdRatioBased(samplerate)))
    trace.set_tracer_provider(provider)

    if config.OTLP_ENDPOINT:
        exporter = OTLPSpanExporter(endpoint=config.OTLP_ENDPOINT, insecure=True)
        span_processor = BatchSpanProcessor(exporter)
    else:
        exporter = ConsoleSpanExporter()
        span_processor = SimpleSpanProcessor(exporter)
    provider.add_span_processor(span_processor)
    tracer = trace.get_tracer(__name__)
# ------------------------------------------------------------------

app = FastAPI(servers=[{"url": config.API_URL_ROOT}], **config.API_CONFIG)


class _ExternalRequestContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        token = _api_url_base.set(None)
        try:
            set_api_url_base(request)
            set_auth_headers(request)
            logging.getLogger(__name__).info(
                "Incoming request headers: %s",
                dict(request.headers),
            )
            return await call_next(request)
        finally:
            _api_url_base.reset(token)


app.add_middleware(_ExternalRequestContextMiddleware)

if config.OPENTELEMETRY_ENABLED:
    FastAPIInstrumentor.instrument_app(app)

install_error_handlers(app)

api_prefix = f"{config.API_PREFIX}{config.API_URL}"

# Attach routers under the prefix
app.include_router(facility.router, prefix=api_prefix)
app.include_router(status.router, prefix=api_prefix)
app.include_router(account.router, prefix=api_prefix)
app.include_router(compute.router, prefix=api_prefix)
app.include_router(filesystem.router, prefix=api_prefix)
app.include_router(task.router, prefix=api_prefix)

logging.getLogger().info(f"API path: {api_prefix}")
