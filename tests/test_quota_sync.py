
import json

import pytest

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.accounts import Account
from qify_virtual_drive.quota_sync import QuotaSyncError, QuotaSyncService
from qify_virtual_drive.storage_quota import QuotaStore


class FakeCredentials:
    expired = False
    refresh_token = None
    valid = True


class FakeTokenStore:
    def load(self, subject):
        return json.dumps({"token": "test-token"})

    def save(self, subject, token_json):
        pass


class FakeRequest:
    def __init__(self, response):
        self.response = response

    def execute(self):
        return self.response


class FakeAbout:
    def __init__(self, response):
        self.response = response

    def get(self, **kwargs):
        assert "storageQuota" in kwargs["fields"]
        return FakeRequest(self.response)


class FakeDriveService:
    def __init__(self, response):
        self.response = response

    def about(self):
        return FakeAbout(self.response)


def make_service(tmp_path, monkeypatch, response):
    database_path = tmp_path / "accounts.sqlite3"
    database = AccountDatabase(database_path)

    database.add_account(
        Account(
            account_id="google:subject-1",
            email="user@example.com",
            google_subject_id="subject-1",
        )
    )

    quota_store = QuotaStore(database_path)

    monkeypatch.setattr(
        "qify_virtual_drive.quota_sync.Credentials.from_authorized_user_info",
        lambda *args, **kwargs: FakeCredentials(),
    )
    monkeypatch.setattr(
        "qify_virtual_drive.quota_sync.build",
        lambda *args, **kwargs: FakeDriveService(response),
    )

    service = QuotaSyncService(database, quota_store, FakeTokenStore())
    return service, quota_store


def test_quota_sync_saves_real_api_values(tmp_path, monkeypatch):
    service, store = make_service(
        tmp_path,
        monkeypatch,
        {
            "storageQuota": {
                "limit": "15000000000",
                "usage": "4000000000",
            }
        },
    )

    result = service.sync_account("google:subject-1")

    assert result.total_bytes == 15_000_000_000
    assert result.used_bytes == 4_000_000_000
    assert store.get("google:subject-1") == result


def test_quota_sync_rejects_unknown_account(tmp_path):
    database_path = tmp_path / "accounts.sqlite3"
    database = AccountDatabase(database_path)

    service = QuotaSyncService(
        database,
        QuotaStore(database_path),
        FakeTokenStore(),
    )

    with pytest.raises(QuotaSyncError, match="Unknown account"):
        service.sync_account("missing")


def test_missing_quota_limit_is_not_saved(tmp_path, monkeypatch):
    service, store = make_service(
        tmp_path,
        monkeypatch,
        {"storageQuota": {"usage": "4000"}},
    )

    with pytest.raises(QuotaSyncError, match="did not report"):
        service.sync_account("google:subject-1")

    assert store.get("google:subject-1") is None


def test_invalid_quota_value_is_rejected(tmp_path, monkeypatch):
    service, store = make_service(
        tmp_path,
        monkeypatch,
        {"storageQuota": {"limit": "invalid", "usage": "4000"}},
    )

    with pytest.raises(QuotaSyncError, match="invalid quota values"):
        service.sync_account("google:subject-1")

    assert store.get("google:subject-1") is None
