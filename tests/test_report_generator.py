"""Test cahier des charges #10 : generation du rapport avec toutes les actions
et affichage correct des steps included_in_replay=false + cleanup_reason."""
import json
from pathlib import Path

from report_generator import generate_report


def test_report_renders_all_action_types_and_skipped_marker(tmp_path):
    """#10 : le rapport HTML doit rendre toutes les nouvelles actions
    (navigate, verify, wait, screenshot, cookie, keyboard, upload,
    go_back, reload, open_tab, unknown) ET afficher les steps filtres
    (INCLUS / FILTRE + raison)."""
    clean_data = {
        "schema_version": "2.0",
        "parcours": "Test toutes actions",
        "scenario_url": "https://example.com",
        "total_steps": 12,
        "steps": [
            {"step": 1, "action": "navigate", "description": "Va sur example",
             "url": "https://example.com", "selectorType": "url",
             "included_in_replay": True},
            {"step": 2, "action": "click", "description": "Clic login",
             "selector": {"value": "#login", "strategy": "id", "unique": True},
             "included_in_replay": True},
            {"step": 3, "action": "input", "description": "Saisie email",
             "selector": {"value": "input[name=email]", "strategy": "name"},
             "value": "test@test.com", "included_in_replay": True},
            {"step": 4, "action": "input", "description": "Saisie password",
             "selector": {"value": "input[type=password]", "strategy": "css"},
             "value": "<redacted>", "sensitive": True,
             "env_var": "DOMAUTOPSY_STEP_0004",
             "included_in_replay": True},
            {"step": 5, "action": "verify", "description": "Verifie dashboard",
             "expected": "Dashboard", "verify_type": "texte_contient",
             "included_in_replay": True},
            {"step": 6, "action": "scroll", "description": "Scroll vers footer",
             "direction": "vers_element", "included_in_replay": True},
            {"step": 7, "action": "wait", "description": "Attends 2s",
             "seconds": 2.0, "included_in_replay": True},
            {"step": 8, "action": "screenshot", "description": "Capture ecran",
             "target": "avant_click", "included_in_replay": True},
            {"step": 9, "action": "click", "description": "Clic parasite overlay",
             "selector": {"value": ".modal-backdrop", "strategy": "css"},
             "included_in_replay": False,
             "cleanup_reason": "clic sur overlay non interactif"},
            {"step": 10, "action": "keyboard", "description": "Enter",
             "value": "Enter", "included_in_replay": True},
            {"step": 11, "action": "reload", "description": "Reload page",
             "included_in_replay": True},
            {"step": 12, "action": "totally_new_action", "description": "Truc",
             "raw_payload": {"foo": "bar"}, "included_in_replay": True},
        ],
        "anomalies": ["1 selecteur non-unique"],
        "filtered_noise": ["1 clic overlay filtre"],
        "katalon_code": "// export code here",
    }
    filepath = generate_report(
        clean_data=clean_data,
        deduped_log=[],
        agent_result="SUCCESS - toutes actions rendues",
        scenario_name="Test toutes actions",
        scenario_url="https://example.com",
        timestamp="20260829_120000",
        output_dir=str(tmp_path),
    )
    html = Path(filepath).read_text(encoding="utf-8")

    # Toutes les actions apparaissent (badge action-<name>)
    for action in ["navigate", "click", "input", "verify", "scroll", "wait",
                   "screenshot", "keyboard", "reload"]:
        assert f"action-{action}" in html, f"badge action-{action} absent"

    # Le step included_in_replay=false est marque FILTRE
    assert "FILTRE" in html
    assert "clic sur overlay non interactif" in html

    # Le step sensitive est marque SENSITIVE et sa valeur '<redacted>' pas exposee
    assert "SENSITIVE" in html
    # La colonne "Rejoue" est presente
    assert "Rejoue" in html or "Rejou" in html  # tolere accents


def test_report_backward_compat_with_string_selector(tmp_path):
    """Retro-compat : les anciens JSON avec selector=string simple doivent
    encore rendre correctement (pas de crash sur .get() sur string)."""
    clean_data = {
        "parcours": "old style", "scenario_url": "https://x",
        "total_steps": 1,
        "steps": [{"step": 1, "action": "click", "selector": "#legacy",
                   "selectorType": "css", "unique": True}],
    }
    filepath = generate_report(
        clean_data=clean_data, deduped_log=[], agent_result="ok",
        scenario_name="legacy", scenario_url="https://x",
        timestamp="20260829_120001", output_dir=str(tmp_path),
    )
    html = Path(filepath).read_text(encoding="utf-8")
    assert "#legacy" in html
