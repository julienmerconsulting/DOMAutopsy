"""
QA Explorer - Browser-Use + DOM Listener + AI Cleanup
=====================================================
Par Julien Mer / JMer Consulting - POC Browser-Use -> Katalon

Pipeline :
  1. Lance Chromium via Playwright avec CDP (port 9222)
  2. Injecte un DOM listener JS (capture clics + inputs dans localStorage)
  3. browser-use se connecte au meme Chromium via CDP et execute le parcours
  4. Recupere le log brut des locateurs captures
  5. Deduplique les inputs (garde la derniere valeur par champ)
  6. Envoie a GPT-4.1-mini pour nettoyer/ordonner les steps
  7. Sauvegarde : locator_log.json (brut) + clean_steps.json (nettoye)

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
  
Variables d'environnement :
  OPENAI_API_KEY=sk-...
"""

from browser_use import Agent, BrowserSession, ChatOpenAI
from playwright.async_api import async_playwright
from openai import OpenAI
import asyncio
import json
import os
import sys
import argparse
from datetime import datetime

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
    """Patch la methode buggee de browser-use pour survivre aux iframes detruites"""
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

    except ImportError:
        print("  [PATCH] browser-use pas installe, patch ignore")
    except AttributeError:
        print("  [PATCH] API browser-use modifiee, patch non applique")

_patch_browser_use()


# ============================================================
# CONFIGURATION
# ============================================================

LLM_MODEL = "gpt-4.1-mini"
CDP_PORT = 9222

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
        "--model", default=LLM_MODEL,
        help=f"Modele LLM (defaut: {LLM_MODEL})"
    )
    parser.add_argument(
        "--port", type=int, default=CDP_PORT,
        help=f"Port CDP Chromium (defaut: {CDP_PORT})"
    )

    args = parser.parse_args()

    # Mode 1 : Fichier JSON du Scenario Builder
    if args.scenario_file:
        if not os.path.exists(args.scenario_file):
            print(f"  Fichier introuvable : {args.scenario_file}")
            sys.exit(1)
        with open(args.scenario_file, "r", encoding="utf-8") as f:
            scenario = json.load(f)
        task = build_task_from_json(args.scenario_file)
        return task, args.model, args.port, scenario.get("name", "Scenario"), scenario.get("url", ""), scenario.get("steps", [])

    # Mode 2 : --url + --task en ligne de commande
    if args.url and args.task:
        task = f"""Va sur {args.url}

IMPORTANT : Si un element n'est pas visible ou cliquable, scroll pour le trouver. Si un popup de cookies apparait, accepte-le d'abord.

{args.task}

Retourne SUCCESS si tout s'est bien passe, FAIL avec la raison sinon."""
        print_header("MODE LIGNE DE COMMANDE")
        print(f"  URL  : {args.url}")
        print(f"  Task : {args.task}")
        return task, args.model, args.port, "CLI", args.url, []

    # Mode 3 : TASK par defaut (legacy)
    print_header("MODE LEGACY (TASK PAR DEFAUT)")
    print("  Aucun scenario JSON ni --url/--task fourni")
    print("  Utilisation du TASK hardcode")
    return DEFAULT_TASK, args.model, args.port, "Legacy", "", []


# ============================================================
# DOM LISTENER (injecte dans le navigateur)
# ============================================================

DOM_LISTENER_JS = """
(function() {
    if (window.__qaListenerInstalled) return;
    window.__qaListenerInstalled = true;

    // -- SAUVEGARDE --
    function saveEntry(entry) {
        try {
            let log = JSON.parse(localStorage.getItem('__qaLocatorLog') || '[]');
            log.push(entry);
            localStorage.setItem('__qaLocatorLog', JSON.stringify(log));
        } catch(e) {}
    }

    // -- DETECTION SHADOW DOM --
    function isInShadowDOM(el) {
        let root = el.getRootNode();
        return root instanceof ShadowRoot;
    }

    // -- SELECTEUR SHADOW DOM (chaine) --
    function getShadowSelector(el) {
        let chain = [];
        let current = el;
        while (current) {
            let root = current.getRootNode();
            let localSel = getQuickSelector(current);
            if (root instanceof ShadowRoot) {
                chain.unshift({selector: localSel, shadow: true});
                current = root.host;
            } else {
                chain.unshift({selector: localSel, shadow: false});
                break;
            }
        }
        let pwSel = chain.map(c => c.selector).join(' >>> ');
        let jsSel = chain.map((c, i) => {
            if (i === 0) return 'document.querySelector("' + c.selector + '")';
            return '.shadowRoot.querySelector("' + c.selector + '")';
        }).join('');
        return {
            strategy: 'shadow',
            value: pwSel,
            inShadowDOM: true,
            shadowChain: chain,
            playwrightSelector: pwSel,
            jsSelector: jsSel,
            unique: true,
            matchCount: 1
        };
    }

    // -- SELECTEUR RAPIDE (pour shadow chain ou parent lookup) --
    function getQuickSelector(el) {
        if (!el || !el.getAttribute) return 'unknown';
        if (el.getAttribute('data-testid')) return '[data-testid="' + el.getAttribute('data-testid') + '"]';
        if (el.id && !el.id.match(/^[0-9]|react|ember|__/)) return '#' + el.id;
        if (el.getAttribute('name')) return el.tagName.toLowerCase() + '[name="' + el.getAttribute('name') + '"]';
        if (el.getAttribute('aria-label')) return '[aria-label="' + el.getAttribute('aria-label') + '"]';
        let sel = el.tagName.toLowerCase();
        if (el.className && typeof el.className === 'string') {
            let cls = el.className.trim().split(/\\s+/).filter(c => c.length > 2 && c.length < 30).slice(0, 2);
            if (cls.length) sel += '.' + cls.join('.');
        }
        return sel;
    }

    // =========================================================
    // -- SELECTEUR INTELLIGENT : cascade stricte + validation --
    // =========================================================
    function getBestSelector(el) {
        if (!el || !el.getAttribute) return {strategy:'unknown', value:'unknown', inShadowDOM:false, unique:false, matchCount:0};

        // Shadow DOM -> logique separee
        if (isInShadowDOM(el)) return getShadowSelector(el);

        let best = null;
        let strategy = 'unknown';

        // === Tier 1 : Attributs stables (fiabilite max) ===
        if (el.getAttribute('data-testid')) {
            best = '[data-testid="' + el.getAttribute('data-testid') + '"]';
            strategy = 'data-testid';
        }
        else if (el.id && !el.id.match(/^[0-9]|react|ember|__|:/)) {
            best = '#' + el.id;
            strategy = 'id';
        }
        else if (el.getAttribute('name')) {
            best = el.tagName.toLowerCase() + '[name="' + el.getAttribute('name') + '"]';
            strategy = 'name';
        }

        // === Tier 2 : Attributs semantiques ===
        if (!best && el.getAttribute('aria-label')) {
            best = '[aria-label="' + el.getAttribute('aria-label') + '"]';
            strategy = 'aria-label';
        }
        if (!best && el.getAttribute('placeholder')) {
            best = el.tagName.toLowerCase() + '[placeholder="' + el.getAttribute('placeholder') + '"]';
            strategy = 'placeholder';
        }
        if (!best && el.getAttribute('title')) {
            best = '[title="' + el.getAttribute('title') + '"]';
            strategy = 'title';
        }

        // === Tier 3 : Href pour les liens ===
        if (!best && el.tagName === 'A' && el.getAttribute('href')) {
            let href = el.getAttribute('href');
            if (href !== '#' && href !== '/' && href !== 'javascript:void(0)' && href.length < 100) {
                best = 'a[href="' + href + '"]';
                strategy = 'href';
            }
        }

        // === Tier 4 : Remonter au parent avec attribut stable (icones, SVG) ===
        if (!best) {
            let parent = el.closest('[aria-label], [data-testid], [title]');
            if (parent && parent !== el) {
                if (parent.getAttribute('aria-label')) {
                    best = '[aria-label="' + parent.getAttribute('aria-label') + '"]';
                    strategy = 'parent-aria-label';
                } else if (parent.getAttribute('data-testid')) {
                    best = '[data-testid="' + parent.getAttribute('data-testid') + '"]';
                    strategy = 'parent-data-testid';
                } else if (parent.getAttribute('title')) {
                    best = '[title="' + parent.getAttribute('title') + '"]';
                    strategy = 'parent-title';
                }
            }
        }

        // === Tier 5 : Label associe (pour inputs) ===
        if (!best && (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA')) {
            let label = null;
            if (el.id) label = document.querySelector('label[for="' + el.id + '"]');
            if (!label) label = el.closest('label');
            if (label) {
                let labelText = (label.textContent || '').trim().substring(0, 40);
                if (labelText) {
                    best = '//label[contains(text(),"' + labelText + '")]//input';
                    strategy = 'label-xpath';
                }
            }
        }

        // === Tier 6 : CSS court + nth-of-type (dernier recours CSS) ===
        if (!best) {
            let sel = el.tagName.toLowerCase();
            if (el.className && typeof el.className === 'string') {
                let classes = el.className.trim().split(/\\s+/)
                    .filter(c => c.length > 2 && c.length < 30
                            && !c.match(/active|hover|focus|open|visible|show|selected|current/))
                    .slice(0, 2);
                if (classes.length) sel += '.' + classes.join('.');
            }
            if (el.parentElement) {
                let same = Array.from(el.parentElement.children).filter(s => s.tagName === el.tagName);
                if (same.length > 1) {
                    sel += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
                }
            }
            best = sel;
            strategy = 'css-short';
        }

        // === VALIDATION : est-ce que le selecteur matche 1 seul element ? ===
        let matchCount = 0;
        try {
            if (strategy === 'label-xpath' || best.startsWith('//')) {
                let xr = document.evaluate(best, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                matchCount = xr.snapshotLength;
            } else {
                matchCount = document.querySelectorAll(best).length;
            }
        } catch(e) { matchCount = -1; }

        // Si multi-match -> tenter XPath text-based
        let text = (el.innerText || el.textContent || '').trim();
        if (matchCount !== 1 && text && text.length > 0 && text.length < 50 && text.indexOf('\\n') === -1) {
            let xpathText = '//' + el.tagName.toLowerCase() + '[contains(text(),"' + text.replace(/"/g, "'") + '")]';
            let xpathCount = 0;
            try {
                let xr = document.evaluate(xpathText, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                xpathCount = xr.snapshotLength;
            } catch(e) {}
            if (xpathCount === 1) {
                best = xpathText;
                strategy = 'xpath-text';
                matchCount = 1;
            } else if (xpathCount > 0 && xpathCount < matchCount) {
                best = xpathText;
                strategy = 'xpath-text';
                matchCount = xpathCount;
            }
        }

        return {
            strategy: strategy,
            value: best,
            inShadowDOM: false,
            unique: matchCount === 1,
            matchCount: matchCount
        };
    }

    // -- RESOLUTION ELEMENT REEL (composedPath + bubble up) --
    function getRealTarget(e) {
        let el = e.target;
        if (e.composedPath && e.composedPath().length > 0) {
            el = e.composedPath()[0];
        }
        let interactiveTags = ['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'];
        let maxDepth = 5;
        let depth = 0;
        while (el && depth < maxDepth) {
            if (el.id || (el.getAttribute && el.getAttribute('data-testid')) || (el.getAttribute && el.getAttribute('name'))) break;
            if (interactiveTags.includes(el.tagName)) break;
            if (el.getAttribute && el.getAttribute('aria-label')) break;
            let parent = el.parentElement;
            if (!parent) break;
            if (interactiveTags.includes(parent.tagName) || parent.id
                || (parent.getAttribute && parent.getAttribute('data-testid'))
                || (parent.getAttribute && parent.getAttribute('aria-label'))) {
                el = parent;
                break;
            }
            el = parent;
            depth++;
        }
        return el;
    }

    // -- EVENT LISTENERS --
    document.addEventListener('click', (e) => {
        let el = getRealTarget(e);
        let sel = getBestSelector(el);
        let text = (el.innerText || el.textContent || '').trim().substring(0, 80);
        saveEntry({
            action: 'click',
            timestamp: Date.now(),
            tag: el.tagName,
            text: text,
            selector: sel,
            url: location.href,
            inShadowDOM: sel.inShadowDOM,
            attributes: {
                id: el.id || null,
                name: el.getAttribute ? el.getAttribute('name') : null,
                type: el.getAttribute ? el.getAttribute('type') : null,
                class: (typeof el.className === 'string') ? el.className : null,
                href: el.getAttribute ? el.getAttribute('href') : null,
                'data-testid': el.getAttribute ? el.getAttribute('data-testid') : null,
                'aria-label': el.getAttribute ? el.getAttribute('aria-label') : null,
                role: el.getAttribute ? el.getAttribute('role') : null
            }
        });
    }, true);

    document.addEventListener('input', (e) => {
        let el = getRealTarget(e);
        let sel = getBestSelector(el);
        saveEntry({
            action: 'input',
            timestamp: Date.now(),
            tag: el.tagName,
            value: el.value || '',
            selector: sel,
            url: location.href,
            inShadowDOM: sel.inShadowDOM,
            attributes: {
                id: el.id || null,
                name: el.getAttribute ? el.getAttribute('name') : null,
                type: el.getAttribute ? el.getAttribute('type') : null,
                placeholder: el.getAttribute ? el.getAttribute('placeholder') : null,
                'data-testid': el.getAttribute ? el.getAttribute('data-testid') : null,
                'aria-label': el.getAttribute ? el.getAttribute('aria-label') : null
            }
        });
    }, true);

    // -- SCROLL LISTENER (debounced) --
    let scrollTimer = null;
    let scrollStartY = window.scrollY;
    window.addEventListener('scroll', () => {
        if (!scrollTimer) {
            scrollStartY = window.scrollY;
        }
        clearTimeout(scrollTimer);
        scrollTimer = setTimeout(() => {
            let scrollEndY = window.scrollY;
            let delta = scrollEndY - scrollStartY;
            if (Math.abs(delta) > 50) {
                saveEntry({
                    action: 'scroll',
                    timestamp: Date.now(),
                    tag: 'WINDOW',
                    direction: delta > 0 ? 'down' : 'up',
                    deltaY: delta,
                    scrollY: scrollEndY,
                    viewport: {
                        width: window.innerWidth,
                        height: window.innerHeight,
                        docHeight: document.documentElement.scrollHeight
                    },
                    url: location.href,
                    selector: {strategy: 'window', value: 'window.scrollTo(0, ' + scrollEndY + ')', inShadowDOM: false, unique: true, matchCount: 1},
                    inShadowDOM: false,
                    attributes: {}
                });
            }
            scrollTimer = null;
        }, 250);
    }, true);

    console.log('[QA Listener V3] Installed on ' + location.href + ' (Smart selectors + Shadow DOM + Scroll)');
})();
"""


# ============================================================
# DEDUPLICATION
# ============================================================

def dedup_log(raw_log):
    """
    Deduplique le log brut :
    - Clics : supprime les clics consecutifs sur le meme selecteur (meme URL)
    - Scrolls : gardes tels quels
    - Inputs : garde uniquement la derniere valeur par selecteur + URL
    """
    clean = []
    last_input = {}

    for entry in raw_log:
        if entry['action'] == 'click':
            # Verifier si le clic precedent etait sur le meme selecteur + meme URL
            if clean:
                prev = clean[-1]
                if (prev['action'] == 'click' 
                    and prev['selector']['value'] == entry['selector']['value']
                    and prev.get('url', '') == entry.get('url', '')):
                    # Doublon consecutif, on skip
                    continue
            clean.append(entry)
        elif entry['action'] == 'scroll':
            clean.append(entry)
        elif entry['action'] == 'input':
            key = (entry['selector']['value'], entry.get('url', ''))
            if key in last_input:
                clean[last_input[key]] = entry
            else:
                last_input[key] = len(clean)
                clean.append(entry)

    return clean


# ============================================================
# NETTOYAGE IA
# ============================================================

def ai_cleanup(deduped_log, scenario_steps=None, model=LLM_MODEL):
    """
    Envoie le log deduplique a GPT-4.1-mini pour :
    - Reconstituer le parcours ideal dans l'ordre
    - Supprimer les actions en double / inutiles
    - Comparer avec le scenario attendu si fourni
    - Signaler les anomalies
    """
    client = OpenAI()

    # Bloc optionnel : scenario attendu pour comparaison
    scenario_block = ""
    if scenario_steps:
        scenario_block = f"""
SCENARIO ATTENDU (ce que l'utilisateur a demande) :
{json.dumps(scenario_steps, indent=2, ensure_ascii=False)}

REGLE IMPORTANTE :
- Compare chaque action capturee avec le scenario attendu
- Si une action capturee ne correspond a aucune etape du scenario (ex: clic sur un overlay,
  un modal, un conteneur div, un fond de page), c'est du BRUIT → supprime-la
- Si une etape du scenario n'a pas d'action correspondante dans le log, signale-la en anomalie
- Le parcours nettoye doit correspondre au scenario attendu, pas au log brut
"""

    prompt = f"""Tu es un expert QA automation Katalon Studio. Voici le log des actions capturees 
pendant un parcours automatise par un agent IA (browser-use).

L'agent a potentiellement fait des erreurs : clics dans le desordre, 
etapes recommencees, clics parasites sur des overlays/modals/conteneurs, etc.
{scenario_block}
ACTIONS CAPTUREES (deja dedupliquees pour les inputs) :
{json.dumps(deduped_log, indent=2, ensure_ascii=False)}

REGLES DE FILTRAGE DU BRUIT :
- Ignore les clics sur des elements NON INTERACTIFS : div, span, section, modal, overlay,
  backdrop, conteneur generique (sauf s'ils ont un role="button" ou un aria-label explicite)
- Ignore les clics dont le selecteur est un ID de modal (#menuModal, #overlay, #backdrop, etc.)
- Ignore les clics en double sur le meme element
- Garde UNIQUEMENT les clics sur : button, a, input, select, textarea, [role="button"],
  elements avec aria-label, data-testid, ou un texte significatif
- Si deux clics se suivent sur la meme zone (meme URL, tags proches), garde celui
  qui a le selecteur le plus precis et le tag le plus interactif

CONSIGNE :
1. Reconstitue le parcours E2E IDEAL dans l'ordre logique (sans les erreurs ni le bruit)
2. Supprime les actions parasites, en double ou les retours arriere inutiles
3. Pour chaque step, donne :
   - step : numero
   - action : "click" ou "input"
   - description : description humaine en francais
   - selector : le champ selector.value EXACTEMENT tel quel (NE MODIFIE JAMAIS les selecteurs)
   - selectorType : "xpath" si le selecteur commence par "//" sinon "css"
   - value : valeur saisie (pour les inputs uniquement)
   - page : URL de la page
   - unique : copier selector.unique (true/false)
4. Si un selecteur a "unique": false, signale-le dans les anomalies
5. Liste les anomalies detectees :
   - Selecteurs non uniques
   - Clics parasites supprimes (indiquer lesquels et pourquoi)
   - Etapes du scenario non couvertes par le log
   - Tout ecart entre le scenario attendu et le parcours reconstitue
6. Genere le code Katalon Studio (Groovy) complet et fonctionnel pour rejouer ce parcours.
   
   REGLE CRITIQUE POUR LE CODE KATALON :
   - Ne modifie JAMAIS les selecteurs qui ont unique: true. Utilise-les EXACTEMENT tels quels.
   - EXCEPTION pour unique: false : si le selecteur est non-unique ET qu'un texte est disponible
     dans le log (champ "text"), genere un XPath avec le texte pour fiabiliser le clic.
     Exemple : selecteur "a.listItem__container" (15 matchs) avec texte "Compétitions"
     → to.addProperty("xpath", ConditionType.EQUALS, "//a[contains(@class,'listItem__container') and contains(text(),'Compétitions')]")
     Signale ce remplacement dans les anomalies.
   - Pour creer un TestObject, utilise TOUJOURS ce pattern :
     
     TestObject to = new TestObject("nomDescriptif")
     Si le selecteur commence par "//" :
       to.addProperty("xpath", ConditionType.EQUALS, "le_selecteur_exact")
     Sinon :
       to.addProperty("css", ConditionType.EQUALS, "le_selecteur_exact")
   
   - Import requis en haut : 
     import com.kms.katalon.core.testobject.TestObject
     import com.kms.katalon.core.testobject.ConditionType
     import com.kms.katalon.core.webui.keyword.WebUiBuiltInKeywords as WebUI
   - Utilise WebUI.openBrowser('') et WebUI.navigateToUrl() pour ouvrir la page
   - Utilise WebUI.click(to) pour les clics
   - Utilise WebUI.setText(to, value) pour les inputs
   - Ajoute WebUI.delay(1) entre les etapes pour la stabilite
   - Ajoute WebUI.verifyElementPresent(to, 10) avant chaque interaction
   - Si l'entree a "inShadowDOM": true, utilise WebUI.executeJavaScript() avec le jsSelector fourni
   - Termine avec WebUI.closeBrowser()
   - Ajoute des commentaires en francais pour chaque etape

Reponds UNIQUEMENT en JSON valide, pas de markdown, pas de commentaires.
Structure attendue :
{{
  "parcours": "nom du parcours",
  "date": "date du test",
  "total_steps": nombre,
  "filtered_noise": ["description du bruit supprime 1", "description 2"],
  "anomalies": ["anomalie 1", "anomalie 2"],
  "steps": [
    {{
      "step": 1,
      "action": "click",
      "description": "...",
      "selector": "...",
      "selectorType": "css ou xpath",
      "value": null,
      "page": "...",
      "unique": true
    }}
  ],
  "katalon_code": "// Code Katalon complet ici en une seule string avec des \\n pour les sauts de ligne"
}}"""

    print("\n  Nettoyage IA en cours...")
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )

    raw_response = response.choices[0].message.content

    # Nettoyer la reponse (enlever les backticks markdown si presents)
    cleaned = raw_response.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1]
    if cleaned.endswith("```"):
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()

    try:
        result = json.loads(cleaned)
        # Afficher le bruit filtre si present
        filtered = result.get('filtered_noise', [])
        if filtered:
            print(f"  Bruit filtre ({len(filtered)} actions parasites supprimees) :")
            for f_item in filtered:
                print(f"    - {f_item}")
        return result
    except json.JSONDecodeError:
        print("  Reponse IA pas en JSON valide, sauvegarde brute")
        return {"raw_response": cleaned}


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

async def run(task, model=LLM_MODEL, cdp_port=CDP_PORT, scenario_name="", scenario_url="", scenario_steps=None):
    # Stocker les steps du scenario pour le cleanup IA
    global _scenario_steps
    _scenario_steps = scenario_steps
    """Fonction principale d'execution du QA Explorer"""
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # -- ETAPE 1 : Lancer Chromium avec CDP (plein ecran) --
    print_header("LANCEMENT CHROMIUM + CDP")
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=[
            f"--remote-debugging-port={cdp_port}",
            "--start-maximized"
        ]
    )
    context = browser.contexts[0] if browser.contexts else await browser.new_context(no_viewport=True)
    page = context.pages[0] if context.pages else await context.new_page()

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
    await context.add_init_script(DOM_LISTENER_JS)
    await page.evaluate(DOM_LISTENER_JS)
    print(f"  DOM Listener injecte via Playwright")

    # Via CDP direct (couvre TOUS les contextes, y compris celui de browser-use)
    try:
        cdp_session = await browser.new_browser_cdp_session()
        await cdp_session.send("Page.addScriptToEvaluateOnNewDocument", {
            "source": DOM_LISTENER_JS
        })
        print(f"  DOM Listener injecte via CDP (global, tous contextes)")
    except Exception as e:
        print(f"  Injection CDP echouee (pas critique): {e}")

    # -- ETAPE 3 : Lancer browser-use via CDP --
    print_header("LANCEMENT BROWSER-USE")
    llm = ChatOpenAI(model=model)
    browser_session = BrowserSession(cdp_url=f"http://localhost:{cdp_port}")

    agent = Agent(
        task=task,
        llm=llm,
        browser_session=browser_session,
    )

    result = await agent.run()

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

    # Sauvegarder le log brut
    raw_file = f"locator_log_{timestamp}.json"
    with open(raw_file, "w", encoding="utf-8") as f:
        json.dump(raw_log, f, indent=2, ensure_ascii=False)
    print(f"  Log brut -> {raw_file}")

    # -- ETAPE 5 : Dedupliquer les inputs --
    deduped = dedup_log(raw_log)
    print(f"  {len(raw_log)} -> {len(deduped)} apres deduplication")

    dedup_file = f"locator_dedup_{timestamp}.json"
    with open(dedup_file, "w", encoding="utf-8") as f:
        json.dump(deduped, f, indent=2, ensure_ascii=False)
    print(f"  Log deduplique -> {dedup_file}")

    # -- ETAPE 6 : Afficher le resultat agent --
    print_header("RESULTAT AGENT")
    print(f"  {result.final_result()}")

    # -- ETAPE 7 : Afficher le log deduplique --
    print_raw_log(deduped)

    # -- ETAPE 8 : Nettoyage IA --
    if len(deduped) > 0:
        clean_data = ai_cleanup(deduped, scenario_steps=_scenario_steps, model=model)

        clean_file = f"clean_steps_{timestamp}.json"
        with open(clean_file, "w", encoding="utf-8") as f:
            json.dump(clean_data, f, indent=2, ensure_ascii=False)
        print(f"\n  Parcours nettoye -> {clean_file}")

        print_clean_steps(clean_data)

        # -- ETAPE 9 : Sauvegarder le code Katalon --
        katalon_code = clean_data.get('katalon_code', '')
        if katalon_code:
            katalon_file = f"katalon_test_{timestamp}.groovy"
            with open(katalon_file, "w", encoding="utf-8") as f:
                f.write(katalon_code)
            print(f"\n  Code Katalon -> {katalon_file}")

        # -- ETAPE 10 : Generer le rapport HTML --
        if HAS_REPORT:
            print_header("GENERATION DU RAPPORT")
            report_path = generate_report(
                clean_data=clean_data,
                deduped_log=deduped,
                agent_result=str(result.final_result()),
                scenario_name=scenario_name,
                scenario_url=scenario_url,
                timestamp=timestamp,
            )
            print(f"  Rapport HTML -> {report_path}")
            open_report(report_path)
            print(f"  Rapport ouvert dans le navigateur")
        else:
            print("\n  report_generator.py absent, pas de rapport HTML")
    else:
        print("\n  Aucune entree capturee, pas de nettoyage IA")

    # -- CLEANUP --
    print_header("FERMETURE")
    await browser.close()
    await pw.stop()
    print("  Termine !")


if __name__ == "__main__":
    task, model, port, scenario_name, scenario_url, scenario_steps = resolve_task()
    asyncio.run(run(task, model, port, scenario_name, scenario_url, scenario_steps))