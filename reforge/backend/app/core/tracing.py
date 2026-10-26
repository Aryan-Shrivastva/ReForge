"""OpenTelemetry tracing, exported to LangFuse (or any OTLP collector).

Every run produces a trace: one span per workflow pass, child spans per stage
transition, agent call, test run, MCP tool call, and A2A request.  Google ADK
emits its own OpenTelemetry spans (model calls, tool calls), which nest under
ReForge's agent spans because they share the global tracer provider.

Configuration is read from the environment only (never from ``Settings``, so
secrets stay out of run records):

* LangFuse - ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``, and
  ``LANGFUSE_HOST`` (default ``https://cloud.langfuse.com``).  Spans go to
  LangFuse's OTLP endpoint ``/api/public/otel/v1/traces``.  Optional
  ``LANGFUSE_PROJECT_ID`` lets the dashboard link straight to a trace.
* Any OTLP collector - ``OTEL_EXPORTER_OTLP_ENDPOINT`` (and the standard
  ``OTEL_EXPORTER_OTLP_HEADERS``).
* Neither set - spans are still created, so trace ids exist for correlation,
  but nothing is exported.

If the OpenTelemetry packages are not installed, every helper here is a no-op.
"""

from __future__ import annotations

import base64
import logging
import os
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

LOGGER = logging.getLogger(__name__)
SERVICE_NAME = "reforge"
_LOCK = threading.Lock()
_STATE: dict[str, str] = {}


def configure_tracing() -> str:
    """Install the global tracer provider once; return the active export target."""
    with _LOCK:
        if "target" in _STATE:
            return _STATE["target"]
        try:
            from opentelemetry import trace
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
        except ImportError:
            _STATE["target"] = "unavailable"
            return _STATE["target"]

        provider = TracerProvider(resource=Resource.create({"service.name": SERVICE_NAME}))
        target = "local"
        exporter = _langfuse_exporter() or _otlp_exporter()
        if exporter is not None:
            provider.add_span_processor(BatchSpanProcessor(exporter[1]))
            target = exporter[0]
        try:
            trace.set_tracer_provider(provider)
        except Exception as exc:  # A provider set by another library wins; ours is then unused.
            LOGGER.warning("Could not install the ReForge tracer provider: %s", exc)
        _STATE["target"] = target
        LOGGER.info("ReForge tracing configured (export: %s).", target)
        return target


def _langfuse_exporter() -> tuple[str, Any] | None:
    public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "").strip()
    secret_key = os.getenv("LANGFUSE_SECRET_KEY", "").strip()
    if not public_key or not secret_key:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        LOGGER.warning("LangFuse keys are set but opentelemetry-exporter-otlp-proto-http is not installed.")
        return None
    host = langfuse_host()
    token = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return "langfuse", OTLPSpanExporter(
        endpoint=f"{host}/api/public/otel/v1/traces",
        headers={"Authorization": f"Basic {token}"},
    )


def _otlp_exporter() -> tuple[str, Any] | None:
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT", "").strip():
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError:
        LOGGER.warning("OTEL_EXPORTER_OTLP_ENDPOINT is set but opentelemetry-exporter-otlp-proto-http is not installed.")
        return None
    return "otlp", OTLPSpanExporter()  # reads the standard OTEL_EXPORTER_OTLP_* variables


def langfuse_host() -> str:
    return os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com").strip().rstrip("/")


def trace_url(trace_id: str) -> str | None:
    """A browser link to the trace in LangFuse, when the project id is known."""
    project = os.getenv("LANGFUSE_PROJECT_ID", "").strip()
    if not trace_id or not project or not os.getenv("LANGFUSE_PUBLIC_KEY"):
        return None
    return f"{langfuse_host()}/project/{project}/traces/{trace_id}"


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a child span of the current one; a no-op when OpenTelemetry is missing."""
    try:
        from opentelemetry import trace
    except ImportError:
        yield None
        return
    tracer = trace.get_tracer(SERVICE_NAME)
    with tracer.start_as_current_span(name, attributes=_clean(attributes)) as current:
        try:
            yield current
        except Exception as exc:
            current.record_exception(exc)
            current.set_attribute("reforge.error", f"{type(exc).__name__}: {exc}"[:500])
            raise


def set_attributes(target: Any, **attributes: Any) -> None:
    if target is not None:
        for key, value in _clean(attributes).items():
            target.set_attribute(key, value)


def add_event(name: str, **attributes: Any) -> None:
    """Attach an event (e.g. a callback's intermediate output) to the current span."""
    try:
        from opentelemetry import trace
    except ImportError:
        return
    trace.get_current_span().add_event(name, attributes=_clean(attributes))


def current_trace_id() -> str:
    """Hex id of the active trace, or '' when tracing is unavailable."""
    try:
        from opentelemetry import trace
    except ImportError:
        return ""
    context = trace.get_current_span().get_span_context()
    return format(context.trace_id, "032x") if context.trace_id else ""


def _clean(attributes: dict[str, Any]) -> dict[str, Any]:
    """OpenTelemetry attributes must be primitives; drop None and stringify the rest."""
    cleaned: dict[str, Any] = {}
    for key, value in attributes.items():
        if value is None:
            continue
        key = key.replace("__", ".")
        if isinstance(value, (bool, int, float, str)):
            cleaned[key] = value if not isinstance(value, str) else value[:4_000]
        else:
            cleaned[key] = str(value)[:4_000]
    return cleaned
