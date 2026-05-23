"""
Session context management using contextvars.
"""
import uuid
from contextvars import ContextVar
from typing import Optional

_session_id_var: ContextVar[str] = ContextVar("session_id", default="")


def get_session_id() -> str:
    """
    Get current session ID from context, auto-generate if empty.
    """
    session_id = _session_id_var.get()
    if not session_id:
        session_id = str(uuid.uuid4())
        _session_id_var.set(session_id)
    return session_id


def set_session_id(session_id: str) -> None:
    """Set session ID in current context."""
    _session_id_var.set(session_id)


class _SessionContext:
    """
    Context manager for setting session ID.
    Internal implementation used by logger.py's session() contextmanager.
    """
    def __init__(self, session_id: Optional[str] = None):
        self._session_id = session_id or str(uuid.uuid4())
        self._old_session_id: str = ""

    def __enter__(self) -> str:
        self._old_session_id = _session_id_var.get()
        set_session_id(self._session_id)
        return self._session_id

    def __exit__(self, *args) -> None:
        set_session_id(self._old_session_id)
