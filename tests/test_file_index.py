
import pytest

from qify_virtual_drive.file_index import FileIndex, IndexedFile


def make_file(account_id, file_id, name="report.txt"):
    return IndexedFile(
        account_id=account_id,
        file_id=file_id,
        name=name,
        mime_type="text/plain",
        size_bytes=100,
    )


def test_upsert_and_get_file(tmp_path):
    index = FileIndex(tmp_path / "index.sqlite3")
    item = make_file("account-1", "file-1")

    index.upsert(item)

    assert index.get("account-1", "file-1") == item


def test_same_name_from_different_accounts_stays_separate(tmp_path):
    index = FileIndex(tmp_path / "index.sqlite3")
    index.upsert(make_file("account-1", "file-a"))
    index.upsert(make_file("account-2", "file-b"))

    results = index.list_files()

    assert len(results) == 2
    assert {item.account_id for item in results} == {"account-1", "account-2"}


def test_same_file_id_from_different_accounts_stays_separate(tmp_path):
    index = FileIndex(tmp_path / "index.sqlite3")
    index.upsert(make_file("account-1", "shared-id"))
    index.upsert(make_file("account-2", "shared-id"))

    assert index.get("account-1", "shared-id") is not None
    assert index.get("account-2", "shared-id") is not None


def test_upsert_updates_metadata_without_changing_owner(tmp_path):
    index = FileIndex(tmp_path / "index.sqlite3")
    index.upsert(make_file("account-1", "file-1", "old.txt"))
    index.upsert(make_file("account-1", "file-1", "new.txt"))

    result = index.get("account-1", "file-1")

    assert result.name == "new.txt"
    assert result.account_id == "account-1"


def test_parent_listing_is_scoped_to_account(tmp_path):
    index = FileIndex(tmp_path / "index.sqlite3")
    index.upsert(
        IndexedFile("account-1", "file-1", "one.txt", "text/plain", "folder-1")
    )
    index.upsert(
        IndexedFile("account-2", "file-2", "two.txt", "text/plain", "folder-1")
    )

    results = index.list_files(account_id="account-1", parent_id="folder-1")

    assert len(results) == 1
    assert results[0].name == "one.txt"


def test_trashed_files_are_hidden_by_default(tmp_path):
    index = FileIndex(tmp_path / "index.sqlite3")
    index.upsert(
        IndexedFile(
            "account-1", "file-1", "old.txt", "text/plain", trashed=True
        )
    )

    assert index.list_files() == []
    assert len(index.list_files(include_trashed=True)) == 1


def test_negative_file_size_is_rejected(tmp_path):
    index = FileIndex(tmp_path / "index.sqlite3")

    with pytest.raises(ValueError, match="negative"):
        index.upsert(make_file("account-1", "file-1").__class__(
            "account-1", "file-1", "bad.txt", "text/plain", size_bytes=-1
        ))
