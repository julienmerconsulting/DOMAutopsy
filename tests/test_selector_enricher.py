"""Preuves de selecteurs live et consommation deterministe."""

import asyncio
import sys
import types
from types import SimpleNamespace

from clean_steps_builder import (
    _apply_interacted_element,
    _element_to_dict,
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
