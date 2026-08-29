"""
DOMAutopsy - Installateur du runtime autonome (Node + Playwright + Chromium)
=============================================================================
Provisionne l'ecosysteme Node dans DOMAutopsy/runtime/ pour que
/api/replay puisse tourner sans dependre du `npx` global ni du cache
Playwright utilisateur.

Layout produit :
    runtime/
    ├── node/
    │   ├── node.exe             # Windows
    │   ├── npm.cmd
    │   └── npx.cmd
    ├── node_modules/@playwright/test/cli.js
    └── browsers/
        └── chromium-XXXX/       # binaire cible sur channel:'chromium'

Idempotent : ne re-telecharge pas ce qui est deja en place et cohabite
avec le manifest runtime_manifest.json (traceabilite des versions).

Versions cibles : figees dans MANIFEST_TARGET ci-dessous pour cohabitation
avec browser-use==0.12.9 + playwright==1.57.0 (Python) sans risque
d'upgrade silencieux.

Commandes CLI (via domautopsy_cli.py) :
    python domautopsy_cli.py runtime install [--force]
    python domautopsy_cli.py runtime status
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional


ROOT = Path(__file__).parent
RUNTIME_DIR = ROOT / "runtime"
NODE_DIR = RUNTIME_DIR / "node"
NODE_MODULES_DIR = RUNTIME_DIR / "node_modules"
BROWSERS_DIR = RUNTIME_DIR / "browsers"
MANIFEST_PATH = RUNTIME_DIR / "runtime_manifest.json"


# Versions cibles - alignees avec le package-lock.json + Chromium cache
# Playwright Python 1.57 (chromium-1234). Mises a jour uniquement quand
# la stack Python bouge, jamais silencieusement.
MANIFEST_TARGET = {
    "node": {
        "version": "20.18.1",  # LTS, stable, Playwright 1.49+ OK
    },
    "playwright_test": {
        # Version du @playwright/test dans package-lock.json.
        # Verifie dynamiquement au status via runtime/node_modules.
    },
    "chromium_channel": "chromium",  # channel: 'chromium' dans playwright.config.ts
}


# ============================================================
# Downloads verifies
# ============================================================

def _detect_arch() -> str:
    """Retourne l'archetype cible : win-x64, linux-x64, darwin-x64, darwin-arm64."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        return "win-x64"
    if system == "linux":
        return "linux-x64" if machine in ("x86_64", "amd64") else f"linux-{machine}"
    if system == "darwin":
        return "darwin-arm64" if machine == "arm64" else "darwin-x64"
    raise RuntimeError(f"Plateforme non supportee : {system}/{machine}")


def _node_download_url(version: str, arch: str) -> tuple[str, str]:
    """Retourne (url, ext). Node distribution officielle nodejs.org."""
    base = f"https://nodejs.org/dist/v{version}"
    if arch == "win-x64":
        return f"{base}/node-v{version}-win-x64.zip", "zip"
    if arch == "linux-x64":
        return f"{base}/node-v{version}-linux-x64.tar.xz", "tar.xz"
    if arch == "darwin-x64":
        return f"{base}/node-v{version}-darwin-x64.tar.gz", "tar.gz"
    if arch == "darwin-arm64":
        return f"{base}/node-v{version}-darwin-arm64.tar.gz", "tar.gz"
    raise RuntimeError(f"URL Node non definie pour arch={arch}")


def _download(url: str, out_path: Path, progress_prefix: str = "") -> None:
    """Telecharge url vers out_path avec progress bar minimale."""
    print(f"  {progress_prefix}Telechargement : {url}")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".part")
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            with open(tmp, "wb") as f:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = int(downloaded * 100 / total)
                        sys.stdout.write(f"\r  {progress_prefix}  {pct}% ({downloaded // 1024} / {total // 1024} KB)")
                        sys.stdout.flush()
        print()
        tmp.rename(out_path)
    except urllib.error.URLError as e:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"Echec telechargement {url} : {e}")


def _sha256(path: Path) -> str:
    """Calcule le SHA-256 d'un fichier. Sert pour la verification checksum
    quand un manifest de reference l'expose."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract(archive: Path, dest: Path) -> None:
    """Extrait un .zip ou .tar.* dans dest."""
    dest.mkdir(parents=True, exist_ok=True)
    name = archive.name.lower()
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(dest)
    elif name.endswith(".tar.xz") or name.endswith(".tar.gz"):
        import tarfile
        mode = "r:xz" if name.endswith(".xz") else "r:gz"
        with tarfile.open(archive, mode) as tf:
            tf.extractall(dest)
    else:
        raise RuntimeError(f"Format d'archive non supporte : {archive.name}")


# ============================================================
# Node installation
# ============================================================

def install_node(force: bool = False) -> Path:
    """Telecharge et extrait Node dans runtime/node/. Retourne le chemin
    de node.exe (ou node sous Linux/macOS). Idempotent : skip si deja OK."""
    node_bin = NODE_DIR / ("node.exe" if platform.system().lower() == "windows" else "bin/node")
    if node_bin.exists() and not force:
        print(f"  [SKIP] Node deja present : {node_bin}")
        return node_bin

    if NODE_DIR.exists():
        print(f"  Suppression de l'ancien NODE_DIR ({NODE_DIR})...")
        shutil.rmtree(NODE_DIR)

    arch = _detect_arch()
    version = MANIFEST_TARGET["node"]["version"]
    url, ext = _node_download_url(version, arch)

    with tempfile.TemporaryDirectory(prefix="domautopsy-node-") as tmpdir:
        archive = Path(tmpdir) / f"node-v{version}.{ext}"
        _download(url, archive, "  [Node]  ")

        extract_dir = Path(tmpdir) / "extracted"
        _extract(archive, extract_dir)

        # Node extrait dans <arch>-node-vX.Y.Z/ selon la distribution
        # Windows : node-vXX-win-x64/  Linux : node-vXX-linux-x64/  etc.
        sub = next(extract_dir.iterdir())
        NODE_DIR.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(sub), str(NODE_DIR))

    if not node_bin.exists():
        raise RuntimeError(f"Node installe mais binaire attendu manquant : {node_bin}")
    print(f"  [OK] Node v{version} installe : {node_bin}")
    return node_bin


def install_playwright_and_browser(node_bin: Path) -> tuple[Path, Path]:
    """Installe @playwright/test dans runtime/node_modules via `npm ci` puis
    telecharge Chromium via `playwright install chromium` avec
    PLAYWRIGHT_BROWSERS_PATH pointe sur runtime/browsers.

    Retourne (playwright_cli.js path, browsers_path).
    """
    # 1. Copier package.json + package-lock.json dans runtime/ pour un
    # npm ci isole (n'affecte pas le node_modules eventuel du repo)
    for f in ("package.json", "package-lock.json"):
        src = ROOT / f
        if not src.exists():
            raise RuntimeError(f"{f} manquant dans le repo : impossible de bootstrap")
        shutil.copy2(src, RUNTIME_DIR / f)

    # 2. npm ci dans runtime/
    system = platform.system().lower()
    npm = NODE_DIR / ("npm.cmd" if system == "windows" else "bin/npm")
    if not npm.exists():
        raise RuntimeError(f"npm attendu manquant : {npm}")

    print(f"  [Playwright] npm ci dans {RUNTIME_DIR}...")
    env = dict(os.environ)
    # Force npm a utiliser le node embarque
    if system == "windows":
        env["PATH"] = str(NODE_DIR) + os.pathsep + env.get("PATH", "")
    else:
        env["PATH"] = str(NODE_DIR / "bin") + os.pathsep + env.get("PATH", "")

    result = subprocess.run(
        [str(npm), "ci"], cwd=str(RUNTIME_DIR), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"npm ci a echoue (code {result.returncode}) :\n"
            f"stdout: {result.stdout[-1500:]}\nstderr: {result.stderr[-1500:]}"
        )
    print(f"  [OK] @playwright/test installe dans runtime/node_modules/")

    # 3. Localiser cli.js
    playwright_cli = RUNTIME_DIR / "node_modules" / "@playwright" / "test" / "cli.js"
    if not playwright_cli.exists():
        raise RuntimeError(f"cli.js attendu manquant : {playwright_cli}")

    # 4. Telecharger Chromium via playwright install, avec browsers path isole
    print(f"  [Chromium] Telechargement via playwright install...")
    BROWSERS_DIR.mkdir(parents=True, exist_ok=True)
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS_DIR)
    result = subprocess.run(
        [str(node_bin), str(playwright_cli), "install", "chromium"],
        cwd=str(RUNTIME_DIR), env=env,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"playwright install chromium a echoue (code {result.returncode}) :\n"
            f"stdout: {result.stdout[-1500:]}\nstderr: {result.stderr[-1500:]}"
        )
    print(f"  [OK] Chromium installe dans {BROWSERS_DIR}")

    return playwright_cli, BROWSERS_DIR


# ============================================================
# Manifest + status
# ============================================================

def _read_playwright_version_from_node_modules() -> Optional[str]:
    """Lit la version reelle de @playwright/test installee dans runtime/."""
    pkg = RUNTIME_DIR / "node_modules" / "@playwright" / "test" / "package.json"
    if not pkg.exists():
        return None
    try:
        return json.loads(pkg.read_text(encoding="utf-8")).get("version")
    except Exception:
        return None


def _read_node_version(node_bin: Path) -> Optional[str]:
    """Interroge node.exe --version."""
    if not node_bin.exists():
        return None
    try:
        result = subprocess.run(
            [str(node_bin), "--version"], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10,
        )
        return result.stdout.strip().lstrip("v") if result.returncode == 0 else None
    except Exception:
        return None


def _list_installed_chromium_versions() -> list[str]:
    """Liste les dossiers chromium-XXXX presents dans runtime/browsers/."""
    if not BROWSERS_DIR.exists():
        return []
    return sorted(
        d.name for d in BROWSERS_DIR.iterdir()
        if d.is_dir() and d.name.startswith("chromium-")
    )


def write_manifest(node_bin: Path, playwright_cli: Path, browsers_path: Path) -> None:
    """Ecrit runtime_manifest.json avec les versions REELLEMENT installees
    (pas les targets), pour traceabilite et debug."""
    manifest = {
        "installed_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "arch": _detect_arch(),
        "node": {
            "version": _read_node_version(node_bin),
            "target_version": MANIFEST_TARGET["node"]["version"],
            "path": str(node_bin.relative_to(ROOT)),
            "sha256": _sha256(node_bin) if node_bin.stat().st_size < 100 * 1024 * 1024 else "skipped_too_large",
        },
        "playwright_test": {
            "version": _read_playwright_version_from_node_modules(),
            "cli_path": str(playwright_cli.relative_to(ROOT)),
        },
        "chromium": {
            "channel": MANIFEST_TARGET["chromium_channel"],
            "installed_versions": _list_installed_chromium_versions(),
            "browsers_path": str(browsers_path.relative_to(ROOT)),
        },
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"  Manifest ecrit : {MANIFEST_PATH.relative_to(ROOT)}")


def status() -> dict:
    """Retourne l'etat du runtime autonome, exploitable en JSON pour CI ou UI."""
    system = platform.system().lower()
    node_bin = NODE_DIR / ("node.exe" if system == "windows" else "bin/node")
    playwright_cli = RUNTIME_DIR / "node_modules" / "@playwright" / "test" / "cli.js"
    manifest = None
    if MANIFEST_PATH.exists():
        try:
            manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
        except Exception:
            manifest = {"parse_error": True}
    chromiums = _list_installed_chromium_versions()
    complete = node_bin.exists() and playwright_cli.exists() and bool(chromiums)
    return {
        "runtime_dir": str(RUNTIME_DIR),
        "complete": complete,
        "node": {
            "installed": node_bin.exists(),
            "version": _read_node_version(node_bin),
            "target": MANIFEST_TARGET["node"]["version"],
            "path": str(node_bin) if node_bin.exists() else None,
        },
        "playwright_test": {
            "installed": playwright_cli.exists(),
            "version": _read_playwright_version_from_node_modules(),
            "cli_path": str(playwright_cli) if playwright_cli.exists() else None,
        },
        "chromium": {
            "installed_versions": chromiums,
            "browsers_path": str(BROWSERS_DIR) if BROWSERS_DIR.exists() else None,
        },
        "manifest": manifest,
        "recommended_env_vars": {
            "DOMAUTOPSY_NODE_PATH": str(node_bin.relative_to(ROOT)) if node_bin.exists() else "(runtime absent)",
            "DOMAUTOPSY_PLAYWRIGHT_CLI": str(playwright_cli.relative_to(ROOT)) if playwright_cli.exists() else "(runtime absent)",
            "DOMAUTOPSY_BROWSERS_PATH": str(BROWSERS_DIR.relative_to(ROOT)) if BROWSERS_DIR.exists() else "(runtime absent)",
        },
    }


# ============================================================
# Entry point orchestration
# ============================================================

def install(force: bool = False) -> dict:
    """Orchestration complete : Node -> Playwright -> Chromium -> manifest.
    Idempotent : reprend la ou ca s'etait arrete."""
    print(f"\n{'=' * 60}\n  DOMAutopsy - Install runtime autonome dans {RUNTIME_DIR}\n{'=' * 60}\n")
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    node_bin = install_node(force=force)
    playwright_cli, browsers_path = install_playwright_and_browser(node_bin)
    write_manifest(node_bin, playwright_cli, browsers_path)
    st = status()
    print(f"\n{'=' * 60}")
    print(f"  Runtime {'COMPLET' if st['complete'] else 'INCOMPLET'}")
    print(f"{'=' * 60}\n")
    print(f"  Pour activer dans .env :")
    for k, v in st["recommended_env_vars"].items():
        print(f"    {k}={v}")
    print()
    return st


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="DOMAutopsy runtime installer")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_install = sub.add_parser("install", help="Provisionne runtime/node + Playwright + Chromium")
    p_install.add_argument("--force", action="store_true", help="Re-telecharge meme si deja present")
    sub.add_parser("status", help="Affiche l'etat du runtime en JSON")
    args = parser.parse_args()
    if args.cmd == "install":
        install(force=args.force)
    elif args.cmd == "status":
        print(json.dumps(status(), indent=2, ensure_ascii=False))
