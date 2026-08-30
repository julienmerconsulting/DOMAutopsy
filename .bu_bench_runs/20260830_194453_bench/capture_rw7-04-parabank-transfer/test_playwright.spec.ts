// Genere automatiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.
// Genere le 2026-08-30T21:55:50
// Parcours : BU_Bench_V1/rw7-04-parabank-transfer
// Schema JSON  : 2.0
// Steps totaux : 27
//
// Ce fichier est le format canonique de replay DOMAutopsy. Il est
// lance par POST /api/replay/{run_id} via `npx playwright test`.
// L'encapsulation test.step('[step-XXXX] ...') permet au rapport de
// rapprocher les resultats Playwright avec les etapes du JSON.

import { test, expect } from '@playwright/test';

test("replay: BU_Bench_V1/rw7-04-parabank-transfer", async ({ page }, testInfo) => {
  await test.step("[step-0001] NAVIGATE - Va sur https://parabank.parasoft.com/parabank/index.htm", async () => {
    await page.goto("https://parabank.parasoft.com/parabank/index.htm");
  });

  await test.step("[step-0002] CLICK - Register", async () => {
    await page.locator("a[href*=\"register.htm\"]").click();
  });

  await test.step("[step-0003] INPUT - input", async () => {
    await page.locator("#customer\\.firstName").fill("Jean");
  });

  await test.step("[step-0004] INPUT - input", async () => {
    await page.locator("#customer\\.lastName").fill("Test");
  });

  await test.step("[step-0005] INPUT - input", async () => {
    await page.locator("#customer\\.address\\.street").fill("1 rue Test");
  });

  await test.step("[step-0006] INPUT - input", async () => {
    await page.locator("#customer\\.address\\.city").fill("Paris");
  });

  await test.step("[step-0007] INPUT - input", async () => {
    await page.locator("#customer\\.address\\.state").fill("IDF");
  });

  await test.step("[step-0008] INPUT - input", async () => {
    await page.locator("#customer\\.address\\.zipCode").fill("75001");
  });

  await test.step("[step-0009] INPUT - input", async () => {
    await page.locator("#customer\\.phoneNumber").fill("0100000000");
  });

  await test.step("[step-0010] INPUT - input", async () => {
    await page.locator("#customer\\.ssn").fill("123-45-6789");
  });

  await test.step("[step-0011] INPUT - input", async () => {
    await page.locator("#customer\\.username").fill("jeantest_q5K8r2");
  });

  await test.step("[step-0012] INPUT - input", async () => {
    await page.locator("#customer\\.password").fill("TestPass123!");
  });

  await test.step("[step-0013] INPUT - input", async () => {
    await page.locator("#repeatedPassword").fill("TestPass123!");
  });

  await test.step("[step-0014] CLICK - click", async () => {
    await page.locator("input[value=\"Register\"]").click();
  });

  await test.step("[step-0015] CLICK - Accounts Overview", async () => {
    await page.locator("a[href=\"overview.htm\"]").click();
  });

  await test.step("[step-0016] CLICK - Open New Account", async () => {
    await page.locator("a[href=\"openaccount.htm\"]").click();
  });

  await test.step("[step-0017] SELECT - select", async () => {
    await page.locator("#type").selectOption({ label: "SAVINGS" });
  });

  await test.step("[step-0018] CLICK - click", async () => {
    await page.locator("input.button").click();
  });

  await test.step("[step-0019] CLICK - Transfer Funds", async () => {
    await page.locator("a[href=\"transfer.htm\"]").click();
  });

  // SKIPPED [step-0020] EVALUATE - Evaluate JS : (function(){try{var from=document.getElementById('fromAccountId');
  //   Raison : fusionne avec step-0019 (evaluate JS workaround dont l'intent est deja capture par un click canonique)

  // SKIPPED [step-0021] CLICK - Click (BU-only, sans DOM event)
  //   Raison : action click sans selecteur (BU sans interacted_element ni correspondance DOM listener)

  await test.step("[step-0022] INPUT - input", async () => {
    await page.locator("#amount").fill("100");
  });

  await test.step("[step-0023] CLICK - click", async () => {
    await page.locator("input.button").click();
  });

  await test.step("[step-0024] CLICK - Admin Page", async () => {
    await page.locator("xpath=//a[contains(text(),'Admin Page')]").click();
  });

  await test.step("[step-0025] WAIT - Attends 1.0s", async () => {
    await page.waitForTimeout(1000);
  });

  // SKIPPED [step-0026] EXTRACT - Extract LLM : lecture DOM
  //   Raison : action extract (lecture LLM only, pas d'interaction utilisateur reproductible)

  await test.step("[step-0027] CLICK - CLEAN", async () => {
    await page.locator("button[value=\"CLEAN\"]").click();
  });

  await test.step("[oracle] validation finale du scenario", async () => {
    await expect(page.locator('body')).toContainText("Database Cleaned", { timeout: 15000 });
  });

});
