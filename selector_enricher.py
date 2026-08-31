"""Enrichissement live des elements Browser Use en candidats de selecteurs.

Ce module s'execute avant chaque lot d'actions de ``agent.run()`` puis une
derniere fois avant la fermeture de Chromium. ``backend_node_id`` permet
alors de retrouver le noeud exact via CDP. Les candidats sont construits et
mesures dans le DOM vivant ; le post-traitement Python ne fait ensuite que
choisir parmi ces preuves.

``backend_node_id`` est volontairement absent des selecteurs de replay : il
n'est valide que dans la session CDP de capture.
"""

from __future__ import annotations

from typing import Any


_BUILD_CANDIDATES_JS = r"""
function(buXPath, expected) {
  const el = this;
  const out = [];
  const seen = new Set();

  // DOM.resolveNode est execute sur plusieurs cibles CDP. Un
  // backendNodeId est propre a sa cible ; on refuse donc un noeud resolu
  // dans la mauvaise page si son empreinte capturee ne correspond pas.
  expected = expected || {};
  const expectedTag = String(expected.tag || '').toLowerCase();
  if (expectedTag && String(el.tagName || '').toLowerCase() !== expectedTag) return [];
  const expectedAttrs = expected.attributes || {};
  for (const name of ['id', 'name', 'data-testid', 'data-qa', 'data-test', 'data-cy',
                      'aria-label', 'placeholder', 'href', 'type']) {
    if (expectedAttrs[name] == null || expectedAttrs[name] === '') continue;
    if (el.getAttribute(name) !== String(expectedAttrs[name])) return [];
  }

  const quoted = value => JSON.stringify(String(value));
  const dynamicValue = value => {
    const v = String(value || '');
    return !v || v.length > 100
      || /^[0-9]{10,}$/.test(v)
      || /^[a-f0-9]{12,}$/i.test(v)
      || /^[0-9a-f]{8}-[0-9a-f-]{27,}$/i.test(v)
      || /(?:^|[-_:])(react|ember|vue|ng|uid|uuid|session|token)[-_:0-9a-f]/i.test(v);
  };
  const dynamicClass = value => /^(?:active|focus|hover|open|show|selected|current)$/i.test(value)
    || /^(?:css|sc|jsx)-[a-z0-9_-]{5,}$/i.test(value)
    || /^[a-f0-9]{8,}$/i.test(value);
  const rootFor = node => node.getRootNode && node.getRootNode() || document;

  const addCss = (selector, strategy, stability, priority, root, target) => {
    if (!selector || seen.has('css:' + selector)) return;
    let nodes;
    try { nodes = Array.from(root.querySelectorAll(selector)); }
    catch (_) { return; }
    if (!nodes.includes(target)) return;
    seen.add('css:' + selector);
    out.push({
      value: selector,
      strategy,
      selectorType: 'css',
      matchCount: nodes.length,
      unique: nodes.length === 1,
      stability,
      priority,
      verifiedAtCapture: true,
    });
  };

  // Playwright supporte le pseudo-selecteur :visible, contrairement au DOM
  // natif. On le mesure donc explicitement sur les resultats querySelectorAll
  // avant de l'emettre. Cas concret : Automation Exercise rend deux boutons
  // data-product-id identiques (carte normale + overlay), mais un seul visible.
  const isVisible = node => {
    if (!node || !node.isConnected) return false;
    const style = getComputedStyle(node);
    if (style.display === 'none' || style.visibility === 'hidden' || style.visibility === 'collapse') return false;
    const rect = node.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0;
  };
  const addVisibleCss = (selector, strategy, stability, priority, root, target) => {
    if (!selector || seen.has('css-visible:' + selector)) return;
    let nodes;
    try { nodes = Array.from(root.querySelectorAll(selector)).filter(isVisible); }
    catch (_) { return; }
    if (nodes.length !== 1 || nodes[0] !== target) return;
    seen.add('css-visible:' + selector);
    out.push({
      value: selector + ':visible',
      strategy: strategy + '-visible',
      selectorType: 'css',
      matchCount: 1,
      unique: true,
      stability: stability === 'high' ? 'medium' : stability,
      priority: priority + 10,
      verifiedAtCapture: true,
      visibilityMeasured: true,
    });
  };

  const localCandidates = (node, root) => {
    const local = [];
    const localSeen = new Set();
    const add = (selector, strategy, stability, priority) => {
      if (!selector || localSeen.has(selector)) return;
      let nodes;
      try { nodes = Array.from(root.querySelectorAll(selector)); }
      catch (_) { return; }
      if (!nodes.includes(node)) return;
      localSeen.add(selector);
      local.push({selector, strategy, stability, priority, count: nodes.length});
    };
    const tag = (node.tagName || '').toLowerCase();
    const id = node.getAttribute && node.getAttribute('id');
    if (id) add('[id=' + quoted(id) + ']', 'id-attr', dynamicValue(id) ? 'low' : 'high', dynamicValue(id) ? 70 : 10);
    for (const name of ['data-testid', 'data-qa', 'data-test', 'data-cy']) {
      const value = node.getAttribute && node.getAttribute(name);
      if (value) add(
        '[' + name + '=' + quoted(value) + ']', name,
        dynamicValue(value) ? 'low' : 'high', dynamicValue(value) ? 75 : 5
      );
    }
    const name = node.getAttribute && node.getAttribute('name');
    if (name && tag) add(tag + '[name=' + quoted(name) + ']', 'name', 'high', 15);
    const aria = node.getAttribute && node.getAttribute('aria-label');
    if (aria) add('[aria-label=' + quoted(aria) + ']', 'aria-label', 'high', 20);
    const placeholder = node.getAttribute && node.getAttribute('placeholder');
    if (placeholder && tag) add(tag + '[placeholder=' + quoted(placeholder) + ']', 'placeholder', 'medium', 35);
    const action = node.getAttribute && node.getAttribute('action');
    if (action && tag === 'form' && !/(?:jsessionid|sessionid|token|sid)=/i.test(action)) {
      add('form[action=' + quoted(action) + ']', 'form-action', 'high', 18);
    }
    const href = node.getAttribute && node.getAttribute('href');
    if (href && tag === 'a' && !/(?:jsessionid|sessionid|token|sid)=/i.test(href)) {
      add('a[href=' + quoted(href) + ']', 'href', 'medium', 40);
    }
    const title = node.getAttribute && node.getAttribute('title');
    if (title) add('[title=' + quoted(title) + ']', 'title', 'medium', 42);
    const value = node.getAttribute && node.getAttribute('value');
    if (value && (tag === 'button' || tag === 'input') && !dynamicValue(value)) {
      add(tag + '[value=' + quoted(value) + ']', 'value-attr', 'medium', 38);
    }
    if (node.attributes) {
      for (const attr of Array.from(node.attributes)) {
        if (!attr.name.startsWith('data-') || !attr.value) continue;
        if (['data-testid', 'data-qa', 'data-test', 'data-cy'].includes(attr.name)) continue;
        add('[' + attr.name + '=' + quoted(attr.value) + ']', 'data-attr', dynamicValue(attr.value) ? 'low' : 'medium', dynamicValue(attr.value) ? 80 : 30);
      }
    }
    const classes = typeof node.className === 'string'
      ? node.className.trim().split(/\s+/).filter(c => c && !dynamicClass(c)).slice(0, 3)
      : [];
    if (tag && classes.length) {
      const escaped = classes.map(c => { try { return CSS.escape(c); } catch (_) { return c; } });
      add(tag + '.' + escaped.join('.'), 'class-scope', 'medium', 45);
    }
    if (tag) add(tag, 'tag', 'low', 95);
    return local.sort((a, b) => a.priority - b.priority || a.selector.length - b.selector.length);
  };

  // Un element dans un shadow root requiert une chaine dont CHAQUE segment
  // est unique dans sa racine locale. querySelectorAll(document) ne traverse
  // pas les shadow roots, donc on ne fabrique jamais matchCount=1.
  const initialRoot = rootFor(el);
  if (initialRoot instanceof ShadowRoot) {
    const chain = [];
    let node = el;
    let valid = true;
    while (node) {
      const root = rootFor(node);
      const unique = localCandidates(node, root).find(c => c.count === 1 && c.stability !== 'low');
      if (!unique) { valid = false; break; }
      chain.unshift({selector: unique.selector, shadow: root instanceof ShadowRoot});
      if (!(root instanceof ShadowRoot)) break;
      node = root.host;
    }
    if (valid && chain.length > 1) {
      out.push({
        value: chain.map(x => x.selector).join(' >>> '),
        strategy: 'shadow-chain',
        selectorType: 'css',
        matchCount: 1,
        unique: true,
        stability: 'high',
        priority: 8,
        verifiedAtCapture: true,
        inShadowDOM: true,
        shadowChain: chain,
      });
    }
    return out;
  }

  const tag = (el.tagName || '').toLowerCase();
  for (const c of localCandidates(el, document)) {
    addCss(c.selector, c.strategy, c.stability, c.priority, document, el);
    if (c.count > 1 && isVisible(el)) {
      addVisibleCss(c.selector, c.strategy, c.stability, c.priority, document, el);
    }
  }

  const href = el.getAttribute && el.getAttribute('href');
  if (href && tag === 'a' && !/(?:jsessionid|sessionid|token|sid)=/i.test(href)) {
    addCss('a[href=' + quoted(href) + ']', 'href', 'medium', 40, document, el);
  }
  const title = el.getAttribute && el.getAttribute('title');
  if (title) addCss('[title=' + quoted(title) + ']', 'title', 'medium', 42, document, el);

  // Si le candidat local est ambigu, on le scope avec un ancetre mesurable.
  const targetCandidates = out.filter(c => c.selectorType === 'css').map(c => c.value);
  let ancestor = el.parentElement;
  let depth = 0;
  while (ancestor && ancestor !== document.body && depth < 6) {
    const ancestorCandidates = localCandidates(ancestor, document)
      .filter(c => c.count >= 1 && c.stability !== 'low')
      .slice(0, 8);
    for (const ac of ancestorCandidates) {
      for (const target of targetCandidates) {
        addCss(ac.selector + ' ' + target, 'ancestor-scope',
          ac.stability === 'high' ? 'high' : 'medium', 25 + depth,
          document, el);
      }
    }
    ancestor = ancestor.parentElement;
    depth += 1;
  }

  const addXPath = xpath => {
    if (!xpath || seen.has('xpath:' + xpath)) return;
    try {
      const snapshot = document.evaluate(xpath, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
      let contains = false;
      for (let i = 0; i < snapshot.snapshotLength; i++) {
        if (snapshot.snapshotItem(i) === el) { contains = true; break; }
      }
      if (!contains) return;
      seen.add('xpath:' + xpath);
      out.push({
        value: xpath,
        strategy: 'xpath',
        selectorType: 'xpath',
        matchCount: snapshot.snapshotLength,
        unique: snapshot.snapshotLength === 1,
        stability: 'low',
        priority: 90,
        verifiedAtCapture: true,
      });
    } catch (_) {}
  };
  addXPath(buXPath);

  return out.sort((a, b) => a.priority - b.priority || a.value.length - b.value.length);
}
"""


def _iter_elements(history: list[dict[str, Any]]):
    """Yield ``(element, URL du step)`` (legacy + normalized)."""
    seen: set[int] = set()
    for step in history:
        if not isinstance(step, dict):
            continue
        state = step.get("state") or {}
        step_url = state.get("url") if isinstance(state, dict) else None
        legacy = step.get("interacted_element")
        if isinstance(legacy, dict) and id(legacy) not in seen:
            seen.add(id(legacy))
            yield legacy, step_url
        for action in step.get("normalized_actions") or []:
            if not isinstance(action, dict):
                continue
            elem = action.get("interacted_element")
            if isinstance(elem, dict) and id(elem) not in seen:
                seen.add(id(elem))
                yield elem, step_url


async def _candidates_from_session(
    session: Any,
    backend_node_id: int,
    xpath: str | None,
    element: dict[str, Any],
) -> list[dict[str, Any]]:
    resolved = await session.send("DOM.resolveNode", {"backendNodeId": backend_node_id})
    object_id = ((resolved or {}).get("object") or {}).get("objectId")
    if not object_id:
        raise RuntimeError("DOM.resolveNode n'a retourne aucun objectId")
    try:
        response = await session.send("Runtime.callFunctionOn", {
            "objectId": object_id,
            "functionDeclaration": _BUILD_CANDIDATES_JS,
            "arguments": [
                {"value": xpath or ""},
                {"value": {
                    "tag": element.get("node_name") or element.get("tag_name") or "",
                    "attributes": element.get("attributes") or {},
                }},
            ],
            "returnByValue": True,
            "awaitPromise": False,
        })
        remote = (response or {}).get("result") or {}
        value = remote.get("value")
        return value if isinstance(value, list) else []
    finally:
        try:
            await session.send("Runtime.releaseObject", {"objectId": object_id})
        except Exception:
            pass


async def enrich_browser_use_history_selectors(
    history: list[dict[str, Any]],
    context: Any,
    evidence_cache: dict[
        tuple[int, str | None, int | str | None, str | None],
        tuple[list[dict[str, Any]], str | None],
    ] | None = None,
) -> dict[str, int]:
    """Ajoute ``selector_candidates`` aux interacted_element de BU.

    Toutes les pages/contextes CDP sont essayes car ``backend_node_id`` est
    rattache a une cible precise. Un echec d'enrichissement reste trace dans
    ``selector_enrichment`` et ne provoque jamais la fabrication d'un locator.
    """
    stats = {"elements": 0, "resolved": 0, "unique": 0, "unresolved": 0}
    elements = list(_iter_elements(history))
    stats["elements"] = len(elements)
    if not elements:
        return stats

    sessions: list[tuple[Any, str]] = []
    sessions_initialized = False

    async def _ensure_sessions() -> None:
        nonlocal sessions_initialized
        if sessions_initialized:
            return
        sessions_initialized = True
        for page in list(getattr(context, "pages", []) or []):
            try:
                session = await context.new_cdp_session(page)
                await session.send("DOM.enable")
                await session.send("Runtime.enable")
                sessions.append((session, getattr(page, "url", "")))
            except Exception:
                continue

    persistent_cache = evidence_cache if evidence_cache is not None else {}
    attempt_cache: dict[
        tuple[int, str | None, int | str | None, str | None],
        tuple[list[dict[str, Any]], str | None, str | None],
    ] = {}
    for elem, step_url in elements:
        backend_raw = elem.get("backend_node_id")
        try:
            backend_node_id = int(backend_raw)
        except (TypeError, ValueError):
            elem["selector_candidates"] = []
            elem["selector_enrichment"] = {"status": "unresolved", "reason": "backend_node_id absent"}
            stats["unresolved"] += 1
            continue
        fingerprint = (
            elem.get("stable_hash")
            or elem.get("element_hash")
            or elem.get("x_path")
            or elem.get("xpath")
        )
        key = (backend_node_id, elem.get("frame_id"), fingerprint, step_url)
        if key in persistent_cache:
            cached_candidates, cached_url = persistent_cache[key]
            attempt_cache[key] = (cached_candidates, cached_url, None)
        elif key not in attempt_cache:
            await _ensure_sessions()
            candidates: list[dict[str, Any]] = []
            resolved_url: str | None = None
            last_error: str | None = None
            ordered_sessions = sorted(
                sessions,
                key=lambda item: 0 if step_url and item[1] == step_url else 1,
            )
            for session, page_url in ordered_sessions:
                try:
                    candidates = await _candidates_from_session(
                        session,
                        backend_node_id,
                        elem.get("x_path") or elem.get("xpath"),
                        elem,
                    )
                    if candidates:
                        resolved_url = page_url
                        last_error = None
                        break
                    last_error = "noeud resolu mais empreinte/candidats incompatibles"
                except Exception as exc:
                    last_error = f"{type(exc).__name__}: {exc}"[:300]
            attempt_cache[key] = (candidates, resolved_url, last_error)
            # Une preuve positive reste valable pour l'artefact de capture,
            # meme si le noeud est detache apres une navigation ulterieure.
            if candidates:
                persistent_cache[key] = (candidates, resolved_url)
        candidates, resolved_url, error = attempt_cache[key]
        elem["selector_candidates"] = candidates
        if candidates:
            unique_count = sum(
                1 for candidate in candidates
                if candidate.get("unique") is True and candidate.get("matchCount") == 1
            )
            elem["selector_enrichment"] = {
                "status": "resolved",
                "page": resolved_url,
                "candidateCount": len(candidates),
                "uniqueCandidateCount": unique_count,
            }
            stats["resolved"] += 1
            if unique_count:
                stats["unique"] += 1
        else:
            elem["selector_enrichment"] = {
                "status": "unresolved",
                "page": resolved_url,
                "reason": error or "aucun candidat ne cible le noeud exact",
            }
            stats["unresolved"] += 1

    for session, _ in sessions:
        try:
            await session.detach()
        except Exception:
            pass
    return stats


async def enrich_browser_use_step_snapshot(
    browser_state_summary: Any,
    model_output: Any,
    context: Any,
    evidence_cache: dict[
        tuple[int, str | None, int | str | None, str | None],
        tuple[list[dict[str, Any]], str | None],
    ],
) -> dict[str, int]:
    """Mesure les cibles d'un step BU *avant* l'execution des actions.

    Browser Use appelle ``register_new_step_callback`` apres avoir produit
    ``model_output``, mais avant ``multi_act``. C'est le seul moment qui
    garantit que tous les backendNodeId issus du selector_map sont encore
    attaches, meme si une action suivante navigue vers une autre page.
    """
    from browser_use.agent.views import AgentHistory

    dom_state = getattr(browser_state_summary, "dom_state", None)
    selector_map = getattr(dom_state, "selector_map", None) or {}
    elements = AgentHistory.get_interacted_element(model_output, selector_map)

    normalized_actions: list[dict[str, Any]] = []
    for element in elements:
        if element is None:
            normalized_actions.append({"interacted_element": None})
            continue
        if hasattr(element, "to_dict"):
            element_dict = element.to_dict()
        elif hasattr(element, "model_dump"):
            element_dict = element.model_dump(exclude_none=True)
        else:
            element_dict = {}
        normalized_actions.append({
            "interacted_element": element_dict if isinstance(element_dict, dict) else None,
        })

    snapshot = [{
        "state": {"url": getattr(browser_state_summary, "url", None)},
        "normalized_actions": normalized_actions,
    }]
    return await enrich_browser_use_history_selectors(
        snapshot,
        context,
        evidence_cache=evidence_cache,
    )
