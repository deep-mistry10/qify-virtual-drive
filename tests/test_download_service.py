from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from qify_virtual_drive.download_service import DownloadError, DownloadService
from qify_virtual_drive.file_index import FileIndex, IndexedFile


@pytest.fixture
def download_setup(tmp_path):
    database = MagicMock()
    token_store = MagicMock()
    file_index = FileIndex(tmp_path / "index.sqlite3")

    account = SimpleNamespace(
        account_id="google:account-1",
        google_subject_id="subject-1",
        email="test@example.com",
        status=SimpleNamespace(value="connected"),
    )
    database.get_account.return_value = account

    file_index.upsert(
        IndexedFile(
            account_id="google:account-1",
            file_id="drive-file-1",
            name="hello.txt",
            mime_type="text/plain",
            size_bytes=10,
        )
    )

    service = DownloadService(database, file_index, token_store)
    credentials = MagicMock()
    credentials.valid = True
    credentials.expired = False

    return service, database, token_store, credentials, tmp_path


def test_download_writes_file_without_overwriting(download_setup):
    service, _, _, credentials, tmp_path = download_setup
    destination = tmp_path / "downloaded.txt"

    class FakeDownloader:
        def __init__(self, output, request):
            self.output = output
            self.written = False

        def next_chunk(self):
            if not self.written:
                self.output.write(b"downloaded")
                self.written = True
            return None, True

    drive = MagicMock()

    with (
        patch.object(service, "_get_credentials", return_value=credentials),
        patch("qify_virtual_drive.download_service.build", return_value=drive),
        patch(
            "qify_virtual_drive.download_service.MediaIoBaseDownload",
            FakeDownloader,
        ),
    ):
        result = service.download_file(
            "google:account-1",
            "drive-file-1",
            destination,
        )

    assert result == destination
    assert destination.read_bytes() == b"downloaded"
    drive.files.return_value.get_media.assert_called_once_with(fileId="drive-file-1")


def test_download_refuses_existing_destination(download_setup):
    service, _, _, _, tmp_path = download_setup
    destination = tmp_path / "existing.txt"
    destination.write_text("keep this", encoding="utf-8")

    with pytest.raises(DownloadError, match="already exists"):
        service.download_file(
            "google:account-1",
            "drive-file-1",
            destination,
        )

    assert destination.read_text(encoding="utf-8") == "keep this"


def test_download_rejects_unindexed_file(download_setup):
    service, _, _, _, tmp_path = download_setup

    with pytest.raises(DownloadError, match="not indexed"):
        service.download_file(
            "google:account-1",
            "unknown-file",
            tmp_path / "unknown.txt",
        )


def test_download_rejects_missing_destination_directory(download_setup):
    service, _, _, _, tmp_path = download_setup

    with pytest.raises(DownloadError, match="does not exist"):
        service.download_file(
            "google:account-1",
            "drive-file-1",
            tmp_path / "missing" / "downloaded.txt",
        )


def test_failed_download_does_not_leave_partial_destination(download_setup):
    service, _, _, credentials, tmp_path = download_setup
    destination = tmp_path / "downloaded.txt"

    class FailedDownloader:
        def __init__(self, output, request):
            self.output = output

        def next_chunk(self):
            self.output.write(b"partial")
            raise OSError("simulated network interruption")

    with (
        patch.object(service, "_get_credentials", return_value=credentials),
        patch(
            "qify_virtual_drive.download_service.build",
            return_value=MagicMock(),
        ),
        patch(
            "qify_virtual_drive.download_service.MediaIoBaseDownload",
            FailedDownloader,
        ),
        pytest.raises(DownloadError, match="download failed"),
    ):
        service.download_file(
            "google:account-1",
            "drive-file-1",
            destination,
        )

    assert not destination.exists()
    assert list(tmp_path.glob(".qify-download-*.tmp")) == []
