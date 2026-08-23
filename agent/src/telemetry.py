"""OpenTelemetry setup plus the two wrap helpers the pipeline uses.

Local dev needs zero config: with OTEL_EXPORTER_OTLP_ENDPOINT unset, spans go to the console
(user decision: no dashboard container locally). Set the endpoint and everything switches to
OTLP/HTTP -- Azure Monitor or any collector -- with trace-correlated logs riding along. Escape
hatch if console spans get too chatty: OTEL_TRACES_EXPORTER=none.

Diagnostic model: a node exception is recorded on its span (thread_id/stage attributes) AND
logger.exception'd -- exceptions raised mid-SSE-stream on the AG-UI endpoint can never reach a
FastAPI exception handler (the response has already started), so the node wrapper is the
diagnostic of record.
"""

from __future__ import annotations

import inspect
import logging
import os
import re
from typing import Any, Awaitable, Callable

from langchain_core.runnables import RunnableConfig
from langgraph.errors import GraphBubbleUp
from opentelemetry import trace
from opentelemetry.trace import StatusCode

logger = logging.getLogger(__name__)

# ProxyTracer: resolves against whatever provider setup() installs later.
tracer = trace.get_tracer("ai-dev-workflow-agent")

# Redacts contiguous base64-ish runs (>=40 chars) from exec commands before they become span
# attributes -- git_ops.push_head embeds its credential-helper script as one such blob. 40 is
# deliberate: a classic 40-char ghp_ token is exactly at the threshold; never raise it.
_B64_RUN = re.compile(r"[A-Za-z0-9+/=_-]{40,}")
_COMMAND_ATTR_MAX = 300


def setup(app: Any) -> None:
    """Install the tracer provider + FastAPI auto-instrumentation. Called once from main.py."""
    if os.environ.get("OTEL_TRACES_EXPORTER", "").lower() == "none":
        return

    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    resource = Resource.create({"service.name": "ai-dev-workflow-agent"})
    provider = TracerProvider(resource=resource)

    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
    if endpoint:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))

        # Logs over OTLP too, trace-correlated (trace_id attached automatically). Console-only
        # mode skips this on purpose: stdout already carries the log lines.
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        logging.getLogger().addHandler(LoggingHandler(logger_provider=log_provider))
    else:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))

    trace.set_tracer_provider(provider)

    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    FastAPIInstrumentor.instrument_app(app)


def traced_node(
    name: str, fn: Callable[..., Awaitable[dict[str, Any]]]
) -> Callable[..., Awaitable[dict[str, Any]]]:
    """Wrap one graph-node callable in a span named node/<name>.

    Signature rules are load-bearing (both bit us or an audit caught them):
    - The wrapper is one fixed shape `(state, config: RunnableConfig)`. LangGraph injects config
      by parameter NAME and skips injection if the annotation is anything other than
      RunnableConfig/unannotated -- so the annotation below must stay exactly RunnableConfig.
    - No functools.wraps: it copies __wrapped__, which inspect.signature follows, so LangGraph
      would see the inner node's arity instead of the wrapper's (the _manifest_branch_node bug
      class). __name__ is copied manually for readable stack traces.
    - The inner call dispatches on the wrapped fn's own arity: nodes are (state, config) except
      the odd (state)-only pass-through.
    """
    wants_config = len(inspect.signature(fn).parameters) >= 2

    async def wrapper(state: Any, config: RunnableConfig) -> dict[str, Any]:
        thread_id = ((config or {}).get("configurable") or {}).get("thread_id", "")
        with tracer.start_as_current_span(
            f"node/{name}",
            attributes={"thread_id": thread_id, "node": name, "stage": name.rsplit("_", 1)[0]},
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            try:
                if wants_config:
                    return await fn(state, config)
                return await fn(state)
            except GraphBubbleUp:
                # interrupt() gate pauses and other control-flow bubbles -- not errors.
                raise
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(StatusCode.ERROR, str(exc))
                logger.exception("node %s failed (thread_id=%s)", name, thread_id)
                # Third run-terminal, and the only one with no node of its own: an unhandled
                # node exception aborts the whole graph invocation, so exit_finalize_node and
                # escalate_node never get to release this thread's ~20 Copilot sessions.
                # GraphBubbleUp is excluded above on purpose -- that is a gate interrupt, where
                # the stage's conversation continuity is exactly what we want to keep.
                # The SYNC forget, not close_thread_session: we are inside an except about to
                # re-raise. forget_thread_sessions_everywhere is a pure dict pop across both
                # providers, nothing here awaits a disconnect() (no such thing exists) -- staying
                # sync keeps this handler simple and correct even if the exception itself is a
                # dead/reaped sandbox.
                #
                # forget_thread_sessions_everywhere, not the single-provider forget_thread_sessions
                # (Ruling 4): this wrapper wraps EVERY node, intake included, so `state` here is not
                # always guaranteed to carry a pinned "provider" -- an exception inside intake_node
                # itself, on a thread's very first-ever intake, is exactly that case. The original
                # fix for this (state.get("provider") or await get_provider()) put a DB call inside
                # a handler that is about to re-raise -- found by the whole-branch review: if the
                # ORIGINATING exception was itself a DB failure (org_settings/session_store/
                # approvals share one connection pool), this handler's own get_provider() call
                # would fail too, replacing the real exception with a confusing new one -- exactly
                # the masking this handler exists to prevent. It also was not needed in the first
                # place: the only case `state` lacks "provider" is a thread's first-ever intake,
                # where no session could exist under EITHER provider yet -- precisely
                # forget_thread_sessions_everywhere's own no-DB, evict-both contract (see its
                # docstring in chat_model.py).
                if thread_id:
                    from .chat_model import forget_thread_sessions_everywhere

                    forget_thread_sessions_everywhere(thread_id)
                raise

    wrapper.__name__ = getattr(fn, "__name__", name)
    return wrapper


def traced_exec(fn: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    """Decorator for SandboxProvider.exec_in_sandbox implementations.

    rc is an attribute, never an ERROR status: non-zero exits are routine here (optional-file
    probes, `git commit` with nothing to commit, scanners that exit non-zero on findings) and
    flagging them would paint every healthy run red. traced_node owns ERROR status.
    """

    async def wrapper(self: Any, session_id: str, command: str) -> Any:
        with tracer.start_as_current_span(
            "sandbox/exec",
            attributes={
                "thread_id": session_id,
                "command": _B64_RUN.sub("<redacted>", command)[:_COMMAND_ATTR_MAX],
            },
        ) as span:
            result = await fn(self, session_id, command)
            span.set_attribute("rc", result.returncode)
            return result

    wrapper.__name__ = getattr(fn, "__name__", "exec_in_sandbox")
    return wrapper
