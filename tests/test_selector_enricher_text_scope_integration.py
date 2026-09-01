"""Preuve opt-in du candidat ancestor-text-scope dans un vrai Chromium.

Lancer avec :

    DOMAUTOPSY_RUN_SELECTOR_INTEGRATION=1 python -m pytest -m slow \
        tests/test_selector_enricher_text_scope_integration.py
"""

from __future__ import annotations

import asyncio
import os

import pytest

from selector_enricher import _candidates_from_session


async def _backend_node_id(session, selector: str) -> int:
    document = await session.send("DOM.getDocument")
    node = await session.send("DOM.querySelector", {
        "nodeId": document["root"]["nodeId"],
        "selector": selector,
    })
    described = await session.send("DOM.describeNode", {"nodeId": node["nodeId"]})
    return described["node"]["backendNodeId"]


async def _candidates_for(page, selector: str):
    session = await page.context.new_cdp_session(page)
    try:
        backend_node_id = await _backend_node_id(session, selector)
        return await _candidates_from_session(
            session,
            backend_node_id,
            None,
            {
                "node_name": "A",
                "attributes": {
                    "class": "add-to-cart",
                    "data-product-id": "1",
                },
            },
        )
    finally:
        await session.detach()


async def _exercise_text_scope() -> None:
    async_playwright = pytest.importorskip("playwright.async_api").async_playwright
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content("""
              <section class="features_items">
                <div class="single-products">
                  <div class="productinfo text-center">
                    <p>Blue Top</p>
                    <a class="add-to-cart" data-product-id="1">Add to cart</a>
                  </div>
                  <div class="product-overlay">
                    <a class="add-to-cart" data-product-id="1">Add to cart</a>
                  </div>
                </div>
              </section>
              <section class="recommended_items">
                <div class="single-products">
                  <div class="productinfo text-center">
                    <p>Blue Top</p>
                    <a class="add-to-cart" data-product-id="1">Add to cart</a>
                  </div>
                  <div class="product-overlay">
                    <a class="add-to-cart" data-product-id="1">Add to cart</a>
                  </div>
                </div>
              </section>
            """)
            candidates = await _candidates_for(
                page,
                "section.features_items [data-product-id='1']",
            )
            structured = [
                candidate for candidate in candidates
                if candidate.get("strategy") == "ancestor-text-scope"
                and candidate.get("hasText") == "Blue Top"
                and "div.productinfo.text-center" in candidate.get("targetSelector", "")
                and '[data-product-id="1"]' in candidate.get("targetSelector", "")
            ]
            assert structured
            assert all(
                candidate["unique"] is True
                and candidate["matchCount"] == 1
                and candidate["verifiedAtCapture"] is True
                for candidate in structured
            )
            assert any("features_items" in candidate["ancestorSelector"] for candidate in structured)

            # Deux duplicatas semantiquement identiques dans le meme scope :
            # aucune selection arbitraire n'est autorisee.
            await page.set_content("""
              <section class="features_items">
                <div class="single-products">
                  <div class="productinfo text-center">
                    <p>Blue Top</p>
                    <a class="add-to-cart" data-product-id="1">Add to cart</a>
                  </div>
                  <div class="product-overlay">
                    <a class="add-to-cart" data-product-id="1">Add to cart</a>
                  </div>
                </div>
                <div class="single-products">
                  <div class="productinfo text-center">
                    <p>Blue Top</p>
                    <a class="add-to-cart" data-product-id="1">Add to cart</a>
                  </div>
                  <div class="product-overlay">
                    <a class="add-to-cart" data-product-id="1">Add to cart</a>
                  </div>
                </div>
              </section>
            """)
            ambiguous = await _candidates_for(
                page,
                "section.features_items .productinfo:first-of-type [data-product-id='1']",
            )
            assert not any(
                candidate.get("strategy") == "ancestor-text-scope"
                and candidate.get("hasText") == "Blue Top"
                for candidate in ambiguous
            )
        finally:
            await browser.close()


@pytest.mark.slow
def test_real_ancestor_text_scope_escalates_and_rejects_true_duplicates():
    if os.environ.get("DOMAUTOPSY_RUN_SELECTOR_INTEGRATION") != "1":
        pytest.skip("test Chromium ancestor-text-scope opt-in")
    asyncio.run(_exercise_text_scope())
