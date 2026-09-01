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
  4. classify_steps(steps)
     -> politique locale stricte : aucune decision LLM, aucun reseau
  5. CleanSteps.model_validate(...)
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
    # BU 0.13.8 fournit la representation canonique sur la dataclass elle-
    # meme. Elle contient notamment backend_node_id, frame_id, x_path,
    # element_hash, stable_hash, bounds et ax_name. La consulter AVANT un
    # eventuel model_dump partiel evite de ne conserver que ``attributes``.
    if hasattr(elem, "to_dict"):
        try:
            value = elem.to_dict()
            if isinstance(value, dict):
                return value
        except Exception:
            pass
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
    for attr in ("node_id", "backend_node_id", "frame_id", "node_type",
                 "node_value", "node_name", "attributes", "xpath", "x_path",
                 "css_selector", "tag_name", "element_hash", "stable_hash",
                 "bounds", "ax_name", "is_visible", "is_interactive",
                 "shadow_root", "highlight_index"):
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
    measured = sel.get("unique") is not None and sel.get("matchCount") is not None
    return Selector(
        strategy=sel.get("strategy"),
        value=sel.get("value"),
        inShadowDOM=bool(sel.get("inShadowDOM")),
        unique=sel.get("unique"),
        matchCount=sel.get("matchCount"),
        shadowChain=sel.get("shadowChain"),
        playwrightSelector=sel.get("playwrightSelector"),
        jsSelector=sel.get("jsSelector"),
        verifiedAtCapture=measured,
        captureSource="dom_listener" if measured else "dom_listener_unverified",
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
    raw = {
        "tag": entry.get("tag"),
        "attributes": entry.get("attributes") if isinstance(entry.get("attributes"), dict) else {},
    }
    if "isTrusted" in entry:
        raw["isTrusted"] = bool(entry.get("isTrusted"))
    if entry.get("parentLabel"):
        raw["parentLabel"] = entry["parentLabel"]
        raw["parentLabelMatchCount"] = entry.get("parentLabelMatchCount")
        raw["parentScopedMatchCount"] = entry.get("parentScopedMatchCount")
        raw["parentCheckboxMatchCount"] = entry.get("parentCheckboxMatchCount")
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
        included_in_replay=True,  # sera valide ou exclu par classify_steps
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
        # Conserve le code pour le diagnostic, mais classify_steps l'exclut
        # systematiquement : un script arbitraire n'est pas un replay canonique.
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
    elif normalized in ("click", "input", "select", "scroll", "hover", "check", "uncheck"):
        # Actions interactives BU sans correspondance DOM listener.
        # interacted_element a peut-etre fourni le selecteur.
        if normalized == "input":
            step.value = params.get("text") or params.get("value")
        elif normalized == "select":
            step.value = params.get("value") or params.get("option") or params.get("text")
        elif normalized == "scroll":
            step.direction = "down" if (params.get("direction") in (None, "down", "bas")) else "up"
            step.deltaY = params.get("amount") or params.get("delta") or 650
        elif normalized in ("check", "uncheck"):
            step.value = "true" if normalized == "check" else "false"
        step.description = f"{normalized.capitalize()} (BU-only, sans DOM event)"
        # Regle cahier : "action sans selecteur conservee comme anomalie".
        # Le step est GARDE dans le JSON (tracabilite) mais marque
        # included_in_replay=False + cleanup_reason quand aucun selecteur
        # n'a pu etre extrait (ni DOM ni interacted_element BU). Le TS
        # emit un commentaire SKIPPED, pas un throw fatal.
        if normalized != "scroll" and (
            step.selector is None or (
                hasattr(step.selector, "value") and not step.selector.value
            )
        ):
            step.included_in_replay = False
            step.replay_blocking = True
            step.cleanup_reason = (
                f"action {normalized} sans selecteur (BU sans interacted_element "
                f"ni correspondance DOM listener)"
            )
    else:
        step.action = "unknown"
        step.description = f"Action browser-use non standard : {action_name}"

    step.page = current_url
    return step


def _css_attr_selector(name: str, value: str) -> str:
    """Construit un selecteur d'attribut CSS correctement echappe.

    ``[id="customer.firstName"]`` reste valide alors que
    ``#customer.firstName`` ne cible pas le meme identifiant.
    """
    return f"[{name}={json.dumps(str(value), ensure_ascii=False)}]"


def _verified_selector_from_element(elem: dict[str, Any] | None) -> Selector | None:
    """Choisit le meilleur candidat *mesure* pendant la capture.

    La stabilite et la priorite ne sont que des criteres de tri. La preuve
    indispensable est ``verifiedAtCapture=true, unique=true, matchCount=1``.
    """
    if not elem or not isinstance(elem, dict):
        return None
    candidates = elem.get("selector_candidates") or []
    valid = [
        candidate for candidate in candidates
        if isinstance(candidate, dict)
        and isinstance(candidate.get("value"), str)
        and candidate.get("value")
        and candidate.get("verifiedAtCapture") is True
        and candidate.get("unique") is True
        and candidate.get("matchCount") == 1
        and candidate.get("stability") in ("high", "medium")
    ]
    if not valid:
        return None
    stability_rank = {"high": 0, "medium": 1, "low": 2}
    valid.sort(key=lambda candidate: (
        stability_rank.get(candidate.get("stability"), 9),
        int(candidate.get("priority", 999)),
        len(candidate["value"]),
    ))
    candidate = valid[0]
    return Selector(
        strategy=candidate.get("strategy") or "browser-use-live",
        value=candidate["value"],
        unique=True,
        matchCount=1,
        inShadowDOM=bool(candidate.get("inShadowDOM")),
        shadowChain=candidate.get("shadowChain"),
        verifiedAtCapture=True,
        stability=candidate.get("stability"),
        priority=candidate.get("priority"),
        captureSource="browser_use_live_cdp",
    )


def _selector_is_verified_unique(selector: Selector | str | None) -> bool:
    if selector is None or isinstance(selector, str):
        return False
    return (
        selector.verifiedAtCapture is True
        and selector.unique is True
        and selector.matchCount == 1
    )


def _selector_rank(selector: Selector | str | None) -> tuple[int, int, int]:
    """Classe deux preuves uniques sans inventer de nouveau locator."""
    if selector is None or isinstance(selector, str):
        return (99, 999, 9999)
    explicit = {"high": 0, "medium": 1, "low": 2}.get(selector.stability)
    strategy = (selector.strategy or "").lower()
    if explicit is None:
        if any(token in strategy for token in ("testid", "data-test", "data-qa", "id", "name", "aria")):
            explicit = 0
        elif any(token in strategy for token in ("placeholder", "href", "data-attr", "ancestor", "title")):
            explicit = 1
        else:
            explicit = 2
    priority = selector.priority if selector.priority is not None else 999
    return (explicit, priority, len(selector.value or ""))


def _apply_interacted_element(step: Step, elem: dict[str, Any] | None) -> None:
    """Applique une preuve live BU, ou conserve un indice non verifie.

    Les attributs de ``DOMInteractedElement`` ne prouvent pas qu'un locator
    est unique. Sans candidat mesure live, on garde donc un locator de trace
    marque ``verifiedAtCapture=False`` ; le classificateur local l'exclura du
    replay. ``ax_name`` n'est jamais transforme en ``aria-label`` : ces deux
    notions ne sont pas equivalentes.
    """
    if not elem or not isinstance(elem, dict):
        return
    attrs = elem.get("attributes") or {}
    if not isinstance(attrs, dict):
        attrs = {}
    ax_name = elem.get("ax_name") if isinstance(elem.get("ax_name"), str) else None

    verified = _verified_selector_from_element(elem)
    if verified is not None:
        step.selector = verified
        candidate = next(
            candidate for candidate in elem.get("selector_candidates", [])
            if isinstance(candidate, dict) and candidate.get("value") == verified.value
        )
        step.selectorType = candidate.get("selectorType", "css")
    else:
        def _set_unverified(strategy: str, value: str, selector_type: str = "css") -> None:
            step.selector = Selector(
                strategy=strategy,
                value=value,
                unique=None,
                matchCount=None,
                verifiedAtCapture=False,
                captureSource="browser_use_unverified_hint",
            )
            step.selectorType = selector_type  # type: ignore[assignment]

        css = elem.get("css_selector")
        xpath = elem.get("xpath") or elem.get("x_path")
        eid = attrs.get("id")
        if isinstance(css, str) and css:
            _set_unverified("bu-unverified-css", css)
        elif isinstance(eid, str) and eid.strip():
            _set_unverified("bu-unverified-id", _css_attr_selector("id", eid))
        else:
            attribute_hint = next((
                (name, value) for name, value in attrs.items()
                if isinstance(name, str)
                and (name.startswith("data-") or name in ("aria-label", "name"))
                and isinstance(value, str) and value and len(value) <= 100
            ), None)
            if attribute_hint:
                name, value = attribute_hint
                _set_unverified("bu-unverified-attr", _css_attr_selector(name, value))
            elif isinstance(xpath, str) and xpath:
                _set_unverified("bu-unverified-xpath", xpath, "xpath")

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
    """Promeut uniquement vers un candidat BU mesure unique live."""
    cur = step.selector
    if _selector_is_verified_unique(cur):
        return False
    verified = _verified_selector_from_element(elem)
    if verified is None:
        return False
    step.selector = verified
    step.selectorType = "xpath" if verified.strategy == "xpath" else "css"
    return True


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
        # BU 0.13 evaluate : conserve pour audit, puis exclu du replay par
        # classify_steps (aucun JavaScript arbitraire dans le TS canonique).
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
        # role/type ne sont que des gardes : leur egalite est trop courante
        # pour prouver l'identite (plusieurs buttons/checkboxes par page).
        for key in ("role", "type"):
            bu_v = bu_attrs.get(key)
            dom_v = dom_attrs.get(key)
            if bu_v and dom_v and bu_v != dom_v:
                return False
        discriminants = {
            key for key in set(bu_attrs) | set(dom_attrs)
            if key in ("id", "name", "href", "aria-label", "placeholder",
                       "data-testid", "data-qa", "data-test", "data-cy")
            or key.startswith("data-")
        }
        for key in discriminants:
            bu_v = bu_attrs.get(key)
            dom_v = dom_attrs.get(key)
            if bu_v and dom_v and bu_v != dom_v:
                return False
        # 2) Exige AU MOINS UN signal commun : soit un attr discriminant
        #    identique non-vide, soit une intersection de class.
        for key in discriminants:
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
            # Les actions d'un meme step BU partagent souvent la meme grande
            # fenetre temporelle. Les consommer dans l'ordre DOM conserve
            # alors l'alignement action[0] -> event[0], action[1] -> event[1].
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
                    dom_context = step.raw_payload if isinstance(step.raw_payload, dict) else {}
                    step.raw_payload = {
                        **dom_context,
                        "bu_action": action_dict,
                        "action_index": na.get("action_index"),
                        "interacted_element": per_action_element,
                    }
                    # Un selecteur DOM deja mesure unique est une preuve et
                    # ne doit jamais etre ecrase par un simple attribut BU.
                    # Le candidat BU ne gagne que s'il a lui aussi ete mesure
                    # live et que le DOM listener n'avait pas cette preuve.
                    _saved_sel = step.selector
                    _saved_type = step.selectorType
                    live_selector = _verified_selector_from_element(per_action_element)
                    if live_selector is not None and (
                        not _selector_is_verified_unique(_saved_sel)
                        or _selector_rank(live_selector) < _selector_rank(_saved_sel)
                    ):
                        step.selector = live_selector
                        step.selectorType = "xpath" if live_selector.strategy == "xpath" else "css"
                    else:
                        step.selector = _saved_sel
                        step.selectorType = _saved_type
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
            # Fenetre d'execution BU conservee pour etablir une causalite
            # exacte entre un evaluate et les events DOM synchrones qu'il a
            # effectivement declenches. Le timestamp du step seul correspond
            # au debut du lot et ne suffit pas pour les multi-actions.
            if step.source in ("browser_use_history", "bu+dom"):
                raw = step.raw_payload if isinstance(step.raw_payload, dict) else {}
                # Indispensable au rapprochement tardif lorsqu'aucun event
                # DOM n'est tombe dans la fenetre initiale.
                if isinstance(per_action_element, dict):
                    raw["interacted_element"] = per_action_element
                # Ne pollue pas les anciens payloads sans metadata : certains
                # consommateurs les utilisent comme representation brute de
                # l'action inconnue.
                if start_ms is not None or end_ms is not None:
                    raw["bu_step_start_time"] = start_ms
                    raw["bu_step_end_time"] = end_ms
                    raw["bu_action_index"] = a_idx
                if na.get("action_result") is not None:
                    raw["bu_action_result"] = na.get("action_result")
                step.raw_payload = raw
            steps.append(step)
            if step.url:
                current_url = step.url

    # 3. DOM entries orphelins : conserves pour l'audit, jamais rejoues sans
    #    une action BU correspondante. Les supprimer rendait le diagnostic
    #    impossible et contredisait la regle "ne rien inventer/perdre".
    for i, dom in enumerate(dom_entries):
        if dom_consumed[i]:
            continue
        orphan = _step_from_dom_entry(dom["entry"], _next_index())
        orphan.source = "dom_orphan"
        orphan.included_in_replay = False
        orphan.cleanup_reason = "observation DOM sans action Browser Use correspondante"
        steps.append(orphan)

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
    steps_indexed = list(enumerate(steps))
    steps_indexed.sort(key=lambda pair: (
        pair[1].timestamp if pair[1].timestamp is not None else 10 ** 18,
        pair[0],
    ))
    steps = [s for _, s in steps_indexed]

    # Les fusions ci-dessous inscrivent les IDs de leurs sources/cibles dans
    # cleanup_reason et raw_payload. Stabiliser les IDs APRES le tri mais
    # AVANT les fusions evite des references devenues fausses au renumerotage
    # final (ex. "fusionne dans step-0022" alors que la cible est step-0020).
    for i, step in enumerate(steps, start=1):
        step.id = f"step-{i:04d}"
        step.step = i

    # 5bis. RECONCILIATION tardive BU -> DOM : si le matching initial a
    # rate mais que le listener a observe un event trusted sur la meme cible,
    # le DOM fixe le nombre reel d'actions. Les intentions BU en doublon sont
    # absorbees dans les events confirmes.
    _reconcile_bu_actions_with_dom_evidence(steps)

    # 5ter. PROMOTION evaluate -> preuves DOM : un evaluate n'est jamais
    # rejoue tel quel. En revanche, les events DOM non-trusted qu'il a
    # effectivement declenches dans sa fenetre BU peuvent devenir les
    # actions canoniques deterministes.
    _promote_dom_events_confirmed_by_evaluate(steps)

    # 5quater. PREFERENCE SEMANTIQUE : si l'evaluate a d'abord promu le
    # click d'une checkbox, mais que le change check/uncheck correspondant
    # est le prochain event DOM de la meme cible, transfere la preuve vers
    # le change. Cette relation structurelle ne depend pas des timings BU.
    _prefer_semantic_checkbox_changes(steps)

    # 5quinquies. FUSION checkbox/radio : quand un click + change (+ evaluate BU)
    # arrivent proches sur la meme famille selecteur, ces 3 sources
    # observent UNE SEULE interaction utilisateur. Le change porte la
    # semantique (checked bool) + parentLabel, on le garde comme step
    # canonique. Le click et l'evaluate sont marques included_in_replay=
    # False + raw_payload.fused_sources trace les preuves fusionnees.
    _fuse_checkbox_interactions(steps)

    # 5sexies. FUSION GENERIQUE : les evaluate JS BU qui font click()/type()
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


_EVALUATE_SELECTOR_RE = re.compile(
    r"(?:querySelector|querySelectorAll)\s*\(\s*(['\"])(.*?)\1\s*\)",
    re.DOTALL,
)
_EVALUATE_ID_RE = re.compile(r"getElementById\s*\(\s*(['\"])(.*?)\1\s*\)", re.DOTALL)
_EVALUATE_MUTATION_RE = re.compile(
    r"(?:\.click\s*\(|\.checked\s*=|dispatchEvent\s*\(\s*new\s+(?:MouseEvent|Event)\s*\(\s*['\"](?:click|change)['\"])",
    re.IGNORECASE | re.DOTALL,
)
_EVALUATE_DIRECT_INDEX_RE = re.compile(
    r"querySelectorAll\s*\(\s*(['\"])(.*?)\1\s*\)\s*\[\s*(\d+)\s*\]",
    re.DOTALL,
)
_EVALUATE_COLLECTION_RE = re.compile(
    r"(?:var|let|const)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:Array\.from\s*\(\s*)?"
    r"document\.querySelectorAll\s*\(\s*(['\"])(.*?)\2\s*\)\s*\)?",
    re.DOTALL,
)
_CSS_ATTR_PART_RE = re.compile(
    r"\[([A-Za-z_:][-A-Za-z0-9_:.]*)(?:\s*=\s*(['\"])(.*?)\2)?\]"
)


def _selectors_referenced_by_evaluate(code: str | None) -> set[str]:
    """Extrait seulement les cibles DOM explicites d'un evaluate brut."""
    if not code:
        return set()
    selectors = {match.group(2) for match in _EVALUATE_SELECTOR_RE.finditer(code)}
    selectors.update(
        _css_attr_selector("id", match.group(2))
        for match in _EVALUATE_ID_RE.finditer(code)
    )
    return selectors


def _evaluate_target_limit(code: str) -> int | None:
    """Nombre maximal de cibles statiquement identifiables dans le JS.

    ``None`` signifie une boucle explicite (forEach), auquel cas tous les
    events compatibles de la fenetre peuvent etre consommes. Sans index
    explicite, on reste conservateur et n'autorise qu'une cible.
    """
    if re.search(r"\.(?:forEach|map)\s*\(", code):
        return None
    indexed: set[tuple[str, int]] = {
        (match.group(2), int(match.group(3)))
        for match in _EVALUATE_DIRECT_INDEX_RE.finditer(code)
    }
    for collection in _EVALUATE_COLLECTION_RE.finditer(code):
        var_name, selector = collection.group(1), collection.group(3)
        use_re = re.compile(
            rf"\b{re.escape(var_name)}\s*(?:\[\s*(\d+)\s*\]|\.item\s*\(\s*(\d+)\s*\))"
        )
        for use in use_re.finditer(code):
            raw_index = use.group(1) or use.group(2)
            indexed.add((selector, int(raw_index)))
    return max(1, len(indexed))


def _terminal_css_compounds(selector: str) -> list[str]:
    """Retourne les composantes terminales simples des groupes CSS."""
    out: list[str] = []
    for group in selector.split(","):
        group = group.strip()
        if not group:
            continue
        # Le dernier compound suffit pour verifier l'element event target ;
        # la causalite temporelle/non-trusted prouve le reste de la chaine.
        parts = re.split(r"\s+|\s*[>+~]\s*", group)
        tail = next((part for part in reversed(parts) if part), "")
        tail = re.sub(r":(?:nth-child|nth-of-type|eq)\([^)]*\)", "", tail)
        tail = tail.replace(":visible", "")
        if tail:
            out.append(tail)
    return out


def _dom_step_matches_evaluate_selector(step: Step, selector: str) -> bool:
    """Verifie le compound terminal CSS contre l'event DOM capture."""
    step_selector = _step_selector_value(step) or ""
    if selector == step_selector:
        return True

    raw = step.raw_payload if isinstance(step.raw_payload, dict) else {}
    attrs = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
    tag = str(raw.get("tag") or "").lower()
    classes = set(str(attrs.get("class") or "").split())

    for tail in _terminal_css_compounds(selector):
        tag_match = re.match(r"^([A-Za-z][\w-]*)", tail)
        expected_tag = tag_match.group(1).lower() if tag_match else None
        expected_id = next(iter(re.findall(r"#([A-Za-z0-9_-]+)", tail)), None)
        expected_classes = set(re.findall(r"\.([A-Za-z0-9_-]+)", tail))
        expected_attrs = [
            (match.group(1), match.group(3))
            for match in _CSS_ATTR_PART_RE.finditer(tail)
        ]
        has_signal = bool(expected_tag or expected_id or expected_classes or expected_attrs)
        if not has_signal:
            continue

        if expected_tag and tag and expected_tag != tag:
            continue
        if expected_id and attrs.get("id") != expected_id:
            continue
        if expected_classes and classes and not expected_classes.issubset(classes):
            continue
        if expected_classes and not classes:
            if not all(f".{name}" in step_selector for name in expected_classes):
                continue
        mismatch = False
        for name, value in expected_attrs:
            if name not in attrs:
                if f"[{name}" not in step_selector:
                    mismatch = True
                    break
            elif value is not None and str(attrs.get(name)) != value:
                mismatch = True
                break
        if mismatch:
            continue
        return True
    return False


def _dom_orphan_matches_evaluate_target(step: Step, selectors: set[str]) -> bool:
    if step.source != "dom_orphan" or step.included_in_replay:
        return False
    if step.action not in ("click", "check", "uncheck"):
        return False
    raw = step.raw_payload if isinstance(step.raw_payload, dict) else {}
    # Les events declenches par element.click()/dispatchEvent() sont
    # isTrusted=false. Sans cette preuve causale, on ne promeut rien.
    if raw.get("isTrusted") is not False:
        return False
    return any(_dom_step_matches_evaluate_selector(step, selector) for selector in selectors)


def _dom_orphan_is_canonical_candidate(step: Step, selectors: set[str]) -> bool:
    if not _dom_orphan_matches_evaluate_target(step, selectors):
        return False
    raw = step.raw_payload if isinstance(step.raw_payload, dict) else {}
    if step.action in ("check", "uncheck"):
        return (
            bool(raw.get("parentLabel"))
            and raw.get("parentLabelMatchCount") == 1
            and raw.get("parentCheckboxMatchCount") == 1
        ) or _selector_is_verified_unique(step.selector)
    return (
        bool(raw.get("parentLabel"))
        and raw.get("parentLabelMatchCount") == 1
        and raw.get("parentScopedMatchCount") == 1
    ) or _selector_is_verified_unique(step.selector)


_TARGET_CLASS_BLACKLIST = {
    "active", "selected", "current", "visible", "hidden", "show", "open",
    "btn", "button", "link", "input", "form-control", "container", "row",
    "col", "flex", "grid", "primary", "secondary", "default",
}


def _step_target_attributes(step: Step) -> dict[str, Any]:
    raw = step.raw_payload if isinstance(step.raw_payload, dict) else {}
    direct = raw.get("attributes")
    if isinstance(direct, dict) and direct:
        return direct
    interacted = raw.get("interacted_element")
    if isinstance(interacted, dict):
        attrs = interacted.get("attributes")
        if isinstance(attrs, dict):
            return attrs
    return {}


def _steps_have_same_target(left: Step, right: Step) -> bool:
    """Identite de cible prouvee par locator ou attribut discriminant."""
    left_selector = _step_selector_value(left) or ""
    right_selector = _step_selector_value(right) or ""
    if left_selector and right_selector and left_selector == right_selector:
        return True

    left_attrs = _step_target_attributes(left)
    right_attrs = _step_target_attributes(right)
    # ``type`` et ``role`` servent uniquement de gardes de compatibilite :
    # deux boutons (ou deux checkboxes) partagent couramment ces valeurs et
    # cela ne prouve jamais qu'ils designent le meme noeud.
    compatibility_guards = ("type", "role")
    for key in compatibility_guards:
        left_value = left_attrs.get(key)
        right_value = right_attrs.get(key)
        if left_value and right_value and left_value != right_value:
            return False

    discriminants = {
        key for key in set(left_attrs) | set(right_attrs)
        if key in ("id", "name", "href", "aria-label", "placeholder")
        or key.startswith("data-")
    }
    matched = False
    for key in discriminants:
        left_value = left_attrs.get(key)
        right_value = right_attrs.get(key)
        if left_value and right_value and left_value != right_value:
            return False
        if left_value and right_value and left_value == right_value:
            matched = True
    if matched:
        return True

    left_classes = set(str(left_attrs.get("class") or "").split()) - _TARGET_CLASS_BLACKLIST
    right_classes = set(str(right_attrs.get("class") or "").split()) - _TARGET_CLASS_BLACKLIST
    if left_classes and right_classes and left_classes & right_classes:
        return True

    # Deux locators scopes differents peuvent finir sur le meme compound
    # exact (ex. footer.footer button.clear-completed).
    if left_selector and right_selector:
        left_tail = set(_terminal_css_compounds(left_selector))
        right_tail = set(_terminal_css_compounds(right_selector))
        if left_tail & right_tail:
            return True
    return False


def _same_document(left: Step, right: Step) -> bool:
    left_url = (left.page or left.url or "").split("#", 1)[0]
    right_url = (right.page or right.url or "").split("#", 1)[0]
    return not left_url or not right_url or left_url == right_url


def _reconcile_bu_actions_with_dom_evidence(steps: list[Step]) -> None:
    """Remplace les intentions BU dupliquees par le nombre d'events reels.

    Un event DOM ``isTrusted=true`` sur la meme cible est la preuve qu'une
    action Playwright/BU a effectivement eu lieu. Quand le premier matching
    temporel l'a rate, on promeut l'event orphelin et on absorbe toutes les
    copies BU de cette cible. Ainsi deux tentatives BU + un seul event DOM
    produisent exactement un seul step de replay.
    """
    supported = {"click", "input", "select", "check", "uncheck"}
    bu_steps = [
        step for step in steps
        if step.source == "browser_use_history"
        and step.included_in_replay
        and step.action in supported
    ]
    dom_evidence = [
        step for step in steps
        if step.source in ("dom_orphan", "bu+dom")
        and step.action in supported
        and isinstance(step.raw_payload, dict)
        and step.raw_payload.get("isTrusted") is True
    ]

    for evidence in dom_evidence:
        matches = [
            bu for bu in bu_steps
            if bu.action == evidence.action
            and _same_document(bu, evidence)
            and _steps_have_same_target(bu, evidence)
        ]
        if not matches:
            continue

        def _distance(bu: Step) -> tuple[int, int]:
            raw = bu.raw_payload if isinstance(bu.raw_payload, dict) else {}
            start = raw.get("bu_step_start_time")
            end = raw.get("bu_step_end_time")
            inside = (
                isinstance(start, int)
                and isinstance(end, int)
                and evidence.timestamp is not None
                and start - 500 <= evidence.timestamp <= end + 500
            )
            distance = abs((bu.timestamp or 0) - (evidence.timestamp or 0))
            return (0 if inside else 1, distance)

        best_bu = min(matches, key=_distance)
        evidence.included_in_replay = True
        evidence.replay_blocking = False
        evidence.source = "bu+dom-reconciled"
        evidence.cleanup_reason = None
        evidence_raw = evidence.raw_payload if isinstance(evidence.raw_payload, dict) else {}
        fused_sources = list(evidence_raw.get("fused_sources") or [])

        # Reutilise le meilleur candidat live BU seulement s'il ameliore la
        # preuve DOM, exactement comme dans le matching principal.
        best_raw = best_bu.raw_payload if isinstance(best_bu.raw_payload, dict) else {}
        interacted = best_raw.get("interacted_element")
        live_selector = _verified_selector_from_element(interacted if isinstance(interacted, dict) else None)
        if live_selector is not None and (
            not _selector_is_verified_unique(evidence.selector)
            or _selector_rank(live_selector) < _selector_rank(evidence.selector)
        ):
            evidence.selector = live_selector
            evidence.selectorType = "xpath" if live_selector.strategy == "xpath" else "css"
        if isinstance(interacted, dict):
            evidence_raw["interacted_element"] = interacted

        for bu in matches:
            bu.included_in_replay = False
            bu.replay_blocking = False
            bu.cleanup_reason = f"fusionne dans {evidence.id} (event DOM trusted confirme)"
            if bu.id not in fused_sources:
                fused_sources.append(bu.id)
        evidence_raw["fused_sources"] = fused_sources
        evidence.raw_payload = evidence_raw


def _promote_dom_events_confirmed_by_evaluate(steps: list[Step]) -> None:
    """Convertit l'effet DOM prouve d'un evaluate en action canonique.

    Le JavaScript reste interdit dans le replay. Seuls des events DOM
    ``isTrusted=false`` observes dans la fenetre exacte du step BU, ciblant
    le meme compound CSS et disposant d'une preuve de ciblage unique, peuvent
    etre promus. La preference structurelle click -> change checkbox est
    appliquee ensuite par :func:`_prefer_semantic_checkbox_changes`.
    """
    TOLERANCE_MS = 500
    claimed: set[int] = set()

    for ev_index, ev in enumerate(steps):
        if ev.action != "evaluate" or not ev.included_in_replay:
            continue
        code = ev.value or ""
        selectors = _selectors_referenced_by_evaluate(code)
        if not selectors or not _EVALUATE_MUTATION_RE.search(code):
            continue
        raw = ev.raw_payload if isinstance(ev.raw_payload, dict) else {}
        start = raw.get("bu_step_start_time")
        end = raw.get("bu_step_end_time")
        if not isinstance(start, int) or not isinstance(end, int):
            continue

        candidates: list[tuple[int, Step]] = []
        for index, candidate in enumerate(steps):
            if index == ev_index or index in claimed or candidate.timestamp is None:
                continue
            if candidate.timestamp < start - TOLERANCE_MS or candidate.timestamp > end + TOLERANCE_MS:
                continue
            if _dom_orphan_is_canonical_candidate(candidate, selectors):
                candidates.append((index, candidate))
        if not candidates:
            continue

        # check/uncheck contient l'etat final ; un click checkbox est une
        # observation redondante qui provoquerait sinon un double toggle.
        semantic = [(index, step) for index, step in candidates if step.action in ("check", "uncheck")]
        chosen_pool = semantic or [(index, step) for index, step in candidates if step.action == "click"]
        target_limit = _evaluate_target_limit(code)
        chosen = chosen_pool if target_limit is None else chosen_pool[:target_limit]
        if not chosen:
            continue

        promoted_ids: list[str] = []
        for index, canonical in chosen:
            claimed.add(index)
            canonical.included_in_replay = True
            canonical.replay_blocking = False
            canonical.source = "evaluate+dom"
            canonical.cleanup_reason = None
            canonical_raw = canonical.raw_payload if isinstance(canonical.raw_payload, dict) else {}
            canonical_raw["confirmed_by_evaluate"] = ev.id
            canonical_raw["evaluate_selectors"] = sorted(selectors)
            fused_sources = list(canonical_raw.get("fused_sources") or [])
            if ev.id not in fused_sources:
                fused_sources.append(ev.id)

            # Absorbe le click synchrone de la meme checkbox.
            parent_label = canonical_raw.get("parentLabel")
            if canonical.action in ("check", "uncheck") and parent_label:
                for companion_index, companion in candidates:
                    if companion_index == index or companion_index in claimed:
                        continue
                    companion_raw = companion.raw_payload if isinstance(companion.raw_payload, dict) else {}
                    if (
                        companion.action == "click"
                        and companion_raw.get("parentLabel") == parent_label
                        and companion.timestamp is not None
                        and canonical.timestamp is not None
                        and abs(companion.timestamp - canonical.timestamp) <= TOLERANCE_MS
                    ):
                        claimed.add(companion_index)
                        companion.cleanup_reason = f"fusionne dans {canonical.id} (click compagnon du change DOM)"
                        if companion.id not in fused_sources:
                            fused_sources.append(companion.id)
            canonical_raw["fused_sources"] = fused_sources
            canonical.raw_payload = canonical_raw
            promoted_ids.append(canonical.id or f"dom-index-{index}")

        ev.included_in_replay = False
        ev.replay_blocking = False
        ev.cleanup_reason = (
            "fusionne en action(s) DOM canonique(s) confirmee(s) : "
            + ", ".join(promoted_ids)
        )


_DOM_EVENT_SOURCES = {
    "dom_listener", "dom_orphan", "bu+dom", "bu+dom-reconciled", "evaluate+dom",
}


def _same_checkbox_event_target(click: Step, semantic: Step) -> bool:
    """Identite stricte click/change sans inference temporelle."""
    click_selector = _step_selector_value(click)
    semantic_selector = _step_selector_value(semantic)
    if not click_selector or click_selector != semantic_selector:
        return False

    click_raw = click.raw_payload if isinstance(click.raw_payload, dict) else {}
    semantic_raw = semantic.raw_payload if isinstance(semantic.raw_payload, dict) else {}
    click_label = click_raw.get("parentLabel")
    semantic_label = semantic_raw.get("parentLabel")
    if not click_label or click_label != semantic_label:
        return False

    click_tag = str(click_raw.get("tag") or "").lower()
    semantic_tag = str(semantic_raw.get("tag") or "").lower()
    if not click_tag or click_tag != semantic_tag:
        return False

    click_attrs = _step_target_attributes(click)
    semantic_attrs = _step_target_attributes(semantic)
    click_type = str(click_attrs.get("type") or "").lower()
    semantic_type = str(semantic_attrs.get("type") or "").lower()
    if click_type not in ("checkbox", "radio") or click_type != semantic_type:
        return False

    # Mismatch explicite sur une caracteristique capturee = cible differente.
    for key in ("aria-label", "class", "id", "name"):
        click_value = click_attrs.get(key)
        semantic_value = semantic_attrs.get(key)
        if click_value and semantic_value and click_value != semantic_value:
            return False
    return _same_document(click, semantic)


def _prefer_semantic_checkbox_changes(steps: list[Step]) -> None:
    """Remplace un click evaluate+dom par son prochain change DOM identique.

    La sequence DOM ``click -> change(check/uncheck)`` porte deux niveaux de
    preuve : le click a ete relie a l'intention BU, le change porte l'etat
    final idempotent. Si ces deux events sont consecutifs dans le flux DOM et
    ont une identite stricte, le change herite de la preuve et devient le seul
    step rejouable.
    """
    for click_index, click in enumerate(steps):
        if (
            click.action != "click"
            or click.source != "evaluate+dom"
            or not click.included_in_replay
        ):
            continue
        click_raw = click.raw_payload if isinstance(click.raw_payload, dict) else {}
        evaluate_id = click_raw.get("confirmed_by_evaluate")
        if not evaluate_id:
            continue

        semantic: Step | None = None
        for candidate in steps[click_index + 1:]:
            if candidate.source not in _DOM_EVENT_SOURCES:
                continue
            if (
                candidate.source == "dom_orphan"
                and not candidate.included_in_replay
                and candidate.action in ("check", "uncheck")
                and _same_checkbox_event_target(click, candidate)
            ):
                semantic = candidate
            # Le premier event DOM suivant clot la paire, qu'il corresponde
            # ou non : aucune recherche lointaine susceptible de fusionner
            # deux interactions distinctes sur la meme checkbox.
            break
        if semantic is None:
            continue

        semantic_raw = semantic.raw_payload if isinstance(semantic.raw_payload, dict) else {}
        semantic_raw["confirmed_by_evaluate"] = evaluate_id
        semantic_raw["evaluate_selectors"] = list(click_raw.get("evaluate_selectors") or [])
        semantic_raw["context_proof_from_click"] = click.id
        if click_raw.get("parentLabelMatchCount") == 1:
            semantic_raw["parentLabelMatchCount"] = 1
        if click_raw.get("parentScopedMatchCount") == 1:
            semantic_raw["parentScopedMatchCount"] = 1
        fused_sources = list(semantic_raw.get("fused_sources") or [])
        for source_id in list(click_raw.get("fused_sources") or []) + [click.id]:
            if source_id and source_id not in fused_sources:
                fused_sources.append(source_id)
        semantic_raw["fused_sources"] = fused_sources
        semantic.raw_payload = semantic_raw
        semantic.included_in_replay = True
        semantic.replay_blocking = False
        semantic.source = "evaluate+dom"
        semantic.cleanup_reason = None

        click.included_in_replay = False
        click.replay_blocking = False
        click.cleanup_reason = (
            f"fusionne dans {semantic.id} (change DOM canonique avec etat checked)"
        )

        # L'evaluate doit pointer vers la cible finale, pas vers le click
        # intermediaire desormais exclu.
        for evaluate in steps:
            if evaluate.id == evaluate_id:
                evaluate.cleanup_reason = (
                    "fusionne en action DOM canonique confirmee : " + str(semantic.id)
                )
                break


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
        # Un check dom_orphan exclu n'est PAS une action canonique. L'ancienne
        # logique pouvait s'en servir pour supprimer un click evaluate+dom
        # valide, puis laisser les deux exclus : zero toggle au replay.
        if canonical.action not in ("check", "uncheck") or not canonical.included_in_replay:
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
                canonical_raw = canonical.raw_payload if isinstance(canonical.raw_payload, dict) else {}
                other_raw = other.raw_payload if isinstance(other.raw_payload, dict) else {}
                canonical_label = canonical_raw.get("parentLabel")
                other_label = other_raw.get("parentLabel")
                same_context = not canonical_label or canonical_label == other_label
                if other_sel and canonical_sel and (
                    other_sel == canonical_sel
                    or other_sel in canonical_sel
                    or canonical_sel in other_sel
                ) and same_context:
                    other.included_in_replay = False
                    other.cleanup_reason = (
                        f"fusionne dans {canonical.id} (canonique check/uncheck "
                        f"avec semantique checked + parentLabel)"
                    )
                    fused.append(other.id)
            # Evaluate fusionne UNIQUEMENT s'il reference exactement le
            # selecteur canonique. La simple proximite temporelle ou le mot
            # "checkbox" ne prouvent pas qu'il s'agit du meme element.
            elif other.action == "evaluate":
                referenced = _selectors_referenced_by_evaluate(other.value)
                if canonical_sel and canonical_sel in referenced:
                    other.included_in_replay = False
                    other.cleanup_reason = (
                        f"fusionne dans {canonical.id} (evaluate cible exactement "
                        "le checkbox canonique deja capture)"
                    )
                    fused.append(other.id)
        if fused:
            raw = canonical.raw_payload or {}
            if isinstance(raw, dict):
                merged = list(raw.get("fused_sources") or [])
                for source_id in fused:
                    if source_id not in merged:
                        merged.append(source_id)
                raw["fused_sources"] = merged
                canonical.raw_payload = raw


def _fuse_evaluate_workarounds(steps: list[Step]) -> None:
    """Fusionne un evaluate seulement avec une preuve d'identite de cible.

    L'ancienne version supprimait des blobs selon leur proximite temporelle
    et la presence de ``.click()``. Deux actions voisines ne sont pas pour
    autant la meme action. Ici un evaluate est fusionne uniquement lorsqu'un
    selecteur explicite dans son code est identique au selecteur canonique.
    Les evaluate restants seront exclus par :func:`classify_steps`.
    """
    FUSE_WINDOW_MS = 3000
    for i, ev in enumerate(steps):
        if ev.action != "evaluate" or not ev.included_in_replay or ev.timestamp is None:
            continue
        referenced = _selectors_referenced_by_evaluate(ev.value)
        if not referenced:
            continue
        for j, other in enumerate(steps):
            if j == i or not other.included_in_replay:
                continue
            if other.timestamp is None or abs(other.timestamp - ev.timestamp) > FUSE_WINDOW_MS:
                continue
            if other.source not in ("dom_listener", "bu+dom"):
                continue
            selector = _step_selector_value(other)
            if (
                other.action in ("click", "input", "check", "uncheck", "select")
                and selector and selector in referenced
            ):
                ev.included_in_replay = False
                ev.cleanup_reason = (
                    f"fusionne avec {other.id} (evaluate et step canonique "
                    "ciblent exactement le meme selecteur)"
                )
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
# 4. Classification locale stricte
# ============================================================

def classify_steps(
    steps: list[Step],
) -> tuple[list[Step], list[str], list[str]]:
    """Decide le replay sans LLM, uniquement a partir de preuves capturees.

    Une action interactive n'est incluse que si son locator a ete mesure
    unique (ou si elle dispose d'une primitive contextuelle explicite telle
    que ``parentLabel`` pour un checkbox). Une absence de preuve devient une
    anomalie auditable, jamais un ``.first()`` silencieux.
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

    interactive_actions = {
        "click", "input", "settext", "select", "hover",
        "check", "uncheck", "setchecked", "upload", "cookie",
    }

    prev_click_key = None
    prev_click_ts: int | None = None
    for s in steps:
        if getattr(s, "included_in_replay", True) is False:
            continue

        act = s.action.lower()
        sel_val = _sel_value(s)
        val = getattr(s, "value", None)
        sid = s.id or f"step-?({act})"

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

        # Le JS arbitraire n'est pas un format de replay canonique : il peut
        # masquer ses erreurs, lancer plusieurs actions et contourner les
        # garanties strictes de Playwright.
        if act == "evaluate":
            s.included_in_replay = False
            s.replay_blocking = True
            s.cleanup_reason = "evaluate JavaScript brut non canonique"
            anomalies.append(f"{sid} : evaluate brut exclu du replay")
            continue

        if act in ("input", "settext") and (val is None or val == ""):
            s.included_in_replay = False
            s.replay_blocking = True
            s.cleanup_reason = "saisie vide"
            anomalies.append(f"{sid} : saisie vide exclue du replay")
            continue

        if act in interactive_actions:
            raw = s.raw_payload if isinstance(s.raw_payload, dict) else {}
            contextual_checkbox = (
                act in ("check", "uncheck", "setchecked")
                and isinstance(raw.get("parentLabel"), str)
                and bool(raw.get("parentLabel"))
                and raw.get("parentLabelMatchCount") == 1
                and (
                    raw.get("parentCheckboxMatchCount") == 1
                    or (raw.get("parentScopedMatchCount") == 1 and bool(sel_val))
                )
            )
            contextual_click = (
                act == "click"
                and isinstance(raw.get("parentLabel"), str)
                and bool(raw.get("parentLabel"))
                and raw.get("parentLabelMatchCount") == 1
                and raw.get("parentScopedMatchCount") == 1
            )
            contextual_scope = contextual_checkbox or contextual_click
            if not sel_val and not contextual_scope:
                s.included_in_replay = False
                s.replay_blocking = True
                s.cleanup_reason = f"action {act} sans selecteur mesure"
                anomalies.append(f"{sid} : {act} sans selecteur mesure")
                continue
            if not contextual_scope and not _selector_is_verified_unique(s.selector):
                mc = getattr(s.selector, "matchCount", None) if not isinstance(s.selector, str) else None
                s.included_in_replay = False
                s.replay_blocking = True
                s.cleanup_reason = "selecteur non verifie unique pendant la capture"
                anomalies.append(f"{sid} : selecteur non verifie unique (matchCount={mc})")
                continue
        if getattr(s, "source", None) == "cdp_fallback":
            anomalies.append(f"{sid} : capture via fallback CDP (selecteur fragile)")

    filtered_noise = [
        f"{count+1} clics consecutifs sur {sel} (garde le 1er)"
        for sel, count in filtered_noise_counter.items()
    ]
    return steps, anomalies, filtered_noise


# Alias historique purement local conserve pour les integrations existantes.
deterministic_classify_steps = classify_steps


# ============================================================
# 5. Orchestration complete
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

    ``model``, ``base_url``, ``api_key``, ``use_llm`` et ``llm_timeout_s``
    restent dans la signature pour compatibilite API, mais ne sont jamais
    utilises : tout le post-traitement est local et deterministe.

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

    # 3. Classification locale stricte, sans appel reseau.
    steps, anomalies, filtered_noise = classify_steps(steps)

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
