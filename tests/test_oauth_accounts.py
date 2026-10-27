import json

import pytest

from qify_virtual_drive.account_database import AccountDatabase
from qify_virtual_drive.accounts import AccountError
from qify_virtual_drive.oauth_accounts import OAuthAccountService


class FakeCredentials:
    def to_json(self):
        return json.dumps({"access_token": "test-only-token"})


class FakeTokenStore:
    def __init__(self):
        self.tokens = {}

    def save(self, subject, token_json):
        self.tokens[subject] = token_json


class FakeFlow:
    def run_local_server(self, **kwargs):
        return FakeCredentials()


class FakeUserInfo:
    def __init__(self, identity):
        self.identity = identity

    def get(self):
        return self

    def execute(self):
        return self.identity


class FakeOAuthService:
    def __init__(self, identity):
        self.identity = identity

    def userinfo(self):
        return FakeUserInfo(self.identity)


def make_service(tmp_path, monkeypatch, identity):
    credentials_path = tmp_path / "credentials.json"
    credentials_path.write_text('{"installed": {}}', encoding="utf-8")

    monkeypatch.setattr(
        "qify_virtual_drive.oauth_accounts.InstalledAppFlow.from_client_secrets_file",
        lambda *args, **kwargs: FakeFlow(),
    )
    monkeypatch.setattr(
        "qify_virtual_drive.oauth_accounts.build",
        lambda *args, **kwargs: FakeOAuthService(identity),
    )

    database = AccountDatabase(tmp_path / "accounts.sqlite3")
    token_store = FakeTokenStore()

    service = OAuthAccountService(database, token_store, credentials_path)
    return service, database, token_store


def identity(number):
    return {
        "id": f"google-id-{number}",
        "email": f"user{number}@example.com",
        "verified_email": True,
    }


def test_connect_registers_account_and_token(tmp_path, monkeypatch):
    service, database, tokens = make_service(tmp_path, monkeypatch, identity(1))

    account = service.connect_account()

    assert account.email == "user1@example.com"
    assert database.get_account(account.account_id) == account
    assert "google-id-1" in tokens.tokens


def test_duplicate_account_is_rejected_without_overwriting_token(tmp_path, monkeypatch):
    service, database, tokens = make_service(tmp_path, monkeypatch, identity(1))
    service.connect_account()
    original_token = tokens.tokens["google-id-1"]

    with pytest.raises(AccountError, match="already registered"):
        service.connect_account()

    assert len(database.list_accounts()) == 1
    assert tokens.tokens["google-id-1"] == original_token


def test_fifth_account_is_rejected(tmp_path, monkeypatch):
    service, database, tokens = make_service(tmp_path, monkeypatch, identity(5))

    for number in range(1, 5):
        from qify_virtual_drive.accounts import Account

        database.add_account(
            Account(
                account_id=f"google:google-id-{number}",
                email=f"user{number}@example.com",
                google_subject_id=f"google-id-{number}",
            )
        )

    with pytest.raises(AccountError, match="slots are in use"):
        service.connect_account()

    assert len(database.list_accounts()) == 4
    assert tokens.tokens == {}
