"""Tests cahier des charges :
  #1 conservation des scrolls
  #2 deduplication des clics
  #3 consolidation des inputs
  #4 conservation des autres actions (pas juste click/input)
  #6 conservation des actions inconnues (via unknown + raw_payload)
"""
import pytest

from qa_explorer import dedup_log
from clean_steps_builder import (
    build_pre_cleanup_steps,
    detect_and_flag_sensitive,
    _normalize_bu_action_name,
)


# --------------------------------------------------------------------
# dedup_log() : test 1 (scrolls), 2 (clicks), 3 (inputs)
# --------------------------------------------------------------------

def _sel(val):
    return {"strategy": "id", "value": val, "unique": True, "matchCount": 1}


def test_dedup_preserves_scrolls():
    """#1 : les scrolls ne sont JAMAIS filtres, meme consecutifs."""
    log = [
        {"action": "scroll", "timestamp": 1, "selector": {}, "direction": "down"},
        {"action": "scroll", "timestamp": 2, "selector": {}, "direction": "down"},
        {"action": "scroll", "timestamp": 3, "selector": {}, "direction": "up"},
    ]
    out = dedup_log(log)
    assert len(out) == 3
    assert all(e["action"] == "scroll" for e in out)


def test_dedup_removes_only_truly_consecutive_identical_clicks():
    """#2 : les clics consecutifs identiques (meme selecteur ET meme URL)
    sont dedupliques, mais pas les clics repetes separes par autre chose."""
    log = [
        {"action": "click", "timestamp": 1, "selector": _sel("#a"), "url": "u1"},
        {"action": "click", "timestamp": 2, "selector": _sel("#a"), "url": "u1"},  # doublon
        {"action": "click", "timestamp": 3, "selector": _sel("#b"), "url": "u1"},
        {"action": "click", "timestamp": 4, "selector": _sel("#a"), "url": "u1"},  # OK, apres #b
    ]
    out = dedup_log(log)
    assert len(out) == 3
    assert [e["selector"]["value"] for e in out] == ["#a", "#b", "#a"]


def test_dedup_consolidates_inputs_keeping_last_value():
    """#3 : les inputs successifs sur le meme champ+URL sont consolides
    en gardant la derniere valeur."""
    log = [
        {"action": "input", "timestamp": 1, "selector": _sel("#email"),
         "url": "u", "value": "j"},
        {"action": "input", "timestamp": 2, "selector": _sel("#email"),
         "url": "u", "value": "julien"},
        {"action": "input", "timestamp": 3, "selector": _sel("#email"),
         "url": "u", "value": "julien.mer@ex.com"},
    ]
    out = dedup_log(log)
    inputs = [e for e in out if e["action"] == "input"]
    assert len(inputs) == 1
    assert inputs[0]["value"] == "julien.mer@ex.com"


# --------------------------------------------------------------------
# build_pre_cleanup_steps() : test 4 (autres actions), 6 (unknown)
# --------------------------------------------------------------------

def test_bu_navigate_becomes_navigate_step():
    """#4 : les actions BU non-DOM (navigate, wait, keyboard...) sont
    ajoutees dans les steps."""
    bu_history = [{
        "actions": [{"go_to_url": {"url": "https://example.com"}}],
    }]
    steps = build_pre_cleanup_steps(
        scenario_steps=None, bu_history=bu_history, dom_log=[], network_log=None,
    )
    navs = [s for s in steps if s.action == "navigate"]
    assert len(navs) == 1
    assert navs[0].url == "https://example.com"


def test_bu_unknown_action_preserved_as_unknown_with_raw_payload():
    """#6 : une action BU non standard est conservee sous type unknown
    avec son payload brut - jamais supprimee silencieusement."""
    bu_history = [{
        "actions": [{"do_barrel_roll": {"speed": 42, "axis": "z"}}],
    }]
    steps = build_pre_cleanup_steps(
        scenario_steps=None, bu_history=bu_history, dom_log=[], network_log=None,
    )
    unknowns = [s for s in steps if s.action == "unknown"]
    assert len(unknowns) == 1
    assert unknowns[0].raw_payload == {"do_barrel_roll": {"speed": 42, "axis": "z"}}


def test_bu_done_extract_content_are_not_translated_to_steps():
    """Les meta-actions LLM (done, extract_content) ne sont pas des
    actions user rejouables : elles ne generent pas de step."""
    bu_history = [{
        "actions": [{"done": {"success": True}}, {"extract_content": {"query": "..."}}],
    }]
    steps = build_pre_cleanup_steps(
        scenario_steps=None, bu_history=bu_history, dom_log=[], network_log=None,
    )
    assert steps == []


def test_scenario_verify_and_cookie_added_as_steps():
    """#4 : verify et cookie du scenario JSON sont ajoutes comme steps
    (declaratifs, non captes par DOM listener)."""
    scenario_steps = [
        {"action": "verify", "target": "Bienvenue", "type": "texte_contient"},
        {"action": "cookie", "target": "Accepter"},
    ]
    steps = build_pre_cleanup_steps(
        scenario_steps=scenario_steps, bu_history=[], dom_log=[], network_log=None,
    )
    actions = [s.action for s in steps]
    assert "verify" in actions
    assert "cookie" in actions


# --------------------------------------------------------------------
# detect_and_flag_sensitive() : #9 (sensitive protection)
# --------------------------------------------------------------------

def test_sensitive_inputs_get_env_var_assigned():
    """#9 : chaque input sensitive=True recoit un env_var DOMAUTOPSY_STEP_XXXX."""
    from schemas import Step
    steps = [
        Step(id="step-0001", step=1, action="input", value="hi", sensitive=False),
        Step(id="step-0002", step=2, action="input", value="secret", sensitive=True),
        Step(id="step-0003", step=3, action="click"),
    ]
    env_vars = detect_and_flag_sensitive(steps)
    assert "DOMAUTOPSY_STEP_0002" in env_vars
    assert steps[1].env_var == "DOMAUTOPSY_STEP_0002"
    assert steps[0].env_var is None  # pas sensitive
    assert steps[2].env_var is None  # pas input


# --------------------------------------------------------------------
# _normalize_bu_action_name() : mapping browser-use -> vocabulaire OculiX
# --------------------------------------------------------------------

@pytest.mark.parametrize("bu_name,expected", [
    ("go_to_url", "navigate"),
    ("input_text", "input"),
    ("press_key", "keyboard"),
    ("scroll_down", "scroll"),
    ("done", None),
    ("extract_content", None),
    ("something_never_seen", "unknown"),
])
def test_bu_action_name_normalization(bu_name, expected):
    assert _normalize_bu_action_name(bu_name) == expected
