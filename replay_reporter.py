"""
DOMAutopsy - Generateur du rapport HTML des runs REPLAY
========================================================
Lit le JSON produit par le Playwright JSON reporter
(replay_results.json) + le meta.json initial du replay, et ecrit un
rapport HTML self-contained dans <replay_dir>/replay_report.html.

Le rapport est appele par server.py apres la fin du subprocess `npx
playwright test` (hook dans _pump_stdout). Il rapproche chaque
test.step('[step-XXXX] ...') du step JSON correspondant via son id.

Fallback : si le fichier JSON n'est pas la (parsing echoue, reporter
crash), ecrit un rapport minimal indiquant l'echec pour ne pas laisser
un run replay sans HTML consultable.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any


STEP_ID_RE = re.compile(r"\[(?P<sid>step-\d{4,})\]")


def _esc(s: Any) -> str:
    return (str(s or "")
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;"))


def _extract_step_results(playwright_report: dict[str, Any]) -> list[dict[str, Any]]:
    """Parcourt la structure hierarchique Playwright JSON reporter et
    retourne une liste plate d'entrees {step_id, title, status, duration_ms, error}."""
    results: list[dict[str, Any]] = []

    def _walk_steps(nodes: list[dict[str, Any]], parent_status: str | None = None):
        for st in nodes or []:
            title = st.get("title") or ""
            m = STEP_ID_RE.search(title)
            step_id = m.group("sid") if m else None
            duration = st.get("duration")
            err = st.get("error") or {}
            err_msg = None
            if err:
                err_msg = err.get("message") or err.get("value")
            # Statut : Playwright n'expose pas explicitement 'passed' au niveau
            # step - une step sans error est passed, avec error est failed.
            status = "failed" if err_msg else "passed"
            if step_id or title.startswith("["):
                results.append({
                    "step_id": step_id,
                    "title": title,
                    "status": status,
                    "duration_ms": duration,
                    "error": err_msg,
                })
            # Recursion : Playwright peut nester des steps (attempt subtests)
            _walk_steps(st.get("steps") or [], status)

    def _walk_suites(suites: list[dict[str, Any]]):
        for suite in suites or []:
            for spec in suite.get("specs", []) or []:
                for test in spec.get("tests", []) or []:
                    for res in test.get("results", []) or []:
                        _walk_steps(res.get("steps") or [])
            _walk_suites(suite.get("suites") or [])

    _walk_suites(playwright_report.get("suites") or [])
    return results


def _top_level_verdict(playwright_report: dict[str, Any]) -> dict[str, Any]:
    """Extrait le verdict global : total, passed, failed, skipped, duration."""
    stats = playwright_report.get("stats") or {}
    # Certaines versions Playwright exposent des noms differents
    passed = stats.get("expected", 0) or stats.get("passed", 0)
    failed = stats.get("unexpected", 0) or stats.get("failed", 0)
    skipped = stats.get("skipped", 0)
    flaky = stats.get("flaky", 0)
    duration = stats.get("duration", 0)
    total = passed + failed + skipped + flaky
    return {
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "flaky": flaky,
        "total": total,
        "duration_ms": duration,
        "verdict": "success" if failed == 0 and total > 0 else ("failure" if failed > 0 else "empty"),
    }


def _load_source_steps(source_run_dir: Path | None) -> dict[str, dict[str, Any]]:
    """Charge le clean_steps.json du run source et retourne un mapping
    {step_id: step_dict} pour enrichir le rapport par lookup. Retourne un
    dict vide si le fichier est absent (le rapport HTML reste utilisable
    avec les seules infos Playwright)."""
    if source_run_dir is None:
        return {}
    p = Path(source_run_dir) / "clean_steps.json"
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for s in (data.get("steps") or []):
        sid = s.get("id")
        if sid:
            out[sid] = s
    return out


def generate_replay_report(replay_dir: Path, source_run_dir: Path | None = None) -> Path | None:
    """Ecrit replay_report.html dans replay_dir. Retourne le chemin, ou None
    si aucune source n'a pu etre chargee.

    Args:
        replay_dir: dossier du run replay
        source_run_dir: dossier du run source (si dispo, permet d'enrichir
            chaque ligne avec le selector, expected/actual, action, network
            et included_in_replay du step JSON canonique)
    """
    replay_dir = Path(replay_dir)
    results_path = replay_dir / "replay_results.json"
    meta_path = replay_dir / "meta.json"

    meta: dict[str, Any] = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    engine = meta.get("engine", "unknown")
    source_run_id = meta.get("source_run_id", "?")
    legacy_fallback = bool(meta.get("legacy_fallback"))
    fallback_reason = meta.get("legacy_fallback_reason")

    # Enrichissement optionnel via le clean_steps.json du run source
    source_steps = _load_source_steps(source_run_dir)

    step_results: list[dict[str, Any]] = []
    verdict: dict[str, Any] = {"passed": 0, "failed": 0, "skipped": 0, "total": 0, "verdict": "unknown"}
    parse_error: str | None = None

    if results_path.exists():
        try:
            pw_report = json.loads(results_path.read_text(encoding="utf-8"))
            step_results = _extract_step_results(pw_report)
            verdict = _top_level_verdict(pw_report)
        except Exception as e:
            parse_error = f"{type(e).__name__} : {e}"
    else:
        parse_error = (
            "replay_results.json absent."
            + (" Engine=qa_player_legacy : ce moteur ecrit son propre replay_report.json, pas de rapport TS a agreger." if legacy_fallback else "")
        )

    # Rendu HTML
    verdict_color = {"success": "#3fb950", "failure": "#f85149", "empty": "#d29922", "unknown": "#d29922"}.get(verdict["verdict"], "#d29922")

    step_rows = ""
    for r in step_results:
        badge_color = "#3fb950" if r["status"] == "passed" else "#f85149"
        dur = f"{r['duration_ms']} ms" if r["duration_ms"] is not None else "&mdash;"
        err = f'<div style="color:#f85149;font-family:monospace;font-size:11px;margin-top:6px">{_esc(r["error"])[:400]}</div>' if r.get("error") else ""

        # Enrichissement source : action, selector, expected/actual, page
        source = source_steps.get(r["step_id"] or "") or {}
        src_action = (source.get("action") or "").upper() if source else ""
        src_sel = ""
        if source:
            sel_val = source.get("selector")
            if isinstance(sel_val, dict):
                sel_val = sel_val.get("value") or sel_val.get("playwrightSelector")
            if sel_val:
                src_sel = f'<code style="font-size:11px">{_esc(str(sel_val)[:80])}</code>'
        src_desc = _esc(source.get("description") or "")[:120] if source else ""
        # Expected/actual pour les verify
        src_expected = source.get("expected") if source else None
        src_actual = source.get("actual") if source else None
        expected_row = ""
        if src_expected is not None or src_actual is not None:
            expected_row = f'<div style="margin-top:6px;font-size:11px;color:#94a3b8">Attendu : <code>{_esc(src_expected)}</code> &middot; Recu : <code>{_esc(src_actual)}</code></div>'
        # Reseau associe
        net = source.get("network") if source else None
        net_line = ""
        if net:
            fails = sum(1 for n in net if isinstance(n, dict) and (n.get("status") or 0) >= 400)
            net_line = f'<div style="margin-top:4px;font-size:11px;color:#94a3b8">Reseau : {len(net)} req' + (f", <span style=\"color:#f85149\">{fails} echec HTTP</span>" if fails else "") + "</div>"

        step_rows += f"""
        <tr>
          <td><code>{_esc(r["step_id"] or "&mdash;")}</code></td>
          <td>
            <div>{('<strong style=\"color:#7dd3fc\">' + src_action + '</strong> - ') if src_action else ''}{src_desc or _esc(r["title"])[:120]}</div>
            {('<div style=\"margin-top:4px\">' + src_sel + '</div>') if src_sel else ''}
            {expected_row}
            {net_line}
          </td>
          <td><span style="background:{badge_color};color:#0e1116;padding:2px 10px;border-radius:12px;font-size:11px;font-weight:700">{r["status"].upper()}</span></td>
          <td>{dur}</td>
          <td>{err}</td>
        </tr>"""
    if not step_rows:
        step_rows = '<tr><td colspan="5" style="text-align:center;color:#8b949e">Aucun step Playwright avec pattern [step-XXXX] detecte.</td></tr>'

    error_banner = ""
    if parse_error:
        error_banner = f"""<div style="background:#7f1d1d;padding:12px 20px;color:#fecaca;font-size:13px;border-bottom:1px solid #dc2626">
          <strong>Attention :</strong> {_esc(parse_error)}
        </div>"""

    fallback_banner = ""
    if legacy_fallback:
        fallback_banner = f"""<div style="background:#78350f;padding:12px 20px;color:#fef3c7;font-size:13px;border-bottom:1px solid #d97706">
          <strong>LEGACY FALLBACK :</strong> ce replay a utilise <code>qa_player.py</code> (click+input seulement). Raison : {_esc(fallback_reason or "test_playwright.spec.ts absent du run source")}.
        </div>"""

    html = f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>Replay Report - {_esc(source_run_id)}</title>
<style>
body {{ margin:0; background:#0e1116; color:#e6edf3; font-family:-apple-system,sans-serif; padding:0 }}
.header {{ padding:24px 40px; display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #30363d }}
h1 {{ margin:0; font-size:22px }}
.verdict {{ padding:8px 20px; border-radius:8px; font-weight:700; font-size:14px; background:{verdict_color}; color:#0e1116 }}
.main {{ padding:24px 40px }}
.kpis {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(140px,1fr)); gap:12px; margin-bottom:24px }}
.kpi {{ background:#161b22; border:1px solid #30363d; border-radius:8px; padding:16px; text-align:center }}
.kpi-value {{ font-size:28px; font-weight:700 }}
.kpi-label {{ color:#8b949e; font-size:11px; text-transform:uppercase; letter-spacing:1px; margin-top:4px }}
table {{ width:100%; border-collapse:collapse; background:#161b22; border:1px solid #30363d; border-radius:8px; overflow:hidden }}
th {{ background:#0d1117; text-align:left; padding:10px 12px; font-size:12px; text-transform:uppercase; color:#8b949e; letter-spacing:1px }}
td {{ padding:10px 12px; border-top:1px solid #30363d; font-size:13px; vertical-align:top }}
code {{ background:#0d1117; padding:2px 6px; border-radius:4px; color:#58a6ff; font-family:ui-monospace,monospace }}
.meta {{ color:#8b949e; font-size:12px }}
</style></head><body>
<div class="header">
  <div>
    <h1>Replay Playwright TS</h1>
    <div class="meta">Source run : <code>{_esc(source_run_id)}</code> &middot; Engine : <code>{_esc(engine)}</code></div>
  </div>
  <span class="verdict">{verdict["verdict"].upper()}</span>
</div>
{fallback_banner}
{error_banner}
<div class="main">
  <div class="kpis">
    <div class="kpi"><div class="kpi-value" style="color:#3fb950">{verdict["passed"]}</div><div class="kpi-label">Passed</div></div>
    <div class="kpi"><div class="kpi-value" style="color:#f85149">{verdict["failed"]}</div><div class="kpi-label">Failed</div></div>
    <div class="kpi"><div class="kpi-value" style="color:#d29922">{verdict["skipped"]}</div><div class="kpi-label">Skipped</div></div>
    <div class="kpi"><div class="kpi-value">{verdict["total"]}</div><div class="kpi-label">Tests</div></div>
    <div class="kpi"><div class="kpi-value">{round(verdict["duration_ms"]/1000, 2) if verdict.get("duration_ms") else "&mdash;"}</div><div class="kpi-label">Duree (s)</div></div>
  </div>
  <h2 style="font-size:16px;margin-bottom:12px">Steps rapproches (test.step[step-XXXX])</h2>
  <table>
    <thead><tr><th>Step ID</th><th>Titre</th><th>Statut</th><th>Duree</th><th>Erreur</th></tr></thead>
    <tbody>{step_rows}</tbody>
  </table>
  <div style="margin-top:24px;color:#8b949e;font-size:12px;text-align:center">
    Rapport genere par DOMAutopsy replay_reporter &middot; format schema v1.0
  </div>
</div>
</body></html>"""

    out_path = replay_dir / "replay_report.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def update_replay_meta_with_verdict(replay_dir: Path) -> dict[str, Any] | None:
    """Enrichit le meta.json d'un replay avec le verdict et les counts extraits
    du JSON reporter Playwright. Utile pour /api/runs/{id} qui expose ces
    valeurs au CLI/dashboard."""
    replay_dir = Path(replay_dir)
    meta_path = replay_dir / "meta.json"
    results_path = replay_dir / "replay_results.json"
    if not meta_path.exists():
        return None
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if results_path.exists():
        try:
            pw = json.loads(results_path.read_text(encoding="utf-8"))
            verdict = _top_level_verdict(pw)
            meta.update({
                "status": verdict["verdict"],
                "replay_passed": verdict["passed"],
                "replay_failed": verdict["failed"],
                "replay_skipped": verdict["skipped"],
                "replay_total": verdict["total"],
                "replay_duration_ms": verdict.get("duration_ms"),
            })
        except Exception as e:
            meta["replay_parse_error"] = f"{type(e).__name__}: {e}"
    else:
        meta.setdefault("status", "unknown")
    meta["report"] = "replay_report.html"
    meta_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")
    return meta
