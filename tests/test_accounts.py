import pytest

from qify_virtual_drive.accounts import (
    Account,
    AccountError,
    AccountRegistry,
    AccountStatus,
)


def make_account(number: int) -> Account:
    return Account(
        account_id=f"account-{number}",
        email=f"user{number}@example.com",
    )


def test_registry_starts_empty():
    registry = AccountRegistry()
    assert registry.list_accounts() == []


def test_add_account():
    registry = AccountRegistry()
    account = make_account(1)

    registry.add_account(account)

    assert registry.get_account("account-1") == account
    assert len(registry.list_accounts()) == 1


def test_allows_four_accounts():
    registry = AccountRegistry()

    for number in range(1, 5):
        registry.add_account(make_account(number))

    assert len(registry.list_accounts()) == 4


def test_rejects_fifth_account():
    registry = AccountRegistry()

    for number in range(1, 5):
        registry.add_account(make_account(number))

    with pytest.raises(AccountError, match="maximum"):
        registry.add_account(make_account(5))


def test_rejects_duplicate_account_id():
    registry = AccountRegistry()
    registry.add_account(make_account(1))

    duplicate_id = Account(
        account_id="account-1",
        email="another@example.com",
    )

    with pytest.raises(AccountError, match="ID"):
        registry.add_account(duplicate_id)


def test_rejects_duplicate_email_case_insensitively():
    registry = AccountRegistry()
    registry.add_account(make_account(1))

    duplicate_email = Account(
        account_id="account-2",
        email="USER1@EXAMPLE.COM",
    )

    with pytest.raises(AccountError, match="already registered"):
        registry.add_account(duplicate_email)


def test_update_account_status():
    registry = AccountRegistry()
    registry.add_account(make_account(1))

    updated = registry.update_status(
        "account-1",
        AccountStatus.NEEDS_REAUTH,
    )

    assert updated.status == AccountStatus.NEEDS_REAUTH
    assert updated.email == "user1@example.com"


def test_remove_account_does_not_affect_other_accounts():
    registry = AccountRegistry()
    registry.add_account(make_account(1))
    registry.add_account(make_account(2))

    removed = registry.remove_account("account-1")

    assert removed.account_id == "account-1"
    assert registry.get_account("account-1") is None
    assert registry.get_account("account-2") is not None


def test_rejects_removing_unknown_account():
    registry = AccountRegistry()

    with pytest.raises(AccountError, match="not found"):
        registry.remove_account("missing")
