"""Command-line interface for QIFY Virtual Drive."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.accounts import AccountError, AccountStatus
from qify_virtual_drive.download_service import DownloadError, DownloadService
from qify_virtual_drive.drive_sync import DriveSyncError, DriveSyncService
from qify_virtual_drive.file_index import FileIndex
from qify_virtual_drive.oauth_accounts import OAuthAccountService
from qify_virtual_drive.quota_sync import QuotaSyncError, QuotaSyncService
from qify_virtual_drive.storage_quota import QuotaStore
from qify_virtual_drive.token_store import TokenStore, TokenStoreError
from qify_virtual_drive.upload_service import UploadError, UploadService
from qify_virtual_drive.virtual_filesystem import create_google_drive_file_system


def project_root() -> Path:
    """Return the repository root for development configuration."""
    return Path(__file__).resolve().parents[2]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qify-vdrive",
        description="Manage Google accounts connected to QIFY Virtual Drive.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="List registered Google accounts")
    commands.add_parser("connect", help="Connect a new Google account using OAuth")
    commands.add_parser("reauthorize", help="Update permissions for an existing Google account")
    commands.add_parser("sync", help="Sync file metadata for a registered account")
    commands.add_parser("files", help="List indexed files across connected accounts")
    commands.add_parser("quota", help="Refresh and display Google Drive storage quotas")

    upload_parser = commands.add_parser(
        "upload", help="Upload one local file to a connected Google Drive account"
    )
    upload_parser.add_argument("path", type=Path, help="Path to the local file to upload")
    upload_parser.add_argument(
        "--parent-id", help="Google Drive folder ID to upload into; defaults to Drive's root"
    )

    download_parser = commands.add_parser(
        "download", help="Download an indexed file from its owning Google Drive account"
    )
    download_parser.add_argument("account_id", help="QIFY account ID that owns the file")
    download_parser.add_argument("file_id", help="Google Drive file ID")
    download_parser.add_argument(
        "destination", type=Path, help="Local destination path; its parent directory must exist"
    )
    download_parser.add_argument(
        "--overwrite", action="store_true", help="Allow replacing an existing destination file"
    )

    mount_parser = commands.add_parser(
        "mount", help="Mount Google Drive accounts as a Windows virtual drive"
    )
    mount_parser.add_argument(
        "mountpoint",
        nargs="?",
        default="Q:",
        help="Windows drive letter or mount directory (default: Q:)",
    )
    mount_parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable WinFsp debug logging",
    )

    return parser


def create_oauth_service(database: AccountDatabase) -> OAuthAccountService:
    """Create the OAuth service using the local Desktop client configuration."""
    return OAuthAccountService(
        database=database,
        token_store=TokenStore(),
        credentials_path=project_root() / "config" / "credentials.json",
    )


def _select_account(accounts):
    """Ask the user to select one account from a list."""
    if not accounts:
        print("No registered accounts. Connect an account first.")
        return None

    print("Registered accounts:")
    for index, account in enumerate(accounts, start=1):
        print(f"{index}. {account.email} [{account.account_id}]")

    selection = input("Enter the account number: ").strip()
    try:
        selected_index = int(selection) - 1
        if not 0 <= selected_index < len(accounts):
            raise ValueError
    except ValueError:
        print("Invalid account selection.", file=sys.stderr)
        return None

    return accounts[selected_index]


def _mount_virtual_drive(database: AccountDatabase, mountpoint: str, debug: bool) -> int:
    """Mount immediately; load metadata only for folders the user browses."""
    accounts = [
        account for account in database.list_accounts() if account.status == AccountStatus.CONNECTED
    ]

    if not accounts:
        print("No connected Google accounts. Run the connect command first.")
        return 1

    token_store = TokenStore()
    file_index = FileIndex()

    sync_service = DriveSyncService(
        database=database,
        file_index=file_index,
        token_store=token_store,
    )

    download_service = DownloadService(
        database=database,
        file_index=file_index,
        token_store=token_store,
    )

    try:
        filesystem = create_google_drive_file_system(
            mountpoint=mountpoint,
            database=database,
            file_index=file_index,
            download_service=download_service,
            debug=debug,
            sync_service=sync_service,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Could not create virtual filesystem: {exc}", file=sys.stderr)
        return 1

    started = False

    try:
        filesystem.start()
        started = True

        print()
        print("QIFY Virtual Drive is mounted.")
        print(f"Mount point: {mountpoint}")
        print("Mode: READ ONLY")
        print("Metadata: fetched on demand when you browse folders.")
        print("File contents: downloaded only when you open a file.")
        print("Press Ctrl+C to unmount.")
        print()

        try:
            while True:
                input("QIFY Virtual Drive is running. Press Ctrl+C to stop.\n")
        except KeyboardInterrupt:
            print("\nUnmounting QIFY Virtual Drive...")

    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Virtual drive error: {exc}", file=sys.stderr)
        return 1
    finally:
        if started:
            filesystem.stop()

    print("QIFY Virtual Drive unmounted.")
    return 0


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
                f"{index}. {account.email} | status={account.status.value} | "
                f"id={account.account_id}"
            )

        return 0

    if args.command == "connect":
        service = create_oauth_service(database)

        try:
            account = service.connect_account()
        except (
            AccountError,
            TokenStoreError,
            FileNotFoundError,
            RuntimeError,
            ValueError,
        ) as exc:
            print(f"Could not connect account: {exc}", file=sys.stderr)
            return 1

        print(f"Connected Google account: {account.email}")
        print(f"Registered account ID: {account.account_id}")
        return 0

    if args.command == "reauthorize":
        account = _select_account(database.list_accounts())
        if account is None:
            return 1

        service = create_oauth_service(database)

        try:
            updated_account = service.reauthorize_account(account.account_id)
        except (
            AccountError,
            TokenStoreError,
            FileNotFoundError,
            RuntimeError,
            ValueError,
        ) as exc:
            print(f"Could not reauthorize account: {exc}", file=sys.stderr)
            return 1

        print(f"Authorization updated for: {updated_account.email}")
        print("The existing account record was preserved.")
        return 0

    if args.command == "sync":
        account = _select_account(database.list_accounts())
        if account is None:
            return 1

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

    if args.command == "files":
        files = FileIndex().list_files()

        if not files:
            print("No indexed files found.")
            print("Run 'python -m qify_virtual_drive.account_cli sync' first.")
            return 0

        print(f"QIFY Virtual Drive - {len(files)} indexed files")
        print("-" * 72)

        for item in files:
            size = f"{item.size_bytes:,} bytes" if item.size_bytes is not None else "size unknown"
            print(f"Name:   {item.name}")
            print(f"Owner:  {item.account_id}")
            print(f"Type:   {item.mime_type}")
            print(f"Size:   {size}")
            print(f"ID:     {item.file_id}")
            print("-" * 72)

        return 0

    if args.command == "quota":
        accounts = database.list_accounts()

        if not accounts:
            print("No registered accounts. Connect an account first.")
            return 1

        service = QuotaSyncService(
            database=database,
            quota_store=QuotaStore(database.database_path),
            token_store=TokenStore(),
        )

        failed = False

        for account in accounts:
            if account.status != AccountStatus.CONNECTED:
                print(f"\nSkipping disconnected account: {account.email}")
                continue

            try:
                quota = service.sync_account(account.account_id)
            except (QuotaSyncError, TokenStoreError) as exc:
                failed = True
                print(
                    f"\n{account.email}: quota refresh failed: {exc}",
                    file=sys.stderr,
                )
                continue

            available_gib = quota.available_bytes / (1024**3)
            total_gib = quota.total_bytes / (1024**3)
            used_gib = quota.used_bytes / (1024**3)

            print(f"\nAccount:   {account.email}")
            print(f"Total:     {total_gib:.2f} GiB")
            print(f"Used:      {used_gib:.2f} GiB")
            print(f"Available: {available_gib:.2f} GiB")
            print(f"Checked:   {quota.checked_at}")

        return 1 if failed else 0

    if args.command == "upload":
        if not args.path.is_file() or args.path.is_symlink():
            print(
                "Upload path must point to an existing, regular, non-symlink file.",
                file=sys.stderr,
            )
            return 1

        accounts = [
            account
            for account in database.list_accounts()
            if account.status == AccountStatus.CONNECTED
        ]

        if not accounts:
            print("No connected Google accounts. Run the connect command first.")
            return 1

        quota_store = QuotaStore(database.database_path)
        token_store = TokenStore()

        quota_service = QuotaSyncService(
            database=database,
            quota_store=quota_store,
            token_store=token_store,
        )

        for account in accounts:
            try:
                quota_service.sync_account(account.account_id)
            except (QuotaSyncError, TokenStoreError) as exc:
                print(
                    f"Cannot refresh quota for {account.email}: {exc}",
                    file=sys.stderr,
                )
                print("Upload cancelled; no file was uploaded.")
                return 1

        service = UploadService(
            database=database,
            file_index=FileIndex(),
            quota_store=quota_store,
            token_store=token_store,
        )

        try:
            indexed_file = service.upload_file(
                args.path,
                parent_id=args.parent_id,
            )
        except (UploadError, TokenStoreError) as exc:
            print(f"Upload failed: {exc}", file=sys.stderr)
            return 1

        print("Upload successful.")
        print(f"File:       {indexed_file.name}")
        print(f"Account ID: {indexed_file.account_id}")
        print(f"Drive ID:   {indexed_file.file_id}")
        print(f"Size:       {indexed_file.size_bytes:,} bytes")
        print("Local file index updated.")
        return 0

    if args.command == "download":
        service = DownloadService(
            database=database,
            file_index=FileIndex(),
            token_store=TokenStore(),
        )

        try:
            downloaded_path = service.download_file(
                account_id=args.account_id,
                file_id=args.file_id,
                destination=args.destination,
                overwrite=args.overwrite,
            )
        except (DownloadError, TokenStoreError) as exc:
            print(f"Download failed: {exc}", file=sys.stderr)
            return 1

        print("Download successful.")
        print(f"Saved to: {downloaded_path}")
        return 0

    if args.command == "mount":
        return _mount_virtual_drive(
            database=database,
            mountpoint=args.mountpoint,
            debug=args.debug,
        )

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
