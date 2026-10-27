
"""Command-line account management for QIFY Virtual Drive."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.accounts import AccountError
from qify_virtual_drive.oauth_accounts import OAuthAccountService
from qify_virtual_drive.token_store import TokenStore, TokenStoreError


def project_root() -> Path:
    """Return the repository root for development configuration."""
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qify-vdrive",
        description="Manage Google accounts connected to QIFY Virtual Drive.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List locally registered Google accounts")
    commands.add_parser("connect", help="Connect a Google account using OAuth")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    database = AccountDatabase()

    if args.command == "list":
        accounts = database.list_accounts()
        print(f"QIFY Virtual Drive accounts ({len(accounts)}/4)")
        if not accounts:
            print("No accounts registered.")
            return 0

        for index, account in enumerate(accounts, start=1):
            print(
                f"{index}. {account.email} | "
                f"status={account.status.value} | "
                f"id={account.account_id}"
            )
        return 0

    if args.command == "connect":
        credentials_path = project_root() / "config" / "credentials.json"
        service = OAuthAccountService(
            database=database,
            token_store=TokenStore(),
            credentials_path=credentials_path,
        )
        try:
            account = service.connect_account()
        except (AccountError, TokenStoreError, FileNotFoundError, RuntimeError, ValueError) as exc:
            print(f"Could not connect account: {exc}", file=sys.stderr)
            return 1

        print(f"Connected Google account: {account.email}")
        print(f"Registered account ID: {account.account_id}")
        print("Drive file access remains read-only in this development stage.")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
