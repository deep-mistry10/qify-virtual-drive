"""Command-line account management for QIFY Virtual Drive."""

from __future__ import annotations

import argparse
import sys
from qify_virtual_drive.file_index import FileIndex
from qify_virtual_drive.drive_sync import DriveSyncError, DriveSyncService
from qify_virtual_drive.file_index import FileIndex
from pathlib import Path
from qify_virtual_drive.quota_sync import QuotaSyncError, QuotaSyncService
from qify_virtual_drive.storage_quota import QuotaStore
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

    commands = parser.add_subparsers(
        dest="command",
        required=True,
    )
    commands.add_parser(
    "quota",
    help="Refresh and display Google Drive storage quotas",
)

    commands.add_parser("list", help="List locally registered Google accounts")
    commands.add_parser("connect", help="Connect a Google account using OAuth")
    commands.add_parser("sync", help="Sync file metadata for a registered account")
    commands.add_parser("files", help="List indexed files across connected Google accounts")

    return parser



def main() -> int:
    args = build_parser().parse_args()
    database = AccountDatabase()



    
    if args.command == "quota":
        accounts = database.list_accounts()

        if not accounts:
            print("No registered accounts. Connect an account first.")
            return 1

        quota_store = QuotaStore(database.database_path)
        service = QuotaSyncService(
            database=database,
            quota_store=quota_store,
            token_store=TokenStore(),
        )

        failed = False

        for account in accounts:
            try:
                quota = service.sync_account(account.account_id)
            except QuotaSyncError as exc:
                failed = True
                print(f"\n{account.email}: quota refresh failed: {exc}")
                continue

            available_gb = quota.available_bytes / (1024 ** 3)
            total_gb = quota.total_bytes / (1024 ** 3)
            used_gb = quota.used_bytes / (1024 ** 3)

            print(f"\nAccount:   {account.email}")
            print(f"Total:     {total_gb:.2f} GiB")
            print(f"Used:      {used_gb:.2f} GiB")
            print(f"Available: {available_gb:.2f} GiB")
            print(f"Checked:   {quota.checked_at}")

        return 1 if failed else 0

    if args.command == "files":
        file_index = FileIndex()
        files = file_index.list_files()

        if not files:
            print("No indexed files found.")
            print("Run 'python -m qify_virtual_drive.account_cli sync' first.")
            return 0

        print(f"QIFY Virtual Drive — {len(files)} indexed files")
        print("-" * 72)

        for item in files:
            size = (
                f"{item.size_bytes:,} bytes"
                if item.size_bytes is not None
                else "size unknown"
            )
            print(f"Name:   {item.name}")
            print(f"Owner:  {item.account_id}")
            print(f"Type:   {item.mime_type}")
            print(f"Size:   {size}")
            print(f"ID:     {item.file_id}")
            print("-" * 72)

        return 0


    if args.command == "sync":
        accounts = database.list_accounts()

        if not accounts:
            print("No registered accounts. Connect an account first.")
            return 1

        print("Registered accounts:")
        for index, account in enumerate(accounts, start=1):
            print(f"{index}. {account.email} [{account.account_id}]")

        selection = input("Enter the account number to sync: ").strip()

        try:
            selected_index = int(selection) - 1
            if not 0 <= selected_index < len(accounts):
                raise ValueError
        except ValueError:
            print("Invalid account selection.", file=sys.stderr)
            return 1

        account = accounts[selected_index]
        service = DriveSyncService(
            database=database,
            file_index=FileIndex(),
            token_store=TokenStore(),
        )

        try:
            count = service.sync_account(account.account_id)
        except (DriveSyncError, TokenStoreError) as exc:
            print(f"Sync failed: {exc}", file=sys.stderr)
            return 1

        print(f"Indexed {count} file records for {account.email}.")
        return 0

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
