# DOMAutopsy — Architecture technique complète

> **Auteur** : Julien Mer / JMer Consulting
> **Stack** : Python 3.12+ · Playwright · browser-use · FastAPI · OpenAI/Groq · Chromium CDP
> **Statut** : v0 web livrée · V2 (import scripts + observabilité CDP) en préparation

---

## 1. Vue d'ensemble

DOMAutopsy est un **outil de capture, d'analyse et de génération de tests QA** qui se branche sur Chromium via le protocole **Chrome DevTools (CDP)**, observe un parcours réalisé par un agent IA (browser-use) ou un humain, et produit en sortie :

1. Un **log brut des locateurs** capturés directement depuis le DOM réel
2. Un **parcours nettoyé** par IA (filtre du bruit, dédup, anomalies)
3. Un **fichier de test rejouable** dans 4 frameworks au choix : Katalon Studio (Groovy), Playwright (TypeScript), Cypress (JS), Selenium (Python)
4. Un **rapport HTML self-service** avec graphiques, KPIs et code généré

Le différenciateur architectural majeur : **la couche de capture (DOM listener JS injecté) est totalement découplée du driver d'exécution**. Browser-use peut être remplacé par Selenium, Playwright direct, ou un humain — la capture continue de fonctionner à l'identique. Aucun autre outil du marché ne fait ce découplage.

---

## 2. Schéma d'architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                          NAVIGATEUR UTILISATEUR                              │
│                       (Chrome / Edge / Firefox)                              │
│                                                                              │
│  ┌────────────────────────────────────────────────────────────────────────┐ │
│  │                   UI HTML (web/index.html + app.js)                    │ │
│  │                                                                        │ │
│  │  ┌─────────────────┐ ┌────────────────────────┐ ┌──────────────────┐   │ │
│  │  │  Form           │ │  Canvas screencast     │ │  Log Python      │   │ │
│  │  │  - URL          │ │  ←── WS frames jpeg    │ │  ←── WS stdout   │   │ │
│  │  │  - Task NL      │ │      base64 (30 fps)   │ │                  │   │ │
│  │  │  - Format ▼     │ │                        │ │  Bouton:         │   │ │
│  │  │  - Provider ▼   │ │                        │ │   Voir rapport   │   │ │
│  │  │  - Timing avncs │ │                        │ │   Voir code test │   │ │
│  │  │  - [Lancer]     │ │                        │ │                  │   │ │
│  │  └─────────────────┘ └────────────────────────┘ └──────────────────┘   │ │
│  │                                                                        │ │
│  │  ┌────────────────────────────────────────────────────────────────────┐│ │
│  │  │                Historique des runs (sidebar)                       ││ │
│  │  │  ▶ run #abc123 — wikipedia.org — 6 actions — success — 16:01:22   ││ │
│  │  │  ▶ run #def456 — demoblaze.com — 3 actions — success — 16:42:15   ││ │
│  │  └────────────────────────────────────────────────────────────────────┘│ │
│  └────────────────────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ HTTP REST + 2 WebSockets
                                       │
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                  SERVEUR FASTAPI (server.py, port 8000)                      │
│                                                                              │
│  POST /api/import            ── parse script existant + redact (V2)          │
│  POST /api/run               ── alloue un run_id, spawn qa_explorer          │
│  WS   /ws/logs/{id}          ── stream stdout du subprocess                  │
│  WS   /ws/screen/{id}        ── relais CDP Page.screencastFrame              │
│  GET  /api/history           ── lit runs/* meta.json, retourne les runs      │
│  GET  /api/report/{id}       ── sert qa_report_*.html                        │
│  GET  /api/run/{id}/files    ── liste les fichiers d'un run                  │
│  GET  /api/run/{id}/file/{f} ── sert un fichier (path traversal-safe)        │
│  DELETE /api/run/{id}        ── kill du subprocess (cleanup onglet fermé)    │
│  GET  /api/runs              ── runs en mémoire (compteur actifs)            │
│  GET  /api/formats           ── katalon/playwright/cypress/selenium          │
│  GET  /api/providers         ── openai/groq + key_present                    │
│                                                                              │
│  Lifespan FastAPI :                                                          │
│   - startup : crée runs/ si absent                                           │
│   - shutdown : terminate puis kill tous les subprocess actifs                │
│                                                                              │
│  Gestion d'allocation port CDP : 9222..9272 (50 runs parallèles max théo)    │
│  Dossier RUNS_DIR : runs/<YYYYMMDD_HHMMSS>_<runid>/                          │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ subprocess.Popen
                                       │ (event-loop-agnostique
                                       │  Windows/Linux/Mac)
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                         qa_explorer.py (subprocess)                          │
│                                                                              │
│  1. Patch monkey browser-use (issue #2808 frame crash)                       │
│  2. Lance Chromium via Playwright avec --remote-debugging-port=<cdp_port>    │
│     Headless ou visible selon flag, args optimisés bas-RAM                   │
│  3. Charge dom_listener.js (read_text) et l'injecte:                         │
│       a. context.add_init_script() pour les nouveaux contextes Playwright    │
│       b. page.evaluate() pour la page courante                               │
│       c. CDP Page.addScriptToEvaluateOnNewDocument (best-effort)             │
│  4. Crée BrowserProfile(min_wait, max_wait, network_idle) — fix SPA timing   │
│  5. Crée BrowserSession(cdp_url=...) connectée au Chromium déjà ouvert       │
│  6. Instancie Agent(task, llm, use_vision selon provider/modèle)             │
│  7. agent.run() — callback pré-action mesure les cibles BU via CDP           │
│  8. Lit localStorage.__qaLocatorLog depuis chaque page (3 fallbacks)         │
│  9. Dédup (clics consécutifs, dernière valeur input par sélecteur+url)       │
│ 10. Enrichit les sélecteurs live via backend_node_id + CDP                   │
│ 11. Classe localement et génère les exports déterministes                    │
│ 12. Génère qa_report_<ts>.html via report_generator.py                      │
│ 13. Écrit meta.json (timestamp, status, agent_result, counts, etc.)          │
│ 14. try/finally : ferme browser + Playwright proprement                      │
└──────────────────────────────────────┬──────────────────────────────────────┘
                                       │ CDP port 9222+offset
                                       ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│                    CHROMIUM (lancé par qa_explorer)                          │
│                                                                              │
│  ┌──────────────────────────────────────────────────────────────────────┐    │
│  │ DOM de la page testée                                                │    │
│  │   ┌────────────────────────────────────────────┐                     │    │
│  │   │ dom_listener.js (notre code)               │                     │    │
│  │   │  - listen click / input / scroll capture   │                     │    │
│  │   │  - getBestSelector() cascade 7 tiers       │                     │    │
│  │   │  - shadow DOM > getRealTarget() composedPath│                    │    │
│  │   │  - sensitive field detection + redacting   │                     │    │
│  │   │  - buffer + flush localStorage périodique  │                     │    │
│  │   └────────────────────────────────────────────┘                     │    │
│  │                                                                      │    │
│  │   ┌────────────────────────────────────────────┐                     │    │
│  │   │ browser-use agent (CDP-piloté)             │                     │    │
│  │   │  - reçoit task + tools + screenshot/AX     │                     │    │
│  │   │  - décide actions (click, type, wait, etc.)│                     │    │
│  │   │  - exécute via CDP -> events DOM           │                     │    │
│  │   └────────────────────────────────────────────┘                     │    │
│  └──────────────────────────────────────────────────────────────────────┘    │
│                                                                              │
│  CDP /json endpoint exposé sur localhost:<cdp_port>                          │
│  Domaines actifs : Page, Browser, DOM (par browser-use)                      │
│  Domaines exposés mais pas encore branchés : Network, Runtime, Console,      │
│  Performance, Coverage (cf. roadmap V2)                                      │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## 3. Composants — détail fichier par fichier

### 3.1 `qa_explorer.py` (1140+ lignes — cœur du pipeline)

**Responsabilités** :

- Parse arguments CLI (3 modes : scenario JSON, --url/--task, legacy task hardcoded)
- Patch `browser_use.dom.service.DomService._get_ax_tree_for_all_frames` pour survivre aux iframes détruites (issue browser-use #2808)
- Lance Chromium via Playwright avec port CDP unique
- Injecte le DOM listener via 3 chemins (Playwright add_init_script, page.evaluate, CDP direct best-effort)
- Configure `BrowserProfile` avec timing personnalisé (min_wait, max_wait, network_idle) — résout les SPA lourdes
- Instancie l'agent browser-use (auto use_vision off pour Groq text-only)
- Récupère `localStorage.__qaLocatorLog` via 3 fallbacks (contexte browser-use → contexte Playwright original → CDP direct)
- Dédup (clics consécutifs identiques, dernière valeur input par sélecteur+URL)
- Cleanup IA via prompt format-aware
- Écrit tous les artefacts dans `runs/<timestamp>_<runid>/`
- Écrit `meta.json` avec metadata du run
- try/finally autour de la session pour garantir le cleanup browser

**Constantes et configuration centrale** :

| Constante | Valeur | Rôle |
|---|---|---|
| `CDP_PORT` | 9222 | Port CDP par défaut |
| `LLM_MODEL` | gpt-4.1-mini | Modèle OpenAI par défaut |
| `MIN_WAIT_PAGE_LOAD` | 2.0s | Wait min avant snapshot DOM |
| `MAX_WAIT_PAGE_LOAD` | 15.0s | Wait max avant timeout snapshot |
| `NETWORK_IDLE_WAIT` | 3.0s | Wait idle réseau |
| `MAX_STEPS` | 25 | Max steps agent (anti-boucle) |
| `PROVIDERS` | dict | openai (gpt-4.1-mini) + groq (llama-3.3-70b-versatile) |
| `GROQ_VISION_MODELS` | set | Modèles Groq qui acceptent format multimodal |
| `OUTPUT_FORMATS` | dict | 4 formats : katalon, playwright, cypress, selenium |

**Flags CLI exposés** : `--url --task --provider --model --port --output-format --headless --output-dir --no-open-report --min-wait --max-wait --network-idle --max-steps --vision --no-vision`

### 3.2 `server.py` (FastAPI server, 350+ lignes)

**Responsabilités** :

- Force `WindowsProactorEventLoopPolicy` (compat Windows asyncio si quelqu'un l'utilise)
- Filtre access logs uvicorn pour endpoints de polling silencieux (`/api/runs`, `/api/formats`, etc.)
- Cache `OUTPUT_FORMATS` au boot (évite re-import qa_explorer à chaque GET)
- Allocateur de port CDP libre (9222 → 9272)
- Spawne `qa_explorer.py` via `subprocess.Popen` standard (event-loop-agnostique, marche avec `uvicorn --reload`)
- Stream stdout subprocess vers WebSocket via `asyncio.run_in_executor` (lecture bloquante déléguée au thread pool)
- Stream CDP screencast : connecte à `http://localhost:<cdp_port>/json`, ouvre WS sur `webSocketDebuggerUrl`, envoie `Page.startScreencast`, forward chaque `Page.screencastFrame` vers le client WebSocket avec ack
- Lifespan FastAPI : kill tous subprocess actifs au shutdown (terminate puis kill après 2s)
- DELETE endpoint pour kill manuel d'un run (cleanup quand l'utilisateur ferme l'onglet → `beforeunload` keepalive)
- Endpoint `/api/history` qui scanne `runs/`, lit chaque `meta.json`, trie par date desc
- Path traversal protection sur `/api/run/{id}/file/{filename}`

**Endpoints catalogue** :

| Route | Méthode | Rôle |
|---|---|---|
| `/` | GET | UI principale (web/index.html) |
| `/api/run` | POST | Lance un run, retourne `{run_id, cdp_port}` |
| `/api/run/{id}` | DELETE | Kill subprocess + status="killed" |
| `/api/runs` | GET | Tous les runs en mémoire |
| `/api/history` | GET | Runs persistés sur disque (limit=N) |
| `/api/run/{id}/files` | GET | Liste fichiers d'un run |
| `/api/run/{id}/file/{filename}` | GET | Sert un fichier (HTML/JSON/code) |
| `/api/report/{id}` | GET | Rapport HTML (mémoire OU historique) |
| `/api/status/{id}` | GET | État d'un run en mémoire |
| `/api/formats` | GET | Formats de sortie |
| `/api/providers` | GET | Providers LLM + key_present |
| `/ws/logs/{id}` | WS | Stream stdout subprocess |
| `/ws/screen/{id}` | WS | Stream CDP Page.screencastFrame |

**Entry point** : `python server.py` recommandé sur Windows (set Proactor policy avant uvicorn.run)

### 3.3 `dom_listener.js` (~280 lignes JS injectées dans Chromium)

**Responsabilités** :

- Buffer mémoire avec flush périodique (toutes les 1.5s + sur `beforeunload`/`pagehide`) — évite l'O(n²) localStorage write par event
- 3 listeners : `click`, `input`, `scroll` (debounced 250ms)
- `getRealTarget()` : remonte composedPath jusqu'au parent interactif (button, a, input) avec aria-label/data-testid/id — gère le cas SVG/icon dans bouton
- `getBestSelector()` : **cascade 7-tier** stricte avec validation matchCount runtime :

| Tier | Stratégie | Exemple |
|---|---|---|
| 1 | data-testid | `[data-testid="login-btn"]` |
| 1 | id propre (pas généré) | `#username` |
| 1 | name | `input[name="email"]` |
| 2 | aria-label | `[aria-label="Rechercher"]` |
| 2 | placeholder | `input[placeholder="..."]` |
| 2 | title | `[title="..."]` |
| 3 | href (pour `<a>`) | `a[href="/login"]` |
| 4 | parent stable | `[aria-label="..."]` du closest |
| 5 | label associé (input) | `//label[contains(text(),"X")]//input` |
| 6 | CSS short + nth-of-type | `button.cdx-button:nth-of-type(2)` |
| 7 (fallback) | xpath text-based | `//button[contains(text(),"Rechercher")]` |

- **Validation runtime** : pour chaque sélecteur, count des matches via `querySelectorAll` ou `document.evaluate` → `unique: true/false`, `matchCount: N`
- **Auto-promotion** au tier 7 si le sélecteur du tier 1-6 n'est pas unique mais le texte est court et permet un xpath text-based unique
- **Shadow DOM** : `isInShadowDOM()` + chaîne `>>>` (Playwright) + `jsSelector` (`document.querySelector("...").shadowRoot.querySelector("...")`)
- **Échappement attribut CSS** : `attrValue()` pour `\` et `"`
- **Échappement XPath** : `xpathString()` avec gestion des quotes mixtes via `concat()`
- **CSS.escape()** sur ids et classes
- **Détection sensitive** : type="password", autocomplete cc-*/current-password/new-password/one-time-code, regex sur name/id/aria-label/placeholder/data-testid (password|pwd|secret|token|otp|cvv|cvc|ssn|sin|pin|api_key)
- **Redacting** : `value: "<redacted>"` + flag `sensitive: true`, le **sélecteur est conservé** mais la valeur est purgée

### 3.4 `report_generator.py` (HTML report generator)

- 5 KPI cards (Steps nettoyés, Actions capturées, Sélecteurs uniques, Anomalies, Stratégies utilisées)
- 3 donuts Chart.js (Stratégies, Fiabilité, Types d'actions)
- Liste anomalies (sélecteurs non uniques, remplacements xpath, etc.)
- Tableau parcours nettoyé (steps, action, description, sélecteur, type, unique, valeur)
- Tableau locateurs capturés bruts (avec strategy + matchCount)
- Bloc code de test généré (Katalon/Playwright/Cypress/Selenium) avec syntax highlighting visuel (préformaté)
- Dark theme cohérent avec la web UI
- Self-contained HTML (Chart.js depuis CDN, le reste inline)

### 3.5 `script_parser.py` (~300 lignes — V2 import scripts, modulé prêt)

- Parsers regex pour 4 formats : Katalon Groovy, Playwright TS/JS, Cypress JS, Selenium Python
- Détection format par extension
- Extraction : URL initiale, sélecteurs (CSS/XPath), actions (click/input)
- Redacting automatique des valeurs dans les champs sensibles (pattern SENSITIVE)
- Fonction `to_nl_task()` qui convertit le parse en description NL pour browser-use
- **Pas encore wired** dans server.py — sera la prochaine étape

### 3.6 `scenario_builder.py` (GUI PySide6 — mode legacy)

- GUI desktop pour construire un scenario JSON visuellement (drag & drop d'actions)
- Lance qa_explorer en sous-process avec le JSON
- **Avant la web UI** c'était l'interface principale — toujours fonctionnelle, complémentaire

### 3.7 Frontend `web/`

- `index.html` : header, form, canvas screencast, log box, history sidebar, boutons report/code
- `style.css` : dark theme cohérent, grid responsive, history list styling, status dots animés
- `app.js` : vanilla JS, WebSocket clients, canvas frame painter, history list renderer, escapeHtml helper, polling actif runs (10s, pause si tab hidden)

---

## 4. Pipeline d'exécution complet

```
[USER]
  │
  │ 1. POST /api/run (URL, task, format, provider, headless, timing)
  ▼
[SERVER]
  │ 2. Génère run_id, alloue port CDP libre, crée runs/<ts>_<id>/
  │ 3. Spawn subprocess qa_explorer.py avec tous les args
  │ 4. Démarre _pump_stdout (run_in_executor sur readline)
  │ 5. Réponse {run_id, cdp_port}
  │
  │ 6. Client ouvre WS /ws/logs/{run_id}    ◄── lit la queue stdout
  │ 7. Client ouvre WS /ws/screen/{run_id}  ◄── connect CDP, screencast
  │
  ▼
[QA_EXPLORER subprocess]
  │ 8. Charge .env, applique patch browser-use
  │ 9. Lance Chromium headless avec --remote-debugging-port=<cdp_port>
  │     args optimisés bas-RAM (--no-sandbox, --disable-gpu, etc.)
  │ 10. Lit dom_listener.js depuis disque
  │ 11. Inject listener (3 chemins : add_init_script, page.evaluate, CDP)
  │ 12. Crée BrowserProfile + BrowserSession(cdp_url=localhost:<cdp_port>)
  │ 13. Instancie ChatOpenAI(base_url, api_key) selon provider
  │ 14. Crée Agent(task, llm, use_vision)
  │
  │ 15. await agent.run(max_steps=25)
  │      └─► browser-use décide les actions, les exécute via CDP
  │      └─► Chromium dispatche les events DOM
  │      └─► dom_listener intercepte chaque click/input/scroll
  │      └─► saveEntry() buffer mémoire, flush toutes 1.5s vers localStorage
  │
  │ 16. Récupère localStorage.__qaLocatorLog (3 fallbacks)
  │ 17. Sort par timestamp, dédup par timestamp inter-pages
  │ 18. Écrit runs/<id>/locator_log.json (brut)
  │ 19. dedup_log() : retire clics consécutifs identiques, garde dernière valeur input
  │ 20. Écrit runs/<id>/locator_dedup.json
  │ 21. enrich_browser_use_step_snapshot() puis classify_steps() :
  │      - résolution du nœud exact via backend_node_id avant chaque lot d'actions
  │      - mesure querySelectorAll/XPath des candidats dans le DOM vivant
  │      - inclusion locale stricte, sans appel LLM post-capture
  │ 22. Écrit runs/<id>/clean_steps.json
  │ 23. Écrit runs/<id>/test_<format>.<ext>
  │ 24. generate_report() : produit qa_report_<ts>.html dans runs/<id>/
  │ 25. Écrit runs/<id>/meta.json (timestamp, status, counts, etc.)
  │ 26. try/finally : browser.close() + pw.stop()
  │ 27. Subprocess exit_0
  │
  ▼
[SERVER]
  │ 28. _pump_stdout détecte fin de subprocess, met queue None (sentinelle)
  │ 29. WS /ws/logs envoie {type:"end", status:"exit_0"} et close
  │ 30. RUNS[run_id]["status"] = "exit_0"
  │
  ▼
[CLIENT]
  │ 31. UI active "Voir le rapport HTML" et "Voir le code de test"
  │ 32. loadHistory() rafraîchit la sidebar
  │ 33. Click rapport → window.open(/api/report/{id}) → FileResponse(qa_report_<ts>.html)
```

---

## 5. Modèle de données

### 5.1 Locator log entry (sortie listener)

```json
{
  "action": "click | input | scroll",
  "timestamp": 1778429265564,
  "tag": "INPUT",
  "value": "tomsmith | <redacted>",
  "sensitive": false,
  "text": "Login",
  "selector": {
    "strategy": "id | data-testid | name | aria-label | placeholder | title | href | parent-aria-label | label-xpath | css-short | xpath-text | shadow",
    "value": "#username",
    "inShadowDOM": false,
    "unique": true,
    "matchCount": 1,
    "shadowChain": [{"selector": "host", "shadow": true}, ...],
    "playwrightSelector": "host >>> inner",
    "jsSelector": "document.querySelector('host').shadowRoot.querySelector('inner')"
  },
  "url": "https://...",
  "inShadowDOM": false,
  "attributes": {
    "id": "...", "name": "...", "type": "text", "class": "...", "href": "...",
    "data-testid": "...", "aria-label": "...", "role": "..."
  }
}
```

### 5.2 meta.json (1 par run)

```json
{
  "timestamp": "20260510_180136",
  "started_at": "2026-05-10T18:01:36",
  "ended_at": "2026-05-10T18:02:14",
  "scenario_name": "...",
  "scenario_url": "https://...",
  "task": "...",
  "output_format": "katalon",
  "provider": "openai",
  "model": "gpt-4.1-mini",
  "headless": true,
  "use_vision": true,
  "agent_result": "SUCCESS — ...",
  "raw_count": 22,
  "deduped_count": 6,
  "report": "qa_report_20260510_180136.html",
  "status": "success"
}
```

### 5.3 Structure du dossier de run

```
runs/<YYYYMMDD_HHMMSS>_<runid>/
├── locator_log.json        # capture brute du listener
├── locator_dedup.json      # après dédup
├── clean_steps.json        # nettoyé par IA + anomalies + code généré
├── test_<format>.<ext>     # code de test rejouable (Katalon/PW/Cypress/Selenium)
├── qa_report_<ts>.html     # rapport HTML self-contained
└── meta.json               # metadata du run (alimente /api/history)
```

---

## 6. Extensibilité

### 6.1 Ajouter un provider LLM (5 minutes)

```python
# qa_explorer.py
PROVIDERS = {
    "openai": {...},
    "groq": {...},
    "anthropic": {                              # NOUVEAU
        "base_url": "https://api.anthropic.com/v1",  # ou via SDK natif
        "env_var": "ANTHROPIC_API_KEY",
        "default_model": "claude-sonnet-4-6",
    },
}
```

### 6.2 Ajouter un format de sortie (15 minutes)

```python
# qa_explorer.py
OUTPUT_FORMATS = {
    "katalon": {...},
    # ...
    "robot": {                                  # NOUVEAU : Robot Framework
        "label": "Robot Framework",
        "extension": ".robot",
        "code_instructions": "6. Genere un test Robot Framework...",
    },
}
```

### 6.3 Ajouter un parser de script existant (V2 — 30 minutes par langage)

```python
# script_parser.py
def parse_robot(src: str) -> dict:
    # regex sur Open Browser, Click Element, Input Text...
    pass
PARSERS["robot"] = parse_robot
```

### 6.4 Ajouter un endpoint serveur

FastAPI standard, ajouter dans `server.py` après les imports.

---

## 7. Sécurité, confidentialité, conformité

### 7.1 Données sensibles

- **Détection automatique** côté listener (`isSensitiveField()`) :
  - `type="password"`
  - `autocomplete=current-password|new-password|one-time-code|cc-*`
  - regex sur `name|id|aria-label|placeholder|data-testid|autocomplete` matchant `password|pwd|secret|token|otp|cvv|cvc|ccv|cc-num|card-num|ssn|sin|pin|api_key`
- **Redacting** : `value: "<redacted>"` + `sensitive: true` dans l'entry
- **Le sélecteur est conservé** — le test rejouable contient `setText(to, "REPLACE_ME")` côté utilisateur final
- Conséquence : **les credentials ne quittent JAMAIS la machine locale**, ne sont **JAMAIS envoyés à OpenAI/Groq** au cleanup, ne figurent **JAMAIS dans le rapport HTML**

### 7.2 Path traversal

- `/api/run/{id}/file/{filename}` rejette `/`, `\`, `..` dans le filename
- `_find_run_dir` vérifie le suffixe `_<runid>` strict (pas un `startswith` qui permettrait l'injection)

### 7.3 Subprocess safety

- `subprocess.Popen` avec args en liste (jamais shell=True)
- Args validés par Pydantic via `RunRequest`
- Cleanup sur `beforeunload` (UI) + lifespan shutdown (server) → pas de zombies

### 7.4 RGPD (V2 ajout via Network capture)

- Capture des domaines tiers contactés pendant le run
- Section "Privacy audit" dans le rapport listant les third parties (Google Analytics, Facebook, Hotjar, etc.) et le volume de données envoyé
- Argument vendable aux DPO, conformité article 32 du RGPD (sécurité des traitements)

---

## 8. État actuel — features livrées (v0 → v0.5)

### 8.1 Capture & analyse

- ✅ DOM listener JS injecté avec cascade 7-tier de sélecteurs
- ✅ Validation runtime de l'unicité du sélecteur (matchCount)
- ✅ Auto-promotion vers xpath text-based si sélecteur non unique mais texte court disponible
- ✅ Support Shadow DOM avec chaîne `>>>` Playwright et `jsSelector`
- ✅ Resolution composedPath + bubble-up vers parent interactif (icon dans button)
- ✅ Échappement CSS (`CSS.escape`, `attrValue`) et XPath (`xpathString` avec `concat()` pour quotes mixtes)
- ✅ Buffer mémoire + flush périodique (anti-O(n²) localStorage)
- ✅ Détection automatique des champs sensibles + redacting valeur (sélecteur conservé)
- ✅ Listener scroll debounced 250ms
- ✅ Dédup côté Python (clics consécutifs, dernière valeur input par sélecteur+url)

### 8.2 Driver & timing

- ✅ Chromium lancé via Playwright avec port CDP unique par run
- ✅ Args optimisés bas-RAM en headless (--no-sandbox, --disable-gpu, etc.)
- ✅ Mode headless (web UI) ou visible (CLI) — flag `--headless`
- ✅ Patch monkey browser-use pour iframes détruites (issue #2808)
- ✅ BrowserProfile avec timing personnalisé (min_wait, max_wait, network_idle) — résout les SPA lourdes
- ✅ Fallback sur kwargs directs si BrowserProfile pas dispo
- ✅ try/finally autour de la session pour cleanup browser garanti

### 8.3 IA / providers

- ✅ Multi-provider via API OpenAI-compatible : `openai`, `groq`
- ✅ Auto-détection vision capability (Groq text-only → use_vision=False auto)
- ✅ Default model par provider
- ✅ Lecture clé API depuis env var spécifique au provider
- ✅ Cleanup IA avec scenario_steps comparison (filtre bruit, signale gaps)
- ✅ Génération de code de test format-aware (4 formats)
- ✅ Prompt règle critique : ne JAMAIS modifier les sélecteurs unique:true, promouvoir xpath text-based si unique:false avec texte

### 8.4 Web UI & serveur

- ✅ FastAPI server avec lifespan moderne (pas `@app.on_event`)
- ✅ Subprocess.Popen + run_in_executor (compat Windows + uvicorn --reload)
- ✅ POST /api/run avec port CDP unique alloué automatiquement
- ✅ WebSocket /ws/logs : stream stdout subprocess ligne par ligne
- ✅ WebSocket /ws/screen : CDP Page.startScreencast → forward jpeg base64 vers canvas (30 fps)
- ✅ Multi-run parallèles supportés (un onglet = un run, port CDP unique chacun)
- ✅ DELETE /api/run + beforeunload keepalive : kill subprocess à fermeture d'onglet
- ✅ Lifespan shutdown : kill tous subprocess actifs au Ctrl+C uvicorn
- ✅ /api/history : lit meta.json de chaque run dir, retourne triés par date desc
- ✅ /api/run/{id}/files + /api/run/{id}/file/{filename} avec path traversal protection
- ✅ Cache OUTPUT_FORMATS, filtre access logs uvicorn pour endpoints polling

### 8.5 Frontend

- ✅ Form complet (URL, task, format dropdown, provider, modèle, timing avancé, headless toggle)
- ✅ Canvas screencast en direct
- ✅ Log box couleur (info/warn/error/ok/step) auto-scroll
- ✅ Badge run_id + port CDP du run courant
- ✅ Badge "N actifs / M total" cross-tab (polling 10s, pause si tab hidden)
- ✅ Bouton "Stopper" pour kill manuel
- ✅ Bouton "Voir le rapport HTML" + "Voir le code de test" après run terminé
- ✅ Sidebar Historique (30 derniers runs) avec URL, task, format, count, status, timestamp
- ✅ Click sur historique = ouvre rapport, Shift+click = ouvre code

### 8.6 Outputs & artefacts

- ✅ 1 dossier par run (`runs/<ts>_<runid>/`) avec tous les artefacts
- ✅ Plus d'auto-open du rapport navigateur quand lancé depuis le serveur (`--no-open-report`)
- ✅ meta.json par run (alimente l'historique persistant)
- ✅ Rapport HTML self-contained (Chart.js CDN, KPIs, donuts, tableaux, code)
- ✅ 4 formats de code générés au choix : Katalon Groovy, Playwright TS, Cypress JS, Selenium Python

### 8.7 Hygiène projet

- ✅ `python-dotenv` chargement automatique du `.env`
- ✅ `.env.example` documente OPENAI_API_KEY + GROQ_API_KEY
- ✅ LICENSE MIT
- ✅ `.python-version` (pyenv) et `.env.example` (conventions standards)
- ✅ `requirements.txt` avec versions minimales pinnées
- ✅ `.gitignore` runs/ + patterns historiques

---

## 9. Roadmap — features à venir

### 9.1 V1 (consolidation, ~1 semaine)

| Feature | Effort | Valeur |
|---|---|---|
| Fix `'BrowserSession' object has no attribute 'context'` (alignement API browser-use 0.12) | 1h | bug résiduel |
| Fix `Page.addScriptToEvaluateOnNewDocument wasn't found` (CDP version mismatch Playwright) | 2h | warning cosmétique |
| `--max-tokens` cap par appel LLM + retry avec backoff sur rate limit | 4h | maîtrise des coûts |
| Tests unitaires : `dedup_log`, `getBestSelector`, parsers (au moins 20 tests) | 1j | due diligence |
| Mode CI : `--junit-xml report.xml --exit-code-on-fail` | 4h | intégration CI/CD |

### 9.2 V2 (import scripts existants, ~3 jours)

| Feature | Effort | Valeur |
|---|---|---|
| `script_parser.py` câblage final (déjà écrit, pas wired) | 30 min | fondation V2 |
| POST `/api/import` (multipart upload, parse, retourne URL+task suggérés+selectors_id) | 2h | UX upload |
| Frontend file input + autofill du form après parse | 1h | UX upload |
| Stage diff dans qa_explorer : `--diff-against <selectors.json>`, écrit `diff_report.json` | 4h | différenciateur |
| Si pas de drift : copie le script original `validated_original.<ext>` dans run dir | 30 min | "ton test est encore valide" |
| Rapport HTML enrichi : section "Drift report" si diff détecté | 2h | livrable client |
| Endpoint `/api/run/{id}/diff` pour récupérer diff_report.json | 30 min | API pour intégrations |

### 9.3 V3 (observabilité CDP, ~1 semaine) — ordre d'implémentation strict

**Important** : faire dans cet ordre exact pour éviter le merge hell et les dépendances circulaires.

| Phase | Feature | Effort | Valeur |
|---|---|---|---|
| 1 | `Runtime.exceptionThrown` capture + section "JS Errors silencieux" dans rapport | 30 min | bugs invisibles révélés |
| 1 | `Console.messageAdded` capture + section "Console output" | 30 min | warnings/errors |
| 2 | `Network.requestWillBeSent` + `Network.responseReceived` capture brute (sans assertions) | 2h | API telemetry |
| 2 | Section "Network audit" dans rapport (3rd parties, volumes, status codes) | 2h | argument RGPD |
| 3 | API assertions auto-générées dans le code de test (`expect response.status === 200`) | 4h | qualité tests générés |
| 3 | Mock generator : `--generate-mocks` produit `mocks.json` rejouable offline | 1j | tests deterministes |
| 4 | `Performance.getMetrics` snapshot start/end + delta dans meta.json | 1h | régression perf |
| 4 | Baseline historique + alerte sur drift heap/layout/nodes | 1j | monitoring continu |
| 5 | `Coverage.startPreciseCoverage` + `Coverage.takePreciseCoverage` + section "% code testé" | 2h | couverture réelle |
| 6 | `DOM.attributeModified` idle-wait helper (kill la flake) | 4h | anti-flake structurel |
| 6 | Layout shift detection (CLS-like) | 1j | qualité visuelle |

**Pourquoi cet ordre** :
- Phase 1 d'abord parce que c'est `cdp_session.on(event)` simple, pas de filtrage, valeur immédiate
- Phase 2 sépare capture brute (validable) de la génération d'assertions (qui dépend de la qualité de la capture)
- Phase 3 dépend de Phase 2 (les assertions utilisent le log Network)
- Phase 4 et 5 indépendants, peuvent paralléliser
- Phase 6 en dernier parce que le plus complexe (refactor des waits dans qa_explorer)

### 9.3.1 Pièges techniques V3 — à connaître AVANT d'implémenter

**Piège 1 : Filtrage du bruit Network**

`Network.requestWillBeSent` capture **toutes** les requêtes : images, fonts, CSS, analytics beacons, CDN, etc. Sans filtre, `network_log.json` est inexploitable (200 entries pour 5 vrais API calls).

Mitigation à coder dès la Phase 2 :
```python
# Garder seulement Fetch / XHR / Document
KEEP_TYPES = {"Fetch", "XHR", "Document"}
def on_request(event):
    if event["type"] in KEEP_TYPES:
        network_log.append(event)
```

À NE PAS confondre avec une "pollution par le screencast" : le CDP `Page.screencastFrame` arrive sur le WS CDP en JSON-RPC, pas comme requête HTTP, donc il n'apparaît jamais dans le Network domain. Pas besoin de filtrer le screencast — on filtre les assets/CSS/fonts/images du SUT.

**Piège 2 : Coverage.startPreciseCoverage TIMING CRITIQUE**

`Coverage.startPreciseCoverage` doit être appelé **avant que le JS bootstrap de la page testée ne s'exécute**, sinon le bootstrap n'est pas instrumenté et les % sortent faux (sous-estimés massifs).

Ordre obligatoire dans `qa_explorer.py` :
```python
1. pw.chromium.launch(...)                                  # nouveau Chromium
2. cdp = await browser.new_browser_cdp_session()
3. await cdp.send("Profiler.enable")                        # prérequis Coverage
4. await cdp.send("Coverage.startPreciseCoverage", {        # AVANT browser-use
       "detailed": True, "callCount": False
   })
5. await context.add_init_script(DOM_LISTENER_JS)
6. browser_session = BrowserSession(cdp_url=...)
7. agent.run(...)                                           # navigate + actions ICI
8. coverage = await cdp.send("Coverage.takePreciseCoverage")
```

Si tu enables après `agent.run()`, le bundle initial chargé est marqué comme "non exécuté" alors qu'il l'a été. Bug subtil et silencieux.

**Piège 3 : Sélection de target CDP (Browser session vs Page session)**

`browser.new_browser_cdp_session()` attache au **navigateur entier**, pas à une page spécifique. Pour les domaines `Network`, `Runtime`, `Console`, `DOM` qui sont **par-page**, il faut attacher à la page :

```python
# CORRECT pour Network/Runtime/Console/DOM
cdp = await page.context.new_cdp_session(page)
await cdp.send("Network.enable")
cdp.on("Network.requestWillBeSent", on_request)

# CORRECT pour Browser-level (Coverage, Page screencast)
cdp = await browser.new_browser_cdp_session()
await cdp.send("Coverage.startPreciseCoverage", {...})
```

Si tu mixes, soit tu rates des events (Network sur browser session ne capture rien), soit tu doubles (le multi-onglet va dispatcher le même event sur plusieurs sessions).

**Piège 4 : CDP supporte plusieurs clients sans conflit**

Contrairement à une intuition naturelle, **CDP autorise N clients en parallèle** sur la même cible. Le screencast côté `server.py` et la capture Network côté `qa_explorer.py` ne se "marchent pas dessus" — chaque session reçoit ses events indépendamment. **MAIS** la recommandation de tout faire côté `qa_explorer.py` reste juste, pour 2 raisons orthogonales aux conflits :

1. **Locality** : qa_explorer écrit déjà tous les artefacts dans `runs/<id>/`, c'est le bon endroit pour `network_log.json` / `js_errors.json` / etc.
2. **Lifecycle** : qa_explorer sait quand le run est fini (après `agent.run()` retourne), il peut faire `Network.disable` proprement. Le serveur ne sait pas quand kill ces collectes.

Le serveur garde uniquement le screencast (forwarding pur, pas de logique métier).

### 9.4 V4 (premium / scale, ~2 semaines)

| Feature | Effort | Valeur |
|---|---|---|
| Grille N×N de canvas dans une seule page (12 runs visibles simultanément) | 1j | démo wow |
| Semaphore concurrence côté serveur pour limiter à K runs simultanés | 2h | safety |
| Persistance via SQLite (au lieu de fichiers meta.json) pour query rapide | 1j | scale historique |
| Scheduler intégré : run un parcours toutes les heures + alerte sur diff | 2j | regression monitoring |
| Webhook out : POST le résultat d'un run vers Slack/Teams/Discord | 4h | intégration |
| Multi-LLM : Anthropic, Google Gemini, DeepSeek, Mistral en plus d'OpenAI/Groq | 1j | universalité |
| Authentification (single-user pour l'instant) : login/SSO/SAML | 3-5j | enterprise-ready |
| Export Markdown du rapport (pour Confluence/Notion) | 2h | livrables |
| Mode batch : import d'une liste de scenarios JSON, exécution séquentielle/parallèle | 1j | régression suite |

### 9.5 V5 (idées spéculatives)

- Plugin VS Code : "Click on a test failure → upload to DOMAutopsy → get fixed selectors"
- Plugin Chrome : enregistre directement depuis la session active du dev
- Mode "diff visuel" : screenshot avant/après, highlight des changements
- LLM local (Ollama) en option pour les boîtes qui interdisent les LLM externes
- Distribution : exe Windows / dmg Mac via PyInstaller
- Cloud SaaS : runs hébergés, dashboard équipe, billing par run

---

## 10. Comparaison concurrentielle

| Feature | DOMAutopsy | SDET-GENIE | Rebrowse | web-eval-agent | Saik0s/mcp-browser-use | Selenium IDE | Playwright codegen |
|---|---|---|---|---|---|---|---|
| Capture DOM réelle | ✅ listener JS | ❌ NL→code | ⚠️ video | ❌ visuel | ⚠️ skill API | ✅ recorder | ✅ codegen |
| 4 formats sortie | ✅ | ❌ PW seul | ⚠️ ? | ❌ | ❌ pas de code | ❌ Selenium seul | ❌ PW seul |
| Redacting auto credentials | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Sélecteurs validés runtime | ✅ cascade 7-tier | ❌ | ❌ | ❌ | ❌ | ⚠️ basique | ⚠️ basique |
| Multi-run parallèle | ✅ | ❌ | ❌ | ❌ | ⚠️ via MCP | ❌ | ❌ |
| Live screencast web | ✅ CDP | ❌ | ❌ | ❌ | ✅ dashboard | ❌ | ❌ |
| Listener decouple driver | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| AI cleanup avec anomalies | ✅ | ⚠️ génère | ❌ | ❌ | ❌ | ❌ | ❌ |
| Import scripts existants | 🔜 V2 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Mock API auto-généré | 🔜 V3 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Audit RGPD 3rd parties | 🔜 V3 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| Coverage % par scenario | 🔜 V3 | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |

---

## 11. Limitations et anti-features

- **Single-machine** : Chromium lancé localement, CDP en localhost. Pour SaaS multi-tenant il faudrait dockeriser un Chromium par run.
- **Pas d'auth multi-user** : v0 single-user, le serveur est sur localhost. Pour partage équipe, à ajouter (V4).
- **Pas de support iframe profonde** : le listener est injecté au top frame, les iframes cross-origin ne sont pas captées (limitation security browser).
- **Sites avec antibot agressif** : DDG, Google, Cloudflare → captcha. Pas notre périmètre, c'est un problème inhérent à toute automation.
- **Vision LLM coûteuse** : sur OpenAI gpt-4.1-mini avec vision, un parcours de 20 steps = ~50K tokens. Sur Groq free tier (8K TPM), c'est rejeté. Recommandation : Dev tier Groq ou OpenAI.
- **Pas d'AST parser** côté script_parser.py : regex pures, couvre 95% des tests linéaires mais peut rater des cas exotiques (pattern dynamique, helper functions, etc.).

---

## 12. Variables d'environnement et ports

| Variable | Rôle | Requise si |
|---|---|---|
| `OPENAI_API_KEY` | Clé OpenAI | `--provider openai` (défaut) |
| `GROQ_API_KEY` | Clé Groq | `--provider groq` |

| Port | Rôle |
|---|---|
| 8000 | Serveur FastAPI (UI web) |
| 9222-9272 | Plage CDP Chromium (1 par run actif) |

---

## 13. Stack technique

| Composant | Version min | Rôle |
|---|---|---|
| Python | 3.12 | Runtime principal |
| browser-use | 0.12.6 | Agent IA pilotant Chromium |
| playwright | 1.49 | Driver navigateur + CDP |
| openai | 1.50 | SDK OpenAI (compat Groq) |
| fastapi | 0.115 | Serveur HTTP + WebSocket |
| uvicorn[standard] | 0.32 | ASGI server |
| pydantic | 2.0 | Validation request bodies |
| aiohttp | 3.10 | Client CDP WebSocket pour screencast |
| python-dotenv | 1.0 | Chargement .env |
| PySide6 | 6.8 | GUI desktop legacy (scenario_builder) |
| Chrome.js (CDN) | dernière | Charts dans rapport HTML |

---

## 14. Pitch en 30 secondes

> *"DOMAutopsy fait exécuter le parcours par un agent, puis retrouve chaque nœud exact via CDP et mesure ses candidats de locator dans le DOM vivant. Le nettoyage et la classification post-capture sont locaux, déterministes et fail-closed : aucun LLM n'invente un sélecteur ou ne décide silencieusement quoi rejouer. Le résultat canonique est un test Playwright TypeScript sans LLM au replay, avec assertions fonctionnelles natives."*

---

---

## 15. Refactor Août 2026 — unification du replay sur Playwright TS

### 15.1 Motivation

Avant ce refactor, deux moteurs d'exécution coexistaient sans converger :

- `qa_explorer.py` produisait un `test_<format>.<ext>` (via LLM, format demandé par l'utilisateur) — utilisé comme livrable
- `/api/replay/{run_id}` lançait `qa_player.py` — un runner Python-Playwright limité à `click` + `input`, qui **ignorait** ce `test_*.ts` généré et réimplémentait un moteur à côté

Résultat : le format généré n'était jamais exécuté en interne, `qa_player.py` divergeait fonctionnellement (support partiel), et la roadmap V3+ risquait de dériver en maintenant les deux.

### 15.2 Décision : `test_playwright.spec.ts` = format canonique interne

Depuis le refactor :
- **Toujours généré** par `qa_explorer` (même si l'export livrable demandé est Katalon/Cypress/Selenium)
- **Rejoué directement** par `/api/replay/{run_id}` via `npx playwright test <spec-relatif> --workers=1`
- **Format canonique interne**, indépendant du choix d'export utilisateur

### 15.3 Nouveaux modules

| Fichier | Rôle |
|---|---|
| `schemas.py` | Modèles Pydantic v2 versionnés (`schema_version="2.0"`), 25+ champs par step, migration transparente des anciens JSON |
| `selector_enricher.py` | Résolution `backend_node_id` → nœud exact via CDP, génération et mesure live des candidats, cache des preuves par step |
| `clean_steps_builder.py` | Pipeline unifié : extraction BU → fusion multi-sources → détection sensitive → **classification locale stricte sans LLM** |
| `playwright_generator.py` | Traduction **déterministe** JSON→TS (15 actions, encapsulation `test.step("[step-XXXX] ...")` pour rapprochement rapport, sensitive→`process.env`, action inconnue→`throw`) |
| `replay_reporter.py` | Rapport HTML self-contained pour les runs replay (rapproche `[step-XXXX]` du JSON reporter Playwright avec les données du `clean_steps.json` source) |

### 15.4 Nouveau pipeline `qa_explorer.run()`

```
1. Chromium + CDP + DOM listener (identique)
2. agent.run() (identique)
3. extract_browser_use_history(agent, result)   # NOUVEAU : 3 fallbacks API BU
   -> browser_use_history.json
4. build_clean_steps(scenario, bu_history, dom_log, network)   # NOUVEAU
   -> CleanSteps Pydantic v1.0 → clean_steps.json (toutes actions)
5. generate_playwright_ts(clean_steps, spec.ts)   # NOUVEAU : toujours
   -> test_playwright.spec.ts (format canonique)
6. deterministic_exporters     # si output_format != playwright
   -> test_<format>.<ext> (Katalon / Cypress / Selenium sans LLM)
7. generate_report(clean_data_dict, ...)   # étendu : all actions + Rejoué col
   -> qa_report_<ts>.html
8. meta.json enrichi : schema_version, bu_history_count, sensitive_env_vars,
   clean_steps_included/filtered, replay_blocking_steps, playwright_spec_present
```

### 15.5 Nouveau flow `/api/replay/{run_id}`

```
Détection : test_playwright.spec.ts présent dans le run source ?
│
├─ OUI (moteur PRIMAIRE - playwright_ts) :
│    spec_rel = spec.relative_to(ROOT).as_posix()   # RELATIF, jamais absolu Windows
│    DOMAUTOPSY_REPLAY_JSON = <replay_dir>/replay_results.json
│    subprocess.Popen(npx playwright test <spec_rel> --workers=1 --output=<dir>)
│    Windows : shell=True + quoting (npx est un .cmd)
│    Streaming logs via WebSocket (reporter=list dans config)
│    JSON reporter dans le fichier via env var → post-processing
│    
├─ NON (fallback LEGACY - qa_player_legacy) :
│    print("[server] /api/replay/... -> FALLBACK LEGACY qa_player.py")
│    meta.json : engine=qa_player_legacy, legacy_fallback=true, reason=...
│    subprocess.Popen(python qa_player.py --run-dir ... --output-dir ...)
│
└─ Post-fin subprocess (via _pump_stdout hook) :
     _postprocess_replay(run_id, replay_dir)
     ├─ resolve source_run_dir via source_run_id
     ├─ generate_replay_report(replay_dir, source_run_dir)
     │    → replay_report.html (rapproche [step-XXXX] au clean_steps source)
     └─ update_replay_meta_with_verdict(replay_dir)
          → meta.json enrichi : replay_passed/failed/skipped/duration_ms
     (SECONDAIRE : un échec ici ne transforme pas un test passé en fail)
```

### 15.6 Sécurité + cross-OS

- **Sensitive values** : `sensitive: true` dans le JSON déclenche `env_var = "DOMAUTOPSY_STEP_XXXX"`. Le TS émet `process.env.DOMAUTOPSY_STEP_XXXX` avec `throw new Error` explicit si la var est absente. Jamais la valeur en clair dans le TS, le JSON, les logs ou le rapport.
- **Chemin spec RELATIF** : `spec.relative_to(ROOT).as_posix()` évite d'interpréter `C:\...` comme expression de filtrage Playwright.
- **`shell=True` cross-OS** : uniquement sur Windows, uniquement pour `npx` (qui est un `.cmd`), sans interpolation utilisateur — mêmes garanties que `/api/playwright/run` déjà en place.
- **`channel: 'chromium'`** dans `playwright.config.ts` : réutilise le Chromium classique déjà en cache de Playwright Python (`ms-playwright/chromium-XXXX`), évite de télécharger le nouveau `chromium_headless_shell-XXXX` séparé introduit en Playwright JS 1.49+.

### 15.7 Fallback legacy — dépréciation en cours

`qa_player.py` :
- Reste utilisé pour les runs pré-refactor sans `test_playwright.spec.ts`
- **Ne doit plus évoluer** — toute nouvelle action passe par `playwright_generator`
- Docstring + banner runtime marqués LEGACY FALLBACK
- Réponse `/api/replay` expose `legacy_fallback: true` + `legacy_fallback_reason` pour que le CLI/UI signale à l'utilisateur

### 15.8 Runtime autonome — résolveur en place, packaging à décider

**Le résolveur runtime est implémenté** dans `server.py::_resolve_embedded_runtime()`. Trois variables `.env` optionnelles :

```env
DOMAUTOPSY_NODE_PATH=runtime/node/node.exe
DOMAUTOPSY_PLAYWRIGHT_CLI=runtime/node_modules/@playwright/test/cli.js
DOMAUTOPSY_BROWSERS_PATH=runtime/browsers
```

Chemins résolus relativement à `ROOT`. Si les 3 sont set et pointent sur des fichiers existants, `/api/replay` lance directement :

```python
[node_path, playwright_cli, "test", spec_rel, "--workers=1", f"--output={output_rel}"]
# + env["PLAYWRIGHT_BROWSERS_PATH"] = browsers_path
```

Sinon fallback automatique en mode DEV sur `npx playwright test` global + cache Playwright utilisateur, avec log clair. `meta.json` des runs replay expose `runtime_mode: "embedded" | "system_npx"` pour traçabilité.

Le layout attendu à fournir par l'installateur :
```
DOMAutopsy/
└── runtime/
    ├── node/node.exe                       # Node local, jamais celui du PATH
    ├── node_modules/@playwright/test/      # version verrouillee associee
    └── browsers/chromium-XXXX/             # binaire embarque
```

**Résolveur ET provisionneur sont en place.** Le helper `runtime_installer.py` télécharge Node officiel + fait `npm ci` du lockfile + télécharge Chromium isolé dans `runtime/browsers/` :

```bash
python domautopsy_cli.py runtime install          # idempotent
python domautopsy_cli.py runtime status           # JSON exit 0/1
python domautopsy_cli.py runtime install --force  # re-installe tout
```

Détails du provisionnement :
- **Node** : télécharge depuis `nodejs.org/dist/vXX/` officiel (archétype auto-détecté : `win-x64` / `linux-x64` / `darwin-x64` / `darwin-arm64`), extrait dans `runtime/node/`, jamais dans le PATH
- **@playwright/test** : `npm ci` avec le Node embarqué (PATH prefixé), respecte le `package-lock.json` versionné, installe dans `runtime/node_modules/`
- **Chromium** : `node cli.js install chromium` avec `PLAYWRIGHT_BROWSERS_PATH=runtime/browsers` — isolé du cache utilisateur
- **Manifest** : `runtime/runtime_manifest.json` enregistre versions réelles installées + SHA-256 du binaire Node + timestamps + chemins relatifs (audit)
- **Idempotent** : `install()` reprend proprement si Node est déjà là ; `--force` re-télécharge tout
- **Fallback dev automatique** : si le runtime n'est pas provisionné, `/api/replay` détecte et bascule sur `npx` global + cache utilisateur (mode dev), avec `runtime_mode: "system_npx"` dans le `meta.json`

**Ce qui reste côté distribution** — packaging effectif des binaires vers l'utilisateur final :

- **`pip install domautopsy` + `python -m domautopsy_cli runtime install`** : wheel PyPI léger + le user lance le helper post-install (même pattern que `playwright install` pour Playwright Python). Simple, universel, un télé-chargement.
- **Image Docker** : `docker pull julienmerconsulting/domautopsy` avec `runtime/` déjà provisionné (Dockerfile fait `RUN python domautopsy_cli.py runtime install`).
- **Installeur natif** (msi/dmg via PyInstaller) : le dossier `runtime/` déjà provisionné est embarqué dans le bundle. Décrit dans README section V5.

**Critère d'acceptation autonomie** : lancer DOMAutopsy sur une machine sans Node installé et avec un cache Playwright utilisateur vide → `python domautopsy_cli.py runtime install` puis `/api/replay` fonctionne uniquement avec les binaires de `runtime/`. Le code sait le faire de bout en bout.

### 15.9 Tests couverture cahier des charges

Suite `tests/` — 50 tests, 12.6s de run, dont 1 E2E réel :

| Fichier | Items cahier | Nb tests |
|---|---|---|
| `test_schemas.py` | #5 validation JSON, #6 unknown actions, #11 legacy compat | 8 |
| `test_playwright_generator.py` | #7 génération TS per-action, #8 exclusion parasites, #9 sensitive protection | 16 |
| `test_clean_steps_builder.py` | #1 scrolls, #2 clicks dedup, #3 inputs consolidation, #4 autres actions, #6 unknown, #9 env_var | 15 |
| `test_replay_routing.py` | #12 npx PW test, #13 no qa_player, #14 fallback legacy, #15 chemin relatif | 8 |
| `test_report_generator.py` | #10 all actions rendered + FILTRE with reason | 2 |
| `test_e2e_integration.py` | #16 scénario multi-action, exécution réelle Chromium via `npx playwright test` sur page HTTP locale | 1 |

Lancement : `python -m pytest tests/` (deps : `pytest`, `httpx` dans `requirements-dev.txt`).

---

*Document maintenu à jour à chaque commit majeur. Dernière maj : 2026-08-29 (refactor unification Playwright TS).*
