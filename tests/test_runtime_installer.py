"""Tests unitaires pour runtime_installer sans telecharger de binaires.

Couvre :
- _detect_arch() : platform detection
- status() : retourne bien la structure attendue avec les 3 briques
- _resolve_embedded_runtime() (dans server.py) : cas fichiers presents vs absents
- write_manifest() puis relecture via status() : cohérence
"""
import json
import os
import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from runtime_installer import (
    _detect_arch,
    status,
    MANIFEST_TARGET,
)


def test_detect_arch_returns_supported_platform():
    """_detect_arch retourne l'un des 4 patterns supportes."""
    arch = _detect_arch()
    assert arch in ("win-x64", "linux-x64", "darwin-x64", "darwin-arm64") or arch.startswith("linux-")


def test_status_incomplete_when_runtime_absent(tmp_path, monkeypatch):
    """status() sur un runtime vide/absent retourne complete=False + info coherente."""
    import runtime_installer as ri
    # Isole les paths sur tmp_path
    monkeypatch.setattr(ri, "ROOT", tmp_path)  # relative_to base
    monkeypatch.setattr(ri, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(ri, "NODE_DIR", tmp_path / "runtime" / "node")
    monkeypatch.setattr(ri, "BROWSERS_DIR", tmp_path / "runtime" / "browsers")
    monkeypatch.setattr(ri, "MANIFEST_PATH", tmp_path / "runtime" / "runtime_manifest.json")

    st = ri.status()
    assert st["complete"] is False
    assert st["node"]["installed"] is False
    assert st["playwright_test"]["installed"] is False
    assert st["chromium"]["installed_versions"] == []
    assert st["manifest"] is None
    # recommended_env_vars doit dire "(runtime absent)"
    for v in st["recommended_env_vars"].values():
        assert "absent" in v


def test_status_reads_manifest_when_present(tmp_path, monkeypatch):
    """Un manifest existant est lu et inclus dans le status."""
    import runtime_installer as ri
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setattr(ri, "ROOT", tmp_path)
    monkeypatch.setattr(ri, "RUNTIME_DIR", runtime)
    monkeypatch.setattr(ri, "NODE_DIR", runtime / "node")
    monkeypatch.setattr(ri, "BROWSERS_DIR", runtime / "browsers")
    monkeypatch.setattr(ri, "MANIFEST_PATH", runtime / "runtime_manifest.json")

    fake_manifest = {
        "installed_at": "2026-01-01T00:00:00",
        "arch": "win-x64",
        "node": {"version": "20.18.1"},
    }
    (runtime / "runtime_manifest.json").write_text(json.dumps(fake_manifest), encoding="utf-8")

    st = ri.status()
    assert st["manifest"] == fake_manifest


def test_status_detects_installed_chromium_versions(tmp_path, monkeypatch):
    """Chromium-XXXX presents dans browsers/ sont listes."""
    import runtime_installer as ri
    browsers = tmp_path / "runtime" / "browsers"
    browsers.mkdir(parents=True)
    (browsers / "chromium-1234").mkdir()
    (browsers / "chromium-1200").mkdir()
    (browsers / "not-chromium").mkdir()  # ne doit pas apparaitre

    monkeypatch.setattr(ri, "ROOT", tmp_path)  # necessaire pour relative_to
    monkeypatch.setattr(ri, "RUNTIME_DIR", tmp_path / "runtime")
    monkeypatch.setattr(ri, "BROWSERS_DIR", browsers)
    monkeypatch.setattr(ri, "NODE_DIR", tmp_path / "runtime" / "node")
    monkeypatch.setattr(ri, "MANIFEST_PATH", tmp_path / "runtime" / "runtime_manifest.json")

    st = ri.status()
    assert "chromium-1200" in st["chromium"]["installed_versions"]
    assert "chromium-1234" in st["chromium"]["installed_versions"]
    assert "not-chromium" not in st["chromium"]["installed_versions"]


def test_manifest_target_versions_are_pinned():
    """MANIFEST_TARGET expose au moins Node version + channel Chromium."""
    assert MANIFEST_TARGET["node"]["version"]
    assert MANIFEST_TARGET["chromium_channel"] == "chromium"


# --------------------------------------------------------------------
# _resolve_embedded_runtime (server.py)
# --------------------------------------------------------------------

def test_resolve_embedded_runtime_none_when_env_unset(monkeypatch):
    """Aucune env var -> None (mode dev, fallback npx)."""
    monkeypatch.delenv("DOMAUTOPSY_NODE_PATH", raising=False)
    monkeypatch.delenv("DOMAUTOPSY_PLAYWRIGHT_CLI", raising=False)
    monkeypatch.delenv("DOMAUTOPSY_BROWSERS_PATH", raising=False)
    import server as srv
    assert srv._resolve_embedded_runtime() is None


def test_resolve_embedded_runtime_none_when_files_missing(monkeypatch, tmp_path):
    """Env vars set mais fichiers inexistants -> None + fallback log."""
    monkeypatch.setenv("DOMAUTOPSY_NODE_PATH", "runtime/node/node.exe")
    monkeypatch.setenv("DOMAUTOPSY_PLAYWRIGHT_CLI", "runtime/node_modules/@playwright/test/cli.js")
    monkeypatch.setenv("DOMAUTOPSY_BROWSERS_PATH", "runtime/browsers")
    import server as srv
    monkeypatch.setattr(srv, "ROOT", tmp_path)  # isolate
    # Rien n'existe dans tmp_path
    result = srv._resolve_embedded_runtime()
    assert result is None


def test_resolve_embedded_runtime_returns_paths_when_present(monkeypatch, tmp_path):
    """Env vars set + fichiers presents -> retourne dict avec 3 chemins + mode=embedded."""
    node = tmp_path / "runtime" / "node" / "node.exe"
    cli = tmp_path / "runtime" / "node_modules" / "@playwright" / "test" / "cli.js"
    browsers = tmp_path / "runtime" / "browsers"
    for p in (node, cli):
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("stub")
    browsers.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("DOMAUTOPSY_NODE_PATH", "runtime/node/node.exe")
    monkeypatch.setenv("DOMAUTOPSY_PLAYWRIGHT_CLI", "runtime/node_modules/@playwright/test/cli.js")
    monkeypatch.setenv("DOMAUTOPSY_BROWSERS_PATH", "runtime/browsers")
    import server as srv
    monkeypatch.setattr(srv, "ROOT", tmp_path)

    result = srv._resolve_embedded_runtime()
    assert result is not None
    assert result["mode"] == "embedded"
    assert Path(result["node"]).exists()
    assert Path(result["cli"]).exists()
    assert Path(result["browsers"]).exists()
