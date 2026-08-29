# Fixture R9 TodoMVC — régression test

Origine : run BU réel du 2026-08-30 sur https://demo.playwright.dev/todomvc/
avec `gpt-5-mini`, `max_steps=40`, `max_actions_per_step=10`, headless,
port CDP dynamique 9263, profil temporaire isolé.

## Scénario capturé

Ajout de 4 tâches TodoMVC + toggle "Verifier les selecteurs" + filter
Active + filter Completed + Clear completed. L'agent BU a nécessité un
workaround `evaluate` JS pour toggler la checkbox après échec des
click index (agent BU 0.13.8, sélecteurs `[aria-label="Toggle Todo"]`
matchant 4 éléments).

## Fichiers

- `browser_use_history.json` : historique brut agent BU (23 steps LLM)
- `locator_dedup.json` : événements DOM listener dédupliqués localement
- `network_log.json` : trafic HTTP filtré (Fetch/XHR/Document/WS/EventSource)

Ces fichiers sont figés et servent de fixture pour
`tests/test_r9_regression.py` qui vérifie que le pipeline
(rebuild + génération TS) produit des invariants stables :
- 4 saisies distinctes avec les 4 valeurs exactes
- 4 Enter (pas 8, pas 1)
- 0 saisie parasite "on" sur checkbox
- 1 seule interaction canonique sur la checkbox
- Click ambigu fusionné dans le check canonique via `parentLabel`

Ne pas modifier ces fichiers sans mettre à jour les assertions du test.
