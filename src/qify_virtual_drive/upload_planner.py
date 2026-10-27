"""Plan safe upload destinations across connected Google Drive accounts."""

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.storage_quota import QuotaStore, UploadAllocator


class UploadPlanningError(RuntimeError):
    """Raised when an upload destination cannot be selected."""


class UploadPlanner:
    """Choose a connected account with sufficient known storage."""

    def __init__(
        self,
        database: AccountDatabase,
        quota_store: QuotaStore,
    ) -> None:
        self.database = database
        self.allocator = UploadAllocator(quota_store)

    def choose_account(self, required_bytes: int) -> str:
        if isinstance(required_bytes, bool) or not isinstance(required_bytes, int):
            raise UploadPlanningError("Required file size must be an integer number of bytes.")

        if required_bytes < 0:
            raise UploadPlanningError("Required file size cannot be negative.")

        accounts = self.database.list_accounts()

        connected_ids = [
            account.account_id
            for account in accounts
            if getattr(account.status, "value", account.status) == "connected"
        ]

        if not connected_ids:
            raise UploadPlanningError("No connected Google Drive accounts are available.")

        try:
            return self.allocator.choose_account(
                file_size_bytes=required_bytes,
                connected_account_ids=connected_ids,
            )
        except ValueError as exc:
            raise UploadPlanningError(str(exc)) from exc
