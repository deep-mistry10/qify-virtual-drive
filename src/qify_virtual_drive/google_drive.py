"""Initial Google Drive OAuth and read-only file listing.

Development prototype only. Token storage will be hardened before release.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

APP_NAME = "QIFY Virtual Drive"
SCOPES = ["https://www.googleapis.com/auth/drive.metadata.readonly"]


def get_app_data_dir() -> Path:
    """Return a per-user local application data directory."""
    base = os.environ.get("LOCALAPPDATA")
    if not base:
        raise RuntimeError("LOCALAPPDATA is unavailable on this Windows system.")

    path = Path(base) / "QIFY" / "VirtualDrive"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_credentials_path() -> Path:
    """Find the downloaded desktop OAuth client configuration."""
    project_root = Path(__file__).resolve().parents[2]
    credentials_path = project_root / "config" / "credentials.json"

    if not credentials_path.is_file():
        raise FileNotFoundError(
            "OAuth credentials were not found at "
            f"{credentials_path}. Download the Desktop OAuth JSON file "
            "from Google Cloud and save it there."
        )

    try:
        data = json.loads(credentials_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("The OAuth credentials JSON is unreadable or invalid.") from exc

    if "installed" not in data:
        raise RuntimeError(
            "Expected a Desktop app OAuth JSON file containing an 'installed' section."
        )

    return credentials_path


def authenticate() -> Credentials:
    """Sign in with Google and return credentials for the read-only prototype.

    The temporary token file is stored outside the repository. This storage
    method is for development only and must be replaced before public release.
    """
    token_path = get_app_data_dir() / "dev_token.json"
    credentials: Credentials | None = None

    if token_path.is_file():
        try:
            credentials = Credentials.from_authorized_user_file(str(token_path), SCOPES)
        except (OSError, ValueError, json.JSONDecodeError):
            credentials = None

    if credentials and credentials.expired and credentials.refresh_token:
        credentials.refresh(Request())

    if not credentials or not credentials.valid:
        flow = InstalledAppFlow.from_client_secrets_file(str(get_credentials_path()), SCOPES)
        credentials = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=True,
        )

    if not credentials or not credentials.valid:
        raise RuntimeError("Google authentication did not return valid credentials.")

    token_path.write_text(credentials.to_json(), encoding="utf-8")
    return credentials


def list_drive_files(page_size: int = 20) -> list[dict[str, Any]]:
    """List file metadata from the signed-in account without downloading contents."""
    if not 1 <= page_size <= 100:
        raise ValueError("page_size must be between 1 and 100.")

    credentials = authenticate()
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    try:
        response = (
            service.files()
            .list(
                pageSize=page_size,
                orderBy="name",
                fields="files(id,name,mimeType,size,modifiedTime),nextPageToken",
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
    except (HttpError, GoogleAuthError) as exc:
        raise RuntimeError(f"Google Drive request failed: {exc}") from exc

    return response.get("files", [])


def main() -> None:
    """Authenticate and print a small list of cloud file metadata."""
    try:
        files = list_drive_files()
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(f"QIFY Virtual Drive: {exc}")
        raise SystemExit(1) from exc

    print(f"{APP_NAME} — Google Drive connection successful")
    if not files:
        print("No files were returned for this account.")
        return

    for item in files:
        print(f"- {item.get('name', '(unnamed)')} [{item.get('mimeType', 'unknown')}]")

    print(f"\nListed {len(files)} item(s). File contents were not downloaded.")


if __name__ == "__main__":
    main()
