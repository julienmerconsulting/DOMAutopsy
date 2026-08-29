"""Tests unitaires pour extract_browser_use_history() - le point d'entree
de la fusion pipeline (B1/B2 review).

Le vrai browser-use n'est pas mockable proprement sans installer, donc
on teste avec de faux objets qui simulent la surface API BU 0.12.9 :
AgentHistoryList[AgentHistory(model_output, result, state, metadata)].

Couvre :
- Chemin 1 : result.history
- Chemin 2 : agent.history.history
- Chemin 3 : agent.state.history.history
- Fallback : result.model_actions()
- Extraction interacted_element depuis state ET result
- Extraction metadata timing (step_start_time, step_end_time normalises ms)
- Absence totale d'historique -> [] (pas de crash)
"""
from types import SimpleNamespace
from datetime import datetime, timezone

import pytest

from clean_steps_builder import (
    extract_browser_use_history,
    _to_ms,
)


def _make_history_entry(actions=None, interacted=None, ts_start=None,
                       ts_end=None, step_number=None, url="https://x"):
    """Construit un faux AgentHistory qui expose la surface BU 0.12.9."""
    model_output = SimpleNamespace(
        action=[SimpleNamespace(model_dump=lambda a=a, **kw: a) for a in (actions or [])],
        current_state=SimpleNamespace(next_goal="test goal", memory=None),
    )
    state = SimpleNamespace(url=url, title="Test", tabs=[])
    if interacted is not None:
        state.interacted_element = SimpleNamespace(
            xpath=interacted.get("xpath"),
            css_selector=interacted.get("css_selector"),
            attributes=interacted.get("attributes", {}),
            tag_name=interacted.get("tag_name", "div"),
        )
    metadata = SimpleNamespace(
        step_number=step_number,
        step_start_time=ts_start,
        step_end_time=ts_end,
        input_tokens=100,
    )
    return SimpleNamespace(
        model_output=model_output, result=[], state=state, metadata=metadata,
    )


def test_extract_via_result_history():
    """Chemin 1 : result.history est prioritaire."""
    entry = _make_history_entry(
        actions=[{"go_to_url": {"url": "https://a.com"}}],
        ts_start=1700000000.5, ts_end=1700000001.0, step_number=1,
    )
    result = SimpleNamespace(history=[entry])
    out = extract_browser_use_history(agent=None, result=result)
    assert len(out) == 1
    assert out[0]["actions"] == [{"go_to_url": {"url": "https://a.com"}}]
    assert out[0]["metadata"]["step_number"] == 1
    assert out[0]["metadata"]["step_start_time"] == 1700000000500
    assert out[0]["metadata"]["step_end_time"] == 1700000001000


def test_extract_via_agent_history_property():
    """Chemin 2 : fallback sur agent.history quand result.history est vide."""
    entry = _make_history_entry(actions=[{"click": {"index": 0}}])
    agent = SimpleNamespace(history=SimpleNamespace(history=[entry]))
    result = SimpleNamespace(history=[])  # empty result
    out = extract_browser_use_history(agent=agent, result=result)
    assert len(out) == 1
    assert out[0]["actions"] == [{"click": {"index": 0}}]


def test_extract_via_agent_state_history():
    """Chemin 3 : encore un fallback, agent.state.history."""
    entry = _make_history_entry(actions=[{"input_text": {"text": "hi"}}])
    agent = SimpleNamespace(state=SimpleNamespace(
        history=SimpleNamespace(history=[entry])
    ))
    result = SimpleNamespace()  # no history attribute
    # Force absence pour deconnecter chemin 1
    for attr in ("history",):
        if hasattr(result, attr):
            delattr(result, attr)
    out = extract_browser_use_history(agent=agent, result=result)
    assert len(out) == 1
    assert out[0]["actions"][0]["input_text"]["text"] == "hi"


def test_extract_fallback_model_actions():
    """Fallback ultime : result.model_actions() quand aucun history n'existe."""
    class NoHistoryResult:
        def model_actions(self):
            return [
                {"go_to_url": {"url": "https://x.com"}},
                {"click": {"index": 0}},
            ]
    out = extract_browser_use_history(agent=None, result=NoHistoryResult())
    assert len(out) == 2
    assert out[0]["actions"][0] == {"go_to_url": {"url": "https://x.com"}}
    # Pas de metadata puisqu'on est en fallback plat
    assert "metadata" not in out[0]


def test_extract_interacted_element_from_state():
    """B2 : interacted_element expose sur state est extrait."""
    entry = _make_history_entry(
        actions=[{"click": {}}],
        interacted={"xpath": "//button[1]", "css_selector": "button.submit",
                    "attributes": {"aria-label": "Envoyer"}, "tag_name": "button"},
    )
    result = SimpleNamespace(history=[entry])
    out = extract_browser_use_history(agent=None, result=result)
    ie = out[0].get("interacted_element")
    assert ie is not None
    assert ie["css_selector"] == "button.submit"
    assert ie["xpath"] == "//button[1]"
    assert ie["attributes"]["aria-label"] == "Envoyer"


def test_extract_empty_when_no_history_source():
    """Aucune source disponible -> liste vide, pas de crash."""
    out = extract_browser_use_history(agent=None, result=None)
    assert out == []


def test_extract_defensive_on_broken_entry():
    """Une entree qui explose n'interrompt pas la boucle globale."""
    class BadEntry:
        @property
        def model_output(self):
            raise RuntimeError("broken")
    good = _make_history_entry(actions=[{"click": {}}])
    result = SimpleNamespace(history=[BadEntry(), good])
    out = extract_browser_use_history(agent=None, result=result)
    assert len(out) == 2
    # Premier entry a un _parse_error, deuxieme est OK
    assert "_parse_error" in out[0]
    assert out[1]["actions"] == [{"click": {}}]


# ============================================================
# R2 : Alignment action[i] <-> interacted_element[i]
# ============================================================

def test_step_with_multi_actions_and_aligned_element_list():
    """R2 CRITIQUE : un step avec N actions + une LISTE de N elements
    doit produire N normalized_actions avec chaque element correctement
    apparie a son action (pas le premier partout)."""
    # Simule un step avec 3 actions ou l'element de l'action 1 est None
    entry = _make_history_entry(
        actions=[
            {"click_element": {"index": 0}},
            {"input_text": {"text": "hello"}},
            {"click_element": {"index": 2}},
        ],
        interacted=None,  # on construit la liste manuellement
    )
    # BU 0.13 : state.interacted_element est une LISTE alignee
    entry.state.interacted_element = [
        SimpleNamespace(xpath="//button[1]", css_selector="button.first"),
        None,  # input_text sans element (rare mais possible)
        SimpleNamespace(xpath="//button[3]", css_selector="button.third"),
    ]
    result = SimpleNamespace(history=[entry])
    out = extract_browser_use_history(agent=None, result=result)
    assert len(out) == 1

    na = out[0]["normalized_actions"]
    assert len(na) == 3

    # Action 0 -> element 0 (button.first)
    assert na[0]["action_index"] == 0
    assert na[0]["interacted_element"]["css_selector"] == "button.first"

    # Action 1 -> None (input_text sans element)
    assert na[1]["action_index"] == 1
    assert na[1]["interacted_element"] is None

    # Action 2 -> element 2 (button.third)
    assert na[2]["action_index"] == 2
    assert na[2]["interacted_element"]["css_selector"] == "button.third"


def test_single_element_applies_to_first_action_only_not_all():
    """Regression fix : un element unique pour N actions ne doit PAS
    etre colle a toutes les actions (bug ancien code)."""
    entry = _make_history_entry(
        actions=[{"click_element": {}}, {"input_text": {"text": "x"}}],
        interacted={"css_selector": "#the-only-elem", "xpath": "//button"},
    )
    result = SimpleNamespace(history=[entry])
    out = extract_browser_use_history(agent=None, result=result)
    na = out[0]["normalized_actions"]
    assert len(na) == 2
    # Action 0 recoit l'element unique
    assert na[0]["interacted_element"]["css_selector"] == "#the-only-elem"
    # Action 1 recoit None (pas le meme element clone !)
    assert na[1]["interacted_element"] is None


def test_no_interacted_element_all_actions_get_none():
    """Aucun element dispo -> chaque action a interacted_element=None."""
    entry = _make_history_entry(
        actions=[{"go_to_url": {"url": "https://x"}}, {"wait": {"seconds": 1}}],
        interacted=None,
    )
    result = SimpleNamespace(history=[entry])
    out = extract_browser_use_history(agent=None, result=result)
    na = out[0]["normalized_actions"]
    assert len(na) == 2
    assert all(x["interacted_element"] is None for x in na)


def test_element_list_longer_than_actions_truncated():
    """Liste elements plus longue que actions -> tronque au nb d'actions."""
    entry = _make_history_entry(actions=[{"click_element": {}}])
    entry.state.interacted_element = [
        SimpleNamespace(css_selector="#a"),
        SimpleNamespace(css_selector="#b"),  # sera ignore
    ]
    result = SimpleNamespace(history=[entry])
    out = extract_browser_use_history(agent=None, result=result)
    assert len(out[0]["normalized_actions"]) == 1
    assert out[0]["normalized_actions"][0]["interacted_element"]["css_selector"] == "#a"


def test_element_list_shorter_than_actions_padded_with_none():
    """Liste elements plus courte que actions -> complete avec None."""
    entry = _make_history_entry(
        actions=[{"click_element": {}}, {"click_element": {}}, {"click_element": {}}],
    )
    entry.state.interacted_element = [SimpleNamespace(css_selector="#a")]  # 1 elem pour 3 actions
    result = SimpleNamespace(history=[entry])
    out = extract_browser_use_history(agent=None, result=result)
    na = out[0]["normalized_actions"]
    assert len(na) == 3
    assert na[0]["interacted_element"]["css_selector"] == "#a"
    assert na[1]["interacted_element"] is None
    assert na[2]["interacted_element"] is None


def test_normalized_actions_carry_step_timing_metadata():
    """Chaque normalized action porte step_number/step_start/step_end
    pour permettre la fusion chronologique fine downstream."""
    entry = _make_history_entry(
        actions=[{"click_element": {}}, {"input_text": {"text": "x"}}],
        ts_start=1700000000.0, ts_end=1700000001.0, step_number=5,
    )
    result = SimpleNamespace(history=[entry])
    na = extract_browser_use_history(agent=None, result=result)[0]["normalized_actions"]
    for entry in na:
        assert entry["step_number"] == 5
        assert entry["step_start_time"] == 1700000000000
        assert entry["step_end_time"] == 1700000001000


# _to_ms normalisation
class TestToMs:
    def test_none(self):
        assert _to_ms(None) is None

    def test_datetime(self):
        dt = datetime(2024, 1, 1, tzinfo=timezone.utc)
        expected_ms = int(dt.timestamp() * 1000)
        assert _to_ms(dt) == expected_ms

    def test_float_seconds(self):
        # 1700000000.5 secondes -> 1700000000500 ms
        assert _to_ms(1700000000.5) == 1700000000500

    def test_int_seconds_below_threshold(self):
        # 1700000000 (secondes) -> 1700000000000 (ms)
        assert _to_ms(1700000000) == 1700000000000

    def test_int_already_ms(self):
        # 1700000000500 (ms deja) -> tel quel
        assert _to_ms(1700000000500) == 1700000000500
