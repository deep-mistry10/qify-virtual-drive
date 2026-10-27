
import pytest

from qify_virtual_drive.accounts import Account, AccountError, AccountStatus
from qify_virtual_drive.account_database import AccountDatabase


def make_account(number: int, subject: str | None = None) -> Account:
    return Account(
        account_id=f"account-{number}",
        email=f"user{number}@example.com",
        google_subject_id=subject or f"google-subject-{number}",
    )


def test_accounts_persist_after_reopening(tmp_path):
    database_path = tmp_path / "accounts.sqlite3"

    first = AccountDatabase(database_path)
    first.add_account(make_account(1))

    reopened = AccountDatabase(database_path)
    assert reopened.get_account("account-1") == make_account(1)


def test_database_rejects_fifth_account(tmp_path):
    database = AccountDatabase(tmp_path / "accounts.sqlite3")

    for number in range(1, 5):
        database.add_account(make_account(number))

    with pytest.raises(AccountError, match="maximum"):
        database.add_account(make_account(5))


def test_database_rejects_duplicate_email(tmp_path):
    database = AccountDatabase(tmp_path / "accounts.sqlite3")
    database.add_account(make_account(1))

    duplicate = Account(
        account_id="account-other",
        email="USER1@EXAMPLE.COM",
        google_subject_id="different-subject",
    )

    with pytest.raises(AccountError, match="already registered"):
        database.add_account(duplicate)


def test_database_rejects_duplicate_google_identity(tmp_path):
    database = AccountDatabase(tmp_path / "accounts.sqlite3")
    database.add_account(make_account(1, "same-google-id"))

    with pytest.raises(AccountError, match="already registered"):
        database.add_account(make_account(2, "same-google-id"))


def test_database_updates_status(tmp_path):
    database = AccountDatabase(tmp_path / "accounts.sqlite3")
    database.add_account(make_account(1))

    updated = database.update_status(
        "account-1",
        AccountStatus.NEEDS_REAUTH,
    )

    assert updated.status == AccountStatus.NEEDS_REAUTH
    assert database.get_account("account-1").status == AccountStatus.NEEDS_REAUTH


def test_removal_only_removes_local_metadata(tmp_path):
    database = AccountDatabase(tmp_path / "accounts.sqlite3")
    database.add_account(make_account(1))

    removed = database.remove_account("account-1")

    assert removed.account_id == "account-1"
    assert database.get_account("account-1") is None
    assert (tmp_path / "accounts.sqlite3").exists()
