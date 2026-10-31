import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import pytest

from qify_virtual_drive.write_journal import WriteJournal, WriteJournalError


def make_journal(tmp_path: Path) -> WriteJournal:
    return WriteJournal(tmp_path / "staging", tmp_path / "journal.sqlite3")


def test_stage_write_queue_and_complete(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    item = journal.begin_write("hello.txt", parent_id="folder-1")
    assert item.state == "editing"
    journal.write(item.operation_id, b"world", 0)
    journal.write(item.operation_id, b"hello ", 0)
    queued = journal.queue(item.operation_id)
    assert queued.state == "queued"
    assert queued.local_path.read_bytes() == b"hello "
    journal.mark_uploading(item.operation_id)
    journal.mark_uploaded(item.operation_id)
    assert journal.list_pending() == []
    assert not queued.local_path.exists()


def test_existing_file_keeps_owner_identity(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    item = journal.begin_write(
        "existing.bin", account_id="account-A", file_id="drive-file-7"
    )
    assert item.account_id == "account-A"
    assert item.file_id == "drive-file-7"


def test_partial_existing_identity_is_rejected(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    with pytest.raises(ValueError):
        journal.begin_write("bad.bin", account_id="account-A")


def test_interrupted_edit_is_recovered_as_queued(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    db = tmp_path / "journal.sqlite3"
    first = WriteJournal(root, db)
    item = first.begin_write("recovered.txt")
    first.write(item.operation_id, b"preserve me", 0)

    restarted = WriteJournal(root, db)
    recovered = restarted.get(item.operation_id)
    assert recovered.state == "queued"
    assert recovered.local_path.read_bytes() == b"preserve me"


def test_failed_write_is_preserved_and_retryable(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    item = journal.begin_write("retry.txt")
    journal.write(item.operation_id, b"data", 0)
    journal.queue(item.operation_id)
    journal.mark_uploading(item.operation_id)
    failed = journal.mark_failed(item.operation_id, "network unavailable")
    assert failed.state == "failed"
    assert failed.local_path.read_bytes() == b"data"
    assert journal.mark_uploading(item.operation_id).state == "uploading"


def test_cannot_modify_after_queued(tmp_path: Path) -> None:
    journal = make_journal(tmp_path)
    item = journal.begin_write("locked.txt")
    journal.queue(item.operation_id)
    with pytest.raises(WriteJournalError):
        journal.write(item.operation_id, b"x", 0)


def test_interrupted_upload_requires_reconciliation(tmp_path: Path) -> None:
    root = tmp_path / "staging"
    db = tmp_path / "journal.sqlite3"
    first = WriteJournal(root, db)
    item = first.begin_write("uncertain.txt")
    first.write(item.operation_id, b"maybe uploaded", 0)
    first.queue(item.operation_id)
    first.mark_uploading(item.operation_id)

    restarted = WriteJournal(root, db)
    recovered = restarted.get(item.operation_id)
    assert recovered.state == "review"
    assert "outcome is unknown" in (recovered.last_error or "")
    assert recovered.local_path.read_bytes() == b"maybe uploaded"
