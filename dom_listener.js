(function() {
    if (window.__qaListenerInstalled) return;
    window.__qaListenerInstalled = true;

    // -- BUFFER MEMOIRE + FLUSH PERIODIQUE (evite O(n^2) sur localStorage) --
    let buffer = [];
    try {
        buffer = JSON.parse(localStorage.getItem('__qaLocatorLog') || '[]');
    } catch(e) { buffer = []; }
    let dirty = false;

    function saveEntry(entry) {
        buffer.push(entry);
        dirty = true;
    }
    function flush() {
        if (!dirty) return;
        try {
            localStorage.setItem('__qaLocatorLog', JSON.stringify(buffer));
            dirty = false;
        } catch(e) {}
    }
    setInterval(flush, 1500);
    window.addEventListener('beforeunload', flush);
    window.addEventListener('pagehide', flush);

    // -- ECHAPPEMENT VALEURS D'ATTRIBUT CSS (between double quotes) --
    function attrValue(v) {
        if (v == null) return '';
        return String(v).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
    }

    // -- ECHAPPEMENT LITTERAL XPATH (gere quotes mixtes via concat) --
    function xpathString(s) {
        s = String(s);
        if (s.indexOf("'") === -1) return "'" + s + "'";
        if (s.indexOf('"') === -1) return '"' + s + '"';
        // Les deux quotes presentes -> concat()
        let parts = s.split("'");
        return "concat('" + parts.join("'," + '"' + "'" + '"' + ",'") + "')";
    }

    // -- DETECTION INPUTS SENSIBLES (password, CB, etc.) --
    const SENSITIVE_PATTERN = /(?:^|[\s\-_])(password|passwd|pwd|secret|token|otp|cvv|cvc|ccv|cc[\-_]?num|card[\-_]?num|ssn|sin|pin)(?:$|[\s\-_])/i;
    function isSensitiveField(el) {
        if (!el || !el.getAttribute) return false;
        if ((el.type || '').toLowerCase() === 'password') return true;
        const ac = (el.getAttribute('autocomplete') || '').toLowerCase();
        if (ac.indexOf('cc-') === 0 || ac === 'current-password' || ac === 'new-password' || ac === 'one-time-code') return true;
        const meta = [el.name, el.id, el.getAttribute('aria-label'), el.getAttribute('placeholder'), el.getAttribute('data-testid')]
            .filter(Boolean).join(' ');
        return SENSITIVE_PATTERN.test(meta);
    }

    // -- DETECTION SHADOW DOM --
    function isInShadowDOM(el) {
        let root = el.getRootNode();
        return root instanceof ShadowRoot;
    }

    // -- SELECTEUR SHADOW DOM (chaine) --
    function getShadowSelector(el) {
        let chain = [];
        let current = el;
        while (current) {
            let root = current.getRootNode();
            let localSel = getQuickSelector(current);
            if (root instanceof ShadowRoot) {
                chain.unshift({selector: localSel, shadow: true});
                current = root.host;
            } else {
                chain.unshift({selector: localSel, shadow: false});
                break;
            }
        }
        let pwSel = chain.map(c => c.selector).join(' >>> ');
        let jsSel = chain.map((c, i) => {
            if (i === 0) return 'document.querySelector("' + attrValue(c.selector) + '")';
            return '.shadowRoot.querySelector("' + attrValue(c.selector) + '")';
        }).join('');
        return {
            strategy: 'shadow',
            value: pwSel,
            inShadowDOM: true,
            shadowChain: chain,
            playwrightSelector: pwSel,
            jsSelector: jsSel,
            unique: true,
            matchCount: 1
        };
    }

    // -- SELECTEUR RAPIDE (pour shadow chain ou parent lookup) --
    function getQuickSelector(el) {
        if (!el || !el.getAttribute) return 'unknown';
        if (el.getAttribute('data-testid')) return '[data-testid="' + attrValue(el.getAttribute('data-testid')) + '"]';
        if (el.id && !el.id.match(/^[0-9]|react|ember|__/)) {
            try { return '#' + CSS.escape(el.id); } catch(e) { return '#' + el.id; }
        }
        if (el.getAttribute('name')) return el.tagName.toLowerCase() + '[name="' + attrValue(el.getAttribute('name')) + '"]';
        if (el.getAttribute('aria-label')) return '[aria-label="' + attrValue(el.getAttribute('aria-label')) + '"]';
        let sel = el.tagName.toLowerCase();
        if (el.className && typeof el.className === 'string') {
            let cls = el.className.trim().split(/\s+/).filter(c => c.length > 2 && c.length < 30).slice(0, 2);
            if (cls.length) {
                try { sel += '.' + cls.map(c => CSS.escape(c)).join('.'); }
                catch(e) { sel += '.' + cls.join('.'); }
            }
        }
        return sel;
    }

    // =========================================================
    // -- SELECTEUR INTELLIGENT : cascade stricte + validation --
    // =========================================================
    function getBestSelector(el) {
        if (!el || !el.getAttribute) return {strategy:'unknown', value:'unknown', inShadowDOM:false, unique:false, matchCount:0};

        // Shadow DOM -> logique separee
        if (isInShadowDOM(el)) return getShadowSelector(el);

        let best = null;
        let strategy = 'unknown';
        const tag = el.tagName.toLowerCase();

        // === Tier 1 : Attributs stables (fiabilite max) ===
        if (el.getAttribute('data-testid')) {
            best = '[data-testid="' + attrValue(el.getAttribute('data-testid')) + '"]';
            strategy = 'data-testid';
        }
        else if (el.id && !el.id.match(/^[0-9]|react|ember|__|:/)) {
            try { best = '#' + CSS.escape(el.id); }
            catch(e) { best = '#' + el.id; }
            strategy = 'id';
        }
        else if (el.getAttribute('name')) {
            best = tag + '[name="' + attrValue(el.getAttribute('name')) + '"]';
            strategy = 'name';
        }

        // === Tier 2 : Attributs semantiques ===
        if (!best && el.getAttribute('aria-label')) {
            best = '[aria-label="' + attrValue(el.getAttribute('aria-label')) + '"]';
            strategy = 'aria-label';
        }
        if (!best && el.getAttribute('placeholder')) {
            best = tag + '[placeholder="' + attrValue(el.getAttribute('placeholder')) + '"]';
            strategy = 'placeholder';
        }
        if (!best && el.getAttribute('title')) {
            best = '[title="' + attrValue(el.getAttribute('title')) + '"]';
            strategy = 'title';
        }

        // === Tier 3 : Href pour les liens ===
        if (!best && el.tagName === 'A' && el.getAttribute('href')) {
            let href = el.getAttribute('href');
            if (href !== '#' && href !== '/' && href !== 'javascript:void(0)' && href.length < 100) {
                best = 'a[href="' + attrValue(href) + '"]';
                strategy = 'href';
            }
        }

        // === Tier 4 : Remonter au parent avec attribut stable (icones, SVG) ===
        if (!best) {
            let parent = el.closest('[aria-label], [data-testid], [title]');
            if (parent && parent !== el) {
                if (parent.getAttribute('aria-label')) {
                    best = '[aria-label="' + attrValue(parent.getAttribute('aria-label')) + '"]';
                    strategy = 'parent-aria-label';
                } else if (parent.getAttribute('data-testid')) {
                    best = '[data-testid="' + attrValue(parent.getAttribute('data-testid')) + '"]';
                    strategy = 'parent-data-testid';
                } else if (parent.getAttribute('title')) {
                    best = '[title="' + attrValue(parent.getAttribute('title')) + '"]';
                    strategy = 'parent-title';
                }
            }
        }

        // === Tier 5 : Label associe (pour inputs) ===
        if (!best && (el.tagName === 'INPUT' || el.tagName === 'SELECT' || el.tagName === 'TEXTAREA')) {
            let label = null;
            if (el.id) {
                try { label = document.querySelector('label[for="' + attrValue(el.id) + '"]'); } catch(e) {}
            }
            if (!label) label = el.closest('label');
            if (label) {
                let labelText = (label.textContent || '').trim().substring(0, 40);
                if (labelText) {
                    best = '//label[contains(text(),' + xpathString(labelText) + ')]//input';
                    strategy = 'label-xpath';
                }
            }
        }

        // === Tier 6 : CSS court + nth-of-type (dernier recours CSS) ===
        if (!best) {
            let sel = tag;
            if (el.className && typeof el.className === 'string') {
                let classes = el.className.trim().split(/\s+/)
                    .filter(c => c.length > 2 && c.length < 30
                            && !c.match(/active|hover|focus|open|visible|show|selected|current/))
                    .slice(0, 2);
                if (classes.length) {
                    try { sel += '.' + classes.map(c => CSS.escape(c)).join('.'); }
                    catch(e) { sel += '.' + classes.join('.'); }
                }
            }
            if (el.parentElement) {
                let same = Array.from(el.parentElement.children).filter(s => s.tagName === el.tagName);
                if (same.length > 1) {
                    sel += ':nth-of-type(' + (same.indexOf(el) + 1) + ')';
                }
            }
            best = sel;
            strategy = 'css-short';
        }

        // === VALIDATION : est-ce que le selecteur matche 1 seul element ? ===
        let matchCount = 0;
        try {
            if (strategy === 'label-xpath' || best.startsWith('//')) {
                let xr = document.evaluate(best, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                matchCount = xr.snapshotLength;
            } else {
                matchCount = document.querySelectorAll(best).length;
            }
        } catch(e) { matchCount = -1; }

        // Si multi-match -> tenter XPath text-based
        let text = (el.innerText || el.textContent || '').trim();
        if (matchCount !== 1 && text && text.length > 0 && text.length < 50 && text.indexOf('\n') === -1) {
            let xpathText = '//' + tag + '[contains(text(),' + xpathString(text) + ')]';
            let xpathCount = 0;
            try {
                let xr = document.evaluate(xpathText, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                xpathCount = xr.snapshotLength;
            } catch(e) {}
            if (xpathCount === 1) {
                best = xpathText;
                strategy = 'xpath-text';
                matchCount = 1;
            } else if (xpathCount > 0 && xpathCount < matchCount) {
                best = xpathText;
                strategy = 'xpath-text';
                matchCount = xpathCount;
            }
        }

        return {
            strategy: strategy,
            value: best,
            inShadowDOM: false,
            unique: matchCount === 1,
            matchCount: matchCount
        };
    }

    // -- RESOLUTION ELEMENT REEL (composedPath + bubble up) --
    function getRealTarget(e) {
        let el = e.target;
        if (e.composedPath && e.composedPath().length > 0) {
            el = e.composedPath()[0];
        }
        let interactiveTags = ['BUTTON', 'A', 'INPUT', 'SELECT', 'TEXTAREA'];
        let maxDepth = 5;
        let depth = 0;
        while (el && depth < maxDepth) {
            if (el.id || (el.getAttribute && el.getAttribute('data-testid')) || (el.getAttribute && el.getAttribute('name'))) break;
            if (interactiveTags.includes(el.tagName)) break;
            if (el.getAttribute && el.getAttribute('aria-label')) break;
            let parent = el.parentElement;
            if (!parent) break;
            if (interactiveTags.includes(parent.tagName) || parent.id
                || (parent.getAttribute && parent.getAttribute('data-testid'))
                || (parent.getAttribute && parent.getAttribute('aria-label'))) {
                el = parent;
                break;
            }
            el = parent;
            depth++;
        }
        return el;
    }

    // -- EVENT LISTENERS --
    document.addEventListener('click', (e) => {
        let el = getRealTarget(e);
        let sel = getBestSelector(el);
        let text = (el.innerText || el.textContent || '').trim().substring(0, 80);
        saveEntry({
            action: 'click',
            timestamp: Date.now(),
            tag: el.tagName,
            text: text,
            selector: sel,
            url: location.href,
            inShadowDOM: sel.inShadowDOM,
            attributes: {
                id: el.id || null,
                name: el.getAttribute ? el.getAttribute('name') : null,
                type: el.getAttribute ? el.getAttribute('type') : null,
                class: (typeof el.className === 'string') ? el.className : null,
                href: el.getAttribute ? el.getAttribute('href') : null,
                'data-testid': el.getAttribute ? el.getAttribute('data-testid') : null,
                'aria-label': el.getAttribute ? el.getAttribute('aria-label') : null,
                role: el.getAttribute ? el.getAttribute('role') : null
            }
        });
    }, true);

    document.addEventListener('input', (e) => {
        let el = getRealTarget(e);
        let sel = getBestSelector(el);
        let sensitive = isSensitiveField(el);
        saveEntry({
            action: 'input',
            timestamp: Date.now(),
            tag: el.tagName,
            value: sensitive ? '<redacted>' : (el.value || ''),
            sensitive: sensitive,
            selector: sel,
            url: location.href,
            inShadowDOM: sel.inShadowDOM,
            attributes: {
                id: el.id || null,
                name: el.getAttribute ? el.getAttribute('name') : null,
                type: el.getAttribute ? el.getAttribute('type') : null,
                placeholder: el.getAttribute ? el.getAttribute('placeholder') : null,
                'data-testid': el.getAttribute ? el.getAttribute('data-testid') : null,
                'aria-label': el.getAttribute ? el.getAttribute('aria-label') : null
            }
        });
    }, true);

    // -- SCROLL LISTENER (debounced) --
    let scrollTimer = null;
    let scrollStartY = window.scrollY;
    window.addEventListener('scroll', () => {
        if (!scrollTimer) {
            scrollStartY = window.scrollY;
        }
        clearTimeout(scrollTimer);
        scrollTimer = setTimeout(() => {
            let scrollEndY = window.scrollY;
            let delta = scrollEndY - scrollStartY;
            if (Math.abs(delta) > 50) {
                saveEntry({
                    action: 'scroll',
                    timestamp: Date.now(),
                    tag: 'WINDOW',
                    direction: delta > 0 ? 'down' : 'up',
                    deltaY: delta,
                    scrollY: scrollEndY,
                    viewport: {
                        width: window.innerWidth,
                        height: window.innerHeight,
                        docHeight: document.documentElement.scrollHeight
                    },
                    url: location.href,
                    selector: {strategy: 'window', value: 'window.scrollTo(0, ' + scrollEndY + ')', inShadowDOM: false, unique: true, matchCount: 1},
                    inShadowDOM: false,
                    attributes: {}
                });
            }
            scrollTimer = null;
        }, 250);
    }, true);

    console.log('[QA Listener V3] Installed on ' + location.href + ' (Smart selectors + Shadow DOM + Scroll + Sensitive filter)');
})();
