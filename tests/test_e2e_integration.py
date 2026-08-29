"""Test cahier des charges #16 : integration end-to-end.

Genere un JSON avec plusieurs actions -> produit test_playwright.spec.ts
-> lance REELLEMENT `npx playwright test` sur une page HTML servie en
local -> verifie code retour, replay_results.json, rapprochement
[step-XXXX], et generation replay_report.html.

Skip conditionnel si Node/npx/Playwright browser absent - mais si tout
est installe, ce test doit passer et prouve la chaine complete.
"""
import http.server
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

import pytest

from schemas import CleanSteps, Step, Selector
from playwright_generator import generate_playwright_ts
from replay_reporter import generate_replay_report


ROOT = Path(__file__).parent.parent
FIXTURES = Path(__file__).parent / "fixtures"


# --------------------------------------------------------------------
# Skip guards : verifie que l'environnement d'integration est present
# --------------------------------------------------------------------

def _has_command(cmd):
    return shutil.which(cmd) is not None

def _has_node_modules():
    return (ROOT / "node_modules" / "@playwright" / "test").exists()

pytestmark = [
    pytest.mark.skipif(not _has_command("npx"),
                       reason="npx introuvable (installer Node)"),
    pytest.mark.skipif(not _has_node_modules(),
                       reason="node_modules absent (lancer `npm install`)"),
]


# --------------------------------------------------------------------
# Serveur HTTP local pour la page de test
# --------------------------------------------------------------------

def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@contextmanager
def _serve_fixture(port):
    """Sert tests/fixtures/ sur un port local, thread daemon."""
    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *_args, **_kwargs):
            pass  # pas de spam stdout

    os.chdir(FIXTURES)
    try:
        httpd = http.server.HTTPServer(("127.0.0.1", port), QuietHandler)
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{port}/test_page.html"
        finally:
            httpd.shutdown()
    finally:
        os.chdir(ROOT)


# --------------------------------------------------------------------
# Test E2E complet
# --------------------------------------------------------------------

def test_full_pipeline_multi_action_scenario(tmp_path):
    """#16 : scenario avec plusieurs types d'actions (navigate, input,
    click, verify, scroll) genere un spec.ts qui tourne vraiment sur
    Chromium via npx playwright test, produit replay_results.json
    exploitable, et un replay_report.html rapproche par step_id."""
    port = _free_port()
    with _serve_fixture(port) as page_url:
        # 1. Construire un CleanSteps avec 5+ types d'actions
        cs = CleanSteps(
            schema_version="2.0",
            parcours="e2e test multi-action",
            scenario_url=page_url,
            total_steps=5,
            steps=[
                Step(id="step-0001", step=1, action="navigate", url=page_url),
                Step(id="step-0002", step=2, action="input",
                     selector=Selector(value="#username", strategy="id", unique=True),
                     value="testuser"),
                Step(id="step-0003", step=3, action="click",
                     selector=Selector(value="#login-btn", strategy="id", unique=True)),
                Step(id="step-0004", step=4, action="verify",
                     verify_type="texte_contient", expected="Bienvenue testuser"),
                Step(id="step-0005", step=5, action="scroll",
                     direction="vers_element",
                     selector=Selector(value="#footer-text", strategy="id", unique=True)),
            ],
        )

        # 2. Layout d'un pseudo run dir dans tmp_path -> repo path
        # Pour que --output et le spec relatif marchent, on cree le run
        # dir DANS le repo (runs/tmp_e2e_xxx/) puis on le supprimera.
        run_id = "e2etest01"
        ts = "99999999_e2e"
        run_dir = ROOT / "runs" / f"{ts}_{run_id}"
        run_dir.mkdir(parents=True, exist_ok=True)

        try:
            spec_path = run_dir / "test_playwright.spec.ts"
            gen = generate_playwright_ts(cs, spec_path)
            # Sanity : contenu genere OK, aucune action inconnue
            assert gen["included_count"] == 5
            assert gen["unsupported"] == []
            assert spec_path.exists()

            # Ecrit aussi le clean_steps.json (pour l'enrichissement rapport)
            (run_dir / "clean_steps.json").write_text(
                cs.model_dump_json(exclude_none=True), encoding="utf-8",
            )

            # 3. Prepare replay dir + env pour le JSON reporter
            replay_dir = ROOT / "runs" / f"{ts}_e2e_replay"
            replay_dir.mkdir(exist_ok=True)
            replay_json = replay_dir / "replay_results.json"
            (replay_dir / "meta.json").write_text(json.dumps({
                "engine": "playwright_ts", "is_replay": True,
                "source_run_id": run_id,
            }), encoding="utf-8")

            env = dict(os.environ)
            env["DOMAUTOPSY_REPLAY_JSON"] = str(replay_json)

            spec_rel = spec_path.relative_to(ROOT).as_posix()
            output_rel = replay_dir.relative_to(ROOT).as_posix()
            cmd = ["npx", "playwright", "test", spec_rel,
                   "--workers=1", f"--output={output_rel}"]

            # 4. Lancer reellement - meme pattern shell-cross-OS que
            # server.py::/api/replay : Windows a besoin de shell=True car
            # npx est un .cmd, pas un exe (subprocess.run(['npx',...])
            # sans shell leve FileNotFoundError [WinError 2]).
            if sys.platform == "win32":
                cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
                proc = subprocess.run(
                    cmd_str, shell=True, cwd=str(ROOT), env=env,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=180,
                )
            else:
                proc = subprocess.run(
                    cmd, cwd=str(ROOT), env=env,
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=180,
                )
            # Persist stdout/stderr en fichiers - print() sur Windows cp1252
            # crashe sur les glyphes UTF-8 de Playwright.
            (replay_dir / "stdout.log").write_text(proc.stdout or "", encoding="utf-8")
            (replay_dir / "stderr.log").write_text(proc.stderr or "", encoding="utf-8")

            def _safe(msg: str | None) -> str:
                return (msg or "").encode("ascii", "replace").decode("ascii")

            # 5. Verifier code retour (0 = succes)
            assert proc.returncode == 0, (
                f"npx playwright test a echoue avec code {proc.returncode}\n"
                f"stdout tail: {_safe(proc.stdout)[-2000:]}\n"
                f"stderr tail: {_safe(proc.stderr)[-1000:]}"
            )

            # 6. Verifier replay_results.json produit et exploitable
            assert replay_json.exists(), "replay_results.json non produit"
            pw_report = json.loads(replay_json.read_text(encoding="utf-8"))
            assert pw_report.get("stats", {}).get("expected", 0) >= 1

            # 7. Verifier presence des [step-XXXX] dans le JSON reporter
            report_text = replay_json.read_text(encoding="utf-8")
            for sid in ["step-0001", "step-0002", "step-0003", "step-0004", "step-0005"]:
                assert sid in report_text, f"{sid} absent du JSON reporter"

            # 8. Generer et verifier le rapport HTML final
            html_path = generate_replay_report(replay_dir, source_run_dir=run_dir)
            assert html_path is not None and html_path.exists()
            html = html_path.read_text(encoding="utf-8")
            assert "SUCCESS" in html or "success" in html.lower()
            for sid in ["step-0001", "step-0002", "step-0003"]:
                assert sid in html

        finally:
            # Cleanup - supprime les dossiers temporaires crees dans runs/
            shutil.rmtree(run_dir, ignore_errors=True)
            shutil.rmtree(ROOT / "runs" / f"{ts}_e2e_replay", ignore_errors=True)
