"""
SQL middleware.

Wraps a psycopg2 cursor's execute() to automatically:
  - Log SQLLog entries for mutating statements (INSERT/UPDATE/DELETE/CREATE/ALTER/DROP/TRUNCATE)
  - Read-only statements (SELECT) are passed through without logging
"""
import time
from typing import Any, Optional, Protocol, Sequence

from utils.log.types import SQLLog

try:
    from opentelemetry import trace
    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False

_MUTATING_KEYWORDS = frozenset(
    {"INSERT", "UPDATE", "DELETE", "UPSERT", "CREATE", "ALTER", "DROP", "TRUNCATE", "REPLACE", "MERGE"}
)


def _is_mutating(query: str) -> bool:
    first = query.strip().split()[0].upper() if query.strip() else ""
    return first in _MUTATING_KEYWORDS


class SQLLogger(Protocol):
    def sql_log(self, ctx: Any, sql: SQLLog) -> None: ...


class CursorMiddleware:
    """
    Wraps a psycopg2 cursor and logs mutating SQL statements.

    Usage::

        with conn.cursor() as raw_cur:
            cur = CursorMiddleware(raw_cur, logger=log_instance)
            cur.execute(ctx, "INSERT INTO orders VALUES (%s)", (value,))
    """

    def __init__(self, cursor, logger: Optional[SQLLogger] = None):
        self._cursor = cursor
        self._logger = logger

    def execute(self, ctx: Any, query: str, params: Optional[Sequence] = None) -> None:
        if not _is_mutating(query):
            self._cursor.execute(query, params)
            return

        # Create a DB span for the mutating statement.
        _span_mgr = None
        if _OTEL_AVAILABLE:
            try:
                _tracer = trace.get_tracer("middleware.sql")
                _keyword = query.strip().split()[0].upper() if query.strip() else "SQL"
                _span_mgr = _tracer.start_as_current_span(f"SQL {_keyword}")
                _span_mgr.__enter__()
            except Exception:
                _span_mgr = None

        start = time.monotonic()
        try:
            self._cursor.execute(query, params)
        finally:
            latency_ms = int((time.monotonic() - start) * 1000)
            rows = 0
            try:
                rows = self._cursor.rowcount or 0
            except Exception:
                pass
            if _span_mgr is not None:
                try:
                    _span_mgr.__exit__(None, None, None)
                except Exception:
                    pass
            if self._logger is not None:
                try:
                    self._logger.sql_log(ctx, SQLLog(
                        statement=query,
                        rows_affected=rows,
                        latency_ms=latency_ms,
                    ))
                except Exception:
                    pass

    def __getattr__(self, name: str):
        """Proxy all other cursor attributes/methods transparently."""
        return getattr(self._cursor, name)
