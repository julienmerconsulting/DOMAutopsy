"""Test de regression du run R9 TodoMVC (fixture figee).

Verifie que le pipeline DOMAutopsy applique aux artifacts bruts capture
lors du run R9 real produit :
- 4 saisies distinctes avec les 4 valeurs exactes des taches
- 4 Enter (pas 8 via dedup BU+DOM, pas 1 via fusion input globale bugguee)
- 0 saisie "on" sur checkbox (filtre listener)
- Les contournements evaluate positionnels de cette ancienne capture sont
  refuses : elle doit etre recapturee pour obtenir les preuves live
"""
import json
from pathlib import Path

import pytest

from clean_steps_builder import build_clean_steps
from playwright_generator import generate_playwright_ts
from deterministic_exporters import export_katalon, validate_export_counts


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "r9_todomvc"
ROOT = Path(__file__).parent.parent


@pytest.fixture
def r9_rebuild(tmp_path):
    """Rebuild deterministe depuis les artifacts R9 figes.
    Retourne (clean_steps, spec_path, run_dir)."""
    bu = json.loads((FIXTURE_DIR / "browser_use_history.json").read_text(encoding="utf-8"))
    dom = json.loads((FIXTURE_DIR / "locator_dedup.json").read_text(encoding="utf-8"))
    net = json.loads((FIXTURE_DIR / "network_log.json").read_text(encoding="utf-8"))
    # Post-traitement local strict : api_key/model sont ignores.
    clean, _ = build_clean_steps(
        scenario_name="r9 regression",
        scenario_url="https://demo.playwright.dev/todomvc/",
        scenario_steps=None,
        bu_history=bu, dom_log=dom, network_log=net,
        model="gpt-5-mini", base_url=None, api_key=None,
    )
    spec = tmp_path / "test_playwright.spec.ts"
    generate_playwright_ts(clean, spec, parcours_url="https://demo.playwright.dev/todomvc/")
    return clean, spec, tmp_path


# ============================================================
# Invariants semantiques
# ============================================================

def test_r9_has_exactly_4_distinct_inputs_with_correct_values(r9_rebuild):
    """4 saisies distinctes avec les 4 valeurs exactes des taches."""
    clean, _, _ = r9_rebuild
    inputs = [s for s in clean.steps if s.action == "input" and s.included_in_replay]
    values = [s.value for s in inputs]
    expected = [
        "Preparer le rapport QA",
        "Verifier les selecteurs",
        "Controler les requetes reseau",
        "Valider le replay",
    ]
    assert len(inputs) == 4, f"Attendu 4 inputs, obtenu {len(inputs)} : {values}"
    for exp_val in expected:
        assert exp_val in values, f"Valeur '{exp_val}' absente des inputs : {values}"


def test_r9_has_4_keyboards_enter_not_8_not_1(r9_rebuild):
    """4 Enter distincts (fusion BU send_keys + DOM keydown, pas de doublon)."""
    clean, _, _ = r9_rebuild
    kbs = [s for s in clean.steps if s.action == "keyboard" and s.included_in_replay]
    enters = [s for s in kbs if (s.value or "").lower() == "enter"]
    assert len(enters) == 4, (
        f"Attendu 4 Enter (fusion BU+DOM), obtenu {len(enters)}. "
        f"8 = doublon non fusionne, 1 = consolidation abusive."
    )


def test_r9_no_parasitic_on_input_from_checkbox(r9_rebuild):
    """Aucune saisie 'on' (valeur par defaut d'une checkbox HTML) ne doit
    apparaitre en input rejouable. Le DOM listener filtre les input events
    sur type=checkbox/radio depuis R9."""
    clean, _, _ = r9_rebuild
    parasitic = [s for s in clean.steps
                 if s.action == "input" and s.included_in_replay
                 and (s.value or "").lower() == "on"]
    assert parasitic == [], f"Saisie 'on' parasite trouvee : {parasitic}"


def test_r9_legacy_checkbox_without_live_proof_is_not_replayable(r9_rebuild):
    """Cette fixture predatant les comptages contextuels live ne doit pas
    etre promue artificiellement en interaction checkbox fiable."""
    clean, _, _ = r9_rebuild
    canonical_checks = [
        s for s in clean.steps
        if s.action in ("check", "uncheck") and s.included_in_replay
    ]
    assert canonical_checks == []
    legacy_checks = [s for s in clean.steps if s.action in ("check", "uncheck")]
    assert len(legacy_checks) == 1
    assert legacy_checks[0].source == "dom_orphan"
    assert legacy_checks[0].included_in_replay is False


def test_r9_positional_evaluate_is_excluded(r9_rebuild):
    """Le evaluate querySelectorAll(...)[1] ne devient jamais du TS."""
    clean, _, _ = r9_rebuild
    evaluates = [
        s for s in clean.steps
        if s.action == "evaluate"
    ]
    assert evaluates
    assert all(not step.included_in_replay for step in evaluates)
    assert all("non canonique" in (step.cleanup_reason or "") for step in evaluates)


def test_r9_katalon_export_matches_json_counts(r9_rebuild):
    """L'export Katalon derive strictement du clean_steps : count actions
    identique, ordre preserve, chaque step_id present."""
    clean, _, _ = r9_rebuild
    katalon = export_katalon(clean)
    anomalies = validate_export_counts(clean, katalon, "katalon")
    assert anomalies == [], f"Export Katalon incoherent : {anomalies}"


# ============================================================
# E2E replay reel (marker slow : lourde, skippable)
# ============================================================

def _has_npx():
    import shutil
    return shutil.which("npx") is not None


def _has_node_modules():
    return (ROOT / "node_modules" / "@playwright" / "test").exists()


def test_r9_generated_ts_contains_no_raw_evaluate(r9_rebuild):
    """L'ancien workaround JS reste visible dans le JSON mais pas dans le TS."""
    _, spec_path, _ = r9_rebuild
    body = spec_path.read_text(encoding="utf-8")
    assert "page.evaluate" not in body
    assert "SKIPPED" in body
