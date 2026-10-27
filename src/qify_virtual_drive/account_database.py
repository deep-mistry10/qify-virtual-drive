"""Persistent, non-secret account metadata for QIFY Virtual Drive."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from qify_virtual_drive.accounts import (
    MAX_ACCOUNTS,
    Account,
    AccountError,
    AccountStatus,
)


class AccountDatabase:
    """SQLite repository for account metadata, not OAuth tokens."""

    def __init__(
        self,
        database_path: str | Path | None = None,
        max_accounts: int = MAX_ACCOUNTS,
    ) -> None:
        if not 1 <= max_accounts <= MAX_ACCOUNTS:
            raise ValueError(f"max_accounts must be between 1 and {MAX_ACCOUNTS}")

        if database_path is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise RuntimeError("LOCALAPPDATA is unavailable on this system.")

            directory = Path(local_app_data) / "QIFY" / "VirtualDrive"
            directory.mkdir(parents=True, exist_ok=True)
            database_path = directory / "accounts.sqlite3"
        else:
            database_path = Path(database_path)
            database_path.parent.mkdir(parents=True, exist_ok=True)

        self.database_path = Path(database_path)
        self.max_accounts = max_accounts
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.database_path,
            timeout=10,
            isolation_level="DEFERRED",
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS accounts (
                    account_id TEXT PRIMARY KEY,
                    email TEXT NOT NULL,
                    normalized_email TEXT NOT NULL UNIQUE,
                    google_subject_id TEXT UNIQUE,
                    status TEXT NOT NULL
                )
                """
            )

    @staticmethod
    def _to_account(row: sqlite3.Row) -> Account:
        return Account(
            account_id=row["account_id"],
            email=row["email"],
            status=AccountStatus(row["status"]),
            google_subject_id=row["google_subject_id"],
        )

    def list_accounts(self) -> list[Account]:
        """Load accounts in a stable order."""
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM accounts ORDER BY rowid").fetchall()
        return [self._to_account(row) for row in rows]

    def get_account(self, account_id: str) -> Account | None:
        """Find an account by local ID."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()
        return self._to_account(row) if row else None

    def add_account(self, account: Account) -> None:
        """Add an account while enforcing capacity and identity uniqueness."""
        normalized_email = account.email.strip().casefold()

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")

                count = connection.execute("SELECT COUNT(*) FROM accounts").fetchone()[0]

                if count >= self.max_accounts:
                    raise AccountError(f"A maximum of {self.max_accounts} accounts is allowed")

                connection.execute(
                    """
                    INSERT INTO accounts (
                        account_id,
                        email,
                        normalized_email,
                        google_subject_id,
                        status
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        account.account_id,
                        account.email.strip(),
                        normalized_email,
                        account.google_subject_id,
                        account.status.value,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise AccountError(
                "Account ID, email, or Google identity is already registered"
            ) from exc

    def update_status(
        self,
        account_id: str,
        status: AccountStatus,
    ) -> Account:
        """Update connection status without changing account identity."""
        with self._connect() as connection:
            cursor = connection.execute(
                "UPDATE accounts SET status = ? WHERE account_id = ?",
                (status.value, account_id),
            )

            if cursor.rowcount == 0:
                raise AccountError("Account not found")

            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()

        return self._to_account(row)

    def remove_account(self, account_id: str) -> Account:
        """Remove local metadata only; never calls the Google Drive API."""
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()

            if row is None:
                raise AccountError("Account not found")

            account = self._to_account(row)
            connection.execute(
                "DELETE FROM accounts WHERE account_id = ?",
                (account_id,),
            )

        return account
