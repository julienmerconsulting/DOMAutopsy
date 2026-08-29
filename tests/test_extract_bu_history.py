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
