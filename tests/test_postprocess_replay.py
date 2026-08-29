"""Tests pour _postprocess_replay et update_replay_meta_with_verdict.

Couvre le fix D6 (meta bloqué "running") :
- replay_results.json present + valide -> verdict extrait, status=success/failure
- replay_results.json absent + returncode!=0 -> status=crashed + replay_error
- replay_results.json absent + returncode==0 -> status=unknown + replay_error
- replay_results.json corrompu -> fallback sur returncode
"""
import json
import pytest
from pathlib import Path

from replay_reporter import update_replay_meta_with_verdict


def _write_meta(replay_dir: Path, extra=None):
    meta = {
        "timestamp": "20260101_120000",
        "is_replay": True,
        "engine": "playwright_ts",
        "source_run_id": "abc123",
        "status": "running",
    }
    if extra:
        meta.update(extra)
    (replay_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")


def _write_results(replay_dir: Path, passed=1, failed=0, skipped=0, duration=1000):
    (replay_dir / "replay_results.json").write_text(json.dumps({
        "stats": {"expected": passed, "unexpected": failed, "skipped": skipped, "duration": duration},
        "suites": [],
    }), encoding="utf-8")


def test_meta_success_when_results_ok(tmp_path):
    """results.json + returncode=0 -> status=success + counts extraits."""
    _write_meta(tmp_path)
    _write_results(tmp_path, passed=3, failed=0)
    meta = update_replay_meta_with_verdict(tmp_path, subprocess_returncode=0)
    assert meta["status"] == "success"
    assert meta["replay_passed"] == 3
    assert meta["replay_failed"] == 0


def test_meta_failure_when_playwright_reports_failures(tmp_path):
    """results.json avec failures > 0 -> status=failure."""
    _write_meta(tmp_path)
    _write_results(tmp_path, passed=1, failed=2)
    meta = update_replay_meta_with_verdict(tmp_path, subprocess_returncode=1)
    assert meta["status"] == "failure"
    assert meta["replay_failed"] == 2


def test_meta_crashed_when_no_results_and_returncode_nonzero(tmp_path):
    """D6 : Playwright crash avant results.json + exit != 0 -> status=crashed."""
    _write_meta(tmp_path)
    # PAS de replay_results.json
    meta = update_replay_meta_with_verdict(tmp_path, subprocess_returncode=137)
    assert meta["status"] == "crashed"
    assert "replay_error" in meta
    assert "137" in meta["replay_error"]
    assert meta["replay_exit_code"] == 137


def test_meta_unknown_when_no_results_and_returncode_zero(tmp_path):
    """Cas theorique : exit 0 mais aucun results.json (Playwright a rien
    detecte). Status=unknown + explication."""
    _write_meta(tmp_path)
    meta = update_replay_meta_with_verdict(tmp_path, subprocess_returncode=0)
    assert meta["status"] == "unknown"
    assert "replay_error" in meta
    assert meta["replay_exit_code"] == 0


def test_meta_crashed_when_returncode_none(tmp_path):
    """returncode inconnu (proc pas encore termine ?) et pas de results ->
    status=crashed avec message explicite."""
    _write_meta(tmp_path)
    meta = update_replay_meta_with_verdict(tmp_path, subprocess_returncode=None)
    assert meta["status"] == "crashed"
    assert "inconnu" in meta.get("replay_error", "")


def test_meta_fallback_when_results_json_corrupted(tmp_path):
    """results.json present mais parse fail -> fallback sur returncode."""
    _write_meta(tmp_path)
    (tmp_path / "replay_results.json").write_text("{not valid json", encoding="utf-8")
    meta = update_replay_meta_with_verdict(tmp_path, subprocess_returncode=0)
    assert "replay_parse_error" in meta
    assert meta["status"] == "success"  # fallback returncode 0


def test_meta_none_when_meta_file_missing(tmp_path):
    """Pas de meta.json de depart -> retourne None (rien a mettre a jour)."""
    result = update_replay_meta_with_verdict(tmp_path)
    assert result is None
