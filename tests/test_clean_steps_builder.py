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


def test_dedup_consolidates_consecutive_inputs_same_field():
    """#3 : les inputs successifs SUR LE MEME CHAMP sans autre event entre
    sont consolides en gardant la derniere valeur (chaque frappe clavier
    = 1 event input, on garde l'etat final du champ)."""
    log = [
        {"action": "input", "timestamp": 1, "selector": _sel("#email"),
         "url": "u", "value": "j"},
        {"action": "input", "timestamp": 2, "selector": _sel("#email"),
         "url": "u", "value": "testuser"},
        {"action": "input", "timestamp": 3, "selector": _sel("#email"),
         "url": "u", "value": "user@example.com"},
    ]
    out = dedup_log(log)
    inputs = [e for e in out if e["action"] == "input"]
    assert len(inputs) == 1
    assert inputs[0]["value"] == "user@example.com"


def test_dedup_does_not_consolidate_inputs_separated_by_keyboard():
    """R3 CRITIQUE : "4 saisies + 4 Enter" pattern TodoMVC. Chaque Enter
    separe 2 cycles input. Consequence : 4 inputs distincts + 4 keyboard,
    pas 1 input global (bug ancien)."""
    log = []
    for i, todo in enumerate(["Preparer QA", "Verifier selecteurs", "Controler reseau", "Valider replay"]):
        log.append({"action": "input", "timestamp": i * 10,
                    "selector": _sel("input.new-todo"), "url": "u", "value": todo})
        log.append({"action": "keyboard", "timestamp": i * 10 + 1,
                    "selector": _sel("input.new-todo"), "url": "u", "value": "Enter"})
    out = dedup_log(log)
    inputs = [e for e in out if e["action"] == "input"]
    keyboards = [e for e in out if e["action"] == "keyboard"]
    assert len(inputs) == 4, f"Attendu 4 inputs distincts, obtenu {len(inputs)}"
    assert len(keyboards) == 4
    # Ordre preserve
    assert inputs[0]["value"] == "Preparer QA"
    assert inputs[3]["value"] == "Valider replay"


def test_dedup_does_not_consolidate_inputs_on_different_fields():
    """Inputs consecutifs sur DIFFERENTS champs -> conserves separement."""
    log = [
        {"action": "input", "timestamp": 1, "selector": _sel("#email"),
         "url": "u", "value": "a@b.c"},
        {"action": "input", "timestamp": 2, "selector": _sel("#password"),
         "url": "u", "value": "secret"},
    ]
    out = dedup_log(log)
    assert len(out) == 2


def test_dedup_click_between_inputs_breaks_consolidation():
    """input#A + click#X + input#A -> 3 entrees (click coupe la sequence)."""
    log = [
        {"action": "input", "timestamp": 1, "selector": _sel("#field"),
         "url": "u", "value": "first"},
        {"action": "click", "timestamp": 2, "selector": _sel("#btn"), "url": "u"},
        {"action": "input", "timestamp": 3, "selector": _sel("#field"),
         "url": "u", "value": "second"},
    ]
    out = dedup_log(log)
    assert len(out) == 3
    assert out[0]["value"] == "first"
    assert out[2]["value"] == "second"


def test_dedup_keyboard_events_always_preserved():
    """Enter/Tab/Escape -> jamais deduplique, chaque event conserve."""
    log = [
        {"action": "keyboard", "timestamp": 1, "value": "Enter", "url": "u", "selector": _sel("body")},
        {"action": "keyboard", "timestamp": 2, "value": "Enter", "url": "u", "selector": _sel("body")},
        {"action": "keyboard", "timestamp": 3, "value": "Enter", "url": "u", "selector": _sel("body")},
    ]
    out = dedup_log(log)
    assert len(out) == 3


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


def test_bu_done_is_not_translated_to_step():
    """La meta-action LLM 'done' n'est pas une action user : elle ne
    genere pas de step (aucune interaction reproductible)."""
    bu_history = [{"actions": [{"done": {"success": True}}]}]
    steps = build_pre_cleanup_steps(
        scenario_steps=None, bu_history=bu_history, dom_log=[], network_log=None,
    )
    assert steps == []


def test_bu_extract_creates_step_marked_not_replayable():
    """extract est conservee dans le JSON pour la tracabilite mais
    explicitement marquee non-rejouable (regle : pas de no-op presente
    comme executee). Le TS emit un throw plutot qu'un skip silencieux."""
    bu_history = [{"actions": [{"extract_content": {"query": "titre article"}}]}]
    steps = build_pre_cleanup_steps(
        scenario_steps=None, bu_history=bu_history, dom_log=[], network_log=None,
    )
    assert len(steps) == 1
    s = steps[0]
    assert s.action == "extract"
    assert s.included_in_replay is False
    assert s.cleanup_reason and "extract" in s.cleanup_reason.lower()


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


def test_bu_scroll_without_selector_remains_replayable():
    steps = build_pre_cleanup_steps(
        scenario_steps=None,
        bu_history=[{
            "normalized_actions": [{
                "action": {"scroll": {"direction": "down", "amount": 650}},
                "action_index": 0,
                "interacted_element": None,
            }],
        }],
        dom_log=[],
        network_log=None,
    )
    scroll = next(step for step in steps if step.action == "scroll")
    assert scroll.included_in_replay is True
    assert scroll.replay_blocking is False


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
    assert any(item["name"] == "DOMAUTOPSY_STEP_0002" for item in env_vars)
    assert steps[1].env_var == "DOMAUTOPSY_STEP_0002"
    assert steps[0].env_var is None  # pas sensitive
    assert steps[2].env_var is None  # pas input


# --------------------------------------------------------------------
# _normalize_bu_action_name() : mapping browser-use -> vocabulaire OculiX
# --------------------------------------------------------------------

@pytest.mark.parametrize("bu_name,expected", [
    # Mapping direct
    ("go_to_url", "navigate"),
    ("input_text", "input"),
    ("press_key", "keyboard"),
    ("scroll_down", "scroll"),
    # BU 0.12.9 : noms officiels differents
    ("switch", "switch_tab"),
    ("close", "close_tab"),
    ("select_dropdown", "select"),
    # extract : garde dans le JSON avec action='extract', pas None
    ("extract", "extract"),
    ("extract_content", "extract"),
    # Meta-actions LLM sans interaction : None
    ("done", None),
    ("read_content", None),
    ("assess", None),
    # Inconnu : conserve sous 'unknown'
    ("something_never_seen", "unknown"),
])
def test_bu_action_name_normalization(bu_name, expected):
    assert _normalize_bu_action_name(bu_name) == expected
