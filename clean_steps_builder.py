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
    """Recupere l'historique complet des actions browser-use.

    Le shape de l'API browser-use varie selon la version :
      - >= 0.3 : agent.history est un AgentHistoryList avec .history: list[AgentHistory]
      - certaines versions : result est directement l'AgentHistoryList
      - anciennes : agent.state.history
      - fallback : result.all_history() ou result.model_actions()

    On tente les 3 chemins et normalise en list de dicts. Si tout echoue,
    retourne une liste vide + log warning (pas de crash).
    """
    history_list = None

    # Chemin 1 : result est deja l'AgentHistoryList (browser-use recent)
    if result is not None and hasattr(result, "history") and result.history:
        history_list = result.history
    # Chemin 2 : agent.history
    elif agent is not None and hasattr(agent, "history"):
        h = getattr(agent, "history", None)
        if h is not None:
            history_list = getattr(h, "history", None) or h
    # Chemin 3 : agent.state.history
    if history_list is None and agent is not None and hasattr(agent, "state"):
        state = getattr(agent, "state", None)
        if state is not None:
            h = getattr(state, "history", None)
            if h is not None:
                history_list = getattr(h, "history", None) or h

    if not history_list:
        return []

    normalized: list[dict[str, Any]] = []
    for entry in history_list:
        try:
            # Chaque AgentHistory a typiquement .model_output, .result, .state (screenshot, url, tabs)
            item: dict[str, Any] = {}
            # Actions decidees par le LLM
            model_output = getattr(entry, "model_output", None)
            if model_output is not None:
                # model_output.action est en general une liste d'objets Action
                actions = getattr(model_output, "action", None) or []
                item["actions"] = [_action_to_dict(a) for a in actions]
                if getattr(model_output, "current_state", None):
                    cs = model_output.current_state
                    item["thought"] = getattr(cs, "next_goal", None) or getattr(cs, "memory", None)
            # Resultat de l'execution
            results = getattr(entry, "result", None) or []
            if results:
                item["results"] = [_result_to_dict(r) for r in results]
            # Etat du navigateur au moment du step
            state = getattr(entry, "state", None)
            if state is not None:
                item["state"] = {
                    "url": getattr(state, "url", None),
                    "title": getattr(state, "title", None),
                    "tabs_count": len(getattr(state, "tabs", []) or []),
                }
            normalized.append(item)
        except Exception as e:
            normalized.append({"_parse_error": str(e)})

    return normalized


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
    """
    action = (entry.get("action") or "unknown").lower()
    sel = _dom_selector_to_pydantic(entry.get("selector"))
    val = entry.get("value")
    is_sensitive = bool(entry.get("sensitive"))
    step = Step(
        id=f"step-{index:04d}",
        step=index,
        action=action,
        description=entry.get("text") or None,
        page=entry.get("url"),
        url=entry.get("url"),
        timestamp=entry.get("timestamp"),
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


def _step_from_bu_action(bu_action: dict[str, Any], index: int, current_url: str | None) -> Step | None:
    """Convertit une action browser-use en Step v1.0 quand elle n'a pas
    ete captee par le DOM listener (typiquement : navigate, go_back, reload,
    open_tab, wait, screenshot, keyboard, upload).

    Retourne None si l'action ne se traduit pas en une etape rejouable
    (ex: extract_content, done - ce sont des observations LLM, pas des
    actions user).
    """
    # bu_action est un dict single-key: {action_name: params}
    if not isinstance(bu_action, dict) or not bu_action:
        return None
    # Normalise : {"go_to_url": {"url": "..."}} -> action="navigate", params={"url": ...}
    action_name = next(iter(bu_action.keys()))
    params = bu_action[action_name] if isinstance(bu_action[action_name], dict) else {}

    normalized = _normalize_bu_action_name(action_name)
    if normalized is None:
        # Action LLM sans traduction (ex: done, extract_content) -> non-step
        return None

    step = Step(
        id=f"step-{index:04d}",
        step=index,
        action=normalized,
        source="browser_use_history",
        included_in_replay=True,
        raw_payload=dict(bu_action),
    )

    if normalized == "navigate":
        url = params.get("url") or params.get("website")
        step.url = url or current_url
        step.description = f"Va sur {url}" if url else "Navigation"
        step.selectorType = "url"
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
    elif normalized in ("go_back", "go_forward", "reload"):
        step.description = {"go_back": "Retour arriere", "go_forward": "Avance", "reload": "Recharge la page"}[normalized]
    else:
        # unknown : conserve intact via raw_payload
        step.action = "unknown"
        step.description = f"Action browser-use non standard : {action_name}"

    step.page = current_url
    return step


def _normalize_bu_action_name(name: str) -> str | None:
    """Mappe les noms d'action browser-use vers notre vocabulaire.

    Retourne None pour les actions LLM qui ne se traduisent pas en step
    rejouable (done, extract_content, ...)."""
    n = (name or "").lower()
    NON_REPLAYABLE = {"done", "extract_content", "read_content", "assess", "think", "note"}
    if n in NON_REPLAYABLE:
        return None
    MAP = {
        "go_to_url": "navigate",
        "navigate_to": "navigate",
        "open_url": "navigate",
        "wait": "wait",
        "wait_for": "wait",
        "press_key": "keyboard",
        "key_press": "keyboard",
        "keyboard": "keyboard",
        "send_keys": "keyboard",
        "screenshot": "screenshot",
        "take_screenshot": "screenshot",
        "upload_file": "upload",
        "upload": "upload",
        "go_back": "go_back",
        "back": "go_back",
        "go_forward": "go_forward",
        "reload_page": "reload",
        "refresh": "reload",
        "reload": "reload",
        "open_tab": "open_tab",
        "new_tab": "open_tab",
        "switch_tab": "switch_tab",
        "close_tab": "close_tab",
        "click_element": "click",
        "click_element_by_index": "click",
        "click": "click",
        "input_text": "input",
        "type": "input",
        "fill": "input",
        "select_option": "select",
        "select": "select",
        "hover": "hover",
        "scroll": "scroll",
        "scroll_down": "scroll",
        "scroll_up": "scroll",
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
    steps: list[Step] = []
    current_url: str | None = None
    global_index = 0

    def _next_index() -> int:
        nonlocal global_index
        global_index += 1
        return global_index

    # 1. On demarre par les steps DOM listener (fiables, timestampes) tries par ts
    dom_sorted = sorted(dom_log or [], key=lambda x: x.get("timestamp", 0))
    for e in dom_sorted:
        step = _step_from_dom_entry(e, _next_index())
        current_url = step.url or current_url
        steps.append(step)

    # 2. On intercale les actions BU qui ne sont pas des clicks/inputs/scrolls
    #    (elles ne sont pas dans le DOM listener). On les ajoute a la fin
    #    dans l'ordre de l'historique BU pour rester deterministe. Une fusion
    #    temporelle plus fine (par ts commun) est possible en V+1 quand
    #    l'historique BU exposera aussi ses timestamps.
    for h in bu_history or []:
        for action_dict in (h.get("actions") or []):
            action_name = next(iter(action_dict.keys())) if isinstance(action_dict, dict) else None
            normalized = _normalize_bu_action_name(action_name or "") if action_name else None
            # Les click/input/scroll sont deja captes par le DOM listener,
            # on n'en ajoute pas de doublon depuis le BU history.
            if normalized in ("click", "input", "scroll", None):
                continue
            new_step = _step_from_bu_action(action_dict, _next_index(), current_url)
            if new_step is not None:
                steps.append(new_step)
                if new_step.url:
                    current_url = new_step.url

    # 3. Verify/cookie du scenario si non capturables (ils sont declaratifs)
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

    # 4. Rapprochement network (best-effort par plage timestamp)
    if network_log:
        _link_network_to_steps(steps, network_log)

    # 5. Renumbering final pour garder step-XXXX sequentiel apres fusion
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
