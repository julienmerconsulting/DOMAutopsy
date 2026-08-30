// DOMAutopsy — configuration Playwright pour rejouer les tests generes.
// Le runner est appele par /api/replay/{run_id} qui invoque
//    npx playwright test <chemin-RELATIF-au-repo> --workers=1 --output=<dir>
// (surtout pas de --reporter en CLI : ca ecraserait la config ci-dessous
// et le reporter JSON ne serait plus produit)
// Le TS genere par playwright_generator.py est agnostique de ce fichier
// (il embarque son propre test.step() sans depender de fixtures custom),
// mais on garde une config minimale pour standardiser retries, timeouts
// et outputs cross-run.

import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  // Les specs generes vivent dans runs/<ts>_<runid>/ ET dans
  // .bu_bench_runs/<ts>_bench/capture_<task>/ pour le CRD-7 bench.
  // On elargit testDir a la racine + testMatch restrictif au nom
  // canonique 'test_playwright.spec.ts' -> matche uniquement nos
  // specs generees, jamais un autre .spec.ts du projet.
  testDir: '.',
  testMatch: ['**/test_playwright.spec.ts'],
  testIgnore: ['node_modules/**', '.git/**'],

  // Un seul worker : le replay doit etre deterministe et sequentiel.
  workers: 1,

  // Pas de retry automatique : un flake doit remonter tel quel dans le
  // rapport pour que l'anomalie soit visible.
  retries: 0,

  // Timeout global par test : les scenarios captures ont des waits explicites.
  // On garde 5 minutes comme plafond raisonnable pour les parcours longs.
  timeout: 5 * 60 * 1000,
  expect: {
    timeout: 10 * 1000,
  },

  // Deux reporters en parallele :
  //  - 'list' sur stdout : streame en direct via WebSocket vers l'UI
  //  - 'json' sur fichier : produit un resultat structure per-test/per-step
  //    consomme par report_generator pour rapprocher chaque test.step()
  //    ([step-XXXX]) au step JSON correspondant.
  // Le chemin du JSON est pilote par l'env var DOMAUTOPSY_REPLAY_JSON,
  // positionnee par server.py::/api/replay pour ecrire dans le replay_dir.
  reporter: [
    ['list'],
    ['json', {
      outputFile: process.env.DOMAUTOPSY_REPLAY_JSON ?? 'test-results/replay_results.json',
    }],
  ],

  use: {
    // Trace et screenshot sont produits explicitement dans le TS genere
    // (via test.step + page.screenshot). On garde ici les defauts
    // Playwright pour ne rien imposer aux tests generes.
    trace: 'off',
    screenshot: 'off',
    video: 'off',
    actionTimeout: 15 * 1000,
    navigationTimeout: 30 * 1000,
    // Locale alignee sur celle du capture (Chromium BU tourne en FR sur
    // cette machine Windows FR-FR). Sans ca, les selecteurs captures
    // sur du texte localise ([aria-label="Autoriser"], [aria-label="Ajouter"])
    // ne matchent pas au replay ou le UI serait en anglais par defaut.
    locale: process.env.PW_LOCALE ?? 'fr-FR',
    timezoneId: process.env.PW_TZ ?? 'Europe/Paris',
    extraHTTPHeaders: {
      'Accept-Language': process.env.PW_ACCEPT_LANG ?? 'fr-FR,fr;q=0.9,en;q=0.8',
    },
    // Slow motion optionnel via env var PW_SLOWMO (ms). Utile pour la
    // demo visuelle en headed : PW_SLOWMO=300 npx playwright test ...
    // Sans la var, comportement production identique (aucune latence).
    launchOptions: {
      slowMo: process.env.PW_SLOWMO ? parseInt(process.env.PW_SLOWMO, 10) : 0,
    },
  },

  projects: [
    {
      name: 'chromium',
      use: {
        ...devices['Desktop Chrome'],
        // channel: 'chromium' force l'utilisation du binaire Chromium
        // classique deja telecharge par Playwright Python (partage le
        // cache ms-playwright/chromium-XXXX). Sans cette option,
        // Playwright JS >= 1.49 cherche un binaire separe
        // ms-playwright/chromium_headless_shell-XXXX qui n'est pas
        // installe par Playwright Python, provoquant un "Executable
        // doesn't exist" a la premiere execution.
        channel: 'chromium',
      },
    },
  ],
});
