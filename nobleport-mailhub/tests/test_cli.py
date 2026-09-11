import json

from mailhub import cli
from mailhub.config import Settings


def test_cli_end_to_end(hub, settings, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_hub", lambda s=None: hub)
    assert cli.main(["accounts", "add", "me@gmail.com", "--secret", "pw", "--name", "Mike"]) == 0
    assert "connected me@gmail.com" in capsys.readouterr().out
    assert cli.main(["accounts", "add", "broken@gmail.com", "--secret", "pw"]) == 1

    assert cli.main(["sync"]) == 0
    assert "+3" in capsys.readouterr().out

    assert cli.main(["inbox", "--json", "--tag", "permit"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2

    assert cli.main(["search", "supplyco"]) == 0
    out = capsys.readouterr().out
    assert "Invoice #4471" in out and "invoice" in out

    assert cli.main(["show", str(rows[0]["id"])]) == 0
    assert "Subject: " in capsys.readouterr().out
    assert cli.main(["show", "999"]) == 1

    assert cli.main(["send", "--from", "me@gmail.com", "--to", "a@b.com", "--subject", "Yo", "--body", "hi"]) == 0
    assert "sent <" in capsys.readouterr().out

    assert cli.main(["accounts", "list"]) == 0
    assert "me@gmail.com" in capsys.readouterr().out
    assert cli.main(["accounts", "remove", "me@gmail.com"]) == 0
    assert cli.main(["accounts", "providers"]) == 0


def test_settings_env_prefix(monkeypatch, tmp_path):
    monkeypatch.setenv("MAILHUB_FOLDERS", "INBOX, Archive ,")
    monkeypatch.setenv("MAILHUB_DATA_DIR", str(tmp_path))
    s = Settings()
    assert s.folder_list == ["INBOX", "Archive"]
    assert s.resolved_db_path == tmp_path / "mailhub.db"
