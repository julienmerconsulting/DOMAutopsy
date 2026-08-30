"""
DOMAutopsy - Runner benchmark BU_Bench_V1.
==============================================
Orchestre le dechiffrement du corpus (memoire only), la selection
des 20 taches Custom, l'execution par vagues de 5 workers isoles,
puis les replays TS 3x de chaque tache eligible.

Regles strictes :
- Le fichier BU_Bench_V1.enc n'est jamais decrypte sur disque
- Les taches dechiffrees ne quittent la memoire que via stdin des
  workers (jamais argv, jamais fichier temp)
- Cinq navigateurs GLOBAL max (pas 5 captures + 5 replays)
- Strictement sequentiel : 20 captures d'abord, puis replays
- Chaque worker isole (process Python + Chromium temp + port CDP dyn)
- Timeout 15 min par capture (kill worker + classe 'timeout')
- Aucun MCP Oculix, aucun Docker
"""
from __future__ import annotations

import base64
import concurrent.futures
import hashlib
import json
import os
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


ROOT = Path(__file__).parent
RUNS_ROOT = ROOT / ".bu_bench_runs"

# Categories acceptees pour les "Custom / Page interaction challenges"
CUSTOM_CATEGORY_NAMES = ("Custom", "InteractionTests", "Custom / Page interaction challenges", "custom")
EXPECTED_TASKS_COUNT = 20

DEFAULT_MAX_WORKERS = 5
DEFAULT_CAPTURE_TIMEOUT_S = 900   # 15 min par capture (regle Sol)
DEFAULT_REPLAY_TIMEOUT_S = 180
DEFAULT_REPLAYS = 3


class BenchmarkRunError(Exception):
    pass


# ============================================================
# Dechiffrement OFFICIEL (Fernet, cle = SHA-256("BU_Bench_V1") en b64)
# ============================================================

def load_bu_bench_v1_in_memory(enc_path: Path) -> list[dict]:
    """Dechiffre BU_Bench_V1.enc EN MEMOIRE UNIQUEMENT.
    Algorithme exact du runner officiel (cf. https://github.com/browser-use/benchmark)."""
    try:
        from cryptography.fernet import Fernet
    except ImportError as e:
        raise BenchmarkRunError(
            "Dependance 'cryptography' absente. "
            "Lancer : python domautopsy_cli.py benchmark install --source bu-v1"
        ) from e

    key = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V1").digest())
    encrypted = base64.b64decode(enc_path.read_text(encoding="utf-8"))
    decrypted = Fernet(key).decrypt(encrypted)
    tasks = json.loads(decrypted)
    if not isinstance(tasks, list):
        raise BenchmarkRunError("BU_Bench_V1 doit contenir une liste JSON")
    return tasks


# ============================================================
# Selection categorie Custom
# ============================================================

def select_custom_tasks(tasks: list[dict]) -> tuple[list[dict], dict[str, int], str | None]:
    """Retourne (tasks_selected, counts_by_category, chosen_category).
    Ne selectionne QUE si une categorie matche exactement l'un des noms
    connus ET si son count == 20. Sinon chosen_category=None et
    tasks_selected=[]. Le caller doit alors afficher counts_by_category
    sans texte de tache et abort."""
    counts = Counter(t.get("category", "?") for t in tasks)
    chosen = None
    for name in CUSTOM_CATEGORY_NAMES:
        if counts.get(name) == EXPECTED_TASKS_COUNT:
            chosen = name
            break
    if chosen is None:
        return [], dict(counts), None
    selected = [t for t in tasks if t.get("category") == chosen]
    return selected, dict(counts), chosen


# ============================================================
# Execution par worker via stdin
# ============================================================

def _run_capture_worker(task: dict, output_dir: Path, timeout_s: float) -> dict:
    """Lance benchmark_worker.py en subprocess, transmet la tache par
    STDIN (pas argv). Retourne le dict resultat que le worker a emit
    sur son stdout."""
    output_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        **task,
        "output_dir": str(output_dir),
        "start_url": "about:blank",
        "model": "gpt-5-mini",
        "max_steps": 40,
        "max_actions_per_step": 10,
        "timeout_s": timeout_s,
    }
    env = dict(os.environ)
    cmd = [sys.executable, str(ROOT / "benchmark_worker.py")]
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout_s + 60,  # marge parent pour capturer le stdout
            env=env, cwd=str(ROOT),
        )
        try:
            result = json.loads(proc.stdout.strip().split("\n")[-1])
        except (json.JSONDecodeError, IndexError):
            return {
                "task_id": task.get("task_id"),
                "capture_result": "infrastructure_error",
                "error": f"stdout worker non-JSON. exit={proc.returncode} tail={proc.stdout[-300:]}",
            }
        return result
    except subprocess.TimeoutExpired:
        return {
            "task_id": task.get("task_id"),
            "capture_result": "timeout",
            "error": f"Parent subprocess timeout apres {timeout_s + 60}s",
        }
    except Exception as e:
        return {
            "task_id": task.get("task_id"),
            "capture_result": "infrastructure_error",
            "error": f"{type(e).__name__}: {e}",
        }


def _run_replay(spec_path: Path, replay_output_dir: Path, timeout_s: float) -> dict:
    """Rejoue une spec TS avec npx playwright test. Retourne
    {status, duration_s, error?}. Un LLM n'est jamais implique."""
    replay_output_dir.mkdir(parents=True, exist_ok=True)
    try:
        spec_rel = spec_path.relative_to(ROOT).as_posix()
    except ValueError:
        spec_rel = str(spec_path)
    output_rel = replay_output_dir.relative_to(ROOT).as_posix() if replay_output_dir.is_relative_to(ROOT) else str(replay_output_dir)
    cmd = ["npx", "playwright", "test", spec_rel, "--workers=1", f"--output={output_rel}"]
    t0 = time.monotonic()
    try:
        if sys.platform == "win32":
            cmd_str = " ".join(f'"{c}"' if " " in c else c for c in cmd)
            proc = subprocess.run(
                cmd_str, shell=True, cwd=str(ROOT),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s,
            )
        else:
            proc = subprocess.run(
                cmd, cwd=str(ROOT),
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_s,
            )
        return {
            "status": "pass" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "duration_s": round(time.monotonic() - t0, 1),
            "error": None if proc.returncode == 0 else (proc.stdout or "")[-500:],
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "duration_s": round(time.monotonic() - t0, 1),
            "error": f"Replay timeout apres {timeout_s}s",
        }


def _is_replayable(capture_result: dict) -> bool:
    """Un run est rejouable si la capture BU a reussi (success ou fail
    fonctionnel) ET qu'un test_playwright.spec.ts a ete genere."""
    if capture_result.get("capture_result") != "success":
        return False
    od = capture_result.get("output_dir")
    if not od:
        return False
    return (Path(od) / "test_playwright.spec.ts").exists()


# ============================================================
# Orchestration en vagues
# ============================================================

def run_benchmark(
    tasks: list[dict],
    run_root: Path,
    workers: int = DEFAULT_MAX_WORKERS,
    replays: int = DEFAULT_REPLAYS,
    capture_timeout_s: float = DEFAULT_CAPTURE_TIMEOUT_S,
    replay_timeout_s: float = DEFAULT_REPLAY_TIMEOUT_S,
    progress_cb=None,
) -> dict:
    """Execute 4 vagues * 5 captures STRICTEMENT SEQUENTIELLES, puis
    tous les replays des taches eligibles par vagues de 5. Le plafond
    de workers est GLOBAL : jamais 5 captures + 5 replays simultanes.
    """
    run_root.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now()

    # --- PHASE 1 : Captures ---
    capture_results: list[dict] = []
    total = len(tasks)
    n_waves = (total + workers - 1) // workers
    for wave_idx in range(n_waves):
        wave = tasks[wave_idx * workers : (wave_idx + 1) * workers]
        if progress_cb:
            progress_cb(f"CAPTURE wave {wave_idx+1}/{n_waves} : {len(wave)} taches en parallele")
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futures = {}
            for i, task in enumerate(wave):
                task_id = task.get("task_id") or f"t{wave_idx*workers+i:02d}"
                # output_dir unique par tache
                safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(task_id))[:40]
                out = run_root / f"capture_{safe_id}"
                futures[ex.submit(_run_capture_worker, task, out, capture_timeout_s)] = task
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                capture_results.append(res)
                if progress_cb:
                    progress_cb(f"  -> {res.get('task_id')} : {res.get('capture_result')} ({res.get('duration_s', '?')}s)")

    # --- PHASE 2 : Replays 3x des taches eligibles ---
    replay_results_by_task: dict[str, list[dict]] = {}
    replayable = [r for r in capture_results if _is_replayable(r)]
    if progress_cb:
        progress_cb(f"REPLAY : {len(replayable)}/{len(capture_results)} taches rejouables x {replays}")

    replay_jobs = []
    for cap in replayable:
        spec = Path(cap["output_dir"]) / "test_playwright.spec.ts"
        for rep_idx in range(replays):
            replay_out = run_root / f"replay_{cap['task_id']}_run{rep_idx+1}"
            replay_jobs.append((cap["task_id"], rep_idx + 1, spec, replay_out))

    # Vagues de `workers` replays en parallele (plafond global respecte)
    for i in range(0, len(replay_jobs), workers):
        batch = replay_jobs[i : i + workers]
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            futs = {
                ex.submit(_run_replay, spec, out, replay_timeout_s): (tid, run_num)
                for (tid, run_num, spec, out) in batch
            }
            for fut in concurrent.futures.as_completed(futs):
                tid, run_num = futs[fut]
                res = fut.result()
                res["run_num"] = run_num
                replay_results_by_task.setdefault(tid, []).append(res)
                if progress_cb:
                    progress_cb(f"  replay {tid} #{run_num}: {res['status']} ({res.get('duration_s','?')}s)")

    ended_at = datetime.now()

    summary = {
        "started_at": started_at.isoformat(timespec="seconds"),
        "ended_at": ended_at.isoformat(timespec="seconds"),
        "duration_s": round((ended_at - started_at).total_seconds(), 1),
        "workers_max": workers,
        "replays_per_task": replays,
        "capture_timeout_s": capture_timeout_s,
        "tasks_total": len(tasks),
        "captures": capture_results,
        "replays": replay_results_by_task,
    }
    return summary
