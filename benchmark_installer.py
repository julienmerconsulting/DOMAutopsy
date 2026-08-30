"""
DOMAutopsy - Installation et gestion du corpus BU_Bench_V1.
==============================================================
Clone le repo officiel browser-use/benchmark dans un cache local
GITIGNORE, fige le commit utilise dans un manifest, verifie la
presence de BU_Bench_V1.enc, installe la dep optionnelle
cryptography si absente.

Regle stricte : le fichier .enc reste chiffre sur disque. Le
dechiffrement se fait UNIQUEMENT en memoire au moment du run
(voir benchmark_runner.load_bu_bench_v1_in_memory).

Aucune tache dechiffree n'est ecrite sur disque.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).parent
CACHE_DIR = ROOT / ".bu_bench_cache"
CLONE_DIR = CACHE_DIR / "benchmark"
MANIFEST_PATH = CACHE_DIR / "manifest.json"
ENC_RELATIVE_PATHS = (
    "BU_Bench_V1.enc",
    "tasks/BU_Bench_V1.enc",
    "data/BU_Bench_V1.enc",
    "benchmarks/BU_Bench_V1.enc",
    "eval/BU_Bench_V1.enc",
)


class BenchmarkInstallError(Exception):
    """Erreur explicite pendant install/status du corpus benchmark."""


def _run(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """subprocess helper cross-OS, capture stdout/stderr en UTF-8."""
    result = subprocess.run(
        cmd, cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if check and result.returncode != 0:
        raise BenchmarkInstallError(
            f"Command failed ({' '.join(cmd)}) : exit {result.returncode}\n"
            f"stdout: {result.stdout[-500:]}\nstderr: {result.stderr[-500:]}"
        )
    return result


def _ensure_cryptography() -> None:
    """Installe la dependance optionnelle 'cryptography' si absente.
    Cette lib est OPTIONNELLE de DOMAutopsy (pas dans requirements.txt
    strict) mais REQUISE pour dechiffrer BU_Bench_V1.enc via Fernet."""
    try:
        import cryptography  # noqa: F401
        return
    except ImportError:
        pass
    print("[benchmark] cryptography absent, installation...")
    result = _run(
        [sys.executable, "-m", "pip", "install", "cryptography"],
        check=False,
    )
    if result.returncode != 0:
        raise BenchmarkInstallError(
            "Impossible d'installer cryptography. Installer manuellement :\n"
            f"  {sys.executable} -m pip install cryptography\n"
            f"Sortie pip:\n{result.stdout}\n{result.stderr}"
        )
    print("[benchmark] cryptography installe.")


def _find_enc_path() -> Path | None:
    """Cherche BU_Bench_V1.enc dans les chemins probables du clone."""
    for rel in ENC_RELATIVE_PATHS:
        p = CLONE_DIR / rel
        if p.exists():
            return p
    # Recherche recursive si non trouve aux endroits usuels
    for p in CLONE_DIR.rglob("BU_Bench_V1.enc"):
        return p
    return None


def install(source: str = "bu-v1", force: bool = False) -> dict[str, Any]:
    """Installe le corpus BU_Bench_V1 :
    1. Clone browser-use/benchmark dans .bu_bench_cache/benchmark/
    2. Fige le commit dans .bu_bench_cache/manifest.json
    3. Verifie la presence de BU_Bench_V1.enc
    4. Installe cryptography si absent
    """
    if source != "bu-v1":
        raise BenchmarkInstallError(f"Source '{source}' non supportee. Attendu : 'bu-v1'.")

    CACHE_DIR.mkdir(exist_ok=True, parents=True)
    _ensure_cryptography()

    repo_url = "https://github.com/browser-use/benchmark.git"
    if CLONE_DIR.exists() and force:
        import shutil
        print(f"[benchmark] --force : suppression cache {CLONE_DIR}")
        shutil.rmtree(CLONE_DIR)

    if not CLONE_DIR.exists():
        print(f"[benchmark] Clone {repo_url} -> {CLONE_DIR}...")
        _run(["git", "clone", "--depth", "1", repo_url, str(CLONE_DIR)])
    else:
        print(f"[benchmark] Cache existant : {CLONE_DIR}")

    # Fige le commit courant
    head = _run(["git", "rev-parse", "HEAD"], cwd=CLONE_DIR).stdout.strip()
    branch = _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=CLONE_DIR, check=False).stdout.strip() or "unknown"

    enc_path = _find_enc_path()
    if enc_path is None:
        raise BenchmarkInstallError(
            f"BU_Bench_V1.enc introuvable dans {CLONE_DIR}. "
            f"Le repo a peut-etre change de layout. Reessayer avec --force."
        )

    manifest = {
        "source": "browser-use/benchmark",
        "url": repo_url,
        "commit": head,
        "branch": branch,
        "enc_path": str(enc_path.relative_to(CACHE_DIR)),
        "installed_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[benchmark] Manifest : {MANIFEST_PATH.relative_to(ROOT)}")
    print(f"[benchmark] Commit fige : {head} ({branch})")
    print(f"[benchmark] Enc file : {enc_path.relative_to(CACHE_DIR)}")
    print(f"[benchmark] OK")
    return manifest


def status() -> dict[str, Any]:
    """Retourne l'etat du corpus (installe / commit / enc present)."""
    installed = MANIFEST_PATH.exists()
    manifest = None
    enc_present = False
    enc_size = None
    if installed:
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            manifest = {"_parse_error": True}
        enc = _find_enc_path()
        if enc:
            enc_present = True
            enc_size = enc.stat().st_size
    return {
        "installed": installed and enc_present,
        "cache_dir": str(CACHE_DIR),
        "clone_dir": str(CLONE_DIR),
        "manifest": manifest,
        "enc_present": enc_present,
        "enc_size_bytes": enc_size,
        "cryptography_available": _cryptography_available(),
    }


def _cryptography_available() -> bool:
    try:
        import cryptography  # noqa: F401
        return True
    except ImportError:
        return False


def get_enc_path() -> Path:
    """Retourne le chemin absolu du .enc. Leve si non installe."""
    p = _find_enc_path()
    if p is None:
        raise BenchmarkInstallError(
            "BU_Bench_V1.enc absent. Lancer d'abord :\n"
            "  python domautopsy_cli.py benchmark install --source bu-v1"
        )
    return p


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser("install")
    p_install.add_argument("--source", default="bu-v1")
    p_install.add_argument("--force", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args()
    if args.cmd == "install":
        install(source=args.source, force=args.force)
    elif args.cmd == "status":
        print(json.dumps(status(), indent=2, ensure_ascii=False))
