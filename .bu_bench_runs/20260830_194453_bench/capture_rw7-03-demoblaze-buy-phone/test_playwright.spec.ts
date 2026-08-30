// Genere automatiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.
// Genere le 2026-08-30T19:46:45
// Parcours : BU_Bench_V1/rw7-03-demoblaze-buy-phone
// Schema JSON  : 2.0
// Steps totaux : 7
//
// Ce fichier est le format canonique de replay DOMAutopsy. Il est
// lance par POST /api/replay/{run_id} via `npx playwright test`.
// L'encapsulation test.step('[step-XXXX] ...') permet au rapport de
// rapprocher les resultats Playwright avec les etapes du JSON.

import { test, expect } from '@playwright/test';

test("replay: BU_Bench_V1/rw7-03-demoblaze-buy-phone", async ({ page }, testInfo) => {
  await test.step("[step-0001] NAVIGATE - Va sur https://www.demoblaze.com/", async () => {
    await page.goto("https://www.demoblaze.com/");
  });

  // SKIPPED [step-0002] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0003] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0004] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0005] EXTRACT - Extract LLM : lecture DOM
  //   Raison : action extract (lecture LLM only, pas d'interaction utilisateur reproductible)

  // SKIPPED [step-0006] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0007] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  await test.step("[oracle] validation finale du scenario", async () => {
    await expect(page.locator('body')).toContainText("Thank you for your purchase!", { timeout: 15000 });
  });

});
