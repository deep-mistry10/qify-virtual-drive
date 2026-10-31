"""Read-only WinFsp filesystem backed by multiple Google Drive accounts."""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PureWindowsPath

from winfspy import (
    CREATE_FILE_CREATE_OPTIONS,
    FILE_ATTRIBUTE,
    BaseFileSystemOperations,
    FileSystem,
    NTStatusAccessDenied,
    NTStatusEndOfFile,
    NTStatusMediaWriteProtected,
    NTStatusNotADirectory,
    NTStatusObjectNameNotFound,
)
from winfspy.plumbing.security_descriptor import SecurityDescriptor
from winfspy.plumbing.win32_filetime import filetime_now

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.accounts import Account, AccountStatus
from qify_virtual_drive.download_service import DownloadError, DownloadService
from qify_virtual_drive.drive_sync import DriveSyncError, DriveSyncService
from qify_virtual_drive.file_index import FileIndex, IndexedFile
from qify_virtual_drive.token_store import TokenStoreError

logger = logging.getLogger(__name__)

GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"
VOLUME_LABEL = "QIFY Virtual Drive"
FILESYSTEM_NAME = "QIFY"


@dataclass(frozen=True, slots=True)
class VirtualNode:
    """A directory or file exposed by the virtual filesystem."""

    path: PureWindowsPath
    kind: str
    account: Account | None = None
    indexed_file: IndexedFile | None = None

    @property
    def is_directory(self) -> bool:
        return self.kind == "directory"


@dataclass(slots=True)
class OpenedNode:
    """Context retained by WinFsp while a file or directory is open."""

    node: VirtualNode
    local_path: Path | None = None


def _safe_component(name: str) -> str:
    """Convert a Google Drive name into a valid Windows path component."""
    cleaned = "".join(
        "_" if character in '<>:"/\\|?*' or ord(character) < 32 else character for character in name
    )
    cleaned = cleaned.rstrip(" .").strip()

    if not cleaned:
        cleaned = "_"

    reserved = {
        "CON",
        "PRN",
        "AUX",
        "NUL",
        *(f"COM{number}" for number in range(1, 10)),
        *(f"LPT{number}" for number in range(1, 10)),
    }

    stem = cleaned.split(".", 1)[0].upper()
    if stem in reserved:
        cleaned = f"_{cleaned}"

    return cleaned


def _unique_names(names: list[str]) -> list[str]:
    """Return case-insensitively unique Windows names in stable input order."""
    used: set[str] = set()
    result: list[str] = []

    for original in names:
        base = _safe_component(original)
        candidate = base
        suffix_number = 2

        while candidate.casefold() in used:
            suffix = f" ({suffix_number})"
            candidate = f"{base}{suffix}"
            suffix_number += 1

        used.add(candidate.casefold())
        result.append(candidate)

    return result


def _filetime_from_iso(value: str | None) -> int:
    """Convert a Google RFC3339 timestamp to a Windows FILETIME."""
    if not value:
        return filetime_now()

    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)

        epoch = datetime(1601, 1, 1, tzinfo=UTC)
        delta = parsed.astimezone(UTC) - epoch
        return int(delta.total_seconds() * 10_000_000)
    except (TypeError, ValueError, OverflowError):
        return filetime_now()


class GoogleDriveFileSystemOperations(BaseFileSystemOperations):
    """WinFsp operations for a read-only multi-account Google Drive view."""

    def __init__(
        self,
        database: AccountDatabase,
        file_index: FileIndex,
        download_service: DownloadService,
        sync_service: DriveSyncService | None = None,
        cache_directory: str | Path | None = None,
    ) -> None:
        super().__init__()

        self.database = database
        self.file_index = file_index
        self.download_service = download_service
        self.sync_service = sync_service

        self._folder_sync_lock = threading.Lock()
        self._folder_sync_times: dict[tuple[str, str | None], float] = {}
        self._folder_sync_ttl_seconds = 30.0

        if cache_directory is None:
            local_app_data = os.environ.get("LOCALAPPDATA")
            if not local_app_data:
                raise RuntimeError("LOCALAPPDATA is unavailable.")

            cache_directory = Path(local_app_data) / "QIFY" / "VirtualDrive" / "cache"

        self.cache_directory = Path(cache_directory)
        self.cache_directory.mkdir(parents=True, exist_ok=True)

        self._lock = threading.RLock()
        self._root_path = PureWindowsPath("\\")
        self._root_security = SecurityDescriptor.from_string(
            "O:BAG:BAD:P(A;;FA;;;SY)(A;;FA;;;BA)(A;;FA;;;WD)"
        )

        self._volume_info = {
            "total_size": 15 * 1024**4,
            "free_size": 15 * 1024**4,
            "volume_label": VOLUME_LABEL,
        }

    def _connected_accounts(self) -> list[Account]:
        return [
            account
            for account in self.database.list_accounts()
            if account.status == AccountStatus.CONNECTED
        ]

    def _account_directory_names(self) -> dict[str, str]:
        accounts = self._connected_accounts()
        names = _unique_names([account.email for account in accounts])

        return {account.account_id: name for account, name in zip(accounts, names, strict=True)}

    def _account_by_directory(self, directory_name: str) -> Account | None:
        wanted = directory_name.casefold()
        names = self._account_directory_names()

        for account in self._connected_accounts():
            mapped_name = names.get(account.account_id)

            if mapped_name and mapped_name.casefold() == wanted:
                return account

        return None

    def _refresh_folder_metadata(
        self,
        account: Account,
        parent_id: str | None,
    ) -> None:
        """Fetch metadata for one folder's direct children when its cache is stale.

        This does not recursively scan child folders. Metadata is refreshed at
        most once every 30 seconds per folder during this mount session.
        """
        if self.sync_service is None:
            return

        key = (account.account_id, parent_id)

        with self._folder_sync_lock:
            last_sync = self._folder_sync_times.get(key)

            if (
                last_sync is not None
                and time.monotonic() - last_sync < self._folder_sync_ttl_seconds
            ):
                return

            try:
                count = self.sync_service.sync_folder(
                    account.account_id,
                    parent_id,
                )

                self._folder_sync_times[key] = time.monotonic()

                logger.info(
                    "Loaded %s metadata entries for %s (folder %s)",
                    count,
                    account.email,
                    parent_id or "Drive root",
                )

            except DriveSyncError:
                logger.exception(
                    "Could not refresh folder metadata for %s (folder %s); "
                    "using previously indexed entries if available",
                    account.email,
                    parent_id or "Drive root",
                )

    def _children_for_folder(
        self,
        account: Account,
        parent_id: str | None,
    ) -> list[tuple[str, IndexedFile]]:
        self._refresh_folder_metadata(account, parent_id)

        indexed_items = self.file_index.list_children(
            account.account_id,
            parent_id,
        )

        mapped_names = _unique_names([item.name for item in indexed_items])

        return list(zip(mapped_names, indexed_items, strict=True))

    def _find_child(
        self,
        account: Account,
        parent_id: str | None,
        requested_name: str,
    ) -> IndexedFile | None:
        wanted = requested_name.casefold()

        for display_name, item in self._children_for_folder(
            account,
            parent_id,
        ):
            if display_name.casefold() == wanted:
                return item

        return None

    def _resolve(self, file_name: str) -> VirtualNode:
        """Resolve a WinFsp path into a virtual directory or indexed Drive file."""
        path = PureWindowsPath(file_name)

        if not path.parts or path == self._root_path:
            return VirtualNode(
                path=self._root_path,
                kind="directory",
            )

        parts = [part for part in path.parts if part not in ("\\", "/", "")]

        if not parts:
            return VirtualNode(
                path=self._root_path,
                kind="directory",
            )

        account = self._account_by_directory(parts[0])

        if account is None:
            raise NTStatusObjectNameNotFound()

        account_path = self._root_path / self._account_directory_names()[account.account_id]

        if len(parts) == 1:
            return VirtualNode(
                path=account_path,
                kind="directory",
                account=account,
            )

        parent_id: str | None = None
        current_path = account_path

        for index, component in enumerate(parts[1:]):
            current_item = self._find_child(
                account,
                parent_id,
                component,
            )

            if current_item is None:
                raise NTStatusObjectNameNotFound()

            current_path = current_path / _safe_component(component)

            is_folder = current_item.mime_type == GOOGLE_FOLDER_MIME_TYPE
            is_last = index == len(parts[1:]) - 1

            if not is_folder and not is_last:
                raise NTStatusNotADirectory()

            if is_last:
                return VirtualNode(
                    path=current_path,
                    kind="directory" if is_folder else "file",
                    account=account,
                    indexed_file=current_item,
                )

            parent_id = current_item.file_id

        raise NTStatusObjectNameNotFound()

    def _node_file_info(self, node: VirtualNode) -> dict:
        is_directory = node.is_directory

        attributes = (
            FILE_ATTRIBUTE.FILE_ATTRIBUTE_DIRECTORY
            if is_directory
            else FILE_ATTRIBUTE.FILE_ATTRIBUTE_ARCHIVE
        )

        size = 0
        modified_time = None

        if node.indexed_file is not None:
            size = node.indexed_file.size_bytes or 0
            modified_time = node.indexed_file.modified_time

        allocation_size = ((size + 4095) // 4096) * 4096 if size else 0
        timestamp = _filetime_from_iso(modified_time)

        identity = (
            f"{node.account.account_id}:{node.indexed_file.file_id}"
            if node.account and node.indexed_file
            else str(node.path).casefold()
        )

        index_number = int.from_bytes(
            hashlib.sha256(identity.encode()).digest()[:8],
            "little",
        )

        return {
            "file_attributes": attributes,
            "allocation_size": allocation_size,
            "file_size": size,
            "creation_time": timestamp,
            "last_access_time": timestamp,
            "last_write_time": timestamp,
            "change_time": timestamp,
            "index_number": index_number,
        }

    def _directory_entries(self, node: VirtualNode) -> list[dict]:
        if not node.is_directory:
            raise NTStatusNotADirectory()

        entries: list[dict] = []

        if node.path != self._root_path:
            entries.append(
                {
                    "file_name": ".",
                    **self._node_file_info(node),
                }
            )

            parent_node = self._resolve(str(node.path.parent))

            entries.append(
                {
                    "file_name": "..",
                    **self._node_file_info(parent_node),
                }
            )

        if node.path == self._root_path:
            accounts = self._connected_accounts()
            names = self._account_directory_names()

            for account in accounts:
                account_path = self._root_path / names[account.account_id]

                account_node = VirtualNode(
                    path=account_path,
                    kind="directory",
                    account=account,
                )

                entries.append(
                    {
                        "file_name": names[account.account_id],
                        **self._node_file_info(account_node),
                    }
                )

        else:
            account = node.account

            if account is None:
                raise NTStatusObjectNameNotFound()

            parent_id = node.indexed_file.file_id if node.indexed_file is not None else None

            for display_name, item in self._children_for_folder(
                account,
                parent_id,
            ):
                child_node = VirtualNode(
                    path=node.path / display_name,
                    kind=("directory" if item.mime_type == GOOGLE_FOLDER_MIME_TYPE else "file"),
                    account=account,
                    indexed_file=item,
                )

                entries.append(
                    {
                        "file_name": display_name,
                        **self._node_file_info(child_node),
                    }
                )

        entries.sort(key=lambda entry: entry["file_name"].casefold())

        return entries

    def _cache_path(self, item: IndexedFile) -> Path:
        identity = f"{item.account_id}\0{item.file_id}".encode()
        key = hashlib.sha256(identity).hexdigest()

        return self.cache_directory / f"{key}.cache"

    def _cache_metadata_path(self, item: IndexedFile) -> Path:
        return self._cache_path(item).with_suffix(".meta")

    def _ensure_cached(
        self,
        account: Account,
        item: IndexedFile,
    ) -> Path:
        """Download a file when missing or when indexed metadata has changed."""
        if item.mime_type.startswith("application/vnd.google-apps."):
            raise NTStatusAccessDenied()

        target = self._cache_path(item)
        metadata_path = self._cache_metadata_path(item)

        expected_metadata = (
            f"{item.modified_time or ''}\n{item.size_bytes if item.size_bytes is not None else ''}"
        )

        try:
            cached_metadata = metadata_path.read_text(encoding="utf-8")

            cache_valid = target.is_file() and cached_metadata == expected_metadata

            if cache_valid and item.size_bytes is not None:
                cache_valid = target.stat().st_size == item.size_bytes

            if cache_valid:
                return target

        except OSError:
            pass

        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            downloaded = self.download_service.download_file(
                account_id=account.account_id,
                file_id=item.file_id,
                destination=target,
                overwrite=True,
            )

            metadata_path.write_text(
                expected_metadata,
                encoding="utf-8",
            )

            return downloaded

        except (
            DownloadError,
            TokenStoreError,
            OSError,
            ValueError,
        ) as exc:
            logger.exception(
                "Could not cache Google Drive file %s",
                item.file_id,
            )

            raise NTStatusObjectNameNotFound() from exc

    # WinFsp operations

    def get_volume_info(self) -> dict:
        return self._volume_info

    def set_volume_label(self, volume_label: str) -> None:
        raise NTStatusMediaWriteProtected()

    def get_security_by_name(self, file_name: str):
        with self._lock:
            node = self._resolve(file_name)
            descriptor = self._root_security

            return (
                self._node_file_info(node)["file_attributes"],
                descriptor.handle,
                descriptor.size,
            )

    def get_security(self, file_context: OpenedNode):
        return self._root_security

    def open(self, file_name, create_options, granted_access):
        with self._lock:
            node = self._resolve(file_name)

            if (
                create_options & CREATE_FILE_CREATE_OPTIONS.FILE_DIRECTORY_FILE
                and not node.is_directory
            ):
                raise NTStatusNotADirectory()

            return OpenedNode(node=node)

    def close(self, file_context: OpenedNode) -> None:
        file_context.local_path = None

    def get_file_info(self, file_context: OpenedNode) -> dict:
        with self._lock:
            return self._node_file_info(file_context.node)

    def read_directory(
        self,
        file_context: OpenedNode,
        marker: str | None,
    ):
        with self._lock:
            entries = self._directory_entries(file_context.node)

            if marker is None:
                return entries

            for index, entry in enumerate(entries):
                if entry["file_name"] == marker:
                    return entries[index + 1 :]

            return []

    def get_dir_info_by_name(
        self,
        file_context: OpenedNode,
        file_name: str,
    ):
        with self._lock:
            directory = file_context.node

            if not directory.is_directory:
                raise NTStatusNotADirectory()

            for entry in self._directory_entries(directory):
                if entry["file_name"].casefold() == file_name.casefold():
                    return entry

            raise NTStatusObjectNameNotFound()

    def read(
        self,
        file_context: OpenedNode,
        offset: int,
        length: int,
    ) -> bytes:
        node = file_context.node

        if node.is_directory or node.account is None or node.indexed_file is None:
            raise NTStatusAccessDenied()

        if offset < 0 or length < 0:
            raise ValueError("Read offset and length must not be negative.")

        if length == 0:
            return b""

        with self._lock:
            if file_context.local_path is None:
                file_context.local_path = self._ensure_cached(
                    node.account,
                    node.indexed_file,
                )

            try:
                with file_context.local_path.open("rb") as cached_file:
                    cached_file.seek(offset)
                    data = cached_file.read(length)

            except OSError as exc:
                logger.exception("Could not read cached file")
                raise NTStatusObjectNameNotFound() from exc

        if not data and offset >= (node.indexed_file.size_bytes or 0):
            raise NTStatusEndOfFile()

        return data

    def flush(self, file_context: OpenedNode) -> None:
        return None

    def cleanup(self, file_context: OpenedNode, file_name, flags) -> None:
        return None

    def can_delete(self, file_context: OpenedNode, file_name: str) -> None:
        raise NTStatusMediaWriteProtected()

    def create(
        self,
        file_name,
        create_options,
        granted_access,
        file_attributes,
        security_descriptor,
        allocation_size,
    ):
        raise NTStatusMediaWriteProtected()

    def write(
        self,
        file_context,
        buffer,
        offset,
        write_to_end_of_file,
        constrained_io,
    ):
        raise NTStatusMediaWriteProtected()

    def rename(
        self,
        file_context,
        file_name,
        new_file_name,
        replace_if_exists,
    ):
        raise NTStatusMediaWriteProtected()

    def set_file_size(
        self,
        file_context,
        new_size,
        set_allocation_size,
    ):
        raise NTStatusMediaWriteProtected()

    def overwrite(
        self,
        file_context,
        file_attributes,
        replace_file_attributes,
        allocation_size,
    ):
        raise NTStatusMediaWriteProtected()

    def set_basic_info(
        self,
        file_context,
        file_attributes,
        creation_time,
        last_access_time,
        last_write_time,
        change_time,
        file_info,
    ):
        raise NTStatusMediaWriteProtected()

    def set_security(
        self,
        file_context,
        security_information,
        modification_descriptor,
    ):
        raise NTStatusMediaWriteProtected()

    def set_delete(
        self,
        file_context,
        file_name,
        delete_file,
    ):
        raise NTStatusMediaWriteProtected()


def create_google_drive_file_system(
    mountpoint: str,
    database: AccountDatabase,
    file_index: FileIndex,
    download_service: DownloadService,
    debug: bool = False,
    sync_service: DriveSyncService | None = None,
) -> FileSystem:
    """Create the WinFsp filesystem object without starting the mount."""
    operations = GoogleDriveFileSystemOperations(
        database=database,
        file_index=file_index,
        download_service=download_service,
        sync_service=sync_service,
    )

    return FileSystem(
        mountpoint,
        operations,
        sector_size=512,
        sectors_per_allocation_unit=1,
        volume_creation_time=filetime_now(),
        volume_serial_number=1,
        file_info_timeout=1000,
        case_sensitive_search=0,
        case_preserved_names=1,
        unicode_on_disk=1,
        persistent_acls=1,
        post_cleanup_when_modified_only=1,
        um_file_context_is_user_context2=1,
        file_system_name=FILESYSTEM_NAME,
        debug=debug,
    )
