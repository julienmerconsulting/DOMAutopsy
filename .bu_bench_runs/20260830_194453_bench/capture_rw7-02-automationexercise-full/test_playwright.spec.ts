// Genere automatiquement par DOMAutopsy - NE PAS EDITER MANUELLEMENT.
// Genere le 2026-08-31T00:42:55
// Parcours : https://www.automationexercise.com/
// Schema JSON  : 2.0
// Steps totaux : 0
//
// Ce fichier est le format canonique de replay DOMAutopsy. Il est
// lance par POST /api/replay/{run_id} via `npx playwright test`.
// L'encapsulation test.step('[step-XXXX] ...') permet au rapport de
// rapprocher les resultats Playwright avec les etapes du JSON.

import { test, expect } from '@playwright/test';

test("replay: https://www.automationexercise.com/", async ({ page }, testInfo) => {
  await test.step("[step-0001] NAVIGATE - Va sur https://www.automationexercise.com/", async () => {
    await page.goto("https://www.automationexercise.com/");
  });

  await test.step("[step-0002] CLICK - Autoriser", async () => {
    await page.locator("[aria-label=\"Autoriser\"]").click();
  });

  await test.step("[step-0003] CLICK - Signup / Login", async () => {
    await page.locator("a[href=\"/login\"]").click();
  });

  await test.step("[step-0004] INPUT - input", async () => {
    await page.locator("[data-qa=\"signup-name\"]").fill("Jean Test");
  });

  await test.step("[step-0005] INPUT - input", async () => {
    await page.locator("[data-qa=\"signup-email\"]").fill("jeantest+84217@example.com");
  });

  await test.step("[step-0006] CLICK - Signup", async () => {
    await page.locator("[data-qa=\"signup-button\"]").click();
  });

  await test.step("[step-0007] EVALUATE - Evaluate JS : (function(){try{ /* Fill account form fields and submit once */   ", async () => {
    await page.evaluate(`(function(){try{ /* Fill account form fields and submit once */
  // Title: Mr
  var mr = document.getElementById('id_gender1'); if(mr) mr.checked = true;
  // Password
  var pwd = document.getElementById('password') || document.querySelector('input[type="password"]'); if(pwd) pwd.value = 'TestPass123!';
  // Date of birth: Day=1, Month=January, Year=1990
  var days = document.getElementById('days'); if(days){ days.value = '1'; days.dispatchEvent(new Event('change',{bubbles:true})); }
  var months = document.getElementById('months'); if(months){ months.value = 'January'; months.dispatchEvent(new Event('change',{bubbles:true})); }
  var years = document.getElementById('years'); if(years){ years.value = '1990'; years.dispatchEvent(new Event('change',{bubbles:true})); }
  // Newsletter/optin leave default unchecked
  // Address information
  var first = document.getElementById('first_name'); if(first) first.value = 'Jean';
  var last = document.getElementById('last_name'); if(last) last.value = 'Test';
  var addr1 = document.getElementById('address1'); if(addr1) addr1.value = '1 rue Test';
  // Country: try selecting by visible text 'Canada' or value 'Canada'
  var country = document.getElementById('country'); if(country){
    for(var i=0;i<country.options.length;i++){ if(country.options[i].text.trim()==='Canada' || country.options[i].value==='Canada'){ country.selectedIndex = i; break; }}
    country.dispatchEvent(new Event('change',{bubbles:true}));
  }
  var state = document.getElementById('state'); if(state) state.value = 'Quebec';
  var city = document.getElementById('city'); if(city) city.value = 'Montreal';
  var zipcode = document.getElementById('zipcode'); if(zipcode) zipcode.value = 'H2X1Y4';
  var mobile = document.getElementById('mobile_number'); if(mobile) mobile.value = '0100000000';
  // Submit: click the Create Account button (first button[type=submit] on this form)
  var btn = document.querySelector('button[type="submit"]');
  if(btn){ btn.click(); return 'clicked create account'; }
  return 'create button not found'; }catch(e){ return 'Error: '+e.message; } })()`);
  });

  await test.step("[step-0008] CLICK - Continue", async () => {
    await page.locator("[data-qa=\"continue-button\"]").click();
  });

  await test.step("[step-0010] CLICK -  Products", async () => {
    await page.locator("a[href=\"/products\"]").click();
  });

  await test.step("[step-0011] CLICK - Add to cart", async () => {
    await page.locator("[data-product-id=\"1\"]").first().click();
  });

  await test.step("[step-0012] CLICK - Click (BU-only, sans DOM event)", async () => {
    await page.locator("[data-product-id=\"2\"]").first().click();
  });

  await test.step("[step-0013] CLICK - View Cart", async () => {
    await page.locator("#cartModal a[href=\"/view_cart\"]").click();
  });

  await test.step("[step-0014] CLICK - Proceed To Checkout", async () => {
    await page.locator("a.btn.btn-default").click();
  });

  await test.step("[step-0015] CLICK - Place Order", async () => {
    await page.locator("a[href=\"/payment\"]").click();
  });

  await test.step("[step-0016] INPUT - input", async () => {
    await page.locator("[data-qa=\"name-on-card\"]").fill("Jean Test");
  });

  await test.step("[step-0017] INPUT - input", async () => {
    await page.locator("[data-qa=\"card-number\"]").fill("4111111111111111");
  });

  await test.step("[step-0018] INPUT - input", async () => {
    await page.locator("[data-qa=\"cvc\"]").fill("123");
  });

  await test.step("[step-0019] CLICK - Pay and Confirm Order", async () => {
    await page.locator("#submit").click();
  });

  await test.step("[step-0020] INPUT - input", async () => {
    await page.locator("[data-qa=\"expiry-month\"]").fill("12");
  });

  await test.step("[step-0021] INPUT - input", async () => {
    await page.locator("[data-qa=\"expiry-year\"]").fill("2030");
  });

  await test.step("[step-0022] CLICK - Pay and Confirm Order", async () => {
    await page.locator("#submit").click();
  });

  await test.step("[step-0023] CLICK - Delete Account", async () => {
    await page.locator("a[href=\"/delete_account\"]").click();
  });

});
