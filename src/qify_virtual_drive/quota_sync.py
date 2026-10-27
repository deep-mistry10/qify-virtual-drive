
"""Fetch real Google Drive storage quota information."""

from __future__ import annotations

from datetime import datetime, timezone

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.storage_quota import QuotaStore, StorageQuota
from qify_virtual_drive.token_store import TokenStore, TokenStoreError


QUOTA_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]


class QuotaSyncError(RuntimeError):
    """Raised when real storage quota information cannot be retrieved."""


class QuotaSyncService:
    """Fetch and persist quota snapshots for registered Google accounts."""

    def __init__(
        self,
        database: AccountDatabase,
        quota_store: QuotaStore,
        token_store: TokenStore,
    ) -> None:
        self.database = database
        self.quota_store = quota_store
        self.token_store = token_store

    def sync_account(self, account_id: str) -> StorageQuota:
        account = self.database.get_account(account_id)

        if account is None:
            raise QuotaSyncError(f"Unknown account ID: {account_id}")

        if not account.google_subject_id:
            raise QuotaSyncError(
                f"Account {account.email} has no Google identity recorded."
            )

        try:
            token_json = self.token_store.load(account.google_subject_id)
        except TokenStoreError as exc:
            raise QuotaSyncError(
                f"Could not access credentials for {account.email}."
            ) from exc

        if not token_json:
            raise QuotaSyncError(
                f"No saved credentials for {account.email}. Reconnect the account."
            )

        try:
            import json

            credentials = Credentials.from_authorized_user_info(
                json.loads(token_json),
                scopes=QUOTA_SCOPES,
            )

            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                self.token_store.save(
                    account.google_subject_id,
                    credentials.to_json(),
                )

            if not credentials.valid:
                raise QuotaSyncError(
                    f"Credentials for {account.email} are invalid. "
                    "Reconnect the account."
                )

            service = build(
                "drive",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )

            response = (
                service.about()
                .get(
                    fields=(
                        "storageQuota(limit,usage,usageInDrive,"
                        "usageInDriveTrash)"
                    )
                )
                .execute()
            )

        except QuotaSyncError:
            raise
        except (ValueError, TypeError, GoogleAuthError, HttpError, OSError) as exc:
            raise QuotaSyncError(
                f"Could not retrieve quota for {account.email}: {exc}"
            ) from exc

        storage = response.get("storageQuota") or {}
        raw_limit = storage.get("limit")
        raw_usage = storage.get("usage")

        # Never interpret missing quota information as zero usage or free space.
        if raw_limit is None or raw_usage is None:
            raise QuotaSyncError(
                f"Google Drive did not report a usable storage limit and usage "
                f"for {account.email}. No quota snapshot was saved."
            )

        try:
            total_bytes = int(raw_limit)
            used_bytes = int(raw_usage)
        except (TypeError, ValueError) as exc:
            raise QuotaSyncError(
                f"Google Drive returned invalid quota values for {account.email}."
            ) from exc

        if total_bytes < 0 or used_bytes < 0:
            raise QuotaSyncError("Google Drive returned negative quota values.")

        quota = StorageQuota(
            account_id=account.account_id,
            total_bytes=total_bytes,
            used_bytes=used_bytes,
            checked_at=datetime.now(timezone.utc).isoformat(),
        )

        self.quota_store.save(quota)
        return quota
