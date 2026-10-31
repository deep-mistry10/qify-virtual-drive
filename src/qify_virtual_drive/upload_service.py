"""Upload local files to Google Drive and index their ownership."""

from __future__ import annotations

import json
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.file_index import FileIndex, IndexedFile
from qify_virtual_drive.storage_quota import QuotaStore
from qify_virtual_drive.token_store import TokenStore, TokenStoreError
from qify_virtual_drive.upload_planner import (
    UploadPlanner,
    UploadPlanningError,
)

UPLOAD_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


class UploadError(RuntimeError):
    """Raised when a Google Drive upload fails."""


class UploadService:
    """Upload files using connected accounts and record file ownership."""

    def __init__(
        self,
        database: AccountDatabase,
        file_index: FileIndex,
        quota_store: QuotaStore,
        token_store: TokenStore,
    ) -> None:
        self.database = database
        self.file_index = file_index
        self.token_store = token_store
        self.planner = UploadPlanner(database, quota_store)

    def _credentials_for(self, account_id: str) -> Credentials:
        account = self.database.get_account(account_id)

        if account is None:
            raise UploadError(f"Unknown account: {account_id}")

        if getattr(account.status, "value", account.status) != "connected":
            raise UploadError(f"Google account {account.email} is not connected.")

        if not account.google_subject_id:
            raise UploadError(f"Google account {account.email} has no saved Google identity.")

        try:
            token_json = self.token_store.load(account.google_subject_id)
        except TokenStoreError as exc:
            raise UploadError(f"Could not access credentials for {account.email}.") from exc

        if not token_json:
            raise UploadError(f"No saved credentials for {account.email}. Reauthorize the account.")

        try:
            token_data = json.loads(token_json)
            credentials = Credentials.from_authorized_user_info(
                token_data,
                scopes=UPLOAD_SCOPES,
            )

            if credentials.expired:
                if not credentials.refresh_token:
                    raise UploadError(f"Credentials for {account.email} need reauthorization.")

                credentials.refresh(Request())
                self.token_store.save(
                    account.google_subject_id,
                    credentials.to_json(),
                )

            if not credentials.valid:
                raise UploadError(
                    f"Credentials for {account.email} are invalid. Reauthorize the account."
                )

            return credentials

        except UploadError:
            raise
        except (
            ValueError,
            TypeError,
            GoogleAuthError,
            OSError,
            TokenStoreError,
        ) as exc:
            raise UploadError(f"Could not prepare credentials for {account.email}: {exc}") from exc

    def upload_file(
        self,
        local_path: str | Path,
        parent_id: str | None = None,
    ) -> IndexedFile:
        """Choose an account, upload a file, and index the returned metadata."""
        path = Path(local_path)

        if not path.exists() or not path.is_file():
            raise UploadError(f"Local file does not exist: {path}")

        if path.is_symlink():
            raise UploadError("Symbolic links are not accepted as upload files.")

        try:
            file_size = path.stat().st_size
        except OSError as exc:
            raise UploadError(f"Could not read local file metadata: {exc}") from exc

        try:
            account_id = self.planner.choose_account(file_size)
        except UploadPlanningError as exc:
            raise UploadError(f"Could not choose an upload account: {exc}") from exc

        account = self.database.get_account(account_id)
        if account is None:
            raise UploadError(f"Selected account no longer exists: {account_id}")

        credentials = self._credentials_for(account_id)

        metadata: dict[str, str] = {"name": path.name}
        if parent_id:
            metadata["parents"] = parent_id

        try:
            drive = build(
                "drive",
                "v3",
                credentials=credentials,
                cache_discovery=False,
            )

            media = MediaFileUpload(
                str(path),
                resumable=True,
            )

            response = (
                drive.files()
                .create(
                    body=metadata,
                    media_body=media,
                    fields=("id,name,mimeType,size,modifiedTime,parents,trashed"),
                )
                .execute()
            )

        except (HttpError, GoogleAuthError, OSError, ValueError) as exc:
            raise UploadError(f"Google Drive upload failed: {exc}") from exc

        file_id = response.get("id")
        name = response.get("name")
        mime_type = response.get("mimeType")

        if not all(isinstance(value, str) and value for value in (file_id, name, mime_type)):
            raise UploadError(
                "Google Drive returned incomplete file metadata. "
                "The upload may have succeeded; reconcile Drive before retrying."
            )

        raw_size = response.get("size")
        try:
            size_bytes = int(raw_size) if raw_size is not None else file_size
        except (ValueError, TypeError) as exc:
            raise UploadError("Google Drive returned an invalid file size.") from exc

        if size_bytes < 0:
            raise UploadError("Google Drive returned a negative file size.")

        parents = response.get("parents") or []
        returned_parent = parents[0] if parents else parent_id

        indexed_file = IndexedFile(
            account_id=account_id,
            file_id=file_id,
            name=name,
            mime_type=mime_type,
            parent_id=returned_parent,
            size_bytes=size_bytes,
            modified_time=response.get("modifiedTime"),
            trashed=bool(response.get("trashed", False)),
        )

        try:
            self.file_index.upsert(indexed_file)
        except (OSError, ValueError, TypeError) as exc:
            raise UploadError(
                "The file was uploaded to Google Drive, but QIFY could not "
                "save its local index entry. Reconcile the remote file before "
                "retrying to avoid creating a duplicate."
            ) from exc

        return indexed_file
