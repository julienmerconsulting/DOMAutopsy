"""Preuve opt-in contre le vrai Browser Use 0.13.8 et un vrai Chromium.

Lancer explicitement avec :

    DOMAUTOPSY_RUN_BU_INTEGRATION=1 python -m pytest -m slow \
        tests/test_browser_use_transparent_controls_integration.py

Le test reste local (page ``data:``), sans LLM, compte ni acces reseau.
"""

from __future__ import annotations

import asyncio
from importlib.metadata import PackageNotFoundError, version as package_version
import json
import os
from urllib.parse import quote

import pytest

from browser_use_patches import (
    SUPPORTED_BROWSER_USE_VERSION,
    patch_transparent_form_control_visibility,
)


_HTML = """<!doctype html>
<html lang="fr">
  <head>
    <meta charset="utf-8">
    <style>
      label { display: block; height: 48px; }
      input[type="checkbox"] {
        opacity: 0;
        width: 32px;
        height: 32px;
        pointer-events: auto;
      }
    </style>
  </head>
  <body>
    <label><input type="checkbox" aria-label="Toggle Todo 1">Todo 1</label>
    <label><input type="checkbox" aria-label="Toggle Todo 2">Todo 2</label>
    <label><input type="checkbox" aria-label="Toggle Todo 3">Todo 3</label>
    <label><input type="checkbox" aria-label="Toggle Todo 4">Todo 4</label>
    <script>
      window.__domAutopsyClicks = [];
      document.addEventListener('click', (event) => {
        if (event.target.matches('input[type="checkbox"]')) {
          window.__domAutopsyClicks.push({
            label: event.target.getAttribute('aria-label'),
            trusted: event.isTrusted,
          });
        }
      }, true);
    </script>
  </body>
</html>
"""


def _checkbox_nodes(selector_map):
    return [
        node
        for node in selector_map.values()
        if str(node.tag_name or "").lower() == "input"
        and (node.attributes or {}).get("type") == "checkbox"
    ]


async def _exercise_real_browser_use() -> None:
    from browser_use import BrowserSession
    from browser_use.dom.service import DomService

    session = BrowserSession(headless=True, user_data_dir=None, keep_alive=False)
    await session.start()
    try:
        page = await session.must_get_current_page()
        await page.goto("data:text/html;charset=utf-8," + quote(_HTML))

        for _ in range(40):
            if await page.evaluate("() => document.readyState") == "complete":
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("la page data: n'a pas termine son chargement")

        unpatched_state, _, _ = await DomService(session).get_serialized_dom_tree()
        assert _checkbox_nodes(unpatched_state.selector_map) == []

        assert patch_transparent_form_control_visibility(
            DomService, SUPPORTED_BROWSER_USE_VERSION
        )
        patched_state, _, _ = await DomService(session).get_serialized_dom_tree()
        controls = _checkbox_nodes(patched_state.selector_map)

        assert len(controls) == 4
        assert [node.attributes["aria-label"] for node in controls] == [
            "Toggle Todo 1",
            "Toggle Todo 2",
            "Toggle Todo 3",
            "Toggle Todo 4",
        ]

        # Clique le backend_node_id provenant du selector_map, donc par la meme
        # voie CDP qu'une action agent. Un fallback element.click() JavaScript
        # ferait echouer explicitement la preuve isTrusted ci-dessous.
        element = await page.get_element(controls[0].backend_node_id)
        await element.click()
        result = json.loads(
            await page.evaluate(
                "() => ({"
                "clicks: window.__domAutopsyClicks, "
                "checked: document.querySelector('input').checked"
                "})"
            )
        )
        assert result == {
            "clicks": [{"label": "Toggle Todo 1", "trusted": True}],
            "checked": True,
        }
    finally:
        await session.kill()


@pytest.mark.slow
def test_real_selector_map_contains_four_controls_and_click_is_trusted():
    if os.environ.get("DOMAUTOPSY_RUN_BU_INTEGRATION") != "1":
        pytest.skip("test Browser Use/Chromium opt-in")

    pytest.importorskip("browser_use")
    try:
        installed_version = package_version("browser-use")
    except PackageNotFoundError:
        pytest.skip("browser-use n'est pas installe")
    if installed_version != SUPPORTED_BROWSER_USE_VERSION:
        pytest.skip(
            f"test borne a browser-use {SUPPORTED_BROWSER_USE_VERSION}, "
            f"version installee : {installed_version}"
        )

    asyncio.run(_exercise_real_browser_use())
