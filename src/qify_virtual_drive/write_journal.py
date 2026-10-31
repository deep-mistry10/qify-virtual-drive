"""Crash-safe local staging journal for QIFY Virtual Drive writes.

This module deliberately does not call Google Drive. Files are staged locally and
remain on disk until the caller confirms that the corresponding cloud operation
has succeeded and records that success with mark_uploaded().
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

WriteState = Literal["editing", "queued", "uploading", "failed", "review"]


class WriteJournalError(RuntimeError):
    """Raised when a staged write cannot be safely recorded or accessed."""


@dataclass(frozen=True, slots=True)
class PendingWrite:
    operation_id: str
    account_id: str | None
    file_id: str | None
    parent_id: str | None
    name: str
    local_path: Path
    state: WriteState
    last_error: str | None
    created_at: str
    updated_at: str


class WriteJournal:
    """Durable local write staging backed by SQLite and atomic local files.

    `account_id` and `file_id` are both None for a new file. Existing files keep
    their original account/file identity; the uploader must never silently
    switch ownership when retrying an existing-file update.
    """

    def __init__(self, root_directory: str | Path, database_path: str | Path) -> None:
        self.root_directory = Path(root_directory).resolve()
        self.root_directory.mkdir(parents=True, exist_ok=True)
        self.database_path = Path(database_path).resolve()
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        self._recover_interrupted_edits()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS pending_writes (
                    operation_id TEXT PRIMARY KEY,
                    account_id TEXT,
                    file_id TEXT,
                    parent_id TEXT,
                    name TEXT NOT NULL,
                    local_path TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL CHECK (
                        state IN ('editing', 'queued', 'uploading', 'failed', 'review')
                    ),
                    last_error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CHECK ((account_id IS NULL) = (file_id IS NULL))
                )
                """
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_pending_writes_state "
                "ON pending_writes(state, created_at)"
            )

    def _recover_interrupted_edits(self) -> None:
        """Preserve edits interrupted by a process exit and make them retryable."""
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pending_writes
                SET state = CASE
                        WHEN state = 'uploading' THEN 'review'
                        ELSE 'queued'
                    END,
                    last_error = CASE
                        WHEN state = 'uploading' THEN
                            'Upload outcome is unknown after interruption. Check Google Drive before retrying.'
                        ELSE COALESCE(last_error, 'Recovered after an interrupted editing session.')
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE state IN ('editing', 'uploading')
                """
            )

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("A non-empty file name is required.")
        if name in {".", ".."} or any(ch in name for ch in ("/", "\\", "\0")):
            raise ValueError("The file name must be a single safe path component.")
        if any(ord(ch) < 32 for ch in name):
            raise ValueError("Control characters are not allowed in file names.")
        return name

    def begin_write(
        self,
        name: str,
        *,
        account_id: str | None = None,
        file_id: str | None = None,
        parent_id: str | None = None,
    ) -> PendingWrite:
        """Create a durable local staging file for a new or existing cloud file."""
        safe_name = self._validate_name(name)
        if (account_id is None) != (file_id is None):
            raise ValueError("Existing files require both account_id and file_id.")
        operation_id = uuid.uuid4().hex
        local_path = (self.root_directory / f"{operation_id}.pending").resolve()
        if local_path.parent != self.root_directory:
            raise WriteJournalError("Staging path escaped the configured directory.")

        try:
            with local_path.open("xb") as staged:
                staged.flush()
                os.fsync(staged.fileno())
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO pending_writes (
                        operation_id, account_id, file_id, parent_id, name,
                        local_path, state
                    ) VALUES (?, ?, ?, ?, ?, ?, 'editing')
                    """,
                    (operation_id, account_id, file_id, parent_id, safe_name, str(local_path)),
                )
        except Exception:
            local_path.unlink(missing_ok=True)
            raise
        return self.get(operation_id)

    def write(self, operation_id: str, data: bytes, offset: int) -> int:
        """Write a byte range into a staged file and fsync it before returning."""
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if offset < 0:
            raise ValueError("offset cannot be negative")
        item = self.get(operation_id)
        if item.state != "editing":
            raise WriteJournalError(f"Cannot modify a write in state {item.state!r}.")
        with item.local_path.open("r+b") as staged:
            staged.seek(offset)
            staged.write(data)
            staged.flush()
            os.fsync(staged.fileno())
        with self._connect() as connection:
            connection.execute(
                "UPDATE pending_writes SET updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                (operation_id,),
            )
        return len(data)

    def resize(self, operation_id: str, size: int) -> None:
        """Set the staged file length, preserving existing bytes where possible."""
        if size < 0:
            raise ValueError("size cannot be negative")
        item = self.get(operation_id)
        if item.state != "editing":
            raise WriteJournalError(f"Cannot resize a write in state {item.state!r}.")
        with item.local_path.open("r+b") as staged:
            staged.truncate(size)
            staged.flush()
            os.fsync(staged.fileno())
        with self._connect() as connection:
            connection.execute(
                "UPDATE pending_writes SET updated_at=CURRENT_TIMESTAMP WHERE operation_id=?",
                (operation_id,),
            )

    def queue(self, operation_id: str) -> PendingWrite:
        """Mark a fully flushed staging file ready for upload."""
        item = self.get(operation_id)
        if item.state != "editing":
            if item.state == "queued":
                return item
            raise WriteJournalError(f"Cannot queue a write in state {item.state!r}.")
        if not item.local_path.is_file():
            raise WriteJournalError("Staged file is missing; refusing to queue it.")
        with item.local_path.open("r+b") as staged:
            staged.flush()
            os.fsync(staged.fileno())
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pending_writes
                SET state='queued', last_error=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE operation_id=? AND state='editing'
                """,
                (operation_id,),
            )
        return self.get(operation_id)

    def mark_uploading(self, operation_id: str) -> PendingWrite:
        item = self.get(operation_id)
        if item.state not in ("queued", "failed"):
            raise WriteJournalError(f"Cannot upload a write in state {item.state!r}.")
        with self._connect() as connection:
            connection.execute(
                """
                UPDATE pending_writes
                SET state='uploading', last_error=NULL, updated_at=CURRENT_TIMESTAMP
                WHERE operation_id=?
                """,
                (operation_id,),
            )
        return self.get(operation_id)

    def mark_failed(self, operation_id: str, error: str) -> PendingWrite:
        message = str(error).strip()[:2000] or "Unknown upload error"
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE pending_writes
                SET state='failed', last_error=?, updated_at=CURRENT_TIMESTAMP
                WHERE operation_id=?
                """,
                (message, operation_id),
            )
            if cursor.rowcount != 1:
                raise KeyError(operation_id)
        return self.get(operation_id)

    def mark_uploaded(self, operation_id: str) -> None:
        """Remove staging data only after the caller verifies cloud success."""
        item = self.get(operation_id)
        if item.state != "uploading":
            raise WriteJournalError("Only an uploading operation can be completed.")
        with self._connect() as connection:
            connection.execute("DELETE FROM pending_writes WHERE operation_id=?", (operation_id,))
        try:
            item.local_path.unlink(missing_ok=True)
        except OSError as exc:
            # The cloud write is already confirmed. Keep an orphaned local file
            # rather than recreating the queue item and risking a duplicate upload.
            raise WriteJournalError(
                "Cloud upload was recorded as complete, but the local staging file "
                "could not be removed. It is safe to delete it manually later."
            ) from exc

    def get(self, operation_id: str) -> PendingWrite:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_writes WHERE operation_id=?", (operation_id,)
            ).fetchone()
        if row is None:
            raise KeyError(operation_id)
        return self._from_row(row)

    def list_pending(self) -> list[PendingWrite]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_writes ORDER BY created_at, operation_id"
            ).fetchall()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: sqlite3.Row) -> PendingWrite:
        return PendingWrite(
            operation_id=row["operation_id"],
            account_id=row["account_id"],
            file_id=row["file_id"],
            parent_id=row["parent_id"],
            name=row["name"],
            local_path=Path(row["local_path"]),
            state=row["state"],
            last_error=row["last_error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
