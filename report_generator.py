"""
QA Explorer - Report Generator
================================
Genere un rapport HTML interactif depuis les resultats d'un run QA Explorer.
Piecharts (Chart.js), tableau des steps, anomalies, code Katalon.

JMer Consulting 2026
"""

import json
import os
import webbrowser
from datetime import datetime
from collections import Counter


def generate_report(clean_data, deduped_log, agent_result, scenario_name="",
                    scenario_url="", timestamp=None, output_dir=".",
                    js_errors=None, console_messages=None, network_log=None,
                    perf_before=None, perf_after=None, coverage_summary=None,
                    dom_mutations=None):
    """
    Genere un rapport HTML complet depuis les donnees du run.

    Args:
        clean_data: dict issu du cleanup IA (clean_steps_*.json)
        deduped_log: list des entrees dedupliquees (locator_dedup_*.json)
        agent_result: string du resultat final de l'agent
        scenario_name: nom du scenario
        scenario_url: URL de depart
        timestamp: timestamp du run (format YYYYMMDD_HHMMSS)
        output_dir: dossier de sortie
        js_errors: list de Runtime.exceptionThrown captures (V3 phase 1)
        console_messages: list de Console.messageAdded captures (V3 phase 1)

    Returns:
        filepath: chemin du rapport genere
    """
    js_errors = js_errors or []
    console_messages = console_messages or []
    network_log = network_log or []
    perf_before = perf_before or {}
    perf_after = perf_after or {}
    coverage_summary = coverage_summary or None
    dom_mutations = dom_mutations or {}
    if timestamp is None:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    # --- Statistiques selecteurs ---
    strategies = []
    unique_count = 0
    non_unique_count = 0
    total_selectors = 0

    for entry in deduped_log:
        if entry.get('action') in ('click', 'input', 'select'):
            selector = entry.get('selector', {})
            strategy = selector.get('strategy', 'unknown')
            strategies.append(strategy)
            total_selectors += 1
            if selector.get('unique'):
                unique_count += 1
            else:
                non_unique_count += 1

    strategy_counts = Counter(strategies)
    
    # --- Statistiques actions ---
    action_counts = Counter(e.get('action', 'unknown') for e in deduped_log)

    # --- Steps du parcours nettoye ---
    steps = clean_data.get('steps', [])
    anomalies = clean_data.get('anomalies', [])
    parcours_name = clean_data.get('parcours', scenario_name or 'Parcours QA')
    katalon_code = clean_data.get('katalon_code', '')
    total_steps = clean_data.get('total_steps', len(steps))

    # --- Determination succes/echec ---
    is_success = 'success' in str(agent_result).lower()

    # --- Couleurs pour les strategies ---
    strategy_colors = {
        'id': '#2ecc71',
        'data-testid': '#27ae60',
        'name': '#3498db',
        'aria-label': '#9b59b6',
        'placeholder': '#e67e22',
        'title': '#f39c12',
        'href': '#1abc9c',
        'parent-aria-label': '#8e44ad',
        'parent-data-testid': '#16a085',
        'parent-title': '#d35400',
        'label-xpath': '#2980b9',
        'xpath-text': '#c0392b',
        'css-short': '#e74c3c',
        'shadow': '#34495e',
        'unknown': '#95a5a6',
    }

    # Generer les labels/data/colors pour Chart.js
    chart_labels = list(strategy_counts.keys())
    chart_data = list(strategy_counts.values())
    chart_colors = [strategy_colors.get(s, '#95a5a6') for s in chart_labels]

    # Actions chart - couvre les 10 actions du Scenario Builder + les natives
    # browser-use (navigate, keyboard, upload, tabs, go_back...) + unknown.
    action_labels = list(action_counts.keys())
    action_data = list(action_counts.values())
    action_colors = {
        'click': '#3498db',
        'input': '#2ecc71',
        'scroll': '#e67e22',
        'select': '#9b59b6',
        'hover': '#1abc9c',
        'dom_unstable': '#e74c3c',
        'navigate': '#0ea5e9',
        'verify': '#a3e635',
        'wait': '#facc15',
        'screenshot': '#f472b6',
        'cookie': '#fbbf24',
        'keyboard': '#c084fc',
        'key_press': '#c084fc',
        'upload': '#38bdf8',
        'file_upload': '#38bdf8',
        'go_back': '#94a3b8',
        'go_forward': '#94a3b8',
        'reload': '#94a3b8',
        'open_tab': '#5eead4',
        'switch_tab': '#5eead4',
        'close_tab': '#5eead4',
        'unknown': '#64748b',
    }
    action_chart_colors = [action_colors.get(a, '#95a5a6') for a in action_labels]

    # --- Tier mapping pour affichage ---
    tier_map = {
        'id': 'Tier 1', 'data-testid': 'Tier 1', 'name': 'Tier 1',
        'aria-label': 'Tier 2', 'placeholder': 'Tier 2', 'title': 'Tier 2',
        'href': 'Tier 3',
        'parent-aria-label': 'Tier 4', 'parent-data-testid': 'Tier 4', 'parent-title': 'Tier 4',
        'label-xpath': 'Tier 5',
        'css-short': 'Tier 6', 'xpath-text': 'Tier 6',
        'shadow': 'Shadow DOM',
    }

    # --- Generation HTML ---
    # SECURITE : toutes les valeurs user-controlled (descriptions, selecteurs,
    # values, cleanup_reason, action, etc.) proviennent du DOM d'une page
    # externe. Elles PEUVENT contenir <script>, <img onerror=...>, ou tout
    # autre payload XSS. Le rapport HTML est ouvert dans le navigateur du
    # QA : sans echappement, une page testee peut executer du JS dans la
    # session du QA. Tout doit passer par _esc_text() (attribut) ou
    # _esc_attr() (attribut HTML).
    def _esc_text(v):
        """Echappe pour insertion dans un contenu HTML (<td>foo</td>)."""
        return (str(v) if v is not None else "").replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    def _esc_attr(v):
        """Echappe pour insertion dans un attribut HTML (title="...")."""
        return _esc_text(v).replace('"', '&quot;').replace("'", '&#39;')

    def _esc_class(v):
        """Echappe pour un nom de classe CSS - restreint aux alphanum + _-."""
        import re as _re
        return _re.sub(r'[^A-Za-z0-9_-]', '_', str(v or 'unknown'))

    # Le schema v2.0 (post-refactor Aout 2026) stocke selector comme dict
    # {value, strategy, unique, matchCount, ...}. Les anciens JSON avaient
    # selector comme string simple. On gere les deux via _extract_selector().
    def _extract_selector(s):
        sel = s.get('selector')
        if isinstance(sel, dict):
            return sel.get('value') or sel.get('playwrightSelector') or '?', sel
        return (sel or '?'), None

    steps_rows = ""
    for s in steps:
        action = (s.get('action') or '?').lower()
        desc = s.get('description') or ''
        sel_value, sel_dict = _extract_selector(s)
        sel_type = s.get('selectorType') or (sel_dict.get('strategy') if sel_dict else '?')
        # unique peut vivre au top-level (post-refactor) ou dans selector dict
        unique = s.get('unique')
        if unique is None and sel_dict:
            unique = sel_dict.get('unique')
        value = s.get('value') or ''
        included = s.get('included_in_replay', True)
        cleanup_reason = s.get('cleanup_reason') or ''
        sensitive = bool(s.get('sensitive'))

        # Badge unicite : ok/warn/muted si absent (les actions sans selecteur
        # comme navigate/wait n'ont pas de matchCount et ne doivent pas
        # apparaitre en NON-UNIQUE par defaut faux-negatif)
        if unique is True:
            unique_badge = '<span class="badge badge-ok">UNIQUE</span>'
        elif unique is False:
            unique_badge = '<span class="badge badge-warn">NON-UNIQUE</span>'
        else:
            unique_badge = '<span class="text-muted">&mdash;</span>'

        # Badge replay : INCLUS (vert) ou FILTRE (orange + tooltip raison)
        if included:
            replay_badge = '<span class="badge badge-ok">INCLUS</span>'
        else:
            reason_attr = _esc_attr(cleanup_reason or 'filtre par le nettoyage IA')
            replay_badge = f'<span class="badge badge-warn" title="{reason_attr}">FILTRE</span>'

        # Valeur : masquee si sensitive
        if sensitive:
            value_cell = '<span class="badge badge-sensitive" title="valeur masquee, sera injectee via var d\'env au replay">SENSITIVE</span>'
        elif value:
            value_cell = f'<code>{_esc_text(value)}</code>'
        else:
            value_cell = '<span class="text-muted">&mdash;</span>'

        # Ligne visuellement demarquee si skippee
        row_class = ' class="step-skipped"' if not included else ''
        reason_row = ''
        if not included and cleanup_reason:
            reason_row = f'<tr class="step-skipped-reason"><td></td><td colspan="7" class="cleanup-reason">Raison filtrage : {_esc_text(cleanup_reason)}</td></tr>'

        steps_rows += f"""
        <tr{row_class}>
            <td class="step-num">{_esc_text(s.get('step', '?'))}</td>
            <td><span class="action-tag action-{_esc_class(action)}">{_esc_text(action.upper())}</span></td>
            <td>{_esc_text(desc)}</td>
            <td><code class="selector">{_esc_text(sel_value)}</code></td>
            <td><span class="sel-type">{_esc_text(sel_type)}</span></td>
            <td>{unique_badge}</td>
            <td>{value_cell}</td>
            <td>{replay_badge}</td>
        </tr>{reason_row}"""

    anomalies_html = ""
    if anomalies:
        # Anomalies peuvent contenir texte issu du DOM (selecteur, message
        # d'erreur, description user-controlled) - escape obligatoire.
        anomalies_items = "".join(
            f'<li>{_esc_text(a if not isinstance(a, dict) else a.get("message", str(a)))}</li>'
            for a in anomalies
        )
        anomalies_html = f"""
        <div class="card anomalies">
            <h2>Anomalies detectees ({len(anomalies)})</h2>
            <ul>{anomalies_items}</ul>
        </div>"""

    katalon_html = ""
    if katalon_code:
        # Echapper le HTML dans le code
        escaped_code = (katalon_code
            .replace('&', '&amp;')
            .replace('<', '&lt;')
            .replace('>', '&gt;'))
        katalon_html = f"""
        <div class="card">
            <h2>Code Katalon Studio (Groovy)</h2>
            <pre class="code-block"><code>{escaped_code}</code></pre>
        </div>"""

    # --- DOM mutations (V3 phase 6) ---
    dom_html = ""
    total_mut = (dom_mutations.get("attribute_modified", 0)
                 + dom_mutations.get("child_node_inserted", 0)
                 + dom_mutations.get("child_node_removed", 0))
    if total_mut > 0:
        first_ms = dom_mutations.get("first_mutation_ms") or 0
        last_ms = dom_mutations.get("last_mutation_ms") or 0
        active_window_s = round((last_ms - first_ms) / 1000, 1) if last_ms > first_ms else 0
        rate = round(total_mut / active_window_s, 1) if active_window_s > 0 else 0
        # Page "stable" si le rythme moyen est < 5 mutations/s sur la fenetre active
        rate_color = "#3fb950" if rate < 5 else ("#d29922" if rate < 20 else "#f85149")
        dom_html = f"""
        <div class="card">
            <h2>DOM mutations</h2>
            <p style="color:#8b949e; font-size:13px;">
                Capture via CDP <code>DOM.attributeModified</code> + <code>childNodeInserted</code>
                + <code>childNodeRemoved</code>. Indique le rythme d'activite du DOM pendant le
                parcours - utile pour detecter les pages instables (animation infinie, polling
                long-poll, framework qui re-render en boucle).
            </p>
            <div class="kpis" style="margin-bottom:20px;">
                <div class="kpi">
                    <div class="kpi-value">{total_mut:,}</div>
                    <div class="kpi-label">Mutations totales</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value">{dom_mutations.get('attribute_modified', 0):,}</div>
                    <div class="kpi-label">Attributs modifies</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:#58a6ff">{dom_mutations.get('child_node_inserted', 0):,}</div>
                    <div class="kpi-label">Noeuds inseres</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:#d29922">{dom_mutations.get('child_node_removed', 0):,}</div>
                    <div class="kpi-label">Noeuds supprimes</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:{rate_color}">{rate}</div>
                    <div class="kpi-label">Mutations/s moyennes</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value">{active_window_s}s</div>
                    <div class="kpi-label">Fenetre active</div>
                </div>
            </div>
        </div>"""

    # --- Coverage (V3 phase 5) ---
    coverage_html = ""
    if coverage_summary and coverage_summary.get("total_size", 0) > 0:
        pct = coverage_summary.get("total_pct", 0)
        used_kb = coverage_summary.get("total_used", 0) // 1024
        total_kb = coverage_summary.get("total_size", 0) // 1024
        unused_kb = total_kb - used_kb
        pct_color = "#3fb950" if pct > 60 else ("#d29922" if pct > 30 else "#f85149")

        cov_rows = ""
        for s in coverage_summary.get("scripts", [])[:30]:
            url_short = (s.get("url") or "").rsplit("/", 1)[-1][:60] or "(inline)"
            size_kb = s.get("size", 0) // 1024
            used_kb_s = s.get("used", 0) // 1024
            spct = s.get("pct", 0)
            spct_color = "#3fb950" if spct > 60 else ("#d29922" if spct > 30 else "#f85149")
            cov_rows += f"""
            <tr>
              <td><code style="font-size:11px;">{(s.get('url') or '').replace('<','').replace('>','')[:100]}</code></td>
              <td>{size_kb} KB</td>
              <td>{used_kb_s} KB</td>
              <td style="color:{spct_color};font-weight:600;">{spct}%</td>
            </tr>"""

        coverage_html = f"""
        <div class="card">
            <h2>Coverage JS du parcours</h2>
            <p style="color:#8b949e; font-size:13px;">
                Capture via CDP <code>Profiler.startPreciseCoverage</code> activee AVANT toute
                navigation (sinon le bundle initial est marque comme non-execute, faussant les %).
                Indique quelle proportion du JS charge a ete reellement executee pendant le parcours.
            </p>
            <div class="kpis" style="margin-bottom:20px;">
                <div class="kpi">
                    <div class="kpi-value" style="color:{pct_color}">{pct}%</div>
                    <div class="kpi-label">Code execute</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value">{used_kb} KB</div>
                    <div class="kpi-label">JS execute</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:#d29922">{unused_kb} KB</div>
                    <div class="kpi-label">JS non execute (dead)</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value">{len(coverage_summary.get('scripts', []))}</div>
                    <div class="kpi-label">Scripts charges</div>
                </div>
            </div>
            <h3>Top 30 scripts par taille</h3>
            <div class="table-wrap"><table><thead><tr><th>URL</th><th>Taille</th><th>Execute</th><th>%</th></tr></thead><tbody>{cov_rows}</tbody></table></div>
        </div>"""

    # --- Performance metrics (V3 phase 4) ---
    perf_html = ""
    if perf_after:
        heap_before_mb = round(perf_before.get("JSHeapUsedSize", 0) / 1024 / 1024, 2)
        heap_after_mb = round(perf_after.get("JSHeapUsedSize", 0) / 1024 / 1024, 2)
        heap_delta = round(heap_after_mb - heap_before_mb, 2)
        nodes_before = int(perf_before.get("Nodes", 0))
        nodes_after = int(perf_after.get("Nodes", 0))
        layout_count = int(perf_after.get("LayoutCount", 0)) - int(perf_before.get("LayoutCount", 0))
        recalc_count = int(perf_after.get("RecalcStyleCount", 0)) - int(perf_before.get("RecalcStyleCount", 0))
        script_duration = round(perf_after.get("ScriptDuration", 0) - perf_before.get("ScriptDuration", 0), 3)
        layout_duration = round(perf_after.get("LayoutDuration", 0) - perf_before.get("LayoutDuration", 0), 3)

        # Couleur selon seuils
        heap_color = "#f85149" if heap_delta > 50 else ("#d29922" if heap_delta > 10 else "#3fb950")
        layout_color = "#f85149" if layout_count > 100 else ("#d29922" if layout_count > 30 else "#3fb950")

        perf_html = f"""
        <div class="card">
            <h2>Performance metrics</h2>
            <p style="color:#8b949e; font-size:13px;">
                Snapshot via CDP <code>Performance.getMetrics</code> avant et apres le parcours.
                Le delta indique la croissance du runtime pendant l'execution.
            </p>
            <div class="kpis" style="margin-bottom:20px;">
                <div class="kpi">
                    <div class="kpi-value" style="color:{heap_color}">{heap_delta:+} MB</div>
                    <div class="kpi-label">Heap delta</div>
                    <div style="font-size:10px;color:#6e7681;margin-top:4px">
                        {heap_before_mb} MB -&gt; {heap_after_mb} MB
                    </div>
                </div>
                <div class="kpi">
                    <div class="kpi-value">{nodes_after:,}</div>
                    <div class="kpi-label">DOM nodes (final)</div>
                    <div style="font-size:10px;color:#6e7681;margin-top:4px">
                        {nodes_before:,} -&gt; {nodes_after:,}
                    </div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:{layout_color}">{layout_count}</div>
                    <div class="kpi-label">Layouts forces</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value">{recalc_count}</div>
                    <div class="kpi-label">Recalc styles</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value">{script_duration}s</div>
                    <div class="kpi-label">JS execute</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value">{layout_duration}s</div>
                    <div class="kpi-label">Layout time</div>
                </div>
            </div>
        </div>"""

    # --- Network audit (V3 phase 2) ---
    def _esc_html(s):
        return (str(s or "").replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

    network_html = ""
    if network_log:
        api_count = sum(1 for r in network_log if r.get("type") in ("Fetch", "XHR"))
        fail_count = sum(1 for r in network_log if (r.get("status") or 0) >= 400)
        # Domaines tiers (different de l'origine principale)
        from urllib.parse import urlparse
        origin_domain = ""
        for r in network_log:
            if r.get("type") == "Document" and r.get("url"):
                try:
                    origin_domain = urlparse(r["url"]).netloc
                    break
                except Exception:
                    pass
        third_parties = {}
        for r in network_log:
            try:
                d = urlparse(r.get("url", "")).netloc
                if d and d != origin_domain:
                    third_parties[d] = third_parties.get(d, 0) + 1
            except Exception:
                pass
        third_party_rows = "".join(
            f"<tr><td><code>{_esc_html(d)}</code></td><td>{c}</td></tr>"
            for d, c in sorted(third_parties.items(), key=lambda x: -x[1])[:30]
        )

        net_rows = ""
        for r in network_log[:100]:
            status = r.get("status")
            status_cls = "status-success" if status and status < 400 else ("status-fail" if status else "status-warning")
            method_cls = "status-success" if r.get("method") == "GET" else "status-warning"
            net_rows += f"""
            <tr>
              <td><span class="status-badge {method_cls}">{_esc_html(r.get('method', '?'))}</span></td>
              <td><span class="status-badge {status_cls}">{status if status else '...'}</span></td>
              <td><code style="font-size:11px;">{_esc_html((r.get('url') or '')[:100])}</code></td>
              <td><small>{_esc_html(r.get('type', ''))}</small></td>
              <td><small>{r.get('duration_ms', '?')} ms</small></td>
            </tr>"""

        network_html = f"""
        <div class="card">
            <h2>Network audit</h2>
            <p style="color:#8b949e; font-size:13px;">
                Capture <code>Network.requestWillBeSent</code> / <code>Network.responseReceived</code>
                / <code>Network.loadingFinished</code> sur la page testee. Filtre {','.join(['Fetch','XHR','Document','WebSocket','EventSource'])}
                (les assets images/css/fonts/scripts sont ignores). Headers <code>Cookie</code> et
                <code>Authorization</code> sont redactes pour confidentialite.
            </p>
            <div class="kpis" style="margin-bottom:20px;">
                <div class="kpi">
                    <div class="kpi-value">{len(network_log)}</div>
                    <div class="kpi-label">Requetes capturees</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:#58a6ff">{api_count}</div>
                    <div class="kpi-label">API calls (Fetch/XHR)</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:{'#f85149' if fail_count else '#3fb950'}">{fail_count}</div>
                    <div class="kpi-label">Erreurs >=400</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:#d29922">{len(third_parties)}</div>
                    <div class="kpi-label">Domaines tiers</div>
                </div>
            </div>
            {f'<h3>Domaines tiers contactes (audit RGPD/privacy)</h3><div class="table-wrap"><table><thead><tr><th>Domaine</th><th>Requetes</th></tr></thead><tbody>{third_party_rows}</tbody></table></div>' if third_party_rows else ''}
            <h3>Requetes detaillees ({min(len(network_log), 100)}/{len(network_log)})</h3>
            <div class="table-wrap"><table><thead><tr><th>Method</th><th>Status</th><th>URL</th><th>Type</th><th>Duree</th></tr></thead><tbody>{net_rows}</tbody></table></div>
        </div>"""

    # --- Observabilite (V3 phase 1) ---
    observability_html = ""
    js_err_count = len(js_errors)
    console_err_count = sum(1 for m in console_messages if m.get("level") == "error")
    console_warn_count = sum(1 for m in console_messages if m.get("level") == "warning")
    if js_err_count or console_err_count or console_warn_count:
        def _esc(s):
            return (str(s or "").replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))

        js_rows = ""
        for e in js_errors[:50]:
            stack_top = ""
            if e.get("stackTrace"):
                fr = e["stackTrace"][0]
                stack_top = f"{_esc(fr.get('functionName') or '?')} at {_esc(fr.get('url', '')[:80])}:{fr.get('lineNumber', '?')}"
            js_rows += f"""
            <tr>
              <td><span class="status-badge status-fail">{_esc(e.get('text', '')[:120])}</span></td>
              <td><code>{_esc(e.get('exception', '')[:200])}</code></td>
              <td><small>{stack_top}</small></td>
            </tr>"""

        console_rows = ""
        for m in console_messages[:50]:
            level = m.get("level") or "log"
            badge_cls = "status-fail" if level == "error" else ("status-warning" if level == "warning" else "status-success")
            console_rows += f"""
            <tr>
              <td><span class="status-badge {badge_cls}">{_esc(level)}</span></td>
              <td>{_esc(m.get('text', '')[:200])}</td>
              <td><small>{_esc(m.get('url', '')[:60])}{':' + str(m['line']) if m.get('line') else ''}</small></td>
            </tr>"""

        observability_html = f"""
        <div class="card">
            <h2>Observabilite : Console + JS Errors</h2>
            <p style="color:#8b949e; font-size:13px;">
                Capture via CDP <code>Runtime.exceptionThrown</code> et <code>Console.messageAdded</code>.
                Ces events sont normalement invisibles a l'utilisateur QA mais signalent des bugs JS reels.
            </p>
            <div class="kpis" style="margin-bottom:20px;">
                <div class="kpi">
                    <div class="kpi-value" style="color:{'#f85149' if js_err_count else '#3fb950'}">{js_err_count}</div>
                    <div class="kpi-label">JS Errors silencieux</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:{'#f85149' if console_err_count else '#3fb950'}">{console_err_count}</div>
                    <div class="kpi-label">Console errors</div>
                </div>
                <div class="kpi">
                    <div class="kpi-value" style="color:{'#d29922' if console_warn_count else '#3fb950'}">{console_warn_count}</div>
                    <div class="kpi-label">Console warnings</div>
                </div>
            </div>
            {f'<h3 style="margin-top:0">JS Exceptions ({js_err_count})</h3><div class="table-wrap"><table><thead><tr><th>Texte</th><th>Exception</th><th>Stack top</th></tr></thead><tbody>{js_rows}</tbody></table></div>' if js_rows else ''}
            {f'<h3>Console messages ({len(console_messages)})</h3><div class="table-wrap"><table><thead><tr><th>Niveau</th><th>Message</th><th>Source</th></tr></thead><tbody>{console_rows}</tbody></table></div>' if console_rows else ''}
        </div>"""

    # --- Deduped log details ---
    log_rows = ""
    for i, entry in enumerate(deduped_log):
        action = entry.get('action', '?')
        selector = entry.get('selector', {})
        strategy = selector.get('strategy', '?')
        sel_value = selector.get('value', '?')
        unique = selector.get('unique', False)
        match_count = selector.get('matchCount', '?')
        text = entry.get('text', entry.get('value', ''))
        if text:
            text = str(text)[:60]
        url = entry.get('url', '')
        if len(url) > 80:
            url = '...' + url[-60:]
        in_shadow = entry.get('inShadowDOM', False)
        tier = tier_map.get(strategy, '?')

        unique_badge = (
            '<span class="badge badge-ok">OK</span>' if unique 
            else '<span class="badge badge-warn">!!</span>'
        )
        shadow_badge = ' <span class="badge badge-shadow">SHADOW</span>' if in_shadow else ''

        # SECURITE : selector, text, strategy sont user-controlled (viennent
        # du DOM d'une page externe). Toutes ces valeurs passent par _esc_text
        # / _esc_class pour empecher tout XSS lorsqu'un site testee tenterait
        # d'injecter <script> ou <img onerror=...>.
        log_rows += f"""
        <tr>
            <td class="step-num">{i+1}</td>
            <td><span class="action-tag action-{_esc_class(action)}">{_esc_text(action.upper())}</span>{shadow_badge}</td>
            <td><span class="tier-badge tier-{_esc_class(tier.lower().replace(' ', '-'))}">{_esc_text(tier)}</span> {_esc_text(strategy)}</td>
            <td><code class="selector">{_esc_text(sel_value)}</code></td>
            <td>{unique_badge} ({_esc_text(match_count)})</td>
            <td>{_esc_text(text)}</td>
        </tr>"""

    # --- Status banner ---
    status_class = "success" if is_success else "failure"
    status_text = "SUCCESS" if is_success else "FAIL"
    status_icon = "&#10004;" if is_success else "&#10008;"

    html = f"""<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>QA Explorer Report — {parcours_name}</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: 'Segoe UI', system-ui, -apple-system, sans-serif;
            background: #0f172a;
            color: #e2e8f0;
            line-height: 1.6;
        }}

        /* Header */
        .header {{
            background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%);
            border-bottom: 2px solid #334155;
            padding: 24px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .header h1 {{
            font-size: 22px;
            font-weight: 700;
            color: #f8fafc;
        }}
        .header .subtitle {{
            color: #94a3b8;
            font-size: 13px;
            margin-top: 4px;
        }}
        .header .brand {{
            color: #64748b;
            font-size: 12px;
            text-align: right;
        }}
        .header .brand strong {{ color: #38bdf8; }}

        /* Status banner */
        .status-banner {{
            padding: 16px 40px;
            font-size: 16px;
            font-weight: 700;
            display: flex;
            align-items: center;
            gap: 12px;
        }}
        .status-banner.success {{
            background: linear-gradient(90deg, #064e3b, #0f172a);
            color: #34d399;
            border-bottom: 2px solid #059669;
        }}
        .status-banner.failure {{
            background: linear-gradient(90deg, #7f1d1d, #0f172a);
            color: #f87171;
            border-bottom: 2px solid #dc2626;
        }}
        .status-icon {{ font-size: 24px; }}

        /* Main */
        .main {{ padding: 24px 40px; max-width: 1400px; margin: 0 auto; }}

        /* KPI row */
        .kpi-row {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 24px;
        }}
        .kpi {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 20px;
            text-align: center;
        }}
        .kpi .value {{
            font-size: 32px;
            font-weight: 800;
            color: #f8fafc;
        }}
        .kpi .label {{
            font-size: 12px;
            color: #94a3b8;
            text-transform: uppercase;
            letter-spacing: 1px;
            margin-top: 4px;
        }}
        .kpi.highlight .value {{ color: #38bdf8; }}
        .kpi.warn .value {{ color: #fbbf24; }}
        .kpi.danger .value {{ color: #f87171; }}

        /* Charts row */
        .charts-row {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
            margin-bottom: 24px;
        }}

        /* Card */
        .card {{
            background: #1e293b;
            border: 1px solid #334155;
            border-radius: 10px;
            padding: 24px;
            margin-bottom: 20px;
        }}
        .card h2 {{
            font-size: 16px;
            font-weight: 700;
            color: #f8fafc;
            margin-bottom: 16px;
            padding-bottom: 8px;
            border-bottom: 1px solid #334155;
        }}

        /* Anomalies */
        .anomalies {{ border-left: 4px solid #f59e0b; }}
        .anomalies h2 {{ color: #fbbf24; }}
        .anomalies ul {{ list-style: none; padding: 0; }}
        .anomalies li {{
            padding: 8px 12px;
            margin: 4px 0;
            background: #292524;
            border-radius: 6px;
            font-size: 13px;
            color: #fde68a;
        }}
        .anomalies li::before {{ content: "\\26A0  "; }}

        /* Table */
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        th {{
            background: #334155;
            color: #94a3b8;
            font-size: 11px;
            font-weight: 600;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            padding: 10px 12px;
            text-align: left;
        }}
        td {{
            padding: 10px 12px;
            border-bottom: 1px solid #1e293b;
        }}
        tr:hover td {{ background: #1a2332; }}
        .step-num {{
            font-weight: 700;
            color: #38bdf8;
            text-align: center;
            width: 40px;
        }}

        /* Badges */
        .badge {{
            display: inline-block;
            padding: 2px 8px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 700;
        }}
        .badge-ok {{ background: #064e3b; color: #34d399; }}
        .badge-warn {{ background: #7f1d1d; color: #fca5a5; }}
        .badge-shadow {{ background: #312e81; color: #a5b4fc; }}

        /* Action tags */
        .action-tag {{
            display: inline-block;
            padding: 2px 10px;
            border-radius: 4px;
            font-size: 11px;
            font-weight: 700;
            text-transform: uppercase;
        }}
        .action-click {{ background: #1e3a5f; color: #7dd3fc; }}
        .action-input {{ background: #14532d; color: #86efac; }}
        .action-scroll {{ background: #431407; color: #fdba74; }}
        .action-select {{ background: #3b0764; color: #d8b4fe; }}
        .action-hover {{ background: #134e4a; color: #5eead4; }}
        .action-verify {{ background: #365314; color: #bef264; }}
        .action-navigate {{ background: #0c4a6e; color: #7dd3fc; }}
        .action-wait {{ background: #713f12; color: #fde68a; }}
        .action-screenshot {{ background: #831843; color: #f9a8d4; }}
        .action-cookie {{ background: #78350f; color: #fcd34d; }}
        .action-keyboard {{ background: #4c1d95; color: #c4b5fd; }}
        .action-key_press {{ background: #4c1d95; color: #c4b5fd; }}
        .action-upload {{ background: #075985; color: #7dd3fc; }}
        .action-file_upload {{ background: #075985; color: #7dd3fc; }}
        .action-go_back {{ background: #1f2937; color: #cbd5e1; }}
        .action-go_forward {{ background: #1f2937; color: #cbd5e1; }}
        .action-reload {{ background: #1f2937; color: #cbd5e1; }}
        .action-open_tab {{ background: #134e4a; color: #5eead4; }}
        .action-switch_tab {{ background: #134e4a; color: #5eead4; }}
        .action-close_tab {{ background: #134e4a; color: #5eead4; }}
        .action-unknown {{ background: #292524; color: #a8a29e; }}

        /* Steps filtres par le nettoyage IA : conserves pour tracabilite */
        .step-skipped td {{ opacity: 0.55; }}
        .step-skipped-reason td.cleanup-reason {{
            padding: 4px 12px 12px 12px;
            font-size: 12px;
            color: #fbbf24;
            font-style: italic;
            border-bottom: 1px solid #1e293b;
        }}
        .badge-sensitive {{ background: #0e7490; color: #a5f3fc; }}

        /* Selector type */
        .sel-type {{
            background: #334155;
            color: #94a3b8;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 11px;
        }}

        /* Tier badges */
        .tier-badge {{
            display: inline-block;
            padding: 1px 6px;
            border-radius: 3px;
            font-size: 10px;
            font-weight: 700;
            margin-right: 4px;
        }}
        .tier-tier-1 {{ background: #064e3b; color: #34d399; }}
        .tier-tier-2 {{ background: #1e3a5f; color: #7dd3fc; }}
        .tier-tier-3 {{ background: #134e4a; color: #5eead4; }}
        .tier-tier-4 {{ background: #3b0764; color: #d8b4fe; }}
        .tier-tier-5 {{ background: #431407; color: #fdba74; }}
        .tier-tier-6 {{ background: #7f1d1d; color: #fca5a5; }}
        .tier-shadow-dom {{ background: #312e81; color: #a5b4fc; }}

        /* Code block */
        .code-block {{
            background: #0f172a;
            border: 1px solid #334155;
            border-radius: 8px;
            padding: 20px;
            overflow-x: auto;
            font-family: 'Cascadia Code', 'Fira Code', 'Consolas', monospace;
            font-size: 12px;
            line-height: 1.8;
            color: #e2e8f0;
            max-height: 500px;
            overflow-y: auto;
        }}

        code.selector {{
            background: #334155;
            padding: 2px 6px;
            border-radius: 3px;
            font-size: 12px;
            color: #7dd3fc;
            word-break: break-all;
        }}

        .text-muted {{ color: #64748b; }}

        /* Chart containers */
        .chart-container {{
            position: relative;
            height: 250px;
        }}

        /* Footer */
        .footer {{
            text-align: center;
            padding: 20px;
            color: #475569;
            font-size: 12px;
            border-top: 1px solid #1e293b;
            margin-top: 40px;
        }}

        /* Responsive */
        @media (max-width: 768px) {{
            .header {{ padding: 16px 20px; flex-direction: column; gap: 8px; }}
            .main {{ padding: 16px 20px; }}
            .kpi-row {{ grid-template-columns: repeat(2, 1fr); }}
            .charts-row {{ grid-template-columns: 1fr; }}
        }}
    </style>
</head>
<body>

    <!-- Header -->
    <div class="header">
        <div>
            <h1>{parcours_name}</h1>
            <div class="subtitle">{scenario_url} &mdash; {timestamp}</div>
        </div>
        <div class="brand">
            <strong>QA Explorer</strong><br>
            JMer Consulting
        </div>
    </div>

    <!-- Status -->
    <div class="status-banner {status_class}">
        <span class="status-icon">{status_icon}</span>
        {status_text} &mdash; {agent_result[:120] if agent_result else 'N/A'}
    </div>

    <div class="main">

        <!-- KPIs -->
        <div class="kpi-row">
            <div class="kpi highlight">
                <div class="value">{total_steps}</div>
                <div class="label">Steps nettoyes</div>
            </div>
            <div class="kpi">
                <div class="value">{len(deduped_log)}</div>
                <div class="label">Actions capturees</div>
            </div>
            <div class="kpi {'warn' if non_unique_count > 0 else ''}">
                <div class="value">{unique_count}/{total_selectors}</div>
                <div class="label">Selecteurs uniques</div>
            </div>
            <div class="kpi {'danger' if len(anomalies) > 0 else ''}">
                <div class="value">{len(anomalies)}</div>
                <div class="label">Anomalies</div>
            </div>
            <div class="kpi">
                <div class="value">{len(strategy_counts)}</div>
                <div class="label">Strategies utilisees</div>
            </div>
        </div>

        <!-- Charts -->
        <div class="charts-row">
            <div class="card">
                <h2>Strategies de selection</h2>
                <div class="chart-container">
                    <canvas id="strategyChart"></canvas>
                </div>
            </div>
            <div class="card">
                <h2>Fiabilite des selecteurs</h2>
                <div class="chart-container">
                    <canvas id="uniqueChart"></canvas>
                </div>
            </div>
            <div class="card">
                <h2>Types d'actions</h2>
                <div class="chart-container">
                    <canvas id="actionChart"></canvas>
                </div>
            </div>
        </div>

        <!-- Anomalies -->
        {anomalies_html}

        <!-- Parcours nettoye -->
        <div class="card">
            <h2>Parcours nettoye ({total_steps} steps)</h2>
            <p style="color:#8b949e;font-size:13px;margin-bottom:16px">
                Les steps marques <span class="badge badge-warn">FILTRE</span> sont
                conserves pour la tracabilite mais NE SONT PAS rejoues par
                <code>test_playwright.spec.ts</code>. La raison du filtrage est
                affichee sous chaque ligne concernee.
            </p>
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Action</th>
                        <th>Description</th>
                        <th>Selecteur</th>
                        <th>Type</th>
                        <th>Unique</th>
                        <th>Valeur</th>
                        <th>Rejoue</th>
                    </tr>
                </thead>
                <tbody>
                    {steps_rows}
                </tbody>
            </table>
        </div>

        <!-- Log brut -->
        <div class="card">
            <h2>Locateurs captures (log deduplique — {len(deduped_log)} entrees)</h2>
            <table>
                <thead>
                    <tr>
                        <th>#</th>
                        <th>Action</th>
                        <th>Strategie</th>
                        <th>Selecteur</th>
                        <th>Unique</th>
                        <th>Texte / Valeur</th>
                    </tr>
                </thead>
                <tbody>
                    {log_rows}
                </tbody>
            </table>
        </div>

        <!-- Performance metrics (V3 phase 4) -->
        {perf_html}

        <!-- DOM mutations (V3 phase 6) -->
        {dom_html}

        <!-- Coverage JS (V3 phase 5) -->
        {coverage_html}

        <!-- Network audit (V3 phase 2) -->
        {network_html}

        <!-- Observabilite Console + JS (V3 phase 1) -->
        {observability_html}

        <!-- Code Katalon -->
        {katalon_html}

    </div>

    <!-- Footer -->
    <div class="footer">
        QA Explorer Report &mdash; Genere automatiquement par QA Explorer &mdash; JMer Consulting {datetime.now().year}
    </div>

    <script>
        // --- Pie Chart : Strategies ---
        new Chart(document.getElementById('strategyChart'), {{
            type: 'doughnut',
            data: {{
                labels: {json.dumps(chart_labels)},
                datasets: [{{
                    data: {json.dumps(chart_data)},
                    backgroundColor: {json.dumps(chart_colors)},
                    borderColor: '#0f172a',
                    borderWidth: 2
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        position: 'right',
                        labels: {{ color: '#94a3b8', font: {{ size: 11 }}, padding: 8 }}
                    }}
                }}
            }}
        }});

        // --- Pie Chart : Unique vs Non-unique ---
        new Chart(document.getElementById('uniqueChart'), {{
            type: 'doughnut',
            data: {{
                labels: ['Uniques', 'Non-uniques'],
                datasets: [{{
                    data: [{unique_count}, {non_unique_count}],
                    backgroundColor: ['#059669', '#dc2626'],
                    borderColor: '#0f172a',
                    borderWidth: 2
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        position: 'right',
                        labels: {{ color: '#94a3b8', font: {{ size: 11 }}, padding: 8 }}
                    }}
                }}
            }}
        }});

        // --- Pie Chart : Actions ---
        new Chart(document.getElementById('actionChart'), {{
            type: 'doughnut',
            data: {{
                labels: {json.dumps(action_labels)},
                datasets: [{{
                    data: {json.dumps(action_data)},
                    backgroundColor: {json.dumps(action_chart_colors)},
                    borderColor: '#0f172a',
                    borderWidth: 2
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                plugins: {{
                    legend: {{
                        position: 'right',
                        labels: {{ color: '#94a3b8', font: {{ size: 11 }}, padding: 8 }}
                    }}
                }}
            }}
        }});
    </script>

</body>
</html>"""

    # Sauvegarder et ouvrir
    filepath = os.path.join(output_dir, f"qa_report_{timestamp}.html")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)

    return filepath


def open_report(filepath):
    """Ouvre le rapport dans le navigateur par defaut"""
    webbrowser.open(f"file:///{os.path.abspath(filepath)}")


if __name__ == "__main__":
    """Test standalone avec des donnees fictives"""
    import sys

    if len(sys.argv) > 1:
        # Charger depuis fichiers JSON existants
        clean_file = sys.argv[1]
        dedup_file = sys.argv[2] if len(sys.argv) > 2 else None

        with open(clean_file, "r", encoding="utf-8") as f:
            clean_data = json.load(f)

        deduped_log = []
        if dedup_file:
            with open(dedup_file, "r", encoding="utf-8") as f:
                deduped_log = json.load(f)

        filepath = generate_report(
            clean_data=clean_data,
            deduped_log=deduped_log,
            agent_result="SUCCESS (charge depuis fichier)",
            scenario_name=clean_data.get('parcours', 'Test'),
        )
        print(f"Rapport genere : {filepath}")
        open_report(filepath)
    else:
        print("Usage: python report_generator.py clean_steps.json [locator_dedup.json]")
