"""
DOMAutopsy - Generateur canonique test_playwright.spec.ts
==========================================================
Traduit un clean_steps.json valide (schema v2.0) en un test Playwright TS
lancable via `npx playwright test`.

Principes :
- Generation DETERMINISTE (pas d'IA) : reproductible, pas de token, pas de flake
- Chaque etape est encapsulee dans test.step('[step-XXXX] <ACTION> - <desc>', ...)
  pour permettre au serveur/rapport de rapprocher les resultats Playwright avec
  les steps JSON
- Les steps included_in_replay: false sont SAUTES (mais conserves dans le JSON
  pour la tracabilite dans le rapport)
- Une valeur sensitive: true est REMPLACEE par process.env.<VAR> (jamais en clair)
- Une action inconnue leve NON_TRANSLATABLE_ACTIONS : le caller doit signaler
  clairement (cahier des charges : "fais echouer clairement la generation
  ou le replay selon le cas")

Sortie : un fichier .spec.ts + la liste des variables d'env attendues + la
liste des actions non traduisibles rencontrees.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

from schemas import CleanSteps, Step


# ============================================================
# Utilitaires d'ecriture TS
# ============================================================

def _ts_string(s: str | None) -> str:
    """Serialise une string Python en literal TypeScript entre double quotes."""
    if s is None:
        return '""'
    escaped = (
        s.replace("\\", "\\\\")
         .replace('"', '\\"')
         .replace("\n", "\\n")
         .replace("\r", "\\r")
         .replace("\t", "\\t")
    )
    return f'"{escaped}"'


def _step_id(step: Step, fallback_index: int) -> str:
    """Retourne l'identifiant stable formate step-XXXX pour l'entete test.step()."""
    if step.id:
        return step.id
    n = step.step or fallback_index
    return f"step-{n:04d}"


def _env_var_name(step: Step, fallback_index: int) -> str:
    """Convention de nommage des vars d'env pour les valeurs sensibles."""
    if step.env_var:
        # Sanitise : uppercase, alphanumeric + underscore uniquement
        v = re.sub(r"[^A-Za-z0-9_]", "_", step.env_var).upper()
        return v or f"DOMAUTOPSY_STEP_{fallback_index:04d}"
    return f"DOMAUTOPSY_STEP_{fallback_index:04d}"


# ============================================================
# Construction du locator TS pour un step
# ============================================================

def _selector_value_and_type(step: Step) -> tuple[str | None, str | None]:
    """Extrait (value, type) depuis step.selector qui peut etre soit un
    Selector Pydantic, soit une string brute (retro-compat)."""
    sel = step.selector
    if sel is None:
        return None, step.selectorType
    if isinstance(sel, str):
        return sel, step.selectorType
    # Selector object
    val = getattr(sel, "value", None) or getattr(sel, "playwrightSelector", None)
    if val is None:
        return None, step.selectorType
    # Type deduit : shadow > xpath explicite > css
    inferred = step.selectorType
    if inferred is None:
        if getattr(sel, "inShadowDOM", False):
            inferred = "css"  # shadow chain est deja en syntaxe Playwright ">>>"
        elif isinstance(val, str) and val.startswith("//"):
            inferred = "xpath"
        else:
            inferred = "css"
    return val, inferred


def _locator_expr(step: Step) -> str:
    """Construit l'expression Playwright page.locator(...) pour un step.
    Retourne '' si aucun selecteur exploitable (le caller doit gerer).

    Shadow DOM : le DOM listener produit des chaines "host >>> inner" pour
    les elements dans un shadow root. Playwright locators traversent
    automatiquement les shadow roots OUVERTS quand on chaine .locator(),
    donc on split sur " >>> " et on chaine :
        page.locator("host").locator("inner")
    C'est la syntaxe supportee officiellement (doc Playwright "Locate in
    Shadow DOM"), pas de plugin, pas de piercing syntax exotique.
    """
    value, sel_type = _selector_value_and_type(step)
    if not value:
        return ""
    # Shadow DOM chain : splitter et chainer .locator() proprement
    if " >>> " in value and sel_type != "xpath" and not value.startswith("//"):
        parts = [p.strip() for p in value.split(" >>> ") if p.strip()]
        if len(parts) > 1:
            head = f"page.locator({_ts_string(parts[0])})"
            for p in parts[1:]:
                head += f".locator({_ts_string(p)})"
            return head
    if sel_type == "xpath" or value.startswith("//"):
        arg = value if value.startswith("xpath=") else f"xpath={value}"
        return f"page.locator({_ts_string(arg)})"
    if sel_type == "text":
        return f"page.getByText({_ts_string(value)})"
    if sel_type == "role":
        return f"page.getByRole({_ts_string(value)})"
    return f"page.locator({_ts_string(value)})"


# ============================================================
# Emitters par action
# ============================================================

def _emit_navigate(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    url = step.url or step.target or step.value
    if not url:
        raise UnsupportedAction(step, "navigate sans URL")
    return [f"    await page.goto({_ts_string(url)});"]


def _emit_click(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    loc = _locator_expr(step)
    if not loc:
        raise UnsupportedAction(step, "click sans selecteur")
    # PAS de .first() : Playwright en mode strict crashe sur selecteur
    # ambigu, ce qui est le comportement voulu (faire echouer plutot que
    # cliquer silencieusement sur le mauvais element). Le .first() ne
    # serait justifie que pour un fallback intentionnel comme cookie().
    return [f"    await {loc}.click();"]


def _emit_input(step: Step, sensitive_vars: dict[str, str], index: int) -> list[str]:
    loc = _locator_expr(step)
    if not loc:
        raise UnsupportedAction(step, "input sans selecteur")
    if step.sensitive:
        env_name = _env_var_name(step, index)
        sensitive_vars[env_name] = step.description or step.target or "sensitive input"
        # process.env.X peut etre undefined -> fallback vide + assertion runtime
        # explicite si non defini pour aider a diagnostiquer
        return [
            f'    const _val_{index} = process.env.{env_name};',
            f'    if (_val_{index} === undefined) throw new Error("Env var {env_name} manquante (valeur sensible du step {index})");',
            f"    await {loc}.fill(_val_{index});",
        ]
    val = step.value if step.value is not None else ""
    return [f"    await {loc}.fill({_ts_string(val)});"]


def _emit_select(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    loc = _locator_expr(step)
    if not loc:
        raise UnsupportedAction(step, "select sans selecteur")
    val = step.value or ""
    return [f"    await {loc}.selectOption({_ts_string(val)});"]


def _emit_verify(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    vtype = (step.verify_type or "presence").lower()
    expected = step.expected or step.target or step.value or ""
    loc = _locator_expr(step)
    if vtype in ("texte_contient", "text_contains"):
        return [f"    await expect(page.getByText({_ts_string(expected)}).first()).toBeVisible();"]
    if vtype in ("texte_exact", "text_exact"):
        return [f'    await expect(page.getByText({_ts_string(expected)}, {{ exact: true }}).first()).toBeVisible();']
    if vtype in ("visible",):
        if not loc:
            raise UnsupportedAction(step, "verify visible sans selecteur")
        return [f"    await expect({loc}).toBeVisible();"]
    if vtype in ("absent",):
        if loc:
            return [f"    await expect({loc}).toHaveCount(0);"]
        return [f"    await expect(page.getByText({_ts_string(expected)})).toHaveCount(0);"]
    # presence par defaut
    if loc:
        return [f"    await expect({loc}).toBeAttached();"]
    return [f"    await expect(page.getByText({_ts_string(expected)}).first()).toBeVisible();"]


def _emit_scroll(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    # Scroll vers un element -> scrollIntoViewIfNeeded
    loc = _locator_expr(step)
    direction = (step.direction or "").lower()
    if direction == "vers_element" and loc:
        return [f"    await {loc}.scrollIntoViewIfNeeded();"]
    # Scroll par delta (roue souris)
    delta = step.deltaY
    if delta is None:
        # Deduire depuis direction
        delta = 650 if direction in ("", "bas", "down") else -650
    # Playwright : mouse.wheel(deltaX, deltaY)
    return [f"    await page.mouse.wheel(0, {int(delta)});"]


def _emit_hover(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    loc = _locator_expr(step)
    if not loc:
        raise UnsupportedAction(step, "hover sans selecteur")
    return [f"    await {loc}.hover();"]


def _emit_wait(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    # Attente d'element ou attente temporelle
    loc = _locator_expr(step)
    if loc:
        state = step.wait_state or "visible"
        return [f'    await {loc}.waitFor({{ state: {_ts_string(state)} }});']
    seconds = step.seconds if step.seconds is not None else 2.0
    ms = int(seconds * 1000)
    return [f"    await page.waitForTimeout({ms});"]


def _emit_screenshot(step: Step, sensitive_vars: dict[str, str], index: int) -> list[str]:
    name = step.target or step.description or f"capture-{index}"
    # Nettoie pour un nom de fichier valide
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name)[:60] or f"capture-{index}"
    path = f"screenshot-step-{index:04d}-{safe}.png"
    return [f'    await page.screenshot({{ path: `${{testInfo.outputDir}}/{path}`, fullPage: false }});']


def _emit_cookie(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    # Cookie banner : clic conditionnel avec timeout court, ne pas faire echouer
    # si absent
    loc = _locator_expr(step)
    if loc:
        return [
            f"    try {{",
            f"      await {loc}.first().click({{ timeout: 3000 }});",
            f"    }} catch (_e) {{ /* cookie banner absente */ }}",
        ]
    # Sans selecteur : tentative sur les patterns communs
    return [
        f"    try {{",
        f'      await page.getByRole("button", {{ name: /accept|accepter|autoriser|ok/i }}).first().click({{ timeout: 3000 }});',
        f"    }} catch (_e) {{ /* cookie banner absente */ }}",
    ]


def _emit_keyboard(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    key = step.value or step.target or step.expected
    if not key:
        raise UnsupportedAction(step, "keyboard sans key")
    return [f"    await page.keyboard.press({_ts_string(key)});"]


def _emit_upload(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    loc = _locator_expr(step)
    if not loc:
        raise UnsupportedAction(step, "upload sans selecteur")
    path = step.value or step.target
    if not path:
        raise UnsupportedAction(step, "upload sans chemin de fichier")
    return [f"    await {loc}.setInputFiles({_ts_string(path)});"]


def _emit_go_back(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    return ["    await page.goBack();"]


def _emit_go_forward(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    return ["    await page.goForward();"]


def _emit_reload(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    return ["    await page.reload();"]


def _emit_open_tab(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    url = step.url or step.value
    if url:
        return [
            "    const _newPage = await page.context().newPage();",
            f"    await _newPage.goto({_ts_string(url)});",
            "    page = _newPage;  // switch focus vers le nouvel onglet",
        ]
    return [
        "    const _newPage = await page.context().newPage();",
        "    page = _newPage;",
    ]


def _emit_switch_tab(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    """Bascule sur l'onglet a l'index donne (deterministe par ordre de
    creation, pas par tab_id opaque BU). Playwright expose context.pages()
    dans l'ordre de creation - c'est notre reference stable."""
    idx_str = (step.value or "0").strip()
    try:
        idx = int(idx_str)
    except ValueError:
        raise UnsupportedAction(step, f"switch_tab index invalide '{idx_str}'")
    return [
        f"    const _pages = page.context().pages();",
        f'    if (_pages.length <= {idx}) throw new Error("switch_tab: pas d\'onglet a l\'index {idx} (pages ouvertes: " + _pages.length + ")");',
        f"    page = _pages[{idx}];",
        f"    await page.bringToFront();",
    ]


def _emit_close_tab(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    """Ferme l'onglet par index (ordre de creation) OU l'onglet courant
    si aucun index fourni. Apres close, on repointe sur pages()[0]."""
    if step.value is None or step.value == "":
        return [
            "    await page.close();",
            "    const _pages = page.context().pages();",
            '    if (_pages.length === 0) throw new Error("close_tab: aucun onglet restant apres fermeture");',
            "    page = _pages[0];",
            "    await page.bringToFront();",
        ]
    try:
        idx = int(step.value)
    except ValueError:
        raise UnsupportedAction(step, f"close_tab index invalide '{step.value}'")
    return [
        f"    const _pages = page.context().pages();",
        f'    if (_pages.length <= {idx}) throw new Error("close_tab: pas d\'onglet a l\'index {idx}");',
        f"    await _pages[{idx}].close();",
        f"    const _remaining = page.context().pages();",
        f'    if (_remaining.length === 0) throw new Error("close_tab: aucun onglet restant");',
        f"    page = _remaining[0];",
        f"    await page.bringToFront();",
    ]


def _emit_extract(step: Step, sensitive_vars: dict[str, str]) -> list[str]:
    """extract est une lecture LLM (extraire un texte/donnee pour raisonner),
    aucune interaction reproductible. Normalement, extract steps sont
    marques included_in_replay=False au build_pre_cleanup_steps donc jamais
    presentes ici. Cet emitter est un GARDE-FOU : si quelqu'un force
    included_in_replay=True sur un extract, on genere une exception TS
    explicite plutot qu'un no-op silencieux qui ferait passer le test."""
    goal = (step.description or "extract LLM").replace('"', '\\"')
    return [
        f'    throw new Error("Action extract non rejouable : {goal}. '
        f'Marquer included_in_replay=false dans le JSON pour la skipper propre.");',
    ]


# ============================================================
# Dispatch + generation complete
# ============================================================

class UnsupportedAction(Exception):
    """Levee quand un step ne peut pas etre traduit deterministiquement en TS.
    Le caller doit reporter cet echec dans le JSON (status=fail) et remonter
    dans le rapport, comme demande dans le cahier des charges.
    """
    def __init__(self, step: Step, reason: str):
        self.step = step
        self.reason = reason
        super().__init__(f"Step {step.id or step.step} ({step.action}) non traduisible : {reason}")


EMITTERS = {
    "navigate": _emit_navigate,
    "click": _emit_click,
    "select": _emit_select,
    "verify": _emit_verify,
    "scroll": _emit_scroll,
    "hover": _emit_hover,
    "wait": _emit_wait,
    "cookie": _emit_cookie,
    "keyboard": _emit_keyboard,
    "key_press": _emit_keyboard,
    "upload": _emit_upload,
    "file_upload": _emit_upload,
    "go_back": _emit_go_back,
    "go_forward": _emit_go_forward,
    "reload": _emit_reload,
    "open_tab": _emit_open_tab,
    "switch_tab": _emit_switch_tab,
    "close_tab": _emit_close_tab,
    "extract": _emit_extract,   # garde-fou : leve si included_in_replay=True
    # input et screenshot ont une signature enrichie (index)
}


def _emit_step_body(step: Step, sensitive_vars: dict[str, str], index: int) -> list[str]:
    """Dispatch vers l'emitter selon step.action."""
    action = step.action
    if action == "input":
        return _emit_input(step, sensitive_vars, index)
    if action == "screenshot":
        return _emit_screenshot(step, sensitive_vars, index)
    emitter = EMITTERS.get(action)
    if emitter is None:
        raise UnsupportedAction(step, f"action inconnue '{action}' — payload conserve dans le JSON mais non rejouable")
    return emitter(step, sensitive_vars)


def generate_playwright_ts(
    clean_steps: CleanSteps,
    output_path: Path,
    parcours_url: str | None = None,
) -> dict[str, Any]:
    """Genere test_playwright.spec.ts a partir d'un CleanSteps valide.

    Args:
        clean_steps: parcours valide (schema v2.0)
        output_path: chemin absolu du fichier .spec.ts a ecrire
        parcours_url: URL de depart si absente des steps (fallback goto initial)

    Returns:
        dict avec:
          - path: chemin du fichier ecrit
          - sensitive_vars: {ENV_VAR_NAME: description}
          - unsupported: list[dict] des steps qu'on n'a pas pu traduire
          - included_count: nombre de steps traduits
          - skipped_count: nombre de steps marques included_in_replay=false
    """
    header_lines = [
        "// Genere automatiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.",
        f"// Genere le {datetime.now().isoformat(timespec='seconds')}",
        f"// Parcours : {clean_steps.parcours or clean_steps.scenario_name or 'sans nom'}",
        f"// Schema JSON  : {clean_steps.schema_version}",
        f"// Steps totaux : {clean_steps.total_steps}",
        "//",
        "// Ce fichier est le format canonique de replay DOMAutopsy. Il est",
        "// lance par POST /api/replay/{run_id} via `npx playwright test`.",
        "// L'encapsulation test.step('[step-XXXX] ...') permet au rapport de",
        "// rapprocher les resultats Playwright avec les etapes du JSON.",
        "",
        "import { test, expect } from '@playwright/test';",
        "",
    ]

    parcours_label = clean_steps.parcours or clean_steps.scenario_name or "parcours DOMAutopsy"
    test_title = _ts_string(f"replay: {parcours_label}")

    body_lines: list[str] = [
        f"test({test_title}, async ({{ page }}, testInfo) => {{",
    ]

    sensitive_vars: dict[str, str] = {}
    unsupported: list[dict[str, Any]] = []
    included_count = 0
    skipped_count = 0

    steps = list(clean_steps.steps or [])

    # Fallback : si aucun navigate initial mais on a une URL scenario, on injecte
    has_initial_navigate = any(
        s.action == "navigate" and s.included_in_replay for s in steps[:2]
    )
    if not has_initial_navigate and parcours_url:
        body_lines.append(
            f"  await test.step({_ts_string('[step-0000] NAVIGATE - scenario start URL')}, async () => {{"
        )
        body_lines.append(f"    await page.goto({_ts_string(parcours_url)});")
        body_lines.append("  });")
        body_lines.append("")

    for i, step in enumerate(steps, start=1):
        step_id = _step_id(step, i)
        action_upper = (step.action or "?").upper()
        desc = (step.description or "").replace("\n", " ")[:80] or step.action

        # Filtre : step conserve pour tracabilite mais non rejoue
        if not step.included_in_replay:
            skipped_count += 1
            reason = step.cleanup_reason or "step filtre par le nettoyage IA"
            body_lines.append(
                f"  // SKIPPED [{step_id}] {action_upper} - {desc}"
            )
            body_lines.append(f"  //   Raison : {reason}")
            body_lines.append("")
            continue

        title = _ts_string(f"[{step_id}] {action_upper} - {desc}")
        body_lines.append(f"  await test.step({title}, async () => {{")
        try:
            for line in _emit_step_body(step, sensitive_vars, i):
                body_lines.append(line)
            included_count += 1
        except UnsupportedAction as e:
            unsupported.append({
                "step_id": step_id,
                "action": step.action,
                "reason": e.reason,
                "description": step.description,
            })
            body_lines.append(
                f'    throw new Error({_ts_string(f"Action non traduisible: {e.reason}")});'
            )
        body_lines.append("  });")
        body_lines.append("")

    body_lines.append("});")

    full = "\n".join(header_lines + body_lines) + "\n"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(full, encoding="utf-8")

    return {
        "path": str(output_path),
        "sensitive_vars": sensitive_vars,
        "unsupported": unsupported,
        "included_count": included_count,
        "skipped_count": skipped_count,
    }
