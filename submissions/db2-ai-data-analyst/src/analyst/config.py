"""Runtime settings, read from environment variables (optionally loaded from .env)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
load_dotenv(PROJECT_ROOT / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    db2_host: str = field(default_factory=lambda: os.getenv("DB2_HOST", "localhost"))
    db2_port: int = field(default_factory=lambda: _int("DB2_PORT", 50000))
    db2_database: str = field(default_factory=lambda: os.getenv("DB2_DATABASE", "ANALYST"))
    db2_user: str = field(default_factory=lambda: os.getenv("DB2_USER", "db2inst1"))
    db2_password: str = field(default_factory=lambda: os.getenv("DB2_PASSWORD", ""))
    # Optional least-privilege user that only holds SELECT on the business schema.
    db2_ro_user: str = field(default_factory=lambda: os.getenv("DB2_RO_USER", ""))
    db2_ro_password: str = field(default_factory=lambda: os.getenv("DB2_RO_PASSWORD", ""))
    db2_security: str = field(default_factory=lambda: os.getenv("DB2_SECURITY", ""))

    data_schema: str = field(default_factory=lambda: os.getenv("DATA_SCHEMA", "RETAIL").upper())
    app_schema: str = field(default_factory=lambda: os.getenv("APP_SCHEMA", "ANALYST").upper())

    google_api_key: str = field(default_factory=lambda: os.getenv("GOOGLE_API_KEY", ""))
    gemini_model: str = field(default_factory=lambda: os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    embed_model: str = field(default_factory=lambda: os.getenv("GEMINI_EMBED_MODEL", "gemini-embedding-001"))
    embed_dim: int = field(default_factory=lambda: _int("EMBED_DIM", 768))

    max_query_cost: int = field(default_factory=lambda: _int("MAX_QUERY_COST", 500000))
    max_rows: int = field(default_factory=lambda: _int("MAX_ROWS", 500))
    max_retries: int = field(default_factory=lambda: _int("MAX_SQL_RETRIES", 2))
    memory_k: int = field(default_factory=lambda: _int("MEMORY_K", 4))
    app_port: int = field(default_factory=lambda: _int("APP_PORT", 8000))

    def conn_str(self, read_only: bool = False) -> str:
        user, pwd = self.db2_user, self.db2_password
        if read_only and self.db2_ro_user and self.db2_ro_password:
            user, pwd = self.db2_ro_user, self.db2_ro_password
        s = (
            f"DATABASE={self.db2_database};HOSTNAME={self.db2_host};PORT={self.db2_port};"
            f"PROTOCOL=TCPIP;UID={user};PWD={pwd};"
        )
        if self.db2_security.upper() == "SSL":
            s += "SECURITY=SSL;"
        return s

    @property
    def llm_enabled(self) -> bool:
        return bool(self.google_api_key)


settings = Settings()
