
"""Storage quota tracking and automatic upload account selection."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class StorageQuota:
    """Storage quota reported by Google Drive for one account."""

    account_id: str
    total_bytes: int
    used_bytes: int
    checked_at: str

    @property
    def available_bytes(self) -> int:
        return max(0, self.total_bytes - self.used_bytes)

    @property
    def usage_percent(self) -> float:
        if self.total_bytes <= 0:
            return 100.0
        return min(100.0, self.used_bytes * 100 / self.total_bytes)


class QuotaStore:
    """Persist the latest observed storage quota for each account."""

    def __init__(self, database_path: str | Path) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS storage_quotas (
                    account_id TEXT PRIMARY KEY,
                    total_bytes INTEGER NOT NULL,
                    used_bytes INTEGER NOT NULL,
                    checked_at TEXT NOT NULL
                )
                """
            )

    def save(self, quota: StorageQuota) -> None:
        if not quota.account_id.strip():
            raise ValueError("account_id cannot be empty")
        if quota.total_bytes < 0 or quota.used_bytes < 0:
            raise ValueError("Quota values cannot be negative")
        if not quota.checked_at.strip():
            raise ValueError("checked_at cannot be empty")

        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO storage_quotas (
                    account_id, total_bytes, used_bytes, checked_at
                ) VALUES (?, ?, ?, ?)
                ON CONFLICT(account_id) DO UPDATE SET
                    total_bytes = excluded.total_bytes,
                    used_bytes = excluded.used_bytes,
                    checked_at = excluded.checked_at
                """,
                (
                    quota.account_id,
                    quota.total_bytes,
                    quota.used_bytes,
                    quota.checked_at,
                ),
            )

    def get(self, account_id: str) -> StorageQuota | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM storage_quotas WHERE account_id = ?",
                (account_id,),
            ).fetchone()

        if row is None:
            return None

        return StorageQuota(
            account_id=row["account_id"],
            total_bytes=row["total_bytes"],
            used_bytes=row["used_bytes"],
            checked_at=row["checked_at"],
        )

    def list_all(self) -> list[StorageQuota]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM storage_quotas ORDER BY account_id"
            ).fetchall()

        return [
            StorageQuota(
                account_id=row["account_id"],
                total_bytes=row["total_bytes"],
                used_bytes=row["used_bytes"],
                checked_at=row["checked_at"],
            )
            for row in rows
        ]


class UploadAllocator:
    """Select an account using the most recently observed quota."""

    def __init__(self, quota_store: QuotaStore) -> None:
        self.quota_store = quota_store

    def choose_account(
        self,
        file_size_bytes: int,
        connected_account_ids: list[str],
    ) -> str:
        if file_size_bytes < 0:
            raise ValueError("File size cannot be negative")

        if not connected_account_ids:
            raise ValueError("No connected Google accounts")

        # Unknown or stale quotas must not be treated as free capacity.
        eligible: list[StorageQuota] = []
        for account_id in set(connected_account_ids):
            quota = self.quota_store.get(account_id)
            if quota is None:
                continue

            if quota.available_bytes >= file_size_bytes:
                eligible.append(quota)

        if not eligible:
            raise ValueError(
                "No account has a known quota with sufficient available space. "
                "Refresh quotas before uploading."
            )

        # Prefer the account with the most remaining space.
        # Stable account-ID ordering breaks ties deterministically.
        eligible.sort(
            key=lambda quota: (-quota.available_bytes, quota.account_id)
        )
        return eligible[0].account_id
