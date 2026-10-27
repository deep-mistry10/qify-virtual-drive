import pytest

from qify_virtual_drive.storage_quota import (
    QuotaStore,
    StorageQuota,
    UploadAllocator,
)


def quota(account_id, total, used):
    return StorageQuota(
        account_id=account_id,
        total_bytes=total,
        used_bytes=used,
        checked_at="2026-10-27T12:00:00Z",
    )


def test_quota_calculates_available_space():
    item = quota("account-1", 1000, 250)

    assert item.available_bytes == 750
    assert item.usage_percent == 25.0


def test_quota_store_persists_across_instances(tmp_path):
    path = tmp_path / "quotas.sqlite3"
    first = QuotaStore(path)
    first.save(quota("account-1", 1000, 400))

    second = QuotaStore(path)

    assert second.get("account-1").available_bytes == 600


def test_quota_update_replaces_previous_snapshot(tmp_path):
    store = QuotaStore(tmp_path / "quotas.sqlite3")
    store.save(quota("account-1", 1000, 400))
    store.save(quota("account-1", 1000, 700))

    assert store.get("account-1").available_bytes == 300


def test_allocator_chooses_account_with_most_space(tmp_path):
    store = QuotaStore(tmp_path / "quotas.sqlite3")
    store.save(quota("account-1", 1000, 800))
    store.save(quota("account-2", 2000, 500))

    allocator = UploadAllocator(store)

    assert allocator.choose_account(400, ["account-1", "account-2"]) == "account-2"


def test_allocator_rejects_insufficient_space(tmp_path):
    store = QuotaStore(tmp_path / "quotas.sqlite3")
    store.save(quota("account-1", 1000, 950))

    with pytest.raises(ValueError, match="No account"):
        UploadAllocator(store).choose_account(100, ["account-1"])


def test_allocator_skips_unknown_quota(tmp_path):
    store = QuotaStore(tmp_path / "quotas.sqlite3")
    store.save(quota("account-2", 1000, 100))

    result = UploadAllocator(store).choose_account(200, ["account-1", "account-2"])

    assert result == "account-2"


def test_allocator_rejects_negative_file_size(tmp_path):
    allocator = UploadAllocator(QuotaStore(tmp_path / "quotas.sqlite3"))

    with pytest.raises(ValueError, match="negative"):
        allocator.choose_account(-1, ["account-1"])


def test_quota_store_rejects_negative_usage(tmp_path):
    store = QuotaStore(tmp_path / "quotas.sqlite3")

    with pytest.raises(ValueError, match="negative"):
        store.save(quota("account-1", 1000, -1))
