"""
DOMAutopsy - Worker isole pour une tache benchmark.
======================================================
Un process Python par tache, lance par benchmark_runner via
subprocess.Popen. Recoit la tache dechiffree en STDIN (JSON une
seule ligne) - jamais en ligne de commande (visible dans ps aux).

Isolation stricte (regle Julien) :
- Chromium Playwright headless demarrre par ce process
- Profil temporaire jetable (via chromium.launch() sans user_data_dir)
- Port CDP dynamique (find_free_port sur cette machine)
- run_id + output_dir uniques
- Cleanup complet en fin (browser.close + pw.stop)
- Aucun MCP Oculix, aucun Docker, aucune connexion a un Chrome existant

Le worker execute :
1. Recoit stdin : {task_id, confirmed_task, category, answer?, timeout_s?}
2. Lance qa_explorer.run() sur cette tache
3. Ecrit les artifacts dans un output_dir isole
4. Ecrit un resultat structure en STDOUT (JSON une ligne) que le
   runner parent lit
5. Exit code 0 si capture OK, 1 si erreur infrastructure

Usage (jamais direct) :
    echo '{"task_id":"...","confirmed_task":"...","output_dir":"...",...}' | \\
    py -3.12 benchmark_worker.py
"""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sys
import time
from pathlib import Path

ROOT = Path(__file__).parent


def _find_free_cdp_port() -> int:
    """Alloue un port CDP LIBRE et DYNAMIQUE dans [9300, 9500].
    Evite strictement 9222 (defaut Chrome dev qui pourrait etre
    utilise par le navigateur perso du user)."""
    for port in range(9300, 9500):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError("Aucun port CDP libre dans [9300, 9500]")


def _read_task_from_stdin() -> dict:
    """Lit la tache dechiffree en 1 ligne JSON depuis stdin.
    Format attendu :
      {"task_id":"...", "confirmed_task":"...", "category":"...",
       "answer": "...", "output_dir":"...", "start_url":"about:blank",
       "model":"gpt-5-mini", "max_steps":40, "max_actions_per_step":10,
       "timeout_s":900}
    """
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("stdin vide, task attendue en JSON une ligne")
    return json.loads(raw)


async def _run_capture(task: dict) -> dict:
    """Lance qa_explorer.run() en direct (import python) pour eviter
    de spawn un subprocess supplementaire. Timeout applique par
    asyncio.wait_for."""
    sys.path.insert(0, str(ROOT))
    from qa_explorer import _patch_browser_use, run as qa_run

    _patch_browser_use()

    task_id = task["task_id"]
    output_dir = task["output_dir"]
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    start_url = task.get("start_url", "about:blank")
    scenario_prompt = task["confirmed_task"]
    model = task.get("model", "gpt-5-mini")
    max_steps = int(task.get("max_steps", 40))
    max_actions_per_step = int(task.get("max_actions_per_step", 10))
    timeout_s = float(task.get("timeout_s", 900))
    cdp_port = int(task.get("cdp_port") or _find_free_cdp_port())

    # OPENAI_API_KEY doit etre transmis via env par le parent
    from dotenv import load_dotenv
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return {
            "task_id": task_id,
            "capture_result": "infrastructure_error",
            "error": "OPENAI_API_KEY absente dans l'environnement",
            "duration_s": 0.0,
        }

    # Construit le task prompt pour qa_explorer (memes conventions que CLI)
    # start_url = about:blank pour ce benchmark - BU navigate lui-meme.
    task_prompt = (
        f"Va sur {start_url}\n\n"
        "IMPORTANT : Si un element n'est pas visible ou cliquable, "
        "scroll pour le trouver. Si un popup de cookies apparait, "
        "accepte-le d'abord.\n\n"
        f"{scenario_prompt}\n\n"
        "Retourne SUCCESS si tout s'est bien passe, FAIL avec la raison sinon."
    )

    timing_opts = {
        "min_wait": 2.0, "max_wait": 15.0, "network_idle": 3.0,
        "max_steps": max_steps,
        "provider": "openai",
        "base_url": None,
        "api_key": api_key,
        "use_vision": True,
        "output_format": "katalon",
        "headless": True,
        "output_dir": output_dir,
        "open_report": False,
    }

    t0 = time.monotonic()
    try:
        await asyncio.wait_for(
            qa_run(
                task=task_prompt,
                model=model,
                cdp_port=cdp_port,
                scenario_name=f"BU_Bench_V1/{task_id}",
                scenario_url=start_url,
                scenario_steps=None,
                timing_opts=timing_opts,
            ),
            timeout=timeout_s,
        )
        duration = time.monotonic() - t0
        # Lit meta.json produit par qa_explorer pour classification
        meta_path = Path(output_dir) / "meta.json"
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                pass
        return {
            "task_id": task_id,
            "capture_result": "success",
            "agent_status": meta.get("agent_status"),
            "agent_result_short": (meta.get("agent_result") or "")[:200],
            "raw_count": meta.get("raw_count"),
            "clean_steps_total": meta.get("clean_steps_total"),
            "clean_steps_included": meta.get("clean_steps_included"),
            "clean_steps_filtered": meta.get("clean_steps_filtered"),
            "output_dir": output_dir,
            "duration_s": round(duration, 1),
            "cdp_port": cdp_port,
        }
    except asyncio.TimeoutError:
        duration = time.monotonic() - t0
        return {
            "task_id": task_id,
            "capture_result": "timeout",
            "error": f"Timeout apres {timeout_s}s",
            "duration_s": round(duration, 1),
            "cdp_port": cdp_port,
            "output_dir": output_dir,
        }
    except Exception as e:
        duration = time.monotonic() - t0
        return {
            "task_id": task_id,
            "capture_result": "infrastructure_error",
            "error": f"{type(e).__name__}: {str(e)[:400]}",
            "duration_s": round(duration, 1),
            "cdp_port": cdp_port,
            "output_dir": output_dir,
        }


def main():
    task = _read_task_from_stdin()
    result = asyncio.run(_run_capture(task))
    # Emit resultat JSON sur STDOUT ligne unique pour le parent
    sys.stdout.write(json.dumps(result, ensure_ascii=False))
    sys.stdout.flush()
    # Exit code : 0 si tout OK, 1 si infra error, 2 si timeout
    if result.get("capture_result") == "success":
        sys.exit(0)
    elif result.get("capture_result") == "timeout":
        sys.exit(2)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
