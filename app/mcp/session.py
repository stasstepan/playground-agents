"""
MCP session management (in-memory, dict-based).

For MVP we store session metadata (created_at, last_seen) per session_id.
Sessions are created when a client sends initialize and kept alive for
the duration of the HTTP connection sequence.
"""

import time
from typing import Any


class McpSession:
    """Represents a single MCP client session."""

    def __init__(self, session_id: str) -> None:
        self.session_id = session_id
        self.created_at: float = time.time()
        self.last_seen: float = time.time()
        self.initialized: bool = False
        self.meta: dict[str, Any] = {}

    def touch(self) -> None:
        self.last_seen = time.time()


# Module-level registry
_sessions: dict[str, McpSession] = {}


def create_session(session_id: str) -> McpSession:
    session = McpSession(session_id)
    _sessions[session_id] = session
    return session


def get_session(session_id: str) -> McpSession | None:
    return _sessions.get(session_id)


def remove_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


def cleanup_stale(max_age: float = 3600) -> int:
    """Remove sessions older than *max_age* seconds. Returns number removed."""
    now = time.time()
    stale = [sid for sid, s in _sessions.items() if now - s.last_seen > max_age]
    for sid in stale:
        del _sessions[sid]
    return len(stale)