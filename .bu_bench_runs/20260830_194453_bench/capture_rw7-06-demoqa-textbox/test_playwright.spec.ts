// Genere automatiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.
// Genere le 2026-08-30T19:50:18
// Parcours : BU_Bench_V1/rw7-06-demoqa-textbox
// Schema JSON  : 2.0
// Steps totaux : 11
//
// Ce fichier est le format canonique de replay DOMAutopsy. Il est
// lance par POST /api/replay/{run_id} via `npx playwright test`.
// L'encapsulation test.step('[step-XXXX] ...') permet au rapport de
// rapprocher les resultats Playwright avec les etapes du JSON.

import { test, expect } from '@playwright/test';

test("replay: BU_Bench_V1/rw7-06-demoqa-textbox", async ({ page }, testInfo) => {
  await test.step("[step-0001] NAVIGATE - Va sur https://demoqa.com/text-box", async () => {
    await page.goto("https://demoqa.com/text-box");
  });

  await test.step("[step-0002] INPUT - input", async () => {
    await page.locator("#userName").fill("Jean Test");
  });

  await test.step("[step-0003] INPUT - input", async () => {
    await page.locator("#userEmail").fill("jean.test@example.com");
  });

  await test.step("[step-0004] INPUT - input", async () => {
    await page.locator("#currentAddress").fill("1 rue de la Paix, 75001 Paris");
  });

  await test.step("[step-0005] INPUT - input", async () => {
    await page.locator("#permanentAddress").fill("42 avenue du Test, 69000 Lyon");
  });

  await test.step("[step-0006] CLICK - Submit", async () => {
    await page.locator("#submit").click();
  });

  // SKIPPED [step-0007] EXTRACT - Extract LLM : lecture DOM
  //   Raison : action extract (lecture LLM only, pas d'interaction utilisateur reproductible)

  // SKIPPED [step-0008] EXTRACT - Extract LLM : lecture DOM
  //   Raison : action extract (lecture LLM only, pas d'interaction utilisateur reproductible)

  // SKIPPED [step-0009] SCROLL - Scroll (BU-only, sans DOM event)
  //   Raison : action scroll sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0010] EXTRACT - Extract LLM : lecture DOM
  //   Raison : action extract (lecture LLM only, pas d'interaction utilisateur reproductible)

  // SKIPPED [step-0011] EXTRACT - Extract LLM : lecture DOM
  //   Raison : action extract (lecture LLM only, pas d'interaction utilisateur reproductible)

  await test.step("[oracle] validation finale du scenario", async () => {
    await expect(page.locator('body')).toContainText("jean.test@example.com", { timeout: 15000 });
  });

});
