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

# Force stdout/stderr en UTF-8 des le demarrage - regle memoire
# rule-python-utf8-stdout-windows : sur Windows, sys.stdout default =
# cp1252 qui fail silencieusement sur les caracteres UTF-8 (accents FR,
# emojis, etc.). Sans ca, sys.stdout.write(json.dumps(...)) emet rien
# et le parent voit un stdout vide -> classe infrastructure_error a tort.
try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

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
    # BENCH : max_steps/max_actions non passes = defauts BU officiel
    timeout_s = float(task.get("timeout_s", 1800))

    # Print IMMEDIAT du texte de la tache dans worker_stdout.txt (local,
    # gitignore) - permet de voir ce que le worker execute pendant qu'il
    # tourne via tail worker_stdout.txt. Reste STRICTEMENT en local.
    print("=" * 60, flush=True)
    print(f"TASK {task_id}", flush=True)
    print("=" * 60, flush=True)
    print(f"[CONFIRMED_TASK]\n{scenario_prompt}", flush=True)
    print("=" * 60, flush=True)
    # cdp_port=0 (bench par defaut) : Chromium le choisit atomiquement via
    # --remote-debugging-port=0, qa_explorer lit ensuite via ws_endpoint.
    # cdp_port>0 : port fixe (mode standalone, Chrome dev perso).
    # cdp_port absent : fallback legacy self_allocate (jamais en bench).
    port_from_parent = task.get("cdp_port")
    if port_from_parent is None:
        cdp_port = _find_free_cdp_port()
        cdp_port_source = "self_allocated_fallback"
    else:
        cdp_port = int(port_from_parent)
        cdp_port_source = "chromium_delegated" if cdp_port == 0 else "parent_fixed"

    # Journalisation early : pid, port, provenance, timestamp - persiste
    # AVANT tout call reseau/browser. Permet de comparer ts_start entre
    # workers de la meme wave (cold-start Chromium sous contention) et
    # de recouper cdp_port_resolved (rempli par qa_explorer plus tard).
    try:
        (Path(output_dir) / "worker_launch.json").write_text(
            json.dumps({
                "task_id": task_id,
                "pid": os.getpid(),
                "cdp_port_requested": cdp_port,
                "cdp_port_source": cdp_port_source,
                "profile_mode": "temp_ephemeral_via_chromium_launch",
                "ts_start_utc": time.strftime("%Y-%m-%dT%H:%M:%S.", time.gmtime()) + f"{int(time.time()*1000)%1000:03d}Z",
                "ts_start_monotonic": time.monotonic(),
            }, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass

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
    # NOTE : le browser demarre sur about:blank, c'est normal. NE PAS
    # inclure about:blank dans les instructions - BU interpretait
    # "premiere action navigate" comme incluant about:blank et bouclait.
    task_prompt = (
        f"URL DU TEST (obligatoire, publique et fonctionnelle) : {start_url}\n\n"
        f"Ta premiere action utile doit etre : navigate vers cette URL exacte.\n"
        f"Ne cherche pas d'URL alternative (pas de todomvc.com, pas de netlify.app,\n"
        f"pas de recherche moteur). Si cette URL repond 404, retourne FAIL\n"
        f"immediatement sans tenter d'autres URLs.\n\n"
        "IMPORTANT : Si un element n'est pas visible ou cliquable, "
        "scroll pour le trouver. Si un popup de cookies apparait, "
        "accepte-le d'abord. Si un element visible n'a pas d'interactive "
        "index dans browser_state (checkbox dans liste, toggle shadow, "
        "etc.), tu es AUTORISE a utiliser evaluate() JavaScript pour "
        "cliquer directement - mais UN SEUL click par appel evaluate(), "
        "pas de setTimeout, pas de boucle, pas de multi-click chaine.\n\n"
        f"{scenario_prompt}\n\n"
        "Retourne SUCCESS si tout s'est bien passe, FAIL avec la raison sinon."
    )

    # Mode BENCH : reproduit exactement la config Agent officielle
    # browser-use/benchmark. AUCUN override step_timeout/llm_timeout/
    # max_actions/max_steps (defaults BU). Seul use_vision reste.
    # Timeout global par tache = 1800s (30 min, defaut officiel).
    timing_opts = {
        "min_wait": 2.0, "max_wait": 15.0, "network_idle": 3.0,
        "provider": "openai",
        "base_url": None,
        "api_key": api_key,
        "use_vision": True,
        "output_format": "katalon",
        "headless": os.getenv("BENCH_HEADED") != "1",
        "output_dir": output_dir,
        "open_report": False,
        "bench_mode": True,
        # Oracle final : dict {url_contains?, text_contains?} venant du corpus
        # (Real-World 7). qa_explorer l'injecte en assertion Playwright a la
        # fin du TS genere. Aucun LLM implique pour la validation replay.
        "oracle": task.get("oracle"),
    }

    def _persist(result: dict) -> None:
        """Ecrit worker_result.json en local dans output_dir, garanti meme si
        le runner parent meurt (Ctrl-C, TaskStop). Reste STRICTEMENT en
        .bu_bench_runs/ (gitignore) - jamais push."""
        try:
            (Path(output_dir) / "worker_result.json").write_text(
                json.dumps(result, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

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
        result = {
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
        _persist(result)
        return result
    except (asyncio.TimeoutError, TimeoutError) as e:
        duration = time.monotonic() - t0
        result = {
            "task_id": task_id,
            "capture_result": "timeout",
            "error": f"{type(e).__name__}: {str(e)[:400] or f'Timeout apres {timeout_s}s'}",
            "duration_s": round(duration, 1),
            "cdp_port": cdp_port,
            "output_dir": output_dir,
        }
        _persist(result)
        return result
    except Exception as e:
        duration = time.monotonic() - t0
        # Classe comme timeout aussi les erreurs OpenAI/httpx explicitement
        # "timeout" (APITimeoutError, ReadTimeout, WriteTimeout, ConnectTimeout)
        # pour ne pas polluer 'infrastructure_error' avec du network slow.
        etype = type(e).__name__
        is_timeout_like = any(k in etype for k in ("Timeout", "TimeOut"))
        # Print traceback complet sur stderr pour debug local
        import traceback as _tb
        tb_str = _tb.format_exc()
        try:
            sys.stderr.write(f"\n[worker exception traceback]\n{tb_str}\n")
            sys.stderr.flush()
        except Exception:
            pass
        result = {
            "task_id": task_id,
            "capture_result": "timeout" if is_timeout_like else "infrastructure_error",
            "error": f"{etype}: {str(e)[:400]}",
            "traceback": tb_str[-2000:],
            "duration_s": round(duration, 1),
            "cdp_port": cdp_port,
            "output_dir": output_dir,
        }
        _persist(result)
        return result


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
