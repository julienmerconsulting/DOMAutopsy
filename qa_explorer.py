"""
QA Explorer - Browser-Use + DOM Listener + Replay deterministe
===============================================================
Par Julien Mer / JMer Consulting - POC Browser-Use -> Katalon

Pipeline :
  1. Lance Chromium via Playwright avec CDP (port 9222)
  2. Injecte un DOM listener JS (capture clics + inputs dans localStorage)
  3. browser-use se connecte au meme Chromium via CDP et execute le parcours
  4. Recupere le log brut des locateurs captures
  5. Deduplique les inputs (garde la derniere valeur par champ)
  6. Enrichit et classe localement les steps a partir de preuves runtime
  7. Sauvegarde : locator_log.json + clean_steps.json + Playwright TS

Usage :
  # Depuis le Scenario Builder (JSON)
  python qa_explorer.py scenario_zidane.json

  # En ligne de commande rapide
  python qa_explorer.py --url https://example.com --task "Clique sur Login, remplis email..."

  # Mode legacy (TASK hardcode)
  python qa_explorer.py

Prerequis :
  pip install browser-use playwright openai
  playwright install chromium
  
Variables d'environnement (selon --provider) :
  OPENAI_API_KEY=sk-...        # pour --provider openai (defaut)
  GROQ_API_KEY=gsk_...          # pour --provider groq

Exemples avec Groq :
  python qa_explorer.py --provider groq --url https://example.com --task "..."
  python qa_explorer.py --provider groq --model llama-3.3-70b-versatile scenario.json
"""

try:
    from browser_use import Agent, BrowserSession, ChatOpenAI
except ModuleNotFoundError:  # permet les tests purs du post-traitement
    Agent = BrowserSession = ChatOpenAI = None  # type: ignore[assignment]
try:
    from playwright.async_api import async_playwright
except ModuleNotFoundError:
    async_playwright = None  # type: ignore[assignment]
try:
    from dotenv import load_dotenv
except ModuleNotFoundError:
    def load_dotenv(*_args, **_kwargs):
        return False
import asyncio
import json
import os
import re
import sys
import argparse

# Force stdout/stderr UTF-8 sur Windows : sinon un char PUA (ex: icones Font
# Awesome '') dans un print() crash cp1252 -> UnicodeEncodeError.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
from datetime import datetime
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

# Modules refactor Aout 2026 : pipeline unifie autour de test_playwright.spec.ts
from schemas import CURRENT_SCHEMA_VERSION
from clean_steps_builder import (
    extract_browser_use_history,
    build_clean_steps,
)
from playwright_generator import generate_playwright_ts
from selector_enricher import (
    enrich_browser_use_history_selectors,
    enrich_browser_use_step_snapshot,
)
from browser_use_patches import (
    SUPPORTED_BROWSER_USE_VERSION,
    patch_transparent_form_control_visibility,
)

# Charger les variables du .env (OPENAI_API_KEY notamment)
load_dotenv()

# Import du generateur de rapport (optionnel)
try:
    from report_generator import generate_report, open_report
    HAS_REPORT = True
except ImportError:
    HAS_REPORT = False


# ============================================================
# MONKEY-PATCH : Fix browser-use CDP frame crash
# Bug: asyncio.gather sans return_exceptions dans _get_ax_tree_for_all_frames
# Quand une iframe pub/cookies est detruite, le gather crash tout le DOM
# Fix: try/except + retry apres stabilisation DOM
# Ref: https://github.com/browser-use/browser-use/issues/2808
# ============================================================

def _patch_browser_use():
    """Applique les correctifs runtime bornes de Browser Use."""
    try:
        from browser_use.dom.service import DomService

        _original_get_ax_tree = DomService._get_ax_tree_for_all_frames

        async def _patched_get_ax_tree(self, *args, **kwargs):
            """Version patchee : tolere les frames detruites au lieu de crasher"""
            try:
                return await _original_get_ax_tree(self, *args, **kwargs)
            except RuntimeError as e:
                if "frameId is not found" in str(e):
                    print(f"  [PATCH] Frame detruite ignoree, retry apres stabilisation...")
                    await asyncio.sleep(0.5)
                    try:
                        return await _original_get_ax_tree(self, *args, **kwargs)
                    except RuntimeError:
                        print(f"  [PATCH] DOM toujours instable, renvoi arbre vide")
                        return {'nodes': []}
                raise

        DomService._get_ax_tree_for_all_frames = _patched_get_ax_tree
        print("  [PATCH] browser-use CDP frame crash patche")

        try:
            installed_version = package_version("browser-use")
        except PackageNotFoundError:
            installed_version = "inconnue"

        if patch_transparent_form_control_visibility(DomService, installed_version):
            print(
                "  [PATCH] browser-use checkbox/radio transparents accessibles "
                f"actifs ({installed_version})"
            )
        else:
            print(
                "  [PATCH] checkbox/radio transparents non applique : "
                f"browser-use={installed_version}, attendu={SUPPORTED_BROWSER_USE_VERSION}"
            )

    except ImportError:
        print("  [PATCH] browser-use pas installe, patch ignore")
    except (AttributeError, TypeError) as exc:
        print(f"  [PATCH] API browser-use modifiee, patch non applique : {exc}")


# ============================================================
# CONFIGURATION
# ============================================================

LLM_MODEL = "gpt-4.1-mini"            # defaut historique (provider openai)
CDP_PORT = 9222

# Providers LLM compatibles API OpenAI (chat.completions)
# Pour ajouter un provider : meme structure (base_url, env_var, default_model)
PROVIDERS = {
    "openai": {
        "base_url": None,                       # URL par defaut du SDK OpenAI
        "env_var": "OPENAI_API_KEY",
        "default_model": "gpt-4.1-mini",
    },
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "env_var": "GROQ_API_KEY",
        "default_model": "llama-3.3-70b-versatile",
    },
}

# Modeles Groq qui acceptent le format multimodal (content list avec images)
# browser-use envoie un screenshot a chaque step -> sans vision, il faut forcer use_vision=False
GROQ_VISION_MODELS = {
    "meta-llama/llama-4-scout-17b-16e-instruct",
    "meta-llama/llama-4-maverick-17b-128e-instruct",
}

# Formats de sortie supportes pour le code generate par l'IA de cleanup
# Pour ajouter un format : meme structure (label, extension, code_instructions)
# Le bloc code_instructions est injecte dans le prompt IA a la place du bloc Katalon
OUTPUT_FORMATS = {
    "katalon": {
        "label": "Katalon Studio (Groovy)",
        "extension": ".groovy",
        "code_instructions": """6. Genere le code Katalon Studio (Groovy) complet et fonctionnel pour rejouer ce parcours.

   REGLE CRITIQUE :
   - Ne modifie JAMAIS les selecteurs qui ont unique: true. Utilise-les EXACTEMENT tels quels.
   - EXCEPTION pour unique: false : si le selecteur est non-unique ET qu'un texte est disponible
     dans le log (champ "text"), genere un XPath avec le texte pour fiabiliser le clic.
   - Pour creer un TestObject :
     TestObject to = new TestObject("nomDescriptif")
     to.addProperty("xpath" ou "css", ConditionType.EQUALS, "le_selecteur_exact")
   - Imports requis :
     import com.kms.katalon.core.testobject.TestObject
     import com.kms.katalon.core.testobject.ConditionType
     import com.kms.katalon.core.webui.keyword.WebUiBuiltInKeywords as WebUI
   - WebUI.openBrowser('') + WebUI.navigateToUrl(url) en debut
   - WebUI.click(to) pour les clics, WebUI.setText(to, value) pour les inputs
   - WebUI.verifyElementPresent(to, 10) avant chaque interaction, WebUI.delay(1) entre chaque
   - Si "inShadowDOM": true, utilise WebUI.executeJavaScript() avec le jsSelector fourni
   - WebUI.closeBrowser() en fin
   - Commentaires en francais""",
    },
    "playwright": {
        "label": "Playwright (TypeScript)",
        "extension": ".spec.ts",
        "code_instructions": """6. Genere un test Playwright en TypeScript complet et fonctionnel pour rejouer ce parcours.

   REGLE CRITIQUE :
   - Ne modifie JAMAIS les selecteurs unique: true. Utilise-les EXACTEMENT tels quels.
   - Pour les selecteurs unique: false avec texte disponible, utilise getByText() ou getByRole() avec name.
   - Structure :
     import { test, expect } from '@playwright/test';
     test('description', async ({ page }) => {
       await page.goto(url);
       await page.locator('selector').click();
       await page.locator('selector').fill('value');
     });
   - Pour XPath utilise page.locator('xpath=...').click()
   - Pour CSS utilise page.locator('selector').click()
   - Pour shadow DOM utilise page.locator('host >>> inner').click()
   - Ajoute await page.waitForLoadState('networkidle') apres les navigations
   - Commentaires en francais""",
    },
    "cypress": {
        "label": "Cypress (JavaScript)",
        "extension": ".cy.js",
        "code_instructions": """6. Genere un test Cypress en JavaScript complet et fonctionnel pour rejouer ce parcours.

   REGLE CRITIQUE :
   - Ne modifie JAMAIS les selecteurs unique: true. Utilise-les EXACTEMENT tels quels.
   - Pour selecteurs unique: false avec texte disponible, utilise cy.contains() pour fiabiliser.
   - Structure :
     describe('parcours', () => {
       it('test', () => {
         cy.visit(url);
         cy.get('selector').click();
         cy.get('selector').type('value');
       });
     });
   - Pour XPath, installe cypress-xpath et utilise cy.xpath('//...')
   - Pour CSS utilise cy.get('selector')
   - cy.contains(text) si le texte est plus stable que le selecteur
   - Commentaires en francais""",
    },
    "selenium": {
        "label": "Selenium (Python)",
        "extension": ".py",
        "code_instructions": """6. Genere un test Selenium WebDriver en Python complet et fonctionnel pour rejouer ce parcours.

   REGLE CRITIQUE :
   - Ne modifie JAMAIS les selecteurs unique: true. Utilise-les EXACTEMENT tels quels.
   - Pour selecteurs unique: false avec texte, prefere XPath text-based.
   - Structure :
     from selenium import webdriver
     from selenium.webdriver.common.by import By
     from selenium.webdriver.support.ui import WebDriverWait
     from selenium.webdriver.support import expected_conditions as EC
     driver = webdriver.Chrome()
     driver.get(url)
     WebDriverWait(driver, 10).until(EC.element_to_be_clickable((By.CSS_SELECTOR, 'selector'))).click()
     element = driver.find_element(By.CSS_SELECTOR, 'selector')
     element.send_keys('value')
   - Utilise By.XPATH pour selecteurs commencant par //, By.CSS_SELECTOR sinon
   - WebDriverWait avant chaque interaction (attendre element_to_be_clickable)
   - driver.quit() en fin
   - Commentaires en francais""",
    },
}
MIN_WAIT_PAGE_LOAD = 2.0          # seconds, min wait avant snapshot DOM (defaut browser-use: 0.5s, trop court pour les SPA)
MAX_WAIT_PAGE_LOAD = 15.0         # seconds, max wait avant timeout snapshot
NETWORK_IDLE_WAIT = 3.0           # seconds, wait apres derniere requete reseau
MAX_STEPS = 25                    # garde-fou anti-boucle infinie


# ============================================================
# CLASSIFIEUR DU VERDICT AGENT BU
# ============================================================
# BU peut retourner sa reponse sous des formes tres variees :
#   - "SUCCESS: ..." (verdict en tete, ancien pattern)
#   - "RECAPITULATIF...\n...\nretour SUCCESS" (verdict en queue, actuel)
#   - "FAIL : ..." (echec en tete)
#   - texte libre sans verdict (unknown)
# On cherche donc un mot-cle isole (word-boundary) prioritairement en TETE
# du texte, puis dans la QUEUE (derniers 3000 chars) qui contient
# typiquement la conclusion de l'agent apres son recapitulatif.
def _classify_agent_verdict(text: str | None) -> str:
    """Retourne 'success' | 'fail' | 'interrupted' | 'unknown'."""
    if not text:
        return "unknown"
    head_upper = str(text).lstrip().upper()
    if head_upper.startswith("SUCCESS"):
        return "success"
    if head_upper.startswith(("FAIL", "ERROR")):
        return "fail"
    if head_upper.startswith(("INTERRUPT", "STOP")):
        return "interrupted"
    # Fallback : verdict dans la queue (recapitulatif conclut souvent
    # par "retour SUCCESS." ou "-> FAIL: raison")
    tail = str(text)[-3000:]
    if re.search(r"\bSUCCESS\b", tail, re.IGNORECASE):
        return "success"
    if re.search(r"\b(?:FAIL|ERROR)\b", tail, re.IGNORECASE):
        return "fail"
    if re.search(r"\b(?:INTERRUPT|STOP)\b", tail, re.IGNORECASE):
        return "interrupted"
    return "unknown"

# TASK par defaut (legacy, utilise si aucun JSON ni --task)
DEFAULT_TASK = """
Va sur https://www.footmercato.net

IMPORTANT : Si un element n'est pas visible ou cliquable, scroll vers le haut ou le bas pour le trouver avant de reessayer. Si un popup de cookies apparait, accepte-le d'abord.

Sur la page d'accueil :
- Clique sur "Rechercher" (icone loupe en haut a droite)
- Tu vas voir des categories : Competitions, Equipes, Joueurs. Clique sur le mot "Joueurs"
- Dans le champ de recherche qui s'ouvre, tape "zidane"
- Attends que les resultats apparaissent
- Dans la liste qui apparait, clique sur "Zinedine Zidane" (53 ans - France)

Sur la page de Zinedine Zidane :
- Verifie que "Zinedine Zidane" et "Entraineur" sont affiches
- Clique sur le mot "Actus" (a cote de "Resume")
- Lis le titre du premier article

Retourne SUCCESS avec :
- Le nom complet du joueur
- Son role
- Le titre du premier article
Si une etape echoue -> retourne FAIL avec la raison
"""


# ============================================================
# CONSTRUCTION DU TASK DEPUIS JSON (Scenario Builder)
# ============================================================

# Mapping action -> generateur de ligne prompt
# Chaque action du builder est convertie en instruction naturelle pour browser-use
TASK_BUILDERS = {
    "navigate": lambda s: f"- Va sur {s['url']}",

    "click": lambda s: (
        f"- Attends {s['wait_before']} secondes puis clique sur {s['target']}"
        if s.get('wait_before')
        else f"- Clique sur {s['target']}"
    ),

    "input": lambda s: (
        f"- Vide le champ {s['target']} puis tape \"{s['value']}\""
        if s.get('clear_before') in (True, 'true', 'Oui')
        else f"- Dans {s['target']}, tape \"{s['value']}\""
    ),

    "select": lambda s: f"- Dans la liste {s['target']}, selectionne \"{s['value']}\"",

    "verify": lambda s: _build_verify_line(s),

    "scroll": lambda s: (
        f"- Scroll jusqu'a {s['target']}"
        if s.get('direction') == 'vers_element' and s.get('target')
        else f"- Scroll vers le {'bas' if s.get('direction', 'bas') == 'bas' else 'haut'}"
    ),

    "hover": lambda s: f"- Survole {s['target']} (pour faire apparaitre le menu/tooltip)",

    "wait": lambda s: (
        f"- Attends que {s['target']}"
        if s.get('target')
        else f"- Attends {s.get('seconds', 2)} secondes"
    ),

    "screenshot": lambda s: f"- Prends une capture ecran (nom: {s.get('name', 'capture')})",

    "cookie": lambda s: (
        f"- Si une banniere de cookies apparait, clique sur {s['target']}"
        if s.get('target')
        else "- Si une banniere de cookies apparait, accepte-la"
    ),
}


def _build_verify_line(step):
    """Construit la ligne de verification selon le type"""
    target = step['target']
    vtype = step.get('type', 'presence')

    if vtype == 'texte_contient':
        return f"- Verifie que la page contient le texte \"{target}\""
    elif vtype == 'texte_exact':
        return f"- Verifie que le texte exact \"{target}\" est affiche"
    elif vtype == 'visible':
        return f"- Verifie que {target} est visible a l'ecran"
    elif vtype == 'absent':
        return f"- Verifie que {target} n'est PAS present sur la page"
    else:
        # presence (defaut)
        return f"- Verifie que {target} est present"


def build_task_from_json(filepath):
    """
    Construit le prompt TASK pour browser-use depuis un fichier JSON
    genere par le Scenario Builder.
    
    Le JSON attendu :
    {
      "name": "Parcours achat",
      "url": "https://example.com",
      "steps": [
        {"action": "click", "target": "bouton Login"},
        {"action": "input", "target": "champ email", "value": "test@test.com"},
        {"action": "verify", "target": "Dashboard affiche", "type": "presence"}
      ]
    }
    """
    with open(filepath, "r", encoding="utf-8") as f:
        scenario = json.load(f)

    url = scenario.get("url", "")
    name = scenario.get("name", "Scenario")
    steps = scenario.get("steps", [])

    if not url:
        raise ValueError("Le scenario JSON n'a pas d'URL de depart")
    if not steps:
        raise ValueError("Le scenario JSON n'a aucune etape")

    # Construction du prompt
    lines = []
    lines.append(f"Va sur {url}")
    lines.append("")
    lines.append("IMPORTANT : Si un element n'est pas visible ou cliquable, scroll vers le haut ou le bas pour le trouver avant de reessayer. Si un popup de cookies apparait, accepte-le d'abord.")
    lines.append("")

    # Regrouper les verifications finales
    verify_steps = []
    action_steps = []
    for step in steps:
        if step.get("action") == "verify":
            verify_steps.append(step)
        else:
            action_steps.append(step)
            # Si des verifs sont intercalees, on les garde inline
            # On re-ajoute les verifs qui etaient avant la derniere action
            # Non : on garde l'ordre du scenario tel quel
    
    # Finalement on garde l'ordre exact du scenario
    for step in steps:
        action = step.get("action", "")
        builder = TASK_BUILDERS.get(action)
        if builder:
            lines.append(builder(step))
        else:
            # Action inconnue : on la passe en texte brut
            lines.append(f"- {action}: {step}")

    # Consigne de retour
    lines.append("")
    lines.append("Retourne SUCCESS si toutes les etapes et verifications ont reussi.")
    lines.append("Si une etape echoue -> retourne FAIL avec la raison precise.")

    task = "\n".join(lines)

    print_header(f"SCENARIO : {name}")
    print(f"  URL     : {url}")
    print(f"  Etapes  : {len(steps)}")
    print(f"  Source  : {filepath}")
    print(f"\n  --- PROMPT GENERE ---")
    for line in lines:
        print(f"  {line}")
    print(f"  --- FIN PROMPT ---\n")

    return task


def resolve_task():
    """
    Determine le TASK a utiliser selon les arguments :
      1. python qa_explorer.py scenario.json        -> JSON du builder
      2. python qa_explorer.py --url X --task "..."  -> ligne de commande
      3. python qa_explorer.py                       -> TASK par defaut
    """
    parser = argparse.ArgumentParser(
        description="QA Explorer - Browser-Use + DOM Listener + AI Cleanup"
    )
    parser.add_argument(
        "scenario_file", nargs="?", default=None,
        help="Fichier JSON du scenario (genere par Scenario Builder)"
    )
    parser.add_argument(
        "--url", default=None,
        help="URL de depart (mode ligne de commande)"
    )
    parser.add_argument(
        "--task", default=None,
        help="Description du parcours en texte libre"
    )
    parser.add_argument(
        "--provider", default="openai", choices=list(PROVIDERS.keys()),
        help="Provider LLM (defaut: openai). Lit la cle dans la variable d'env du provider."
    )
    parser.add_argument(
        "--model", default=None,
        help="Modele LLM (defaut: depend du provider, ex gpt-4.1-mini pour openai, llama-3.3-70b-versatile pour groq)"
    )
    parser.add_argument(
        "--port", type=int, default=CDP_PORT,
        help=f"Port CDP Chromium (defaut: {CDP_PORT})"
    )
    parser.add_argument(
        "--min-wait", type=float, default=MIN_WAIT_PAGE_LOAD,
        help=f"Temps min d'attente avant snapshot DOM en sec (defaut: {MIN_WAIT_PAGE_LOAD}s, augmente pour les SPA lentes)"
    )
    parser.add_argument(
        "--max-wait", type=float, default=MAX_WAIT_PAGE_LOAD,
        help=f"Temps max d'attente avant snapshot DOM en sec (defaut: {MAX_WAIT_PAGE_LOAD}s)"
    )
    parser.add_argument(
        "--network-idle", type=float, default=NETWORK_IDLE_WAIT,
        help=f"Temps d'attente reseau idle en sec (defaut: {NETWORK_IDLE_WAIT}s)"
    )
    parser.add_argument(
        "--max-steps", type=int, default=MAX_STEPS,
        help=f"Nombre max d'etapes de l'agent (defaut: {MAX_STEPS}, evite les boucles infinies)"
    )
    parser.add_argument(
        "--vision", dest="vision", action="store_true", default=None,
        help="Force l'envoi de screenshots au LLM (defaut: auto - OFF pour Groq non-vision, ON sinon)"
    )
    parser.add_argument(
        "--no-vision", dest="vision", action="store_false",
        help="Force la desactivation des screenshots (utile pour Groq text-only et reduire le coup token)"
    )
    parser.add_argument(
        "--output-format", default="katalon", choices=list(OUTPUT_FORMATS.keys()),
        help="Format du code de test genere (defaut: katalon). Choix: " + ", ".join(OUTPUT_FORMATS.keys())
    )
    parser.add_argument(
        "--headless", action="store_true",
        help="Lance Chromium en mode headless (defaut: visible). Recommande pour le serveur web et le multi-run."
    )
    parser.add_argument(
        "--output-dir", default=None,
        help="Dossier de sortie pour les artefacts du run (defaut: runs/<timestamp>/). Cree s'il n'existe pas."
    )
    parser.add_argument(
        "--no-open-report", dest="open_report", action="store_false", default=True,
        help="Ne pas ouvrir le rapport HTML dans le navigateur a la fin (utile pour le mode serveur)."
    )

    args = parser.parse_args()

    # Resoudre le provider LLM : base_url + cle API + modele par defaut
    provider_cfg = PROVIDERS[args.provider]
    api_key = os.getenv(provider_cfg["env_var"])
    if not api_key:
        print(f"  ERREUR : variable d'env {provider_cfg['env_var']} manquante pour --provider {args.provider}")
        print(f"  Soit la mettre dans .env, soit changer de provider via --provider")
        sys.exit(1)
    model = args.model or provider_cfg["default_model"]
    # Resoudre use_vision : explicite si --vision/--no-vision, sinon auto selon provider/modele
    if args.vision is not None:
        use_vision = args.vision
        vision_reason = "force par CLI"
    elif args.provider == "groq" and model not in GROQ_VISION_MODELS:
        use_vision = False
        vision_reason = "auto OFF (modele Groq text-only)"
    else:
        use_vision = True
        vision_reason = "auto ON"
    print(f"  Provider : {args.provider} (modele: {model})")
    print(f"  Vision   : {use_vision} ({vision_reason})")

    timing_opts = {
        "min_wait": args.min_wait,
        "max_wait": args.max_wait,
        "network_idle": args.network_idle,
        "max_steps": args.max_steps,
        "provider": args.provider,
        "base_url": provider_cfg["base_url"],
        "api_key": api_key,
        "use_vision": use_vision,
        "output_format": args.output_format,
        "headless": args.headless,
        "output_dir": args.output_dir,
        "open_report": args.open_report,
    }

    # Mode 1 : Fichier JSON du Scenario Builder
    if args.scenario_file:
        if not os.path.exists(args.scenario_file):
            print(f"  Fichier introuvable : {args.scenario_file}")
            sys.exit(1)
        with open(args.scenario_file, "r", encoding="utf-8") as f:
            scenario = json.load(f)
        task = build_task_from_json(args.scenario_file)
        return task, model, args.port, scenario.get("name", "Scenario"), scenario.get("url", ""), scenario.get("steps", []), timing_opts

    # Mode 2 : --url + --task en ligne de commande
    if args.url and args.task:
        task = f"""Va sur {args.url}

IMPORTANT : Si un element n'est pas visible ou cliquable, scroll pour le trouver. Si un popup de cookies apparait, accepte-le d'abord.

{args.task}

Retourne SUCCESS si tout s'est bien passe, FAIL avec la raison sinon."""
        print_header("MODE LIGNE DE COMMANDE")
        print(f"  URL  : {args.url}")
        print(f"  Task : {args.task}")
        return task, model, args.port, "CLI", args.url, [], timing_opts

    # Mode 3 : TASK par defaut (legacy)
    print_header("MODE LEGACY (TASK PAR DEFAUT)")
    print("  Aucun scenario JSON ni --url/--task fourni")
    print("  Utilisation du TASK hardcode")
    return DEFAULT_TASK, model, args.port, "Legacy", "", [], timing_opts


# ============================================================
# DOM LISTENER (injecte dans le navigateur)
# ============================================================

DOM_LISTENER_JS = (Path(__file__).parent / "dom_listener.js").read_text(encoding="utf-8")



# ============================================================
# DEDUPLICATION
# ============================================================

def dedup_log(raw_log):
    """
    Deduplique le log brut de facon STRICTEMENT LOCALE (fix R3 review) :
    - Clics : skip UNIQUEMENT si le PRECEDENT immediat est un clic identique
      sur meme selecteur+URL
    - Scrolls : gardes tels quels, jamais dedupliques
    - Inputs : ecrase le PRECEDENT immediat SI meme selecteur+URL (l'utilisateur
      tape lettre par lettre - chaque frappe = 1 event input, on garde le dernier
      etat du champ). Toute autre action DOM entre 2 inputs (click, scroll,
      keyboard, meme un input sur un autre champ) coupe la sequence : le nouvel
      input est traite comme un NOUVEAU cycle et conserve independamment.
    - Keyboard : gardes tels quels (Enter, Tab, Escape - separateurs naturels
      des cycles input, essentiels pour scenarios type TodoMVC "12 saisies +
      12 Enter" qui doivent produire 24 steps distincts, pas 1 seul).
    - Autres actions (hover, etc.) : gardes tels quels.

    L'ancien code consolidait GLOBALEMENT tous les inputs sur (selector, url)
    quelle que soit la distance temporelle. Consequence sur TodoMVC : 12
    saisies successives sur input.new-todo etaient consolidees en 1 seule
    (garde la derniere). Fix : consolidation LOCALE uniquement sur inputs
    strictement adjacents.
    """
    clean = []
    for entry in raw_log:
        action = entry.get('action')
        if action == 'click':
            if clean:
                prev = clean[-1]
                if (prev.get('action') == 'click'
                        and prev.get('selector', {}).get('value') == entry.get('selector', {}).get('value')
                        and prev.get('url', '') == entry.get('url', '')):
                    continue  # doublon strictement consecutif
            clean.append(entry)
        elif action == 'input':
            # Consolidation LOCALE : ecrase l'entree precedente UNIQUEMENT si
            # c'est un input consecutif sur le meme champ (meme selecteur + url).
            # Toute autre action entre 2 inputs coupe la sequence.
            if clean:
                prev = clean[-1]
                if (prev.get('action') == 'input'
                        and prev.get('selector', {}).get('value') == entry.get('selector', {}).get('value')
                        and prev.get('url', '') == entry.get('url', '')):
                    clean[-1] = entry  # meme champ, on met a jour la valeur
                    continue
            clean.append(entry)
        else:
            # scroll, keyboard, hover, ... : jamais deduplique
            clean.append(entry)
    return clean


# ============================================================
# AFFICHAGE
# ============================================================

def print_header(text):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}")


def print_raw_log(log):
    """Affiche le log brut deduplique"""
    shadow_count = sum(1 for e in log if e.get('inShadowDOM'))
    scroll_count = sum(1 for e in log if e.get('action') == 'scroll')
    print_header(f"LOCATEURS CAPTURES (dedupliques) : {len(log)} entrees ({shadow_count} shadow DOM, {scroll_count} scrolls)")
    for i, entry in enumerate(log):
        action = entry['action']
        selector = entry.get('selector', {})
        text = entry.get('text', entry.get('value', ''))
        if text:
            text = str(text)[:50]
        url = entry.get('url', '')
        in_shadow = entry.get('inShadowDOM', False)
        shadow_tag = " SHADOW" if in_shadow else ""

        if action == 'scroll':
            direction = entry.get('direction', '?')
            delta = entry.get('deltaY', 0)
            scrollY = entry.get('scrollY', 0)
            vp = entry.get('viewport', {})
            print(f"\n  [{i+1}] SCROLL sur ...{url[-60:]}")
            print(f"      Direction : {direction} ({delta:+d}px)")
            print(f"      Position  : {scrollY}px / {vp.get('docHeight', '?')}px")
            print(f"      Viewport  : {vp.get('width', '?')}x{vp.get('height', '?')}")
            continue

        print(f"\n  [{i+1}] {action.upper()}{shadow_tag} sur ...{url[-60:]}")
        print(f"      Strategie : {selector.get('strategy', '?')}")
        print(f"      Selecteur : {selector.get('value', '?')}")
        unique = selector.get('unique', '?')
        match_count = selector.get('matchCount', '?')
        unique_icon = "OK" if unique == True else ("!!" if unique == False else "?")
        print(f"      Unique    : {unique_icon} (matchCount: {match_count})")
        if in_shadow and selector.get('jsSelector'):
            print(f"      JS Chain  : {selector['jsSelector']}")
        if in_shadow and selector.get('shadowChain'):
            chain_str = ' >>> '.join([c['selector'] for c in selector['shadowChain']])
            print(f"      PW Chain  : {chain_str}")
        if text:
            print(f"      Texte/Val : {text}")
        if entry.get('attributes', {}).get('name'):
            print(f"      Name      : {entry['attributes']['name']}")


def print_clean_steps(clean_data):
    """Affiche le parcours nettoye par l'IA"""
    if "raw_response" in clean_data:
        print(clean_data["raw_response"])
        return

    print_header(f"PARCOURS NETTOYE : {clean_data.get('parcours', 'N/A')}")

    # Anomalies
    anomalies = clean_data.get('anomalies', [])
    if anomalies:
        print(f"\n  ANOMALIES DETECTEES ({len(anomalies)}) :")
        for a in anomalies:
            print(f"      - {a}")

    # Steps
    steps = clean_data.get('steps', [])
    print(f"\n  STEPS ({len(steps)}) :")
    for s in steps:
        action = s.get('action', '?').upper()
        desc = s.get('description', '?')
        selector = s.get('selector', '?')
        sel_type = s.get('selectorType', '?')
        unique = s.get('unique', '?')
        value = s.get('value')
        unique_icon = "OK" if unique == True else "!!"
        print(f"\n  [{s.get('step', '?')}] {action} -- {desc}")
        print(f"      Selecteur : [{sel_type}] {selector}")
        print(f"      Unique    : {unique_icon}")
        if value:
            print(f"      Valeur    : {value}")

    # Code Katalon
    katalon_code = clean_data.get('katalon_code', '')
    if katalon_code:
        print_header("CODE KATALON STUDIO (Groovy)")
        print(katalon_code)


# ============================================================
# MAIN
# ============================================================

async def run(task, model=LLM_MODEL, cdp_port=CDP_PORT, scenario_name="", scenario_url="", scenario_steps=None, timing_opts=None):
    """Fonction principale d'execution du QA Explorer."""
    if async_playwright is None or Agent is None or BrowserSession is None or ChatOpenAI is None:
        raise RuntimeError("Dependances runtime absentes : lancer pip install -r requirements.txt")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    timing_opts = timing_opts or {}
    started_at = datetime.now().isoformat()
    min_wait = timing_opts.get("min_wait", MIN_WAIT_PAGE_LOAD)
    max_wait = timing_opts.get("max_wait", MAX_WAIT_PAGE_LOAD)
    network_idle = timing_opts.get("network_idle", NETWORK_IDLE_WAIT)
    max_steps = timing_opts.get("max_steps", MAX_STEPS)
    provider = timing_opts.get("provider", "openai")
    base_url = timing_opts.get("base_url")
    api_key = timing_opts.get("api_key")
    use_vision = timing_opts.get("use_vision", True)
    output_format = timing_opts.get("output_format", "katalon")
    headless = timing_opts.get("headless", False)
    output_dir_arg = timing_opts.get("output_dir") or os.path.join("runs", timestamp)
    output_dir = Path(output_dir_arg)
    output_dir.mkdir(parents=True, exist_ok=True)
    should_open_report = timing_opts.get("open_report", True)
    print(f"  Output dir : {output_dir}")

    # -- ETAPE 1 : Lancer Chromium avec CDP --
    # Headless=True : indispensable en multi-run web UI (sinon 12 fenetres se chevauchent)
    # Le screencast CDP marche aussi bien en headless qu'en headed
    print_header("LANCEMENT CHROMIUM + CDP" + (" [HEADLESS]" if headless else ""))
    pw = await async_playwright().start()
    # cdp_port est PRE-ALLOUE par le runner parent (bench) via
    # _allocate_unique_cdp_ports (bind simultane des N sockets, aucun
    # worker ne fait sa propre recherche -> pas de TOCTOU entre workers).
    # Mode standalone : cdp_port passe en CLI (ex: 9222 pour Chrome dev).
    # Locale Chromium alignee sur la langue systeme utilisateur (memes
    # regles pour capture et replay). Sans ca, un CMP GDPR/Consent qui
    # rend son bandeau selon Accept-Language peut renvoyer "Consent" en
    # capture (default en-US) puis "Accepter" au replay (fr-FR) -> selecteur
    # casse. On lit locale.getdefaultlocale() pour rester agnostique OS/user.
    # Override possible via env var PW_LOCALE.
    import locale as _locale
    try:
        _sys_loc, _ = _locale.getdefaultlocale()
    except Exception:
        _sys_loc = None
    _bu_locale = os.getenv("PW_LOCALE") or (
        _sys_loc.replace("_", "-") if _sys_loc else "en-US"
    )
    chromium_args = [
        f"--remote-debugging-port={cdp_port}",
        f"--lang={_bu_locale}",
        f"--accept-lang={_bu_locale}",
    ]
    if headless:
        # Optimisations RAM/CPU pour permettre N Chromiums en parallele
        chromium_args += [
            "--no-sandbox",
            "--disable-gpu",
            "--disable-dev-shm-usage",
            "--disable-background-networking",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--window-size=1280,720",
        ]
    else:
        chromium_args.append("--start-maximized")
    # timeout=600000 (10 min) : sur le bench, 5 Chromium lances en parallele
    # peuvent depasser le default Playwright de 180s (contention I/O disque
    # cold-start Windows). Sans ca, timeout=180000ms tue le worker.
    # Journalisation : ts_before + ts_after launch + duration, ecrit dans
    # output_dir/worker_launch.json (append). Permet de mesurer la vraie
    # duree cold-start Chromium sous contention et distinguer collision
    # de port (fail immediat) vs simple contention I/O (long mais reussi).
    import time as _time
    _ts_launch_start = _time.monotonic()
    browser = await pw.chromium.launch(headless=headless, args=chromium_args, timeout=600000)
    _ts_launch_end = _time.monotonic()
    try:
        _launch_file = Path(timing_opts.get("output_dir") or ".") / "worker_launch.json"
        _data = json.loads(_launch_file.read_text(encoding="utf-8")) if _launch_file.exists() else {}
        _data["ts_launch_start_monotonic"] = _ts_launch_start
        _data["ts_launch_end_monotonic"] = _ts_launch_end
        _data["launch_duration_s"] = round(_ts_launch_end - _ts_launch_start, 2)
        _data["cdp_port_used"] = cdp_port
        _launch_file.write_text(json.dumps(_data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as _log_err:
        print(f"  [WARN] worker_launch.json log echoue : {_log_err}")
    try:
        context = browser.contexts[0] if browser.contexts else await browser.new_context(no_viewport=True)
        page = context.pages[0] if context.pages else await context.new_page()

        # === DIALOG HANDLER : Playwright unique proprietaire ===
        # BU 0.13.8 PopupsWatchdog enregistre son handler CDP sur la session
        # cible ET sur le client racine (double-register). En parallele,
        # Playwright a un auto-handler qui tente aussi de fermer le dialog.
        # Race condition -> ProtocolError "No dialog is showing" non-catchee
        # dans playwright/dialog.ts 1.57 -> crash du process Node driver ->
        # WebSocket CDP fermee -> BU stop apres 5 retries. Observe sur
        # demoblaze (alert 'Product added' apres Add to cart).
        # Fix : on installe UN listener explicite cote Playwright, il devient
        # l'unique proprietaire. Comportement natif preserve (le dialog
        # s'ouvre bien, on l'accepte pour alert/confirm/beforeunload et
        # on dismiss pour prompt selon la policy BU). On log dans les
        # preuves DOMAutopsy pour tracabilite.
        dialog_log = []
        async def _handle_dialog(dialog):
            entry = {
                "type": dialog.type,
                "message": dialog.message,
                "url": page.url,
                "ts": int(__import__('time').time() * 1000),
            }
            dialog_log.append(entry)
            try:
                if dialog.type in ("alert", "confirm", "beforeunload"):
                    await dialog.accept()
                else:  # prompt : dismiss (policy alignee sur BU)
                    await dialog.dismiss()
            except Exception:
                pass  # dialog deja handled ailleurs, ignore silencieusement
        page.on("dialog", _handle_dialog)
        # Aussi sur les futures pages (open_tab par BU)
        context.on("page", lambda newp: newp.on("dialog", _handle_dialog))

        # Forcer plein ecran via CDP
        try:
            cdp = await page.context.new_cdp_session(page)
            window = await cdp.send("Browser.getWindowForTarget")
            window_id = window["windowId"]
            await cdp.send("Browser.setWindowBounds", {
                "windowId": window_id,
                "bounds": {"windowState": "maximized"}
            })
            print(f"  Chromium lance PLEIN ECRAN sur CDP port {cdp_port}")
        except Exception as e:
            print(f"  Chromium lance sur CDP port {cdp_port} (maximized via args)")
            print(f"  CDP maximize fallback: {e}")

        print(f"  Page prete : {page.url}")

        # -- ETAPE 2 : Injecter le DOM Listener --
        # En bench_mode : setter window.__DOMAUTOPSY_BENCH_MODE=true AVANT
        # le listener pour desactiver la redaction des inputs sensibles
        # (le TS de replay a besoin des vraies valeurs pour se reconnecter).
        bench_mode = bool((timing_opts or {}).get("bench_mode", False))
        _bench_prelude = "window.__DOMAUTOPSY_BENCH_MODE = true;" if bench_mode else ""
        await context.add_init_script(_bench_prelude + DOM_LISTENER_JS)
        await page.evaluate(_bench_prelude + DOM_LISTENER_JS)
        print(f"  DOM Listener injecte via Playwright" + (" [BENCH_MODE: pas de redaction]" if bench_mode else ""))

        # Via CDP direct (couvre TOUS les contextes, y compris celui de browser-use)
        try:
            cdp_session = await browser.new_browser_cdp_session()
            await cdp_session.send("Page.addScriptToEvaluateOnNewDocument", {
                "source": DOM_LISTENER_JS
            })
            print(f"  DOM Listener injecte via CDP (global, tous contextes)")
        except Exception as e:
            print(f"  Injection CDP echouee (pas critique): {e}")

        # -- ETAPE 2bis : Brancher la capture observabilite (V3 phases 1 + 2) --
        # Sessions CDP de la PAGE (pas browser-level : ces events sont per-target).
        js_errors = []
        console_messages = []
        network_log = []
        dom_mutations = {"attribute_modified": 0, "child_node_inserted": 0, "child_node_removed": 0, "first_mutation_ms": None, "last_mutation_ms": None}
        # Index pour matcher les responses sur les requests : {requestId: index_dans_network_log}
        _net_index = {}
        # Filtre PIEGE 1 : on garde uniquement les types pertinents pour le QA
        # (Fetch/XHR = API calls, Document = HTML page, WebSocket = realtime)
        # On ignore : Image, Stylesheet, Font, Media, Other, Script (assets)
        NETWORK_KEEP_TYPES = {"Fetch", "XHR", "Document", "WebSocket", "EventSource"}
        try:
            page_cdp = await page.context.new_cdp_session(page)
            await page_cdp.send("Runtime.enable")
            await page_cdp.send("Console.enable")
            await page_cdp.send("Network.enable")

            def on_exception(event):
                exc = event.get("exceptionDetails", {})
                js_errors.append({
                    "timestamp": event.get("timestamp"),
                    "text": exc.get("text", ""),
                    "exception": (exc.get("exception") or {}).get("description", ""),
                    "url": exc.get("url"),
                    "lineNumber": exc.get("lineNumber"),
                    "columnNumber": exc.get("columnNumber"),
                    "stackTrace": (exc.get("stackTrace") or {}).get("callFrames", [])[:5],
                })

            def on_console(event):
                msg = event.get("message", {})
                console_messages.append({
                    "level": msg.get("level"),
                    "text": msg.get("text", "")[:500],
                    "url": msg.get("url"),
                    "line": msg.get("line"),
                })

            def on_request(event):
                # Filtre par resourceType (PIEGE 1)
                rtype = event.get("type", "")
                if rtype not in NETWORK_KEEP_TYPES:
                    return
                request_id = event.get("requestId")
                req = event.get("request", {})
                # Header sensibles (Cookie, Authorization) -> redact
                headers = dict(req.get("headers", {}))
                for sensitive_header in ("Cookie", "cookie", "Authorization", "authorization"):
                    if sensitive_header in headers:
                        headers[sensitive_header] = "<redacted>"
                entry = {
                    "requestId": request_id,
                    "type": rtype,
                    "method": req.get("method"),
                    "url": req.get("url"),
                    "timestamp": event.get("timestamp"),
                    "wallTime": event.get("wallTime"),
                    "headers": headers,
                    "postData": (req.get("postData") or "")[:1000] if req.get("postData") else None,
                    "status": None,
                    "statusText": None,
                    "responseType": None,
                    "duration_ms": None,
                }
                _net_index[request_id] = len(network_log)
                network_log.append(entry)

            def on_response(event):
                request_id = event.get("requestId")
                idx = _net_index.get(request_id)
                if idx is None:
                    return
                resp = event.get("response", {})
                network_log[idx]["status"] = resp.get("status")
                network_log[idx]["statusText"] = resp.get("statusText")
                network_log[idx]["responseType"] = resp.get("mimeType")

            def on_finished(event):
                request_id = event.get("requestId")
                idx = _net_index.get(request_id)
                if idx is None:
                    return
                start = network_log[idx].get("timestamp") or 0
                end = event.get("timestamp") or 0
                if start and end:
                    network_log[idx]["duration_ms"] = round((end - start) * 1000, 1)

            page_cdp.on("Runtime.exceptionThrown", on_exception)
            page_cdp.on("Console.messageAdded", on_console)
            page_cdp.on("Network.requestWillBeSent", on_request)
            page_cdp.on("Network.responseReceived", on_response)
            page_cdp.on("Network.loadingFinished", on_finished)
            await page_cdp.send("Performance.enable")
            # PIEGE : DOM domain doit etre enable, et on doit demander getDocument()
            # pour amorcer le tracking des nodes. Sans ca, les events ne firent pas.
            try:
                import time as _time
                _t0 = _time.monotonic()
                def _now_ms():
                    return int((_time.monotonic() - _t0) * 1000)

                def on_attr_modified(_):
                    dom_mutations["attribute_modified"] += 1
                    if dom_mutations["first_mutation_ms"] is None:
                        dom_mutations["first_mutation_ms"] = _now_ms()
                    dom_mutations["last_mutation_ms"] = _now_ms()
                def on_child_inserted(_):
                    dom_mutations["child_node_inserted"] += 1
                    if dom_mutations["first_mutation_ms"] is None:
                        dom_mutations["first_mutation_ms"] = _now_ms()
                    dom_mutations["last_mutation_ms"] = _now_ms()
                def on_child_removed(_):
                    dom_mutations["child_node_removed"] += 1
                    dom_mutations["last_mutation_ms"] = _now_ms()

                await page_cdp.send("DOM.enable")
                page_cdp.on("DOM.attributeModified", on_attr_modified)
                page_cdp.on("DOM.childNodeInserted", on_child_inserted)
                page_cdp.on("DOM.childNodeRemoved", on_child_removed)
            except Exception as dom_e:
                print(f"  [WARN] DOM mutations capture echouee : {dom_e}")
            # PIEGE 2 : Coverage doit etre active AVANT toute navigation, sinon le bundle
            # initial est marque comme non-execute et les % sortent faussement bas.
            # On le fait ici, avant que browser-use ait fait son premier goto.
            coverage_enabled = False
            try:
                await page_cdp.send("Profiler.enable")
                await page_cdp.send("Profiler.startPreciseCoverage", {
                    "callCount": False,
                    "detailed": True,
                    "allowTriggeredUpdates": False,
                })
                coverage_enabled = True
            except Exception as cov_e:
                print(f"  [WARN] Coverage.startPreciseCoverage echouee : {cov_e}")
            cov_str = " + Coverage" if coverage_enabled else ""
            print(f"  Capture observabilite : Runtime + Console + Network + Performance{cov_str} (filtre {','.join(NETWORK_KEEP_TYPES)})")
        except Exception as e:
            print(f"  [WARN] Capture observabilite echouee : {e}")
            page_cdp = None
            coverage_enabled = False

        # -- ETAPE 3 : Lancer browser-use via CDP --
        print_header("LANCEMENT BROWSER-USE")
        print(f"  Timing : min_wait={min_wait}s max_wait={max_wait}s network_idle={network_idle}s max_steps={max_steps}")
        print(f"  Provider: {provider} -> {model}")
        llm_kwargs = {"model": model}
        if base_url:
            llm_kwargs["base_url"] = base_url
        if api_key:
            llm_kwargs["api_key"] = api_key
        # BU 0.13.8 default max_completion_tokens=4096 -> truncation frequente
        # sur les steps a normalized_actions verbeuses (parabank 12 champs,
        # todomvc 4 taches sequentielles). Passe a 16384 pour laisser place
        # au reasoning + JSON complet sans ModelOutputTruncatedError.
        try:
            llm_kwargs["max_completion_tokens"] = 16384
        except Exception:
            pass
        llm = ChatOpenAI(**llm_kwargs)

        # Preuves de selecteurs capturees AVANT chaque lot d'actions BU.
        # Le callback est declenche une fois le model_output connu, alors que
        # tous les backend_node_id du selector_map pointent encore vers des
        # noeuds attaches. Le cache sera rattache a l'historique final.
        live_selector_evidence: dict = {}
        live_selector_hook_errors: list[str] = []

        async def _capture_planned_selector_evidence(
            browser_state_summary,
            model_output,
            _step_number,
        ) -> None:
            try:
                await enrich_browser_use_step_snapshot(
                    browser_state_summary,
                    model_output,
                    context,
                    live_selector_evidence,
                )
            except Exception as exc:
                # L'observabilite ne doit jamais modifier le comportement de
                # l'agent. L'absence de preuve fera echouer la classification
                # fermee, et l'erreur restera visible dans meta.json.
                live_selector_hook_errors.append(f"{type(exc).__name__}: {exc}"[:300])

        # Construire le BrowserProfile (browser-use 0.12+) qui porte les params de timing
        # Avant 0.12 : kwargs directement sur BrowserSession. Apres : via BrowserProfile.
        browser_session = None
        profile_applied = False

        # Tentative 1 : via BrowserProfile (browser-use 0.12+)
        BrowserProfile = None
        for import_path in ("browser_use", "browser_use.browser.profile", "browser_use.browser"):
            try:
                module = __import__(import_path, fromlist=["BrowserProfile"])
                BrowserProfile = getattr(module, "BrowserProfile", None)
                if BrowserProfile:
                    break
            except Exception:
                continue

        if BrowserProfile:
            try:
                profile = BrowserProfile(
                    minimum_wait_page_load_time=min_wait,
                    maximum_wait_page_load_time=max_wait,
                    wait_for_network_idle_page_load_time=network_idle,
                )
                browser_session = BrowserSession(
                    cdp_url=f"http://localhost:{cdp_port}",
                    browser_profile=profile,
                )
                profile_applied = True
                print(f"  [OK] BrowserProfile applique : min={min_wait}s max={max_wait}s idle={network_idle}s")
            except (TypeError, ValueError) as e:
                print(f"  [WARN] BrowserProfile rejete ({e}), tentative kwargs directs...")

        # Tentative 2 : kwargs directs sur BrowserSession (versions plus anciennes)
        if browser_session is None:
            try:
                browser_session = BrowserSession(
                    cdp_url=f"http://localhost:{cdp_port}",
                    minimum_wait_page_load_time=min_wait,
                    maximum_wait_page_load_time=max_wait,
                    wait_for_network_idle_page_load_time=network_idle,
                )
                profile_applied = True
                print(f"  [OK] BrowserSession kwargs directs appliques : min={min_wait}s max={max_wait}s")
            except TypeError as e:
                print(f"  [WARN] Aucun mecanisme de timing supporte ({e}), defauts browser-use utilises (0.5s/2s)")
                browser_session = BrowserSession(cdp_url=f"http://localhost:{cdp_port}")

        if not profile_applied:
            print(f"  [INFO] Les SPA lourdes (DuckDuckGo, Twitter...) risquent d'echouer sans timing custom")

        # Mode BENCH : reproduit EXACTEMENT la configuration Agent du
        # runner officiel browser-use/benchmark (frameworks/browser_use/
        # run_task.py). Diffs cle : Browser(cdp_url=...) au lieu de
        # BrowserSession, use_judge=False, AUCUN override step_timeout /
        # llm_timeout / max_actions_per_step / max_steps (defaults BU).
        # Sans ca, on handicape l'agent vs le run officiel et on obtient
        # un taux de reussite artificiellement bas.
        bench_mode = bool((timing_opts or {}).get("bench_mode", False))
        # Interdiction par defaut de l'action `evaluate` de BU : elle permet
        # au LLM d'executer du JS arbitraire (`.value=`, `.click()`, etc.)
        # qui contourne isTrusted, actionabilite et visibility, et ne peut
        # pas etre canonicalise proprement en TS Playwright. On la retire
        # du registre via Tools(exclude_actions=...) - BU ne la propose plus
        # au LLM. Un flag ``allow_evaluate=True`` permet de la reactiver en
        # mode diagnostic (replay marque non canonique dans ce cas).
        # Doc BU : https://docs.browser-use.com/customize/tools/remove
        allow_evaluate = bool((timing_opts or {}).get("allow_evaluate", False))
        _bu_tools = None
        if not allow_evaluate:
            try:
                from browser_use import Tools as _BUTools
                _bu_tools = _BUTools(exclude_actions=["evaluate"])
                print("  [BU] action `evaluate` retiree du registre (Tools.exclude_actions)")
            except Exception as _e:
                print(f"  [BU] Impossible de retirer `evaluate` (BU trop ancien ?): {_e}")
                _bu_tools = None
        if bench_mode:
            from browser_use import Browser as _BUBrowser
            bench_browser = _BUBrowser(cdp_url=f"http://localhost:{cdp_port}")
            agent_kwargs = {
                "task": task,
                "llm": llm,
                "browser": bench_browser,
                "use_vision": use_vision,
                "use_judge": False,
                "max_actions_per_step": 4,
                "llm_timeout": 300,
                "register_new_step_callback": _capture_planned_selector_evidence,
            }
            print(f"  [BENCH_MODE] Agent = Browser(cdp_url) + use_judge=False + max_actions_per_step=4 + llm_timeout=300s")
        else:
            agent_kwargs = {
                "task": task,
                "llm": llm,
                "browser_session": browser_session,
                "use_vision": use_vision,
                "max_actions_per_step": 10,
                "register_new_step_callback": _capture_planned_selector_evidence,
            }
            step_timeout_override = (timing_opts or {}).get("step_timeout")
            llm_timeout_override = (timing_opts or {}).get("llm_timeout")
            if step_timeout_override:
                agent_kwargs["step_timeout"] = int(step_timeout_override)
            if llm_timeout_override:
                agent_kwargs["llm_timeout"] = int(llm_timeout_override)
        if _bu_tools is not None:
            agent_kwargs["tools"] = _bu_tools
        try:
            agent = Agent(**agent_kwargs)
        except TypeError:
            # BU plus ancien : retire les overrides puis tools puis use_vision
            for k in ("step_timeout", "llm_timeout", "use_judge"):
                agent_kwargs.pop(k, None)
            try:
                agent = Agent(**agent_kwargs)
            except TypeError:
                agent_kwargs.pop("tools", None)
                try:
                    agent = Agent(**agent_kwargs)
                except TypeError:
                    agent_kwargs.pop("use_vision", None)
                    agent = Agent(**agent_kwargs)

        # -- Snapshot Performance AVANT le run (V3 phase 4) --
        perf_before = {}
        perf_after = {}
        if 'page_cdp' in locals() and page_cdp:
            try:
                resp = await page_cdp.send("Performance.getMetrics")
                perf_before = {m["name"]: m["value"] for m in resp.get("metrics", [])}
            except Exception as e:
                print(f"  [WARN] Performance.getMetrics avant : {e}")

        # BENCH : aucun max_steps impose, comme le runner BU officiel. Mode
        # standard : on garde max_steps comme garde-fou anti-boucle.
        import time as _t
        _phase_agent_start = _t.monotonic()
        if bench_mode:
            result = await agent.run()
        else:
            try:
                result = await agent.run(max_steps=max_steps)
            except TypeError:
                result = await agent.run()
        _phase_agent_end = _t.monotonic()

        # Publication immediate agent_done.json (le browser reste vivant
        # pour les etapes 3-7 qui ont besoin du DOM/CDP - la fermeture
        # precoce se fait plus bas, juste avant build_clean_steps).
        try:
            _agent_status = "unknown"
            _agent_result_str = ""
            try:
                _agent_result_str = str(result.final_result() or "")[:2000]
                _agent_status = _classify_agent_verdict(_agent_result_str)
            except Exception:
                pass
            _agent_done = {
                "agent_status": _agent_status,
                "agent_result": _agent_result_str,
                "agent_duration_s": round(_phase_agent_end - _phase_agent_start, 2),
                "ts_agent_done_monotonic": _phase_agent_end,
                "num_steps": (result.number_of_steps() if hasattr(result, "number_of_steps") else None),
            }
            _tmp = output_dir / "agent_done.json.tmp"
            _fin = output_dir / "agent_done.json"
            _tmp.write_text(json.dumps(_agent_done, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(_tmp, _fin)
            print(f"  [PHASE agent] done in {_agent_done['agent_duration_s']}s ({_agent_status})")
        except Exception as _e:
            print(f"  [WARN] agent_done.json ecriture echouee : {_e}")

        # -- Snapshot Performance APRES le run --
        if 'page_cdp' in locals() and page_cdp:
            try:
                resp = await page_cdp.send("Performance.getMetrics")
                perf_after = {m["name"]: m["value"] for m in resp.get("metrics", [])}
            except Exception as e:
                print(f"  [WARN] Performance.getMetrics apres : {e}")

        # -- Take Coverage APRES le run (V3 phase 5) --
        coverage_summary = None
        if 'page_cdp' in locals() and page_cdp and 'coverage_enabled' in locals() and coverage_enabled:
            try:
                cov_resp = await page_cdp.send("Profiler.takePreciseCoverage")
                # Calcule % couverture par script
                scripts = cov_resp.get("result", [])
                summary = []
                total_used = 0
                total_size = 0
                for s in scripts:
                    url = s.get("url", "")
                    # Skip extensions chrome:// et about:blank
                    if not url or url.startswith(("chrome://", "chrome-extension://", "about:")):
                        continue
                    used = 0
                    size = 0
                    for fn in s.get("functions", []):
                        for r in fn.get("ranges", []):
                            length = r.get("endOffset", 0) - r.get("startOffset", 0)
                            size += length
                            if r.get("count", 0) > 0:
                                used += length
                    if size > 0:
                        summary.append({
                            "url": url[:200],
                            "size": size,
                            "used": used,
                            "pct": round(used * 100 / size, 1),
                        })
                        total_used += used
                        total_size += size
                summary.sort(key=lambda x: -x["size"])
                coverage_summary = {
                    "total_size": total_size,
                    "total_used": total_used,
                    "total_pct": round(total_used * 100 / total_size, 1) if total_size else 0,
                    "scripts": summary[:50],  # top 50 par taille
                }
                await page_cdp.send("Profiler.stopPreciseCoverage")
                print(f"  Coverage : {coverage_summary['total_pct']}% du JS execute ({total_used // 1024} KB / {total_size // 1024} KB sur {len(summary)} scripts)")
            except Exception as e:
                print(f"  [WARN] Profiler.takePreciseCoverage : {e}")

        # -- ETAPE 4 : Recuperer le log brut --
        print_header("RECUPERATION DES LOCATEURS")
        raw_log = []

        # Methode 1 : Via le contexte browser-use (CDP)
        try:
            bu_context = agent.browser_session.context
            if bu_context:
                for p in bu_context.pages:
                    try:
                        log = await p.evaluate(
                            "JSON.parse(localStorage.getItem('__qaLocatorLog') || '[]')"
                        )
                        if log:
                            raw_log.extend(log)
                            print(f"  {len(log)} entrees via contexte browser-use (page: {p.url[:60]})")
                    except Exception as e:
                        print(f"  Page browser-use inaccessible: {e}")
                        continue
        except Exception as e:
            print(f"  Contexte browser-use non accessible: {e}")

        # Methode 2 : Via notre contexte Playwright original (fallback)
        if len(raw_log) == 0:
            print("  Fallback: tentative via contexte Playwright original...")
            try:
                for p in context.pages:
                    try:
                        log = await p.evaluate(
                            "JSON.parse(localStorage.getItem('__qaLocatorLog') || '[]')"
                        )
                        if log:
                            raw_log.extend(log)
                            print(f"  {len(log)} entrees via Playwright (page: {p.url[:60]})")
                    except Exception:
                        continue
            except Exception as e:
                print(f"  Contexte Playwright non accessible: {e}")

        # Methode 3 : Via CDP direct (dernier recours)
        if len(raw_log) == 0:
            print("  Fallback 2: tentative via CDP direct...")
            try:
                cdp = await browser.new_browser_cdp_session()
                targets = await cdp.send("Target.getTargets")
                for target in targets.get("targetInfos", []):
                    if target.get("type") == "page":
                        print(f"  CDP target trouve: {target.get('url', 'N/A')[:60]}")
            except Exception as e:
                print(f"  CDP direct echoue: {e}")

        if len(raw_log) == 0:
            print("  AUCUNE ENTREE CAPTUREE - Le listener n'a peut-etre pas ete injecte dans le bon contexte")
            print("  Astuce: verifier que add_init_script est applique au contexte utilise par browser-use")

        # Dedupliquer par timestamp (si plusieurs pages ont capture la meme chose)
        seen = set()
        unique_log = []
        for entry in raw_log:
            ts = entry.get('timestamp')
            if ts not in seen:
                seen.add(ts)
                unique_log.append(entry)
        raw_log = sorted(unique_log, key=lambda x: x.get('timestamp', 0))

        print(f"  {len(raw_log)} entrees brutes recuperees")

        # Sauvegarder le log brut (dans le dossier du run)
        raw_file = output_dir / "locator_log.json"
        raw_file.write_text(json.dumps(raw_log, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Log brut -> {raw_file}")

        # -- ETAPE 5 : Dedupliquer les inputs --
        deduped = dedup_log(raw_log)
        print(f"  {len(raw_log)} -> {len(deduped)} apres deduplication")

        dedup_file = output_dir / "locator_dedup.json"
        dedup_file.write_text(json.dumps(deduped, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Log deduplique -> {dedup_file}")

        # -- ETAPE 6 : Afficher le resultat agent --
        print_header("RESULTAT AGENT")
        print(f"  {result.final_result()}")

        # -- ETAPE 7 : Afficher le log deduplique --
        print_raw_log(deduped)

        # -- ETAPE 7bis : Extraire l'historique complet browser-use --
        # Sans ca on ne connait que result.final_result() (string plate) et on
        # perd les actions non captees par le DOM listener (navigate, wait,
        # keyboard, upload, tabs, go_back, ...).
        bu_history = extract_browser_use_history(agent, result)
        selector_enrichment_stats = {"elements": 0, "resolved": 0, "unique": 0, "unresolved": 0}
        try:
            enrichment_context = bu_context if 'bu_context' in locals() and bu_context else context
            selector_enrichment_stats = await enrich_browser_use_history_selectors(
                bu_history,
                enrichment_context,
                evidence_cache=live_selector_evidence,
            )
            print(
                "  Preuves selecteurs live : "
                f"{selector_enrichment_stats['unique']} elements avec candidat unique / "
                f"{selector_enrichment_stats['elements']} observes"
            )
            if live_selector_hook_errors:
                print(
                    "  [WARN] Collecte pre-action : "
                    f"{len(live_selector_hook_errors)} erreur(s), premiere : "
                    f"{live_selector_hook_errors[0]}"
                )
        except Exception as e:
            print(f"  [WARN] Enrichissement live des selecteurs echoue : {e}")
        bu_history_file = output_dir / "browser_use_history.json"
        try:
            bu_history_file.write_text(
                json.dumps(bu_history, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"  Historique browser-use ({len(bu_history)} steps) -> {bu_history_file}")
        except Exception as e:
            print(f"  [WARN] Ecriture browser_use_history.json echouee : {e}")

        # -- ETAPE 8 : Construction du clean_steps.json enrichi (schema v2.0) --
        # Nouveau pipeline unifie : fusion multi-sources (scenario + BU history +
        # DOM listener + network) -> classification locale stricte ->
        # validation Pydantic. Aucun LLM n'intervient apres Browser Use.
        clean_steps = None
        sensitive_env_vars: list[str] = []
        if len(deduped) > 0 or bu_history:
            # -- FERMETURE CHROMIUM PRECOCE : libere le slot navigateur AVANT
            # le post-processing (fusion, classify, TS, rapport). Le worker
            # Python reste vivant pour finir les artefacts, mais Chromium
            # est tue net -> pas d'accumulation de Chromium orphelins
            # pendant le post-processing multi-secondes. Cross-platform
            # via psutil : browser.close() de Playwright ne propage pas
            # toujours le kill au sous-process Chromium (bug connu Windows +
            # peut arriver sur Linux si le node driver meurt sans propager
            # SIGTERM). psutil.Process(pid).children.kill() fonctionne
            # identiquement Linux/macOS/Windows.
            _phase_close_start = _t.monotonic()
            try:
                _chromium_pid = None
                _proc_attr = getattr(browser, "process", None)
                if _proc_attr is not None:
                    _chromium_pid = getattr(_proc_attr, "pid", None)
                try:
                    if hasattr(agent, "browser_session") and agent.browser_session:
                        await agent.browser_session.close()
                except Exception:
                    pass
                try:
                    await browser.close()
                except Exception:
                    pass
                if _chromium_pid:
                    try:
                        import psutil as _ps
                        _p = _ps.Process(_chromium_pid)
                        for _c in _p.children(recursive=True):
                            try: _c.kill()
                            except Exception: pass
                        try: _p.kill()
                        except Exception: pass
                    except Exception:
                        pass
            except Exception as _close_e:
                print(f"  [WARN] fermeture Chromium precoce echouee : {_close_e}")
            _phase_close_end = _t.monotonic()
            print(f"  [PHASE close] Chromium ferme en {round(_phase_close_end - _phase_close_start, 2)}s")
            _phase_post_start = _t.monotonic()

            # Post-processing 100% local et deterministe, en bench comme en
            # mode standard. Browser Use reste le seul consommateur de LLM.
            clean_steps, sensitive_env_vars = build_clean_steps(
                scenario_name=scenario_name,
                scenario_url=scenario_url,
                scenario_steps=scenario_steps,
                bu_history=bu_history,
                dom_log=deduped,
                network_log=network_log if 'network_log' in locals() else None,
                model=model,
                base_url=base_url,
                api_key=api_key,
                use_llm=False,
                detect_sensitive=(not bench_mode),
            )

            clean_file = output_dir / "clean_steps.json"
            clean_file.write_text(
                clean_steps.model_dump_json(indent=2, exclude_none=True),
                encoding="utf-8",
            )
            print(f"\n  Parcours nettoye (schema {CURRENT_SCHEMA_VERSION}) -> {clean_file}")
            included = sum(1 for s in clean_steps.steps if s.included_in_replay)
            skipped = len(clean_steps.steps) - included
            print(f"  Steps : {len(clean_steps.steps)} total, {included} rejouables, {skipped} filtres")
            if sensitive_env_vars:
                _bar = "=" * 72
                # Retro-compat : accepte list[str] (ancien format) OU list[dict]
                # (nouveau format avec context : step, selector, page, description).
                _rows = []
                for _v in sensitive_env_vars:
                    if isinstance(_v, str):
                        _rows.append({"name": _v, "selector": "?", "page": "?", "description": ""})
                    elif isinstance(_v, dict):
                        _rows.append(_v)
                print(f"\n{_bar}")
                print("  ACTION REQUISE : variables d'environnement a positionner avant replay")
                print(f"{_bar}")
                print(f"  DOMAutopsy a detecte {len(_rows)} champ(s) sensible(s) dans le parcours")
                print("  (password, secret, token, api_key, credit_card, cvv, ssn, ...).")
                print("")
                print("  Les valeurs saisies pendant la capture ont ete REMPLACEES dans")
                print("  test_playwright.spec.ts par des lectures d'environnement. Le TS")
                print("  refuse de rejouer si ces variables ne sont pas positionnees.")
                print("")
                print("  --- Ce qu'il faut faire ---")
                print("")
                print("  Dans le TS il y a exactement ces reads a satisfaire :")
                for _r in _rows:
                    print(f"    process.env.{_r['name']}   ({_r.get('selector','?')} - {_r.get('page','?')[:60]})")
                print("")
                print("  Positionne chaque variable AVANT de lancer 'npx playwright test' :")
                print("")
                print("  Bash / macOS / Linux :")
                for _r in _rows:
                    print(f'    export {_r["name"]}="<valeur_pour_{_r.get("selector","?").lstrip("#.")}>"')
                print("")
                print("  PowerShell :")
                for _r in _rows:
                    print(f'    $env:{_r["name"]} = "<valeur_pour_{_r.get("selector","?").lstrip("#.")}>"')
                print("")
                print("  cmd.exe :")
                for _r in _rows:
                    print(f'    set {_r["name"]}=<valeur_pour_{_r.get("selector","?").lstrip("#.")}>')
                print("")
                print("  Puis lancer :  npx playwright test test_playwright.spec.ts")
                print(f"{_bar}\n")

            # -- ETAPE 9a : Generer TOUJOURS test_playwright.spec.ts (canonique) --
            # C'est le format interne utilise par /api/replay/{run_id}.
            # oracle transmis via timing_opts (corpus Real-World 7) : injecte
            # une assertion finale expect().toContainText / toHaveURL au TS.
            spec_path = output_dir / "test_playwright.spec.ts"
            gen_result = generate_playwright_ts(
                clean_steps=clean_steps,
                output_path=spec_path,
                parcours_url=scenario_url,
                oracle=(timing_opts or {}).get("oracle"),
            )
            print(f"  test_playwright.spec.ts -> {spec_path}")
            print(f"    Steps traduits : {gen_result['included_count']}, "
                  f"skipped : {gen_result['skipped_count']}, "
                  f"non traduisibles : {len(gen_result['unsupported'])}")
            if gen_result["unsupported"]:
                for u in gen_result["unsupported"]:
                    print(f"    [ATTENTION] {u['step_id']} ({u['action']}) : {u['reason']}")

            # -- ETAPE 9b : Si le format demande n'est pas playwright, generer
            # aussi l'export livrable (Katalon/Cypress/Selenium) DETERMINISTIQUEMENT
            # (fix R5 : ancien code utilisait le LLM qui pouvait reordonner /
            # ajouter / supprimer des actions - ex: 74 Enter alors que le
            # JSON n'en contient que 46). Nouveau : les exporters partent
            # strictement de clean_steps.json et produisent 1 statement par
            # step included_in_replay=True. Validation automatique en sortie.
            if output_format != "playwright":
                from deterministic_exporters import (
                    EXPORTERS, validate_export_counts, validate_export_by_action_type,
                )
                exporter = EXPORTERS.get(output_format)
                if exporter is not None:
                    export_code = exporter(clean_steps)
                    fmt_info = OUTPUT_FORMATS.get(output_format, OUTPUT_FORMATS["katalon"])
                    code_file = output_dir / f"test_{output_format}{fmt_info['extension']}"
                    code_file.write_text(export_code, encoding="utf-8")
                    print(f"  Code {fmt_info['label']} (export DETERMINISTE) -> {code_file}")

                    # Validation coherence export vs clean_steps :
                    # counts + ordre + fantomes + fuites, PUIS
                    # semantique par action_type (setText/click/etc.)
                    export_anomalies = (
                        validate_export_counts(clean_steps, export_code, output_format)
                        + validate_export_by_action_type(clean_steps, export_code, output_format)
                    )
                    if export_anomalies:
                        print(f"  [ATTENTION] {len(export_anomalies)} anomalies validation export {output_format} :")
                        for a in export_anomalies:
                            print(f"    - {a}")
                        # Ajoute aux anomalies globales du clean_steps + reecrit
                        clean_steps.anomalies.extend(export_anomalies)
                        clean_file.write_text(
                            clean_steps.model_dump_json(indent=2, exclude_none=True),
                            encoding="utf-8",
                        )
                else:
                    print(f"  [WARN] Aucun exporter deterministe pour '{output_format}'")

            # -- ETAPE 10 : Generer le rapport HTML --
            # On serialise clean_steps en dict pour rester compatible avec la
            # signature existante de report_generator (dict-based).
            clean_data_dict = clean_steps.model_dump(exclude_none=True)
            print_clean_steps(clean_data_dict)
            if HAS_REPORT:
                print_header("GENERATION DU RAPPORT")
                report_path = generate_report(
                    clean_data=clean_data_dict,
                    deduped_log=deduped,
                    agent_result=str(result.final_result()),
                    scenario_name=scenario_name,
                    scenario_url=scenario_url,
                    timestamp=timestamp,
                    output_dir=str(output_dir),
                    js_errors=js_errors if 'js_errors' in locals() else [],
                    console_messages=console_messages if 'console_messages' in locals() else [],
                    network_log=network_log if 'network_log' in locals() else [],
                    perf_before=perf_before if 'perf_before' in locals() else {},
                    perf_after=perf_after if 'perf_after' in locals() else {},
                    coverage_summary=coverage_summary if 'coverage_summary' in locals() else None,
                    dom_mutations=dom_mutations if 'dom_mutations' in locals() else {},
                )
                print(f"  Rapport HTML -> {report_path}")
                if should_open_report:
                    open_report(report_path)
                    print(f"  Rapport ouvert dans le navigateur")
                else:
                    print(f"  (ouverture auto desactivee, --no-open-report)")
            else:
                print("\n  report_generator.py absent, pas de rapport HTML")
        else:
            print("\n  Aucune entree capturee, aucun step a construire")

        # -- ETAPE 10bis : Sauvegarder les captures observabilite (V3 phases 1+2) --
        try:
            if 'js_errors' in locals():
                (output_dir / "js_errors.json").write_text(
                    json.dumps(js_errors, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                if js_errors:
                    print(f"\n  JS Errors silencieux captures : {len(js_errors)} -> js_errors.json")
            if 'console_messages' in locals():
                (output_dir / "console_messages.json").write_text(
                    json.dumps(console_messages, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                console_warns = sum(1 for m in console_messages if m.get("level") == "warning")
                console_errs = sum(1 for m in console_messages if m.get("level") == "error")
                if console_messages:
                    print(f"  Console : {len(console_messages)} messages ({console_errs} errors, {console_warns} warnings)")
            if 'network_log' in locals():
                (output_dir / "network_log.json").write_text(
                    json.dumps(network_log, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                if network_log:
                    api_calls = sum(1 for r in network_log if r.get("type") in ("Fetch", "XHR"))
                    fail_calls = sum(1 for r in network_log if (r.get("status") or 0) >= 400)
                    print(f"  Network : {len(network_log)} requetes filtrees ({api_calls} API, {fail_calls} >=400)")
            if 'coverage_summary' in locals() and coverage_summary:
                (output_dir / "coverage.json").write_text(
                    json.dumps(coverage_summary, indent=2, ensure_ascii=False), encoding="utf-8"
                )
            if 'dom_mutations' in locals():
                total_mut = sum(v for k, v in dom_mutations.items() if isinstance(v, int) and not k.endswith("_ms"))
                if total_mut > 0:
                    (output_dir / "dom_mutations.json").write_text(
                        json.dumps(dom_mutations, indent=2, ensure_ascii=False), encoding="utf-8"
                    )
                    print(f"  DOM mutations : {dom_mutations['attribute_modified']} attr, {dom_mutations['child_node_inserted']} insert, {dom_mutations['child_node_removed']} remove")
            if 'perf_before' in locals() and 'perf_after' in locals():
                # Calcule delta sur les metriques cles
                perf_delta = {
                    k: round(perf_after.get(k, 0) - perf_before.get(k, 0), 2)
                    for k in perf_after.keys()
                }
                perf_report = {
                    "before": perf_before,
                    "after": perf_after,
                    "delta": perf_delta,
                }
                (output_dir / "performance.json").write_text(
                    json.dumps(perf_report, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                heap_delta_mb = round(perf_delta.get("JSHeapUsedSize", 0) / 1024 / 1024, 2)
                print(f"  Performance : heap delta {heap_delta_mb:+}MB, layouts={int(perf_delta.get('LayoutCount', 0))}, nodes={int(perf_delta.get('Nodes', 0))}")
        except Exception as e:
            print(f"  [WARN] Sauvegarde observabilite echouee : {e}")

        # -- ETAPE 11 : Ecrire meta.json (alimente l'historique du serveur web) --
        try:
            meta = {
                "timestamp": timestamp,
                "started_at": started_at,
                "ended_at": datetime.now().isoformat(),
                "scenario_name": scenario_name,
                "scenario_url": scenario_url,
                "task": task,
                "output_format": output_format,
                "provider": provider,
                "model": model,
                "headless": headless,
                "use_vision": use_vision,
                "agent_result": str(result.final_result()) if 'result' in locals() and result else None,
                "raw_count": len(raw_log),
                "deduped_count": len(deduped) if 'deduped' in locals() else 0,
                "report": f"qa_report_{timestamp}.html" if (output_dir / f"qa_report_{timestamp}.html").exists() else None,
                "js_errors_count": len(js_errors) if 'js_errors' in locals() else 0,
                "console_errors_count": sum(1 for m in console_messages if m.get("level") == "error") if 'console_messages' in locals() else 0,
                "console_warnings_count": sum(1 for m in console_messages if m.get("level") == "warning") if 'console_messages' in locals() else 0,
                "network_count": len(network_log) if 'network_log' in locals() else 0,
                "network_api_count": sum(1 for r in network_log if r.get("type") in ("Fetch", "XHR")) if 'network_log' in locals() else 0,
                "network_fail_count": sum(1 for r in network_log if (r.get("status") or 0) >= 400) if 'network_log' in locals() else 0,
                "perf_heap_delta_mb": round((perf_after.get("JSHeapUsedSize", 0) - perf_before.get("JSHeapUsedSize", 0)) / 1024 / 1024, 2) if 'perf_after' in locals() and 'perf_before' in locals() else None,
                "perf_layout_count": int(perf_after.get("LayoutCount", 0)) if 'perf_after' in locals() else None,
                "perf_nodes": int(perf_after.get("Nodes", 0)) if 'perf_after' in locals() else None,
                "coverage_pct": coverage_summary["total_pct"] if 'coverage_summary' in locals() and coverage_summary else None,
                "coverage_used_kb": coverage_summary["total_used"] // 1024 if 'coverage_summary' in locals() and coverage_summary else None,
                "coverage_total_kb": coverage_summary["total_size"] // 1024 if 'coverage_summary' in locals() and coverage_summary else None,
                "oracle": (timing_opts or {}).get("oracle"),
                "oracle_asserted": gen_result.get("oracle_asserted", False) if 'gen_result' in locals() else False,
                "dialog_log": dialog_log if 'dialog_log' in locals() else [],
                "dialog_count": len(dialog_log) if 'dialog_log' in locals() else 0,
                "dom_mutations_total": (dom_mutations["attribute_modified"] + dom_mutations["child_node_inserted"] + dom_mutations["child_node_removed"]) if 'dom_mutations' in locals() else 0,
                # Refactor Aout 2026 : pipeline unifie Playwright TS
                "schema_version": CURRENT_SCHEMA_VERSION,
                "bu_history_count": len(bu_history) if 'bu_history' in locals() else 0,
                "selector_enrichment": selector_enrichment_stats if 'selector_enrichment_stats' in locals() else {},
                "selector_enrichment_hook_errors": live_selector_hook_errors if 'live_selector_hook_errors' in locals() else [],
                "clean_steps_total": len(clean_steps.steps) if 'clean_steps' in locals() and clean_steps else 0,
                "clean_steps_included": sum(1 for s in clean_steps.steps if s.included_in_replay) if 'clean_steps' in locals() and clean_steps else 0,
                "clean_steps_filtered": sum(1 for s in clean_steps.steps if not s.included_in_replay) if 'clean_steps' in locals() and clean_steps else 0,
                "replay_blocking_steps": sum(1 for s in clean_steps.steps if s.replay_blocking) if 'clean_steps' in locals() and clean_steps else 0,
                "sensitive_env_vars": sensitive_env_vars if 'sensitive_env_vars' in locals() else [],
                "playwright_spec_present": (output_dir / "test_playwright.spec.ts").exists(),
                "playwright_unsupported_count": len(gen_result.get("unsupported", [])) if 'gen_result' in locals() else 0,
            }
            # R6 : statuts DISTINCTS agent vs pipeline (review round 2).
            # agent_status : verdict fonctionnel de l'agent BU (a-t-il
            #   reussi le scenario ? ex: SUCCESS/FAIL/INTERRUPTED dans
            #   final_result). Un agent qui retourne FAIL ne doit JAMAIS
            #   apparaitre comme success meme si le pipeline a bien tourne.
            # pipeline_status : capture + generation artifacts ont-elles
            #   reussi ? On est arrive jusqu'ici sans exception non-catche,
            #   donc "success" (les erreurs partielles sont dans warnings).
            # status : overall = worst(agent, pipeline)
            agent_status = _classify_agent_verdict(str(meta.get("agent_result") or ""))
            pipeline_status = "success"
            if agent_status == "fail":
                overall = "failure"
            elif agent_status == "interrupted":
                overall = "interrupted"
            else:
                overall = "success"
            meta["agent_status"] = agent_status
            meta["pipeline_status"] = pipeline_status
            meta["status"] = overall
            (output_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            print(f"  [WARN] Impossible d'ecrire meta.json: {e}")

    finally:
        # -- CLEANUP : garantit la fermeture meme en cas d'exception --
        print_header("FERMETURE")
        try:
            await browser.close()
        except Exception as e:
            print(f"  Erreur fermeture browser: {e}")
        await pw.stop()
        # Chronometrage phase post-processing (utile pour benchmark : sait
        # exactement ou va le temps entre agent.run() et fin worker).
        try:
            import time as _t_end
            if '_phase_post_start' in locals():
                _post_dur = round(_t_end.monotonic() - _phase_post_start, 2)
                print(f"  [PHASE post] post-processing termine en {_post_dur}s")
                # Enrichit agent_done.json avec le breakdown final si present
                if 'output_dir' in locals():
                    _final = output_dir / "agent_done.json"
                    if _final.exists():
                        try:
                            _data = json.loads(_final.read_text(encoding="utf-8"))
                            _data["post_processing_duration_s"] = _post_dur
                            if '_phase_close_start' in locals() and '_phase_close_end' in locals():
                                _data["close_chromium_duration_s"] = round(_phase_close_end - _phase_close_start, 2)
                            _final.write_text(json.dumps(_data, ensure_ascii=False, indent=2), encoding="utf-8")
                        except Exception:
                            pass
        except Exception:
            pass
        print("  Termine !")


if __name__ == "__main__":
    _patch_browser_use()
    task, model, port, scenario_name, scenario_url, scenario_steps, timing_opts = resolve_task()
    asyncio.run(run(task, model, port, scenario_name, scenario_url, scenario_steps, timing_opts))
