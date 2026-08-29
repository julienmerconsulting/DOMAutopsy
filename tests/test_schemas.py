"""Tests cahier des charges :
  #5  validation du JSON complet
  #6  conservation des actions inconnues
  #11 compatibilite des anciens JSON
"""
import json
import pytest

from schemas import (
    CURRENT_SCHEMA_VERSION,
    CleanSteps,
    Step,
    Selector,
    migrate_legacy_json,
    load_and_validate,
)


def test_schema_version_is_stamped():
    """#5 : chaque CleanSteps neuf porte le schema_version courant."""
    cs = CleanSteps()
    assert cs.schema_version == CURRENT_SCHEMA_VERSION


def test_step_action_normalized_to_lower():
    """Regression : action doit toujours etre lowercase pour dispatch coherent."""
    s = Step(action="CLICK")
    assert s.action == "click"


def test_unknown_action_is_preserved_with_raw_payload():
    """#6 : une action inconnue n'est PAS supprimee, elle est conservee
    avec son nom brut et son raw_payload accessible."""
    s = Step(
        action="some_new_action_never_seen",
        raw_payload={"unusual": "data", "nested": {"k": 1}},
    )
    assert s.action == "some_new_action_never_seen"
    assert s.raw_payload == {"unusual": "data", "nested": {"k": 1}}


def test_migrate_legacy_json_no_version_stamps_current():
    """#11 : un ancien clean_steps.json sans schema_version obtient v1.0."""
    legacy = {
        "parcours": "old parcours",
        "steps": [{"action": "click", "selector": "#btn"}],
    }
    migrated = migrate_legacy_json(legacy)
    assert migrated["schema_version"] == CURRENT_SCHEMA_VERSION


def test_migrate_legacy_json_selector_string_becomes_dict():
    """#11 : les anciens selecteurs strings deviennent Selector(value=..., strategy=raw)."""
    legacy = {"steps": [{"action": "click", "selector": "#loginBtn"}]}
    migrated = migrate_legacy_json(legacy)
    cs = CleanSteps.model_validate(migrated)
    step = cs.steps[0]
    assert isinstance(step.selector, Selector)
    assert step.selector.value == "#loginBtn"
    assert step.selector.strategy == "raw"


def test_migrate_legacy_json_defaults_included_in_replay_true():
    """#11 : les anciens JSON n'ont pas de champ included_in_replay, on
    doit forcer True par defaut (ils ne connaissaient pas le filtrage marque)."""
    legacy = {"steps": [{"action": "click", "selector": "#a"}, {"action": "input", "selector": "#b"}]}
    cs = CleanSteps.model_validate(migrate_legacy_json(legacy))
    assert all(s.included_in_replay for s in cs.steps)


def test_load_and_validate_rejects_non_dict():
    """#5 : validation dure sur le type d'entree."""
    with pytest.raises(TypeError):
        load_and_validate("this is not a dict")  # type: ignore


def test_load_and_validate_roundtrip_serialize_deserialize():
    """#5 : le JSON serialise doit se re-valider a l'identique."""
    original = CleanSteps(
        parcours="test",
        scenario_url="https://example.com",
        total_steps=1,
        steps=[Step(
            id="step-0001", step=1, action="click",
            description="click login",
            selector=Selector(value="#login", strategy="id", unique=True, matchCount=1),
            page="https://example.com",
            included_in_replay=True,
        )],
    )
    dumped = json.loads(original.model_dump_json(exclude_none=True))
    restored = load_and_validate(dumped)
    assert restored.parcours == "test"
    assert restored.steps[0].id == "step-0001"
    assert restored.steps[0].selector.value == "#login"
