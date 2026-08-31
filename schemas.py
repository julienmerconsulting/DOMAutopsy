"""
DOMAutopsy - Schemas versionnes pour clean_steps.json
======================================================
Le clean_steps.json produit par qa_explorer est le pivot de tout le systeme :
- lu par report_generator pour construire le rapport HTML
- lu par playwright_generator pour ecrire test_playwright.spec.ts
- lu par qa_player (fallback legacy) pour le replay Python
- consomme par les integrations tierces (CLI, dashboards CI)

Ce module verrouille le contrat via Pydantic v2 et gere la retro-compat
avec les anciens runs (steps limites a click/input, pas de champ
included_in_replay, pas de schema_version).

Le champ schema_version en tete du JSON permet aux consommateurs de
detecter le format et d'appliquer une migration transparente si besoin.

Version actuelle : "2.0" (Aout 2026 - refactor unification Playwright TS).
L'ancien format non-versionne (pre-refactor) est traite comme "1.x
implicite" par migrate_legacy_json() qui le fait passer a 2.0.
"""

from __future__ import annotations

from typing import Any, Optional, Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator


CURRENT_SCHEMA_VERSION = "2.0"

# Actions livrees natives par le Scenario Builder (10 actions historiques)
BUILDER_ACTIONS = frozenset({
    "navigate", "click", "input", "select", "verify",
    "scroll", "hover", "wait", "screenshot", "cookie",
})

# Actions natives supplementaires renvoyees par browser-use ou capturees
# par le DOM listener CDP. La liste est indicative : tout nom d'action non
# reconnu est conserve sous type generique "unknown" avec son payload brut.
BROWSER_USE_EXTRA_ACTIONS = frozenset({
    "go_back", "go_forward", "reload", "keyboard", "key_press",
    "upload", "file_upload", "open_tab", "switch_tab", "close_tab",
    "extract", "extract_content", "screenshot_element", "wait_for_selector",
    "evaluate", "check", "uncheck",
})

KNOWN_ACTIONS = BUILDER_ACTIONS | BROWSER_USE_EXTRA_ACTIONS


class Selector(BaseModel):
    """Description d'un selecteur DOM issu de la cascade 7-tier ou d'une
    strategie generique (window pour scroll, url pour navigate).
    """
    model_config = ConfigDict(extra="allow")

    strategy: Optional[str] = None
    value: Optional[str] = None
    inShadowDOM: bool = False
    unique: Optional[bool] = None
    matchCount: Optional[int] = None
    shadowChain: Optional[list[dict[str, Any]]] = None
    playwrightSelector: Optional[str] = None
    jsSelector: Optional[str] = None
    verifiedAtCapture: Optional[bool] = None
    stability: Optional[Literal["high", "medium", "low"]] = None
    priority: Optional[int] = None
    captureSource: Optional[str] = None


class NetworkRef(BaseModel):
    """Reference legere vers une requete reseau associee a une etape.
    Le detail complet reste dans network_log.json, on garde juste l'index
    et les infos indispensables au rapprochement dans le rapport.
    """
    model_config = ConfigDict(extra="allow")

    index: int
    method: Optional[str] = None
    url: Optional[str] = None
    status: Optional[int] = None
    type: Optional[str] = None
    duration_ms: Optional[float] = None


class Step(BaseModel):
    """Une etape unique du parcours enrichi.

    Toutes les cles sont optionnelles au niveau typage : c'est l'action
    qui dicte lesquelles sont pertinentes (une navigation n'a pas de
    selecteur, un scroll n'a pas de valeur, etc.). La validation metier
    action-par-action se fait dans qa_explorer avant serialisation.

    Champ included_in_replay :
      True (defaut) = l'etape est traduite dans test_playwright.spec.ts
      False         = l'etape est conservee dans le JSON pour la
                      tracabilite (rapport, debug) mais SKIP au replay.
                      cleanup_reason indique pourquoi.
    """
    model_config = ConfigDict(extra="allow")

    # Identite et ordre
    id: Optional[str] = None
    step: Optional[int] = None

    # Nature de l'action
    action: str
    description: Optional[str] = None

    # Contexte page
    page: Optional[str] = None
    url: Optional[str] = None

    # Timing
    timestamp: Optional[int] = None
    started_at: Optional[str] = None
    ended_at: Optional[str] = None
    duration_ms: Optional[float] = None

    # Ciblage
    selector: Optional[Selector | str] = None
    selectorType: Optional[Literal["css", "xpath", "text", "role", "window", "url", "other"]] = None
    target: Optional[str] = None
    unique: Optional[bool] = None
    matchCount: Optional[int] = None
    inShadowDOM: bool = False

    # Valeurs
    value: Optional[str] = None
    sensitive: bool = False
    env_var: Optional[str] = None  # nom de la var d'env qui remplace value dans le TS

    # Scroll
    direction: Optional[str] = None
    deltaY: Optional[int] = None
    scrollY: Optional[int] = None

    # Attente
    seconds: Optional[float] = None
    wait_for: Optional[str] = None
    wait_state: Optional[str] = None  # attached, visible, hidden, detached...

    # Verification
    verify_type: Optional[str] = None
    expected: Optional[str] = None
    actual: Optional[str] = None

    # Statut
    status: Optional[Literal["pending", "pass", "fail", "skipped", "unknown"]] = "unknown"
    error: Optional[str] = None
    screenshot: Optional[str] = None

    # Rapprochement
    network: Optional[list[NetworkRef]] = None

    # Provenance : d'ou vient cette info (scenario, browser_use_history, dom_listener, ai_cleanup, generated)
    source: Optional[str] = None

    # Filtrage
    included_in_replay: bool = True
    cleanup_reason: Optional[str] = None
    replay_blocking: bool = False

    # Payload brut si action inconnue
    raw_payload: Optional[dict[str, Any]] = None

    @field_validator("action")
    @classmethod
    def _normalize_action(cls, v: str) -> str:
        if not v:
            return "unknown"
        v = v.strip().lower()
        return v


class Anomaly(BaseModel):
    model_config = ConfigDict(extra="allow")
    message: str
    step_id: Optional[str] = None
    severity: Optional[Literal["info", "warning", "error"]] = "warning"


class CleanSteps(BaseModel):
    """Racine du clean_steps.json enrichi.

    schema_version verrouille le contrat. Les consommateurs doivent
    verifier cette valeur et faire tourner migrate_legacy_json() si
    besoin.
    """
    model_config = ConfigDict(extra="allow")

    schema_version: str = CURRENT_SCHEMA_VERSION
    parcours: Optional[str] = None
    scenario_name: Optional[str] = None
    scenario_url: Optional[str] = None
    date: Optional[str] = None
    total_steps: int = 0
    steps: list[Step] = Field(default_factory=list)
    anomalies: list[Anomaly | str] = Field(default_factory=list)
    filtered_noise: list[str] = Field(default_factory=list)

    # Le code genere par IA pour le format demande par l'utilisateur
    # (Katalon/Cypress/Selenium). Le code Playwright TS canonique vit
    # dans test_playwright.spec.ts, PAS dans ce champ.
    # Cle historique gardee : 'katalon_code' pour compat back.
    katalon_code: Optional[str] = None


def migrate_legacy_json(data: dict[str, Any]) -> dict[str, Any]:
    """Convertit un ancien clean_steps.json (pre-1.0) vers le format v1.0.

    Retro-compat :
    - ajoute schema_version si absent
    - normalise chaque step (action lowercase, inclus dans replay par defaut,
      selector garde tel quel)
    - convertit les selecteurs strings en Selector(value=..., strategy='raw')

    Retourne un dict pret a etre passe a CleanSteps.model_validate().
    """
    out = dict(data)
    # Deux chemins de migration :
    # - pas de schema_version : ancien JSON pre-refactor (aucune version
    #   n'existait), stampe directement le CURRENT
    # - schema_version == "1.0" : JSON produit entre le commit initial du
    #   refactor et le bump 2.0 (fenetre courte, faible probabilite mais
    #   theoriquement possible). Migre en "2.0" sans autre transformation
    #   car le format est identique - c'est un renumerotage cosmetique.
    if out.get("schema_version") == "1.0":
        out["schema_version"] = CURRENT_SCHEMA_VERSION
    else:
        out.setdefault("schema_version", CURRENT_SCHEMA_VERSION)

    # Migration des steps
    steps = out.get("steps", []) or []
    migrated_steps = []
    for i, s in enumerate(steps, start=1):
        if not isinstance(s, dict):
            continue
        ns = dict(s)
        # Certains anciens JSON stockent 'selector' comme string simple
        sel = ns.get("selector")
        if isinstance(sel, str):
            ns["selector"] = {
                "value": sel,
                "strategy": "raw",
                "unique": ns.get("unique"),
                "matchCount": None,
            }
        # step number si absent
        ns.setdefault("step", i)
        # included_in_replay defaut True (les anciens JSON n'avaient que
        # les steps a jouer, pas de filtrage marque)
        ns.setdefault("included_in_replay", True)
        # source : historique -> ai_cleanup (c'etait la seule origine)
        ns.setdefault("source", "ai_cleanup")
        migrated_steps.append(ns)
    out["steps"] = migrated_steps

    # total_steps si absent
    out.setdefault("total_steps", len(migrated_steps))

    # anomalies : accepte list[str] -> laisse tel quel (le modele gere l'union)
    out.setdefault("anomalies", [])
    out.setdefault("filtered_noise", [])

    return out


def load_and_validate(raw: dict[str, Any]) -> CleanSteps:
    """Charge un dict quelconque, applique la migration si necessaire,
    et retourne un CleanSteps valide (ou leve ValidationError)."""
    if not isinstance(raw, dict):
        raise TypeError(f"clean_steps.json doit etre un objet JSON, recu {type(raw).__name__}")
    migrated = migrate_legacy_json(raw)
    return CleanSteps.model_validate(migrated)
