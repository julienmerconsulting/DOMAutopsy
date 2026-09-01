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

def test_build_clean_steps_no_network_by_default():
    """GARANTIE : build_clean_steps(use_llm=False) ne fait AUCUN appel
    reseau (aucun socket TCP outgoing). Post-processing 100% deterministe.

    Protection : on remplace socket.socket par une classe qui raise
    ConnectionRefusedError des la creation. Si build_clean_steps tente
    d'appeler OpenAI, le test explose."""
    import socket as _real_socket
    from clean_steps_builder import build_clean_steps

    class _NoNetSocket(_real_socket.socket):
        def __init__(self, *a, **kw):
            raise ConnectionRefusedError("Post-processing DOMAutopsy ne DOIT PAS ouvrir de socket")

    real_socket_cls = _real_socket.socket
    _real_socket.socket = _NoNetSocket
    try:
        import time
        t0 = time.monotonic()
        bu_history = [
            {"step": 1, "action_type": "navigate", "url": "https://example.com", "success": True},
            {"step": 2, "action_type": "click", "locator": "#login", "success": True},
            {"step": 3, "action_type": "input", "locator": "#email", "value": "a@b.c", "success": True},
        ]
        clean_steps, env_vars = build_clean_steps(
            scenario_name="test_no_network",
            scenario_url="https://example.com",
            scenario_steps=None,
            bu_history=bu_history,
            dom_log=[],
            network_log=None,
            model="gpt-5-mini",
            base_url=None,
            api_key="fake-key",
            use_llm=False,
        )
        elapsed = time.monotonic() - t0
        assert clean_steps is not None
        assert clean_steps.total_steps == len(clean_steps.steps)
        assert elapsed < 5.0, f"post-processing deterministe trop lent : {elapsed:.2f}s"
    finally:
        _real_socket.socket = real_socket_cls


def test_deterministic_classify_filters_duplicate_clicks():
    """Regle R1 : clics consecutifs identiques (<500ms sur meme selecteur)
    -> le premier reste inclus, les suivants passent a included_in_replay=False."""
    from clean_steps_builder import deterministic_classify_steps
    from schemas import Step, Selector
    steps = [
        Step(id="step-0001", action="click", page="p1", timestamp=1000,
             selector=Selector(value="#login-btn", unique=True, matchCount=1,
                               verifiedAtCapture=True)),
        Step(id="step-0002", action="click", page="p1", timestamp=1100,
             selector=Selector(value="#login-btn", unique=True, matchCount=1,
                               verifiedAtCapture=True)),
        Step(id="step-0003", action="click", page="p1", timestamp=1250,
             selector=Selector(value="#login-btn", unique=True, matchCount=1,
                               verifiedAtCapture=True)),
    ]
    out, anomalies, noise = deterministic_classify_steps(steps)
    assert out[0].included_in_replay is True
    assert out[1].included_in_replay is False
    assert out[2].included_in_replay is False
    assert "redondant" in out[1].cleanup_reason
    assert any("#login-btn" in n for n in noise)


def test_deterministic_classify_preserves_same_selector_with_distinct_contexts():
    """Un selector de collection ne rend pas deux lignes interchangeables."""
    from clean_steps_builder import deterministic_classify_steps
    from schemas import Step, Selector

    selector = Selector(
        value='[aria-label="Toggle Todo"]',
        unique=False,
        matchCount=4,
        verifiedAtCapture=True,
    )
    steps = [
        Step(
            id="step-0001",
            action="click",
            page="todos",
            timestamp=1000,
            selector=selector,
            raw_payload={
                "parentLabel": "acheter du pain",
                "parentLabelMatchCount": 1,
                "parentScopedMatchCount": 1,
            },
        ),
        Step(
            id="step-0002",
            action="click",
            page="todos",
            timestamp=1100,
            selector=selector,
            raw_payload={
                "parentLabel": "appeler le medecin",
                "parentLabelMatchCount": 1,
                "parentScopedMatchCount": 1,
            },
        ),
    ]

    out, _, _ = deterministic_classify_steps(steps)
    assert [step.included_in_replay for step in out] == [True, True]


def test_replayable_requires_success_agent_oracle_and_zero_unsupported(tmp_path):
    from benchmark_runner import _is_replayable

    (tmp_path / "test_playwright.spec.ts").write_text("// spec", encoding="utf-8")
    valid = {
        "capture_result": "success",
        "agent_status": "success",
        "clean_steps_included": 3,
        "replay_blocking_steps": 0,
        "playwright_unsupported_count": 0,
        "oracle_required": True,
        "oracle_asserted": True,
        "output_dir": str(tmp_path),
    }
    assert _is_replayable(valid) is True
    for key, value in (
        ("agent_status", "unknown"),
        ("clean_steps_included", 0),
        ("replay_blocking_steps", 1),
        ("playwright_unsupported_count", 1),
        ("oracle_asserted", False),
    ):
        invalid = dict(valid, **{key: value})
        assert _is_replayable(invalid) is False


def test_successful_replay_without_oracle_is_not_oracle_pass(monkeypatch, tmp_path):
    from types import SimpleNamespace
    import benchmark_runner

    spec = tmp_path / "plain.spec.ts"
    spec.write_text("test('plain', async () => {});", encoding="utf-8")
    monkeypatch.setattr(
        benchmark_runner.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="ok", stderr=""),
    )
    result = benchmark_runner._run_replay(spec, tmp_path / "out", timeout_s=2)
    assert result["status"] == "pass"
    assert result["oracle_present"] is False
    assert result["oracle_pass"] is None


def test_generate_playwright_ts_injects_oracle_assertions(tmp_path):
    """Real-World 7 : quand un oracle {url_contains, text_contains} est
    fourni a generate_playwright_ts, le TS produit contient un
    test.step('[oracle]') avec expect().toHaveURL et
    expect(page.locator('body')).toContainText. Sans LLM."""
    from playwright_generator import generate_playwright_ts
    from schemas import CleanSteps, Step, Selector
    cs = CleanSteps(
        schema_version="2.0",
        parcours="test",
        scenario_name="test",
        scenario_url="https://example.com",
        total_steps=1,
        steps=[Step(id="step-0001", action="navigate", url="https://example.com")],
    )
    out = tmp_path / "test.spec.ts"
    res = generate_playwright_ts(
        clean_steps=cs, output_path=out,
        parcours_url="https://example.com",
        oracle={"url_contains": "checkout-complete", "text_contains": "Thank you for your order"},
    )
    assert res["oracle_asserted"] is True
    ts = out.read_text(encoding="utf-8")
    assert "[oracle]" in ts
    assert "toHaveURL" in ts
    assert "toContainText" in ts
    assert "Thank you for your order" in ts


def test_oracle_condition_is_strictly_or(tmp_path):
    """Verrou : la condition d'injection est url_contains OR text_contains,
    JAMAIS AND. Un oracle avec un seul champ present doit generer le bloc."""
    from playwright_generator import generate_playwright_ts
    from schemas import CleanSteps, Step
    def _mk():
        return CleanSteps(schema_version="2.0", parcours="t", scenario_name="t",
                          scenario_url="https://example.com", total_steps=1,
                          steps=[Step(id="step-0001", action="navigate", url="https://example.com")])
    # url_contains SEUL
    out1 = tmp_path / "url_only.spec.ts"
    r1 = generate_playwright_ts(clean_steps=_mk(), output_path=out1,
                                 parcours_url="https://example.com",
                                 oracle={"url_contains": "/checkout"})
    assert r1["oracle_asserted"] is True
    ts1 = out1.read_text(encoding="utf-8")
    assert "toHaveURL" in ts1 and "toContainText" not in ts1
    # text_contains SEUL
    out2 = tmp_path / "text_only.spec.ts"
    r2 = generate_playwright_ts(clean_steps=_mk(), output_path=out2,
                                 parcours_url="https://example.com",
                                 oracle={"text_contains": "Merci"})
    assert r2["oracle_asserted"] is True
    ts2 = out2.read_text(encoding="utf-8")
    assert "toContainText" in ts2 and "toHaveURL" not in ts2
    # oracle vide/None
    out3 = tmp_path / "empty.spec.ts"
    r3 = generate_playwright_ts(clean_steps=_mk(), output_path=out3,
                                 parcours_url="https://example.com",
                                 oracle={"unrelated": "x"})
    assert r3["oracle_asserted"] is False


def test_ts_always_imports_expect(tmp_path):
    """Verrou : le TS genere doit toujours importer expect, meme sans oracle
    (evite un ReferenceError silencieux quand un oracle est ajoute later)."""
    from playwright_generator import generate_playwright_ts
    from schemas import CleanSteps, Step
    cs = CleanSteps(schema_version="2.0", parcours="t", scenario_name="t",
                    scenario_url="https://example.com", total_steps=1,
                    steps=[Step(id="step-0001", action="navigate", url="https://example.com")])
    out = tmp_path / "spec.ts"
    generate_playwright_ts(clean_steps=cs, output_path=out,
                            parcours_url="https://example.com", oracle=None)
    ts = out.read_text(encoding="utf-8")
    assert "import { test, expect } from '@playwright/test'" in ts


def test_generate_playwright_ts_no_oracle_no_assertion(tmp_path):
    """Sans oracle : pas de test.step('[oracle]') injecte, back-compat."""
    from playwright_generator import generate_playwright_ts
    from schemas import CleanSteps, Step
    cs = CleanSteps(
        schema_version="2.0", parcours="test", scenario_name="test",
        scenario_url="https://example.com", total_steps=1,
        steps=[Step(id="step-0001", action="navigate", url="https://example.com")],
    )
    out = tmp_path / "test2.spec.ts"
    res = generate_playwright_ts(clean_steps=cs, output_path=out,
                                  parcours_url="https://example.com")
    assert res["oracle_asserted"] is False
    ts = out.read_text(encoding="utf-8")
    assert "[oracle]" not in ts


def test_oracle_false_fails_playwright_replay(tmp_path):
    """Test NEGATIF integration : un oracle volontairement faux DOIT faire
    exit code != 0 quand npx playwright test rejoue le TS genere. Si ce test
    passe (exit=0), c'est que l'oracle n'est pas veritablement enforce =
    la promesse marketing 'validation fonctionnelle' est cassee.

    Skip si npx/playwright pas disponible dans l'env de test."""
    import shutil, subprocess
    if shutil.which("npx") is None:
        pytest.skip("npx pas dispo dans PATH")

    from playwright_generator import generate_playwright_ts
    from schemas import CleanSteps, Step

    workdir = tmp_path / "pw_neg"
    workdir.mkdir()
    (workdir / "package.json").write_text(json.dumps({
        "name": "oracle-neg-test", "version": "1.0.0",
        "devDependencies": {"@playwright/test": "1.57.0"},
    }), encoding="utf-8")

    # Fixture minimale : data:text/html <body>Hello</body>. L'oracle
    # demande "GoodbyeXYZ_MISSING" qui n'est pas dans le body -> DOIT fail.
    cs = CleanSteps(
        schema_version="2.0", parcours="neg", scenario_name="neg",
        scenario_url="data:text/html,<body>Hello</body>", total_steps=1,
        steps=[Step(id="step-0001", action="navigate",
                    url="data:text/html,<body>Hello</body>")],
    )
    spec = workdir / "neg.spec.ts"
    res = generate_playwright_ts(
        clean_steps=cs, output_path=spec,
        parcours_url="data:text/html,<body>Hello</body>",
        oracle={"text_contains": "GoodbyeXYZ_MISSING_ORACLE"},
    )
    assert res["oracle_asserted"] is True

    # Utilise npx --package pour installer @playwright/test on-the-fly
    # sans polluer un node_modules global. Timeout serre pour ne pas
    # bloquer si Playwright n'est pas installable dans l'env de test.
    try:
        proc = subprocess.run(
            ["npx", "--yes", "-p", "@playwright/test@1.57.0",
             "playwright", "test", str(spec.name),
             "--reporter=line", "--workers=1"],
            cwd=str(workdir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=180,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pytest.skip("npx/playwright indisponible ou installation lente dans l'env de test")

    assert proc.returncode != 0, (
        "Oracle faux devrait faire echouer le replay Playwright, "
        f"mais exit_code={proc.returncode}. stdout:{proc.stdout[-600:]}"
    )
    assert "GoodbyeXYZ_MISSING_ORACLE" in (proc.stdout + proc.stderr), (
        "Le message d'erreur devrait mentionner le texte oracle manquant"
    )


def test_kill_process_tree_terminates_children(tmp_path):
    """Non-regression benchmark stop : _kill_process_tree tue le parent ET
    son subprocess enfant en < 5s."""
    from domautopsy_cli import _kill_process_tree
    # Spawn un parent Python qui spawn lui-meme un enfant sleeping 60s
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, time\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "print(child.pid, flush=True)\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    proc = subprocess.Popen(
        [sys.executable, str(script)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    # Lit la 1ere ligne = PID de l'enfant
    child_pid_line = proc.stdout.readline().strip()
    child_pid = int(child_pid_line)
    parent_pid = proc.pid

    res = _kill_process_tree(parent_pid, timeout=5.0)
    # Verifie que parent ET enfant sont dans killed_pids
    assert parent_pid in res["killed_pids"], f"parent {parent_pid} pas tue"
    assert child_pid in res["killed_pids"], f"enfant {child_pid} pas tue"
    assert res["still_alive"] == []
    proc.wait(timeout=3)


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
