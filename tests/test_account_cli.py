from qify_virtual_drive.account_cli import build_parser


def test_parser_accepts_list():
    args = build_parser().parse_args(["list"])
    assert args.command == "list"


def test_parser_accepts_connect():
    args = build_parser().parse_args(["connect"])
    assert args.command == "connect"


def test_parser_rejects_unknown_command():
    import pytest

    with pytest.raises(SystemExit):
        build_parser().parse_args(["delete-everything"])


def test_parser_accepts_files():
    args = build_parser().parse_args(["files"])
    assert args.command == "files"
