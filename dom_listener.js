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

    // Attributs utiles au rapprochement BU/DOM. Tous les data-* sont
    // conserves : limiter la liste a data-testid faisait perdre, entre
    // autres, data-product-id sur les grilles e-commerce.
    function captureAttributes(el) {
        if (!el || !el.getAttribute) return {};
        const out = {
            id: el.id || null,
            name: el.getAttribute('name'),
            type: el.getAttribute('type'),
            class: (typeof el.className === 'string') ? el.className : null,
            href: el.getAttribute('href'),
            placeholder: el.getAttribute('placeholder'),
            'aria-label': el.getAttribute('aria-label'),
            role: el.getAttribute('role')
        };
        if (el.attributes) {
            for (const attr of Array.from(el.attributes)) {
                if (attr.name.startsWith('data-')) out[attr.name] = attr.value;
            }
        }
        return out;
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
    // En mode BENCH (window.__DOMAUTOPSY_BENCH_MODE=true injecte avant le
    // listener), la redaction est desactivee : on veut la vraie valeur
    // dans le TS pour que le replay puisse se reconnecter/soumettre.
    // Les credentials du corpus bench sont deja publics (comptes demo).
    const SENSITIVE_PATTERN = /(?:^|[\s\-_])(password|passwd|pwd|secret|token|otp|cvv|cvc|ccv|cc[\-_]?num|card[\-_]?num|ssn|sin|pin)(?:$|[\s\-_])/i;
    function isSensitiveField(el) {
        if (window.__DOMAUTOPSY_BENCH_MODE === true) return false;
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
        let allSegmentsUnique = true;
        while (current) {
            let root = current.getRootNode();
            let localSel = getQuickSelector(current);
            let localCount = 0;
            try {
                const localMatches = Array.from(root.querySelectorAll(localSel));
                localCount = localMatches.length;
                if (localCount !== 1 || localMatches[0] !== current) allSegmentsUnique = false;
            } catch(e) {
                allSegmentsUnique = false;
            }
            if (root instanceof ShadowRoot) {
                chain.unshift({selector: localSel, shadow: true, matchCount: localCount});
                current = root.host;
            } else {
                chain.unshift({selector: localSel, shadow: false, matchCount: localCount});
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
            unique: allSegmentsUnique,
            matchCount: allSegmentsUnique ? 1 : null,
            verifiedAtCapture: true
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
        const countCssTarget = (candidate) => {
            try {
                const nodes = Array.from(document.querySelectorAll(candidate));
                return nodes.includes(el) ? nodes.length : 0;
            } catch(e) { return -1; }
        };
        const countXPathTarget = (candidate) => {
            try {
                const xr = document.evaluate(candidate, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null);
                let containsTarget = false;
                for (let i = 0; i < xr.snapshotLength; i++) {
                    if (xr.snapshotItem(i) === el) { containsTarget = true; break; }
                }
                return containsTarget ? xr.snapshotLength : 0;
            } catch(e) { return -1; }
        };

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
            let rawHref = el.getAttribute('href');
            // Detecte-t-on un token de session dans le href brut ? Si oui,
            // le vrai <a href="..."> sur la page rendue peut aussi contenir
            // un token (session-scoped) -> match EXACT casse. On strip pour
            // l'affichage ET on utilise [href*="..."] (partial contains)
            // au lieu de [href="..."] pour etre robuste a la re-attribution
            // du token a chaque nouvelle session (cas ParaBank overview.htm;jsessionid=NEW).
            const SESSION_TOKEN_RE = /(;jsessionid=|;jsession=|[?&](?:jsessionid|phpsessid|asp\.net_sessionid|sid|sessionid|csrftoken)=)/i;
            const hasSessionToken = SESSION_TOKEN_RE.test(rawHref);
            let href = rawHref.replace(/;jsessionid=[^?&#]*/i, '')
                       .replace(/;jsession=[^?&#]*/i, '')
                       .replace(/[?&](jsessionid|phpsessid|asp\.net_sessionid|sid|sessionid|csrftoken)=[^&#]*/gi, function(m, k, offset, s){
                           const rest = s.slice(offset + m.length);
                           if (m[0] === '?' && rest.startsWith('&')) return '?';
                           if (m[0] === '?') return '';
                           return '';
                       })
                       .replace(/\?$/, '');
            if (href !== '#' && href !== '/' && href !== 'javascript:void(0)' && href.length < 100) {
                if (hasSessionToken) {
                    // partial contains : robuste au token qui change a chaque session
                    best = 'a[href*="' + attrValue(href) + '"]';
                    strategy = 'href-contains';
                } else {
                    best = 'a[href="' + attrValue(href) + '"]';
                    strategy = 'href';
                }
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
        if (strategy === 'label-xpath' || best.startsWith('//')) {
            matchCount = countXPathTarget(best);
        } else {
            matchCount = countCssTarget(best);
        }

        // Si multi-match ET c'est un input/button avec 'value' visible
        // (ex: <input type="submit" value="Register">), enrichir avec le
        // value pour discriminer. Cas frequent sur les formulaires legacy
        // qui ont juste class="button" partage entre Login/Register/Cancel.
        if (matchCount !== 1 && (tag === 'input' || tag === 'button')) {
            let val = el.getAttribute('value');
            if (val && val.length > 0 && val.length < 60) {
                let valSel = tag + '[value="' + attrValue(val) + '"]';
                try {
                    let valCount = countCssTarget(valSel);
                    if (valCount === 1) {
                        best = valSel;
                        strategy = 'value-attr';
                        matchCount = 1;
                    } else if (valCount > 0 && valCount < matchCount) {
                        // Combiner class actuelle + value pour affiner
                        let combined = best + '[value="' + attrValue(val) + '"]';
                        let combCount = countCssTarget(combined);
                        if (combCount === 1) {
                            best = combined;
                            strategy = 'class-value';
                            matchCount = 1;
                        }
                    }
                } catch(e) {}
            }
        }

        // Si multi-match sur un INPUT sans value attr : tenter des
        // discriminants stables (placeholder, form parent id, section
        // ancestor). Cas ex automationexercise : 2 forms sur /login,
        // chacun avec input[name="email"] -> selecteur non-unique.
        if (matchCount !== 1 && tag === 'input') {
            const tryRefine = (candidate, strat) => {
                try {
                    const c = countCssTarget(candidate);
                    if (c === 1) { best = candidate; strategy = strat; matchCount = 1; return true; }
                    if (c > 0 && c < matchCount) { best = candidate; strategy = strat; matchCount = c; }
                } catch(e) {}
                return false;
            };
            const ph = el.getAttribute('placeholder');
            if (ph && ph.length > 0 && ph.length < 60 && matchCount !== 1) {
                if (tryRefine(best + '[placeholder="' + attrValue(ph) + '"]', 'name-placeholder')) {}
            }
            if (matchCount !== 1) {
                const form = el.closest('form');
                if (form) {
                    if (form.id) {
                        tryRefine('form#' + CSS.escape(form.id) + ' ' + best, 'form-id-scope');
                    } else if (form.getAttribute('name')) {
                        tryRefine('form[name="' + attrValue(form.getAttribute('name')) + '"] ' + best, 'form-name-scope');
                    } else if (form.getAttribute('action')) {
                        const act = form.getAttribute('action');
                        if (act.length < 60) tryRefine('form[action="' + attrValue(act) + '"] ' + best, 'form-action-scope');
                    }
                }
            }
            // Pas de fallback `best:nth-of-type(globalIndex)`: nth-of-type
            // est relatif aux freres, pas a document.querySelectorAll(best).
            // Une telle conversion pouvait produire un locator unique mais
            // pointant vers un autre champ.
        }

        // Si multi-match -> tenter XPath text-based
        let text = (el.innerText || el.textContent || '').trim();
        if (matchCount !== 1 && text && text.length > 0 && text.length < 50 && text.indexOf('\n') === -1) {
            let xpathText = '//' + tag + '[contains(text(),' + xpathString(text) + ')]';
            let xpathCount = 0;
            xpathCount = countXPathTarget(xpathText);
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

        // === Tier "self data-attr" (generique tous tags) ===
        // Beaucoup de sites listent N items structurellement identiques
        // (products grid, table rows, feed cards) ou seul un data-* de
        // l'element identifie l'item : data-product-id, data-item-id,
        // data-row-id, data-index, data-key. Un [data-product-id="1"]
        // rend le selecteur unique la ou class+tag ne peuvent pas.
        if (matchCount !== 1 && el.attributes) {
            for (let i = 0; i < el.attributes.length; i++) {
                const a = el.attributes[i];
                if (!a.name.startsWith('data-')) continue;
                if (!a.value || a.value.length === 0 || a.value.length > 60) continue;
                const cand = '[' + a.name + '="' + attrValue(a.value) + '"]';
                try {
                    const c = countCssTarget(cand);
                    if (c === 1) {
                        best = cand;
                        strategy = 'self-data-attr';
                        matchCount = 1;
                        break;
                    }
                } catch(e) {}
            }
        }

        // === Tier "ancestor scope" (generique tous tags) ===
        // Remonter l'arbre jusqu'a trouver un ancetre avec un attribut
        // discriminant (id, data-testid, data-*) qui, combine au selecteur
        // courant, rend l'ensemble unique. Cas type : bouton "Add to cart"
        // dans une carte produit dont l'ancetre proche porte data-product-id
        // ou id="product-X". Se limite a 6 niveaux (au-dela = trop generique).
        if (matchCount !== 1) {
            let anc = el.parentElement;
            let depth = 0;
            while (anc && anc !== document.body && depth < 6) {
                const ancCandidates = [];
                if (anc.id) {
                    ancCandidates.push('#' + CSS.escape(anc.id));
                }
                if (anc.getAttribute && anc.getAttribute('data-testid')) {
                    ancCandidates.push('[data-testid="' + attrValue(anc.getAttribute('data-testid')) + '"]');
                }
                if (anc.attributes) {
                    for (let i = 0; i < anc.attributes.length; i++) {
                        const a = anc.attributes[i];
                        if (!a.name.startsWith('data-')) continue;
                        if (a.name === 'data-testid') continue;
                        if (!a.value || a.value.length === 0 || a.value.length > 60) continue;
                        ancCandidates.push('[' + a.name + '="' + attrValue(a.value) + '"]');
                    }
                }
                for (const ancSel of ancCandidates) {
                    const scoped = ancSel + ' ' + best;
                    try {
                        const c = countCssTarget(scoped);
                        if (c === 1) {
                            best = scoped;
                            strategy = 'ancestor-scope';
                            matchCount = 1;
                            break;
                        }
                    } catch(e) {}
                }
                if (matchCount === 1) break;
                anc = anc.parentElement;
                depth++;
            }
        }

        return {
            strategy: strategy,
            value: best,
            inShadowDOM: false,
            unique: matchCount === 1,
            matchCount: matchCount,
            verifiedAtCapture: true
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

    function getParentListItemContext(el, selector) {
        const li = el && el.closest ? el.closest('li') : null;
        if (!li) return {label: null, matchCount: null, scopedMatchCount: null, checkboxMatchCount: null};
        const labelEl = li.querySelector('label');
        const label = labelEl
            ? (labelEl.innerText || labelEl.textContent || '').trim().substring(0, 200)
            : null;
        if (!label) return {label: null, matchCount: null, scopedMatchCount: null, checkboxMatchCount: null};
        let matchCount = null;
        let scopedMatchCount = null;
        let checkboxMatchCount = null;
        try {
            const root = li.getRootNode();
            matchCount = Array.from(root.querySelectorAll('li')).filter(candidate => {
                const candidateLabel = candidate.querySelector('label');
                const text = candidateLabel
                    ? (candidateLabel.innerText || candidateLabel.textContent || '').trim().substring(0, 200)
                    : null;
                return text === label;
            }).length;
            checkboxMatchCount = li.querySelectorAll('input[type="checkbox"], [role="checkbox"]').length;
            if (selector && selector.value && !selector.value.startsWith('//') && !selector.inShadowDOM) {
                const scoped = Array.from(li.querySelectorAll(selector.value));
                if (scoped.includes(el)) scopedMatchCount = scoped.length;
            }
        } catch(e) {}
        return {
            label: label,
            matchCount: matchCount,
            scopedMatchCount: scopedMatchCount,
            checkboxMatchCount: checkboxMatchCount
        };
    }

    // -- EVENT LISTENERS --
    document.addEventListener('click', (e) => {
        let el = getRealTarget(e);
        let sel = getBestSelector(el);
        let text = (el.innerText || el.textContent || '').trim().substring(0, 80);
        // Contexte parent li (TodoMVC-like) : si le click est dans un
        // <li>, on capture le texte du label pour permettre la
        // desambiguisation dans le TS via getByRole('listitem').filter()
        // On garde le click (pas de skip meme sur checkbox) : le change
        // event le complete avec la semantique on/off, mais le click
        // reste l'action canonique de reference.
        const parentContext = getParentListItemContext(el, sel);
        let parentLabel = parentContext.label;
        saveEntry({
            action: 'click',
            timestamp: Date.now(),
            isTrusted: e.isTrusted === true,
            tag: el.tagName,
            text: text,
            parentLabel: parentLabel,
            parentLabelMatchCount: parentContext.matchCount,
            parentScopedMatchCount: parentContext.scopedMatchCount,
            selector: sel,
            url: location.href,
            inShadowDOM: sel.inShadowDOM,
            attributes: captureAttributes(el)
        });
    }, true);

    // -- INPUT LISTENER (fix cahier R9 : les checkbox/radio ne sont PAS
    // des inputs texte, filter pour ne pas produire de setText("on")
    // parasite qui fait des saisies fantomes). Le vrai toggle est
    // capture par le click listener (ou par l'evaluate BU).
    document.addEventListener('input', (e) => {
        let el = getRealTarget(e);
        // Skip checkbox/radio : leur "input" event = toggle, capturable
        // via 'change' ou via le click deja capture. Value=="on" par defaut
        // n'a aucun sens en saisie texte.
        const elType = (el && el.getAttribute && (el.getAttribute('type') || '').toLowerCase()) || '';
        if (elType === 'checkbox' || elType === 'radio') return;
        let sel = getBestSelector(el);
        let sensitive = isSensitiveField(el);
        // <select> : capture le LABEL visible de l'option choisie + index +
        // unicite du label parmi les options. Le generateur TS emet
        // .selectOption({label:...}) si label unique, sinon fallback index.
        // Evite les .fill(value_technique) qui casse : (a) fill sur select
        // throw en Playwright, (b) la value peut etre un ID ephemere
        // (ex: ParaBank fromAccountId=15120 change a chaque register).
        if (el.tagName === 'SELECT') {
            let selectedIdx = el.selectedIndex;
            let selectedOpt = selectedIdx >= 0 ? el.options[selectedIdx] : null;
            let optionLabel = selectedOpt ? (selectedOpt.text || selectedOpt.innerText || '').trim() : '';
            let optionValue = selectedOpt ? (selectedOpt.value || '') : '';
            // Comptage : ce label est-il unique parmi les options ?
            let sameLabelCount = 0;
            for (let i = 0; i < el.options.length; i++) {
                if ((el.options[i].text || el.options[i].innerText || '').trim() === optionLabel) sameLabelCount++;
            }
            saveEntry({
                action: 'select',
                timestamp: Date.now(),
                isTrusted: e.isTrusted === true,
                tag: 'SELECT',
                value: optionValue,           // value technique (peut etre ephemere)
                label: optionLabel,           // texte visible - source de verite au replay
                selectedIndex: selectedIdx,
                optionCount: el.options.length,
                labelIsUnique: sameLabelCount === 1,
                selector: sel,
                url: location.href,
                inShadowDOM: sel.inShadowDOM,
                attributes: captureAttributes(el)
            });
            return;
        }
        saveEntry({
            action: 'input',
            timestamp: Date.now(),
            isTrusted: e.isTrusted === true,
            tag: el.tagName,
            value: sensitive ? '<redacted>' : (el.value || ''),
            sensitive: sensitive,
            selector: sel,
            url: location.href,
            inShadowDOM: sel.inShadowDOM,
            attributes: captureAttributes(el)
        });
    }, true);

    // -- CHANGE LISTENER dedie aux checkbox/radio.
    // Produit une action SEMANTIQUE 'check' ou 'uncheck' avec tout le
    // contexte necessaire au replay TS deterministe :
    //   - checked (bool) : etat cible reel apres la modification
    //   - matchCount : nb elements que le selecteur matche (permet au
    //     replay de savoir qu'il y a ambiguite meme sans .first())
    //   - accessibleName : aria-label ou label associe (fallback textuel)
    //   - parentLabel : texte du <li> parent (pattern TodoMVC-like)
    //   - parentSelector : css_short du parent li pour construire
    //     getByRole('listitem').filter({hasText: parentLabel}).locator(sel)
    //   - timestamp precis
    document.addEventListener('change', (e) => {
        let el = getRealTarget(e);
        const elType = (el && el.getAttribute && (el.getAttribute('type') || '').toLowerCase()) || '';
        if (elType !== 'checkbox' && elType !== 'radio') return;
        let sel = getBestSelector(el);
        // Contexte parent li (TodoMVC) pour desambiguisation
        let parentLabel = null;
        let parentLabelMatchCount = null;
        let parentCheckboxMatchCount = null;
        let parentSelector = null;
        let li = el.closest ? el.closest('li') : null;
        if (li) {
            const parentContext = getParentListItemContext(el, sel);
            parentLabel = parentContext.label;
            parentLabelMatchCount = parentContext.matchCount;
            parentCheckboxMatchCount = parentContext.checkboxMatchCount;
            parentSelector = getQuickSelector(li);
        }
        // Accessible name : aria-label, label associe ou nearest text
        let accessibleName = null;
        if (el.getAttribute && el.getAttribute('aria-label')) {
            accessibleName = el.getAttribute('aria-label');
        } else if (el.id) {
            try {
                let lbl = document.querySelector('label[for="' + attrValue(el.id) + '"]');
                if (lbl) accessibleName = (lbl.innerText || lbl.textContent || '').trim().substring(0, 100);
            } catch(_e) {}
        }
        saveEntry({
            action: el.checked ? 'check' : 'uncheck',
            timestamp: Date.now(),
            isTrusted: e.isTrusted === true,
            tag: el.tagName,
            checked: !!el.checked,        // etat cible boolean explicite
            value: el.checked ? 'true' : 'false',
            selector: sel,
            matchCount: sel.matchCount,   // exposition explicite (deja dans sel mais duplication utile)
            url: location.href,
            inShadowDOM: sel.inShadowDOM,
            parentLabel: parentLabel,
            parentLabelMatchCount: parentLabelMatchCount,
            parentCheckboxMatchCount: parentCheckboxMatchCount,
            parentSelector: parentSelector,
            accessibleName: accessibleName,
            attributes: captureAttributes(el)
        });
    }, true);

    // -- KEYDOWN LISTENER (fix R3 : separateurs input->cycle input suivant)
    // Sans capture Enter/Tab, une sequence "input A + Enter + input B" ou
    // Enter n'est PAS un event input, apparait comme "input A -> input B"
    // consecutifs cote listener -> dedup_log les consolide en 1 seul. Fix :
    // on emet un event 'keyboard' pour les touches qui changent l'etat
    // (Enter valide un formulaire/todo, Tab change focus, Escape ferme).
    // Le playwright_generator._emit_keyboard traduit en page.keyboard.press().
    const CAPTURED_KEYS = new Set(['Enter', 'Tab', 'Escape']);
    document.addEventListener('keydown', (e) => {
        if (!CAPTURED_KEYS.has(e.key)) return;
        let el = getRealTarget(e);
        let sel = getBestSelector(el);
        saveEntry({
            action: 'keyboard',
            timestamp: Date.now(),
            isTrusted: e.isTrusted === true,
            tag: el.tagName,
            value: e.key,
            selector: sel,
            url: location.href,
            inShadowDOM: sel.inShadowDOM,
            attributes: captureAttributes(el)
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
