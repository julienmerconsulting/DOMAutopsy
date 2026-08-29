"""Tests R5 - exports deterministes Katalon/Cypress/Selenium et validation.

Chaque exporter doit produire exactement 1 statement par step
included_in_replay=True, sans reordonner ni ajouter/supprimer.
"""
from schemas import CleanSteps, Step, Selector
from deterministic_exporters import (
    export_katalon, export_cypress, export_selenium,
    validate_export_counts, EXPORTERS,
)


def _cs(*steps: Step) -> CleanSteps:
    return CleanSteps(
        parcours="test", scenario_url="https://x",
        total_steps=len(steps), steps=list(steps),
    )


# ============================================================
# Chaque exporter respecte le count et l'ordre
# ============================================================

def _todomvc_12_saisies() -> CleanSteps:
    """Simule le pattern TodoMVC : 12 (input + Enter) = 24 steps."""
    steps = []
    idx = 1
    for todo in [f"Tache {i}" for i in range(1, 13)]:
        steps.append(Step(
            id=f"step-{idx:04d}", step=idx, action="input",
            selector=Selector(value="input.new-todo", strategy="css"),
            value=todo,
        ))
        idx += 1
        steps.append(Step(
            id=f"step-{idx:04d}", step=idx, action="keyboard", value="Enter",
        ))
        idx += 1
    return CleanSteps(parcours="12 taches", scenario_url="https://x",
                     total_steps=len(steps), steps=steps)


def test_katalon_export_produces_12_inputs_and_12_enters():
    """R5 : bug ancien produisait 74 Enter pour 46 attendus. Fix : 12=12."""
    cs = _todomvc_12_saisies()
    code = export_katalon(cs)
    assert code.count("WebUI.setText") == 12
    assert code.count("Keys.ENTER") == 12


def test_cypress_export_produces_12_inputs_and_12_enters():
    cs = _todomvc_12_saisies()
    code = export_cypress(cs)
    # cy.get(...).clear().type(...) x 12 pour les inputs
    assert code.count(".clear().type(") == 12
    # cy.get('body').type('{enter}') x 12
    assert code.count("'{enter}'") == 12


def test_selenium_export_produces_12_inputs_and_12_enters():
    cs = _todomvc_12_saisies()
    code = export_selenium(cs)
    assert code.count("send_keys(\"") == 12
    assert code.count("Keys.ENTER") == 12


# ============================================================
# Validation cohérence
# ============================================================

def test_validate_flags_missing_step_id():
    """Si un step attendu n'apparait pas dans l'export -> anomalie."""
    cs = _cs(
        Step(id="step-0001", step=1, action="click",
             selector=Selector(value="#a", strategy="id")),
        Step(id="step-0002", step=2, action="click",
             selector=Selector(value="#b", strategy="id")),
    )
    export_ok = export_katalon(cs)
    anomalies = validate_export_counts(cs, export_ok, "katalon")
    assert anomalies == []

    # Simule un export qui a PERDU step-0002 (toutes references retirees)
    import re
    export_bad = re.sub(r"step-0002", "step-XXXX", export_ok)
    anomalies_bad = validate_export_counts(cs, export_bad, "katalon")
    assert len(anomalies_bad) >= 1
    assert any("step-0002" in a for a in anomalies_bad)


def test_validate_flags_out_of_order():
    """L'ordre des steps doit etre preserve : une inversion doit
    remonter une anomalie ORDRE explicite."""
    cs = _cs(
        Step(id="step-0001", step=1, action="click",
             selector=Selector(value="#a", strategy="id")),
        Step(id="step-0002", step=2, action="click",
             selector=Selector(value="#b", strategy="id")),
    )
    # Inverse les 2 headers dans l'export
    export_ok = export_katalon(cs)
    export_swapped = export_ok.replace("[step-0001]", "TMPA").replace("[step-0002]", "[step-0001]").replace("TMPA", "[step-0002]")
    anomalies = validate_export_counts(cs, export_swapped, "katalon")
    assert any("ORDRE" in a for a in anomalies)


def test_validate_flags_ghost_step_in_export():
    """Un step-XXXX qui n'existe pas dans clean_steps doit lever une
    anomalie fantome."""
    cs = _cs(Step(id="step-0001", step=1, action="click",
                  selector=Selector(value="#a", strategy="id")))
    export = export_katalon(cs) + "\n// [step-9999] injected ghost"
    anomalies = validate_export_counts(cs, export, "katalon")
    assert any("fantome" in a for a in anomalies)


def test_validate_semantic_by_action_type_katalon():
    """R5 : la semantique par action_type doit matcher. Si clean_steps a
    2 inputs + 1 click + 1 navigate, l'export Katalon doit avoir
    exactement 2 WebUI.setText + 1 WebUI.click + 1 WebUI.navigateToUrl."""
    from deterministic_exporters import validate_export_by_action_type
    cs = CleanSteps(
        parcours="test", scenario_url="https://example.com",
        total_steps=4,
        steps=[
            # navigate initial explicite pour matcher le navigateToUrl que
            # Katalon emet toujours en tete depuis scenario_url
            Step(id="step-0001", step=1, action="navigate", url="https://example.com"),
            Step(id="step-0002", step=2, action="input",
                 selector=Selector(value="#a", strategy="id"), value="v1"),
            Step(id="step-0003", step=3, action="input",
                 selector=Selector(value="#b", strategy="id"), value="v2"),
            Step(id="step-0004", step=4, action="click",
                 selector=Selector(value="#btn", strategy="id")),
        ],
    )
    ok = export_katalon(cs)
    anomalies = validate_export_by_action_type(cs, ok, "katalon")
    assert anomalies == [], f"Katalon coherent devrait retourner [], got: {anomalies}"


def test_validate_semantic_detects_injection():
    """Si un exporter injecte un statement en trop (setText fantome),
    la validation semantique le detecte."""
    from deterministic_exporters import validate_export_by_action_type
    cs = _cs(
        Step(id="step-0001", step=1, action="input",
             selector=Selector(value="#a", strategy="id"), value="hello"),
    )
    tampered = export_katalon(cs) + "\nWebUI.setText(injected, 'fantome')\n"
    anomalies = validate_export_by_action_type(cs, tampered, "katalon")
    assert any("input" in a.lower() for a in anomalies)


def test_validate_flags_count_mismatch():
    """Si l'export contient un nombre different de step markers -> anomalie."""
    cs = _cs(
        Step(id="step-0001", step=1, action="click",
             selector=Selector(value="#a", strategy="id")),
        Step(id="step-0002", step=2, action="click",
             selector=Selector(value="#b", strategy="id")),
    )
    # Export qui ajoute un step-0099 fantome
    export_extra = export_katalon(cs) + "\n// [step-0099] injected"
    anomalies = validate_export_counts(cs, export_extra, "katalon")
    assert any("nombre de steps" in a for a in anomalies)


def test_all_exporters_registered():
    """3 exporters attendus : katalon, cypress, selenium."""
    assert set(EXPORTERS.keys()) == {"katalon", "cypress", "selenium"}


# ============================================================
# Actions non supportees generent commentaire explicite pas silence
# ============================================================

def test_extract_produces_documented_skip_in_all_formats():
    """extract est non-rejouable : chaque exporter emit un commentaire
    explicite, jamais un statement silencieux."""
    cs = _cs(Step(id="step-0001", step=1, action="extract",
                  description="lit titre", included_in_replay=True))
    for fmt, exporter in EXPORTERS.items():
        code = exporter(cs)
        assert "extract" in code.lower() or "non rejouable" in code.lower(), (
            f"{fmt} : extract doit apparaitre en commentaire explicite"
        )


def test_included_in_replay_false_excluded_from_export():
    """Steps marques included_in_replay=False ne doivent PAS apparaitre
    dans l'export (le JSON les garde pour la tracabilite mais l'export
    ne joue que ce qui est rejouable)."""
    cs = _cs(
        Step(id="step-0001", step=1, action="click",
             selector=Selector(value="#a", strategy="id"),
             included_in_replay=True),
        Step(id="step-0002", step=2, action="click",
             selector=Selector(value="#overlay", strategy="css"),
             included_in_replay=False,
             cleanup_reason="clic overlay parasite"),
    )
    for fmt, exporter in EXPORTERS.items():
        code = exporter(cs)
        assert "step-0001" in code
        assert "step-0002" not in code, f"{fmt} : step-0002 filtre ne doit pas apparaitre"


def test_sensitive_input_uses_env_var_in_all_formats():
    """R5 + regle sensitive : valeur sensible substituee par env var,
    jamais en clair, dans les 3 formats."""
    cs = _cs(Step(
        id="step-0001", step=1, action="input",
        selector=Selector(value="input[type='password']", strategy="css"),
        value="realpassword_should_not_appear", sensitive=True,
        env_var="DOMAUTOPSY_TEST_PWD",
    ))
    for fmt, exporter in EXPORTERS.items():
        code = exporter(cs)
        assert "realpassword_should_not_appear" not in code, f"{fmt} : valeur sensible fuit !"
        assert "DOMAUTOPSY_TEST_PWD" in code
