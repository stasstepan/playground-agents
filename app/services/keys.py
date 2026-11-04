"""
Shared key generation logic.

Used by REST API, MCP handlers, and admin panel.
Centralised to avoid duplication.
"""

import hashlib
import secrets
import string

ALPHABET = string.ascii_letters + string.digits


def generate_raw_key() -> str:
    """Generate a pgk_ prefix + 40 random alphanumeric characters."""
    random_part = "".join(secrets.choice(ALPHABET) for _ in range(40))
    return f"pgk_{random_part}"


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


def key_prefix(raw_key: str) -> str:
    """Return first 8 characters of the raw key for display."""
    return raw_key[:8]