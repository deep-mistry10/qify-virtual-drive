from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.accounts import Account
from qify_virtual_drive.drive_sync import DriveSyncService
from qify_virtual_drive.file_index import FileIndex


class FakeCredentials:
    expired = False
    refresh_token = None
    valid = True


class FakeTokenStore:
    def load(self, subject):
        return '{"token":"fake","refresh_token":"fake-refresh"}'

    def save(self, subject, token_json):
        pass


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeFiles:
    def __init__(self):
        self.calls = []

    def list(self, **kwargs):
        self.calls.append(kwargs)

        if kwargs.get("pageToken") is None:
            return FakeRequest(
                {
                    "files": [
                        {
                            "id": "file-1",
                            "name": "report.txt",
                            "mimeType": "text/plain",
                            "size": "120",
                            "parents": ["folder-1"],
                            "modifiedTime": "2026-10-27T08:00:00Z",
                        }
                    ],
                    "nextPageToken": "page-2",
                }
            )

        return FakeRequest(
            {
                "files": [
                    {
                        "id": "file-2",
                        "name": "image.png",
                        "mimeType": "image/png",
                        "size": "250",
                    }
                ]
            }
        )


class FakeDriveService:
    def __init__(self):
        self.fake_files = FakeFiles()

    def files(self):
        return self.fake_files


def test_sync_indexes_files_across_pages(tmp_path, monkeypatch):
    database = AccountDatabase(tmp_path / "accounts.sqlite3")
    database.add_account(
        Account(
            account_id="google:subject-1",
            email="user@example.com",
            google_subject_id="subject-1",
        )
    )

    index = FileIndex(tmp_path / "accounts.sqlite3")
    fake_service = FakeDriveService()

    monkeypatch.setattr(
        "qify_virtual_drive.drive_sync.Credentials.from_authorized_user_info",
        lambda *args, **kwargs: FakeCredentials(),
    )
    monkeypatch.setattr(
        "qify_virtual_drive.drive_sync.build",
        lambda *args, **kwargs: fake_service,
    )

    sync = DriveSyncService(database, index, FakeTokenStore())
    count = sync.sync_account("google:subject-1")

    assert count == 2
    assert index.get("google:subject-1", "file-1").name == "report.txt"
    assert index.get("google:subject-1", "file-1").parent_id == "folder-1"
    assert index.get("google:subject-1", "file-2").name == "image.png"
    assert len(fake_service.fake_files.calls) == 2


def test_sync_rejects_unknown_account(tmp_path):
    database = AccountDatabase(tmp_path / "accounts.sqlite3")
    index = FileIndex(tmp_path / "accounts.sqlite3")

    sync = DriveSyncService(database, index, FakeTokenStore())

    import pytest

    from qify_virtual_drive.drive_sync import DriveSyncError

    with pytest.raises(DriveSyncError, match="Unknown account"):
        sync.sync_account("missing")
