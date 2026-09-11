import json

from mailhub.rules import Tagger


def test_default_construction_tags():
    t = Tagger()
    assert "permit" in t.tags_for("Permit approved", "city@town.gov", "")
    assert set(t.tags_for("Invoice past due", "billing@x.com", "final notice")) >= {"invoice", "urgent"}
    assert t.tags_for("Lunch?", "friend@x.com", "pizza") == []


def test_sender_rules_and_custom_file(tmp_path):
    (tmp_path / "rules.json").write_text(json.dumps({"oakstreet": ["12 oak st"], "permit": ["permit", "co issued"]}))
    t = Tagger.load(tmp_path)
    tags = t.tags_for("CO issued for 12 Oak St", "noreply@docusign.net", "")
    assert set(tags) == {"oakstreet", "permit", "docusign"}
    assert "invoice" in t.rules  # defaults preserved
