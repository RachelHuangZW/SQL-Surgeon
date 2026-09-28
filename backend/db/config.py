import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class DBSecurityConfig:
    # Least-privilege DSN used for running user-submitted SQL (EXPLAIN ANALYZE).
    # None -> fall back to DATABASE_URL (still read-only transaction + timeout, but no role isolation).
    readonly_dsn: Optional[str]
    statement_timeout_ms: int
    lock_timeout_ms: int
    pool_min: int
    pool_max: int


def _int_env(name: str, default: int, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"{name} must be an integer, got {raw!r}")
    if value < minimum:
        raise ValueError(f"{name} must be >= {minimum}, got {value}")
    return value


def load_security_config() -> DBSecurityConfig:
    # Read lazily (not at import time) so load_dotenv() in the caller has already run.
    # statement_timeout minimum is 1ms: 0 would mean "no timeout" in PostgreSQL.
    return DBSecurityConfig(
        readonly_dsn=os.getenv("SURGEON_READONLY_DATABASE_URL") or None,
        statement_timeout_ms=_int_env("SURGEON_STATEMENT_TIMEOUT_MS", 5000, minimum=1),
        lock_timeout_ms=_int_env("SURGEON_LOCK_TIMEOUT_MS", 2000, minimum=1),
        pool_min=_int_env("SURGEON_DB_POOL_MIN", 1, minimum=0),
        pool_max=_int_env("SURGEON_DB_POOL_MAX", 10, minimum=1),
    )
