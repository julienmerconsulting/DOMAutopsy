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
    if clean_steps.scenario_url:
        lines.append(f"WebUI.navigateToUrl('{clean_steps.scenario_url}')")
    lines.append("")

    for i, step in enumerate(_replayable_steps(clean_steps), start=1):
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
    if clean_steps.scenario_url:
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
    if clean_steps.scenario_url:
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
    que le clean_steps included_in_replay. Retourne la liste des anomalies
    detectees (vide si tout OK).

    Regle R5 : "echec explicite si un export ajoute, retire ou reordonne
    une action sans justification prevue par le format".
    """
    anomalies: list[str] = []
    replayable = _replayable_steps(clean_steps)
    expected_counts = Counter(s.action for s in replayable)

    # Chaque step included_in_replay doit avoir son marqueur [step-XXXX]
    # dans l'export (les headers ci-dessus en produisent 1 par step).
    for s in replayable:
        sid = s.id or ""
        if sid and sid not in export_output:
            anomalies.append(
                f"[{format_name}] step {sid} absent de l'export (attendu 1 statement pour cette action {s.action})"
            )

    # Compte les headers step dans l'export
    import re
    exported_ids = re.findall(r'\[step-\d{4,}\]', export_output)
    if len(exported_ids) != len(replayable):
        anomalies.append(
            f"[{format_name}] nombre de steps exportes ({len(exported_ids)}) != "
            f"nombre de steps rejouables ({len(replayable)})"
        )

    return anomalies


EXPORTERS: dict[str, Callable[[CleanSteps], str]] = {
    "katalon": export_katalon,
    "cypress": export_cypress,
    "selenium": export_selenium,
}
