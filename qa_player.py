"""
QA Player - Replay deterministe d'un parcours capture
=====================================================
Prend un dossier runs/<id>/ existant, lit son clean_steps.json (parcours
nettoye par l'IA), et le rejoue via Playwright pur. Aucun LLM, aucun
browser-use : juste les selecteurs et les actions structures.

Cas d'usage :
- Verifier que les selecteurs d'un parcours capture sont encore valides
  (regression detection sur selector drift)
- Rejouer un test en boucle pour valider la stabilite (anti-flake)
- Integration CI : exit code != 0 si un step echoue

Output :
- replay_report.json : {steps: [{index, action, selector, status, duration_ms, error}], total_pass, total_fail, ...}
- step_<N>_<status>.png : screenshot apres chaque step

Usage :
  python qa_player.py --run-dir runs/20260510_180136_abc123 \\
                      --output-dir runs/20260510_185000_replay_of_abc123 \\
                      --port 9222 --headless

Exit codes :
- 0 : tous les steps OK
- 1 : au moins 1 step en echec
- 2 : erreur de demarrage (clean_steps.json absent, etc.)
"""

from playwright.async_api import async_playwright
from dotenv import load_dotenv
from pathlib import Path
from datetime import datetime
import asyncio
import argparse
import json
import sys
import time

load_dotenv()


def _print_header(text):
    print(f"\n{'=' * 60}")
    print(f"  {text}")
    print(f"{'=' * 60}", flush=True)


def _esc(s):
    return (str(s or "").replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;'))


def _build_replay_html(report: dict, ts: str) -> str:
    """Genere un rapport HTML self-contained pour un replay"""
    verdict_color = "#3fb950" if report["all_pass"] else "#f85149"
    verdict_label = "SUCCESS" if report["all_pass"] else "FAILURE"
    step_rows = ""
    for s in report["steps"]:
        status = s.get("status", "?")
        if status == "pass":
            badge_color = "#3fb950"
            badge_label = "PASS"
        elif status == "fail":
            badge_color = "#f85149"
            badge_label = "FAIL"
        else:
            badge_color = "#d29922"
            badge_label = status.upper()
        shot = s.get("screenshot")
        shot_cell = f'<a href="{shot}" target="_blank"><img src="{shot}" style="max-height:80px;border-radius:4px"/></a>' if shot else "—"
        err = f'<div style="color:#f85149;font-size:11px;margin-top:4px;font-family:monospace">{_esc(s.get("error",""))}</div>' if s.get("error") else ""
        step_rows += f"""
        <tr>
          <td>{s['index']}</td>
          <td><span style="background:{badge_color};color:#0e1116;padding:2px 8px;border-radius:10px;font-size:11px;font-weight:600">{badge_label}</span></td>
          <td>{_esc(s.get('action', '?')).upper()}</td>
          <td><code style="font-size:11px">{_esc((s.get('selector') or '')[:80])}</code></td>
          <td>{_esc(s.get('description', '')[:80])}{err}</td>
          <td>{s.get('duration_ms', '?')} ms</td>
          <td>{shot_cell}</td>
        </tr>"""

    return f"""<!doctype html>
<html lang="fr"><head><meta charset="utf-8"><title>QA Replay Report {ts}</title>
<style>
body{{margin:0;background:#0e1116;color:#e6edf3;font-family:-apple-system,sans-serif;padding:24px}}
.header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:24px}}
h1{{margin:0;font-size:22px}}
.verdict{{padding:8px 16px;border-radius:8px;font-weight:600;font-size:14px;background:{verdict_color};color:#0e1116}}
.kpis{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}}
.kpi{{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:16px;text-align:center}}
.kpi-value{{font-size:28px;font-weight:600}}
.kpi-label{{color:#8b949e;font-size:11px;text-transform:uppercase;letter-spacing:1px;margin-top:4px}}
table{{width:100%;border-collapse:collapse;background:#161b22;border:1px solid #30363d;border-radius:8px;overflow:hidden}}
th{{background:#0d1117;text-align:left;padding:10px 12px;font-size:12px;text-transform:uppercase;color:#8b949e;letter-spacing:1px}}
td{{padding:10px 12px;border-top:1px solid #30363d;font-size:13px;vertical-align:top}}
code{{background:#0d1117;padding:2px 6px;border-radius:4px;color:#58a6ff;font-family:ui-monospace,monospace}}
a{{color:#58a6ff}}
.source{{color:#8b949e;font-size:13px;margin-bottom:24px}}
</style></head><body>
<div class="header">
  <div>
    <h1>QA Replay Report</h1>
    <div class="source">Source : <code>{_esc(report['source_run_dir'])}</code> &middot; URL : <code>{_esc(report['scenario_url'])}</code> &middot; {ts}</div>
  </div>
  <span class="verdict">{verdict_label}</span>
</div>
<div class="kpis">
  <div class="kpi"><div class="kpi-value" style="color:#3fb950">{report['passed']}</div><div class="kpi-label">Passed</div></div>
  <div class="kpi"><div class="kpi-value" style="color:#f85149">{report['failed']}</div><div class="kpi-label">Failed</div></div>
  <div class="kpi"><div class="kpi-value" style="color:#d29922">{report['skipped']}</div><div class="kpi-label">Skipped</div></div>
  <div class="kpi"><div class="kpi-value">{report['total_steps']}</div><div class="kpi-label">Total</div></div>
</div>
<table>
  <thead><tr><th>#</th><th>Status</th><th>Action</th><th>Selecteur</th><th>Description</th><th>Duree</th><th>Screenshot</th></tr></thead>
  <tbody>{step_rows}</tbody>
</table>
<div style="margin-top:24px;color:#8b949e;font-size:12px;text-align:center">
QA Replay Report &middot; DOMAutopsy &middot; JMer Consulting
</div>
</body></html>"""


async def play(run_dir: Path, output_dir: Path, cdp_port: int = 9222,
               headless: bool = True, step_timeout_ms: int = 10000):
    """Rejoue un parcours capture, prend screenshots, ecrit replay_report.json"""
    output_dir.mkdir(parents=True, exist_ok=True)

    # 1. Charger le parcours nettoye
    clean_file = run_dir / "clean_steps.json"
    if not clean_file.exists():
        print(f"ERREUR : {clean_file} introuvable")
        return 2
    clean_data = json.loads(clean_file.read_text(encoding="utf-8"))
    steps = clean_data.get("steps", [])
    if not steps:
        print(f"ERREUR : aucun step dans {clean_file}")
        return 2

    # 2. Determiner l'URL de depart depuis meta.json
    meta_file = run_dir / "meta.json"
    start_url = None
    if meta_file.exists():
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        start_url = meta.get("scenario_url")
    # Fallback : on prend l'URL du 1er step
    if not start_url and steps and steps[0].get("page"):
        start_url = steps[0]["page"]
    if not start_url:
        print("ERREUR : URL de depart introuvable (ni meta.json ni step[0].page)")
        return 2

    _print_header(f"REPLAY : {clean_data.get('parcours', run_dir.name)}")
    print(f"  Source     : {run_dir}")
    print(f"  Output     : {output_dir}")
    print(f"  URL        : {start_url}")
    print(f"  Steps      : {len(steps)}")
    print(f"  Headless   : {headless}")
    print(f"  CDP port   : {cdp_port}")

    # 3. Lancer Chromium via Playwright (memes args optimises que qa_explorer)
    pw = await async_playwright().start()
    chromium_args = [f"--remote-debugging-port={cdp_port}"]
    if headless:
        chromium_args += [
            "--no-sandbox", "--disable-gpu", "--disable-dev-shm-usage",
            "--disable-background-networking", "--window-size=1280,720",
        ]
    else:
        chromium_args.append("--start-maximized")
    browser = await pw.chromium.launch(headless=headless, args=chromium_args)

    try:
        context = browser.contexts[0] if browser.contexts else await browser.new_context(no_viewport=headless)
        page = context.pages[0] if context.pages else await context.new_page()

        # 4. Navigate vers l'URL de depart
        _print_header("NAVIGATION")
        await page.goto(start_url, wait_until="domcontentloaded", timeout=30000)
        await page.wait_for_load_state("networkidle", timeout=15000)
        print(f"  Charge : {page.url}")

        # 5. Rejouer chaque step
        _print_header("REPLAY DES STEPS")
        report_steps = []
        all_pass = True
        for i, step in enumerate(steps, start=1):
            action = step.get("action", "?")
            selector = step.get("selector", "")
            sel_type = step.get("selectorType", "css")
            value = step.get("value")
            description = step.get("description", "")
            t0 = time.monotonic()

            # Build Playwright locator
            if sel_type == "xpath" or selector.startswith("//"):
                locator = page.locator(f"xpath={selector}")
            else:
                locator = page.locator(selector)

            print(f"\n  [{i}/{len(steps)}] {action.upper()} - {description[:80]}")
            print(f"      Selecteur : {selector}")
            try:
                # Attendre que l'element soit attache au DOM
                await locator.first.wait_for(state="attached", timeout=step_timeout_ms)

                if action == "click":
                    await locator.first.click(timeout=step_timeout_ms)
                elif action == "input":
                    if value is None or value == "<REDACTED>" or value == "<redacted>":
                        print(f"      [SKIP] Valeur redactee, step ignore (champ sensible)")
                        report_steps.append({
                            "index": i, "action": action, "selector": selector,
                            "status": "skipped_sensitive", "duration_ms": 0,
                        })
                        continue
                    await locator.first.fill(str(value), timeout=step_timeout_ms)
                else:
                    print(f"      [WARN] Action '{action}' non supportee dans le player V0")
                    report_steps.append({
                        "index": i, "action": action, "selector": selector,
                        "status": "skipped_unknown_action", "duration_ms": 0,
                    })
                    continue

                # Petite pause pour laisser l'UI se stabiliser apres l'action
                await asyncio.sleep(0.3)
                duration_ms = round((time.monotonic() - t0) * 1000, 1)

                # Screenshot pass
                screenshot_path = output_dir / f"step_{i:02d}_pass.png"
                await page.screenshot(path=str(screenshot_path), full_page=False)

                report_steps.append({
                    "index": i, "action": action, "selector": selector,
                    "selectorType": sel_type, "value": value if action == "input" and not str(value or "").startswith("<") else None,
                    "description": description,
                    "status": "pass", "duration_ms": duration_ms,
                    "screenshot": screenshot_path.name,
                    "url_after": page.url,
                })
                print(f"      [OK] {duration_ms} ms")
            except Exception as e:
                duration_ms = round((time.monotonic() - t0) * 1000, 1)
                # Screenshot fail
                screenshot_path = output_dir / f"step_{i:02d}_fail.png"
                try:
                    await page.screenshot(path=str(screenshot_path), full_page=False)
                except Exception:
                    pass
                err_text = type(e).__name__ + ": " + str(e)[:300]
                report_steps.append({
                    "index": i, "action": action, "selector": selector,
                    "selectorType": sel_type, "description": description,
                    "status": "fail", "duration_ms": duration_ms,
                    "error": err_text, "screenshot": screenshot_path.name,
                    "url_after": page.url,
                })
                print(f"      [FAIL] {err_text}")
                all_pass = False
                break  # Stop au premier fail (mode fail-fast)

        # 6. Synthese
        _print_header("SYNTHESE")
        total = len(report_steps)
        passed = sum(1 for s in report_steps if s["status"] == "pass")
        failed = sum(1 for s in report_steps if s["status"] == "fail")
        skipped = sum(1 for s in report_steps if s["status"].startswith("skipped"))
        print(f"  {passed}/{total} PASS, {failed} FAIL, {skipped} SKIPPED")
        print(f"  Verdict : {'SUCCESS' if all_pass and failed == 0 else 'FAILURE'}")

        # 7. Sauvegarder le rapport JSON
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        replay_report = {
            "timestamp": ts,
            "source_run_dir": str(run_dir),
            "scenario_url": start_url,
            "parcours": clean_data.get("parcours", ""),
            "total_steps": len(steps),
            "passed": passed,
            "failed": failed,
            "skipped": skipped,
            "all_pass": all_pass,
            "verdict": "success" if all_pass else "failure",
            "steps": report_steps,
        }
        report_file = output_dir / "replay_report.json"
        report_file.write_text(json.dumps(replay_report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"  Rapport JSON -> {report_file}")

        # 8. Sauvegarder un meta.json minimaliste pour l'historique
        meta = {
            "timestamp": ts,
            "started_at": replay_report["timestamp"],
            "ended_at": datetime.now().isoformat(),
            "scenario_url": start_url,
            "scenario_name": f"Replay: {clean_data.get('parcours', '')[:80]}",
            "task": f"Replay deterministe de {run_dir.name}",
            "output_format": "replay",
            "provider": "none",
            "model": "playwright-pure",
            "headless": headless,
            "deduped_count": passed,
            "agent_result": f"{'SUCCESS' if all_pass else 'FAILURE'} - {passed}/{len(steps)} pass, {failed} fail, {skipped} skip",
            "status": "success" if all_pass else "failure",
            "is_replay": True,
            "source_run_dir": run_dir.name,
            "report": f"qa_replay_report_{ts}.html",
        }
        (output_dir / "meta.json").write_text(json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8")

        # 9. Generer un rapport HTML minimal
        html = _build_replay_html(replay_report, ts)
        html_file = output_dir / f"qa_replay_report_{ts}.html"
        html_file.write_text(html, encoding="utf-8")
        print(f"  Rapport HTML -> {html_file}")

        return 0 if all_pass else 1

    finally:
        try:
            await browser.close()
        except Exception as e:
            print(f"  [WARN] Fermeture browser : {e}")
        await pw.stop()
        print("\n  Termine !")


def main():
    parser = argparse.ArgumentParser(description="DOMAutopsy Player - replay deterministe d'un parcours capture")
    parser.add_argument("--run-dir", required=True, help="Dossier du run source (contient clean_steps.json + meta.json)")
    parser.add_argument("--output-dir", default=None, help="Dossier de sortie du replay (defaut: runs/<ts>_replay_of_<srcdir>/)")
    parser.add_argument("--port", type=int, default=9222, help="Port CDP Chromium (defaut: 9222)")
    parser.add_argument("--headless", action="store_true", help="Mode headless (defaut: visible)")
    parser.add_argument("--step-timeout", type=int, default=10000, help="Timeout par step en ms (defaut: 10000)")
    args = parser.parse_args()

    run_dir = Path(args.run_dir).resolve()
    if not run_dir.exists():
        print(f"ERREUR : run-dir introuvable : {run_dir}")
        sys.exit(2)

    if args.output_dir:
        output_dir = Path(args.output_dir)
    else:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_dir = run_dir.parent / f"{ts}_replay_of_{run_dir.name}"

    exit_code = asyncio.run(play(
        run_dir=run_dir, output_dir=output_dir,
        cdp_port=args.port, headless=args.headless,
        step_timeout_ms=args.step_timeout,
    ))
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
