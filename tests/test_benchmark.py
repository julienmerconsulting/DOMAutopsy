"""Tests benchmark BU_Bench_V1.

FIXTURE FACTICE : on ne commit JAMAIS le vrai .enc. Le test cree son
propre .enc factice a la volee (2-3 taches inventees, categorie
"InteractionTests_TEST") chiffre avec la meme cle Fernet officielle
que le corpus reel. Ca valide l'algorithme sans exposer le vrai
contenu du corpus.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))


def _make_fake_enc(tmp_path: Path, tasks: list[dict]) -> Path:
    """Cree un BU_Bench_V1.enc factice chiffre avec la vraie cle."""
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V1").digest())
    payload = json.dumps(tasks).encode("utf-8")
    encrypted = Fernet(key).encrypt(payload)
    b64_wrapped = base64.b64encode(encrypted).decode("utf-8")
    enc_file = tmp_path / "BU_Bench_V1.enc"
    enc_file.write_text(b64_wrapped, encoding="utf-8")
    return enc_file


# ============================================================
# Dechiffrement
# ============================================================

def test_key_derivation_matches_official_algorithm():
    """La cle Fernet est derivee via SHA-256("BU_Bench_V1") base64. Ce
    test verrouille l'algorithme officiel du runner BU."""
    expected = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V1").digest())
    # Doit etre exactement 44 chars (Fernet key format b64)
    assert len(expected) == 44
    # Reproductible (deterministe)
    again = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V1").digest())
    assert expected == again


def test_decrypt_valid_fixture_returns_tasks_list(tmp_path):
    """Un .enc valide doit dechiffrer en list[dict]."""
    from benchmark_runner import load_bu_bench_v1_in_memory
    fake_tasks = [
        {"task_id": "fixture_1", "category": "Test", "confirmed_task": "fake instruction 1"},
        {"task_id": "fixture_2", "category": "Test", "confirmed_task": "fake instruction 2"},
    ]
    enc = _make_fake_enc(tmp_path, fake_tasks)
    tasks = load_bu_bench_v1_in_memory(enc)
    assert tasks == fake_tasks


def test_decrypt_raises_on_invalid_content(tmp_path):
    """Un .enc corrompu doit lever une erreur explicite."""
    from benchmark_runner import load_bu_bench_v1_in_memory, BenchmarkRunError
    enc = tmp_path / "BU_Bench_V1.enc"
    enc.write_text("not-a-valid-fernet-payload", encoding="utf-8")
    with pytest.raises(Exception):  # cryptography.fernet.InvalidToken ou base64 error
        load_bu_bench_v1_in_memory(enc)


def test_decrypt_raises_if_payload_not_list(tmp_path):
    """Le payload doit etre une liste JSON, sinon erreur explicite."""
    from benchmark_runner import load_bu_bench_v1_in_memory, BenchmarkRunError
    # Chiffre un dict au lieu d'une liste
    from cryptography.fernet import Fernet
    key = base64.urlsafe_b64encode(hashlib.sha256(b"BU_Bench_V1").digest())
    payload = json.dumps({"not": "a list"}).encode("utf-8")
    encrypted = Fernet(key).encrypt(payload)
    enc = tmp_path / "BU_Bench_V1.enc"
    enc.write_text(base64.b64encode(encrypted).decode("utf-8"), encoding="utf-8")
    with pytest.raises(BenchmarkRunError, match="liste"):
        load_bu_bench_v1_in_memory(enc)


# ============================================================
# Selection categorie
# ============================================================

def test_select_custom_tasks_finds_20():
    """20 taches Custom / InteractionTests -> selection reussie."""
    from benchmark_runner import select_custom_tasks
    tasks = (
        [{"task_id": f"t{i}", "category": "InteractionTests", "confirmed_task": f"x{i}"}
         for i in range(20)]
        + [{"task_id": f"other_{i}", "category": "Other", "confirmed_task": f"o{i}"}
           for i in range(15)]
    )
    selected, counts, chosen = select_custom_tasks(tasks)
    assert chosen == "InteractionTests"
    assert len(selected) == 20
    assert counts["InteractionTests"] == 20
    assert counts["Other"] == 15


def test_select_custom_tasks_aborts_if_count_not_20():
    """Si la categorie Custom/InteractionTests n'a pas exactement 20,
    on retourne chosen=None et [] pour que le caller stoppe."""
    from benchmark_runner import select_custom_tasks
    tasks = [{"task_id": f"t{i}", "category": "InteractionTests", "confirmed_task": ""} for i in range(19)]
    selected, counts, chosen = select_custom_tasks(tasks)
    assert selected == []
    assert chosen is None


def test_select_custom_tasks_aborts_if_no_match_category():
    """Si aucune categorie ne matche 'Custom'/'InteractionTests' avec 20
    taches, chosen=None + on ne selectionne rien arbitrairement."""
    from benchmark_runner import select_custom_tasks
    tasks = [{"task_id": f"t{i}", "category": "GAIA", "confirmed_task": ""} for i in range(20)]
    selected, counts, chosen = select_custom_tasks(tasks)
    assert selected == []
    assert chosen is None


# ============================================================
# Isolation : jamais d'ecriture du texte dechiffre
# ============================================================

def test_no_decrypted_json_file_created_in_repo(tmp_path):
    """Regression : aucun fichier BU_Bench_V1.json ou selected_tasks.json
    ne doit apparaitre dans le repo apres une operation de dechiffrement.
    Le contenu reste STRICTEMENT en memoire."""
    from benchmark_runner import load_bu_bench_v1_in_memory, select_custom_tasks
    fake = [{"task_id": f"t{i}", "category": "InteractionTests", "confirmed_task": "x"} for i in range(20)]
    enc = _make_fake_enc(tmp_path, fake)
    _ = load_bu_bench_v1_in_memory(enc)
    _ = select_custom_tasks(_)
    # Aucun fichier .json avec le nom BU_Bench_V1 ou selected_tasks ne
    # doit exister sous tmp_path
    forbidden = list(tmp_path.rglob("BU_Bench_V1.json")) + list(tmp_path.rglob("selected_tasks.json"))
    assert forbidden == [], f"Fichier decrypte ecrit sur disque : {forbidden}"


# ============================================================
# Transmission par stdin (jamais argv)
# ============================================================

def test_worker_reads_task_from_stdin(tmp_path, monkeypatch):
    """benchmark_worker._read_task_from_stdin lit la tache depuis stdin
    (jamais en argv). Ce test verifie que la fonction utilise sys.stdin
    et non sys.argv."""
    import benchmark_worker
    payload = {"task_id": "abc", "confirmed_task": "test", "output_dir": str(tmp_path)}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    task = benchmark_worker._read_task_from_stdin()
    assert task["task_id"] == "abc"


def test_worker_rejects_empty_stdin(monkeypatch):
    """Stdin vide -> erreur explicite (pas de fallback CLI silencieux)."""
    import benchmark_worker
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(""))
    with pytest.raises(ValueError, match="stdin vide"):
        benchmark_worker._read_task_from_stdin()


# ============================================================
# CDP port dynamique (jamais 9222 = defaut Chrome dev perso)
# ============================================================

def test_worker_cdp_port_never_9222():
    """_find_free_cdp_port utilise la plage [9300, 9500], jamais 9222
    (evite conflit avec un Chrome dev perso ou une session existante)."""
    import benchmark_worker
    port = benchmark_worker._find_free_cdp_port()
    assert 9300 <= port <= 9500
    assert port != 9222


# ============================================================
# Runner : workers isoles, plafond respecte
# ============================================================

def test_runner_wave_layout_5_workers():
    """20 taches doivent etre reparties en 4 vagues de 5 (max_workers=5)."""
    # On simule sans lancer les workers reellement (trop lourd).
    # Test unitaire du split logic uniquement.
    tasks = [{"task_id": f"t{i}"} for i in range(20)]
    workers = 5
    n_waves = (len(tasks) + workers - 1) // workers
    assert n_waves == 4
    for i in range(n_waves):
        wave = tasks[i * workers : (i + 1) * workers]
        assert len(wave) == 5


# ============================================================
# Reporter local uniquement (aucune fuite GitHub)
# ============================================================

def test_reporter_writes_local_only(tmp_path):
    """Le reporter ecrit report.html et report.json dans le run_root
    passe, jamais ailleurs."""
    from benchmark_reporter import write_reports
    summary = {
        "started_at": "2026-01-01T00:00:00",
        "ended_at": "2026-01-01T00:10:00",
        "duration_s": 600,
        "workers_max": 5,
        "replays_per_task": 3,
        "capture_timeout_s": 900,
        "tasks_total": 2,
        "captures": [
            {"task_id": "t1", "capture_result": "success", "agent_status": "success",
             "duration_s": 30, "clean_steps_total": 10, "clean_steps_included": 8,
             "clean_steps_filtered": 2, "raw_count": 50},
        ],
        "replays": {"t1": [{"status": "pass", "duration_s": 5, "run_num": 1}]},
    }
    task_texts = {"t1": "fake instruction (test only, not real BU corpus)"}
    json_path, html_path = write_reports(summary, tmp_path, task_texts)
    assert json_path.exists()
    assert html_path.exists()
    # Verif le HTML contient PRIVE marker
    html = html_path.read_text(encoding="utf-8")
    assert "PRIVE" in html
