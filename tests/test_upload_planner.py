import pytest

from qify_virtual_drive.accounts import Account, AccountStatus
from qify_virtual_drive.storage_quota import QuotaStore, StorageQuota
from qify_virtual_drive.upload_planner import (
    UploadPlanner,
    UploadPlanningError,
)


class FakeAccountDatabase:
    """Provide account records without changing the real account database."""

    def __init__(self, accounts):
        self._accounts = accounts

    def list_accounts(self):
        return self._accounts


def make_account(account_id, status=AccountStatus.CONNECTED):
    return Account(
        account_id=account_id,
        email=f"{account_id}@example.com",
        status=status,
        google_subject_id=f"subject-{account_id}",
    )


def save_quota(store, account_id, total_bytes, used_bytes):
    store.save(
        StorageQuota(
            account_id=account_id,
            total_bytes=total_bytes,
            used_bytes=used_bytes,
            checked_at="2026-10-27T00:00:00+00:00",
        )
    )


def make_planner(tmp_path, accounts):
    database = FakeAccountDatabase(accounts)
    quota_store = QuotaStore(tmp_path / "quotas.sqlite3")
    return UploadPlanner(database, quota_store), quota_store


def test_chooses_account_with_most_available_space(tmp_path):
    planner, store = make_planner(
        tmp_path,
        [make_account("account-a"), make_account("account-b")],
    )
    save_quota(store, "account-a", 1_000, 200)
    save_quota(store, "account-b", 2_000, 1_000)

    assert planner.choose_account(500) == "account-b"


def test_ignores_disconnected_account(tmp_path):
    planner, store = make_planner(
        tmp_path,
        [
            make_account("connected"),
            make_account("disconnected", AccountStatus.DISCONNECTED),
        ],
    )
    save_quota(store, "connected", 1_000, 500)
    save_quota(store, "disconnected", 10_000, 0)

    assert planner.choose_account(100) == "connected"


def test_missing_quota_is_not_selected(tmp_path):
    planner, store = make_planner(
        tmp_path,
        [make_account("known"), make_account("unknown")],
    )
    save_quota(store, "known", 1_000, 900)

    with pytest.raises(UploadPlanningError, match="sufficient available space"):
        planner.choose_account(200)


def test_rejects_file_larger_than_available_storage(tmp_path):
    planner, store = make_planner(
        tmp_path,
        [make_account("account-a")],
    )
    save_quota(store, "account-a", 1_000, 900)

    with pytest.raises(UploadPlanningError, match="sufficient available space"):
        planner.choose_account(200)


@pytest.mark.parametrize("file_size", [-1, 1.5, True, "100"])
def test_rejects_invalid_file_size(tmp_path, file_size):
    planner, _ = make_planner(
        tmp_path,
        [make_account("account-a")],
    )

    with pytest.raises(UploadPlanningError):
        planner.choose_account(file_size)
