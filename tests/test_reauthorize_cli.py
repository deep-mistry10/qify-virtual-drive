from unittest.mock import Mock

from qify_virtual_drive.account_cli import build_parser, main
from qify_virtual_drive.accounts import Account, AccountStatus


def make_account():
    return Account(
        account_id="google:test-subject",
        email="test@example.com",
        status=AccountStatus.CONNECTED,
        google_subject_id="test-subject",
    )


def test_parser_accepts_reauthorize():
    args = build_parser().parse_args(["reauthorize"])

    assert args.command == "reauthorize"


def test_reauthorize_uses_selected_account(monkeypatch, capsys):
    account = make_account()

    database = Mock()
    database.list_accounts.return_value = [account]

    oauth_service = Mock()
    oauth_service.reauthorize_account.return_value = account

    monkeypatch.setattr(
        "qify_virtual_drive.account_cli.AccountDatabase",
        lambda: database,
    )
    monkeypatch.setattr(
        "qify_virtual_drive.account_cli.create_oauth_service",
        lambda db: oauth_service,
    )
    monkeypatch.setattr("builtins.input", lambda _: "1")
    monkeypatch.setattr(
        "sys.argv",
        ["qify-vdrive", "reauthorize"],
    )

    result = main()

    assert result == 0
    oauth_service.reauthorize_account.assert_called_once_with("google:test-subject")
    assert "Authorization updated" in capsys.readouterr().out


def test_reauthorize_rejects_invalid_selection(monkeypatch, capsys):
    account = make_account()

    database = Mock()
    database.list_accounts.return_value = [account]

    monkeypatch.setattr(
        "qify_virtual_drive.account_cli.AccountDatabase",
        lambda: database,
    )
    monkeypatch.setattr("builtins.input", lambda _: "99")
    monkeypatch.setattr(
        "sys.argv",
        ["qify-vdrive", "reauthorize"],
    )

    result = main()

    assert result == 1
    assert "Invalid account selection" in capsys.readouterr().err


def test_reauthorize_handles_no_accounts(monkeypatch, capsys):
    database = Mock()
    database.list_accounts.return_value = []

    monkeypatch.setattr(
        "qify_virtual_drive.account_cli.AccountDatabase",
        lambda: database,
    )
    monkeypatch.setattr(
        "sys.argv",
        ["qify-vdrive", "reauthorize"],
    )

    result = main()

    assert result == 1
    assert "No registered accounts" in capsys.readouterr().out
