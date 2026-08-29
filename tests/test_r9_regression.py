"""Test de regression du run R9 TodoMVC (fixture figee).

Verifie que le pipeline DOMAutopsy applique aux artifacts bruts capture
lors du run R9 real produit :
- 4 saisies distinctes avec les 4 valeurs exactes des taches
- 4 Enter (pas 8 via dedup BU+DOM, pas 1 via fusion input globale bugguee)
- 0 saisie "on" sur checkbox (filtre listener)
- 1 seule interaction canonique sur la checkbox 'Verifier les selecteurs'
- Click ambigu fusionne dans le check canonique via parentLabel
- Le TS canonique genere passe reellement dans Chromium via
  `npx playwright test` (test E2E) - stabilite 3x consecutif
"""
import json
import subprocess
import sys
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
    # api_key=None -> ai_classify_steps echoue silencieusement, on garde
    # les steps tels quels (pipeline deterministe pur).
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


def test_r9_single_canonical_checkbox_interaction(r9_rebuild):
    """Une SEULE interaction canonique sur la checkbox, pas 3 (click +
    change + evaluate) qui aurait produit 3 toggles au replay."""
    clean, _, _ = r9_rebuild
    canonical_checks = [
        s for s in clean.steps
        if s.action in ("check", "uncheck") and s.included_in_replay
    ]
    assert len(canonical_checks) == 1, (
        f"Attendu 1 canonique check/uncheck, obtenu {len(canonical_checks)}"
    )
    # Doit porter le parentLabel de la bonne tache
    raw = canonical_checks[0].raw_payload or {}
    assert raw.get("parentLabel") == "Verifier les selecteurs"


def test_r9_ambiguous_click_fused_into_canonical_check(r9_rebuild):
    """Le click DOM sur [aria-label='Toggle Todo'] doit etre fusionne
    (included_in_replay=False) car son intent est deja capture par
    le check canonique."""
    clean, _, _ = r9_rebuild
    ambiguous_clicks = [
        s for s in clean.steps
        if s.action == "click"
        and not s.included_in_replay
        and "fusionne dans" in (s.cleanup_reason or "")
    ]
    assert len(ambiguous_clicks) >= 1, (
        "Attendu au moins 1 click DOM fusionne dans le check canonique"
    )


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


@pytest.mark.skipif(not _has_npx() or not _has_node_modules(),
                    reason="npx ou @playwright/test absent")
def test_r9_ts_replay_passes_on_real_chromium(r9_rebuild):
    """Le TS canonique regenere depuis les artifacts R9 doit passer
    reellement dans Chromium via `npx playwright test`. Preuve que la
    fusion + parentLabel + setChecked eliminent les strict mode violations
    sur les selecteurs ambigus TodoMVC."""
    clean, spec_path, tmp_path = r9_rebuild
    # spec_path est dans tmp_path : on doit le placer sous ROOT/runs/
    # pour que le chemin relatif marche pour npx playwright test.
    dst_dir = ROOT / "runs" / "_r9_regression_tmp"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst_spec = dst_dir / "test_playwright.spec.ts"
    dst_spec.write_text(spec_path.read_text(encoding="utf-8"), encoding="utf-8")
    spec_rel = dst_spec.relative_to(ROOT).as_posix()

    try:
        cmd = ["npx", "playwright", "test", spec_rel, "--workers=1", "--reporter=list"]
        if sys.platform == "win32":
            cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
            result = subprocess.run(
                cmd_str, shell=True, cwd=str(ROOT),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
        else:
            result = subprocess.run(
                cmd, cwd=str(ROOT),
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120,
            )
        assert result.returncode == 0, (
            f"Replay R9 fail (exit {result.returncode})\n"
            f"stdout: {(result.stdout or '')[-2000:]}\n"
            f"stderr: {(result.stderr or '')[-1000:]}"
        )
    finally:
        import shutil as _sh
        _sh.rmtree(dst_dir, ignore_errors=True)
