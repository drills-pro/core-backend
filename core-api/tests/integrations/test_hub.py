from app.integrations import hub

DOC = {"siblings": [{"rfilename": "model.safetensors", "size": 100}, {"rfilename": "config.json", "size": 5}],
       "cardData": {"license": "Apache-2.0"}, "config": {"architectures": ["X"]}}


def test_a_plain_apache_safetensors_model_is_admitted_and_its_licence_lowercased():
    f = hub.facts_from(DOC)
    assert f.license == "apache-2.0" and f.total_bytes == 105 and hub.refusals(f) == []


def test_each_rule_refuses_on_its_own():
    assert "safetensors" in hub.refusals(hub.facts_from({**DOC, "siblings": [{"rfilename": "pytorch_model.bin"}]}))[0]
    assert "auto_map" in hub.refusals(hub.facts_from({**DOC, "config": {"auto_map": {}}}))[0]
    assert "allowlist" in hub.refusals(hub.facts_from({**DOC, "cardData": {"license": "cc-by-nc-4.0"}}))[0]
    assert "not declared" in hub.refusals(hub.facts_from({**DOC, "cardData": {}}))[0]
    big = {**DOC, "siblings": [{"rfilename": "m.safetensors", "size": hub.settings.training.model_max_bytes + 1}]}
    assert "limit" in hub.refusals(hub.facts_from(big))[0]


def test_the_licence_falls_back_to_the_hub_tag():
    assert hub.facts_from({**DOC, "cardData": {}, "tags": ["license:mit"]}).license == "mit"
