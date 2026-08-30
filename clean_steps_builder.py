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
import re
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
    # BU 0.13+ expose x_path (avec underscore), stable_hash, ax_name,
    # backend_node_id, node_name en plus des champs BU 0.12 legacy.
    # On extrait tout - _apply_interacted_element choisira le meilleur
    # selecteur selon une cascade priorisee.
    for attr in ("xpath", "x_path", "css_selector", "tag_name", "node_name",
                 "attributes", "is_visible", "is_interactive", "shadow_root",
                 "highlight_index", "ax_name", "stable_hash", "backend_node_id"):
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
    # parentLabel : contexte capture par le DOM listener pour les
    # change events sur checkbox/radio (permet la desambiguisation
    # ulterieure dans les listes type TodoMVC).
    raw = {}
    if entry.get("parentLabel"):
        raw["parentLabel"] = entry["parentLabel"]
    step = Step(
        id=f"step-{index:04d}",
        step=index,
        action=action,
        description=entry.get("text") or entry.get("parentLabel") or None,
        page=entry.get("url"),
        url=entry.get("url"),
        timestamp=ts_ms,
        selector=sel,
        selectorType=("xpath" if sel and sel.value and sel.value.startswith("//") else ("window" if action == "scroll" else "css")),
        target=entry.get("text") or entry.get("parentLabel"),
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
        raw_payload=raw or None,
        # Champs SELECT (extra="allow" sur Step) : le generateur TS les lit
        # via getattr pour emettre .selectOption({label|index}) au lieu de
        # .fill() qui casse sur <select>.
        label=entry.get("label"),
        labelIsUnique=entry.get("labelIsUnique"),
        selectedIndex=entry.get("selectedIndex"),
        optionCount=entry.get("optionCount"),
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
    # Fallback : si BU n'a pas fourni css_selector/xpath mais expose des
    # data-* dans attributes, construire un selecteur discriminant. Sinon
    # l'action serait perdue (skippee "sans selecteur"). Cas type : le 2eme
    # Add to cart d'automationex n'a pas de DOM event capture (dom_listener
    # a rate), mais BU expose data-product-id="2" -> selecteur unique.
    if normalized in ("click", "input", "hover", "check", "uncheck"):
        if step.selector is None or (
            hasattr(step.selector, "value") and not getattr(step.selector, "value", None)
        ):
            _promote_from_bu_data_attrs(step, interacted_element)
    # Marquer les clicks ad-related pour skip du replay (pattern universel :
    # une pub apparait au capture, absente au replay -> click qui timeout).
    if normalized == "click":
        _mark_ad_related(step, interacted_element)

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
    elif normalized == "evaluate":
        # Execution JS brute (workaround click BU quand selecteur ambigu).
        # Preserve le code JS pour l'emitter TS qui le passera a page.evaluate().
        # Rejouable = true : c'est une vraie interaction (peut modifier le DOM).
        code = params.get("code") or params.get("script") or params.get("js")
        step.description = f"Evaluate JS : {(code or '')[:80]}"
        step.value = code
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
        # interacted_element a peut-etre fourni le selecteur.
        if normalized == "input":
            step.value = params.get("text") or params.get("value")
        elif normalized == "select":
            step.value = params.get("value") or params.get("option") or params.get("text")
        elif normalized == "scroll":
            step.direction = "down" if (params.get("direction") in (None, "down", "bas")) else "up"
            step.deltaY = params.get("amount") or params.get("delta") or 650
        step.description = f"{normalized.capitalize()} (BU-only, sans DOM event)"
        # Regle cahier : "action sans selecteur conservee comme anomalie".
        # Le step est GARDE dans le JSON (tracabilite) mais marque
        # included_in_replay=False + cleanup_reason quand aucun selecteur
        # n'a pu etre extrait (ni DOM ni interacted_element BU). Le TS
        # emit un commentaire SKIPPED, pas un throw fatal.
        if step.selector is None or (
            hasattr(step.selector, "value") and not step.selector.value
        ):
            step.included_in_replay = False
            step.cleanup_reason = (
                f"action {normalized} sans selecteur (BU sans interacted_element "
                f"ni correspondance DOM listener)"
            )
    else:
        step.action = "unknown"
        step.description = f"Action browser-use non standard : {action_name}"

    step.page = current_url
    return step


def _apply_interacted_element(step: Step, elem: dict[str, Any] | None) -> None:
    """Extrait un selecteur robuste depuis state.interacted_element de BU.

    Cascade priorisee par fiabilite (BU 0.13+ expose bien plus qu'attributes) :
      1. #id                              - unique par definition W3C
      2. [data-testid|data-qa]            - conventions test explicites
      3. [data-*] discriminant            - data-product-id, data-item-id...
      4. [aria-label] (issu de ax_name)   - accessibility, stable sur redesign
      5. bu-css / bu-xpath fournis        - si BU les a poses (rare)
      6. x_path complet (BU 0.13)         - dernier recours, position-sensible

    Le `stable_hash` de BU sert de fingerprint interne pour valider la
    stabilite ; ax_name est le texte accessible visible (plus stable
    qu'un xpath positionnel).
    """
    if not elem or not isinstance(elem, dict):
        return
    attrs = elem.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    ax_name = elem.get("ax_name") if isinstance(elem.get("ax_name"), str) else None

    def _set(strategy: str, value: str, selector_type: str = "css") -> None:
        escaped = value if selector_type == "xpath" else value
        step.selector = Selector(strategy=strategy, value=escaped, unique=None, matchCount=None)
        step.selectorType = selector_type  # type: ignore[assignment]

    # 1. id
    eid = attrs.get("id")
    if isinstance(eid, str) and eid.strip():
        _set("bu-id", f"#{eid}")
    # 2. data-testid / data-qa
    elif isinstance(attrs.get("data-testid"), str) and attrs["data-testid"]:
        _set("bu-data-testid", f'[data-testid="{attrs["data-testid"]}"]')
    elif isinstance(attrs.get("data-qa"), str) and attrs["data-qa"]:
        _set("bu-data-qa", f'[data-qa="{attrs["data-qa"]}"]')
    else:
        # 3. Autre data-* non-vide (product-id, item-id, index, key...)
        promoted = False
        for name, val in attrs.items():
            if not isinstance(name, str) or not name.startswith("data-"):
                continue
            if not val or not isinstance(val, str) or len(val) > 60:
                continue
            escaped = val.replace("\\", "\\\\").replace('"', '\\"')
            _set("bu-data-attr", f'[{name}="{escaped}"]')
            promoted = True
            break
        if not promoted:
            # 4. aria-label ou ax_name (accessibility name = texte visible)
            aria = attrs.get("aria-label")
            if isinstance(aria, str) and aria.strip():
                escaped = aria.replace("\\", "\\\\").replace('"', '\\"')
                _set("bu-aria-label", f'[aria-label="{escaped}"]')
            elif ax_name and ax_name.strip():
                escaped = ax_name.replace("\\", "\\\\").replace('"', '\\"')
                _set("bu-ax-name", f'[aria-label="{escaped}"]')
            else:
                # 5. css_selector explicitement fourni par BU
                css = elem.get("css_selector")
                xpath = elem.get("xpath") or elem.get("x_path")
                if isinstance(css, str) and css:
                    _set("bu-css", css)
                elif isinstance(xpath, str) and xpath:
                    # 6. x_path complet en dernier recours (position-sensible,
                    # peut casser si le site insere des elements dynamiques)
                    _set("bu-xpath", xpath, "xpath")

    # target = label lisible pour humain (rapport)
    if not step.target:
        step.target = attrs.get("aria-label") or attrs.get("name") or attrs.get("id") or ax_name


_AD_TOKEN_RE = re.compile(r"\bad\b", re.IGNORECASE)


def _mark_ad_related(step: Step, elem: dict[str, Any] | None) -> None:
    """Detecte les clicks sur elements ad-related (overlays publicitaires
    aleatoires). Ne rentrent PAS dans la timeline replay : au lieu de ca,
    le generator installe un `page.addLocatorHandler()` global qui se
    declenche automatiquement des que l'overlay apparait et bloque un
    click canonique.

    Marque le step included_in_replay=False + stocke dans raw_payload
    ['ad_handler_selector'] le selecteur a installer dans le handler
    global (le generator collecte tous ces selectors uniques).

    Detection : token 'ad' isole (word-boundary) dans les attributs.
    """
    if not elem or not isinstance(elem, dict):
        return
    attrs = elem.get("attributes") or {}
    if not isinstance(attrs, dict):
        return
    for v in attrs.values():
        if not isinstance(v, str) or not _AD_TOKEN_RE.search(v):
            continue
        # Construit le selecteur de l'element ad depuis ses attrs discriminants
        # (aria-label, id, class). Priorite aux plus specifiques.
        ad_sel = None
        aria = attrs.get("aria-label")
        eid = attrs.get("id")
        cls = attrs.get("class")
        if isinstance(aria, str) and aria:
            escaped = aria.replace("\\", "\\\\").replace('"', '\\"')
            ad_sel = f'[aria-label="{escaped}"]'
        elif isinstance(eid, str) and eid:
            ad_sel = f'#{eid}'
        elif isinstance(cls, str) and cls:
            first_cls = cls.split()[0]
            ad_sel = f'.{first_cls}'
        step.included_in_replay = False
        step.cleanup_reason = (
            "click ad-related : gere par un page.addLocatorHandler global "
            "installe au debut du test (declenche automatiquement si l'overlay "
            "apparait au replay et bloque un click canonique)"
        )
        raw = step.raw_payload or {}
        if not isinstance(raw, dict):
            raw = {"legacy_raw": raw}
        if ad_sel:
            raw["ad_handler_selector"] = ad_sel
        step.raw_payload = raw
        return


def _promote_from_bu_data_attrs(step: Step, elem: dict[str, Any] | None) -> bool:
    """Promeut le selecteur du step vers un [data-*="Y"] quand BU expose
    un attribut data-* dans interacted_element.attributes.

    Cas d'usage : le DOM listener a produit un selecteur non-unique
    (ex: 'a.btn.btn-default' matchCount=68 sur une grille produits), mais
    BU voit l'element cible avec ses attributs et expose data-product-id="1".
    Un data-attr non-vide est semantiquement discriminant (product-id,
    item-id, row-key, index) - source de verite plus fiable que la cascade
    heuristique DOM sur un container generique.

    Ne promeut que si le selecteur courant est non-unique (unique is False).
    Retourne True si une promotion a eu lieu.
    """
    if not elem or not isinstance(elem, dict):
        return False
    attrs = elem.get("attributes") or {}
    if not isinstance(attrs, dict):
        return False
    cur = step.selector
    if cur is not None and getattr(cur, "unique", None) is True:
        return False
    for name, val in attrs.items():
        if not isinstance(name, str) or not name.startswith("data-"):
            continue
        if not val or not isinstance(val, str):
            continue
        if len(val) > 60:
            continue
        escaped = val.replace("\\", "\\\\").replace('"', '\\"')
        cand = f'[{name}="{escaped}"]'
        step.selector = Selector(
            strategy="bu-data-attr", value=cand, unique=None, matchCount=None
        )
        step.selectorType = "css"
        return True
    return False


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
    # Meta-actions LLM/agent PURES : aucune interaction utilisateur avec la
    # page, aucun effet DOM reproductible - a filtrer AVANT toute inclusion
    # dans clean_steps.json (donc jamais envoyees ni au LLM classifier ni
    # au generateur TS).
    #   done / assess / think / note : delibere LLM interne
    #   read_content : lecture DOM par l'agent (pas d'action user)
    #   write_file / read_file : planner interne BU 0.13+ ecrit un todo.md
    #     ou similaire dans son sandbox pour se rememorer les taches
    #     (aucun rapport avec un upload utilisateur sur la page cible)
    #   todo_write / todo_read / plan / planner : variantes selon versions
    NON_INTERACTION = {
        "done", "read_content", "assess", "think", "note",
        "write_file", "read_file", "file_write", "file_read",
        "todo_write", "todo_read", "plan", "planner", "planning",
        "remember", "memorize", "log",
    }
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
        # Lectures DOM read-only - BU utilise plusieurs noms selon version
        # (extract 0.12, extract_content 0.11, find_elements 0.13). Toutes
        # sont marquees included_in_replay=False + cleanup_reason (aucune
        # interaction utilisateur reproductible, valeur = lecture LLM only).
        "extract": "extract",
        "extract_content": "extract",
        "find_elements": "extract",
        "find_element": "extract",
        "get_dom_state": "extract",
        "query_selector": "extract",
        "search_page": "extract",
        "read_dom": "extract",
        "get_page_content": "extract",
        # BU 0.13 evaluate : execution JS brute (workaround click quand
        # selecteur ambigu ou element hors-flow). Rejouable en TS via
        # page.evaluate() - c'est une vraie interaction reproductible.
        "evaluate": "evaluate",
        "execute_javascript": "evaluate",
        "run_js": "evaluate",
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

    def _elements_compatible(bu_elem: dict | None, dom_entry: dict) -> bool:
        """Verifie que l'element BU (interacted_element) et le DOM entry
        parlent du meme element. Cas d'usage : BU click index=762
        (aria-label='Close ad', id='dismiss-button') matche par erreur un
        DOM event Delete Account (href='/delete_account', aria-label=null).

        Regle STRICTE : quand BU expose des attributs, exige AU MOINS UN
        match strict (identique) sur un attr discriminant (id, name, href,
        aria-label, data-testid) OU une intersection de class non-vide.
        Sans aucun signal commun, on refuse le match : deux elements
        differents partageant juste "action=click" ne doivent pas etre
        apparies. Un mismatch prouve (valeurs differentes non-vides) est
        aussi un refus.

        BU sans interacted_element ou sans attributes -> True (pas de signal).
        """
        if not bu_elem or not isinstance(bu_elem, dict):
            return True
        bu_attrs = bu_elem.get("attributes") or {}
        if not isinstance(bu_attrs, dict) or not bu_attrs:
            return True
        dom_attrs = (dom_entry.get("attributes") or {}) if isinstance(dom_entry, dict) else {}
        if not isinstance(dom_attrs, dict) or not dom_attrs:
            return True
        # 1) Refus immediat si mismatch prouve sur un attr discriminant
        for key in ("id", "name", "href", "aria-label", "data-testid"):
            bu_v = bu_attrs.get(key)
            dom_v = dom_attrs.get(key)
            if bu_v and dom_v and bu_v != dom_v:
                return False
        # 2) Exige AU MOINS UN signal commun : soit un attr discriminant
        #    identique non-vide, soit une intersection de class.
        for key in ("id", "name", "href", "aria-label", "data-testid"):
            bu_v = bu_attrs.get(key)
            dom_v = dom_attrs.get(key)
            if bu_v and dom_v and bu_v == dom_v:
                return True
        # Classes utilitaires ultra-generiques : trop faibles pour valider un
        # match a elles seules (deux boutons distincts partagent souvent 'btn').
        _CLASS_BLACKLIST = {
            "btn", "button", "form-control", "container", "row", "col", "block",
            "inline", "active", "primary", "secondary", "default", "success",
            "danger", "warning", "info", "link", "text", "input", "form",
            "flex", "grid", "clearfix", "hidden", "visible", "show", "d-block",
            "d-none", "text-center", "text-left", "text-right",
        }
        bu_cls = set((bu_attrs.get("class") or "").split()) - _CLASS_BLACKLIST
        dom_cls = set((dom_attrs.get("class") or "").split()) - _CLASS_BLACKLIST
        if bu_cls and dom_cls and (bu_cls & dom_cls):
            return True
        # 3) Aucun signal commun -> refus
        return False

    def _find_matching_dom(normalized_action: str, window_start: int | None,
                           window_end: int | None,
                           bu_elem: dict | None = None) -> int | None:
        """Cherche un DOM entry non-consomme, meme action, dans la fenetre.
        Si bu_elem fourni, valide la compatibilite des attributs pour
        eviter un faux match cross-element (ex: BU sur 'Continue' colle
        a un DOM event 'Create Account')."""
        for i, d in enumerate(dom_entries):
            if dom_consumed[i]:
                continue
            if d["ts_ms"] is None:
                continue
            if window_start is not None and d["ts_ms"] < window_start:
                continue
            if window_end is not None and d["ts_ms"] > window_end:
                continue
            if (d["entry"].get("action") or "").lower() != normalized_action:
                continue
            if not _elements_compatible(bu_elem, d["entry"]):
                continue
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
        # BU results par action_index : sert a detecter les actions qui ont
        # echoue (error != None) - ces actions ne doivent PAS devenir des
        # steps rejouables. Ex: BU16 click Delete Account -> results.error
        # 'may be stale' = tentative ratee, page deja navigue.
        step_results = h.get("results") or []
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

            # Skip deterministe : action BU qui a echoue (results.error).
            # Ex: BU16 Delete Account 'may be stale' apres nav BU15 -> ne
            # doit PAS devenir un step Playwright (bouton disparu au replay).
            a_idx = na.get("action_index")
            if isinstance(a_idx, int) and 0 <= a_idx < len(step_results):
                res = step_results[a_idx]
                if isinstance(res, dict) and res.get("error"):
                    continue

            # Element APPARIE a cette action precise (pas step-level)
            per_action_element = na.get("interacted_element")

            step: Step | None = None
            # keyboard/check/uncheck ajoutes : le DOM listener capture ces
            # events aussi (keydown Enter/Tab, change checkbox) - on doit
            # dedup contre le BU pour ne pas emit 2 fois la meme action.
            if normalized in ("click", "input", "scroll", "select", "hover", "keyboard", "check", "uncheck"):
                matched_idx = _find_matching_dom(normalized, window_start, window_end, per_action_element)
                if matched_idx is not None:
                    dom_consumed[matched_idx] = True
                    step = _step_from_dom_entry(
                        dom_entries[matched_idx]["entry"], _next_index()
                    )
                    step.source = "bu+dom"
                    # Conserver interacted_element COMPLET dans raw_payload
                    # (attributes, ax_name, x_path, stable_hash, backend_node_id,
                    # bounds, node_name, node_value...). Source de verite BU.
                    step.raw_payload = {
                        "bu_action": action_dict,
                        "action_index": na.get("action_index"),
                        "interacted_element": per_action_element,
                    }
                    # BU = source de verite : sa cascade prioritaire (id >
                    # data-testid > data-qa > data-* > aria-label > ax_name)
                    # ecrase le selecteur DOM heuristique quand disponible.
                    # Le sel DOM reste en fallback si BU n'a rien.
                    _saved_sel = step.selector
                    _apply_interacted_element(step, per_action_element)
                    if step.selector is None:
                        step.selector = _saved_sel
                    _mark_ad_related(step, per_action_element)
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
    #    Sources possibles : event auto-fire du site (JS interne, animation),
    #    event bubble d'un click BU capte sur un parent, bug de matching
    #    temporel BU/DOM. Aucun cas legitime a rejouer -> supprimes du
    #    clean_steps. Regle deterministe : pas de trace BU derriere ->
    #    pas dans le clean_steps ni dans le TS. Le LLM ne decide rien ici.
    #    (Les DOM entries consommes par un BU step restent visibles en tant
    #    que source='bu+dom' des steps existants.)

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

    # 5bis. FUSION checkbox/radio : quand un click + change (+ evaluate BU)
    # arrivent proches sur la meme famille selecteur, ces 3 sources
    # observent UNE SEULE interaction utilisateur. Le change porte la
    # semantique (checked bool) + parentLabel, on le garde comme step
    # canonique. Le click et l'evaluate sont marques included_in_replay=
    # False + raw_payload.fused_sources trace les preuves fusionnees.
    _fuse_checkbox_interactions(steps)

    # 5ter. FUSION GENERIQUE : les evaluate JS BU qui font click()/type()
    # sur un element deja capture par le DOM listener (workaround agent
    # apres echec du click direct). Rejouer les 2 = double action.
    _fuse_evaluate_workarounds(steps)

    # 6. Rapprochement network
    if network_log:
        _link_network_to_steps(steps, network_log)

    # 7. Renumbering final
    for i, s in enumerate(steps, start=1):
        s.id = f"step-{i:04d}"
        s.step = i

    return steps


def _step_selector_value(step: Step) -> str | None:
    sel = step.selector
    if sel is None:
        return None
    if isinstance(sel, str):
        return sel
    return getattr(sel, "value", None)


def _fuse_checkbox_interactions(steps: list[Step]) -> None:
    """Fusion des observations multiples d'une SEULE interaction checkbox.

    Contexte : un toggle checkbox produit dans le brut :
      - 1 DOM click (parfois sur selecteur ambigu ex [aria-label="Toggle Todo"])
      - 1 DOM change (avec checked bool + parentLabel + accessibleName)
      - eventuellement 1 BU evaluate (workaround JS si le click direct BU
        n'a rien fait a cause de l'ambiguite)

    Ces trois sources decrivent LA MEME action. Rejouer les trois donne
    des double-clicks / triple toggles imprevisibles. Le nettoyage doit
    produire UN SEUL step canonique.

    Strategie :
      - Le step 'check' ou 'uncheck' (issu du DOM change) est canonique :
        il porte la semantique reelle (checked bool) + parentLabel +
        accessibleName + selecteur exact. C'est celui qu'on rejoue.
      - Le click DOM avec selecteur identique dans une fenetre FUSE_WINDOW_MS
        est fusionne : included_in_replay=False + cleanup_reason.
      - L'evaluate BU avec code JS qui manipule .toggle/.checked/.click()
        dans la meme fenetre est fusionne aussi.
      - raw_payload.fused_sources trace les IDs des steps fusionnes pour
        audit et rapport.
    """
    FUSE_WINDOW_MS = 2000
    for i, canonical in enumerate(steps):
        if canonical.action not in ("check", "uncheck"):
            continue
        ts = canonical.timestamp
        if ts is None:
            continue
        canonical_sel = _step_selector_value(canonical)
        fused = []
        for j, other in enumerate(steps):
            if j == i or other.timestamp is None:
                continue
            if not other.included_in_replay:
                continue
            if abs(other.timestamp - ts) > FUSE_WINDOW_MS:
                continue
            # DOM click sur meme selecteur ou dans le meme sous-arbre
            if other.action == "click":
                other_sel = _step_selector_value(other)
                if other_sel and canonical_sel and (
                    other_sel == canonical_sel
                    or other_sel in canonical_sel
                    or canonical_sel in other_sel
                ):
                    other.included_in_replay = False
                    other.cleanup_reason = (
                        f"fusionne dans {canonical.id} (canonique check/uncheck "
                        f"avec semantique checked + parentLabel)"
                    )
                    fused.append(other.id)
            # BU evaluate qui touche a un toggle/checkbox
            elif other.action == "evaluate":
                code = ((other.value or "") + " " + (other.description or "")).lower()
                if any(kw in code for kw in (".toggle", ".checked", "checkbox", "querySelectorAll".lower())):
                    if ".click()" in code or ".checked" in code:
                        other.included_in_replay = False
                        other.cleanup_reason = (
                            f"fusionne dans {canonical.id} (workaround JS "
                            f"pour la meme interaction, deja capturee par le change)"
                        )
                        fused.append(other.id)
        if fused:
            raw = canonical.raw_payload or {}
            if isinstance(raw, dict):
                raw["fused_sources"] = fused
                canonical.raw_payload = raw


def _fuse_evaluate_workarounds(steps: list[Step]) -> None:
    """Fusion generique : un evaluate BU qui manipule un element (via
    .click(), .value=, .checked=, .dispatchEvent...) alors qu'un DOM
    click/input/check/uncheck adjacent dans la meme fenetre a deja
    capture l'interaction reelle -> l'evaluate est le WORKAROUND du meme
    intent user. Le rejouer PLUS le DOM canonique = double action.

    Regle : garder le DOM canonique (source riche, selecteur validee
    runtime), marquer l'evaluate included_in_replay=False + reason.

    Ne s'applique QUE si l'evaluate n'a pas deja ete fusionne par le
    passe checkbox (car le pattern _fuse_checkbox_interactions est plus
    strict avec le contexte parentLabel).
    """
    FUSE_WINDOW_MS = 3000
    for i, ev in enumerate(steps):
        if ev.action != "evaluate":
            continue
        if not ev.included_in_replay:
            continue  # deja fusionne par le passe checkbox
        code_raw = (ev.value or "") + " " + (ev.description or "")
        code = code_raw.lower()
        # REGLE INCONDITIONNELLE : evaluate qui contient un workflow entier
        # (multi-clicks ou primitives async) => JAMAIS rejouable en TS.
        # Ces blobs font plusieurs actions en background via setTimeout,
        # sans coordination avec le driver Playwright -> race conditions
        # garanties avec les steps canoniques suivants (cas todomvc step 12
        # qui click Clear completed via setTimeout avant que Playwright
        # atteigne le step 17 canonique -> bouton disparu -> timeout).
        n_clicks = code.count(".click(")
        has_async = ("settimeout" in code or "setinterval" in code
                     or "await " in code or ".then(" in code or "promise" in code)
        if n_clicks >= 2 or has_async:
            ev.included_in_replay = False
            reasons = []
            if n_clicks >= 2:
                reasons.append(f"multi-clicks dans le blob ({n_clicks})")
            if has_async:
                reasons.append("primitives async (setTimeout/Promise) non attendues par Playwright")
            ev.cleanup_reason = (
                "evaluate workflow non-rejouable : "
                + ", ".join(reasons)
                + " - les steps canoniques adjacents couvrent l'intent"
            )
            continue
        ts = ev.timestamp
        if ts is None:
            continue
        # Heuristique : l'evaluate manipule un element interactif
        touches_dom = (
            ".click()" in code or ".value" in code or ".checked" in code
            or ".dispatchevent" in code or ".submit()" in code or ".focus()" in code
        )
        if not touches_dom:
            continue
        # PROTECTION : un evaluate qui ecrit >=2 champs (.value= ou .checked=)
        # est un blob de remplissage formulaire complet (ex: gender radio +
        # password + adresse + submit d'un signup). Intent DIFFERENT d'un
        # simple click canonique adjacent. Le fusionner avec le click qui
        # ouvre le formulaire laisserait le form vide au replay.
        n_field_writes = code.count(".value =") + code.count(".value=") + code.count(".checked =") + code.count(".checked=")
        if n_field_writes >= 2:
            # Blob remplissage formulaire complet : intent different d'un
            # click canonique adjacent. Ne pas fusionner l'evaluate (il
            # doit rester actif pour remplir les champs au replay). Les
            # eventuels doublons du click interne .click() sont deja
            # elimines : ils apparaissent comme dom_orphan (pas de BU action
            # correspondante) et sont supprimes par la regle dom_orphan.
            continue
        # Cherche un step DOM canonique (click/input/check/uncheck)
        # dans la fenetre temporelle qui refleterait le meme intent
        for j, other in enumerate(steps):
            if j == i or not other.included_in_replay:
                continue
            if other.timestamp is None or abs(other.timestamp - ts) > FUSE_WINDOW_MS:
                continue
            # Source riche : DOM listener direct ou BU+DOM fusionne
            if other.source not in ("dom_listener", "bu+dom"):
                continue
            if other.action in ("click", "input", "check", "uncheck", "select"):
                ev.included_in_replay = False
                ev.cleanup_reason = (
                    f"fusionne avec {other.id} (evaluate JS workaround dont "
                    f"l'intent est deja capture par un {other.action} canonique)"
                )
                # Trace inverse sur le canonique
                raw = other.raw_payload or {}
                if isinstance(raw, dict):
                    fused = list(raw.get("fused_sources") or [])
                    if ev.id not in fused:
                        fused.append(ev.id)
                    raw["fused_sources"] = fused
                    other.raw_payload = raw
                break


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

def detect_and_flag_sensitive(steps: list[Step]) -> list[dict]:
    """Pour chaque step input avec sensitive=True, assigne un nom d'env
    var stable (DOMAUTOPSY_STEP_XXXX) que le TS Playwright utilisera.
    Retourne une liste de dicts {name, step, selector, page, description}
    pour permettre au caller d'afficher un message pedagogique detaille."""
    env_vars: list[dict] = []
    for s in steps:
        if s.action == "input" and s.sensitive and s.env_var is None:
            name = f"DOMAUTOPSY_STEP_{s.step or 0:04d}"
            s.env_var = name
            sel_val = None
            if s.selector is not None:
                if isinstance(s.selector, str):
                    sel_val = s.selector
                else:
                    sel_val = getattr(s.selector, "value", None)
            env_vars.append({
                "name": name,
                "step": s.step,
                "selector": sel_val or "?",
                "page": s.page or s.url or "?",
                "description": (s.description or "")[:120],
            })
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


def deterministic_classify_steps(
    steps: list[Step],
) -> tuple[list[Step], list[str], list[str]]:
    """Classifie included_in_replay + anomalies + filtered_noise SANS AUCUN
    appel LLM ni reseau. Retourne (steps annotes, anomalies, filtered_noise).

    Regles conservatrices : par defaut included_in_replay=True. Marque False
    UNIQUEMENT quand une regle explicite matche. Preserve les fusions deja
    posees en amont (checkbox setChecked, evaluate) qui ont deja pose False.

    Regles de rejet :
      R1  clics consecutifs identiques (meme selector, meme page, < 500ms) :
          garde le premier, marque les suivants False + "clic consecutif redondant"
      R2  click sans selector exploitable : action=click ET selector absent /
          value vide -> False + "clic sans selecteur"
      R3  input sans valeur : action in {input, setText} ET value vide -> False
          + "saisie vide"

    Anomalies remontees (sans modifier les steps) :
      A1  selector non-unique (unique=False OR matchCount > 1) :
          "step-XXXX : selecteur non-unique (matchCount=N)"
      A2  step interactif sans selector : "step-XXXX : action X sans selecteur"
      A3  source cdp_fallback : "step-XXXX : capture via fallback CDP (fragile)"
    """
    if not steps:
        return steps, [], []

    anomalies: list[str] = []
    filtered_noise_counter: dict[str, int] = {}

    def _sel_value(s: Step) -> str | None:
        sel = s.selector
        if sel is None:
            return None
        if isinstance(sel, str):
            return sel or None
        return getattr(sel, "value", None) or None

    def _sel_unique(s: Step) -> bool | None:
        sel = s.selector
        if sel is None or isinstance(sel, str):
            return None
        return getattr(sel, "unique", None)

    def _sel_matchcount(s: Step) -> int | None:
        sel = s.selector
        if sel is None or isinstance(sel, str):
            return None
        return getattr(sel, "matchCount", None)

    INTERACTIVE_ACTIONS = {"click", "input", "setText", "select", "hover",
                           "check", "uncheck", "setChecked", "upload"}

    prev_click_key = None
    prev_click_ts: int | None = None
    for s in steps:
        if getattr(s, "included_in_replay", True) is False:
            continue

        act = s.action
        sel_val = _sel_value(s)
        val = getattr(s, "value", None)

        # R1 : clic consecutif identique
        if act == "click" and sel_val:
            key = (s.page or "", sel_val)
            ts = s.timestamp
            if prev_click_key == key and ts and prev_click_ts and (ts - prev_click_ts) < 500:
                s.included_in_replay = False
                s.cleanup_reason = "clic consecutif redondant (<500ms sur meme selecteur)"
                filtered_noise_counter[sel_val] = filtered_noise_counter.get(sel_val, 0) + 1
                continue
            prev_click_key = key
            prev_click_ts = ts
        else:
            prev_click_key = None
            prev_click_ts = None

        # R2 : click sans selector
        if act == "click" and not sel_val:
            s.included_in_replay = False
            s.cleanup_reason = "clic sans selecteur exploitable"
            continue

        # R3 : input vide
        if act in ("input", "setText") and (val is None or val == ""):
            s.included_in_replay = False
            s.cleanup_reason = "saisie vide"
            continue

        # Anomalies (sans modifier included_in_replay)
        sid = s.id or f"step-?({act})"
        if act in INTERACTIVE_ACTIONS:
            unique = _sel_unique(s)
            mc = _sel_matchcount(s)
            if unique is False or (mc is not None and mc > 1):
                anomalies.append(f"{sid} : selecteur non-unique (matchCount={mc})")
            if sel_val is None and act != "upload":
                anomalies.append(f"{sid} : {act} sans selecteur")
        if getattr(s, "source", None) == "cdp_fallback":
            anomalies.append(f"{sid} : capture via fallback CDP (selecteur fragile)")

    filtered_noise = [
        f"{count+1} clics consecutifs sur {sel} (garde le 1er)"
        for sel, count in filtered_noise_counter.items()
    ]
    return steps, anomalies, filtered_noise


def ai_classify_steps(
    steps: list[Step],
    scenario_steps: list[dict[str, Any]] | None,
    model: str,
    base_url: str | None,
    api_key: str | None,
    use_llm: bool = False,
    llm_timeout_s: float = 30.0,
) -> tuple[list[Step], list[str], list[str]]:
    """Classifie les steps. Par defaut (use_llm=False) : 100% deterministe,
    zero appel reseau, retourne en <1s meme sur 200 steps.

    Si use_llm=True : lance d'abord la classif deterministe (baseline safe),
    puis un appel LLM avec TIMEOUT COURT (30s default) pour raffiner sur les
    steps ambigus. En cas d'echec/timeout LLM, le baseline deterministe est
    garanti - le JSON, le TS et les exports restent generables meme si
    OpenAI ne repond pas apres la fin de Browser Use.
    """
    if not steps:
        return steps, [], []

    # Phase 1 : classification deterministe (toujours, garantie 0 reseau)
    steps, det_anomalies, det_noise = deterministic_classify_steps(steps)

    if not use_llm:
        return steps, det_anomalies, det_noise

    # Phase 2 (optionnelle) : raffinement LLM sur les steps encore inclus
    # (les False deterministes sont ACQUIS et ne sont jamais reevalues).
    ambiguous = [s for s in steps if getattr(s, "included_in_replay", True) is True]
    if not ambiguous:
        return steps, det_anomalies, det_noise

    projected = [
        {
            "step_id": s.id,
            "action": s.action,
            "description": s.description,
            "selector": (s.selector.value if s.selector and not isinstance(s.selector, str) else s.selector),
            "unique": _sel_unique_flat(s),
            "matchCount": _sel_matchcount_flat(s),
            "page": s.page,
            "source": s.source,
        }
        for s in ambiguous
    ]

    user_content = (
        (f"SCENARIO ATTENDU :\n{json.dumps(scenario_steps, ensure_ascii=False)}\n\n" if scenario_steps else "")
        + f"STEPS A CLASSIFIER :\n{json.dumps(projected, ensure_ascii=False)}"
    )

    client_kwargs = {"timeout": llm_timeout_s}
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
            timeout=llm_timeout_s,
        )
        raw = response.choices[0].message.content.strip()
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1]
        if raw.endswith("```"):
            raw = raw.rsplit("```", 1)[0]
        parsed = json.loads(raw.strip())
    except Exception as e:
        # Fallback : deterministic baseline garde ses decisions,
        # les steps ambigus restent inclus (needs_review implicite via anomalie)
        return steps, det_anomalies + [
            f"[needs_review] Raffinement LLM en echec sur {len(ambiguous)} steps ambigus : {type(e).__name__}"
        ], det_noise

    # Applique les classifications LLM sur les steps ambigus UNIQUEMENT
    by_id = {s.id: s for s in ambiguous}
    for c in (parsed.get("classifications") or []):
        sid = c.get("step_id")
        s = by_id.get(sid)
        if not s:
            continue
        included = c.get("included_in_replay")
        if included is False:
            s.included_in_replay = False
            if c.get("cleanup_reason"):
                s.cleanup_reason = c["cleanup_reason"][:200]

    llm_anomalies = [a for a in (parsed.get("anomalies") or []) if isinstance(a, str)]
    llm_noise = [f for f in (parsed.get("filtered_noise") or []) if isinstance(f, str)]
    return steps, det_anomalies + llm_anomalies, det_noise + llm_noise


def _sel_unique_flat(s: Step) -> bool | None:
    sel = s.selector
    if sel is None or isinstance(sel, str):
        return None
    return getattr(sel, "unique", None)


def _sel_matchcount_flat(s: Step) -> int | None:
    sel = s.selector
    if sel is None or isinstance(sel, str):
        return None
    return getattr(sel, "matchCount", None)


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
    use_llm: bool = False,
    llm_timeout_s: float = 30.0,
    detect_sensitive: bool = True,
) -> tuple[CleanSteps, list[str]]:
    """Orchestration complete de la construction du clean_steps.json enrichi.

    Par defaut (use_llm=False) : 100% DETERMINISTE, ZERO appel reseau.
    Le JSON, le TS et les exports sont generables meme si OpenAI ne repond
    plus apres la fin de Browser Use. Termine en <1s meme sur 200 steps.

    Si use_llm=True : classif deterministe d'abord (baseline safe),
    puis raffinement LLM avec timeout court + fallback needs_review.

    Retourne (CleanSteps valide, list des env_vars sensibles a configurer).
    """
    # 1. Fusion multi-sources (pure Python, aucun reseau)
    steps = build_pre_cleanup_steps(scenario_steps, bu_history, dom_log, network_log)

    # 2. Detection sensitive -> env_var (pattern matching local, aucun reseau).
    # SKIP en bench_mode (detect_sensitive=False) : les replays doivent etre
    # autonomes sans env var externes, et les credentials du corpus public
    # (rw7-01 saucedemo 'secret_sauce', rw7-05 heroku 'SuperSecretPassword!'
    # ...) sont deja publiques dans le confirmed_task.
    sensitive_env_vars = detect_and_flag_sensitive(steps) if detect_sensitive else []

    # 3. Classification : deterministe garantie + LLM optionnel avec timeout
    steps, anomalies, filtered_noise = ai_classify_steps(
        steps, scenario_steps, model=model, base_url=base_url, api_key=api_key,
        use_llm=use_llm, llm_timeout_s=llm_timeout_s,
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
