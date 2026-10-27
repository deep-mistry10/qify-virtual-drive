"""Secure storage for OAuth tokens using the operating system keyring."""

from __future__ import annotations

import json

import keyring
from keyring.errors import KeyringError

SERVICE_NAME = "QIFY.VirtualDrive"
SUPPORTED_TOKEN_VERSION = 1


class TokenStoreError(RuntimeError):
    """Raised when secure token storage fails."""


class TokenStore:
    """Store OAuth credentials separately for each Google account."""

    @staticmethod
    def _username(google_subject_id: str) -> str:
        if not google_subject_id or not google_subject_id.strip():
            raise ValueError("Google subject ID cannot be empty")
        return f"google-account:{google_subject_id}"

    def save(self, google_subject_id: str, token_json: str) -> None:
        """Save serialized OAuth credentials in the OS credential store."""
        try:
            payload = json.loads(token_json)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Token data must be valid JSON") from exc

        if not isinstance(payload, dict):
            raise TypeError("Token data must be a JSON object")

        try:
            keyring.set_password(
                SERVICE_NAME,
                self._username(google_subject_id),
                json.dumps(
                    {
                        "version": SUPPORTED_TOKEN_VERSION,
                        "credentials": payload,
                    },
                ),
            )
        except (KeyringError, RuntimeError, OSError) as exc:
            raise TokenStoreError(
                "Could not save credentials to the operating system credential store"
            ) from exc

    def load(self, google_subject_id: str) -> str | None:
        """Load credentials for one account, or return None if absent."""
        try:
            stored = keyring.get_password(
                SERVICE_NAME,
                self._username(google_subject_id),
            )
        except (KeyringError, RuntimeError, OSError) as exc:
            raise TokenStoreError("Could not access the operating system credential store") from exc

        if stored is None:
            return None

        try:
            payload = json.loads(stored)
            if (
                not isinstance(payload, dict)
                or payload.get("version") != SUPPORTED_TOKEN_VERSION
                or not isinstance(payload.get("credentials"), dict)
            ):
                raise ValueError("Unsupported token-store format")

            return json.dumps(payload["credentials"])
        except (json.JSONDecodeError, ValueError) as exc:
            raise TokenStoreError("Stored credentials are invalid") from exc

    def delete(self, google_subject_id: str) -> None:
        """Delete only this account's locally stored credentials."""
        username = self._username(google_subject_id)

        try:
            existing = keyring.get_password(SERVICE_NAME, username)
            if existing is not None:
                keyring.delete_password(SERVICE_NAME, username)
        except (KeyringError, RuntimeError, OSError) as exc:
            raise TokenStoreError(
                "Could not remove credentials from the operating system credential store"
            ) from exc
