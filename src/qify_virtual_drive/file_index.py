"""Persistent unified index for files owned by connected Google accounts."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class IndexedFile:
    """Metadata for one cloud file; identity is account ID plus Drive file ID."""

    account_id: str
    file_id: str
    name: str
    mime_type: str
    parent_id: str | None = None
    size_bytes: int | None = None
    modified_time: str | None = None
    trashed: bool = False


class FileIndex:
    """Local metadata index. This class never downloads, edits, or deletes cloud files."""

    def __init__(self, database_path: str | Path | None = None) -> None:
        if database_path is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise RuntimeError("LOCALAPPDATA is unavailable.")

            directory = Path(local_app_data) / "QIFY" / "VirtualDrive"
            directory.mkdir(parents=True, exist_ok=True)
            database_path = directory / "accounts.sqlite3"
        else:
            database_path = Path(database_path)
            database_path.parent.mkdir(parents=True, exist_ok=True)

        self.database_path = Path(database_path)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS indexed_files (
                    account_id TEXT NOT NULL,
                    file_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    parent_id TEXT,
                    size_bytes INTEGER,
                    modified_time TEXT,
                    trashed INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (account_id, file_id)
                )
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_indexed_files_parent
                ON indexed_files(account_id, parent_id)
                """
            )
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_indexed_files_name
                ON indexed_files(name COLLATE NOCASE)
                """
            )

    @staticmethod
    def _validate(item: IndexedFile) -> None:
        if not item.account_id.strip():
            raise ValueError("account_id cannot be empty")
        if not item.file_id.strip():
            raise ValueError("file_id cannot be empty")
        if not item.name:
            raise ValueError("name cannot be empty")
        if item.size_bytes is not None and item.size_bytes < 0:
            raise ValueError("size_bytes cannot be negative")

    def upsert(self, item: IndexedFile) -> None:
        """Insert or refresh one file without changing its owning account."""
        self._validate(item)

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO indexed_files (
                    account_id, file_id, name, mime_type, parent_id,
                    size_bytes, modified_time, trashed
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, file_id) DO UPDATE SET
                    name = excluded.name,
                    mime_type = excluded.mime_type,
                    parent_id = excluded.parent_id,
                    size_bytes = excluded.size_bytes,
                    modified_time = excluded.modified_time,
                    trashed = excluded.trashed
                """,
                (
                    item.account_id,
                    item.file_id,
                    item.name,
                    item.mime_type,
                    item.parent_id,
                    item.size_bytes,
                    item.modified_time,
                    int(item.trashed),
                ),
            )

    def get(self, account_id: str, file_id: str) -> IndexedFile | None:
        """Retrieve a file using its complete cloud identity."""
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM indexed_files
                WHERE account_id = ? AND file_id = ?
                """,
                (account_id, file_id),
            ).fetchone()

        return self._from_row(row) if row else None

    def list_files(
        self,
        account_id: str | None = None,
        parent_id: str | None = None,
        include_trashed: bool = False,
    ) -> list[IndexedFile]:
        """List indexed files, optionally scoped to one account and parent."""
        clauses = []
        values: list[object] = []

        if account_id is not None:
            clauses.append("account_id = ?")
            values.append(account_id)

        if parent_id is not None:
            clauses.append("parent_id = ?")
            values.append(parent_id)

        if not include_trashed:
            clauses.append("trashed = 0")

        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""

        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM indexed_files
                {where}
                ORDER BY account_id, name COLLATE NOCASE, file_id
                """,
                values,
            ).fetchall()

        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> IndexedFile:
        return IndexedFile(
            account_id=row["account_id"],
            file_id=row["file_id"],
            name=row["name"],
            mime_type=row["mime_type"],
            parent_id=row["parent_id"],
            size_bytes=row["size_bytes"],
            modified_time=row["modified_time"],
            trashed=bool(row["trashed"]),
        )
