// Genere automatiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.
// Genere le 2026-08-30T19:53:29
// Parcours : BU_Bench_V1/rw7-07-todomvc-filters
// Schema JSON  : 2.0
// Steps totaux : 25
//
// Ce fichier est le format canonique de replay DOMAutopsy. Il est
// lance par POST /api/replay/{run_id} via `npx playwright test`.
// L'encapsulation test.step('[step-XXXX] ...') permet au rapport de
// rapprocher les resultats Playwright avec les etapes du JSON.

import { test, expect } from '@playwright/test';

test("replay: BU_Bench_V1/rw7-07-todomvc-filters", async ({ page }, testInfo) => {
  await test.step("[step-0001] NAVIGATE - Va sur https://demo.playwright.dev/todomvc/", async () => {
    await page.goto("https://demo.playwright.dev/todomvc/");
  });

  await test.step("[step-0002] INPUT - input", async () => {
    await page.locator("input[placeholder=\"What needs to be done?\"]").fill("acheter du pain");
  });

  await test.step("[step-0003] KEYBOARD - keyboard", async () => {
    await page.keyboard.press("Enter");
  });

  await test.step("[step-0004] INPUT - input", async () => {
    await page.locator("input[placeholder=\"What needs to be done?\"]").fill("appeler le medecin");
  });

  await test.step("[step-0005] KEYBOARD - keyboard", async () => {
    await page.keyboard.press("Enter");
  });

  await test.step("[step-0006] INPUT - input", async () => {
    await page.locator("input[placeholder=\"What needs to be done?\"]").fill("ranger le bureau");
  });

  await test.step("[step-0007] KEYBOARD - keyboard", async () => {
    await page.keyboard.press("Enter");
  });

  await test.step("[step-0008] INPUT - input", async () => {
    await page.locator("input[placeholder=\"What needs to be done?\"]").fill("reviser Playwright");
  });

  await test.step("[step-0009] KEYBOARD - keyboard", async () => {
    await page.keyboard.press("Enter");
  });

  // SKIPPED [step-0010] EVALUATE - Evaluate JS : (function(){try{const el=document.querySelector('ul.todo-list li:n
  //   Raison : fusionne avec step-0006 (evaluate JS workaround dont l'intent est deja capture par un input canonique)

  // SKIPPED [step-0011] EVALUATE - Evaluate JS : (function(){try{const el=document.querySelector('ul.todo-list li:n
  //   Raison : fusionne avec step-0006 (evaluate JS workaround dont l'intent est deja capture par un input canonique)

  // SKIPPED [step-0012] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0013] CLICK - acheter du pain
  //   Raison : fusionne dans step-0024 (canonique check/uncheck avec semantique checked + parentLabel)

  await test.step("[step-0014] CHECK - acheter du pain", async () => {
    await page.getByRole('listitem').filter({ hasText: "acheter du pain" }).getByRole('checkbox').setChecked(true);
  });

  // SKIPPED [step-0015] EVALUATE - Evaluate JS : (function(){try{const items = Array.from(document.querySelectorAll
  //   Raison : fusionne dans step-0024 (workaround JS pour la meme interaction, deja capturee par le change)

  // SKIPPED [step-0016] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0017] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0018] CLICK - appeler le medecin
  //   Raison : fusionne dans step-0025 (canonique check/uncheck avec semantique checked + parentLabel)

  await test.step("[step-0019] CHECK - appeler le medecin", async () => {
    await page.getByRole('listitem').filter({ hasText: "appeler le medecin" }).getByRole('checkbox').setChecked(true);
  });

  await test.step("[step-0020] CLICK - Active", async () => {
    await page.locator("a[href=\"#/active\"]").click();
  });

  // SKIPPED [step-0021] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  // SKIPPED [step-0022] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  await test.step("[step-0023] CLICK - Completed", async () => {
    await page.locator("a[href=\"#/completed\"]").click();
  });

  await test.step("[step-0024] CLICK - Clear completed", async () => {
    await page.locator("button.clear-completed").click();
  });

  await test.step("[step-0025] CLICK - All", async () => {
    await page.locator("a[href=\"#/\"]").click();
  });

  await test.step("[oracle] validation finale du scenario", async () => {
    await expect(page.locator('body')).toContainText("2 items left", { timeout: 15000 });
  });

});
