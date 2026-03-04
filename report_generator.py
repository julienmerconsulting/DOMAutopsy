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
                    scenario_url="", timestamp=None, output_dir="."):
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
    
    Returns:
        filepath: chemin du rapport genere
    """
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

    # Actions chart
    action_labels = list(action_counts.keys())
    action_data = list(action_counts.values())
    action_colors = {
        'click': '#3498db',
        'input': '#2ecc71',
        'scroll': '#e67e22',
        'select': '#9b59b6',
        'hover': '#1abc9c',
        'dom_unstable': '#e74c3c',
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
    steps_rows = ""
    for s in steps:
        action = s.get('action', '?')
        desc = s.get('description', '?')
        selector = s.get('selector', '?')
        sel_type = s.get('selectorType', '?')
        unique = s.get('unique', False)
        value = s.get('value', '')

        unique_badge = (
            '<span class="badge badge-ok">UNIQUE</span>' if unique 
            else '<span class="badge badge-warn">NON-UNIQUE</span>'
        )
        value_cell = f'<code>{value}</code>' if value else '<span class="text-muted">—</span>'

        steps_rows += f"""
        <tr>
            <td class="step-num">{s.get('step', '?')}</td>
            <td><span class="action-tag action-{action}">{action.upper()}</span></td>
            <td>{desc}</td>
            <td><code class="selector">{selector}</code></td>
            <td><span class="sel-type">{sel_type}</span></td>
            <td>{unique_badge}</td>
            <td>{value_cell}</td>
        </tr>"""

    anomalies_html = ""
    if anomalies:
        anomalies_items = "".join(f'<li>{a}</li>' for a in anomalies)
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

        log_rows += f"""
        <tr>
            <td class="step-num">{i+1}</td>
            <td><span class="action-tag action-{action}">{action.upper()}</span>{shadow_badge}</td>
            <td><span class="tier-badge tier-{tier.lower().replace(' ', '-')}">{tier}</span> {strategy}</td>
            <td><code class="selector">{sel_value}</code></td>
            <td>{unique_badge} ({match_count})</td>
            <td>{text}</td>
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
