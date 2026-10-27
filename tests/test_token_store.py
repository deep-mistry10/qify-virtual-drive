import json

import pytest

from qify_virtual_drive.token_store import TokenStore


@pytest.fixture
def memory_keyring(monkeypatch):
    """Replace the real OS credential store with an isolated test dictionary."""
    values = {}

    def set_password(service, username, password):
        values[(service, username)] = password

    def get_password(service, username):
        return values.get((service, username))

    def delete_password(service, username):
        if (service, username) not in values:
            import keyring.errors

            raise keyring.errors.PasswordDeleteError("Credential not found")
        del values[(service, username)]

    monkeypatch.setattr("qify_virtual_drive.token_store.keyring.set_password", set_password)
    monkeypatch.setattr("qify_virtual_drive.token_store.keyring.get_password", get_password)
    monkeypatch.setattr("qify_virtual_drive.token_store.keyring.delete_password", delete_password)
    return values


def test_save_and_load_token(memory_keyring):
    store = TokenStore()
    original = json.dumps({"refresh_token": "test-only-token"})

    store.save("google-id-1", original)

    loaded = json.loads(store.load("google-id-1"))
    assert loaded["refresh_token"] == "test-only-token"


def test_accounts_have_separate_tokens(memory_keyring):
    store = TokenStore()
    store.save("google-id-1", '{"access_token":"token-one"}')
    store.save("google-id-2", '{"access_token":"token-two"}')

    assert json.loads(store.load("google-id-1"))["access_token"] == "token-one"
    assert json.loads(store.load("google-id-2"))["access_token"] == "token-two"


def test_missing_token_returns_none(memory_keyring):
    assert TokenStore().load("google-id-missing") is None


def test_delete_removes_only_selected_account(memory_keyring):
    store = TokenStore()
    store.save("google-id-1", '{"access_token":"one"}')
    store.save("google-id-2", '{"access_token":"two"}')

    store.delete("google-id-1")

    assert store.load("google-id-1") is None
    assert store.load("google-id-2") is not None


def test_invalid_token_json_is_rejected(memory_keyring):
    with pytest.raises(ValueError):
        TokenStore().save("google-id-1", "not-json")
