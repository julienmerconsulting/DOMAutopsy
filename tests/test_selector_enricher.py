"""Preuves de selecteurs live et consommation deterministe."""

import asyncio
import sys
import types
from types import SimpleNamespace

from clean_steps_builder import (
    _apply_interacted_element,
    _element_to_dict,
    _fuse_checkbox_interactions,
    _fuse_evaluate_workarounds,
    build_pre_cleanup_steps,
    classify_steps,
)
from playwright_generator import generate_playwright_ts
from schemas import CleanSteps, Selector, Step
from selector_enricher import (
    enrich_browser_use_history_selectors,
    enrich_browser_use_step_snapshot,
)


class _ElementWithCanonicalDict:
    def model_dump(self, **_kwargs):
        return {"attributes": {"id": "partial"}}

    def to_dict(self):
        return {
            "backend_node_id": 42,
            "x_path": "/html/body/button",
            "stable_hash": "stable",
            "ax_name": "Acheter",
            "attributes": {"data-product-id": "1"},
        }


def test_element_to_dict_prefers_browser_use_canonical_method():
    out = _element_to_dict(_ElementWithCanonicalDict())
    assert out["backend_node_id"] == 42
    assert out["attributes"]["data-product-id"] == "1"
    assert out["stable_hash"] == "stable"


class _Page:
    url = "https://example.test/products"


class _Session:
    def __init__(self):
        self.calls = []
        self.detached = False

    async def send(self, method, params=None):
        self.calls.append((method, params))
        if method == "DOM.resolveNode":
            return {"object": {"objectId": "node-42"}}
        if method == "Runtime.callFunctionOn":
            return {"result": {"value": [{
                "value": '[data-product-id="1"]',
                "strategy": "data-attr",
                "selectorType": "css",
                "matchCount": 1,
                "unique": True,
                "stability": "medium",
                "priority": 30,
                "verifiedAtCapture": True,
            }]}}
        return {}

    async def detach(self):
        self.detached = True


class _Context:
    pages = [_Page()]

    def __init__(self):
        self.session = _Session()

    async def new_cdp_session(self, _page):
        return self.session


def test_live_enrichment_resolves_backend_node_and_stores_proof():
    elem = {"backend_node_id": 42, "frame_id": "f", "x_path": "/html/body/button"}
    history = [{"normalized_actions": [{"interacted_element": elem}]}]
    context = _Context()

    stats = asyncio.run(enrich_browser_use_history_selectors(history, context))

    assert stats == {"elements": 1, "resolved": 1, "unique": 1, "unresolved": 0}
    assert elem["selector_candidates"][0]["matchCount"] == 1
    assert elem["selector_enrichment"]["status"] == "resolved"
    assert context.session.detached is True


def test_step_evidence_cache_survives_node_detachment():
    cache = {}
    first_elem = {
        "backend_node_id": 42,
        "frame_id": "f",
        "stable_hash": "same-node",
        "x_path": "/html/body/button",
    }
    first = [{
        "state": {"url": "https://example.test/products"},
        "normalized_actions": [{"interacted_element": first_elem}],
    }]
    asyncio.run(enrich_browser_use_history_selectors(first, _Context(), cache))

    # L'artefact final contient de nouveaux dicts serialises. La page peut
    # deja avoir ete quittee : aucune nouvelle session CDP n'est disponible.
    final_elem = dict(first_elem)
    final = [{
        "state": {"url": "https://example.test/products"},
        "normalized_actions": [{"interacted_element": final_elem}],
    }]

    class _DetachedContext:
        pages = []

    stats = asyncio.run(
        enrich_browser_use_history_selectors(final, _DetachedContext(), cache)
    )
    assert stats["unique"] == 1
    assert final_elem["selector_candidates"][0]["verifiedAtCapture"] is True


def test_pre_action_snapshot_measures_planned_targets(monkeypatch):
    element = _ElementWithCanonicalDict()

    class _FakeAgentHistory:
        @staticmethod
        def get_interacted_element(_model_output, selector_map):
            assert selector_map == {7: "live-node"}
            return [element]

    browser_use_module = types.ModuleType("browser_use")
    browser_use_module.__path__ = []
    agent_module = types.ModuleType("browser_use.agent")
    agent_module.__path__ = []
    views_module = types.ModuleType("browser_use.agent.views")
    views_module.AgentHistory = _FakeAgentHistory
    monkeypatch.setitem(sys.modules, "browser_use", browser_use_module)
    monkeypatch.setitem(sys.modules, "browser_use.agent", agent_module)
    monkeypatch.setitem(sys.modules, "browser_use.agent.views", views_module)

    state = SimpleNamespace(
        url="https://example.test/products",
        dom_state=SimpleNamespace(selector_map={7: "live-node"}),
    )
    cache = {}
    stats = asyncio.run(
        enrich_browser_use_step_snapshot(state, object(), _Context(), cache)
    )
    assert stats["unique"] == 1
    assert len(cache) == 1


def test_verified_live_candidate_wins_and_unverified_id_is_only_a_hint():
    verified_step = Step(action="click")
    _apply_interacted_element(verified_step, {
        "attributes": {"id": "fallback"},
        "selector_candidates": [{
            "value": '[data-testid="checkout"]',
            "strategy": "data-testid",
            "selectorType": "css",
            "unique": True,
            "matchCount": 1,
            "verifiedAtCapture": True,
            "stability": "high",
            "priority": 5,
        }],
    })
    assert verified_step.selector.value == '[data-testid="checkout"]'
    assert verified_step.selector.verifiedAtCapture is True

    hint_step = Step(action="click")
    _apply_interacted_element(hint_step, {"attributes": {"id": "customer.firstName"}})
    assert hint_step.selector.value == '[id="customer.firstName"]'
    assert hint_step.selector.verifiedAtCapture is False
    classified, anomalies, _ = classify_steps([hint_step])
    assert classified[0].included_in_replay is False
    assert classified[0].replay_blocking is True
    assert "non verifie unique" in anomalies[0]


def test_legacy_unique_flag_without_runtime_proof_is_rejected():
    legacy = Step(
        id="step-0001",
        action="click",
        selector=Selector(value="#looks-unique", unique=True, matchCount=1),
    )
    classified, anomalies, _ = classify_steps([legacy])
    assert classified[0].included_in_replay is False
    assert classified[0].replay_blocking is True
    assert "non verifie unique" in anomalies[0]


def test_low_stability_live_candidate_is_not_promoted():
    step = Step(action="click")
    _apply_interacted_element(step, {
        "x_path": "/html/body/div[3]/button[2]",
        "selector_candidates": [{
            "value": "/html/body/div[3]/button[2]",
            "strategy": "xpath",
            "selectorType": "xpath",
            "unique": True,
            "matchCount": 1,
            "verifiedAtCapture": True,
            "stability": "low",
            "priority": 90,
        }],
    })
    assert step.selector.verifiedAtCapture is False
    classified, _, _ = classify_steps([step])
    assert classified[0].included_in_replay is False
    assert classified[0].replay_blocking is True


def test_verified_dom_selector_is_not_overwritten_by_unverified_bu_hint():
    steps = build_pre_cleanup_steps(
        scenario_steps=None,
        bu_history=[{
            "metadata": {"step_start_time": 1000, "step_end_time": 1200},
            "normalized_actions": [{
                "action": {"click_element": {"index": 1}},
                "action_index": 0,
                "interacted_element": {"attributes": {"data-product-id": "1"}},
            }],
        }],
        dom_log=[{
            "action": "click",
            "timestamp": 1100,
            "selector": {"value": "form#signup button", "unique": True, "matchCount": 1},
            "attributes": {"data-product-id": "1"},
        }],
    )
    assert len(steps) == 1
    assert steps[0].selector.value == "form#signup button"
    assert steps[0].selector.captureSource == "dom_listener"


def test_better_verified_live_candidate_replaces_structural_dom_selector():
    live = {
        "attributes": {"data-testid": "checkout"},
        "selector_candidates": [{
            "value": '[data-testid="checkout"]',
            "strategy": "data-testid",
            "selectorType": "css",
            "unique": True,
            "matchCount": 1,
            "verifiedAtCapture": True,
            "stability": "high",
            "priority": 5,
        }],
    }
    steps = build_pre_cleanup_steps(
        None,
        [{
            "metadata": {"step_start_time": 1000, "step_end_time": 1200},
            "normalized_actions": [{
                "action": {"click_element": {"index": 1}},
                "action_index": 0,
                "interacted_element": live,
            }],
        }],
        [{
            "action": "click", "timestamp": 1100,
            "selector": {"strategy": "css-short", "value": "main div button:nth-of-type(2)", "unique": True, "matchCount": 1},
            "attributes": {"data-testid": "checkout"},
        }],
    )
    assert steps[0].selector.value == '[data-testid="checkout"]'
    assert steps[0].selector.captureSource == "browser_use_live_cdp"


def test_raw_evaluate_is_excluded_and_generator_refuses_forced_evaluate(tmp_path):
    step = Step(
        id="step-0001",
        action="evaluate",
        value="document.querySelector('#pay').click()",
    )
    classified, anomalies, _ = classify_steps([step])
    assert classified[0].included_in_replay is False
    assert any("evaluate brut" in anomaly for anomaly in anomalies)

    step.included_in_replay = True
    clean = CleanSteps(parcours="forced evaluate", total_steps=1, steps=[step])
    result = generate_playwright_ts(clean, tmp_path / "forced.spec.ts")
    body = (tmp_path / "forced.spec.ts").read_text(encoding="utf-8")
    assert len(result["unsupported"]) == 1
    assert "evaluate JavaScript brut interdit" in body
    assert "page.evaluate" not in body


def test_temporally_adjacent_evaluate_is_not_fused_without_same_target():
    canonical = Step(
        id="step-0001", action="click", timestamp=1000, source="bu+dom",
        selector=Selector(value="#checkout", unique=True, matchCount=1),
    )
    evaluate = Step(
        id="step-0002", action="evaluate", timestamp=1100,
        value="document.querySelector('#delete-account').click()",
    )
    _fuse_evaluate_workarounds([canonical, evaluate])
    assert evaluate.included_in_replay is True
    assert evaluate.cleanup_reason is None


def test_evaluate_promotes_confirmed_dom_checkbox_events(tmp_path):
    evaluate_first = (
        "(function(){var firstToggle=document.querySelectorAll('.todo-list .toggle')[0];"
        "firstToggle.checked=true;firstToggle.dispatchEvent(new Event('change',{bubbles:true}));})()"
    )
    evaluate_second = (
        "(function(){var els=document.querySelectorAll('.todo-list .toggle');"
        "els[1].click();})()"
    )
    bu_history = [{
        "metadata": {"step_start_time": 1000, "step_end_time": 1300},
        "normalized_actions": [
            {
                "action": {"evaluate": {"code": evaluate_first}},
                "action_index": 0,
                "interacted_element": None,
            },
            {
                "action": {"evaluate": {"code": evaluate_second}},
                "action_index": 1,
                "interacted_element": None,
            },
        ],
    }]

    def _event(action, timestamp, label):
        event = {
            "action": action,
            "timestamp": timestamp,
            "tag": "INPUT",
            "value": "true" if action == "check" else None,
            "text": label,
            "parentLabel": label,
            "parentLabelMatchCount": 1,
            "isTrusted": False,
            "attributes": {"class": "toggle", "type": "checkbox"},
            "selector": {
                "strategy": "class-scope",
                "value": "input.toggle",
                "unique": False,
                "matchCount": 4,
            },
            "url": "https://demo.playwright.dev/todomvc/",
        }
        # Reproduit le run CRD-7 reel : le click porte la preuve d'unicite
        # dans le <li>, mais le change check n'expose pas la mesure checkbox.
        if action == "click":
            event["parentScopedMatchCount"] = 1
        return event

    dom_log = [
        _event("click", 1100, "acheter du pain"),
        _event("check", 1101, "acheter du pain"),
        _event("click", 1200, "appeler le medecin"),
        _event("check", 1201, "appeler le medecin"),
    ]
    steps = build_pre_cleanup_steps(None, bu_history, dom_log, None)
    steps, anomalies, _ = classify_steps(steps)

    checks = [step for step in steps if step.action == "check" and step.included_in_replay]
    evaluates = [step for step in steps if step.action == "evaluate"]
    clicks = [step for step in steps if step.action == "click"]
    assert [step.raw_payload["parentLabel"] for step in checks] == [
        "acheter du pain",
        "appeler le medecin",
    ]
    assert all(step.source == "evaluate+dom" for step in checks)
    assert all(not step.replay_blocking for step in checks)
    assert all(step.raw_payload["parentScopedMatchCount"] == 1 for step in checks)
    assert all(step.raw_payload.get("parentCheckboxMatchCount") is None for step in checks)
    assert all(step.raw_payload.get("context_proof_from_click") for step in checks)
    assert all(not step.included_in_replay for step in evaluates)
    assert all("fusionne en action" in step.cleanup_reason for step in evaluates)
    assert all(not step.included_in_replay for step in clicks)
    assert not any("non verifie unique" in anomaly for anomaly in anomalies)

    clean = CleanSteps(parcours="evaluate dom proof", total_steps=len(steps), steps=steps)
    generate_playwright_ts(clean, tmp_path / "evaluate-proof.spec.ts")
    body = (tmp_path / "evaluate-proof.spec.ts").read_text(encoding="utf-8")
    assert body.count(".setChecked(true)") == 2
    assert body.count("getByRole('listitem').filter") == 2
    assert body.count('.locator("input.toggle").setChecked(true)') == 2
    assert "page.evaluate" not in body

    final_ids = {step.id for step in steps}
    for step in evaluates:
        referenced_id = step.cleanup_reason.rsplit(" ", 1)[-1]
        assert referenced_id in final_ids


def test_excluded_checkbox_observation_cannot_swallow_promoted_click():
    selector = Selector(
        strategy="class-scope",
        value="input.toggle",
        unique=False,
        matchCount=4,
        verifiedAtCapture=True,
    )
    promoted_click = Step(
        id="step-0001",
        action="click",
        timestamp=1000,
        selector=selector,
        source="evaluate+dom",
        included_in_replay=True,
        raw_payload={
            "parentLabel": "acheter du pain",
            "parentLabelMatchCount": 1,
            "parentScopedMatchCount": 1,
        },
    )
    excluded_check = Step(
        id="step-0002",
        action="check",
        timestamp=1001,
        selector=selector,
        source="dom_orphan",
        included_in_replay=False,
        cleanup_reason="observation DOM sans action Browser Use correspondante",
        raw_payload={"parentLabel": "acheter du pain"},
    )

    _fuse_checkbox_interactions([promoted_click, excluded_check])

    assert promoted_click.included_in_replay is True
    assert excluded_check.included_in_replay is False


def test_dom_event_count_replaces_duplicate_bu_click_intents(tmp_path):
    interacted = {
        "attributes": {"class": "clear-completed"},
        "selector_candidates": [{
            "value": "footer.footer button",
            "strategy": "ancestor-scope",
            "selectorType": "css",
            "unique": True,
            "matchCount": 1,
            "verifiedAtCapture": True,
            "stability": "medium",
            "priority": 27,
        }],
    }
    bu_history = []
    for start in (1000, 1200):
        bu_history.append({
            "metadata": {"step_start_time": start, "step_end_time": start + 100},
            "normalized_actions": [{
                "action": {"click_element": {"index": 7}},
                "action_index": 0,
                "interacted_element": interacted,
            }],
        })
    dom_log = [{
        "action": "click",
        "timestamp": 2000,
        "tag": "BUTTON",
        "text": "Clear completed",
        "isTrusted": True,
        "attributes": {"class": "clear-completed"},
        "selector": {
            "strategy": "class-scope",
            "value": "button.clear-completed",
            "unique": True,
            "matchCount": 1,
        },
        "url": "https://demo.playwright.dev/todomvc/",
    }]

    steps = build_pre_cleanup_steps(None, bu_history, dom_log, None)
    steps, anomalies, _ = classify_steps(steps)
    included_clicks = [step for step in steps if step.action == "click" and step.included_in_replay]
    bu_clicks = [step for step in steps if step.source == "browser_use_history"]
    assert len(included_clicks) == 1
    assert included_clicks[0].source == "bu+dom-reconciled"
    assert all(not step.included_in_replay for step in bu_clicks)
    assert anomalies == []

    clean = CleanSteps(parcours="dedup confirmed clicks", total_steps=len(steps), steps=steps)
    generate_playwright_ts(clean, tmp_path / "dedup-clicks.spec.ts")
    body = (tmp_path / "dedup-clicks.spec.ts").read_text(encoding="utf-8")
    assert body.count(".click();") == 1


def test_matched_dom_event_absorbs_duplicate_bu_click_intent(tmp_path):
    interacted = {
        "attributes": {"class": "clear-completed"},
        "selector_candidates": [{
            "value": "footer.footer button",
            "strategy": "ancestor-scope",
            "selectorType": "css",
            "unique": True,
            "matchCount": 1,
            "verifiedAtCapture": True,
            "stability": "medium",
            "priority": 27,
        }],
    }
    bu_history = [
        {
            "metadata": {"step_start_time": 1000, "step_end_time": 1100},
            "normalized_actions": [{
                "action": {"click_element": {"index": 7}},
                "action_index": 0,
                "interacted_element": interacted,
            }],
        },
        {
            "metadata": {"step_start_time": 1200, "step_end_time": 1300},
            "normalized_actions": [{
                "action": {"click_element": {"index": 7}},
                "action_index": 0,
                "interacted_element": interacted,
            }],
        },
    ]
    dom_log = [{
        "action": "click",
        "timestamp": 1050,
        "tag": "BUTTON",
        "text": "Clear completed",
        "isTrusted": True,
        "attributes": {"class": "clear-completed"},
        "selector": {
            "strategy": "class-scope",
            "value": "button.clear-completed",
            "unique": True,
            "matchCount": 1,
        },
        "url": "https://demo.playwright.dev/todomvc/",
    }]

    steps = build_pre_cleanup_steps(None, bu_history, dom_log, None)
    steps, anomalies, _ = classify_steps(steps)
    included_clicks = [step for step in steps if step.action == "click" and step.included_in_replay]
    assert len(included_clicks) == 1
    assert included_clicks[0].source == "bu+dom-reconciled"
    assert included_clicks[0].selector.value == "footer.footer button"
    assert anomalies == []

    clean = CleanSteps(parcours="matched event wins", total_steps=len(steps), steps=steps)
    generate_playwright_ts(clean, tmp_path / "matched-event.spec.ts")
    body = (tmp_path / "matched-event.spec.ts").read_text(encoding="utf-8")
    assert body.count(".click();") == 1


def test_shared_button_type_is_not_target_identity():
    interacted = {
        "attributes": {"class": "save-profile", "type": "button"},
        "selector_candidates": [{
            "value": "button.save-profile",
            "strategy": "class-scope",
            "unique": True,
            "matchCount": 1,
            "verifiedAtCapture": True,
            "stability": "medium",
            "priority": 27,
        }],
    }
    bu_history = [{
        "metadata": {"step_start_time": 1000, "step_end_time": 1100},
        "normalized_actions": [{
            "action": {"click_element": {"index": 3}},
            "action_index": 0,
            "interacted_element": interacted,
        }],
    }]
    dom_log = [{
        "action": "click",
        "timestamp": 1050,
        "tag": "BUTTON",
        "text": "Cancel",
        "isTrusted": True,
        "attributes": {"class": "cancel-dialog", "type": "button"},
        "selector": {
            "strategy": "class-scope",
            "value": "button.cancel-dialog",
            "unique": True,
            "matchCount": 1,
        },
        "url": "https://example.test/settings",
    }]

    steps = build_pre_cleanup_steps(None, bu_history, dom_log, None)
    included = [step for step in steps if step.included_in_replay]
    orphans = [step for step in steps if step.source == "dom_orphan"]
    assert len(included) == 1
    assert included[0].source == "browser_use_history"
    assert included[0].selector.value == "button.save-profile"
    assert len(orphans) == 1
    assert not orphans[0].included_in_replay


def test_bu_data_attribute_click_never_uses_first(tmp_path):
    step = Step(
        id="step-0001",
        action="click",
        selector=Selector(
            strategy="data-attr",
            value='[data-product-id="1"]',
            unique=True,
            matchCount=1,
            verifiedAtCapture=True,
        ),
    )
    clean = CleanSteps(parcours="strict click", total_steps=1, steps=[step])
    generate_playwright_ts(clean, tmp_path / "strict.spec.ts")
    body = (tmp_path / "strict.spec.ts").read_text(encoding="utf-8")
    assert 'page.locator("[data-product-id=\\"1\\"]").click()' in body
    assert ".first().click()" not in body
