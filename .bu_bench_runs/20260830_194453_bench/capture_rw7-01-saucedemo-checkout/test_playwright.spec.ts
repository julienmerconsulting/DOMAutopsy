// Genere automatiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.
// Genere le 2026-08-30T19:47:06
// Parcours : BU_Bench_V1/rw7-01-saucedemo-checkout
// Schema JSON  : 2.0
// Steps totaux : 12
//
// Ce fichier est le format canonique de replay DOMAutopsy. Il est
// lance par POST /api/replay/{run_id} via `npx playwright test`.
// L'encapsulation test.step('[step-XXXX] ...') permet au rapport de
// rapprocher les resultats Playwright avec les etapes du JSON.

import { test, expect } from '@playwright/test';

test("replay: BU_Bench_V1/rw7-01-saucedemo-checkout", async ({ page }, testInfo) => {
  await test.step("[step-0000] NAVIGATE - scenario start URL", async () => {
    await page.goto("https://www.saucedemo.com/");
  });

  await test.step("[step-0001] OPEN_TAB - Ouvre nouvel onglet : https://www.saucedemo.com/", async () => {
    const _newPage = await page.context().newPage();
    await _newPage.goto("https://www.saucedemo.com/");
    page = _newPage;  // switch focus vers le nouvel onglet
  });

  await test.step("[step-0002] INPUT - input", async () => {
    await page.locator("#user-name").fill("standard_user");
  });

  await test.step("[step-0003] INPUT - input", async () => {
    await page.locator("#password").fill("secret_sauce");
  });

  await test.step("[step-0004] CLICK - click", async () => {
    await page.locator("#login-button").click();
  });

  await test.step("[step-0005] CLICK - Add to cart", async () => {
    await page.locator("#add-to-cart-sauce-labs-backpack").click();
  });

  await test.step("[step-0006] CLICK - 1", async () => {
    await page.locator("a.shopping_cart_link").click();
  });

  await test.step("[step-0007] CLICK - Checkout", async () => {
    await page.locator("#checkout").click();
  });

  await test.step("[step-0008] INPUT - input", async () => {
    await page.locator("#first-name").fill("Jean");
  });

  await test.step("[step-0009] INPUT - input", async () => {
    await page.locator("#last-name").fill("Test");
  });

  await test.step("[step-0010] INPUT - input", async () => {
    await page.locator("#postal-code").fill("75001");
  });

  await test.step("[step-0011] CLICK - click", async () => {
    await page.locator("#continue").click();
  });

  await test.step("[step-0012] CLICK - Finish", async () => {
    await page.locator("#finish").click();
  });

  await test.step("[oracle] validation finale du scenario", async () => {
    await expect(page).toHaveURL(new RegExp("checkout\\-complete"), { timeout: 15000 });
    await expect(page.locator('body')).toContainText("Thank you for your order!", { timeout: 15000 });
  });

});
