from unittest.mock import MagicMock, patch

import pytest

from qify_virtual_drive.upload_service import UploadError, UploadService


@pytest.fixture
def upload_setup(tmp_path):
    """Create an upload service with mocked dependencies."""
    database = MagicMock()
    file_index = MagicMock()
    quota_store = MagicMock()
    token_store = MagicMock()

    service = UploadService(
        database=database,
        file_index=file_index,
        quota_store=quota_store,
        token_store=token_store,
    )

    service.planner = MagicMock()
    service.planner.choose_account.return_value = "google:account-1"

    account = MagicMock()
    account.account_id = "google:account-1"
    account.google_subject_id = "subject-1"
    account.status.value = "connected"

    database.get_account.return_value = account
    token_store.load.return_value = '{"token": "fake-token"}'

    credentials = MagicMock()
    credentials.valid = True
    credentials.expired = False

    return service, database, file_index, token_store, credentials


def test_upload_rejects_missing_file(upload_setup, tmp_path):
    service, *_ = upload_setup
    missing_file = tmp_path / "missing.txt"

    with pytest.raises(UploadError):
        service.upload_file(missing_file)


def test_upload_rejects_directory(upload_setup, tmp_path):
    service, *_ = upload_setup

    with pytest.raises(UploadError):
        service.upload_file(tmp_path)


def test_upload_rejects_symbolic_link(upload_setup, tmp_path):
    service, *_ = upload_setup
    target = tmp_path / "target.txt"
    target.write_text("test", encoding="utf-8")

    link = tmp_path / "link.txt"

    try:
        link.symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symbolic links are not available in this environment")

    with pytest.raises(UploadError):
        service.upload_file(link)


def test_upload_uses_planner_and_indexes_result(upload_setup, tmp_path):
    service, database, file_index, token_store, credentials = upload_setup

    source = tmp_path / "example.txt"
    source.write_text("hello QIFY", encoding="utf-8")

    response = {
        "id": "drive-file-123",
        "name": "example.txt",
        "mimeType": "text/plain",
        "size": "10",
        "modifiedTime": "2026-10-27T10:00:00.000Z",
        "parents": ["parent-123"],
        "trashed": False,
    }

    drive = MagicMock()
    drive.files.return_value.create.return_value.execute.return_value = response

    with (
        patch.object(service, "_credentials_for", return_value=credentials),
        patch(
            "qify_virtual_drive.upload_service.build",
            return_value=drive,
        ),
        patch(
            "qify_virtual_drive.upload_service.MediaFileUpload",
        ) as media_upload,
    ):
        result = service.upload_file(source, parent_id="parent-123")

    service.planner.choose_account.assert_called_once_with(10)
    database.get_account.assert_called_with("google:account-1")
    token_store.load.assert_not_called()
    media_upload.assert_called_once()

    drive.files.return_value.create.assert_called_once()
    file_index.upsert.assert_called_once_with(result)

    assert result.account_id == "google:account-1"
    assert result.file_id == "drive-file-123"
    assert result.name == "example.txt"
    assert result.parent_id == "parent-123"
    assert result.size_bytes == 10


def test_upload_fails_when_planner_has_no_account(upload_setup, tmp_path):
    service, *_ = upload_setup
    source = tmp_path / "example.txt"
    source.write_text("hello", encoding="utf-8")
    service.planner.choose_account.side_effect = RuntimeError(
        "No account has enough available storage"
    )

    with pytest.raises((UploadError, RuntimeError)):
        service.upload_file(source)


def test_upload_fails_when_google_returns_no_file_id(upload_setup, tmp_path):
    service, _, _, _, credentials = upload_setup
    source = tmp_path / "example.txt"
    source.write_text("hello", encoding="utf-8")

    drive = MagicMock()
    drive.files.return_value.create.return_value.execute.return_value = {
        "name": "example.txt",
        "mimeType": "text/plain",
    }

    with (
        patch.object(service, "_credentials_for", return_value=credentials),
        patch(
            "qify_virtual_drive.upload_service.build",
            return_value=drive,
        ),
        patch(
            "qify_virtual_drive.upload_service.MediaFileUpload",
        ),
        pytest.raises(UploadError),
    ):
        service.upload_file(source)
