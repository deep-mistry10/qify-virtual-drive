
"""Local account registry for QIFY Virtual Drive.

This module manages account metadata only. It does not store OAuth tokens
or perform Google Drive API requests.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


MAX_ACCOUNTS = 4


class AccountError(ValueError):
    """Raised when an account operation violates a registry rule."""


class AccountStatus(str, Enum):
    CONNECTED = "connected"
    NEEDS_REAUTH = "needs_reauth"
    DISCONNECTED = "disconnected"
    ERROR = "error"



@dataclass(frozen=True, slots=True)
class Account:
    """Non-secret metadata for one connected Google account."""

    account_id: str
    email: str
    status: AccountStatus = AccountStatus.CONNECTED
    google_subject_id: str | None = None

    def __post_init__(self) -> None:
        if not self.account_id.strip():
            raise AccountError("account_id cannot be empty")
        if not self.email.strip():
            raise AccountError("email cannot be empty")
        if self.google_subject_id is not None and not self.google_subject_id.strip():
            raise AccountError("google_subject_id cannot be blank")



class AccountRegistry:
    """In-memory account registry; persistence will be added separately."""

    def __init__(self, max_accounts: int = MAX_ACCOUNTS) -> None:
        if not 1 <= max_accounts <= MAX_ACCOUNTS:
            raise ValueError(f"max_accounts must be between 1 and {MAX_ACCOUNTS}")

        self._max_accounts = max_accounts
        self._accounts: dict[str, Account] = {}

    @property
    def max_accounts(self) -> int:
        return self._max_accounts

    def list_accounts(self) -> list[Account]:
        """Return accounts in insertion order."""
        return list(self._accounts.values())

    def get_account(self, account_id: str) -> Account | None:
        """Return an account by its stable local ID."""
        return self._accounts.get(account_id)

    def add_account(self, account: Account) -> None:
        """Add an account if its ID and email are not already registered."""
        if account.account_id in self._accounts:
            raise AccountError("This account ID is already registered")

        normalized_email = account.email.strip().casefold()
        if any(
            existing.email.strip().casefold() == normalized_email
            for existing in self._accounts.values()
        ):
            raise AccountError("This Google account is already registered")

        if len(self._accounts) >= self._max_accounts:
            raise AccountError(f"A maximum of {self._max_accounts} accounts is allowed")

        self._accounts[account.account_id] = account

    def update_status(self, account_id: str, status: AccountStatus) -> Account:
        """Update account status without changing its identity."""
        existing = self._accounts.get(account_id)
        if existing is None:
            raise AccountError("Account not found")

        updated = Account(
            account_id=existing.account_id,
            email=existing.email,
            status=status,
        )
        self._accounts[account_id] = updated
        return updated

    def remove_account(self, account_id: str) -> Account:
        """Remove local registry metadata only; never deletes cloud files."""
        try:
            return self._accounts.pop(account_id)
        except KeyError as exc:
            raise AccountError("Account not found") from exc
