"""Read-only Google Drive metadata synchronization."""

from __future__ import annotations

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.accounts import Account
from qify_virtual_drive.file_index import FileIndex, IndexedFile
from qify_virtual_drive.token_store import TokenStore, TokenStoreError

DRIVE_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]


class DriveSyncError(RuntimeError):
    """Raised when a Drive metadata synchronization fails."""


class DriveSyncService:
    """Synchronize metadata from one registered account into the local index."""

    def __init__(
        self,
        database: AccountDatabase,
        file_index: FileIndex,
        token_store: TokenStore,
    ) -> None:
        self.database = database
        self.file_index = file_index
        self.token_store = token_store

    def _get_credentials(self, account: Account) -> Credentials:
        if not account.google_subject_id:
            raise DriveSyncError(f"Account {account.email} has no Google identity recorded.")

        try:
            token_json = self.token_store.load(account.google_subject_id)
        except TokenStoreError as exc:
            raise DriveSyncError(f"Could not access credentials for {account.email}.") from exc

        if token_json is None:
            raise DriveSyncError(
                f"No saved credentials for {account.email}. Reconnect this account."
            )

        try:
            credentials = Credentials.from_authorized_user_info(
                __import__("json").loads(token_json),
                scopes=DRIVE_SCOPES,
            )

            if credentials.expired and credentials.refresh_token:
                credentials.refresh(Request())
                self.token_store.save(
                    account.google_subject_id,
                    credentials.to_json(),
                )

            if not credentials.valid:
                raise DriveSyncError(
                    f"Credentials for {account.email} are expired or invalid. "
                    "Reconnect the account."
                )

            return credentials
        except (ValueError, TypeError, GoogleAuthError, OSError) as exc:
            raise DriveSyncError(f"Could not prepare credentials for {account.email}.") from exc

    def sync_account(self, account_id: str) -> int:
        """Fetch all accessible file metadata pages for one account.

        Returns the number of indexed file records processed.
        """
        account = self.database.get_account(account_id)
        if account is None:
            raise DriveSyncError(f"Unknown account ID: {account_id}")

        credentials = self._get_credentials(account)

        try:
            service = build(
                "drive",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )

            page_token = None
            processed = 0

            while True:
                response = (
                    service.files()
                    .list(
                        pageSize=100,
                        pageToken=page_token,
                        orderBy="name",
                        fields=(
                            "nextPageToken,"
                            "files(id,name,mimeType,size,modifiedTime,parents,trashed)"
                        ),
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                    )
                    .execute()
                )

                for item in response.get("files", []):
                    file_id = item.get("id")
                    name = item.get("name")
                    mime_type = item.get("mimeType")

                    if not all(
                        isinstance(value, str) and value for value in (file_id, name, mime_type)
                    ):
                        continue

                    raw_size = item.get("size")
                    try:
                        size_bytes = int(raw_size) if raw_size is not None else None
                    except (TypeError, ValueError):
                        size_bytes = None

                    parents = item.get("parents") or []
                    parent_id = parents[0] if parents else None

                    self.file_index.upsert(
                        IndexedFile(
                            account_id=account.account_id,
                            file_id=file_id,
                            name=name,
                            mime_type=mime_type,
                            parent_id=parent_id,
                            size_bytes=size_bytes,
                            modified_time=item.get("modifiedTime"),
                            trashed=bool(item.get("trashed", False)),
                        )
                    )
                    processed += 1

                page_token = response.get("nextPageToken")
                if not page_token:
                    break

            return processed

        except (HttpError, GoogleAuthError, OSError, ValueError) as exc:
            raise DriveSyncError(
                f"Google Drive synchronization failed for {account.email}: {exc}"
            ) from exc
