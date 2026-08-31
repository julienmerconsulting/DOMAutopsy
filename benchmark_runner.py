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
import socket
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any


def _allocate_unique_cdp_ports(n: int, start: int = 9300, end: int = 9500) -> list[int]:
    """Alloue N ports CDP UNIQUES et non-collidants en un seul passage
    single-thread. Bind chaque port dans une socket qu'on garde ouverte
    JUSQU'A AVOIR ALLOUE LES N PORTS, puis on les libere tous ensemble.
    Ca elimine la race ou 2 workers appellent chacun 'find_free' et
    obtiennent le meme port entre bind et return."""
    sockets = []
    ports: list[int] = []
    try:
        port = start
        while len(ports) < n and port < end:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                s.bind(("127.0.0.1", port))
                sockets.append(s)
                ports.append(port)
            except OSError:
                s.close()
            port += 1
        if len(ports) < n:
            raise RuntimeError(f"Impossible d'allouer {n} ports libres dans [{start},{end})")
        return ports
    finally:
        for s in sockets:
            try: s.close()
            except: pass


ROOT = Path(__file__).parent
RUNS_ROOT = ROOT / ".bu_bench_runs"

# Categories acceptees pour les "Custom / Page interaction challenges"
CUSTOM_CATEGORY_NAMES = ("Custom", "InteractionTests", "Custom / Page interaction challenges", "custom")
EXPECTED_TASKS_COUNT = 20

DEFAULT_MAX_WORKERS = 5
DEFAULT_CAPTURE_TIMEOUT_S = 1800  # 30 min par tache = defaut officiel BU benchmark
                                  # (frameworks/__init__.py:DEFAULT_TASK_TIMEOUT)
DEFAULT_REPLAY_TIMEOUT_S = 180
DEFAULT_REPLAYS = 3


class BenchmarkRunError(Exception):
    pass


# ============================================================
# Dechiffrement OFFICIEL (Fernet, cle = SHA-256("BU_Bench_V1") en b64)
# ============================================================

def load_real_world_7() -> list[dict]:
    """Charge le corpus DOMAutopsy Real-World 7 depuis benchmarks/real_world_7.json.

    Complementaire au BU_Bench_V1 : 7 scenarios QA realistes (saucedemo,
    automationexercise, demoblaze, parabank, heroku login, demoqa text-box,
    todomvc). Chaque tache declare un oracle final deterministe. Le fichier
    est LIBRE (pas chiffre) car ce sont des sites publics."""
    corpus_path = ROOT / "benchmarks" / "real_world_7.json"
    if not corpus_path.exists():
        raise BenchmarkRunError(f"Corpus Real-World 7 absent : {corpus_path}")
    data = json.loads(corpus_path.read_text(encoding="utf-8"))
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise BenchmarkRunError("real_world_7.json : cle 'tasks' absente ou vide")
    return tasks


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

def _run_capture_worker(task: dict, output_dir: Path, timeout_s: float, cdp_port: int | None = None) -> dict:
    """Lance benchmark_worker.py en subprocess, transmet la tache par
    STDIN (pas argv). Retourne le dict resultat que le worker a emit
    sur son stdout. `cdp_port` alloue par le runner (single-thread,
    non-collidant) - passer None seulement pour tests unitaires."""
    output_dir.mkdir(parents=True, exist_ok=True)
    # Preserve task["start_url"] du corpus (real_world_7.json a des URLs
    # cibles precises comme https://www.saucedemo.com/). Un fallback
    # "about:blank" est ajoute UNIQUEMENT si la task n'en declare pas.
    payload = {
        **task,
        "output_dir": str(output_dir),
        "start_url": task.get("start_url") or "about:blank",
        "model": "gpt-5-mini",
        "timeout_s": timeout_s,
    }
    if cdp_port is not None:
        payload["cdp_port"] = cdp_port
    env = dict(os.environ)
    cmd = [sys.executable, str(ROOT / "benchmark_worker.py")]
    # Streaming direct sur disque : Popen + fichiers ouverts + python -u.
    # Permet de tail -f worker_stdout.txt / stderr.txt pendant que le
    # worker tourne, plutot que d'attendre sa fin pour voir un dump.
    # cmd inclut -u (unbuffered stdin/stdout/stderr Python).
    stdout_path = output_dir / "worker_stdout.txt"
    stderr_path = output_dir / "worker_stderr.txt"
    cmd_stream = [sys.executable, "-u", str(ROOT / "benchmark_worker.py")]
    try:
        with open(stdout_path, "w", encoding="utf-8", errors="replace") as fout, \
             open(stderr_path, "w", encoding="utf-8", errors="replace") as ferr:
            proc = subprocess.Popen(
                cmd_stream, stdin=subprocess.PIPE, stdout=fout, stderr=ferr,
                env=env, cwd=str(ROOT), text=True, encoding="utf-8", errors="replace",
            )
            try:
                proc.communicate(input=json.dumps(payload), timeout=timeout_s + 60)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
                return {
                    "task_id": task.get("task_id"),
                    "capture_result": "timeout",
                    "error": f"Parent subprocess timeout apres {timeout_s + 60}s",
                    "output_dir": str(output_dir),
                }
        # Extrait la derniere ligne JSON du stdout ecrit par le worker
        try:
            stdout_txt = stdout_path.read_text(encoding="utf-8", errors="replace")
            last_line = stdout_txt.strip().split("\n")[-1]
            result = json.loads(last_line)
        except (json.JSONDecodeError, IndexError, FileNotFoundError):
            tail = stdout_txt[-300:] if 'stdout_txt' in locals() else ""
            return {
                "task_id": task.get("task_id"),
                "capture_result": "infrastructure_error",
                "error": f"stdout worker non-JSON. exit={proc.returncode} tail={tail}",
                "output_dir": str(output_dir),
            }
        return result
    except Exception as e:
        return {
            "task_id": task.get("task_id"),
            "capture_result": "infrastructure_error",
            "error": f"{type(e).__name__}: {e}",
            "output_dir": str(output_dir),
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
    try:
        oracle_present = "[oracle]" in spec_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        oracle_present = False
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
        # Detection du step qui a fail : si [oracle] apparait dans un bloc
        # d'erreur, on marque oracle_pass=False ; sinon oracle_pass=None
        # (fail sur un autre step, avant meme d'atteindre l'oracle).
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        combined = stdout + "\n" + stderr
        oracle_step_failed = oracle_present and ("[oracle]" in combined) and (proc.returncode != 0)
        oracle_pass: bool | None
        if proc.returncode == 0 and oracle_present:
            oracle_pass = True
        elif oracle_step_failed:
            oracle_pass = False
        else:
            oracle_pass = None  # fail sur un step non-oracle, oracle jamais atteint
        return {
            "status": "pass" if proc.returncode == 0 else "fail",
            "returncode": proc.returncode,
            "oracle_present": oracle_present,
            "oracle_pass": oracle_pass,
            "duration_s": round(time.monotonic() - t0, 1),
            "error": None if proc.returncode == 0 else combined[-1000:],
        }
    except subprocess.TimeoutExpired:
        return {
            "status": "timeout",
            "duration_s": round(time.monotonic() - t0, 1),
            "error": f"Replay timeout apres {timeout_s}s",
        }


def _is_replayable(capture_result: dict) -> bool:
    """Valide qu'un artefact merite reellement d'entrer dans le replay."""
    if capture_result.get("capture_result") != "success":
        return False
    if capture_result.get("agent_status") != "success":
        return False
    if not capture_result.get("clean_steps_included"):
        return False
    if int(capture_result.get("replay_blocking_steps") or 0) > 0:
        return False
    if int(capture_result.get("playwright_unsupported_count") or 0) > 0:
        return False
    if capture_result.get("oracle_required") and capture_result.get("oracle_asserted") is not True:
        return False
    od = capture_result.get("output_dir")
    if not od:
        return False
    return (Path(od) / "test_playwright.spec.ts").exists()


# ============================================================
# Orchestration : pipeline continu (pool de N slots)
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
    """Pipeline continu de N workers concurrents (defaut 5). Des qu'une
    tache termine, la suivante est lancee immediatement -> toujours N en
    vol tant qu'il reste des taches en file. Elimine le temps mort des
    vagues (ou la wave etait bloquee par sa tache la plus lente).
    Puis phase 2 : replays des taches eligibles (memes N slots).
    """
    run_root.mkdir(parents=True, exist_ok=True)
    started_at = datetime.now()

    # --- PHASE 1 : Captures en pipeline continu ---
    capture_results: list[dict] = []
    total = len(tasks)

    # Pre-alloue UN port unique par tache (bind simultane single-thread ->
    # aucun TOCTOU entre workers). Assignation stable index->port.
    all_ports = _allocate_unique_cdp_ports(total, start=9300)
    if progress_cb:
        progress_cb(f"CAPTURE pipeline : {total} taches, {workers} slots concurrents")
        progress_cb(f"  ports CDP alloues : {all_ports[0]}..{all_ports[-1]}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {}
        for i, task in enumerate(tasks):
            task_id = task.get("task_id") or f"t{i:02d}"
            safe_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(task_id))[:40]
            out = run_root / f"capture_{safe_id}"
            futures[ex.submit(_run_capture_worker, task, out, capture_timeout_s, all_ports[i])] = task
        # as_completed re-emet des que N'IMPORTE lequel finit. Le pool
        # de N=workers threads reutilise automatiquement le slot libere.
        done_count = 0
        for fut in concurrent.futures.as_completed(futures):
            res = fut.result()
            capture_results.append(res)
            done_count += 1
            if progress_cb:
                progress_cb(f"  [{done_count}/{total}] {res.get('task_id')} : {res.get('capture_result')} ({res.get('duration_s','?')}s)")

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

    # Detection heuristique automatique des incidents runner : ecrit un
    # runner_incident.json signalant les taches suspectes de contamination
    # (kill externe, perte CDP, etc.), pour orienter un 'benchmark rerun'.
    incident = _detect_runner_incident(capture_results, run_root)
    if incident and progress_cb:
        progress_cb(f"[incident] {len(incident['suspect_task_ids'])} taches suspectes -> {incident['suggested_command']}")

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
        "runner_incident": incident,
    }
    return summary


# ============================================================
# Detection automatique d'incident runner
# ============================================================

_INCIDENT_KEYWORDS = (
    "CDP", "cdp", "Connection", "connection lost", "closed", "aborted",
    "killed", "SIGTERM", "SIGKILL", "BrowserType.launch", "Target closed",
    "TargetClosedError", "WebSocket", "protocol", "Session closed",
)

_INCIDENT_MIN_INFRA_RATIO = 0.20   # >= 20% de taches en infra_error = suspect
_INCIDENT_MIN_ABSOLUTE = 2          # ou >= 2 infra_error avec messages suspects


def _detect_runner_incident(capture_results: list[dict], run_root: Path) -> dict | None:
    """Analyse les capture_results et retourne un dict incident si des
    signes de contamination (kill externe, perte CDP, connection reset)
    sont detectes. Ecrit runner_incident.json dans run_root en parallele.

    Retourne None si aucun signe suspect. Ne bloque pas le run.
    Heuristiques :
      H1  taux d'infrastructure_error >= 20% (seuil arbitraire, pouvant
          etre serre plus tard avec des donnees historiques)
      H2  au moins 2 infra_errors dont le message contient un mot-cle
          suspect (Connection, CDP, closed, killed, WebSocket, etc.)
    """
    infra_errors = [r for r in capture_results if r.get("capture_result") == "infrastructure_error"]
    if not infra_errors or not capture_results:
        return None

    ratio = len(infra_errors) / len(capture_results)
    suspicious_messages = []
    for r in infra_errors:
        msg = str(r.get("error") or "")
        if any(k in msg for k in _INCIDENT_KEYWORDS):
            suspicious_messages.append({"task_id": r.get("task_id"), "error_snippet": msg[:300]})

    if ratio < _INCIDENT_MIN_INFRA_RATIO and len(suspicious_messages) < _INCIDENT_MIN_ABSOLUTE:
        return None

    suspect_task_ids = [r.get("task_id") for r in infra_errors]
    incident = {
        "type": "runner_kill_contamination_suspected",
        "detected_at": datetime.now().isoformat(timespec="seconds"),
        "captures_total": len(capture_results),
        "infra_error_count": len(infra_errors),
        "infra_error_ratio": round(ratio, 3),
        "suspect_task_ids": suspect_task_ids,
        "suspicious_messages": suspicious_messages[:20],
        "reasoning": (
            f"H1_ratio={ratio >= _INCIDENT_MIN_INFRA_RATIO} "
            f"H2_keywords={len(suspicious_messages) >= _INCIDENT_MIN_ABSOLUTE}"
        ),
        "suggested_command": (
            f"py -3.12 domautopsy_cli.py benchmark rerun --tasks "
            f"{','.join(suspect_task_ids)} --reason contamination_suspected"
        ),
        "note": "Fichier LOCAL uniquement (.bu_bench_runs/ gitignore).",
    }
    try:
        (run_root / "runner_incident.json").write_text(
            json.dumps(incident, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception:
        pass
    return incident
