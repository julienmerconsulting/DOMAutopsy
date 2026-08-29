"""Tests cahier des charges :
  #12 execution de `npx playwright test` par /api/replay
  #13 absence d'appel a qa_player.py pour un nouveau run
  #14 fonctionnement du fallback legacy
  #15 securite des chemins et arguments

FastAPI TestClient + monkeypatch de subprocess.Popen : on n'exec pas
un vrai npx, on verifie ce que serveur INSTRUIT au subprocess.
"""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def app_with_stub_popen(monkeypatch, tmp_path):
    """Isole RUNS_DIR sur tmp_path + stub subprocess.Popen pour capturer
    les commandes lancees par /api/replay."""
    import server as srv

    monkeypatch.setattr(srv, "RUNS_DIR", tmp_path)
    monkeypatch.setattr(srv, "ROOT", tmp_path)
    # Reset RUNS pour ne pas polluer entre tests
    monkeypatch.setattr(srv, "RUNS", {})

    captured = {"calls": []}

    def _fake_popen(*args, **kwargs):
        captured["calls"].append({
            "args": args[0] if args else kwargs.get("args"),
            "kwargs": {k: v for k, v in kwargs.items() if k not in ("stdout", "stderr")},
        })
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        mock_proc.stdout = MagicMock()
        mock_proc.stdout.readline.return_value = b""  # EOF immediat
        mock_proc.returncode = 0
        mock_proc.wait.return_value = 0
        return mock_proc

    monkeypatch.setattr(srv.subprocess, "Popen", _fake_popen)
    return srv, captured, tmp_path


def _make_run_dir(root, run_id, with_ts=True):
    """Cree un faux run dir avec clean_steps.json et optionnellement TS."""
    ts = "20260829_120000"
    d = root / f"{ts}_{run_id}"
    d.mkdir()
    (d / "clean_steps.json").write_text(json.dumps({
        "schema_version": "1.0",
        "parcours": "test", "steps": [{"action": "click", "selector": "#x"}],
    }), encoding="utf-8")
    if with_ts:
        (d / "test_playwright.spec.ts").write_text("// fake spec", encoding="utf-8")
    return d


def test_replay_uses_playwright_ts_when_spec_present(app_with_stub_popen):
    """#12 + #13 : run avec test_playwright.spec.ts -> npx playwright test,
    aucune reference a qa_player.py."""
    srv, captured, tmp_path = app_with_stub_popen
    _make_run_dir(tmp_path, "abc123", with_ts=True)
    client = TestClient(srv.app)
    resp = client.post("/api/replay/abc123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "playwright_ts"
    assert body["legacy_fallback"] is False
    # Verifier ce qui a ete lance
    assert len(captured["calls"]) == 1
    call = captured["calls"][0]
    cmd_repr = str(call["args"])
    assert "playwright" in cmd_repr
    assert "test" in cmd_repr
    assert "qa_player.py" not in cmd_repr


def test_replay_falls_back_to_qa_player_when_no_ts(app_with_stub_popen):
    """#14 : run sans test_playwright.spec.ts -> fallback qa_player.py
    explicite, avec legacy_fallback_reason non vide."""
    srv, captured, tmp_path = app_with_stub_popen
    _make_run_dir(tmp_path, "old123", with_ts=False)
    client = TestClient(srv.app)
    resp = client.post("/api/replay/old123")
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine"] == "qa_player_legacy"
    assert body["legacy_fallback"] is True
    assert body["legacy_fallback_reason"]
    # Verifier qu'on lance bien qa_player
    cmd_repr = str(captured["calls"][0]["args"])
    assert "qa_player.py" in cmd_repr


def test_replay_passes_relative_spec_path_not_absolute_windows(app_with_stub_popen):
    """#15 : le chemin du spec passe a `npx playwright test` doit etre
    RELATIF au repo. Un chemin absolu Windows (C:\\...) serait interprete
    comme expression de filtrage par Playwright a cause du ":"."""
    srv, captured, tmp_path = app_with_stub_popen
    _make_run_dir(tmp_path, "abc456", with_ts=True)
    client = TestClient(srv.app)
    resp = client.post("/api/replay/abc456")
    assert resp.status_code == 200
    # Sur Windows Popen recoit une string (shell=True), sur Linux une list
    cmd_arg = captured["calls"][0]["args"]
    if isinstance(cmd_arg, str):
        # Windows : verifier que le path est POSIX-slash et sans ":"
        # (le "C:" du chemin absolu Windows)
        assert "test_playwright.spec.ts" in cmd_arg
        # Le chemin doit etre relative-style (pas commencer par un drive letter)
        assert not any(cmd_arg.find(f'{drive}:') > 0 for drive in "CDEF")
    else:
        # Linux : dans la liste, aucun element ne doit contenir C:
        for arg in cmd_arg:
            assert not arg.startswith("/mnt/") or arg.startswith("/mnt/c")  # tolere WSL
            assert ":" not in arg or arg.startswith("--")


def test_replay_uses_workers_1_for_deterministic(app_with_stub_popen):
    """Cahier : "Utilise un seul worker pour le replay deterministe.""" ""
    srv, captured, tmp_path = app_with_stub_popen
    _make_run_dir(tmp_path, "det123", with_ts=True)
    client = TestClient(srv.app)
    client.post("/api/replay/det123")
    cmd_repr = str(captured["calls"][0]["args"])
    assert "--workers=1" in cmd_repr


def test_replay_404_on_unknown_run(app_with_stub_popen):
    srv, captured, tmp_path = app_with_stub_popen
    client = TestClient(srv.app)
    resp = client.post("/api/replay/inexistent")
    assert resp.status_code == 404


def test_replay_400_when_clean_steps_missing(app_with_stub_popen):
    srv, captured, tmp_path = app_with_stub_popen
    d = tmp_path / "20260829_120000_bad"
    d.mkdir()
    # Pas de clean_steps.json
    client = TestClient(srv.app)
    resp = client.post("/api/replay/bad")
    assert resp.status_code == 400


def test_replay_writes_initial_meta_with_engine(app_with_stub_popen):
    """meta.json initial doit contenir engine + is_replay + source_run_id
    des le POST, avant meme la fin du subprocess."""
    srv, captured, tmp_path = app_with_stub_popen
    _make_run_dir(tmp_path, "meta1", with_ts=True)
    client = TestClient(srv.app)
    resp = client.post("/api/replay/meta1")
    body = resp.json()
    replay_dir = tmp_path / body["replay_dir"]
    meta_file = replay_dir / "meta.json"
    assert meta_file.exists()
    meta = json.loads(meta_file.read_text(encoding="utf-8"))
    assert meta["engine"] == "playwright_ts"
    assert meta["is_replay"] is True
    assert meta["source_run_id"] == "meta1"


def test_path_traversal_rejected_on_file_endpoint(app_with_stub_popen):
    """#15 (securite chemin) : /api/run/{id}/file/ rejette les path
    traversals (heritage : cette securite existait deja, test le maintien
    apres refactor pour eviter regression)."""
    srv, captured, tmp_path = app_with_stub_popen
    _make_run_dir(tmp_path, "sec1", with_ts=True)
    client = TestClient(srv.app)
    # Path traversal via ".."
    resp = client.get("/api/run/sec1/file/..%2F..%2Fetc%2Fpasswd")
    # FastAPI decode l'URL avant le handler, le check "/" ou "\\" doit rejeter
    assert resp.status_code in (400, 404)
