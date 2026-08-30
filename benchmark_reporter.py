"""
DOMAutopsy - Rapport benchmark BU_Bench_V1 (HTML + JSON local).
==================================================================
Produit un rapport local dans `.bu_bench_runs/<ts>_bench/report.html`
et `report.json` a partir du summary retourne par benchmark_runner.

ATTENTION : le rapport local peut contenir les instructions
dechiffrees (`confirmed_task`) puisqu'il documente ce qui a ete
execute. Il est ecrit dans un dossier `.bu_bench_runs/` GITIGNORE.
Ne JAMAIS publier ces fichiers sur GitHub ou en CI.
"""
from __future__ import annotations

import html
import json
from datetime import datetime
from pathlib import Path


def _esc(s):
    return html.escape(str(s) if s is not None else "")


def write_reports(summary: dict, run_root: Path, task_texts: dict[str, str] | None = None) -> tuple[Path, Path]:
    """Ecrit report.json et report.html. Retourne (json_path, html_path).

    task_texts : {task_id: confirmed_task} - optionnel, inclus dans le
    rapport HTML local (jamais dans le JSON commit). Passe UNIQUEMENT
    quand on veut la trace complete (rapport local, gitignore).
    """
    run_root.mkdir(parents=True, exist_ok=True)
    json_path = run_root / "report.json"
    html_path = run_root / "report.html"

    # JSON : count et status, PAS le texte des taches (pour eviter
    # tout risque de fuite si le JSON etait commit par erreur)
    json_data = {
        "started_at": summary.get("started_at"),
        "ended_at": summary.get("ended_at"),
        "duration_s": summary.get("duration_s"),
        "workers_max": summary.get("workers_max"),
        "replays_per_task": summary.get("replays_per_task"),
        "capture_timeout_s": summary.get("capture_timeout_s"),
        "tasks_total": summary.get("tasks_total"),
        "captures_summary": _summarize_captures(summary.get("captures", [])),
        "replays_summary": _summarize_replays(summary.get("replays", {})),
        "captures": summary.get("captures", []),
        "replays_by_task": summary.get("replays", {}),
    }
    json_path.write_text(json.dumps(json_data, indent=2, ensure_ascii=False), encoding="utf-8")

    # HTML : plus riche, inclut les tasks texts si fournis
    html_content = _build_html(summary, task_texts or {})
    html_path.write_text(html_content, encoding="utf-8")
    return json_path, html_path


def _summarize_captures(captures: list[dict]) -> dict:
    from collections import Counter
    c = Counter(r.get("capture_result") for r in captures)
    return {
        "success": c.get("success", 0),
        "timeout": c.get("timeout", 0),
        "infrastructure_error": c.get("infrastructure_error", 0),
        "total": len(captures),
        "agent_success": sum(1 for r in captures if r.get("agent_status") == "success"),
        "agent_fail": sum(1 for r in captures if r.get("agent_status") == "fail"),
    }


def _summarize_replays(replays_by_task: dict[str, list[dict]]) -> dict:
    total = sum(len(v) for v in replays_by_task.values())
    from collections import Counter
    c = Counter()
    for lst in replays_by_task.values():
        for r in lst:
            c[r.get("status")] += 1
    return {
        "total_replays": total,
        "pass": c.get("pass", 0),
        "fail": c.get("fail", 0),
        "timeout": c.get("timeout", 0),
        "tasks_with_all_pass": sum(
            1 for lst in replays_by_task.values()
            if lst and all(r.get("status") == "pass" for r in lst)
        ),
        "tasks_covered": len(replays_by_task),
    }


def _build_html(summary: dict, task_texts: dict[str, str]) -> str:
    cap_sum = _summarize_captures(summary.get("captures", []))
    rep_sum = _summarize_replays(summary.get("replays", {}))

    cap_rows = []
    for c in summary.get("captures", []):
        tid = c.get("task_id", "?")
        text = task_texts.get(tid, "")
        cap_rows.append(f"""
        <tr>
          <td><code>{_esc(tid)}</code></td>
          <td><span class="badge {c.get('capture_result')}">{_esc(c.get('capture_result'))}</span></td>
          <td>{_esc(c.get('agent_status'))}</td>
          <td>{_esc(c.get('duration_s'))}s</td>
          <td>{_esc(c.get('clean_steps_total'))} / {_esc(c.get('clean_steps_included'))} inc / {_esc(c.get('clean_steps_filtered'))} filt</td>
          <td>{_esc(c.get('raw_count'))}</td>
          <td><small>{_esc(text[:200])}{'...' if len(text)>200 else ''}</small></td>
        </tr>""")

    rep_rows = []
    for tid, lst in summary.get("replays", {}).items():
        statuses = " ".join(f'<span class="badge {r["status"]}">{_esc(r["status"])}</span>' for r in lst)
        avg_ms = round(sum(r.get("duration_s", 0) for r in lst) / max(len(lst), 1), 1)
        rep_rows.append(f"""
        <tr>
          <td><code>{_esc(tid)}</code></td>
          <td>{statuses}</td>
          <td>{len(lst)}</td>
          <td>{avg_ms}s (avg)</td>
        </tr>""")

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Bench BU_Bench_V1</title>
<style>
body{{background:#0e1116;color:#e6edf3;font-family:-apple-system,sans-serif;padding:24px;margin:0}}
h1{{margin:0 0 8px}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin:16px 0 24px}}
.kpi{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;text-align:center}}
.kpi-val{{font-size:28px;font-weight:700}}
.kpi-lbl{{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-top:4px}}
table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden;margin-bottom:24px}}
th{{background:#0d1117;text-align:left;padding:10px 12px;font-size:12px;color:#8b949e;text-transform:uppercase;letter-spacing:1px}}
td{{padding:10px 12px;border-top:1px solid #30363d;font-size:13px;vertical-align:top}}
code{{background:#0d1117;padding:2px 6px;border-radius:4px;color:#58a6ff}}
.badge{{padding:2px 8px;border-radius:10px;font-size:11px;font-weight:700}}
.badge.success,.badge.pass{{background:#238636;color:#fff}}
.badge.fail{{background:#da3633;color:#fff}}
.badge.timeout{{background:#d29922;color:#000}}
.badge.infrastructure_error{{background:#8250df;color:#fff}}
.notice{{background:#7f1d1d;padding:12px 16px;border-radius:8px;margin-bottom:16px;color:#fecaca}}
</style></head><body>
<h1>Benchmark BU_Bench_V1 - rapport local</h1>
<p class="notice">
  <strong>PRIVE</strong> : ce rapport contient les instructions dechiffrees
  du corpus BU_Bench_V1 et NE DOIT PAS etre publie (GitHub, docs, CI).
  Dossier .bu_bench_runs/ gitignore.
</p>
<div class="kpis">
  <div class="kpi"><div class="kpi-val">{cap_sum['total']}</div><div class="kpi-lbl">Taches executees</div></div>
  <div class="kpi"><div class="kpi-val" style="color:#3fb950">{cap_sum['success']}</div><div class="kpi-lbl">Captures OK</div></div>
  <div class="kpi"><div class="kpi-val" style="color:#f85149">{cap_sum['infrastructure_error']}</div><div class="kpi-lbl">Infra errors</div></div>
  <div class="kpi"><div class="kpi-val" style="color:#d29922">{cap_sum['timeout']}</div><div class="kpi-lbl">Timeouts</div></div>
  <div class="kpi"><div class="kpi-val" style="color:#3fb950">{cap_sum['agent_success']}</div><div class="kpi-lbl">Agent SUCCESS</div></div>
  <div class="kpi"><div class="kpi-val" style="color:#f85149">{cap_sum['agent_fail']}</div><div class="kpi-lbl">Agent FAIL</div></div>
  <div class="kpi"><div class="kpi-val">{rep_sum['total_replays']}</div><div class="kpi-lbl">Replays lances</div></div>
  <div class="kpi"><div class="kpi-val" style="color:#3fb950">{rep_sum['pass']}</div><div class="kpi-lbl">Replays PASS</div></div>
  <div class="kpi"><div class="kpi-val" style="color:#f85149">{rep_sum['fail']}</div><div class="kpi-lbl">Replays FAIL</div></div>
  <div class="kpi"><div class="kpi-val" style="color:#3fb950">{rep_sum['tasks_with_all_pass']}</div><div class="kpi-lbl">Taches 3/3 PASS</div></div>
  <div class="kpi"><div class="kpi-val">{summary.get('duration_s')}s</div><div class="kpi-lbl">Duree totale</div></div>
</div>

<h2>Captures BU</h2>
<table><thead><tr><th>Task ID</th><th>Capture</th><th>Agent</th><th>Duree</th><th>Steps</th><th>Raw</th><th>Instruction (extrait)</th></tr></thead>
<tbody>{"".join(cap_rows)}</tbody></table>

<h2>Replays 3x par tache eligible</h2>
<table><thead><tr><th>Task ID</th><th>Runs</th><th>Total</th><th>Duree moy</th></tr></thead>
<tbody>{"".join(rep_rows)}</tbody></table>

<div style="text-align:center;color:#8b949e;font-size:12px;margin-top:24px">
Genere par DOMAutopsy benchmark_runner - {datetime.now().isoformat(timespec="seconds")}
</div>
</body></html>"""
