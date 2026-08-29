"""Verification que requirements.txt est reellement installable et
consistent avec les contraintes upstream (notamment browser-use).

Repond au gap souleve dans la review du refactor Aout 2026 :
"les 49 tests peuvent passer sans jamais verifier que les
dependances de production sont installables ensemble".

Deux tests :
  1. Parse requirements.txt et compare CHAQUE pin avec les Requires-Dist
     officielles de browser-use==0.12.9 (via importlib.metadata). Rapide,
     sans reseau. Detecte le cas exact qu'on a rate avant : openai==2.41
     dans req.txt vs browser-use qui demande ==2.16.
  2. Optionnel (marker slow) : `pip install --dry-run -r requirements.txt`
     qui simule une install fresh et laisse pip resoudre tout l'arbre.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parent.parent
REQUIREMENTS = ROOT / "requirements.txt"


def _parse_requirements(path: Path) -> dict[str, str]:
    """Extrait {package_name.lower(): version_pin} depuis un requirements.txt.
    Ignore commentaires, lignes vides, options (-r, --extra-index, etc).
    Ne gere que les pins ==X.Y.Z (pas les >=, ~=, etc.)."""
    pins: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        # Nom+extra optionnels : uvicorn[standard]==0.40.0
        m = re.match(r"^([A-Za-z0-9_.-]+)(\[[^\]]+\])?\s*==\s*([^\s;]+)", line)
        if not m:
            continue
        name = m.group(1).lower()
        version = m.group(3).strip()
        pins[name] = version
    return pins


def _parse_browser_use_requires() -> dict[str, str]:
    """Extrait les Requires-Dist strict pins de browser-use installe."""
    try:
        import importlib.metadata as m
    except ImportError:
        pytest.skip("importlib.metadata absent")
    try:
        dist = m.distribution("browser-use")
    except m.PackageNotFoundError:
        pytest.skip("browser-use pas installe dans l'env de test")

    strict_pins: dict[str, str] = {}
    for req in (dist.metadata.get_all("Requires-Dist") or []):
        # Ignore les extras (contiennent "extra == '...'")
        if "extra ==" in req:
            continue
        # Match nom==version au debut
        m2 = re.match(r"^([A-Za-z0-9_.-]+)\s*==\s*([^\s;]+)", req)
        if not m2:
            continue
        strict_pins[m2.group(1).lower()] = m2.group(2).strip()
    return strict_pins


def test_requirements_pins_satisfy_browser_use_constraints():
    """Chaque pin de requirements.txt qui coexiste avec un Requires-Dist
    strict de browser-use doit avoir la meme version. Detecte
    immediatement une regression du type openai==2.41 vs browser-use qui
    demande ==2.16."""
    pins = _parse_requirements(REQUIREMENTS)
    if "browser-use" not in pins:
        pytest.skip("browser-use pas dans requirements.txt (test non applicable)")
    bu_pins = _parse_browser_use_requires()

    conflicts = []
    for pkg, our_version in pins.items():
        upstream_version = bu_pins.get(pkg)
        if upstream_version is None:
            continue
        if our_version != upstream_version:
            conflicts.append(
                f"  - {pkg} : requirements.txt pin '{our_version}', "
                f"browser-use exige '{upstream_version}'"
            )
    assert not conflicts, (
        "requirements.txt contient des pins incompatibles avec les "
        "contraintes strictes de browser-use :\n" + "\n".join(conflicts)
        + "\n\nFix : aligner les valeurs OU downgrade browser-use."
    )


def test_requirements_has_core_pins():
    """Sanity : les packages critiques doivent etre epingles STRICT (==),
    pas >= ni ~= (pour reproductibilite fresh clone)."""
    pins = _parse_requirements(REQUIREMENTS)
    # Ces packages doivent etre epingles strict par requirements.txt
    critical = ("browser-use", "playwright", "openai", "fastapi", "pydantic")
    missing = [p for p in critical if p not in pins]
    assert not missing, (
        f"Packages critiques non epingles avec == dans requirements.txt : "
        f"{missing}. Le pinning strict est requis pour la reproductibilite."
    )


@pytest.mark.slow
def test_requirements_pip_install_dry_run():
    """Test lourd (~30s+) : demande a pip de resoudre tout requirements.txt
    en mode --dry-run (pas d'install effective mais telecharge les
    metadonnees et resout l'arbre complet). Detecte les conflits
    transitifs qui echapperaient au test statique ci-dessus.

    Marquer @slow pour skip par defaut : pytest tests/ -m slow pour le
    lancer explicitement."""
    result = subprocess.run(
        [sys.executable, "-m", "pip", "install", "--dry-run", "--quiet",
         "-r", str(REQUIREMENTS)],
        capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=180,
    )
    assert result.returncode == 0, (
        f"pip install --dry-run a echoue (exit {result.returncode}) :\n"
        f"stdout: {(result.stdout or '')[-1500:]}\n"
        f"stderr: {(result.stderr or '')[-1500:]}"
    )
