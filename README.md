<p align="center">
  <img src="https://img.shields.io/badge/🔬_DOMAutopsy-Intelligent_Locator_Harvester-ef4444?style=for-the-badge&labelColor=0b1220" alt="DOMAutopsy" />
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-3776AB?style=flat-square&logo=python&logoColor=white" />
  <img src="https://img.shields.io/badge/browser--use-AI_Agent-10B981?style=flat-square" />
  <img src="https://img.shields.io/badge/Playwright-2EAD33?style=flat-square&logo=playwright&logoColor=white" />
  <img src="https://img.shields.io/badge/OpenAI-GPT--4.1--mini-412991?style=flat-square&logo=openai&logoColor=white" />
  <img src="https://img.shields.io/badge/PySide6-UI_Builder-41CD52?style=flat-square&logo=qt&logoColor=white" />
  <img src="https://img.shields.io/badge/Shadow_DOM-supported-8b5cf6?style=flat-square" />
  <img src="https://img.shields.io/badge/licence-MIT-green?style=flat-square" />
</p>

<p align="center">
  <strong>Autopsie chirurgicale du DOM. Locators parfaits. Code test prêt.</strong><br/>
  Un agent IA navigue. Un listener capture. L'IA nettoie. Tu reçois le code.<br/>
  <em>Framework-agnostique. Zéro record/replay. Zéro sélecteur fragile.</em>
</p>

---

## 🎯 Pourquoi DOMAutopsy ?

Les outils existants (Selenium IDE, Katalon Recorder, Cypress Studio) font du **record/replay** : ils enregistrent ce qu'ils voient, avec les sélecteurs qu'ils trouvent — souvent des XPath absolus ou des classes CSS générées qui cassent au premier refactoring.

DOMAutopsy fait de l'**autopsie intelligente** : un agent IA navigue comme un humain, un listener JS intercepte chaque interaction et applique une **cascade de 7 niveaux de sélecteurs** pour choisir le plus robuste. L'IA nettoie le bruit. Tu reçois du code prod.

| | Selenium IDE | Katalon Recorder | **DOMAutopsy** |
|---|---|---|---|
| Approche | Record/replay | Record/replay | **Agent IA + DOM Listener** |
| Sélecteurs | XPath absolu / CSS fragile | XPath / CSS | **Cascade 7 niveaux, validée** |
| Shadow DOM | ❌ | ❌ | **✅ natif** |
| Nettoyage IA | ❌ | ❌ | **✅ GPT-4.1-mini** |
| Output | Script propriétaire | Script Katalon | **JSON + Groovy + HTML** |
| Prix | Gratuit | Gratuit | **0€** |

---

## 🧠 La cascade de sélecteurs

Le listener JS injecté dans le navigateur applique une **cascade stricte** à chaque clic et chaque input. Il choisit le sélecteur le plus robuste disponible, valide son unicité en temps réel, et remonte si nécessaire.

| Priorité | Stratégie | Exemples |
|----------|-----------|---------|
| 🟢 **Tier 1** — `data-testid` · `id` · `name` | **Attributs stables** — Priorité maximale. IDs sémantiques, attributs `name`, `data-testid`. Uniques par nature. | `#loginButton` · `[name="email"]` · `[data-testid="submit"]` |
| 🔵 **Tier 2** — `aria-label` · `placeholder` · `title` | **Attributs sémantiques (accessibilité)** — Stables car porteurs de sens métier. Doublon accessibilité → automatisation, **1 pierre 2 coups**. | `[aria-label="Rechercher"]` · `input[placeholder="Email"]` |
| 🩵 **Tier 3** — `href` | **Liens par href** — Uniquement si href non générique (`#`, `/`, `javascript:void`) et < 100 chars. | `a[href="/products/catalog"]` |
| 🟣 **Tier 4** — `parent aria-label` · `parent data-testid` | **Remontée au parent (icônes, SVG)** — Si l'élément cliqué est une icône ou un SVG sans attribut, remonte au parent interactif porteur de sens via `closest()`. | `[aria-label="Fermer le menu"]` |
| 🟡 **Tier 5** — `label XPath` | **Label associé (inputs)** — Pour les inputs sans attribut stable, trouve le `<label for>` ou `<label>` wrapping. | `//label[contains(text(),"Mot de passe")]//input` |
| 🔴 **Tier 6** — `CSS court` · `XPath texte` | **Dernier recours** — CSS court (max 2 classes stables + nth-of-type). Si multi-match, tentative XPath texte. | `button.btn-primary:nth-of-type(2)` · `//button[contains(text(),"Valider")]` |
| 🟤 **Shadow DOM** — chaîne `>>>` | **Détection automatique** — Construction de la chaîne avec `>>>` (Playwright) et `.shadowRoot.querySelector()` (JS). | `my-component >>> [aria-label="Submit"]` |

**Validation temps réel** : chaque sélecteur est validé via `querySelectorAll` ou `XPathResult`. Si `matchCount > 1`, le listener tente automatiquement un XPath texte avant de passer en Tier 6.

---

## ⚡ Installation

```bash
git clone https://github.com/julienmerconsulting/DOMAutopsy
cd DOMAutopsy
pip install -r requirements.txt
playwright install chromium
```

**Prérequis** : Python 3.12+ (PySide6 ne supporte pas les versions antérieures)

Configurer la clé API :
```bash
cp .env.example .env
# Éditer .env avec ta clé OPENAI_API_KEY
```

---

## 🚀 Utilisation

### Mode 1 — Scenario Builder (GUI PySide6)

```bash
python scenario_builder.py
```

Interface graphique pour construire le scénario étape par étape : navigate, click, input, verify, scroll, hover, wait, screenshot, cookie. Export JSON + lancement direct de qa_explorer.

### Mode 2 — JSON en ligne de commande

```bash
python qa_explorer.py mon_scenario.json
```

### Mode 3 — CLI rapide

```bash
python qa_explorer.py --url https://example.com --task "Clique sur Login, remplis email avec test@test.com, clique sur Connexion"
```

---

## 🔄 Pipeline complet

```
┌─────────────────────────────────┐
│   scenario_builder.py (PySide6) │  ← Construction visuelle du scénario
│   10 types d'actions            │
└────────────┬────────────────────┘
             │ JSON
             ▼
┌─────────────────────────────────┐
│   qa_explorer.py                │
│   ├── Lance Chromium (CDP 9222) │  ← Playwright
│   ├── Injecte DOM Listener V3   │  ← Capture clics + inputs + scrolls
│   ├── browser-use navigue       │  ← Agent IA GPT-4.1-mini
│   ├── Récupère localStorage     │  ← Log brut des locators
│   ├── Déduplique inputs/clics   │  ← Garde dernière valeur par champ
│   └── Nettoyage IA              │  ← Filtre bruit, ordonne steps
└────────────┬────────────────────┘
             │
     ┌───────┼───────────┐
     ▼       ▼           ▼
  JSON    Groovy       HTML
  brut    Katalon      Rapport
```

---

## 📤 Outputs

| Fichier | Contenu |
|---------|---------|
| `locator_log_TIMESTAMP.json` | Log brut capturé (tous les events) |
| `clean_steps_TIMESTAMP.json` | Parcours nettoyé par l'IA (steps + anomalies) |
| `katalon_test_TIMESTAMP.groovy` | Code Katalon Studio prêt à coller |
| `qa_report_TIMESTAMP.html` | Rapport interactif (Charts.js, dark theme) |

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

## 🖥️ Rapport HTML

Le rapport interactif généré par `report_generator.py` inclut :

- **KPIs** : steps nettoyés, actions capturées, sélecteurs uniques/non-uniques, anomalies
- **3 piecharts** : stratégies de sélection, fiabilité (unique vs non-unique), types d'actions
- **Tableau des steps** nettoyés avec badges de sévérité
- **Log brut dédupliqué** avec tier et stratégie par entrée
- **Code Katalon** affiché avec coloration syntaxique
- **Thème dark** cohérent avec la suite Lens

---

## 🏗️ Architecture

```
DOMAutopsy
│
├── qa_explorer.py        # Pipeline principal : Chromium + listener + browser-use + IA
├── scenario_builder.py   # GUI PySide6 : constructeur de scénarios
├── report_generator.py   # Rapport HTML interactif (Chart.js)
├── .env.example          # Template variables d'environnement
├── requirements.txt      # Dépendances pip
└── .python-version       # Python 3.12+
```

---

## 🐛 Patches inclus

**Fix browser-use CDP frame crash** — bug connu ([#2808](https://github.com/browser-use/browser-use/issues/2808)) : quand une iframe pub ou cookies est détruite pendant la navigation, `asyncio.gather` crashe tout le DOM. DOMAutopsy patche `DomService._get_ax_tree_for_all_frames` au runtime avec retry + fallback arbre vide.

---

## 📐 Principes de conception

<table>
<tr>
<td align="center" width="25%">
<img src="https://img.shields.io/badge/Zéro_Record_Replay-ef4444?style=for-the-badge" /><br/>
<sub>L'agent navigue, pas toi. Aucune interaction manuelle.</sub>
</td>
<td align="center" width="25%">
<img src="https://img.shields.io/badge/Framework_Agnostic-10B981?style=for-the-badge" /><br/>
<sub>JSON de steps bruts. Katalon, Playwright, Selenium, Robot : tu choisis.</sub>
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

## 🔮 Évolutions prévues

- Support **multi-LLM** : Anthropic Claude, Mistral, Ollama local
- Export **Playwright TypeScript** et **Cypress**
- Mode **headless** pour CI/CD
- Intégration dans **QA OPS LAB** comme module Test Generation

---

## 🌐 Écosystème JMer

| Outil | Fonction |
|-------|----------|
| [LogLens](https://github.com/julienmerconsulting/loglens) | Monitoring de logs temps réel |
| [DiffLens](https://github.com/julienmerconsulting/difflens) | Diff visuel sémantique 5 couches |
| **DOMAutopsy** | Harvester de locators IA + génération code test |
| [QA OPS LAB](https://github.com/julienmerconsulting/qaopslab) | SaaS Test Management complet |

Même ADN : Python, zéro dépendance inutile, `python main.py`, thème dark.

---

<p align="center">
  <img src="https://img.shields.io/badge/Made_with-☕_et_trop_peu_de_sommeil-0b1220?style=for-the-badge&labelColor=ef4444" />
</p>

<p align="center">
  <a href="https://github.com/julienmerconsulting"><img src="https://img.shields.io/badge/GitHub-julienmerconsulting-181717?style=flat-square&logo=github" /></a>
  <a href="https://linkedin.com/in/julienmer"><img src="https://img.shields.io/badge/LinkedIn-Julien_Mer-0A66C2?style=flat-square&logo=linkedin" /></a>
  <a href="https://www.bonnes-pratiques-qa.fr"><img src="https://img.shields.io/badge/Newsletter-Bonnes_Pratiques_QA-10B981?style=flat-square" /></a>
</p>

<p align="center"><sub>JMer Consulting — QA OPS LAB — 2026</sub></p>