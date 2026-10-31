"""Safely download indexed files from their owning Google Drive accounts."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.file_index import FileIndex
from qify_virtual_drive.token_store import TokenStore, TokenStoreError

DOWNLOAD_SCOPES = [
    "https://www.googleapis.com/auth/drive.metadata.readonly",
    "https://www.googleapis.com/auth/drive.file",
]


class DownloadError(RuntimeError):
    """Raised when a Google Drive download fails."""


class DownloadService:
    """Download indexed files using their recorded owning account."""

    def __init__(
        self,
        database: AccountDatabase,
        file_index: FileIndex,
        token_store: TokenStore,
    ) -> None:
        self.database = database
        self.file_index = file_index
        self.token_store = token_store

    def _get_credentials(self, account_id: str) -> Credentials:
        account = self.database.get_account(account_id)

        if account is None:
            raise DownloadError(f"Unknown account: {account_id}")

        if getattr(account.status, "value", account.status) != "connected":
            raise DownloadError(f"Google account {account.email} is not connected.")

        if not account.google_subject_id:
            raise DownloadError(f"Google account {account.email} has no saved identity.")

        try:
            token_json = self.token_store.load(account.google_subject_id)
            if not token_json:
                raise DownloadError(
                    f"No saved credentials for {account.email}. Reauthorize the account."
                )

            credentials = Credentials.from_authorized_user_info(
                json.loads(token_json),
                scopes=DOWNLOAD_SCOPES,
            )

            if credentials.expired:
                if not credentials.refresh_token:
                    raise DownloadError(f"Credentials for {account.email} need reauthorization.")

                credentials.refresh(Request())
                self.token_store.save(
                    account.google_subject_id,
                    credentials.to_json(),
                )

            if not credentials.valid:
                raise DownloadError(
                    f"Credentials for {account.email} are invalid. Reauthorize the account."
                )

            return credentials

        except DownloadError:
            raise
        except (
            ValueError,
            TypeError,
            GoogleAuthError,
            OSError,
            TokenStoreError,
        ) as exc:
            raise DownloadError(
                f"Could not prepare credentials for {account.email}: {exc}"
            ) from exc

    def download_file(
        self,
        account_id: str,
        file_id: str,
        destination: str | Path,
        *,
        overwrite: bool = False,
    ) -> Path:
        """Download an indexed binary file without overwriting by default."""
        item = self.file_index.get(account_id, file_id)

        if item is None:
            raise DownloadError(f"File is not indexed for account {account_id}: {file_id}")

        if item.trashed:
            raise DownloadError("Cannot download a file marked as trashed in the index.")

        target = Path(destination)

        if target.is_symlink():
            raise DownloadError("Symbolic-link destinations are not accepted.")

        if not target.parent.is_dir():
            raise DownloadError(f"Destination directory does not exist: {target.parent}")

        if target.exists() and not overwrite:
            raise DownloadError(
                f"Destination already exists: {target}. "
                "Choose another path or explicitly enable overwrite."
            )

        credentials = self._get_credentials(account_id)
        temporary_path: Path | None = None

        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".qify-download-",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as temporary_file:
                temporary_path = Path(temporary_file.name)

                drive = build(
                    "drive",
                    "v3",
                    credentials=credentials,
                    cache_discovery=False,
                )
                request = drive.files().get_media(fileId=file_id)
                downloader = MediaIoBaseDownload(temporary_file, request)

                done = False
                while not done:
                    _, done = downloader.next_chunk()

                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            if overwrite:
                os.replace(temporary_path, target)
            else:
                # An atomic same-directory hard link fails if the target
                # already exists, preventing accidental replacement.
                os.link(temporary_path, target)
                temporary_path.unlink()

            return target

        except DownloadError:
            raise
        except FileExistsError as exc:
            raise DownloadError(
                f"Destination already exists: {target}. Existing data was preserved."
            ) from exc
        except (HttpError, GoogleAuthError, OSError, ValueError) as exc:
            raise DownloadError(f"Google Drive download failed for {item.name}: {exc}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink(missing_ok=True)
                except OSError:
                    pass
