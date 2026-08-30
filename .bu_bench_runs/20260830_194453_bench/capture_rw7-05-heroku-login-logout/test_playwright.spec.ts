// Genere automatiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.
// Genere le 2026-08-30T19:48:20
// Parcours : BU_Bench_V1/rw7-05-heroku-login-logout
// Schema JSON  : 2.0
// Steps totaux : 5
//
// Ce fichier est le format canonique de replay DOMAutopsy. Il est
// lance par POST /api/replay/{run_id} via `npx playwright test`.
// L'encapsulation test.step('[step-XXXX] ...') permet au rapport de
// rapprocher les resultats Playwright avec les etapes du JSON.

import { test, expect } from '@playwright/test';

test("replay: BU_Bench_V1/rw7-05-heroku-login-logout", async ({ page }, testInfo) => {
  await test.step("[step-0001] NAVIGATE - Va sur https://the-internet.herokuapp.com/login", async () => {
    await page.goto("https://the-internet.herokuapp.com/login");
  });

  await test.step("[step-0002] INPUT - input", async () => {
    await page.locator("#username").fill("tomsmith");
  });

  await test.step("[step-0003] INPUT - input", async () => {
    await page.locator("#password").fill("SuperSecretPassword!");
  });

  await test.step("[step-0004] CLICK - Login", async () => {
    await page.locator("button.radius").click();
  });

  await test.step("[step-0005] CLICK - Logout", async () => {
    await page.locator("a[href=\"/logout\"]").click();
  });

  await test.step("[oracle] validation finale du scenario", async () => {
    await expect(page).toHaveURL(new RegExp("/login"), { timeout: 15000 });
    await expect(page.locator('body')).toContainText("You logged out of the secure area!", { timeout: 15000 });
  });

});
