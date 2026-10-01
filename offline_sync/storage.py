"""SQLite setup and transaction helpers for the sync demonstration."""

from contextlib import contextmanager
from pathlib import Path
import sqlite3
from typing import Iterator


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id TEXT PRIMARY KEY,
    data_json TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (version >= 1),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS changes (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    device_id TEXT NOT NULL,
    operation_id TEXT,
    base_version INTEGER NOT NULL,
    changed_fields_json TEXT NOT NULL,
    changes_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (document_id, version)
);

CREATE TABLE IF NOT EXISTS sync_operations (
    document_id TEXT NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    operation_id TEXT NOT NULL,
    request_hash TEXT NOT NULL,
    http_status INTEGER NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (document_id, operation_id)
);

CREATE INDEX IF NOT EXISTS changes_by_document_version
    ON changes(document_id, version);
"""


class SQLiteStore:
    """Owns a file-backed SQLite database and creates per-operation connections."""

    def __init__(self, db_path: str | Path) -> None:
        if str(db_path) == ":memory:":
            raise ValueError("Use a file-backed database so state persists between requests.")

        resolved_path = Path(db_path).expanduser().resolve()
        resolved_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = str(resolved_path)

        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            # Reserve the write transaction before reading the document version. This
            # makes conflict checking and the eventual update one atomic operation.
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

