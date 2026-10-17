from steph_email.config import Settings


def test_env_prefix_and_lists(monkeypatch, tmp_path):
    monkeypatch.setenv("STEPH_EMAIL_FOLDERS", "INBOX, Archive ,")
    monkeypatch.setenv("STEPH_EMAIL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("STEPH_EMAIL_VIP_DOMAINS", "Town.gov, cooley.com")
    monkeypatch.setenv("STEPH_EMAIL_OWNER_ADDRESSES", "Mike@NoblePort.net")
    s = Settings()
    assert s.folder_list == ["INBOX", "Archive"]
    assert s.resolved_db_path == tmp_path / "steph-email.db"
    assert s.vip_domain_list == ["town.gov", "cooley.com"]
    assert s.owner_address_list == ["mike@nobleport.net"]
    assert s.raw_dir == tmp_path / "raw" and s.audio_dir == tmp_path / "audio" and s.evidence_dir == tmp_path / "evidence"


def test_configured_flags(tmp_path):
    s = Settings(data_dir=tmp_path)
    assert not s.twilio_configured and not s.elevenlabs_configured
    s = Settings(data_dir=tmp_path, twilio_account_sid="AC1", twilio_auth_token="t", twilio_from_number="+15555550100",
                 elevenlabs_api_key="k", elevenlabs_voice_id="v")
    assert s.twilio_configured and s.elevenlabs_configured


def test_timezone_object(tmp_path):
    s = Settings(data_dir=tmp_path, timezone="America/Chicago")
    assert str(s.tz) == "America/Chicago"
