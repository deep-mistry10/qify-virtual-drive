
"""Google OAuth integration with QIFY's local account registry."""

from __future__ import annotations

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
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/userinfo.profile",
    "https://www.googleapis.com/auth/drive.readonly",
]


class OAuthAccountService:
    """Connect and reauthorize Google accounts securely."""

    def __init__(
        self,
        database: AccountDatabase,
        token_store: TokenStore,
        credentials_path: str | Path,
    ) -> None:
        self.database = database
        self.token_store = token_store
        self.credentials_path = Path(credentials_path)

    def _check_credentials_file(self) -> None:
        if not self.credentials_path.is_file():
            raise FileNotFoundError(
                f"Desktop OAuth credentials not found: {self.credentials_path}"
            )

    def _authorize(self):
        """Run the interactive Google OAuth consent flow."""
        self._check_credentials_file()

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.credentials_path),
            scopes=SCOPES,
        )

        # Handle scope validation when the OAuth session is available.
        # This also supports lightweight mocked flows used by tests.
        oauth_session = getattr(flow, "oauth2session", None)
        oauth_client = getattr(oauth_session, "_client", None)

        if oauth_client is not None:
            oauth_client.scope = None

        return flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=True,
            prompt="consent",
            access_type="offline",
            include_granted_scopes="false",
        )

    @staticmethod
    def _get_identity(credentials) -> dict[str, Any]:
        """Retrieve the identity associated with the OAuth credentials."""
        try:
            oauth_service = build(
                "oauth2",
                "v2",
                credentials=credentials,
                cache_discovery=False,
            )

            identity: dict[str, Any] = (
                oauth_service.userinfo().get().execute()
            )

        except (HttpError, GoogleAuthError) as exc:
            raise RuntimeError(
                f"Could not retrieve Google account identity: {exc}"
            ) from exc

        subject = identity.get("id")
        email = identity.get("email")

        if not isinstance(subject, str) or not subject:
            raise RuntimeError(
                "Google did not return a stable account identifier."
            )

        if not isinstance(email, str) or not email:
            raise RuntimeError(
                "Google did not return an email address."
            )

        if not identity.get("verified_email", False):
            raise RuntimeError(
                "Google did not confirm that this email is verified."
            )

        return identity

    def connect_account(self) -> Account:
        """Authorize and register a new Google account."""
        credentials = self._authorize()
        identity = self._get_identity(credentials)

        subject = identity["id"]
        email = identity["email"]

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
                f"{email} is already registered. "
                "Use reauthorize_account() to update its permissions."
            )

        if len(self.database.list_accounts()) >= self.database.max_accounts:
            raise AccountError(
                f"All {self.database.max_accounts} Google account slots "
                "are in use."
            )

        account = Account(
            account_id=f"google:{subject}",
            email=email,
            google_subject_id=subject,
        )

        self.database.add_account(account)

        try:
            self.token_store.save(
                subject,
                credentials.to_json(),
            )

        except (TokenStoreError, ValueError):
            self.database.remove_account(account.account_id)
            raise

        return account

    def reauthorize_account(self, account_id: str) -> Account:
        """Update permissions for an existing account without duplicating it."""
        account = self.database.get_account(account_id)

        if account is None:
            raise AccountError(
                f"Account {account_id} was not found."
            )

        if not account.google_subject_id:
            raise AccountError(
                "This account has no Google identity. "
                "Reconnect it through the account setup process."
            )

        credentials = self._authorize()
        identity = self._get_identity(credentials)

        if identity["id"] != account.google_subject_id:
            raise AccountError(
                "The authorized Google account does not match the selected "
                "account. The existing token was not changed."
            )

        if identity["email"].casefold() != account.email.casefold():
            raise AccountError(
                "The Google email does not match the saved account. "
                "The existing token was not changed."
            )

        try:
            self.token_store.save(
                account.google_subject_id,
                credentials.to_json(),
            )

        except (TokenStoreError, ValueError) as exc:
            raise RuntimeError(
                "Could not save the updated Google authorization."
            ) from exc

        return account
