
"""Google OAuth integration with QIFY's local account registry."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from google.auth.exceptions import GoogleAuthError
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.accounts import Account, AccountError
from qify_virtual_drive.token_store import TokenStore, TokenStoreError


SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.metadata.readonly",
]


class OAuthAccountService:
    """Connect Google accounts without storing tokens in SQLite."""

    def __init__(
        self,
        database: AccountDatabase,
        token_store: TokenStore,
        credentials_path: str | Path,
    ) -> None:
        self.database = database
        self.token_store = token_store
        self.credentials_path = Path(credentials_path)

    def connect_account(self) -> Account:
        """Run interactive OAuth and register the authorized Google identity."""
        if not self.credentials_path.is_file():
            raise FileNotFoundError(
                f"Desktop OAuth credentials not found: {self.credentials_path}"
            )

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            SCOPES,
        )
        credentials = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=True,
        )

        try:
            oauth_service = build(
                "oauth2",
                "v2",
                credentials=credentials,
                cache_discovery=False,
            )
            identity: dict[str, Any] = oauth_service.userinfo().get().execute()
        except (HttpError, GoogleAuthError) as exc:
            raise RuntimeError(f"Could not retrieve Google account identity: {exc}") from exc

        subject = identity.get("id")
        email = identity.get("email")
        verified = identity.get("verified_email", False)

        if not isinstance(subject, str) or not subject:
            raise RuntimeError("Google did not return a stable account identifier")
        if not isinstance(email, str) or not email:
            raise RuntimeError("Google did not return an email address")
        if not verified:
            raise RuntimeError("Google did not confirm that this email is verified")

        existing = next(
            (
                account
                for account in self.database.list_accounts()
                if account.google_subject_id == subject
            ),
            None,
        )

        if existing is not None:
            raise AccountError(
                f"{email} is already registered. Reconnection support will be added next."
            )

        # Check capacity after identity is known, so duplicate identities can
        # be recognized even when the registry already contains four accounts.
        if len(self.database.list_accounts()) >= self.database.max_accounts:
            raise AccountError(
                f"All {self.database.max_accounts} Google account slots are in use."
            )

        account = Account(
            account_id=f"google:{subject}",
            email=email,
            google_subject_id=subject,
        )

        # Register metadata first. If secure token storage fails, roll back the
        # metadata entry so the registry does not claim a usable connection.
        self.database.add_account(account)
        try:
            self.token_store.save(subject, credentials.to_json())
        except (TokenStoreError, ValueError):
            self.database.remove_account(account.account_id)
            raise

        return account
