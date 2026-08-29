"""Tests unitaires pour la FUSION CHRONOLOGIQUE (B1 review).

Le bug corrige : l'ancien code ajoutait DOM entries puis BU actions a la
fin, produisant l'ordre 'click -> click -> navigate -> wait' pour une
execution reelle 'navigate -> click -> wait -> click'. Ces tests
verifient que la nouvelle logique respecte l'ordre temporel effectif.

Note : on court-circuite ai_classify_steps (qui appelle un LLM externe)
en testant directement build_pre_cleanup_steps.
"""
import pytest

from clean_steps_builder import build_pre_cleanup_steps


def _dom_entry(action, selector_value, ts_ms, url="https://x", value=None):
    return {
        "action": action,
        "timestamp": ts_ms,
        "url": url,
        "selector": {"strategy": "id", "value": selector_value, "unique": True, "matchCount": 1},
        "value": value,
        "text": f"text-{selector_value}",
    }


def _bu_step(actions, start_ms, end_ms):
    return {
        "actions": actions,
        "metadata": {"step_start_time": start_ms, "step_end_time": end_ms},
    }


def test_chronological_order_navigate_click_wait_click():
    """Scenario reel : navigate a T=1000, click a T=2000, wait a T=3000,
    click a T=4000. L'ancien code produisait click/click/navigate/wait,
    le nouveau doit produire navigate/click/wait/click."""
    bu_history = [
        _bu_step([{"go_to_url": {"url": "https://x/login"}}], 1000, 1100),
        _bu_step([{"click_element": {"index": 0}}], 2000, 2100),
        _bu_step([{"wait": {"seconds": 1}}], 3000, 3100),
        _bu_step([{"click_element": {"index": 1}}], 4000, 4100),
    ]
    dom_log = [
        _dom_entry("click", "#login", ts_ms=2050),   # matche BU step 2
        _dom_entry("click", "#submit", ts_ms=4050),  # matche BU step 4
    ]
    steps = build_pre_cleanup_steps(
        scenario_steps=None, bu_history=bu_history, dom_log=dom_log, network_log=None,
    )
    actions_order = [s.action for s in steps]
    assert actions_order == ["navigate", "click", "wait", "click"], (
        f"Ordre attendu navigate/click/wait/click, obtenu : {actions_order}"
    )


def test_dom_click_in_bu_window_uses_dom_selector():
    """Quand un click BU tombe dans la fenetre temporelle d'un DOM entry,
    on utilise le DOM selector (fiabilite runtime) et on marque source=bu+dom."""
    bu_history = [_bu_step([{"click_element": {"index": 0}}], 1000, 1200)]
    dom_log = [_dom_entry("click", "#loginBtn", ts_ms=1100)]
    steps = build_pre_cleanup_steps(None, bu_history, dom_log)
    assert len(steps) == 1
    assert steps[0].source == "bu+dom"
    assert steps[0].selector.value == "#loginBtn"


def test_dom_orphan_kept_when_no_bu_correspondence():
    """DOM entry en dehors de toute fenetre BU -> conserve avec
    source=dom_orphan (regle cahier : jamais supprimer sans preuve)."""
    bu_history = [_bu_step([{"go_to_url": {"url": "https://x"}}], 1000, 1100)]
    dom_log = [_dom_entry("click", "#stray", ts_ms=99999)]  # tres loin
    steps = build_pre_cleanup_steps(None, bu_history, dom_log)
    orphans = [s for s in steps if s.source == "dom_orphan"]
    assert len(orphans) == 1
    assert orphans[0].selector.value == "#stray"


def test_bu_click_without_dom_uses_interacted_element():
    """BU click sans DOM correspondance : selecteur pris depuis
    interacted_element (BU fournit), source=browser_use_history."""
    bu_history = [{
        "actions": [{"click_element": {"index": 0}}],
        "metadata": {"step_start_time": 1000, "step_end_time": 1200},
        "interacted_element": {
            "css_selector": "button.dashboard-refresh",
            "xpath": "//button[@class='dashboard-refresh']",
        },
    }]
    dom_log = []  # aucun DOM event
    steps = build_pre_cleanup_steps(None, bu_history, dom_log)
    assert len(steps) == 1
    s = steps[0]
    assert s.action == "click"
    assert s.source == "browser_use_history"
    assert s.selector is not None
    assert s.selector.value == "button.dashboard-refresh"
    assert s.selector.strategy == "bu-css"


def test_bu_click_no_dom_no_interacted_element_kept_without_selector():
    """Regle cahier : jamais fabriquer un selecteur. Si BU ne fournit ni
    DOM match ni interacted_element, le step est conserve mais sans
    selector (le classifier LLM le signalera en anomalie)."""
    bu_history = [{
        "actions": [{"click_element": {"index": 0}}],
        "metadata": {"step_start_time": 1000, "step_end_time": 1200},
    }]
    steps = build_pre_cleanup_steps(None, bu_history, [])
    assert len(steps) == 1
    assert steps[0].action == "click"
    assert steps[0].selector is None or (
        hasattr(steps[0].selector, "value") and steps[0].selector.value is None
    )


def test_switch_tab_uses_page_index():
    """switch (BU 0.12.9) -> switch_tab avec value = index de page."""
    bu_history = [_bu_step([{"switch": {"page_index": 2}}], 1000, 1100)]
    steps = build_pre_cleanup_steps(None, bu_history, [])
    assert steps[0].action == "switch_tab"
    assert steps[0].value == "2"


def test_close_tab_variant_names():
    """close (BU 0.12.9) OU close_tab -> close_tab."""
    bu_history = [
        _bu_step([{"close": {"page_index": 1}}], 1000, 1100),
        _bu_step([{"close_tab": {"page_index": 0}}], 2000, 2100),
    ]
    steps = build_pre_cleanup_steps(None, bu_history, [])
    assert steps[0].action == "close_tab"
    assert steps[0].value == "1"
    assert steps[1].action == "close_tab"


def test_navigate_new_tab_becomes_open_tab():
    """navigate(new_tab=True) -> open_tab (les 2 sont differents cote replay)."""
    bu_history = [
        _bu_step([{"go_to_url": {"url": "https://a", "new_tab": True}}], 1000, 1100),
    ]
    steps = build_pre_cleanup_steps(None, bu_history, [])
    assert steps[0].action == "open_tab"
    assert steps[0].url == "https://a"


def test_network_association_by_timestamp():
    """Les requetes reseau dans la fenetre [step.ts, step_next.ts] sont
    attachees au step precedent."""
    bu_history = [
        _bu_step([{"click_element": {"index": 0}}], 1000, 1100),
        _bu_step([{"click_element": {"index": 1}}], 3000, 3100),
    ]
    dom_log = [
        _dom_entry("click", "#a", ts_ms=1050),
        _dom_entry("click", "#b", ts_ms=3050),
    ]
    # network_log a des wallTime en secondes float (format CDP)
    network_log = [
        {"method": "POST", "url": "https://api.x/login", "status": 200,
         "type": "Fetch", "wallTime": 1.05, "duration_ms": 42},
        {"method": "GET", "url": "https://api.x/data", "status": 500,
         "type": "XHR", "wallTime": 3.05, "duration_ms": 100},
    ]
    steps = build_pre_cleanup_steps(None, bu_history, dom_log, network_log)
    # Chaque click doit avoir sa network associee
    step_a = next(s for s in steps if s.selector and s.selector.value == "#a")
    step_b = next(s for s in steps if s.selector and s.selector.value == "#b")
    # wallTime * 1000 = 1050 et 3050 : 1050 tombe dans window step_a
    assert step_a.network is not None and len(step_a.network) >= 1
    assert step_b.network is not None and len(step_b.network) >= 1
    assert step_b.network[0].status == 500
