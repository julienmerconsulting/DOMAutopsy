<p align="center">
  <img src="https://img.shields.io/badge/🔬_DOMAutopsy-Intelligent_QA_Capture_Server-ef4444?style=for-the-badge&labelColor=0b1220" alt="DOMAutopsy" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/FastAPI-async_server-009688?style=flat-square&logo=fastapi&logoColor=white" />
  <img src="https://img.shields.io/badge/browser--use-AI_Agent-10B981?style=flat-square" />
  <img src="https://img.shields.io/badge/Playwright-CDP_driver-2EAD33?style=flat-square&logo=playwright&logoColor=white" />
  <img src="https://img.shields.io/badge/Multi--run-50_parallèles-3b82f6?style=flat-square" />
  <img src="https://img.shields.io/badge/WebSocket-live_logs_+_screencast-8b5cf6?style=flat-square" />
  <img src="https://img.shields.io/badge/Bearer_Auth-master_+_TTL_tokens-f59e0b?style=flat-square" />
  <img src="https://img.shields.io/badge/Shadow_DOM-supported-d946ef?style=flat-square" />
  <img src="https://img.shields.io/badge/licence-MIT-green?style=flat-square" />
</p>

<p align="center">
  <strong>Autopsie chirurgicale du DOM. Serveur multi-run. Live screencast. Code test prêt.</strong><br/>
  Un agent IA navigue. Un listener capture. L'IA nettoie. Tu reçois le code — et tu regardes le navigateur travailler en direct dans ton onglet.<br/>
  <em>Framework-agnostique. CI-ready. Zéro record/replay. Zéro sélecteur fragile.</em>
</p>

---

## 🎯 Pourquoi DOMAutopsy

Les outils existants (Selenium IDE, Katalon Recorder, Cypress Studio) font du **record/replay** : ils enregistrent ce qu'ils voient, avec les sélecteurs qu'ils trouvent — souvent des XPath absolus ou des classes CSS générées qui cassent au premier refactoring.

DOMAutopsy fait de l'**autopsie intelligente** : un agent IA navigue comme un humain, puis le nœud exact est retrouvé avant fermeture du navigateur. Les candidats de sélecteurs sont mesurés dans le DOM vivant et le nettoyage est local, déterministe et fail-closed. Tu reçois du code prod.

Si une interaction ne possède aucune preuve live unique et stable, elle reste visible dans les artefacts mais porte `replay_blocking=true` : le replay est refusé au lieu d'exécuter silencieusement un scénario partiel.

Et depuis la v0.5, c'est un **serveur web async multi-run** : tu lances N runs en parallèle, tu regardes chacun en live screencast dans ton navigateur, tu intègres tout ça à ton CI/CD via le CLI et le dashboard `/ci/{run_id}` partageable.

| | Selenium IDE | Katalon Recorder | Playwright codegen | **DOMAutopsy** |
|---|---|---|---|---|
| Approche | Record/replay | Record/replay | Record/replay | **Agent IA + DOM Listener découplés** |
| Sélecteurs | XPath absolu / CSS fragile | XPath / CSS | XPath / CSS | **Cascade 7 niveaux, validée runtime** |
| Shadow DOM | ❌ | ❌ | ⚠️ partiel | **✅ natif (chaîne `>>>`)** |
| Nettoyage post-capture | ❌ | ❌ | ❌ | **✅ local, déterministe, sans LLM** |
| Redacting credentials | ❌ | ❌ | ❌ | **✅ automatique (sélecteur conservé)** |
| 4 formats sortie | ❌ | ❌ Katalon seul | ❌ PW seul | **✅ Katalon / Playwright / Cypress / Selenium** |
| Multi-run parallèle | ❌ | ❌ | ❌ | **✅ 50 simultanés, port CDP unique chacun** |
| Live screencast web | ❌ | ❌ | ❌ | **✅ WebSocket CDP, 1 capture → N viewers** |
| Mode CI + CLI | ❌ | ⚠️ | ⚠️ | **✅ Bearer token TTL + dashboard partageable** |
| Replay sans LLM | ❌ | ❌ | ❌ | **✅ `test_playwright.spec.ts` généré + `npx playwright test`** |
| Import scripts existants | ❌ | ❌ | ❌ | **✅ parse Katalon/PW/Cy/Sel → NL task** |
| Prix | Gratuit | Gratuit | Gratuit | **0€** |

---

## ⚡ Démarrer

### Prérequis

- Python 3.12+
- (optionnel) Caddy 2.x pour le déploiement public/VPN
- Une clé API OpenAI ou Groq

### Installation

```bash
git clone https://github.com/julienmerconsulting/DOMAutopsy
cd DOMAutopsy

# 1. Python (browser-use + playwright + fastapi + openai + le reste)
pip install -r requirements.txt
python -m playwright install chromium   # cache: ~/AppData/Local/ms-playwright/chromium-XXXX

# 2. Node/Playwright TS (pour le nouveau replay via `npx playwright test`)
#    Depuis le refactor Aout 2026, /api/replay execute le TS canonique
#    genere. Le cache Chromium Python (chromium-XXXX) est reutilise via
#    `channel: 'chromium'` dans playwright.config.ts - pas besoin de
#    re-telecharger.
#
#    IMPORTANT : `npm ci` (pas `npm install`). Le lockfile fige
#    @playwright/test a la version testee compatible avec le Chromium
#    Python en cache. Un `npm install` peut resoudre une version plus
#    recente et casser cette compatibilite.
npm ci

# Ou tout en une commande (utilise les scripts npm) :
#   npm run setup

# ATTENTION : ne pas lancer `pip install --upgrade` sur browser-use,
# playwright, ou toute autre lib versionnee `==` dans requirements.txt.
# L'API browser-use bouge entre versions (les fallbacks defensifs de
# extract_browser_use_history sont testes contre 0.12.9). Un upgrade
# non teste peut casser silencieusement l'extraction d'historique.

cp .env.example .env
# édite .env : remplis OPENAI_API_KEY (ou GROQ_API_KEY)
```

### Lancer les tests (verification setup)

```bash
python -m pip install pytest httpx     # deps de dev, isolees de requirements.txt
python -m pytest tests/                # 103 tests, dont 1 E2E reel via npx playwright
```

### Installation autonome (optionnel — pour prod / distribution)

Le mode DEV utilise le `npx` global + le cache Chromium partagé de Playwright Python. Pour un déploiement **sans dépendance système** (installateur, Docker, machine sans Node), provisionner un runtime isolé dans `runtime/`. À noter : le provisionnement lui-même télécharge Node + Chromium depuis Internet — c'est l'**exécution** post-install qui est autonome, pas l'install.

```bash
python domautopsy_cli.py runtime install    # télécharge Node LTS + @playwright/test + Chromium
python domautopsy_cli.py runtime status     # exit 0 si complet, 1 si incomplet
```

Puis dans le `.env` (voir `.env.example`) :

```env
DOMAUTOPSY_NODE_PATH=runtime/node/node.exe
DOMAUTOPSY_PLAYWRIGHT_CLI=runtime/node_modules/@playwright/test/cli.js
DOMAUTOPSY_BROWSERS_PATH=runtime/browsers
```

`/api/replay` détecte automatiquement le runtime et bascule dessus. Sinon fallback dev sur `npx` global. Traçabilité complète : `runtime/runtime_manifest.json` (versions réelles + SHA-256 + timestamps).

### Lancer le serveur

```bash
python server.py --port 8000
# ouvre http://localhost:8000
```

C'est tout. Le serveur est prêt — UI web, API REST, WebSocket logs+screencast, dashboard CI, le tout sur un seul port.

> 💡 **Mode no-auth par défaut** : tant que `DOMAUTOPSY_API_TOKEN` n'est pas set dans `.env`, le serveur tourne sans authentification (parfait pour dev local). Active l'auth bearer en une ligne — section [Authentification](#-authentification) plus bas.

---

## 🧠 La cascade de sélecteurs — le cœur

Le listener JS injecté dans Chromium applique une **cascade stricte** à chaque clic et chaque input. Il choisit le sélecteur le plus robuste disponible, **valide son unicité en temps réel via `querySelectorAll` ou `document.evaluate`**, et remonte si nécessaire.

| Priorité | Stratégie | Exemples |
|----------|-----------|---------|
| 🟢 **Tier 1** — `data-testid` · `id` · `name` | **Attributs stables** — IDs sémantiques, attributs `name`, `data-testid`. Uniques par nature. | `#loginButton` · `[name="email"]` · `[data-testid="submit"]` |
| 🔵 **Tier 2** — `aria-label` · `placeholder` · `title` | **Attributs sémantiques (accessibilité)** — Stables car porteurs de sens métier. Doublon a11y → automatisation, **1 pierre 2 coups**. | `[aria-label="Rechercher"]` · `input[placeholder="Email"]` |
| 🩵 **Tier 3** — `href` | **Liens par href** — Uniquement si href non générique (`#`, `/`, `javascript:void`) et < 100 chars. | `a[href="/products/catalog"]` |
| 🟣 **Tier 4** — parent stable | **Remontée au parent (icônes, SVG)** — Si l'élément cliqué est une icône ou un SVG sans attribut, remonte au parent interactif via `composedPath()` + `closest()`. | `[aria-label="Fermer le menu"]` |
| 🟡 **Tier 5** — `label XPath` | **Label associé (inputs)** — Pour les inputs sans attribut stable, trouve le `<label for>` ou `<label>` wrapping. | `//label[contains(text(),"Mot de passe")]//input` |
| 🔴 **Tier 6** — `CSS court` | **Dernier recours CSS** — Max 2 classes stables + `nth-of-type`. | `button.btn-primary:nth-of-type(2)` |
| 🟤 **Tier 7** — `XPath texte` | **Auto-promotion** — Si Tier 1-6 retourne `matchCount > 1` mais le texte est court et unique, escalade auto en XPath text-based. | `//button[contains(text(),"Valider")]` |
| 🌑 **Shadow DOM** — chaîne `>>>` | **Détection automatique** — Construction de la chaîne avec `>>>` (Playwright) et `.shadowRoot.querySelector()` (JS). | `my-component >>> [aria-label="Submit"]` |

**Validation runtime** : chaque sélecteur sort de la cascade avec `unique: bool` + `matchCount: N`. Pas de devinette : ce qui est marqué `unique: true` l'est, mesuré dans le DOM réel au moment de la capture.

**Bonus sécurité** : détection auto des champs sensibles (`type="password"`, autocomplete `cc-*`/`current-password`/`otp`, patterns `secret|token|ssn|cvv|pin|api_key` sur `name|id|aria-label|placeholder|data-testid`). Le sélecteur est conservé pour générer un test rejouable, **mais la valeur est purgée et marquée `sensitive: true`**. Les credentials ne quittent **jamais** la machine, ne sont **jamais** envoyés à l'IA de cleanup, ne figurent **jamais** dans le rapport HTML.

---

## 🏗️ Architecture en un coup d'œil

```
┌──────────────────┐    HTTP REST + 2 WebSockets    ┌──────────────────────────┐
│   Navigateur     │ ◄────────────────────────────► │  Serveur FastAPI         │
│   (UI web)       │                                │  server.py — port 8000   │
│                  │  /ws/logs/{run_id}             │                          │
│   ▸ form         │  /ws/screen/{run_id}           │  Bearer auth (optionnel) │
│   ▸ canvas live  │                                │  Alloc CDP 9222-9272     │
│   ▸ log box      │                                │  ScreencastHub (1→N)     │
│   ▸ history      │                                │  Lifespan cleanup        │
└──────────────────┘                                └──────────────┬───────────┘
                                                                   │
                                                                   │ subprocess.Popen
                                                                   ▼
                       ┌─────────────────────────────────────────────────────────┐
                       │  qa_explorer.py (1 sous-process par run)                │
                       │                                                          │
                       │  Lance Chromium avec --remote-debugging-port=<unique>   │
                       │  Inject dom_listener.js (3 chemins : add_init_script,   │
                       │    page.evaluate, CDP)                                   │
                       │  Pilote browser-use (Agent + LLM)                       │
                       │  Récupère localStorage.__qaLocatorLog                   │
                       │  Dédup → AI cleanup → génère code test → HTML rapport   │
                       │  Écrit tout dans runs/<timestamp>_<run_id>/             │
                       └──────────────────────────┬──────────────────────────────┘
                                                  │ CDP port 9222+offset
                                                  ▼
                       ┌─────────────────────────────────────────────────────────┐
                       │  Chromium (1 instance par run)                          │
                       │   ▸ dom_listener.js capture click/input/scroll          │
                       │   ▸ browser-use agent décide les actions                │
                       │   ▸ CDP exposé : Page, DOM, (V3 : Network, Console...)  │
                       └─────────────────────────────────────────────────────────┘
```

📐 Architecture détaillée fichier-par-fichier, schémas séquentiels, pièges techniques : **[`ARCHITECTURE.md`](ARCHITECTURE.md)** (~770 lignes).

---

## 🚀 4 modes d'exécution

### Mode 1 — UI web (le défaut)

Ouvre `http://localhost:8000` après `python server.py`. Tu remplis :
- URL cible
- Task en langage naturel (*"Login avec demo/demo, ouvre la facture la plus récente, exporte en PDF"*)
- Format de sortie (Katalon / Playwright / Cypress / Selenium)
- Provider LLM (OpenAI / Groq)
- Timing avancé (min_wait, max_wait, network_idle) pour les SPA lourdes
- Headless ou visible

Tu cliques **Lancer**. Un canvas affiche le **navigateur en streaming live**, une log box scrolle le stdout du subprocess. À la fin : boutons *"Voir le rapport HTML"* et *"Voir le code de test"*. L'historique des runs est dans la sidebar.

### Mode 2 — CLI client `domautopsy_cli.py` (CI-friendly)

Pour déclencher un run depuis GitHub Actions, Jenkins, ou ton terminal :

```bash
# Lance + attend le verdict (exit code 0/1)
python domautopsy_cli.py run \
  --server https://dom.example.com \
  --url https://app.example.com/login \
  --task "Login avec demo/demo, valide" \
  --format playwright \
  --token $DOMAUTOPSY_TOKEN \
  --wait

# Lance + tail le log en direct
python domautopsy_cli.py run --server ... --url ... --task ... --tail

# Suivi d'un run en cours
python domautopsy_cli.py status --server ... --run-id abc123def456
```

Le CLI utilise uniquement `urllib` stdlib pour le polling REST (zéro dépendance), et fallback proprement si la lib `websockets` est absente pour `--tail`.

### Mode 3 — Playwright native runner

Tu as déjà une suite Playwright (TypeScript / JS) ? Tu peux la lancer **via le serveur**, qui stream `stdout` en WebSocket et écrit un `meta.json` réutilisable :

```bash
python domautopsy_cli.py playwright \
  --server https://dom.example.com \
  --project-dir /workspace/my-app/tests \
  --target login.spec.ts \
  --args "--workers 4 --grep happy-path" \
  --token $DOMAUTOPSY_TOKEN \
  --wait
```

Le serveur appelle `npx playwright test` dans ton `project-dir`, force `--reporter=list` pour un output stream-friendly, et garde la même API `/ws/logs/{id}` que les runs browser-use. Pas de screencast (Playwright peut spawner N workers, pas de CDP unique).

### Mode 4 — Replay d'un run historique (zéro LLM)

Tu as un run validé hier ? Tu veux le rejouer aujourd'hui pour vérifier que rien n'a cassé ? `/api/replay/{run_id}` lance **`npx playwright test`** sur le `test_playwright.spec.ts` généré systématiquement par `qa_explorer` (format canonique interne). Zéro LLM, déterministe.

```bash
python domautopsy_cli.py replay \
  --server https://dom.example.com \
  --run-id abc123def456 \
  --wait
```

**Sous le capot** (depuis le refactor Août 2026) :
- Chaque capture produit toujours un `test_playwright.spec.ts` complet (toutes actions du Scenario Builder + natives browser-use + fallback `unknown`), avec chaque étape encapsulée dans `test.step('[step-XXXX] ...')` pour le rapprochement du rapport.
- Le replay lance `npx playwright test <spec-relative> --workers=1` avec deux reporters (list stream pour le WebSocket, JSON pour le rapport HTML final consommé par `replay_reporter.py`).
- Un `replay_report.html` self-contained est généré à la fin, rapproché avec le `clean_steps.json` du run source (action + sélecteur + expected/actual + réseau associé).
- Les valeurs sensibles (`sensitive: true` dans le JSON) sont substituées par des variables d'environnement `DOMAUTOPSY_STEP_XXXX` dans le TS — jamais en clair. À positionner avant `/api/replay`.

**Fallback legacy** : pour les runs pré-refactor sans TS canonique, `qa_player.py` reste utilisé mais avec un warning explicite dans les logs + `engine: "qa_player_legacy"` dans le `meta.json`. Il ne supporte que `click` et `input` et ne sera plus étendu.

### Bonus — Import d'un script existant

`POST /api/import` accepte un fichier `.groovy` / `.ts` / `.js` / `.spec.ts` / `.py`. Le serveur parse l'URL initiale + les sélecteurs + les actions, **redact automatiquement les valeurs sensibles**, et retourne une **task en langage naturel suggérée** prête à être autofillée dans le form web. Utile pour transformer ta suite legacy en run DOMAutopsy comparable.

---

## 📺 Le serveur en détail

### Multi-run parallèle

Chaque run obtient :
- un `run_id` (uuid hex 12 chars)
- un **port CDP unique** dans la plage `9222-9272`
- un **dossier `runs/<timestamp>_<run_id>/`** isolé
- une **queue stdout** alimentée par `_pump_stdout` (lecture bloquante déléguée au thread pool)
- un **subprocess.Popen** géré par le serveur (kill au shutdown, kill manuel via `DELETE /api/run/{id}`)

Jusqu'à **50 runs parallèles théoriques** (limite = plage de ports CDP).

### Live screencast — 1 capture CDP → N viewers

C'est le détail technique le plus subtil : **plusieurs onglets peuvent regarder le même run**. Naïvement, chaque viewer ouvrirait sa propre session CDP → N streams Chromium → CPU saturation + bande passante × N.

DOMAutopsy implémente un **`ScreencastHub`** par run : **1 seule connexion CDP** lit les `Page.screencastFrame` (jpeg base64) et **broadcast** vers tous les WebSockets viewers. Quand le dernier viewer se déconnecte, le hub stoppe la capture pour libérer Chromium. **Cache de la dernière frame** envoyé immédiatement aux nouveaux viewers — pas d'écran noir au join.

3 variables d'env pour tuner :
- `DOMAUTOPSY_SCREENCAST_EVERY_N=2` — 1 frame sur N envoyée (defaut 2 ≈ 15fps si CDP délivre 30)
- `DOMAUTOPSY_SCREENCAST_QUALITY=60` — qualité JPEG 0-100
- `DOMAUTOPSY_SCREENCAST_MAX_WIDTH=1280` — downscale à la source

### Dashboard CI partageable — `/ci/{run_id}`

Page HTML publique qui s'auto-connecte au log WebSocket d'un run et affiche son verdict. Cas d'usage canonique :

1. Ton GitHub Action mint un token TTL → lance un run via CLI → récupère `run_id`
2. Le step suivant met `https://dom.example.com/ci/<run_id>` dans le `$GITHUB_STEP_SUMMARY`
3. N'importe qui dans l'équipe ouvre l'URL → voit le log live + le verdict final, sans avoir besoin de credentials

### Historique persistant — `runs/`

Chaque run écrit un `meta.json` dans son dossier. `/api/history` scanne `runs/`, lit chaque meta, trie par date desc. La sidebar UI consomme cet endpoint. Pas de DB pour l'instant (V4 prévoit SQLite pour query rapide).

### Endpoints REST (catalogue rapide)

| Route | Méthode | Auth | Rôle |
|---|---|---|---|
| `/` | GET | — | UI principale |
| `/health` | GET | — | Active runs + auth status (sondes K8s) |
| `/api/run` | POST | bearer | Lance un run browser-use |
| `/api/playwright/run` | POST | bearer | Lance `npx playwright test` |
| `/api/replay/{run_id}` | POST | bearer | Rejoue un run via qa_player |
| `/api/import` | POST | bearer | Parse un script existant (multipart) |
| `/api/run/{id}` | DELETE | bearer | Kill subprocess |
| `/api/runs` | GET | — | Runs en mémoire |
| `/api/runs/{id}` | GET | — | Statut complet d'un run (mémoire OU disque) |
| `/api/history` | GET | — | Runs historiques (limit=N) |
| `/api/report/{id}` | GET | — | Rapport HTML self-contained |
| `/api/run/{id}/files` | GET | — | Liste fichiers d'un run |
| `/api/run/{id}/file/{f}` | GET | — | Sert un fichier (path-traversal-safe) |
| `/api/formats` | GET | — | Formats de sortie supportés |
| `/api/providers` | GET | — | Providers LLM + `key_present` |
| `/ci/{run_id}` | GET | — | Dashboard CI public |
| `/ws/logs/{id}` | WS | — | Stream stdout |
| `/ws/screen/{id}` | WS | — | Stream CDP screencast |
| `/api/auth/token` | POST | master | Mint un token fils |
| `/api/auth/tokens` | GET | master | Liste tokens actifs (sans valeurs) |
| `/api/auth/token/{suffix}` | DELETE | master | Révoque un token fils |
| `/api/auth/me` | GET | bearer | Whoami |

---

## 🔐 Authentification

Modèle simple, deux niveaux :

### Master token

Configuré côté serveur via `DOMAUTOPSY_API_TOKEN` dans `.env`. Immutable, donne tous les droits, sert à **minter des tokens fils**. Si non set : **mode no-auth** (parfait pour dev local).

Génération d'un master fort (256 bits d'entropie) :

```bash
python domautopsy_cli.py auth genkey --env >> .env
# ou
openssl rand -hex 32
# ou
python -c "import secrets; print(secrets.token_hex(32))"
```

### Tokens fils (TTL, in-memory)

Générés à la volée via le master token, avec un **label descriptif** + **TTL en secondes** + **scope cosmétique**. Stockés en mémoire (perdus au restart serveur — le master continue de marcher). Idéal CI :

```bash
# Job de setup en début de pipeline (1 fois)
TOKEN=$(curl -X POST https://dom.example.com/api/auth/token \
  -H "Authorization: Bearer $MASTER_TOKEN" \
  -d '{"label":"gh-action-run-${{ github.run_id }}","ttl_seconds":7200}' \
  | jq -r .token)

# Tous les jobs suivants utilisent $TOKEN
# Il s'auto-détruit dans 2h. Pas besoin de révoquer manuellement.
```

Le CLI a un wrapper plus pratique :

```bash
# Récupère juste le token brut, pratique pour eval
export TOKEN=$(python domautopsy_cli.py auth mint \
  --token $MASTER_TOKEN --label "ci-job-42" --ttl 7200)

# Liste les tokens actifs
python domautopsy_cli.py auth list --token $MASTER_TOKEN

# Révoque un token par son suffix (8 derniers chars)
python domautopsy_cli.py auth revoke --token $MASTER_TOKEN --suffix abc12345
```

---

## 📤 Outputs — 1 dossier par run

```
runs/<YYYYMMDD_HHMMSS>_<runid>/
├── locator_log.json           # capture brute du listener
├── locator_dedup.json         # après dédup (clics consécutifs, dernière valeur input)
├── browser_use_history.json   # historique complet des actions BU (nouveau v0.6)
├── clean_steps.json           # schéma v1.0 (Pydantic-validé), toutes actions,
│                              # marquage included_in_replay + cleanup_reason
├── test_playwright.spec.ts    # TOUJOURS généré : format canonique de replay
├── test_<format>.<ext>        # export livrable optionnel : .groovy / .cy.js / .py
├── qa_report_<ts>.html        # rapport self-contained (Chart.js, dark theme)
├── network_log.json           # trafic HTTP filtré (Fetch/XHR/Document/WS)
├── js_errors.json             # Runtime.exceptionThrown captures
├── console_messages.json      # Console.messageAdded captures
└── meta.json                  # metadata + counts + schema_version + engine
```

**Runs de replay** (dans `runs/<ts>_replay_<id>/`) :
```
├── replay_results.json        # JSON reporter Playwright (verdict per test.step)
├── replay_report.html         # rapport self-contained enrichi source_run
├── stdout.log / stderr.log    # trace complète du subprocess
└── meta.json                  # engine (playwright_ts | qa_player_legacy)
```

### Le rapport HTML (`qa_report_*.html`)

Self-contained, Chart.js depuis CDN, le reste inline. Contient :
- **5 KPIs** : steps nettoyés, actions capturées, sélecteurs uniques, non-uniques, anomalies
- **3 donuts** : répartition des stratégies de sélection, fiabilité (unique vs non-unique), types d'actions
- **Tableau du parcours nettoyé** avec badges de sévérité
- **Tableau des locateurs bruts** avec stratégie et matchCount par entrée
- **Bloc de code généré** avec préformaté (mise en évidence visuelle)
- Thème dark cohérent avec la web UI

### Exemple de step nettoyé

```json
{
  "step": 3,
  "action": "click",
  "description": "Clique sur le bouton Connexion",
  "selector": "[aria-label='Se connecter']",
  "selectorType": "css",
  "unique": true,
  "page": "https://example.com/login"
}
```

### Exemple de code Katalon généré

```groovy
import com.kms.katalon.core.testobject.TestObject
import com.kms.katalon.core.testobject.ConditionType
import com.kms.katalon.core.webui.keyword.WebUiBuiltInKeywords as WebUI

// Etape 3 : Clique sur le bouton Connexion
TestObject btnConnexion = new TestObject("btnConnexion")
btnConnexion.addProperty("css", ConditionType.EQUALS, "[aria-label='Se connecter']")
WebUI.verifyElementPresent(btnConnexion, 10)
WebUI.click(btnConnexion)
WebUI.delay(1)
```

---

## 🚢 Déploiement

DOMAutopsy s'exécute directement avec `python server.py --host 0.0.0.0 --port 8000`. Pour la prod, **Caddy** est recommandé en reverse proxy — `Caddyfile.example` fournit deux modes prêts à coller :

- **Mode 1 — public HTTPS** avec Let's Encrypt auto, rate limit, security headers (HSTS / CSP / X-Frame-Options...), blocage des paths sensibles, **upgrade WebSocket explicite pour `/ws/*`** (sinon le screencast et le log live meurent silencieusement derrière un proxy mal configuré).
- **Mode 2 — interne** via VPN/Tailscale avec CA locale Caddy (lifetime 30j, auto-renewal), restriction par `remote_ip 127.0.0.1 / VPN range`.

```bash
# 1. Configure .env
echo "OPENAI_API_KEY=sk-..." >> .env
echo "DOMAUTOPSY_API_TOKEN=$(python domautopsy_cli.py auth genkey)" >> .env

# 2. Lance le serveur Python
python server.py --host 0.0.0.0 --port 8000 &

# 3. Lance Caddy (Let's Encrypt s'occupe du TLS)
sudo caddy run --config Caddyfile

# 4. Vérifie
curl https://domautopsy.example.com/health
# → {"status":"ok", "active_runs":0, "auth_enabled":true, ...}
```

### Variables d'environnement

| Variable | Rôle | Défaut |
|---|---|---|
| `OPENAI_API_KEY` | Clé OpenAI | requis si `--provider openai` |
| `GROQ_API_KEY` | Clé Groq | requis si `--provider groq` |
| `DOMAUTOPSY_API_TOKEN` | Master Bearer token | vide = no-auth |
| `DOMAUTOPSY_SCREENCAST_EVERY_N` | 1 frame sur N | 2 |
| `DOMAUTOPSY_SCREENCAST_QUALITY` | Qualité JPEG (0-100) | 60 |
| `DOMAUTOPSY_SCREENCAST_MAX_WIDTH` | Largeur max px | 1280 |
| `DOMAUTOPSY_SERVER` (CLI client) | URL serveur défaut | `http://localhost:8000` |

### Ports

| Port | Rôle |
|---|---|
| 8000 | Serveur FastAPI (HTTP + WebSocket) |
| 9222-9272 | Plage CDP Chromium (1 par run actif) |

---

## 🛡️ Sécurité et confidentialité

- **Redacting credentials côté listener** : détection regex + autocomplete + `type=password`, valeur remplacée par `<redacted>`, `sensitive: true`, **le sélecteur est conservé** pour générer un test rejouable (l'utilisateur final injecte sa propre valeur)
- **Aucun credential ne quitte la machine** : pas envoyé à l'IA de cleanup, pas écrit dans `locator_log.json`, pas dans le rapport HTML
- **Path traversal protection** sur `/api/run/{id}/file/{filename}` : rejette `/`, `\`, `..`
- **Subprocess safety** : `subprocess.Popen` avec args en liste (jamais `shell=True`), args validés par Pydantic (`RunRequest`)
- **Cleanup au shutdown** : lifespan FastAPI tue tous les subprocess actifs (terminate puis kill après 2s)
- **Tokens fils in-memory** : pas de persistance des credentials secondaires, le master suffit pour les re-mint
- **CSP / HSTS / X-Frame-Options** : configurés dans `Caddyfile.example`
- **Rate limit Caddy** : 500 events/min par IP côté reverse proxy
- **Blocage des paths sensibles** : `/.git`, `/.env`, `/admin`, `*.py`, `*.db` côté Caddy

---

## 🧰 Stack technique

| Composant | Version min | Rôle |
|---|---|---|
| Python | 3.12 | Runtime principal |
| browser-use | 0.3+ | Agent IA pilotant Chromium |
| playwright | 1.49+ | Driver navigateur + CDP |
| openai | 1.50+ | SDK OpenAI (compat Groq) |
| fastapi | 0.115+ | Serveur HTTP + WebSocket |
| uvicorn[standard] | 0.32+ | ASGI server |
| pydantic | 2.0+ | Validation request bodies |
| aiohttp | 3.10+ | Client CDP WebSocket pour screencast |
| python-dotenv | 1.0+ | Chargement `.env` |
| python-multipart | 0.0.18+ | Upload `/api/import` |
| Caddy (recommandé) | 2.x | Reverse proxy HTTPS + WS upgrade |
| Chart.js (CDN) | dernière | Charts dans le rapport HTML |

---

## 🐛 Patches inclus

**Fix browser-use CDP frame crash** — bug connu ([browser-use#2808](https://github.com/browser-use/browser-use/issues/2808)) : quand une iframe (pub ou cookies) est détruite pendant la navigation, `asyncio.gather` crashe tout le DOM. DOMAutopsy patche `DomService._get_ax_tree_for_all_frames` au runtime avec retry + fallback arbre vide.

---

## 📐 Principes de conception

<table>
<tr>
<td align="center" width="25%">
<img src="https://img.shields.io/badge/Zéro_Record_Replay-ef4444?style=for-the-badge" /><br/>
<sub>L'agent navigue. L'humain ne clique pas — sauf via UI web s'il veut juste regarder.</sub>
</td>
<td align="center" width="25%">
<img src="https://img.shields.io/badge/Framework_Agnostic-10B981?style=for-the-badge" /><br/>
<sub>JSON de steps bruts. Katalon, Playwright, Cypress, Selenium : tu choisis.</sub>
</td>
<td align="center" width="25%">
<img src="https://img.shields.io/badge/Shadow_DOM_Natif-8b5cf6?style=for-the-badge" /><br/>
<sub>Web components, LitElement, custom elements : tous capturés.</sub>
</td>
<td align="center" width="25%">
<img src="https://img.shields.io/badge/IA_Comme_Filtre-3b82f6?style=for-the-badge" /><br/>
<sub>L'IA ne génère pas les sélecteurs. Elle filtre le bruit. Les sélecteurs viennent du DOM réel.</sub>
</td>
</tr>
</table>

---

## 🔮 Roadmap

**V1 — consolidation** (1 semaine) : alignement API browser-use 0.12, retry+backoff LLM, mode CI `--junit-xml`, tests unitaires.
**V2 — import scripts + diff** (3 jours) : drift report contre un script existant, validation "ton test est encore valide".
**V3 — observabilité CDP** (1 semaine) : capture `Runtime.exceptionThrown` / `Console.messageAdded` / `Network.*` / `Coverage.*` / `Performance.*`, audit RGPD 3rd parties, mock generator, anti-flake structurel.
**V4 — premium / scale** (2 semaines) : grille N×N viewers, persistance SQLite, scheduler intégré, webhooks Slack/Teams, multi-LLM (Anthropic, Gemini, Mistral, DeepSeek), SSO/SAML, export Markdown.

Roadmap détaillée (avec pièges techniques V3 sur le timing de `Coverage.startPreciseCoverage`, le filtrage Network, et la distinction Browser session vs Page session) : **[`ARCHITECTURE.md`](ARCHITECTURE.md) §9**.

---

## 🌐 Écosystème JMer

| Outil | Fonction |
|-------|----------|
| [LogLens](https://github.com/julienmerconsulting/loglens) | Monitoring de logs temps réel |
| [DiffLens](https://github.com/julienmerconsulting/difflens) | Diff visuel sémantique 5 couches |
| **DOMAutopsy** | Capture DOM + génération code test multi-format |
| [QA OPS LAB](https://github.com/julienmerconsulting/qaopslab) | SaaS Test Management complet |

Même ADN : Python, zéro dépendance inutile, `python server.py`, thème dark.

---

<p align="center">
  <img src="https://img.shields.io/badge/Made_with-☕_et_trop_peu_de_sommeil-0b1220?style=for-the-badge&labelColor=ef4444" />
</p>

<p align="center">
  <a href="https://github.com/julienmerconsulting"><img src="https://img.shields.io/badge/GitHub-julienmerconsulting-181717?style=flat-square&logo=github" /></a>
  <a href="https://linkedin.com/in/julienmer"><img src="https://img.shields.io/badge/LinkedIn-Julien_Mer-0A66C2?style=flat-square&logo=linkedin" /></a>
  <a href="https://www.bonnes-pratiques-qa.fr"><img src="https://img.shields.io/badge/Newsletter-Bonnes_Pratiques_QA-10B981?style=flat-square" /></a>
</p>

<p align="center"><sub>JMer Consulting — JMer Lens Suite — 2026</sub></p>
