import json

from steph_email import cli
from tests.conftest import NOW


def test_cli_accounts_sync_inbox_send(engine, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_engine", lambda s=None: engine)
    assert cli.main(["accounts", "add", "me@gmail.com", "--secret", "pw", "--name", "Michael"]) == 0
    assert "connected me@gmail.com" in capsys.readouterr().out
    assert cli.main(["accounts", "add", "broken@gmail.com", "--secret", "pw"]) == 1

    assert cli.main(["sync"]) == 0
    out = capsys.readouterr().out
    assert "+7" in out and "urgent: 1" in out

    assert cli.main(["urgent", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows and rows[0]["urgency"] == "critical"

    assert cli.main(["inbox", "--urgency", "high", "--needs-reply"]) == 0
    assert "change order" in capsys.readouterr().out.lower()

    assert cli.main(["search", "supplyco"]) == 0
    assert "Invoice #4471" in capsys.readouterr().out

    assert cli.main(["show", str(rows[0]["id"])]) == 0
    out = capsys.readouterr().out
    assert "Urgency: critical" in out and "Original:" in out
    assert cli.main(["show", "999"]) == 1

    assert cli.main(["send", "--from", "me@gmail.com", "--to", "a@b.com", "--subject", "Yo", "--body", "hi", "--due-days", "2"]) == 0
    assert "tracking reply #1" in capsys.readouterr().out
    assert cli.main(["accounts", "list"]) == 0 and cli.main(["accounts", "providers"]) == 0
    assert cli.main(["accounts", "remove", "me@gmail.com"]) == 0


def test_cli_status_replies_brief_cleanup(engine, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_engine", lambda s=None: engine)
    assert cli.main(["status"]) == 0
    assert "ACTIVATION PENDING" in capsys.readouterr().out
    cli.main(["accounts", "add", "me@gmail.com", "--secret", "pw"]); cli.main(["sync"]); capsys.readouterr()

    assert cli.main(["replies"]) == 0
    out = capsys.readouterr().out
    assert "WAITING ON THEM" in out and "NEEDS YOUR REPLY" in out and "change order" in out.lower()
    cli.main(["send", "--from", "me@gmail.com", "--to", "sam@vendor.com", "--subject", "COI", "--body", "pls"]); capsys.readouterr()
    assert cli.main(["replies", "nudge", "1"]) == 0
    assert "Following up" in capsys.readouterr().out
    assert cli.main(["replies", "close", "1"]) == 0; capsys.readouterr()

    assert cli.main(["brief", "--print", "--deliver"]) == 0
    out = capsys.readouterr().out
    assert "# Email brief" in out and "spoken script" in out and "---- sms: ok simulated [STAGED]" in out
    assert cli.main(["brief", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["brief_date"]

    assert cli.main(["cleanup"]) == 0
    assert "2 candidates" in capsys.readouterr().out
    assert cli.main(["cleanup", "--run"]) == 0
    assert "archived 2" in capsys.readouterr().out

    assert cli.main(["notify", "test-sms"]) == 0
    assert "[STAGED]" in capsys.readouterr().out
    assert cli.main(["voice", "verify"]) == 2
    assert "NOT VERIFIED" in capsys.readouterr().out
    assert cli.main(["status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["mailboxes"]["authorized"] is True


def test_cli_parser_shapes():
    p = cli.build_parser()
    args = p.parse_args(["brief", "--date", "2026-09-14", "--voice"])
    assert args.date == "2026-09-14" and args.voice is True and args.deliver is False
    args = p.parse_args(["send", "--from", "a@b.com", "--to", "c@d.com", "--no-track"])
    assert args.no_track and args.sender == "a@b.com"
    args = p.parse_args(["cleanup", "--run", "--copy-to-server"])
    assert args.run and args.copy_to_server
