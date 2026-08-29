"""
DOMAutopsy - Exporters DETERMINISTES pour Katalon / Cypress / Selenium
========================================================================
Remplace l'ancien generate_export_code() qui utilisait le LLM pour
reconstruire independamment un scenario a partir de clean_steps.

Regle R5 (review round 2) :
- Tous les exports sont derives strictement du meme clean_steps.json
- Le LLM ne peut ni reordonner, ni supprimer, ni ajouter une action
- Chaque action included_in_replay=True produit exactement 1 statement
  dans chaque export (ou une exception documentee si le format ne
  supporte pas l'action)
- Validation automatique : count actions par type dans export ==
  count actions par type dans clean_steps

Format d'un exporter :
    def export_<format>(clean_steps: CleanSteps) -> str
    def validate_export_<format>(clean_steps, export_output) -> list[str]

Actions supportees par format :
                Katalon  Cypress  Selenium
    navigate       ✓        ✓        ✓
    click          ✓        ✓        ✓
    input          ✓        ✓        ✓
    select         ✓        ✓        ✓
    verify         ✓        ✓        ✓
    scroll         ~        ~        ~     (mouse wheel via JS)
    hover          ✓        ✓        ✓
    wait           ✓        ✓        ✓
    keyboard       ✓        ✓        ✓
    upload         ✓        ✓        ✓
    go_back        ✓        ✓        ✓
    reload         ✓        ✓        ✓
    screenshot     ✓        ✓        ✓
    cookie         ~        ~        ~     (try/catch conditionnel)
    open/switch/close_tab  ✗ (limitations frameworks) - commentaire + skip
    extract        ✗ (jamais rejouable, marque non-supporte)
"""

from __future__ import annotations

from typing import Callable
from collections import Counter

from schemas import CleanSteps, Step, Selector


# ============================================================
# Helpers communs
# ============================================================

def _sel_value(step: Step) -> str | None:
    """Extrait la valeur du selecteur (dict ou string)."""
    sel = step.selector
    if sel is None:
        return None
    if isinstance(sel, str):
        return sel
    return getattr(sel, "value", None) or getattr(sel, "playwrightSelector", None)


def _sel_is_xpath(step: Step) -> bool:
    if step.selectorType == "xpath":
        return True
    v = _sel_value(step)
    return bool(v and v.startswith("//"))


def _replayable_steps(clean_steps: CleanSteps) -> list[Step]:
    """Retourne uniquement les steps included_in_replay=True."""
    return [s for s in clean_steps.steps if s.included_in_replay]


# ============================================================
# KATALON exporter (Groovy)
# ============================================================

def export_katalon(clean_steps: CleanSteps) -> str:
    """Genere le code Katalon Studio (Groovy) deterministe."""
    lines = [
        "// Genere deterministiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.",
        f"// Parcours : {clean_steps.parcours or 'sans nom'}",
        "",
        "import com.kms.katalon.core.testobject.TestObject",
        "import com.kms.katalon.core.testobject.ConditionType",
        "import com.kms.katalon.core.webui.keyword.WebUiBuiltInKeywords as WebUI",
        "",
        "WebUI.openBrowser('')",
    ]
    # Emit navigateToUrl initial UNIQUEMENT si aucun step navigate ne
    # va couvrir l'URL scenario en tete (evite le doublon navigate qui
    # casse la validation semantique).
    replayables = _replayable_steps(clean_steps)
    first_step_is_matching_nav = (
        replayables
        and replayables[0].action == "navigate"
        and (replayables[0].url or replayables[0].value) == clean_steps.scenario_url
    )
    if clean_steps.scenario_url and not first_step_is_matching_nav:
        lines.append(f"WebUI.navigateToUrl('{clean_steps.scenario_url}')")
    lines.append("")

    for i, step in enumerate(replayables, start=1):
        lines.extend(_katalon_step(step, i))
        lines.append("")

    lines.append("WebUI.closeBrowser()")
    return "\n".join(lines) + "\n"


def _katalon_step(step: Step, index: int) -> list[str]:
    sid = step.id or f"step-{index:04d}"
    header = f"// [{sid}] {step.action.upper()} - {step.description or ''}"
    val = _sel_value(step)
    sel_type = "xpath" if _sel_is_xpath(step) else "css"
    to_var = f"to_{index}"

    def _make_to():
        return [
            f'TestObject {to_var} = new TestObject("{sid}")',
            f'{to_var}.addProperty("{sel_type}", ConditionType.EQUALS, "{val}")' if val else f'// {to_var} sans selecteur',
        ]

    a = step.action
    if a == "navigate":
        url = step.url or step.value or ""
        return [header, f'WebUI.navigateToUrl("{url}")']
    if a == "click":
        if not val:
            return [header, f'// non-executable : click sans selecteur ({sid})']
        return [header, *_make_to(), f'WebUI.click({to_var})']
    if a == "input":
        if not val:
            return [header, f'// non-executable : input sans selecteur ({sid})']
        v = step.value or ""
        if step.sensitive and step.env_var:
            return [header, *_make_to(), f'WebUI.setText({to_var}, System.getenv("{step.env_var}"))']
        return [header, *_make_to(), f'WebUI.setText({to_var}, "{v}")']
    if a == "select":
        if not val:
            return [header, f'// non-executable : select sans selecteur ({sid})']
        return [header, *_make_to(), f'WebUI.selectOptionByLabel({to_var}, "{step.value or ""}", false)']
    if a == "verify":
        vt = (step.verify_type or "presence").lower()
        expected = step.expected or step.value or ""
        if vt in ("texte_contient", "text_contains"):
            return [header, f'WebUI.verifyTextPresent("{expected}", false)']
        if vt in ("visible",) and val:
            return [header, *_make_to(), f'WebUI.verifyElementVisible({to_var})']
        if vt == "absent" and val:
            return [header, *_make_to(), f'WebUI.verifyElementNotPresent({to_var}, 5)']
        if val:
            return [header, *_make_to(), f'WebUI.verifyElementPresent({to_var}, 10)']
        return [header, f'WebUI.verifyTextPresent("{expected}", false)']
    if a == "scroll":
        if val:
            return [header, *_make_to(), f'WebUI.scrollToElement({to_var}, 5)']
        delta = step.deltaY or 650
        return [header, f'WebUI.executeJavaScript("window.scrollBy(0, {delta})", null)']
    if a == "hover":
        if not val:
            return [header, f'// non-executable : hover sans selecteur ({sid})']
        return [header, *_make_to(), f'WebUI.mouseOver({to_var})']
    if a == "wait":
        if val:
            return [header, *_make_to(), f'WebUI.waitForElementVisible({to_var}, 10)']
        secs = int(step.seconds or 2)
        return [header, f'WebUI.delay({secs})']
    if a == "keyboard":
        key = step.value or "Enter"
        return [header, f'WebUI.sendKeys(null, org.openqa.selenium.Keys.{key.upper()})']
    if a == "upload":
        if not val:
            return [header, f'// non-executable : upload sans selecteur ({sid})']
        return [header, *_make_to(), f'WebUI.uploadFile({to_var}, "{step.value or step.target or ""}")']
    if a in ("go_back", "reload", "go_forward"):
        return [header, {"go_back": "WebUI.back()", "go_forward": "WebUI.forward()", "reload": "WebUI.refresh()"}[a]]
    if a == "screenshot":
        name = step.target or f"capture_{index}"
        return [header, f'WebUI.takeScreenshot("{name}.png")']
    if a == "cookie":
        if val:
            return [header, *_make_to(),
                    'try {',
                    f'    WebUI.click({to_var})',
                    '} catch (Exception _e) { /* cookie banner absent */ }']
        return [header, '// cookie sans selecteur - skip']
    # Actions non supportees Katalon
    if a in ("open_tab", "switch_tab", "close_tab", "extract"):
        return [header, f'// [KATALON] action "{a}" non supportee dans ce format (limitations framework)']
    return [header, f'// action inconnue : {a}']


# ============================================================
# CYPRESS exporter (JavaScript)
# ============================================================

def export_cypress(clean_steps: CleanSteps) -> str:
    """Genere le code Cypress deterministe."""
    lines = [
        "// Genere deterministiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.",
        f"// Parcours : {clean_steps.parcours or 'sans nom'}",
        "",
        f"describe('{(clean_steps.parcours or 'DOMAutopsy replay').replace(chr(39), chr(92) + chr(39))}', () => {{",
        "  it('runs the captured scenario', () => {",
    ]
    replayables = _replayable_steps(clean_steps)
    first_is_nav = (replayables and replayables[0].action == "navigate"
                    and (replayables[0].url or replayables[0].value) == clean_steps.scenario_url)
    if clean_steps.scenario_url and not first_is_nav:
        lines.append(f"    cy.visit('{clean_steps.scenario_url}');")
    for i, step in enumerate(_replayable_steps(clean_steps), start=1):
        lines.extend("    " + l for l in _cypress_step(step, i))
    lines.append("  });")
    lines.append("});")
    return "\n".join(lines) + "\n"


def _cy_get(val: str, is_xpath: bool) -> str:
    if is_xpath:
        return f"cy.xpath('{val}')"
    return f"cy.get('{val.replace(chr(39), chr(92) + chr(39))}')"


def _cypress_step(step: Step, index: int) -> list[str]:
    sid = step.id or f"step-{index:04d}"
    header = f"// [{sid}] {step.action.upper()} - {step.description or ''}"
    val = _sel_value(step)
    is_xp = _sel_is_xpath(step)
    a = step.action

    if a == "navigate":
        return [header, f"cy.visit('{step.url or step.value or ''}');"]
    if a == "click":
        if not val: return [header, f"// non-executable : click sans selecteur ({sid})"]
        return [header, f"{_cy_get(val, is_xp)}.click();"]
    if a == "input":
        if not val: return [header, f"// non-executable : input sans selecteur ({sid})"]
        if step.sensitive and step.env_var:
            return [header, f"{_cy_get(val, is_xp)}.type(Cypress.env('{step.env_var}'));"]
        return [header, f"{_cy_get(val, is_xp)}.clear().type('{(step.value or '').replace(chr(39), chr(92) + chr(39))}');"]
    if a == "select":
        if not val: return [header, f"// non-executable : select sans selecteur ({sid})"]
        return [header, f"{_cy_get(val, is_xp)}.select('{step.value or ''}');"]
    if a == "verify":
        vt = (step.verify_type or "presence").lower()
        expected = step.expected or step.value or ""
        if vt in ("texte_contient", "text_contains"):
            return [header, f"cy.contains('{expected}').should('be.visible');"]
        if vt == "visible" and val:
            return [header, f"{_cy_get(val, is_xp)}.should('be.visible');"]
        if vt == "absent" and val:
            return [header, f"{_cy_get(val, is_xp)}.should('not.exist');"]
        if val:
            return [header, f"{_cy_get(val, is_xp)}.should('exist');"]
        return [header, f"cy.contains('{expected}').should('exist');"]
    if a == "scroll":
        if val:
            return [header, f"{_cy_get(val, is_xp)}.scrollIntoView();"]
        return [header, f"cy.scrollTo(0, {step.deltaY or 650});"]
    if a == "hover":
        if not val: return [header, f"// non-executable : hover sans selecteur ({sid})"]
        return [header, f"{_cy_get(val, is_xp)}.trigger('mouseover');"]
    if a == "wait":
        if val:
            return [header, f"{_cy_get(val, is_xp)}.should('be.visible');"]
        return [header, f"cy.wait({int((step.seconds or 2) * 1000)});"]
    if a == "keyboard":
        key = step.value or "Enter"
        # Cypress : cy.get('body').type('{enter}') pattern
        return [header, f"cy.get('body').type('{{{key.lower()}}}');"]
    if a == "upload":
        if not val: return [header, f"// non-executable : upload sans selecteur ({sid})"]
        return [header, f"{_cy_get(val, is_xp)}.selectFile('{step.value or step.target or ''}');"]
    if a == "go_back":
        return [header, "cy.go('back');"]
    if a == "go_forward":
        return [header, "cy.go('forward');"]
    if a == "reload":
        return [header, "cy.reload();"]
    if a == "screenshot":
        return [header, f"cy.screenshot('{step.target or f'capture_{index}'}');"]
    if a == "cookie":
        if val:
            return [header, f"{_cy_get(val, is_xp)}.click({{timeout: 3000}}).catch(() => {{}});"]
        return [header, "// cookie sans selecteur - skip"]
    if a in ("open_tab", "switch_tab", "close_tab"):
        return [header, f"// [CYPRESS] '{a}' non supporte (Cypress ne gere pas nativement les onglets)"]
    if a == "extract":
        return [header, f"// [CYPRESS] '{a}' non rejouable (lecture LLM only)"]
    return [header, f"// action inconnue : {a}"]


# ============================================================
# SELENIUM exporter (Python)
# ============================================================

def export_selenium(clean_steps: CleanSteps) -> str:
    """Genere le code Selenium WebDriver (Python) deterministe."""
    lines = [
        "# Genere deterministiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.",
        f"# Parcours : {clean_steps.parcours or 'sans nom'}",
        "",
        "import os",
        "from selenium import webdriver",
        "from selenium.webdriver.common.by import By",
        "from selenium.webdriver.common.keys import Keys",
        "from selenium.webdriver.common.action_chains import ActionChains",
        "from selenium.webdriver.support.ui import WebDriverWait, Select",
        "from selenium.webdriver.support import expected_conditions as EC",
        "import time",
        "",
        "driver = webdriver.Chrome()",
        "wait = WebDriverWait(driver, 10)",
    ]
    replayables = _replayable_steps(clean_steps)
    first_is_nav = (replayables and replayables[0].action == "navigate"
                    and (replayables[0].url or replayables[0].value) == clean_steps.scenario_url)
    if clean_steps.scenario_url and not first_is_nav:
        lines.append(f'driver.get("{clean_steps.scenario_url}")')
    lines.append("")
    for i, step in enumerate(_replayable_steps(clean_steps), start=1):
        lines.extend(_selenium_step(step, i))
        lines.append("")
    lines.append("driver.quit()")
    return "\n".join(lines) + "\n"


def _sel_by(val: str, is_xpath: bool) -> str:
    by = "By.XPATH" if is_xpath else "By.CSS_SELECTOR"
    escaped = val.replace('"', '\\"')
    return f'({by}, "{escaped}")'


def _selenium_step(step: Step, index: int) -> list[str]:
    sid = step.id or f"step-{index:04d}"
    header = f"# [{sid}] {step.action.upper()} - {step.description or ''}"
    val = _sel_value(step)
    is_xp = _sel_is_xpath(step)
    a = step.action

    def _get_el():
        return f'wait.until(EC.presence_of_element_located({_sel_by(val, is_xp)}))'

    if a == "navigate":
        return [header, f'driver.get("{step.url or step.value or ""}")']
    if a == "click":
        if not val: return [header, f"# non-executable : click sans selecteur ({sid})"]
        return [header, f'wait.until(EC.element_to_be_clickable({_sel_by(val, is_xp)})).click()']
    if a == "input":
        if not val: return [header, f"# non-executable : input sans selecteur ({sid})"]
        if step.sensitive and step.env_var:
            return [header, f'{_get_el()}.send_keys(os.environ["{step.env_var}"])']
        v = (step.value or "").replace('"', '\\"')
        return [header, f'{_get_el()}.send_keys("{v}")']
    if a == "select":
        if not val: return [header, f"# non-executable : select sans selecteur ({sid})"]
        return [header, f'Select({_get_el()}).select_by_visible_text("{step.value or ""}")']
    if a == "verify":
        vt = (step.verify_type or "presence").lower()
        expected = step.expected or step.value or ""
        if vt in ("texte_contient", "text_contains"):
            return [header, f'assert "{expected}" in driver.page_source']
        if vt == "visible" and val:
            return [header, f'assert wait.until(EC.visibility_of_element_located({_sel_by(val, is_xp)}))']
        if vt == "absent" and val:
            return [header, f'assert len(driver.find_elements{_sel_by(val, is_xp)}) == 0']
        if val:
            return [header, f'assert {_get_el()} is not None']
        return [header, f'assert "{expected}" in driver.page_source']
    if a == "scroll":
        if val:
            return [header, f'driver.execute_script("arguments[0].scrollIntoView({{block:\\"center\\"}});", {_get_el()})']
        return [header, f'driver.execute_script("window.scrollBy(0, {step.deltaY or 650});")']
    if a == "hover":
        if not val: return [header, f"# non-executable : hover sans selecteur ({sid})"]
        return [header, f'ActionChains(driver).move_to_element({_get_el()}).perform()']
    if a == "wait":
        if val:
            return [header, f'wait.until(EC.visibility_of_element_located({_sel_by(val, is_xp)}))']
        return [header, f'time.sleep({step.seconds or 2})']
    if a == "keyboard":
        key = step.value or "Enter"
        return [header, f'ActionChains(driver).send_keys(Keys.{key.upper()}).perform()']
    if a == "upload":
        if not val: return [header, f"# non-executable : upload sans selecteur ({sid})"]
        return [header, f'{_get_el()}.send_keys("{step.value or step.target or ""}")']
    if a == "go_back":
        return [header, "driver.back()"]
    if a == "go_forward":
        return [header, "driver.forward()"]
    if a == "reload":
        return [header, "driver.refresh()"]
    if a == "screenshot":
        return [header, f'driver.save_screenshot("{step.target or f"capture_{index}"}.png")']
    if a == "cookie":
        if val:
            return [header,
                    "try:",
                    f'    wait.until(EC.element_to_be_clickable({_sel_by(val, is_xp)})).click()',
                    "except Exception:",
                    "    pass  # cookie banner absent"]
        return [header, "# cookie sans selecteur - skip"]
    if a in ("open_tab", "switch_tab", "close_tab"):
        # Selenium supporte les tabs via driver.switch_to.window mais c'est
        # complexe cross-browser. On documente et on skip.
        return [header, f'# [SELENIUM] "{a}" support minimal - a implementer selon setup driver']
    if a == "extract":
        return [header, f'# [SELENIUM] "{a}" non rejouable (lecture LLM only)']
    return [header, f"# action inconnue : {a}"]


# ============================================================
# Validation cohérence export vs clean_steps
# ============================================================

def validate_export_counts(clean_steps: CleanSteps, export_output: str,
                           format_name: str) -> list[str]:
    """Verifie que l'export contient EXACTEMENT autant d'actions par type
    que le clean_steps included_in_replay, dans le MEME ORDRE. Retourne
    la liste des anomalies detectees (vide si tout OK).

    Regle R5 : "echec explicite si un export ajoute, retire ou reordonne
    une action sans justification prevue par le format". Verifications :
      1. Chaque step included_in_replay a son marqueur [step-XXXX]
      2. Nombre de headers step == nombre de rejouables
      3. ORDRE des [step-XXXX] dans l'export == ordre dans clean_steps
      4. Aucun [step-XXXX] fantome dans l'export (pas dans clean_steps)
      5. Aucun step FILTRE (included_in_replay=False) ne doit apparaitre
    """
    import re
    anomalies: list[str] = []
    replayable = _replayable_steps(clean_steps)
    filtered = [s for s in clean_steps.steps if not s.included_in_replay]

    # 1. Chaque step rejouable est present
    for s in replayable:
        sid = s.id or ""
        if sid and sid not in export_output:
            anomalies.append(
                f"[{format_name}] step {sid} absent de l'export "
                f"(attendu 1 statement pour cette action {s.action})"
            )

    # 2. Nombre de headers step
    exported_ids = re.findall(r'\[step-\d{4,}\]', export_output)
    exported_ids_clean = [x.strip('[]') for x in exported_ids]
    if len(exported_ids) != len(replayable):
        anomalies.append(
            f"[{format_name}] nombre de steps exportes ({len(exported_ids)}) != "
            f"nombre de steps rejouables ({len(replayable)})"
        )

    # 3. Ordre preserve
    expected_order = [s.id for s in replayable if s.id]
    if exported_ids_clean != expected_order:
        # Trouve la premiere divergence pour un message utile
        first_diff = None
        for i, (exp, got) in enumerate(zip(expected_order, exported_ids_clean)):
            if exp != got:
                first_diff = (i, exp, got)
                break
        if first_diff:
            i, exp, got = first_diff
            anomalies.append(
                f"[{format_name}] ORDRE des steps casse a la position {i} : "
                f"attendu '{exp}', obtenu '{got}'. Un export ne doit jamais "
                f"reordonner les actions."
            )
        else:
            anomalies.append(
                f"[{format_name}] ordre des steps different : "
                f"attendu {expected_order[:5]}..., obtenu {exported_ids_clean[:5]}..."
            )

    # 4. Aucun fantome
    expected_set = set(expected_order)
    ghosts = [sid for sid in exported_ids_clean if sid not in expected_set]
    if ghosts:
        anomalies.append(
            f"[{format_name}] {len(ghosts)} step(s) fantome(s) dans l'export "
            f"(pas dans clean_steps) : {ghosts[:5]}"
        )

    # 5. Aucun step filtre ne doit apparaitre comme statement executable
    #    (ils peuvent apparaitre en commentaire // SKIPPED, mais pas en
    #    header actif [step-XXXX])
    filtered_ids = {s.id for s in filtered if s.id}
    exported_set = set(exported_ids_clean)
    leaked_filtered = filtered_ids & exported_set
    if leaked_filtered:
        anomalies.append(
            f"[{format_name}] {len(leaked_filtered)} step(s) filtre(s) presents "
            f"dans l'export comme actifs : {list(leaked_filtered)[:5]}"
        )

    return anomalies


def validate_export_by_action_type(clean_steps: CleanSteps, export_output: str,
                                   format_name: str,
                                   type_markers: dict[str, list[str]] | None = None) -> list[str]:
    """Verifie la SEMANTIQUE : pour chaque action_type, le nombre de
    marqueurs specifiques dans l'export matche le count attendu depuis
    clean_steps. Ex Katalon : action=input rejouable N fois -> N appels
    a WebUI.setText, N action=keyboard Enter -> N Keys.ENTER.

    type_markers : dict {action_name: [marker_strings_a_chercher]}. Si None,
    utilise le mapping par defaut pour chaque format connu."""
    if type_markers is None:
        type_markers = _DEFAULT_TYPE_MARKERS.get(format_name, {})
    if not type_markers:
        return []
    anomalies: list[str] = []
    replayable = _replayable_steps(clean_steps)
    expected_counts = Counter(s.action for s in replayable)
    for action_name, markers in type_markers.items():
        expected = expected_counts.get(action_name, 0)
        actual = sum(export_output.count(m) for m in markers)
        if actual != expected:
            anomalies.append(
                f"[{format_name}] action '{action_name}' : "
                f"clean_steps attend {expected} occurrence(s) mais l'export "
                f"contient {actual} marqueur(s) parmi {markers}. Divergence "
                f"semantique - l'export a possiblement ajoute/retire des "
                f"actions par rapport au JSON canonique."
            )
    return anomalies


_DEFAULT_TYPE_MARKERS: dict[str, dict[str, list[str]]] = {
    "katalon": {
        "input": ["WebUI.setText"],
        "click": ["WebUI.click("],
        "select": ["WebUI.selectOptionByLabel"],
        "navigate": ["WebUI.navigateToUrl"],
        "keyboard": ["WebUI.sendKeys"],
        "hover": ["WebUI.mouseOver"],
        "upload": ["WebUI.uploadFile"],
        "reload": ["WebUI.refresh()"],
        "go_back": ["WebUI.back()"],
    },
    "cypress": {
        "input": [".clear().type("],
        "click": [".click();"],
        "select": [".select("],
        "navigate": ["cy.visit("],
        "keyboard": ["cy.get('body').type("],
        "hover": [".trigger('mouseover'"],
        "reload": ["cy.reload()"],
        "go_back": ["cy.go('back')"],
    },
    "selenium": {
        "input": ["send_keys(\""],
        "click": [".click()"],
        "select": ["select_by_visible_text"],
        "navigate": ["driver.get("],
        "keyboard": ["ActionChains(driver).send_keys("],
        "reload": ["driver.refresh()"],
        "go_back": ["driver.back()"],
    },
}


EXPORTERS: dict[str, Callable[[CleanSteps], str]] = {
    "katalon": export_katalon,
    "cypress": export_cypress,
    "selenium": export_selenium,
}
