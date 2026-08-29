"""
DOMAutopsy - Construction du clean_steps.json enrichi (schema v2.0)
====================================================================
Remplace l'ancien ai_cleanup() qui produisait un JSON limite (click/input).

Pipeline (dans cet ordre) :
  1. extract_browser_use_history(agent, result)
     -> historique complet de l'agent (defensif : 3 fallbacks d'API)
  2. build_pre_cleanup_steps(scenario_steps, bu_history, dom_log, network_log)
     -> liste initiale de Step avec toutes les actions (pas juste click/input),
        rapprochee par timestamp au DOM listener quand possible, source tracee
  3. detect_and_flag_sensitive(steps)
     -> assigne env_var aux steps input sensitive (le TS pointera dessus)
  4. ai_classify_steps(steps, scenario_steps, ...)
     -> le LLM annote chaque step (included_in_replay + cleanup_reason) et
        remonte les anomalies globales - il NE CONSTRUIT PLUS les steps
  5. generate_export_code(steps, format, ...)  [optionnel, si format != playwright]
     -> IA genere le code Katalon/Cypress/Selenium pour l'export livrable
  6. CleanSteps.model_validate(...)
     -> validation Pydantic finale avant ecriture disque

Regle du cahier des charges strictement appliquee :
  "Ne fabrique jamais un selecteur lorsqu'aucune correspondance fiable
  n'existe. Conserve l'information et signale l'incertitude dans les
  anomalies."
"""

from __future__ import annotations

import json
from typing import Any, Optional

from openai import OpenAI

from schemas import (
    CURRENT_SCHEMA_VERSION,
    CleanSteps,
    Step,
    Selector,
    NetworkRef,
    KNOWN_ACTIONS,
)


# ============================================================
# 1. Extraction defensive de l'historique browser-use
# ============================================================

def extract_browser_use_history(agent: Any, result: Any) -> list[dict[str, Any]]:
    """Recupere l'historique complet des actions browser-use 0.13.8.

    CRITICAL FIX (review round 2) : interacted_element est une LISTE
    alignee avec model_output.action (BU >= 0.12). L'ancien code prenait
    juste state.interacted_element (le premier) et le collait a TOUTES
    les actions du step - faux pour les steps multi-actions. Fix :
    aligner explicitement action[i] <-> interacted_element[i] via
    _align_actions_with_elements().

    Ordre de resolution des sources :
      1. result.history (AgentHistoryList) - PRINCIPAL, expose actions +
         elements + metadata timing intacts (chemin recommande)
      2. agent.history.history / agent.state.history - fallbacks defensifs
         pour variations d'API entre versions BU
      3. result.model_actions() - fallback ULTIME quand aucune history
         list n'est disponible. Cette methode retourne une liste plate
         d'actions SANS metadata timing (pas de step_start_time). On
         l'utilise pour ne rien perdre mais on ne peut pas faire de
         fusion chronologique dessus.

    Retourne : list[step_dict] ou chaque step_dict a :
      - normalized_actions : list[{action, interacted_element, action_index,
                                    step_number, step_start_time, step_end_time,
                                    thought, action_result}]
        Chaque entree est deja resolue pour son element - pas de reference
        step-level qui pourrait etre mal appliquee downstream.
      - Anomalies dans un step sont capturees per-action, pas au step level.

    Ancien shape (actions/interacted_element/metadata au step level) est
    egalement conserve pour retro-compat des tests et de browser_use_history.json.
    """
    history_list = _resolve_history_list(agent, result)

    # Fallback ULTIME : model_actions() flat, sans metadata timing.
    # NE PAS utiliser en principal si history_list est dispo (perte info).
    if not history_list and result is not None and hasattr(result, "model_actions"):
        try:
            flat = result.model_actions()
            if flat:
                out = []
                for i, a in enumerate(flat):
                    action_dict = _action_to_dict(a)
                    out.append({
                        "actions": [action_dict],
                        "normalized_actions": [{
                            "action": action_dict,
                            "action_index": 0,
                            "interacted_element": None,
                            "step_number": None,
                            "step_start_time": None,
                            "step_end_time": None,
                            "source_hint": "model_actions_fallback",
                        }],
                    })
                return out
        except Exception:
            pass

    if not history_list:
        return []

    normalized: list[dict[str, Any]] = []
    for entry in history_list:
        try:
            item: dict[str, Any] = {}

            # Actions LLM du step
            model_output = getattr(entry, "model_output", None)
            actions_raw: list[Any] = []
            if model_output is not None:
                actions_raw = getattr(model_output, "action", None) or []
                item["actions"] = [_action_to_dict(a) for a in actions_raw]
                if getattr(model_output, "current_state", None):
                    cs = model_output.current_state
                    item["thought"] = getattr(cs, "next_goal", None) or getattr(cs, "memory", None)

            # Results per-action
            results_raw = getattr(entry, "result", None) or []
            if results_raw:
                item["results"] = [_result_to_dict(r) for r in results_raw]

            # State avec interacted_element (peut etre LISTE alignee)
            state = getattr(entry, "state", None)
            interacted_source: Any = None
            if state is not None:
                item["state"] = {
                    "url": getattr(state, "url", None),
                    "title": getattr(state, "title", None),
                    "tabs_count": len(getattr(state, "tabs", []) or []),
                }
                interacted_source = getattr(state, "interacted_element", None)
                # BU 0.13.8 peut aussi exposer sur ActionResult
                if interacted_source is None and results_raw:
                    per_result = []
                    any_found = False
                    for r in results_raw:
                        r_ie = getattr(r, "interacted_element", None)
                        per_result.append(r_ie)
                        if r_ie is not None:
                            any_found = True
                    if any_found:
                        interacted_source = per_result  # deja aligne aux results

            # Retro-compat step-level (single element pour anciens consumers)
            if interacted_source is not None and not isinstance(interacted_source, list):
                item["interacted_element"] = _element_to_dict(interacted_source)
            elif isinstance(interacted_source, list) and interacted_source:
                # Prend le premier NON-NULL pour retrocompat (le vrai
                # matching action-par-action est dans normalized_actions).
                first_nn = next((e for e in interacted_source if e is not None), None)
                if first_nn is not None:
                    item["interacted_element"] = _element_to_dict(first_nn)

            # Metadata timing
            metadata = getattr(entry, "metadata", None)
            if metadata is not None:
                item["metadata"] = {
                    "step_number": getattr(metadata, "step_number", None),
                    "step_start_time": _to_ms(getattr(metadata, "step_start_time", None)),
                    "step_end_time": _to_ms(getattr(metadata, "step_end_time", None)),
                    "input_tokens": getattr(metadata, "input_tokens", None),
                }

            # ALIGNMENT CRITIQUE : action[i] <-> interacted_element[i]
            item["normalized_actions"] = _align_actions_with_elements(
                actions_raw=actions_raw,
                interacted_source=interacted_source,
                results_raw=results_raw,
                metadata=item.get("metadata") or {},
            )

            normalized.append(item)
        except Exception as e:
            normalized.append({"_parse_error": str(e)})

    return normalized


def _align_actions_with_elements(
    actions_raw: list[Any],
    interacted_source: Any,
    results_raw: list[Any],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Cœur du fix R2 : construit une liste normalisee ou chaque action
    est APPARIEE a son element correspondant (par index).

    Cas gerés :
      - interacted_source = LISTE de N elements pour N actions -> alignement direct
      - interacted_source = SEUL element pour 1 seule action -> alignement direct
      - interacted_source = SEUL element pour N actions -> applique a l'action 0,
        None pour les autres (evite le bug d'ancien code qui collait le meme
        element a toutes)
      - interacted_source = None -> tous les elements a None
      - LISTE plus courte/longue que actions -> aligne autant que possible,
        les extras cote actions ont element=None, les extras cote elements
        sont ignores (l'action est la source de verite du nombre d'entrees)
    """
    out: list[dict[str, Any]] = []
    n = len(actions_raw)
    if n == 0:
        return out

    # Normalise interacted_source en liste de meme longueur que actions
    if isinstance(interacted_source, list):
        elements_list = list(interacted_source)
    elif interacted_source is not None:
        # Element unique - applique a l'action 0 uniquement
        elements_list = [interacted_source] + [None] * (n - 1)
    else:
        elements_list = [None] * n

    # Ajuste la longueur : tronque si trop long, complete avec None si trop court
    if len(elements_list) < n:
        elements_list = elements_list + [None] * (n - len(elements_list))
    elif len(elements_list) > n:
        elements_list = elements_list[:n]

    step_number = metadata.get("step_number")
    step_start_time = metadata.get("step_start_time")
    step_end_time = metadata.get("step_end_time")

    for i, action in enumerate(actions_raw):
        elem = elements_list[i]
        action_result = None
        if results_raw and i < len(results_raw):
            action_result = _result_to_dict(results_raw[i])
        out.append({
            "action": _action_to_dict(action),
            "action_index": i,
            "interacted_element": _element_to_dict(elem) if elem is not None else None,
            "step_number": step_number,
            "step_start_time": step_start_time,
            "step_end_time": step_end_time,
            "action_result": action_result,
        })
    return out


def _resolve_history_list(agent: Any, result: Any) -> Any:
    """3 chemins d'acces defensifs a AgentHistoryList selon la version BU."""
    # 1. result.history (BU 0.12.9 : c'est ici que ca vit typiquement)
    if result is not None and hasattr(result, "history") and getattr(result, "history"):
        return result.history
    # 2. agent.history (property qui expose AgentHistoryList)
    if agent is not None and hasattr(agent, "history"):
        h = getattr(agent, "history", None)
        if h is not None:
            return getattr(h, "history", None) or h
    # 3. agent.state.history (versions plus anciennes)
    if agent is not None and hasattr(agent, "state"):
        state = getattr(agent, "state", None)
        if state is not None:
            h = getattr(state, "history", None)
            if h is not None:
                return getattr(h, "history", None) or h
    return None


def _element_to_dict(elem: Any) -> dict[str, Any]:
    """Serialise defensivement un DOMHistoryElement browser-use."""
    if isinstance(elem, dict):
        return dict(elem)
    if hasattr(elem, "model_dump"):
        try:
            return elem.model_dump(exclude_none=True)
        except Exception:
            pass
    out: dict[str, Any] = {}
    for attr in ("xpath", "css_selector", "tag_name", "attributes",
                 "is_visible", "is_interactive", "shadow_root", "highlight_index"):
        v = getattr(elem, attr, None)
        if v is not None:
            out[attr] = v
    return out or {"raw": str(elem)}


def _to_ms(ts: Any) -> int | None:
    """Normalise un timestamp BU (datetime, float seconds, int ms) en int ms
    pour permettre la fusion chronologique avec les timestamps DOM listener
    (Date.now() JS = int ms since epoch)."""
    if ts is None:
        return None
    # datetime avec .timestamp() -> secondes float
    if hasattr(ts, "timestamp") and callable(ts.timestamp):
        try:
            return int(ts.timestamp() * 1000)
        except Exception:
            pass
    # float seconds (time.time() typique)
    if isinstance(ts, float):
        return int(ts * 1000) if ts < 1e12 else int(ts)
    # int - heuristique : > 1e12 = deja en ms, sinon secondes
    if isinstance(ts, int):
        return ts if ts > 1e12 else ts * 1000
    return None


def _action_to_dict(action: Any) -> dict[str, Any]:
    """Convertit un Action browser-use en dict serialisable (defensif)."""
    if hasattr(action, "model_dump"):
        try:
            return action.model_dump(exclude_none=True)
        except Exception:
            pass
    if hasattr(action, "dict"):
        try:
            return action.dict()
        except Exception:
            pass
    if isinstance(action, dict):
        return dict(action)
    return {"raw": str(action)}


def _result_to_dict(result: Any) -> dict[str, Any]:
    """Convertit un ActionResult browser-use en dict serialisable."""
    if hasattr(result, "model_dump"):
        try:
            return result.model_dump(exclude_none=True)
        except Exception:
            pass
    out: dict[str, Any] = {}
    for attr in ("extracted_content", "error", "is_done", "success"):
        v = getattr(result, attr, None)
        if v is not None:
            out[attr] = v
    return out or {"raw": str(result)}


# ============================================================
# 2. Construction initiale des Step (fusion multi-sources)
# ============================================================

def _dom_selector_to_pydantic(sel: dict[str, Any] | None) -> Optional[Selector]:
    """Convertit un selector du DOM listener en Selector Pydantic."""
    if not sel or not isinstance(sel, dict):
        return None
    return Selector(
        strategy=sel.get("strategy"),
        value=sel.get("value"),
        inShadowDOM=bool(sel.get("inShadowDOM")),
        unique=sel.get("unique"),
        matchCount=sel.get("matchCount"),
        shadowChain=sel.get("shadowChain"),
        playwrightSelector=sel.get("playwrightSelector"),
        jsSelector=sel.get("jsSelector"),
    )


def _step_from_dom_entry(entry: dict[str, Any], index: int) -> Step:
    """Convertit une entree du DOM listener en Step v1.0.
    C'est la source la plus fiable pour click/input/scroll : le selecteur
    a ete valide runtime via querySelectorAll.

    Le timestamp DOM listener vient de Date.now() JS = int ms since epoch.
    On le stocke tel quel dans step.timestamp (unite normalisee ms utilisee
    par la fusion chronologique).
    """
    action = (entry.get("action") or "unknown").lower()
    sel = _dom_selector_to_pydantic(entry.get("selector"))
    val = entry.get("value")
    is_sensitive = bool(entry.get("sensitive"))
    # DOM listener produit Date.now() JS = int ms depuis epoch (toujours).
    # On accepte n'importe quel int/float comme etant DEJA en ms - jamais
    # de conversion sec->ms qui casserait un test avec de petits ts.
    ts_raw = entry.get("timestamp")
    ts_ms = int(ts_raw) if isinstance(ts_raw, (int, float)) else None
    step = Step(
        id=f"step-{index:04d}",
        step=index,
        action=action,
        description=entry.get("text") or None,
        page=entry.get("url"),
        url=entry.get("url"),
        timestamp=ts_ms,
        selector=sel,
        selectorType=("xpath" if sel and sel.value and sel.value.startswith("//") else ("window" if action == "scroll" else "css")),
        target=entry.get("text"),
        unique=(sel.unique if sel else None),
        matchCount=(sel.matchCount if sel else None),
        inShadowDOM=bool(entry.get("inShadowDOM")),
        value=val,
        sensitive=is_sensitive,
        direction=entry.get("direction"),
        deltaY=entry.get("deltaY"),
        scrollY=entry.get("scrollY"),
        source="dom_listener",
        included_in_replay=True,  # sera potentiellement passe a false par ai_classify_steps
    )
    return step


def _step_from_bu_action(
    bu_action: dict[str, Any],
    index: int,
    current_url: str | None,
    interacted_element: dict[str, Any] | None = None,
    ts_ms: int | None = None,
) -> Step | None:
    """Convertit une action browser-use en Step v1.0.

    interacted_element : le state.interacted_element expose par BU 0.12.9
    pour les actions qui ciblent un element (click, input, select). On
    l'utilise pour extraire un selecteur QUAND le DOM listener n'a pas
    capture l'evenement (scroll conteneur, iframe cross-origin, action
    avant injection listener). Regle du cahier : "ne fabrique jamais un
    selecteur sans correspondance fiable" - donc si interacted_element
    ne fournit pas un selecteur valide, on garde le step SANS selecteur
    et on signale l'incertitude via anomalies.

    ts_ms : timestamp normalise en ms (int) pour la fusion chronologique.
    Retourne None UNIQUEMENT pour les meta-actions LLM sans lien
    interaction (done, read_content, etc.). Les actions extract sont
    retournees mais marquees included_in_replay=False + cleanup_reason.
    """
    if not isinstance(bu_action, dict) or not bu_action:
        return None
    action_name = next(iter(bu_action.keys()))
    params = bu_action[action_name] if isinstance(bu_action[action_name], dict) else {}

    normalized = _normalize_bu_action_name(action_name)
    if normalized is None:
        # done, read_content, assess, think, note - meta-actions LLM pures
        return None

    step = Step(
        id=f"step-{index:04d}",
        step=index,
        action=normalized,
        source="browser_use_history",
        included_in_replay=True,
        raw_payload=dict(bu_action),
        timestamp=ts_ms,
    )

    # Extraction de selecteur depuis interacted_element si BU l'expose.
    # Structure typique : {xpath, css_selector, attributes: {...}, tag_name}
    _apply_interacted_element(step, interacted_element)

    if normalized == "navigate":
        url = params.get("url") or params.get("website")
        step.url = url or current_url
        step.description = f"Va sur {url}" if url else "Navigation"
        step.selectorType = "url"
        # navigate(new_tab=True) est different d'un simple navigate
        if params.get("new_tab"):
            step.action = "open_tab"
            step.description = f"Ouvre nouvel onglet : {url or 'blank'}"
    elif normalized == "wait":
        secs = params.get("seconds") or params.get("duration") or 2
        try:
            step.seconds = float(secs)
        except Exception:
            step.seconds = 2.0
        step.description = f"Attends {step.seconds}s"
    elif normalized == "keyboard":
        step.value = params.get("keys") or params.get("key") or params.get("text")
        step.description = f"Touche clavier : {step.value}"
    elif normalized == "screenshot":
        step.target = params.get("name") or f"capture_{index}"
        step.description = f"Capture ecran : {step.target}"
    elif normalized == "upload":
        step.value = params.get("file_path") or params.get("path")
        step.description = f"Upload fichier : {step.value}"
    elif normalized == "open_tab":
        step.url = params.get("url")
        step.description = f"Ouvre onglet : {step.url or 'blank'}"
    elif normalized == "switch_tab":
        # Gestion deterministe par ORDRE DE CREATION (page_index), pas
        # par tab_id opaque BU. Le TS utilisera context().pages()[index].
        idx = params.get("page_index")
        if idx is None:
            idx = params.get("index", 0)
        try:
            step.value = str(int(idx))
        except (TypeError, ValueError):
            step.value = "0"
        step.description = f"Bascule sur l'onglet [{step.value}]"
    elif normalized == "close_tab":
        idx = params.get("page_index")
        if idx is None:
            idx = params.get("index")
        step.value = str(int(idx)) if idx is not None else None
        step.description = (
            f"Ferme l'onglet [{step.value}]" if step.value else
            "Ferme l'onglet courant"
        )
    elif normalized in ("go_back", "go_forward", "reload"):
        step.description = {
            "go_back": "Retour arriere",
            "go_forward": "Avance",
            "reload": "Recharge la page",
        }[normalized]
    elif normalized == "extract":
        # Extract est une lecture LLM (extraire un texte/donnee depuis
        # le DOM pour raisonner). Aucune interaction user, NON rejouable.
        # On garde dans le JSON pour tracabilite + rapport, mais on
        # marque explicitement included_in_replay=False.
        step.description = f"Extract LLM : {params.get('goal') or params.get('query') or 'lecture DOM'}"
        step.included_in_replay = False
        step.cleanup_reason = "action extract (lecture LLM only, pas d'interaction utilisateur reproductible)"
    elif normalized in ("click", "input", "select", "scroll", "hover"):
        # Actions interactives BU sans correspondance DOM listener.
        # interacted_element a peut-etre fourni le selecteur. Sinon :
        # valeur/description best-effort, anomalie signalee au niveau
        # global (via _link_bu_dom_signal_anomalies).
        if normalized == "input":
            step.value = params.get("text") or params.get("value")
        elif normalized == "select":
            step.value = params.get("value") or params.get("option") or params.get("text")
        elif normalized == "scroll":
            step.direction = "down" if (params.get("direction") in (None, "down", "bas")) else "up"
            step.deltaY = params.get("amount") or params.get("delta") or 650
        step.description = f"{normalized.capitalize()} (BU-only, sans DOM event)"
    else:
        step.action = "unknown"
        step.description = f"Action browser-use non standard : {action_name}"

    step.page = current_url
    return step


def _apply_interacted_element(step: Step, elem: dict[str, Any] | None) -> None:
    """Extrait un selecteur robuste depuis state.interacted_element de BU.

    BU 0.12.9 expose : {xpath, css_selector, attributes: {id, class, ...},
    tag_name, is_visible, is_interactive}. Regle du cahier : jamais
    fabriquer un selecteur - on prend UNIQUEMENT ce que BU nous donne
    (xpath ou css directement).
    """
    if not elem or not isinstance(elem, dict):
        return
    css = elem.get("css_selector")
    xpath = elem.get("xpath")
    if css and isinstance(css, str):
        step.selector = Selector(strategy="bu-css", value=css, unique=None, matchCount=None)
        step.selectorType = "css"
    elif xpath and isinstance(xpath, str):
        step.selector = Selector(strategy="bu-xpath", value=xpath, unique=None, matchCount=None)
        step.selectorType = "xpath"
    # Attributes peuvent enrichir la description sans devenir selecteur
    attrs = elem.get("attributes") or {}
    if isinstance(attrs, dict) and not step.target:
        step.target = attrs.get("aria-label") or attrs.get("name") or attrs.get("id")


def _normalize_bu_action_name(name: str) -> str | None:
    """Mappe les noms d'action browser-use 0.12.9 vers notre vocabulaire.

    Verifie contre les noms REELS du service browser-use (voir
    https://github.com/browser-use/browser-use/blob/0.12.9/browser_use/tools/service.py)
    - pas les noms qu'on aimerait avoir. Actions BU 0.12.9 :
      switch, close, select_dropdown, extract (et non switch_tab, close_tab,
      select_option, extract_content).

    Retourne None UNIQUEMENT pour les meta-actions LLM sans lien avec une
    interaction utilisateur (done, read_content, assess, think, note).
    'extract' est retourne tel quel et sera marque non-rejouable au step
    level (pas None : on veut le tracer dans le JSON, pas le supprimer)."""
    n = (name or "").lower()
    # Meta-actions LLM pures (aucune interaction utilisateur reelle)
    NON_INTERACTION = {"done", "read_content", "assess", "think", "note"}
    if n in NON_INTERACTION:
        return None
    MAP = {
        # Navigation
        "go_to_url": "navigate",
        "navigate_to": "navigate",
        "open_url": "navigate",
        # Attente
        "wait": "wait",
        "wait_for": "wait",
        # Clavier
        "press_key": "keyboard",
        "key_press": "keyboard",
        "keyboard": "keyboard",
        "send_keys": "keyboard",
        # Screenshot
        "screenshot": "screenshot",
        "take_screenshot": "screenshot",
        # Upload
        "upload_file": "upload",
        "upload": "upload",
        # Historique navigation
        "go_back": "go_back",
        "back": "go_back",
        "go_forward": "go_forward",
        "reload_page": "reload",
        "refresh": "reload",
        "reload": "reload",
        # Onglets - BU 0.12.9 utilise switch/close (pas switch_tab/close_tab)
        "open_tab": "open_tab",
        "new_tab": "open_tab",
        "switch": "switch_tab",
        "switch_tab": "switch_tab",
        "close": "close_tab",
        "close_tab": "close_tab",
        # Interactions elements
        "click_element": "click",
        "click_element_by_index": "click",
        "click": "click",
        "input_text": "input",
        "type": "input",
        "fill": "input",
        # Dropdown - BU 0.12.9 utilise select_dropdown (pas select_option)
        "select_dropdown": "select",
        "select_option": "select",
        "select": "select",
        "hover": "hover",
        # Scroll
        "scroll": "scroll",
        "scroll_down": "scroll",
        "scroll_up": "scroll",
        # Lecture DOM - BU 0.12.9 utilise extract (pas extract_content)
        # Garde dans le JSON, marquera included_in_replay=False + reason
        "extract": "extract",
        "extract_content": "extract",
    }
    return MAP.get(n, n if n in KNOWN_ACTIONS else "unknown")


def build_pre_cleanup_steps(
    scenario_steps: list[dict[str, Any]] | None,
    bu_history: list[dict[str, Any]],
    dom_log: list[dict[str, Any]],
    network_log: list[dict[str, Any]] | None = None,
) -> list[Step]:
    """Construit la liste initiale de Step en fusionnant les sources.

    Strategie de fusion :
      - Le DOM listener est LA source de verite pour les selecteurs runtime
        (click/input/scroll) : chaque entree devient un Step.
      - L'historique BU apporte les actions que le listener ne capture pas
        (navigate, wait, keyboard, screenshot, upload, tabs...) : on les
        ajoute en interpolant l'ordre temporel.
      - Le scenario JSON demande sert de contexte (verify, hover, cookie)
        s'ils ne sont pas capturables par le DOM.

    Rapprochement network : chaque step recoit un list[NetworkRef] pour
    les requetes tombees dans sa fenetre temporelle [ts, ts_next].
    """
    # ============================================================
    # FUSION CHRONOLOGIQUE REELLE (B1)
    # ============================================================
    # 1. Normalise les DOM entries en list ordonnee par timestamp ms.
    #    dom_consumed[i]=True quand une entree a ete rattachee a un BU step.
    #    DOM listener JS produit Date.now() en ms deja - on accepte tel quel.
    dom_entries = []
    for e in (dom_log or []):
        ts_raw = e.get("timestamp")
        ts_ms = int(ts_raw) if isinstance(ts_raw, (int, float)) else None
        dom_entries.append({"ts_ms": ts_ms, "entry": e})
    dom_entries.sort(key=lambda x: x["ts_ms"] or 0)
    dom_consumed = [False] * len(dom_entries)

    steps: list[Step] = []
    current_url: str | None = None
    global_index = 0
    # Buffer temporel : DOM event peut arriver un peu avant/apres la fenetre
    # BU (delais de flush localStorage cote listener, delais network cote
    # BU). 500ms est empirique - reste ajustable via TOLERANCE_MS.
    TOLERANCE_MS = 500

    def _next_index() -> int:
        nonlocal global_index
        global_index += 1
        return global_index

    def _find_matching_dom(normalized_action: str, window_start: int | None,
                           window_end: int | None) -> int | None:
        """Cherche un DOM entry non-consomme, meme action, dans la fenetre."""
        for i, d in enumerate(dom_entries):
            if dom_consumed[i]:
                continue
            if d["ts_ms"] is None:
                continue
            if window_start is not None and d["ts_ms"] < window_start:
                continue
            if window_end is not None and d["ts_ms"] > window_end:
                continue
            if (d["entry"].get("action") or "").lower() == normalized_action:
                return i
        return None

    # 2. Parcours de l'historique BU dans l'ordre.
    #    Pour chaque BU step (avec fenetre [start, end]) :
    #      - matcher chaque click/input/scroll/select/hover a un DOM entry
    #        de la fenetre (source=bu+dom, selecteur DOM fiable)
    #      - si pas de match : construire depuis interacted_element de BU
    #        (source=browser_use_history, selecteur BU-provided, anomalie
    #        conservee dans le rapport)
    #      - autres actions (navigate, wait, keyboard, etc.) : ajout direct
    for h in bu_history or []:
        if not isinstance(h, dict):
            continue
        metadata = h.get("metadata") or {}
        start_ms = metadata.get("step_start_time")
        end_ms = metadata.get("step_end_time")
        window_start = (start_ms - TOLERANCE_MS) if start_ms is not None else None
        window_end = (end_ms + TOLERANCE_MS) if end_ms is not None else None
        # BU step level : ts par defaut = start_ms (pour ordering des actions
        # au sein d'un meme step LLM qui declenche plusieurs actions)
        default_step_ts = start_ms

        # R2 : utilise normalized_actions (chaque action a son element PROPRE
        # deja apparie par _align_actions_with_elements) au lieu de l'ancien
        # interacted_element step-level qui melangeait toutes les actions.
        # Retro-compat : si normalized_actions absent (vieux JSON), on construit
        # a la volee depuis actions + interacted_element step-level.
        normalized_actions_list = h.get("normalized_actions")
        if not normalized_actions_list:
            legacy_interacted = h.get("interacted_element")
            actions_dicts = h.get("actions") or []
            normalized_actions_list = _align_actions_with_elements(
                actions_raw=actions_dicts,
                interacted_source=legacy_interacted,
                results_raw=[],
                metadata=metadata,
            )

        for na in normalized_actions_list:
            action_dict = na.get("action") or {}
            if not isinstance(action_dict, dict) or not action_dict:
                continue
            action_name = next(iter(action_dict.keys()))
            normalized = _normalize_bu_action_name(action_name)
            if normalized is None:
                # Meta LLM (done, read_content, etc.) - jamais un step
                continue

            # Element APPARIE a cette action precise (pas step-level)
            per_action_element = na.get("interacted_element")

            step: Step | None = None
            if normalized in ("click", "input", "scroll", "select", "hover"):
                matched_idx = _find_matching_dom(normalized, window_start, window_end)
                if matched_idx is not None:
                    dom_consumed[matched_idx] = True
                    step = _step_from_dom_entry(
                        dom_entries[matched_idx]["entry"], _next_index()
                    )
                    step.source = "bu+dom"
                    step.raw_payload = {"bu_action": action_dict, "action_index": na.get("action_index")}
                else:
                    # Pas de correspondance DOM demontree - construit depuis
                    # BU + interacted_element de CETTE action. Regle cahier :
                    # jamais fabriquer, mais BU peut fournir le selecteur.
                    step = _step_from_bu_action(
                        action_dict, _next_index(), current_url,
                        interacted_element=per_action_element, ts_ms=default_step_ts,
                    )
            else:
                # navigate, wait, keyboard, screenshot, upload, tabs,
                # go_back, reload, extract, ...
                step = _step_from_bu_action(
                    action_dict, _next_index(), current_url,
                    interacted_element=per_action_element, ts_ms=default_step_ts,
                )

            if step is None:
                continue
            steps.append(step)
            if step.url:
                current_url = step.url

    # 3. DOM entries orphelins : capture DOM sans correspondance BU.
    #    Cause possible : clic manuel de l'utilisateur pendant la
    #    demonstration, event avant/apres injection listener, scroll
    #    conteneur. On les GARDE (regle cahier : ne jamais supprimer sans
    #    correspondance demontree) avec source="dom_orphan" pour permettre
    #    au LLM classifier de decider.
    for i, d in enumerate(dom_entries):
        if dom_consumed[i]:
            continue
        step = _step_from_dom_entry(d["entry"], _next_index())
        step.source = "dom_orphan"
        steps.append(step)

    # 4. Verify/cookie du scenario (declaratifs, non captes par DOM)
    for s in (scenario_steps or []):
        act = (s.get("action") or "").lower()
        if act in ("verify", "cookie"):
            steps.append(Step(
                id=f"step-{_next_index():04d}",
                step=global_index,
                action=act,
                description=s.get("target") or s.get("value"),
                target=s.get("target"),
                expected=s.get("target") if act == "verify" else None,
                verify_type=s.get("type") if act == "verify" else None,
                value=s.get("value"),
                source="scenario",
                page=current_url,
                included_in_replay=True,
            ))

    # 5. Tri final chronologique STABLE : timestamp croissant, les steps
    #    sans timestamp gardent leur ordre d'insertion (verify/cookie
    #    scenario tombent en fin, ce qui reflete leur nature declarative).
    for i, s in enumerate(steps):
        # Ordre d'insertion memorise dans un attribut prive stable via
        # step-number initial ; on n'attache rien de nouveau au schema.
        pass
    steps_indexed = list(enumerate(steps))
    steps_indexed.sort(key=lambda pair: (
        pair[1].timestamp if pair[1].timestamp is not None else 10 ** 18,
        pair[0],
    ))
    steps = [s for _, s in steps_indexed]

    # 6. Rapprochement network
    if network_log:
        _link_network_to_steps(steps, network_log)

    # 7. Renumbering final
    for i, s in enumerate(steps, start=1):
        s.id = f"step-{i:04d}"
        s.step = i

    return steps


def _link_network_to_steps(steps: list[Step], network_log: list[dict[str, Any]]) -> None:
    """Attache network[] a chaque step pour les requetes qui tombent dans
    sa fenetre temporelle [step.timestamp, next_step.timestamp).
    Les steps sans timestamp (BU actions) ne recoivent rien."""
    # network_log entries ont un timestamp CDP monotone. On tolere aussi wallTime.
    if not steps:
        return
    for i, step in enumerate(steps):
        ts = step.timestamp
        if ts is None:
            continue
        ts_next = None
        for s2 in steps[i + 1:]:
            if s2.timestamp is not None:
                ts_next = s2.timestamp
                break
        matched: list[NetworkRef] = []
        for idx, req in enumerate(network_log):
            wt = req.get("wallTime")
            if wt is None:
                continue
            wt_ms = int(wt * 1000)  # wallTime CDP est en secondes float
            if wt_ms < ts:
                continue
            if ts_next is not None and wt_ms >= ts_next:
                break
            matched.append(NetworkRef(
                index=idx,
                method=req.get("method"),
                url=req.get("url"),
                status=req.get("status"),
                type=req.get("type"),
                duration_ms=req.get("duration_ms"),
            ))
        if matched:
            step.network = matched


# ============================================================
# 3. Detection sensitive + assignation env_var
# ============================================================

def detect_and_flag_sensitive(steps: list[Step]) -> list[str]:
    """Pour chaque step input avec sensitive=True, assigne un nom d'env
    var stable (DOMAUTOPSY_STEP_XXXX) que le TS Playwright utilisera.
    Retourne la liste des vars a positionner avant le replay."""
    env_vars: list[str] = []
    for s in steps:
        if s.action == "input" and s.sensitive and s.env_var is None:
            name = f"DOMAUTOPSY_STEP_{s.step or 0:04d}"
            s.env_var = name
            env_vars.append(name)
    return env_vars


# ============================================================
# 4. LLM en mode classificateur (pas constructeur)
# ============================================================

CLASSIFIER_SYSTEM_PROMPT = """Tu es un classificateur QA. On te fournit une liste
de steps DEJA CONSTRUITS a partir de captures runtime (DOM listener + browser-use
history). Ton role N'EST PAS de reconstruire les steps ni d'inventer des selecteurs.

Ton role est UNIQUEMENT :
1. Pour chaque step, decider s'il doit etre inclus dans le replay ou marque
   comme parasite (included_in_replay: false + cleanup_reason court).
2. Lister les anomalies detectees au niveau global (sans modifier les steps).
3. Fournir une courte liste filtered_noise resumant ce qui a ete ecarte.

Regles de filtrage :
- CONSERVE : click sur bouton/lien/input/select interactifs, input, select,
  navigate, verify, scroll, hover, wait, screenshot, cookie, keyboard, upload,
  go_back/forward/reload, open_tab/switch_tab.
- MARQUE PARASITE (included_in_replay=false) : clics consecutifs identiques,
  clics sur backdrop/overlay/modal non interactif, clics sur conteneur div/section
  sans role/aria-label, saisies successives redondantes sur le meme champ.
- NE FILTRE PAS un scroll : le scroll est significatif pour reveler du contenu.

Ne modifie JAMAIS le champ selector d'un step. Tu es en lecture uniquement pour
ces valeurs. Toute correction se fait via anomalies + cleanup_reason.

Reponds UNIQUEMENT en JSON strict :
{
  "classifications": [
    {"step_id": "step-0001", "included_in_replay": true},
    {"step_id": "step-0002", "included_in_replay": false, "cleanup_reason": "clic sur overlay modal"},
    ...
  ],
  "anomalies": ["selecteur non-unique sur step-0004 (matchCount=3)", "..."],
  "filtered_noise": ["3 clics consecutifs sur #login-btn (garde le 1er)"]
}
"""


def ai_classify_steps(
    steps: list[Step],
    scenario_steps: list[dict[str, Any]] | None,
    model: str,
    base_url: str | None,
    api_key: str | None,
) -> tuple[list[Step], list[str], list[str]]:
    """Fait annoter la liste par le LLM. Retourne (steps annotes, anomalies, filtered_noise).

    En cas d'echec du LLM (rate limit, JSON malforme), on retourne les steps
    tels quels avec une anomalie signaletique - le replay reste possible.
    """
    if not steps:
        return steps, [], []

    # Reduction : on envoie au LLM une projection legere de chaque step
    projected = [
        {
            "step_id": s.id,
            "action": s.action,
            "description": s.description,
            "selector": (s.selector.value if s.selector and not isinstance(s.selector, str) else s.selector),
            "unique": s.unique,
            "matchCount": s.matchCount,
            "page": s.page,
            "source": s.source,
        }
        for s in steps
    ]

    user_content = (
        (f"SCENARIO ATTENDU :\n{json.dumps(scenario_steps, ensure_ascii=False)}\n\n" if scenario_steps else "")
        + f"STEPS A CLASSIFIER :\n{json.dumps(projected, ensure_ascii=False)}"
    )

    client_kwargs = {}
    if base_url:
        client_kwargs["base_url"] = base_url
    if api_key:
        client_kwargs["api_key"] = api_key
    try:
        client = OpenAI(**client_kwargs)
        response = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": CLASSIFIER_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        parsed = json.loads(raw.strip())
    except Exception as e:
        return steps, [f"Classification LLM en echec : {type(e).__name__} - {str(e)[:200]}"], []

    # Applique les classifications
    by_id = {s.id: s for s in steps}
    for c in (parsed.get("classifications") or []):
        sid = c.get("step_id")
        s = by_id.get(sid)
        if not s:
            continue
        included = c.get("included_in_replay")
        if included is not None:
            s.included_in_replay = bool(included)
        if c.get("cleanup_reason"):
            s.cleanup_reason = c["cleanup_reason"][:200]

    anomalies = [a for a in (parsed.get("anomalies") or []) if isinstance(a, str)]
    filtered_noise = [f for f in (parsed.get("filtered_noise") or []) if isinstance(f, str)]
    return steps, anomalies, filtered_noise


# ============================================================
# 5. Generation d'export code (Katalon/Cypress/Selenium)
# ============================================================

EXPORT_SYSTEM_PROMPT_TEMPLATE = """Tu es un generateur de code test QA. On te fournit
une liste de steps DEJA VALIDES issus d'une capture DOMAutopsy. Ton role est de
generer un test fonctionnel dans le format {format_label}.

REGLES STRICTES :
- Ne modifie JAMAIS un selecteur qui a unique: true. Utilise-le tel quel.
- Pour les steps sensitive: true, remplace la valeur par une variable d'environnement
  (ex: process.env.NOM_VAR ou System.getenv, selon le langage).
- Utilise les selecteurs XPath (commencant par //) via l'API xpath du framework.
- Skip les steps included_in_replay: false : ils sont marques comme parasites.
- Commentaires en francais, code fonctionnel, imports en tete.

{code_instructions}

Reponds UNIQUEMENT avec le code, pas de markdown, pas d'explication.
"""


def generate_export_code(
    clean_steps: CleanSteps,
    output_format: str,
    model: str,
    base_url: str | None,
    api_key: str | None,
    output_formats_map: dict[str, dict[str, str]],
) -> str | None:
    """Genere le code d'export dans le format demande (Katalon/Cypress/Selenium/Playwright).

    Pour 'playwright', le TS canonique est deja produit par playwright_generator
    (deterministe) - on renvoie None pour eviter de doublonner.
    """
    if output_format == "playwright":
        return None
    fmt = output_formats_map.get(output_format)
    if fmt is None:
        return None
    steps_projected = [
        s.model_dump(exclude_none=True) for s in clean_steps.steps
        if s.included_in_replay
    ]
    system = EXPORT_SYSTEM_PROMPT_TEMPLATE.format(
        format_label=fmt["label"],
        code_instructions=fmt["code_instructions"],
    )
    user = json.dumps({
        "parcours": clean_steps.parcours,
        "scenario_url": clean_steps.scenario_url,
        "steps": steps_projected,
    }, ensure_ascii=False, indent=2)

    client_kwargs = {}
    if base_url:
        client_kwargs["base_url"] = base_url
    if api_key:
        client_kwargs["api_key"] = api_key
    try:
        client = OpenAI(**client_kwargs)
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=0,
        )
        raw = resp.choices[0].message.content.strip()
        # Nettoyage markdown fences si presents
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        return raw.strip()
    except Exception as e:
        return f"// [DOMAutopsy] Generation code {output_format} en echec : {e}\n"


# ============================================================
# 6. Orchestration complete
# ============================================================

def build_clean_steps(
    scenario_name: str,
    scenario_url: str,
    scenario_steps: list[dict[str, Any]] | None,
    bu_history: list[dict[str, Any]],
    dom_log: list[dict[str, Any]],
    network_log: list[dict[str, Any]] | None,
    model: str,
    base_url: str | None,
    api_key: str | None,
) -> tuple[CleanSteps, list[str]]:
    """Orchestration complete de la construction du clean_steps.json enrichi.

    Retourne (CleanSteps valide, list des env_vars sensibles a configurer).
    """
    # 1. Fusion multi-sources
    steps = build_pre_cleanup_steps(scenario_steps, bu_history, dom_log, network_log)

    # 2. Detection sensitive -> env_var
    sensitive_env_vars = detect_and_flag_sensitive(steps)

    # 3. Classification LLM (included_in_replay + anomalies)
    steps, anomalies, filtered_noise = ai_classify_steps(
        steps, scenario_steps, model=model, base_url=base_url, api_key=api_key,
    )

    # 4. Construction du CleanSteps
    clean = CleanSteps(
        schema_version=CURRENT_SCHEMA_VERSION,
        parcours=scenario_name or "parcours DOMAutopsy",
        scenario_name=scenario_name,
        scenario_url=scenario_url,
        total_steps=len(steps),
        steps=steps,
        anomalies=anomalies,
        filtered_noise=filtered_noise,
    )
    return clean, sensitive_env_vars
