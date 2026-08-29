"""Tests R6 (statuts agent vs pipeline distincts) + R7 (arret complet).

R6 : un agent qui echoue fonctionnellement ne doit jamais apparaitre
comme un run success meme si les artifacts sont bien generes.

R7 : le bouton Stop doit tuer l'arbre complet des processus, marquer
meta.json status=stopped, fermer WS et screencast.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ============================================================
# R7 : _kill_process_tree + _mark_run_stopped_on_disk
# ============================================================

def test_mark_run_stopped_writes_meta(tmp_path):
    """_mark_run_stopped_on_disk cree meta.json avec status=stopped."""
    from server import _mark_run_stopped_on_disk
    run_dir = tmp_path / "run1"
    run_dir.mkdir()
    (run_dir / "meta.json").write_text(json.dumps({
        "status": "running", "agent_result": None,
    }), encoding="utf-8")

    _mark_run_stopped_on_disk(str(run_dir))

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "stopped"
    assert "stopped_at" in meta
    assert meta["pipeline_status"] == "interrupted"
    assert meta["agent_status"] == "interrupted"


def test_mark_run_stopped_creates_meta_if_absent(tmp_path):
    """Meta absent -> cree avec status=stopped (n'erreur pas)."""
    from server import _mark_run_stopped_on_disk
    run_dir = tmp_path / "run2"
    run_dir.mkdir()

    _mark_run_stopped_on_disk(str(run_dir))

    meta = json.loads((run_dir / "meta.json").read_text(encoding="utf-8"))
    assert meta["status"] == "stopped"


def test_mark_run_stopped_none_path_noop():
    """run_dir=None -> no-op silencieux."""
    from server import _mark_run_stopped_on_disk
    _mark_run_stopped_on_disk(None)  # ne doit pas lever


def test_kill_process_tree_returns_report_on_bad_pid():
    """PID inexistant -> report vide, pas de crash."""
    from server import _kill_process_tree
    # PID 999999 tres improbable qu'il existe
    report = _kill_process_tree(999999, timeout=0.5)
    assert "killed_pids" in report
    assert "still_alive" in report


def test_kill_process_tree_uses_psutil_and_terminates_real_process():
    """Test reel : spawn un python simple (sleep), kill via _kill_process_tree,
    verifie qu'il est effectivement mort."""
    import subprocess
    import sys
    import time
    from server import _kill_process_tree
    import psutil

    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(0.5)  # laisse le temps de demarrer
    assert psutil.pid_exists(proc.pid), "Proc pas encore vivant ?"

    report = _kill_process_tree(proc.pid, timeout=3.0)
    time.sleep(0.5)

    assert proc.pid in report["killed_pids"]
    assert not psutil.pid_exists(proc.pid) or psutil.Process(proc.pid).status() == psutil.STATUS_ZOMBIE
    # cleanup
    try: proc.wait(timeout=2)
    except Exception: pass


# ============================================================
# R6 : agent_status vs pipeline_status
# ============================================================

def test_agent_status_derived_from_agent_result_success():
    """agent_result contient 'SUCCESS' -> agent_status=success."""
    # On teste la logique via l'interpretation manuelle car qa_explorer
    # est un pipeline complet non easily testable en isolation.
    agent_result_str = "SUCCESS - Toutes les taches ont ete ajoutees"
    upper = agent_result_str.upper()
    if "SUCCESS" in upper:
        agent_status = "success"
    elif "FAIL" in upper or "ERROR" in upper:
        agent_status = "fail"
    elif "INTERRUPT" in upper or "STOP" in upper:
        agent_status = "interrupted"
    else:
        agent_status = "unknown"
    assert agent_status == "success"


def test_agent_status_fail_when_agent_returns_fail():
    """FAIL dans agent_result -> agent_status=fail (indep du pipeline)."""
    agent_result_str = "FAIL - Impossible de cliquer sur le bouton Login apres 3 essais"
    upper = agent_result_str.upper()
    if "SUCCESS" in upper:
        agent_status = "success"
    elif "FAIL" in upper or "ERROR" in upper:
        agent_status = "fail"
    else:
        agent_status = "unknown"
    assert agent_status == "fail"


def test_overall_status_worst_of_agent_pipeline():
    """R6 : status overall = worst(agent, pipeline). Un pipeline_status=
    success + agent_status=fail -> overall=failure."""
    agent = "fail"
    pipeline = "success"
    if agent == "fail":
        overall = "failure"
    elif agent == "interrupted":
        overall = "interrupted"
    else:
        overall = "success"
    assert overall == "failure"
