"""
Script parser : extrait URL + actions + selecteurs depuis un test existant.

Supporte Katalon Groovy, Playwright TS, Cypress JS, Selenium Python.
Approche regex (pas d'AST) : couvre 95% des tests lineaires de production.

Usage :
    from script_parser import parse_script, to_nl_task
    parsed = parse_script("test_login.groovy", source_code)
    task = to_nl_task(parsed)

Retour de parse_script :
{
    "format": "katalon" | "playwright" | "cypress" | "selenium",
    "url": "https://..." or None,
    "selectors": [{"var": "to1", "type": "css|xpath", "value": "..."}],
    "actions": [{"action": "click|input", "var": "to1", "value": None|str, "sensitive": bool}],
    "redacted": int,
}
"""

import re
from pathlib import Path
from typing import Optional


# Patterns "champ sensible" : nom de variable / sélecteur évoque un secret
SENSITIVE_PATTERN = re.compile(
    r"(?:^|[\W_])(password|passwd|pwd|secret|token|otp|cvv|cvc|ccv|cc[\W_]?num|card[\W_]?num|ssn|sin|pin|api[\W_]?key)(?:[\W_]|$)",
    re.IGNORECASE,
)


def detect_format(filename: str) -> str:
    """Detecte le format depuis le nom de fichier (extension)"""
    name = filename.lower()
    if name.endswith(".groovy"):
        return "katalon"
    if name.endswith(".spec.ts") or name.endswith(".spec.js") or name.endswith(".pw.ts"):
        return "playwright"
    if name.endswith(".cy.js") or name.endswith(".cy.ts"):
        return "cypress"
    if name.endswith(".py"):
        return "selenium"
    if name.endswith(".ts") or name.endswith(".js"):
        # Heuristique sur le contenu (sera levee plus tard si necessaire)
        return "playwright"
    return "unknown"


def _is_sensitive(text: str) -> bool:
    """True si le texte (nom de variable / selecteur) evoque un secret"""
    return bool(SENSITIVE_PATTERN.search(text or ""))


# ============================================================
# KATALON GROOVY
# ============================================================
# Pattern typique :
#   WebUI.openBrowser('')
#   WebUI.navigateToUrl('https://...')
#   TestObject to1 = new TestObject('login')
#   to1.addProperty('css', ConditionType.EQUALS, '#username')
#   WebUI.setText(to1, 'tomsmith')
#   WebUI.click(toBtn)
def parse_katalon(src: str) -> dict:
    url_m = re.search(r"WebUI\.navigateToUrl\(['\"]([^'\"]+)['\"]\)", src)
    selectors = []
    for m in re.finditer(
        r"(\w+)\.addProperty\(['\"](css|xpath)['\"]\s*,\s*ConditionType\.\w+\s*,\s*['\"]([^'\"]+)['\"]\)",
        src,
    ):
        selectors.append({"var": m.group(1), "type": m.group(2), "value": m.group(3)})

    sel_map = {s["var"]: s for s in selectors}
    actions = []
    redacted = 0
    for m in re.finditer(
        r"WebUI\.(click|setText)\(\s*(\w+)\s*(?:,\s*['\"]([^'\"]*)['\"])?\s*\)", src
    ):
        kind, var, value = m.group(1), m.group(2), m.group(3)
        sel = sel_map.get(var)
        sel_str = (sel["value"] if sel else var)
        sensitive = _is_sensitive(sel_str) or _is_sensitive(var)
        if kind == "click":
            actions.append({"action": "click", "var": var, "value": None, "sensitive": False})
        else:  # setText
            if sensitive and value:
                redacted += 1
                value = "<REDACTED>"
            actions.append({"action": "input", "var": var, "value": value, "sensitive": sensitive})

    return {
        "format": "katalon",
        "url": url_m.group(1) if url_m else None,
        "selectors": selectors,
        "actions": actions,
        "redacted": redacted,
    }


# ============================================================
# PLAYWRIGHT TS / JS
# ============================================================
# Pattern typique :
#   await page.goto('https://...');
#   await page.locator('#username').fill('tomsmith');
#   await page.locator('button[type=submit]').click();
def parse_playwright(src: str) -> dict:
    url_m = re.search(r"\.goto\(\s*['\"]([^'\"]+)['\"]", src)
    actions = []
    selectors = []
    redacted = 0

    # locator(...).click() / locator(...).fill('x')
    for m in re.finditer(
        r"\.locator\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*(click|fill|press|type)\s*\(\s*(?:['\"]([^'\"]*)['\"])?",
        src,
    ):
        sel, kind, value = m.group(1), m.group(2), m.group(3)
        sel_type = "xpath" if sel.startswith("//") or sel.startswith("xpath=") else "css"
        var_id = f"sel_{len(selectors) + 1}"
        selectors.append({"var": var_id, "type": sel_type, "value": sel})
        sensitive = _is_sensitive(sel)
        if kind == "click" or kind == "press":
            actions.append({"action": "click", "var": var_id, "value": None, "sensitive": False})
        else:  # fill / type
            v = value
            if sensitive and v:
                redacted += 1
                v = "<REDACTED>"
            actions.append({"action": "input", "var": var_id, "value": v, "sensitive": sensitive})

    return {
        "format": "playwright",
        "url": url_m.group(1) if url_m else None,
        "selectors": selectors,
        "actions": actions,
        "redacted": redacted,
    }


# ============================================================
# CYPRESS JS
# ============================================================
# Pattern typique :
#   cy.visit('https://...')
#   cy.get('#username').type('tomsmith')
#   cy.get('button.login').click()
def parse_cypress(src: str) -> dict:
    url_m = re.search(r"cy\.visit\(\s*['\"]([^'\"]+)['\"]", src)
    actions = []
    selectors = []
    redacted = 0

    for m in re.finditer(
        r"cy\.(get|xpath|contains)\(\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*(click|type|check|select)\s*\(\s*(?:['\"]([^'\"]*)['\"])?",
        src,
    ):
        get_kind, sel, kind, value = m.group(1), m.group(2), m.group(3), m.group(4)
        sel_type = "xpath" if get_kind == "xpath" or sel.startswith("//") else ("text" if get_kind == "contains" else "css")
        var_id = f"sel_{len(selectors) + 1}"
        selectors.append({"var": var_id, "type": sel_type, "value": sel})
        sensitive = _is_sensitive(sel)
        if kind == "click":
            actions.append({"action": "click", "var": var_id, "value": None, "sensitive": False})
        else:  # type / select / check
            v = value
            if sensitive and v:
                redacted += 1
                v = "<REDACTED>"
            actions.append({"action": "input", "var": var_id, "value": v, "sensitive": sensitive})

    return {
        "format": "cypress",
        "url": url_m.group(1) if url_m else None,
        "selectors": selectors,
        "actions": actions,
        "redacted": redacted,
    }


# ============================================================
# SELENIUM PYTHON
# ============================================================
# Pattern typique :
#   driver.get('https://...')
#   driver.find_element(By.CSS_SELECTOR, '#username').send_keys('tomsmith')
#   driver.find_element(By.ID, 'login-btn').click()
def parse_selenium(src: str) -> dict:
    url_m = re.search(r"driver\.get\(\s*['\"]([^'\"]+)['\"]", src)
    actions = []
    selectors = []
    redacted = 0

    # find_element(By.X, 'sel').action(value)
    BY_MAP = {
        "ID": "css",  # on convertit #id en CSS
        "CSS_SELECTOR": "css",
        "XPATH": "xpath",
        "NAME": "css",
        "CLASS_NAME": "css",
        "TAG_NAME": "css",
        "LINK_TEXT": "text",
        "PARTIAL_LINK_TEXT": "text",
    }
    for m in re.finditer(
        r"find_element\(\s*By\.(\w+)\s*,\s*['\"]([^'\"]+)['\"]\s*\)\s*\.\s*(click|send_keys|clear)\(\s*(?:['\"]([^'\"]*)['\"])?",
        src,
    ):
        by, raw_sel, kind, value = m.group(1), m.group(2), m.group(3), m.group(4)
        sel_type = BY_MAP.get(by, "css")
        # Normalise selecteur en CSS quand possible
        if by == "ID":
            sel = f"#{raw_sel}"
        elif by == "NAME":
            sel = f"[name='{raw_sel}']"
        elif by == "CLASS_NAME":
            sel = f".{raw_sel}"
        else:
            sel = raw_sel
        var_id = f"sel_{len(selectors) + 1}"
        selectors.append({"var": var_id, "type": sel_type, "value": sel})
        sensitive = _is_sensitive(sel) or _is_sensitive(raw_sel)
        if kind == "click":
            actions.append({"action": "click", "var": var_id, "value": None, "sensitive": False})
        elif kind == "send_keys":
            v = value
            if sensitive and v:
                redacted += 1
                v = "<REDACTED>"
            actions.append({"action": "input", "var": var_id, "value": v, "sensitive": sensitive})
        # clear : on ignore (pas une action pour browser-use)

    return {
        "format": "selenium",
        "url": url_m.group(1) if url_m else None,
        "selectors": selectors,
        "actions": actions,
        "redacted": redacted,
    }


# ============================================================
# DISPATCH
# ============================================================
PARSERS = {
    "katalon": parse_katalon,
    "playwright": parse_playwright,
    "cypress": parse_cypress,
    "selenium": parse_selenium,
}


def parse_script(filename: str, source: str, format_hint: Optional[str] = None) -> dict:
    """Parse un script de test, retourne un dict structure"""
    fmt = format_hint or detect_format(filename)
    parser = PARSERS.get(fmt)
    if not parser:
        return {
            "format": "unknown",
            "url": None,
            "selectors": [],
            "actions": [],
            "redacted": 0,
            "error": f"Format non supporte : {filename}",
        }
    parsed = parser(source)
    parsed["filename"] = filename
    return parsed


# ============================================================
# CONVERSION VERS TASK NL POUR BROWSER-USE
# ============================================================
def to_nl_task(parsed: dict) -> str:
    """Convertit un parse en description en langage naturel pour browser-use"""
    if parsed.get("error"):
        return ""
    sel_map = {s["var"]: s for s in parsed.get("selectors", [])}
    lines = []
    for a in parsed.get("actions", []):
        sel = sel_map.get(a["var"])
        if sel:
            sel_desc = f"l'element ayant le selecteur {sel['type']} '{sel['value']}'"
        else:
            sel_desc = a["var"]
        if a["action"] == "click":
            lines.append(f"Clique sur {sel_desc}")
        elif a["action"] == "input":
            v = a.get("value")
            if v == "<REDACTED>":
                lines.append(f"Tape la valeur attendue (champ sensible, sera fournie au runtime) dans {sel_desc}")
            elif v:
                lines.append(f"Tape '{v}' dans {sel_desc}")
            else:
                lines.append(f"Saisis une valeur dans {sel_desc}")
    return ". ".join(lines) + "." if lines else ""
