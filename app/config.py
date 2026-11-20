"""Application configuration."""
from __future__ import annotations

import os


class Settings:
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://research:research@localhost:5432/researchdb",
    )
    # Maximum number of entries accepted in one transaction Bundle.
    MAX_BUNDLE_ENTRIES: int = int(os.getenv("MAX_BUNDLE_ENTRIES", "20"))
    # A pending idempotency record older than this is treated as a crashed
    # request and may be re-driven (seconds).
    IDEMPOTENCY_STALE_SECONDS: int = int(
        os.getenv("IDEMPOTENCY_STALE_SECONDS", "60")
    )
    # SQLite (shared file, multiple API instances) serializes writers with a
    # file lock. Wait this long for the lock before surfacing a transient error.
    SQLITE_BUSY_TIMEOUT_SECONDS: int = int(
        os.getenv("SQLITE_BUSY_TIMEOUT_SECONDS", "30")
    )
    # How many times a bundle is re-validated and retried when a concurrent
    # writer invalidated its read snapshot (SQLite BUSY_SNAPSHOT / lock loss).
    DB_BUSY_RETRIES: int = int(os.getenv("DB_BUSY_RETRIES", "8"))
    DB_BUSY_BACKOFF_SECONDS: float = float(
        os.getenv("DB_BUSY_BACKOFF_SECONDS", "0.02")
    )


settings = Settings()
