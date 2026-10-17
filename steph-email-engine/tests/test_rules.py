import json

from steph_email.rules import Tagger


def test_default_construction_tags():
    t = Tagger()
    assert "permit" in t.tags_for("Permit approved", "city@town.gov", "")
    assert set(t.tags_for("Invoice past due", "billing@x.com", "final notice")) >= {"invoice", "urgent"}
    assert t.tags_for("Lunch?", "friend@x.com", "pizza") == []
    assert "safety" in t.tags_for("Stop work on site", "x@y.com", "")


def test_sender_rules_and_custom_file(tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps({"oakstreet": ["12 oak st"], "permit": ["permit", "co issued"]}))
    t = Tagger.load(tmp_path)
    tags = t.tags_for("CO issued for 12 Oak St", "noreply@docusign.net", "")
    assert {"oakstreet", "permit", "docusign", "newsletter"} <= set(tags)
    assert "invoice" in t.rules


def test_newsletter_detection():
    t = Tagger()
    assert "newsletter" in t.tags_for("Weekly deals", "marketing@bigbox.com", "Unsubscribe here")
    assert "newsletter" in t.tags_for("Webinar Thursday", "events@x.com", "")
